#!/usr/bin/env bash
# Update the app to the latest commit of its branch, safely. Usage (as root):
#   bash deploy/update.sh
#
#   1. pre-flight checks (nothing is changed if one fails)
#   2. maintenance page on, service stopped
#   3. backup of the database taken while nothing writes to it (must succeed)
#   4. pull, install requirements, start, health check
#   5. on any failure: automatic rollback to the previous commit AND the backup taken in
#      step 3 (the new code may have changed the database), then health check again
#   6. maintenance page off, security headers checked through nginx
#
# Tested in containers: bash deploy/test/update-test.sh
set -uo pipefail

APP_NAME=partnership-accounting
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER=pa-accounts
ENV_FILE=/etc/$APP_NAME.env
BACKUP_DIR=/var/backups/$APP_NAME
NGINX_SITE=/etc/nginx/sites-available/$APP_NAME.conf
MAINT_FLAG=/run/$APP_NAME.maintenance
REQUIRED_HEADERS=("X-Frame-Options: DENY" "X-Content-Type-Options: nosniff" "X-Robots-Tag: noindex, nofollow")

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
say "Pre-flight checks (no changes yet)"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash deploy/update.sh)"
cd "$APP_DIR"
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found: run deploy/install.sh first"
systemctl cat $APP_NAME >/dev/null 2>&1 || die "the $APP_NAME service is not installed: run deploy/install.sh first"
[ -x .venv/bin/python ] || die "$APP_DIR/.venv is missing: run deploy/install.sh first"
PORT=$(grep '^PA_PORT=' "$ENV_FILE" | cut -d= -f2)
DATA_DIR=$(grep '^PA_DATA_DIR=' "$ENV_FILE" | cut -d= -f2)
DB="$DATA_DIR/partnership.db"
[ -n "$PORT" ] && [ -n "$DATA_DIR" ] || die "PA_PORT / PA_DATA_DIR missing in $ENV_FILE"
[ -f "$DB" ] || die "database $DB not found"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "files in $APP_DIR were edited by hand (git status). Nothing was changed; commit or discard them first."
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || die "the checkout has no upstream branch"
git fetch -q "${UPSTREAM%%/*}" || die "git fetch failed (network or deploy key). Nothing was changed."
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "$UPSTREAM")
if [ "$OLD" != "$NEW" ]; then
  git merge-base --is-ancestor "$OLD" "$NEW" || die "$UPSTREAM is not a fast-forward of the deployed commit (history was rewritten?). Nothing was changed."
fi
HAS_MAINT_PAGE=0
grep -q "$MAINT_FLAG" "$NGINX_SITE" 2>/dev/null && HAS_MAINT_PAGE=1
FREE_KB=$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')
DB_KB=$(du -k "$DB" | cut -f1)
[ "$FREE_KB" -gt $((DB_KB * 2 + 10240)) ] || die "not enough free disk space in $BACKUP_DIR for a backup. Nothing was changed."
echo "checks passed"

# nginx address of this app, used for the header check: "<name> <port>"
nginx_target() {
  local name port
  name=$(awk '$1=="server_name"{gsub(";","",$2); print $2; exit}' "$NGINX_SITE")
  port=$(grep -Eo 'listen[[:space:]]+([0-9.]+:)?[0-9]+[[:space:]]+ssl' "$NGINX_SITE" | head -1 | grep -Eo '[0-9]+[[:space:]]+ssl' | grep -Eo '^[0-9]+')
  [ "$name" = "_" ] && name=127.0.0.1
  echo "$name ${port:-443}"
}

check_headers() {
  [ -f "$NGINX_SITE" ] || { echo "no nginx site file, header check skipped"; return 0; }
  local name port hdrs missing=()
  read -r name port < <(nginx_target)
  hdrs=$(curl -ksS -D - -o /dev/null --resolve "$name:$port:127.0.0.1" "https://$name:$port/healthz" 2>&1) || true
  for h in "${REQUIRED_HEADERS[@]}"; do
    grep -qi "^${h}"$'\r'"\?$" <<<"$hdrs" || missing+=("$h")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    printf 'missing security headers on https://%s:%s : %s\n' "$name" "$port" "${missing[*]}" >&2
    return 1
  fi
  echo "security headers present: ${REQUIRED_HEADERS[*]}"
}

healthy() {
  for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

maintenance_on() {
  [ "$HAS_MAINT_PAGE" = 1 ] && : > "$MAINT_FLAG"
  [ "$HAS_MAINT_PAGE" = 1 ] || echo "(no maintenance page in the nginx site yet; re-run deploy/install.sh once to add it)"
}
maintenance_off() { rm -f "$MAINT_FLAG"; }

if [ "$OLD" = "$NEW" ]; then
  say "Already up to date ($OLD)"
  check_headers || die "security headers missing. Re-run deploy/install.sh to refresh the nginx site."
  exit 0
fi

# ---------------------------------------------------------------------------
say "Maintenance mode on, stopping the service"
maintenance_on
systemctl stop $APP_NAME

say "Backup before the update"
install -d -o root -g root -m 700 "$BACKUP_DIR"
STAMP=$(date +%Y-%m-%d-%H%M%S)
BACKUP="$BACKUP_DIR/pre-update-$STAMP-${OLD:0:12}.db"
if ! .venv/bin/python - "$DB" "$BACKUP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
dst.close(); src.close()
sys.exit(0 if ok else 1)
PY
then
  rm -f "$BACKUP"
  systemctl start $APP_NAME
  healthy && maintenance_off
  die "the backup failed, so the update was not started. The previous version is running again."
fi
chmod 600 "$BACKUP"
echo "saved $BACKUP"

rollback() {
  printf '\n\033[1;31m==> %s — rolling back to %s\033[0m\n' "$1" "${OLD:0:12}" >&2
  systemctl stop $APP_NAME
  git reset -q --hard "$OLD"
  .venv/bin/pip install -q -r requirements.txt || true
  chmod -R a+rX "$APP_DIR"
  # the new code may already have changed the database: put back the copy taken while stopped
  rm -f "$DB-wal" "$DB-shm"
  install -o $APP_USER -g $APP_USER -m 600 "$BACKUP" "$DB"
  systemctl start $APP_NAME
  if healthy; then
    maintenance_off
    die "update to ${NEW:0:12} failed ($1). ROLLED BACK: version ${OLD:0:12} and the database from before the update are running again."
  fi
  journalctl -u $APP_NAME -n 40 --no-pager
  die "update failed AND the rollback did not start. The maintenance page stays on. Backup: $BACKUP"
}

say "Updating ${OLD:0:12} -> ${NEW:0:12}"
git merge -q --ff-only "$NEW" || rollback "git merge failed"
.venv/bin/pip install -q -r requirements.txt || rollback "pip install failed"
chmod -R a+rX "$APP_DIR"
systemctl start $APP_NAME || rollback "the service did not start"
if ! healthy; then
  journalctl -u $APP_NAME -n 40 --no-pager
  rollback "the new version did not answer /healthz within 30 seconds"
fi

maintenance_off
say "Checking security headers through nginx"
check_headers || die "updated to ${NEW:0:12} and running, but security headers are missing. Re-run deploy/install.sh to refresh the nginx site."
say "Updated to ${NEW:0:12} and running"
