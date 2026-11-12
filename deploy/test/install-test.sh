#!/usr/bin/env bash
# Runs deploy/install.sh inside throw-away systemd containers, never on a real server.
#
#   bash deploy/test/build-image.sh            # once: builds the pa-test:noble image
#   bash deploy/test/install-test.sh           # all scenarios
#
# Scenarios:
#   fresh-refuse   fresh server, no approval      -> stops, changes nothing
#   fresh-install  fresh server, INSTALL_PACKAGES=1 -> app answers through nginx over HTTPS
#   rerun          install again on the same box  -> no apt-get, still works
#   apache         Apache owns port 80            -> stops, Apache untouched, nothing changed
#   other80        an unknown program owns 80     -> stops, nothing changed
#   nginx-stopped  nginx installed but stopped    -> stops, does not start nginx
#   shared-nginx   nginx already serves another site -> no apt-get, other site untouched
set -uo pipefail
. "$(dirname "$0")/lib.sh"

snapshot() {  # what the install script could change on the "server"
  cx bash -c 'id pa-accounts 2>&1; ls -la /etc/partnership-accounting.env /etc/systemd/system/partnership-accounting.service /etc/nginx/sites-enabled /var/lib/partnership-accounting 2>&1; dpkg -l | md5sum; systemctl is-active nginx apache2 2>&1' | sort
}

scenario fresh-refuse
before=$(snapshot)
out=$(cx bash /opt/partnership-accounting/deploy/install.sh 2>&1); rc=$?
expect "exit code is non-zero" test $rc -ne 0
expect "lists the missing packages" grep -q "missing: .*nginx" <<<"$out"
expect "apt-get was not run" bash -c '! grep -q "Installing only" <<<"$1"' _ "$out"
expect "nothing changed on the server" test "$before" = "$(snapshot)"

scenario fresh-install
out=$(cx env INSTALL_PACKAGES=1 bash /opt/partnership-accounting/deploy/install.sh 2>&1); rc=$?
echo "$out" | tail -5
expect "exit code 0" test $rc -eq 0
expect "installs only what was missing" grep -q "Installing only the missing system packages: nginx" <<<"$out"
expect "app answers through nginx over HTTPS" cx curl -kfsS -o /dev/null https://127.0.0.1:8443/login
expect "service runs as pa-accounts, not root" cx bash -c 'ps -eo user:20,cmd | grep "[r]un.py" | grep -q "^pa-accounts "'
expect "env file is mode 600" test "$(cx stat -c %a /etc/partnership-accounting.env)" = 600

scenario rerun   # same container, nginx now present
out=$(cx bash /opt/partnership-accounting/deploy/install.sh 2>&1); rc=$?
expect "exit code 0" test $rc -eq 0
expect "apt-get not used" grep -q "apt-get not used" <<<"$out"
expect "still answers" cx curl -kfsS -o /dev/null https://127.0.0.1:8443/login
finish_container

scenario apache
cx bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -q apache2 >/dev/null 2>&1 && systemctl is-active apache2'
before=$(snapshot)
out=$(cx env INSTALL_PACKAGES=1 bash /opt/partnership-accounting/deploy/install.sh 2>&1); rc=$?
expect "exit code is non-zero" test $rc -ne 0
expect "names apache as the owner of port 80" grep -q "port 80 is used by 'apache2'" <<<"$out"
expect "Apache still running and serving" cx curl -fsS -o /dev/null http://127.0.0.1/
expect "nginx was not installed, nothing changed" test "$before" = "$(snapshot)"
finish_container

scenario other80
cx bash -c 'nohup python3 -m http.server 80 >/dev/null 2>&1 & sleep 1'
before=$(snapshot)
out=$(cx env INSTALL_PACKAGES=1 bash /opt/partnership-accounting/deploy/install.sh 2>&1); rc=$?
expect "exit code is non-zero" test $rc -ne 0
expect "names the program on port 80" grep -q "port 80 is used by 'python3'" <<<"$out"
expect "nothing changed" test "$before" = "$(snapshot)"
finish_container

scenario nginx-stopped
install_nginx_quietly
cx systemctl stop nginx
before=$(snapshot)
out=$(cx env INSTALL_PACKAGES=1 bash /opt/partnership-accounting/deploy/install.sh 2>&1); rc=$?
expect "exit code is non-zero" test $rc -ne 0
expect "explains nginx is stopped" grep -q "installed but not running" <<<"$out"
expect "nginx left stopped" bash -c '! cx systemctl is-active --quiet nginx'
expect "nothing changed" test "$before" = "$(snapshot)"
finish_container

scenario shared-nginx
install_nginx_quietly
cx bash -c 'mkdir -p /srv/other && echo other-site > /srv/other/index.html &&
  printf "server { listen 8081; root /srv/other; }\n" > /etc/nginx/sites-available/other &&
  ln -s /etc/nginx/sites-available/other /etc/nginx/sites-enabled/other && systemctl start nginx'
other_sum=$(cx md5sum /etc/nginx/sites-available/other /etc/nginx/nginx.conf)
out=$(cx bash /opt/partnership-accounting/deploy/install.sh 2>&1); rc=$?
expect "exit code 0" test $rc -eq 0
expect "apt-get not used" grep -q "apt-get not used" <<<"$out"
expect "our app answers on 8443" cx curl -kfsS -o /dev/null https://127.0.0.1:8443/login
expect "the other site still answers" bash -c '[ "$(cx curl -fsS http://127.0.0.1:8081/)" = other-site ]'
expect "the other site's files and nginx.conf unchanged" test "$other_sum" = "$(cx md5sum /etc/nginx/sites-available/other /etc/nginx/nginx.conf)"
finish_container

report
