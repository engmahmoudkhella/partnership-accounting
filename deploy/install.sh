#!/usr/bin/env bash
# Install / re-install the partnership accounting app on an Ubuntu/Debian server.
# Safe on a server that already hosts other projects:
#   * runs as its own system user on its own local port (default 8077)
#   * adds ONE separate nginx site file and never edits or disables existing sites
#   * without a domain it serves HTTPS on its own port (default 8443) with a self-signed certificate
#   * with DOMAIN=... it uses that name and a free Let's Encrypt certificate
#   * tests the nginx configuration and rolls back its own file if the test fails
#   * checks everything first and changes nothing if a check fails
#   * never upgrades packages; installs missing ones only with INSTALL_PACKAGES=1
#   * refuses to install nginx if Apache/Docker/another server already owns ports 80/443
#
# Usage (as root, from the cloned repository):
#   bash deploy/install.sh                      # https://SERVER-IP:8443
#   DOMAIN=accounts.example.com bash deploy/install.sh
# Optional: APP_PORT=8077 HTTPS_PORT=8443 EMAIL=you@example.com
#           INSTALL_PACKAGES=1   allow installing missing system packages (listed first)
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
MAINT_FLAG=/run/$APP_NAME.maintenance   # read by nginx; /run is cleared on reboot
NGINX_FRESH=0

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash deploy/install.sh)"
command -v apt-get >/dev/null || die "this script supports Ubuntu/Debian (apt-get) only"
case "$APP_DIR" in
  /root/*|/home/*) die "clone the repository under /opt (the service cannot read $APP_DIR): git clone ... /opt/$APP_NAME" ;;
esac

# ---------------------------------------------------------------------------
# Pre-flight checks. NOTHING on the server is changed before all of them pass.
# The server is shared with other sites: this script never upgrades packages,
# never runs apt-get unless you approve it (INSTALL_PACKAGES=1), and never
# installs or starts a web server next to one that already owns ports 80/443.
# ---------------------------------------------------------------------------
say "Pre-flight checks (no changes yet)"
command -v ss >/dev/null || die "the 'ss' command (package iproute2) is required for the port checks"
port_busy() { ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }
# name of the program listening on a TCP port (empty if the port is free)
port_owner() { ss -ltnpH "( sport = :$1 )" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2; }
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }

if ! systemctl is-active --quiet $APP_NAME 2>/dev/null; then
  port_busy "$APP_PORT" && die "port $APP_PORT is already used by '$(port_owner "$APP_PORT")'. Re-run with APP_PORT=<free port>"
fi
if [ -z "$DOMAIN" ] && ! [ -f "$NGINX_SITE" ]; then
  port_busy "$HTTPS_PORT" && die "port $HTTPS_PORT is already used by '$(port_owner "$HTTPS_PORT")'. Re-run with HTTPS_PORT=<free port>"
fi

MISSING=()
for p in python3 python3-venv openssl ca-certificates curl; do pkg_installed "$p" || MISSING+=("$p"); done

if pkg_installed nginx; then
  # use the nginx that is already there, but never start or restart it ourselves
  systemctl is-active --quiet nginx || die "nginx is installed but not running. This script will not start it on a shared server (it may serve other sites). Start it yourself (systemctl start nginx) and re-run."
else
  # installing nginx would start its default site on port 80: refuse if anything else uses 80/443
  for p in 80 443; do
    if port_busy "$p"; then
      die "nginx is not installed and port $p is used by '$(port_owner "$p")' (Apache, Docker, Caddy...). Installing nginx here would conflict with it, so nothing was changed. Tell the project owner which web server this machine uses; the app must then be published through that server instead."
    fi
  done
  MISSING+=(nginx)
fi
if [ -n "$DOMAIN" ]; then
  owner80=$(port_owner 80)
  if [ -n "$owner80" ] && [ "$owner80" != nginx ]; then
    die "DOMAIN mode needs port 80 for nginx, but port 80 is used by '$owner80'. Nothing was changed."
  fi
  for p in certbot python3-certbot-nginx; do pkg_installed "$p" || MISSING+=("$p"); done
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  if [ "${INSTALL_PACKAGES:-0}" != 1 ]; then
    die "these system packages are missing: ${MISSING[*]}
Nothing was changed. To let this script install ONLY these packages (no upgrades), re-run with:
  INSTALL_PACKAGES=1 bash deploy/install.sh"
  fi
fi
if pkg_installed python3; then
  PYV=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
  [ "$PYV" -ge 310 ] || die "Python 3.10+ is required (found $(python3 --version)). Nothing was changed."
fi
echo "checks passed"

if [ ${#MISSING[@]} -gt 0 ]; then
  say "Installing only the missing system packages: ${MISSING[*]}"
  export DEBIAN_FRONTEND=noninteractive
  NGINX_FRESH=0
  if [[ " ${MISSING[*]} " == *" nginx "* ]]; then
    NGINX_FRESH=1
    # A freshly installed nginx would start its "Welcome" default site on port 80 at once.
    # Keep it from starting during the install; it is started below with our site only.
    [ -e /usr/sbin/policy-rc.d ] && die "/usr/sbin/policy-rc.d already exists; remove it or install nginx yourself. Nothing was changed."
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
  fi
  apt-get update -q   # refreshes the package lists only; nothing is upgraded
  apt-get install -y -q --no-upgrade "${MISSING[@]}" >/dev/null
  if [ "$NGINX_FRESH" = 1 ]; then
    rm -f /usr/sbin/policy-rc.d; trap - EXIT
    rm -f /etc/nginx/sites-enabled/default   # the default site we just installed (nobody else uses it)
  fi
  PYV=$(python3 -c 'import sys; print("%d%02d" % sys.version_info[:2])')
  [ "$PYV" -ge 310 ] || die "Python 3.10+ is required (found $(python3 --version))"
else
  say "All system packages already present (apt-get not used)"
fi

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
    add_header X-Robots-Tag "noindex, nofollow" always;
    # maintenance page while deploy/update.sh runs (it creates and removes this flag)
    error_page 503 @maintenance;
    location @maintenance {
        default_type "text/html; charset=utf-8";
        add_header Retry-After 60 always;
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        add_header X-Robots-Tag "noindex, nofollow" always;
        return 503 '<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><title>تحديث</title><body style="font-family:sans-serif;text-align:center;padding:4em">النظام قيد التحديث، يعود خلال دقيقة.<br>Updating, back in a minute.</body></html>';
    }
    location / {
        if (-f $MAINT_FLAG) { return 503; }
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
if [ "${NGINX_FRESH:-0}" = 1 ]; then
  systemctl enable --now nginx   # we installed it in this run, so starting it touches nobody else
else
  systemctl reload nginx         # existing nginx: reload only (keeps other sites' connections)
fi

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
