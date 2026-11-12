#!/usr/bin/env bash
# Builds a minimal Ubuntu 24.04 image with systemd (like a fresh VPS) for the container
# tests, using debootstrap so it does not depend on Docker Hub. Run as root.
set -euo pipefail
IMAGE=${PA_TEST_IMAGE:-pa-test:noble}
MIRROR=${MIRROR:-http://archive.ubuntu.com/ubuntu}
command -v debootstrap >/dev/null || apt-get install -y -q debootstrap
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
debootstrap --variant=minbase --components=main,universe \
  --include=systemd,systemd-sysv,dbus,iproute2,procps,git,ca-certificates,curl,python3,python3-venv,openssl,cron \
  noble "$ROOT" "$MIRROR"
printf 'deb %s noble main universe\ndeb %s noble-updates main universe\n' "$MIRROR" "$MIRROR" > "$ROOT/etc/apt/sources.list"
chroot "$ROOT" apt-get update -q
tar -C "$ROOT" -c . | docker import -c 'CMD ["/sbin/init"]' - "$IMAGE"
echo "built $IMAGE"
