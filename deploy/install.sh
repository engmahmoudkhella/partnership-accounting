#!/usr/bin/env bash
# Install / re-install the partnership accounting app on an Ubuntu/Debian server.
# Safe on a server that already hosts other projects:
#   * runs as its own system user on its own local port (default 8077)
#   * adds ONE separate nginx site file and never edits or disables existing sites
#   * without a domain it serves HTTPS on its own port (default 8443) with a self-signed certificate
#   * with DOMAIN=... it uses that name and a free Let's Encrypt certificate
#   * tests the nginx configuration and rolls back its own file if the test fails
#
# Usage (as root, from the cloned repository):
#   bash deploy/install.sh                      # https://SERVER-IP:8443
#   DOMAIN=accounts.example.com bash deploy/install.sh
# Optional: APP_PORT=8077 HTTPS_PORT=8443 EMAIL=you@example.com
set -euo pipefail

APP_NAME=partnership-accounting
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR=/var/lib/$APP_NAME
BACKUP_DIR=/var/backups/$APP_NAME
ENV_FILE=/etc/$APP_NAME.env
APP_USER=pa-accounts
APP_PORT=${APP_PORT:-8077}
HTTPS_PORT=${HTTPS_PORT:-8443}
DOMAIN=${DOMAIN:-}
EMAIL=${EMAIL:-}
NGINX_SITE=/etc/nginx/sites-available/$APP_NAME.conf
NGINX_LINK=/etc/nginx/sites-enabled/$APP_NAME.conf

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash deploy/install.sh)"
command -v apt-get >/dev/null || die "this script supports Ubuntu/Debian (apt-get) only"
case "$APP_DIR" in
  /root/*|/home/*) die "clone the repository under /opt (the service cannot read $APP_DIR): git clone ... /opt/$APP_NAME" ;;
esac

port_busy() { ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }
if ! systemctl is-active --quiet $APP_NAME 2>/dev/null; then
  port_busy "$APP_PORT" && die "port $APP_PORT is already used by another program. Re-run with APP_PORT=<free port>"
fi
if [ -z "$DOMAIN" ] && ! [ -f "$NGINX_SITE" ]; then
  port_busy "$HTTPS_PORT" && die "port $HTTPS_PORT is already used. Re-run with HTTPS_PORT=<free port>"
fi

say "Installing system packages (python, nginx, openssl)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip nginx openssl ca-certificates curl >/dev/null
if [ -n "$DOMAIN" ]; then apt-get install -y -q certbot python3-certbot-nginx >/dev/null; fi
PYV=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
[ "$PYV" -ge 310 ] || die "Python 3.10+ is required (found $(python3 --version))"

say "Creating service user and folders"
id -u $APP_USER >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin $APP_USER
install -d -o $APP_USER -g $APP_USER -m 700 "$DATA_DIR"
install -d -o root -g root -m 700 "$BACKUP_DIR"

say "Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chmod -R a+rX "$APP_DIR"

say "Configuration ($ENV_FILE)"
if [ ! -f "$ENV_FILE" ]; then
  SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  cat > "$ENV_FILE" <<CONF
PA_DATA_DIR=$DATA_DIR
PA_HOST=127.0.0.1
PA_PORT=$APP_PORT
PA_SECRET_KEY=$SECRET
PA_SECURE_COOKIES=1
CONF
else
  sed -i "s/^PA_PORT=.*/PA_PORT=$APP_PORT/" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

say "systemd service"
cat > /etc/systemd/system/$APP_NAME.service <<UNIT
[Unit]
Description=Partnership accounting dashboard
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python run.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now $APP_NAME
systemctl restart $APP_NAME
for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
  [ "$i" -eq 30 ] && { journalctl -u $APP_NAME -n 40 --no-pager; die "the app did not start"; }
done

say "nginx (HTTPS)"
if [ -z "$DOMAIN" ]; then
  CERT_DIR=/etc/ssl/$APP_NAME
  install -d -m 700 "$CERT_DIR"
  IP=$(hostname -I | awk '{print $1}')
  if [ ! -f "$CERT_DIR/cert.pem" ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -subj "/CN=$IP" \
      -addext "subjectAltName=IP:$IP" -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" 2>/dev/null
  fi
  LISTEN="listen $HTTPS_PORT ssl;"
  SERVER_NAME="_"
  TLS="ssl_certificate $CERT_DIR/cert.pem;
    ssl_certificate_key $CERT_DIR/key.pem;"
else
  LISTEN="listen 80;"
  SERVER_NAME="$DOMAIN"
  TLS=""
fi
BACKUP_SITE=""
[ -f "$NGINX_SITE" ] && BACKUP_SITE=$(mktemp) && cp "$NGINX_SITE" "$BACKUP_SITE"
cat > "$NGINX_SITE" <<NGX
# managed by $APP_DIR/deploy/install.sh
server {
    $LISTEN
    server_name $SERVER_NAME;
    $TLS
    client_max_body_size 12m;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy same-origin always;
    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }
}
NGX
ln -sf "$NGINX_SITE" "$NGINX_LINK"
if ! nginx -t 2>/tmp/$APP_NAME-nginx.log; then
  cat /tmp/$APP_NAME-nginx.log
  if [ -n "$BACKUP_SITE" ]; then cp "$BACKUP_SITE" "$NGINX_SITE"; else rm -f "$NGINX_SITE" "$NGINX_LINK"; fi
  die "nginx rejected the configuration; your existing sites were not changed"
fi
systemctl reload nginx

if [ -n "$DOMAIN" ]; then
  say "Let's Encrypt certificate for $DOMAIN"
  if [ -n "$EMAIL" ]; then CB_MAIL=(-m "$EMAIL"); else CB_MAIL=(--register-unsafely-without-email); fi
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect "${CB_MAIL[@]}"
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  say "Opening the port in ufw"
  if [ -z "$DOMAIN" ]; then ufw allow "$HTTPS_PORT"/tcp; else ufw allow 'Nginx Full'; fi
fi

say "Daily backup (kept 60 days in $BACKUP_DIR)"
cat > /etc/cron.daily/$APP_NAME-backup <<CRON
#!/bin/sh
# consistent copy of the live SQLite database + the uploaded receipts
set -e
STAMP=\$(date +%Y-%m-%d)
$APP_DIR/.venv/bin/python - <<PY
import sqlite3
src = sqlite3.connect("$DATA_DIR/partnership.db")
dst = sqlite3.connect("$BACKUP_DIR/partnership-\$STAMP.db")
src.backup(dst); dst.close(); src.close()
PY
tar -czf "$BACKUP_DIR/uploads-\$STAMP.tar.gz" -C "$DATA_DIR" uploads 2>/dev/null || true
find "$BACKUP_DIR" -type f -mtime +60 -delete
CRON
chmod 755 /etc/cron.daily/$APP_NAME-backup

if [ -z "$DOMAIN" ]; then URL="https://$IP:$HTTPS_PORT"; else URL="https://$DOMAIN"; fi
say "Done"
echo "Open: $URL"
[ -z "$DOMAIN" ] && echo "(The browser warns once about the self-signed certificate: choose Advanced -> Proceed. The connection is encrypted.)"
echo "First visit creates the administrator account. Logs: journalctl -u $APP_NAME -f"
