#!/usr/bin/env bash
# Back up, pull the latest version, and restart. Usage (as root): bash deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
/etc/cron.daily/partnership-accounting-backup || true   # backup before every update
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
chmod -R a+rX "$APP_DIR"
systemctl restart partnership-accounting
PORT=$(grep '^PA_PORT=' /etc/partnership-accounting.env | cut -d= -f2)
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then echo "updated and running"; exit 0; fi
  sleep 1
done
journalctl -u partnership-accounting -n 40 --no-pager
echo "ERROR: the app did not start after the update. A backup taken just before this update is in /var/backups/partnership-accounting" >&2
exit 1
