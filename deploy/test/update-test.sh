#!/usr/bin/env bash
# Runs deploy/update.sh inside a throw-away systemd container, never on a real server.
#   bash deploy/test/build-image.sh     # once
#   bash deploy/test/update-test.sh
#
# The app is installed with install.sh, the checkout's origin is pointed at a bare repo
# inside the container, and each scenario pushes a new commit there and runs update.sh.
set -uo pipefail
. "$(dirname "$0")/lib.sh"

APP=/opt/partnership-accounting
DB=/var/lib/partnership-accounting/partnership.db

head_of() { cx git -C "$1" rev-parse HEAD; }
sql() { cx runuser -u pa-accounts -- python3 -c "import sqlite3,sys; c=sqlite3.connect('$DB'); r=c.execute(sys.argv[1]).fetchall(); c.commit(); print(r)" "$1"; }
has_table() { sql "select name from sqlite_master where name='$1'" | grep -q "$1"; }
push_commit() {  # push_commit "message" "shell commands run in a clone of origin"
  cx bash -c "set -e; rm -rf /srv/work; git clone -q /srv/remote.git /srv/work; cd /srv/work; $2; git add -A; git commit -qm '$1'; git push -q origin HEAD:main"
}
export DB
export -f head_of sql has_table
update() { cx bash $APP/deploy/update.sh 2>&1; }
via_nginx() { cx curl -ks -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/healthz; }

scenario setup
cx bash -c "git config --global user.email test@example.com && git config --global user.name test &&
  git config --global --add safe.directory '*' && cd $APP && git checkout -q -B main &&
  { git add -A && git commit -qm 'working tree under test' || true; } &&
  git clone -q --bare . /srv/remote.git && git remote set-url origin /srv/remote.git &&
  git fetch -q origin && git branch -q -u origin/main"
out=$(cx env INSTALL_PACKAGES=1 bash $APP/deploy/install.sh 2>&1)
expect "install.sh succeeded" grep -q "==> Done" <<<"$out"
sql "create table test_marker(x)" >/dev/null
expect "marker row written" has_table test_marker

scenario up-to-date
out=$(update); rc=$?
expect "exit code 0" test $rc -eq 0
expect "says already up to date" grep -q "Already up to date" <<<"$out"
expect "header check passed" grep -q "security headers present" <<<"$out"

scenario maintenance-page
cx touch /run/partnership-accounting.maintenance
expect "nginx answers 503 while the flag exists" test "$(via_nginx)" = 503
page=$(cx curl -ksS -D - https://127.0.0.1:8443/login)
expect "maintenance page text" grep -q "قيد التحديث" <<<"$page"
expect "maintenance page keeps X-Frame-Options" grep -qi "^X-Frame-Options: DENY" <<<"$page"
expect "maintenance page has X-Robots-Tag" grep -qi "^X-Robots-Tag: noindex, nofollow" <<<"$page"
cx rm -f /run/partnership-accounting.maintenance
expect "back to 200 without the flag" test "$(via_nginx)" = 200

scenario success
push_commit "add VERSION" "echo 2 > VERSION"
remote=$(head_of /srv/remote.git)
out=$(update); rc=$?
echo "$out" | tail -3
expect "exit code 0" test $rc -eq 0
expect "deployed the new commit" test "$(head_of $APP)" = "$remote"
expect "app healthy" cx curl -fsS http://127.0.0.1:8077/healthz
expect "maintenance flag removed" bash -c '! cx test -e /run/partnership-accounting.maintenance'
expect "data kept" has_table test_marker
expect "pre-update backup exists with mode 600" cx bash -c 'f=$(ls -t /var/backups/partnership-accounting/pre-update-*.db | head -1); [ "$(stat -c %a "$f")" = 600 ]'
expect "headers checked through nginx" grep -q "security headers present" <<<"$out"

scenario maintenance-during-update
push_commit "slow start" "sed -i 's/^import os$/import os, time; time.sleep(8)/' run.py"
cx bash -c 'rm -f /tmp/codes; (for i in $(seq 1 40); do curl -ks -o /dev/null -w "%{http_code}\n" https://127.0.0.1:8443/login >> /tmp/codes; sleep 0.5; done) >/dev/null 2>&1 &'
out=$(update); rc=$?
sleep 1
expect "exit code 0" test $rc -eq 0
expect "visitors saw the 503 maintenance page during the update" cx grep -q '^503$' /tmp/codes
expect "visitors never saw a 502 (app down without the page)" bash -c '! cx grep -q "^502$" /tmp/codes'

scenario rollback
before=$(head_of $APP)
# the broken version changes the database, then crashes on start
push_commit "broken" "printf 'import os, sqlite3\nc = sqlite3.connect(os.environ[\"PA_DATA_DIR\"] + \"/partnership.db\")\nc.execute(\"create table broken_change(x)\"); c.commit()\nraise SystemExit(\"boom\")\n' > run.py"
out=$(update); rc=$?
echo "$out" | grep -E "ERROR|rolling back" | head -3
expect "exit code is non-zero" test $rc -ne 0
expect "reports the rollback" grep -q "ROLLED BACK" <<<"$out"
expect "previous commit is deployed again" test "$(head_of $APP)" = "$before"
expect "app healthy after rollback" cx curl -fsS http://127.0.0.1:8077/healthz
expect "database change from the broken version was undone" bash -c '! has_table broken_change'
expect "data from before the update kept" has_table test_marker
expect "maintenance flag removed" bash -c '! cx test -e /run/partnership-accounting.maintenance'
expect "site answers through nginx" test "$(via_nginx)" = 200
# undo the broken commit upstream for the next scenarios
cx bash -c "cd /srv/work && git revert --no-edit HEAD >/dev/null && git push -q origin HEAD:main"

scenario hand-edited
cx bash -c "echo '# edited' >> $APP/README.md"
before=$(head_of $APP)
n_backups=$(cx bash -c 'ls /var/backups/partnership-accounting | wc -l')
out=$(update); rc=$?
expect "exit code is non-zero" test $rc -ne 0
expect "explains the hand edit" grep -q "edited by hand" <<<"$out"
expect "nothing deployed" test "$(head_of $APP)" = "$before"
expect "service still running" cx systemctl is-active --quiet partnership-accounting
expect "no backup taken, nothing changed" test "$n_backups" = "$(cx bash -c 'ls /var/backups/partnership-accounting | wc -l')"
cx git -C $APP checkout -q README.md

scenario missing-headers
cx bash -c "sed -i '/X-Robots-Tag/d' /etc/nginx/sites-available/partnership-accounting.conf && nginx -t -q && systemctl reload nginx"
out=$(update); rc=$?
expect "exit code is non-zero" test $rc -ne 0
echo "$out" | tail -4
expect "names the missing header" grep -q "missing security headers.*X-Robots-Tag" <<<"$out"
expect "code is not rolled back for a header problem" test "$(head_of $APP)" = "$(head_of /srv/remote.git)"
expect "app still healthy" cx curl -fsS http://127.0.0.1:8077/healthz

report
