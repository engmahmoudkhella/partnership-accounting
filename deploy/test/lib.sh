# Shared helpers for the container tests. Sourced, not run.
#
# Each scenario runs in a fresh privileged systemd container (image $PA_TEST_IMAGE,
# default pa-test:noble from build-image.sh) with a copy of the working tree in
# /opt/partnership-accounting. The containers use the host network so that apt and
# pip work behind a proxy; run the tests on a machine where ports 80, 443, 8077
# and 8443 are free (a laptop or CI, never the production server).
#
# Optional: PA_TEST_CA=/path/ca.pem  extra CA bundle for pip/curl behind a TLS proxy
#           HTTPS_PROXY / HTTP_PROXY   passed through to the container
PA_TEST_IMAGE=${PA_TEST_IMAGE:-pa-test:noble}
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CT=""
PASS=0
FAIL=0
FAILED=()

cx() { docker exec -i "$CT" "$@"; }
export -f cx

start_container() {
  CT="pa-test-$$-$RANDOM"
  export CT
  local args=(-d --name "$CT" --privileged --network host --cgroupns=host
              -v /sys/fs/cgroup:/sys/fs/cgroup:rw)
  [ -n "${HTTPS_PROXY:-}" ] && args+=(-e "HTTPS_PROXY=$HTTPS_PROXY" -e "https_proxy=$HTTPS_PROXY"
                                   -e NO_PROXY=127.0.0.1,localhost -e no_proxy=127.0.0.1,localhost)
  [ -n "${HTTP_PROXY:-}" ] && args+=(-e "HTTP_PROXY=$HTTP_PROXY" -e "http_proxy=$HTTP_PROXY")
  if [ -n "${PA_TEST_CA:-}" ]; then
    args+=(-v "$PA_TEST_CA:/test-ca.pem:ro" -e PIP_CERT=/test-ca.pem -e CURL_CA_BUNDLE=/test-ca.pem
           -e REQUESTS_CA_BUNDLE=/test-ca.pem -e SSL_CERT_FILE=/test-ca.pem)
  fi
  docker run "${args[@]}" "$PA_TEST_IMAGE" /sbin/init >/dev/null || { echo "cannot start container"; exit 2; }
  for _ in $(seq 1 30); do
    state=$(docker exec "$CT" systemctl is-system-running 2>/dev/null)
    [ "$state" = running ] || [ "$state" = degraded ] && break
    sleep 1
  done
  # copy the working tree (tracked + new files, without .git/.venv/data)
  (cd "$REPO_DIR" && git ls-files -co --exclude-standard -z | tar --null -T - -cf -) |
    docker exec -i "$CT" bash -c 'mkdir -p /opt/partnership-accounting && tar -xf - -C /opt/partnership-accounting'
  # a real deployment is a git checkout: update.sh needs the history
  docker cp "$REPO_DIR/.git" "$CT:/opt/partnership-accounting/.git" >/dev/null
}

finish_container() {
  [ -n "$CT" ] && docker rm -f "$CT" >/dev/null 2>&1
  CT=""
}
trap finish_container EXIT

scenario() {
  printf '\n\033[1m### %s\033[0m\n' "$1"
  CURRENT="$1"
  [ -n "$CT" ] || start_container
}

expect() {  # expect "description" command args...
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then
    PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$what"
  else
    FAIL=$((FAIL+1)); FAILED+=("$CURRENT: $what"); printf '  \033[31mFAIL\033[0m %s\n' "$what"
  fi
}

report() {
  printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
  for f in "${FAILED[@]}"; do echo "  FAILED: $f"; done
  [ "$FAIL" -eq 0 ]
}

# install nginx inside the test container without starting its default site
# (that site also listens on IPv6, which some test hosts lack)
install_nginx_quietly() {
  cx bash -c 'printf "#!/bin/sh\nexit 101\n" > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d &&
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q nginx >/dev/null 2>&1;
    rm -f /usr/sbin/policy-rc.d /etc/nginx/sites-enabled/default; systemctl enable nginx >/dev/null 2>&1'
}
