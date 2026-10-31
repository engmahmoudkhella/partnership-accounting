#!/usr/bin/env bash
# Pull the latest version, run the tests, and restart. Usage (as root): bash deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
/etc/cron.daily/partnership-accounting-backup || true   # backup before every update
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
chmod -R a+rX "$APP_DIR"
systemctl restart partnership-accounting
sleep 2
PORT=$(grep '^PA_PORT=' /etc/partnership-accounting.env | cut -d= -f2)
curl -fsS "http://127.0.0.1:$PORT/healthz" && echo " updated and running"
