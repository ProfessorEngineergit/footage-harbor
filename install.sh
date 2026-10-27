#!/usr/bin/env bash
# Footage Harbor installer for Debian-based systems (Raspberry Pi OS, Debian, Ubuntu).
# Safe to run again: it updates the code and keeps your data.
#
#   curl -fsSL https://raw.githubusercontent.com/ProfessorEngineergit/footage-harbor/main/install.sh | sudo bash
#
set -euo pipefail

REPO="ProfessorEngineergit/footage-harbor"
BRANCH="${FH_BRANCH:-main}"
APP_DIR="/opt/footage-harbor"
DATA_DIR="/var/lib/footage-harbor"
PORT="${FH_PORT:-80}"

say()  { printf '\033[38;5;208m▸\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Please run as root: sudo bash install.sh"
command -v apt-get >/dev/null || fail "This installer needs a Debian-based system (apt-get)."

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 exfatprogs ntfs-3g dosfstools udisks2 curl unzip ca-certificates >/dev/null

if ! command -v rclone >/dev/null; then
  say "Installing rclone"
  if ! curl -fsSL https://rclone.org/install.sh | bash >/dev/null 2>&1; then
    apt-get install -y -qq rclone >/dev/null || fail "Could not install rclone"
  fi
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC/footage_harbor/__main__.py" ]; then
  say "Downloading Footage Harbor ($BRANCH)"
  TMP="$(mktemp -d)"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$TMP"
  SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -n1)"
fi

say "Copying app to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/footage_harbor" "$APP_DIR/web"
cp -r "$SRC/footage_harbor" "$SRC/web" "$APP_DIR/"
cp "$SRC/LICENSE" "$SRC/README.md" "$SRC/install.sh" "$SRC/uninstall.sh" "$APP_DIR/" 2>/dev/null || true
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

if [ "$PORT" = "80" ] && command -v ss >/dev/null && ss -ltnH 'sport = :80' | grep -q . \
   && ! systemctl is-active --quiet footage-harbor; then
  PORT=8080
  say "Port 80 is taken, using $PORT"
fi

say "Setting up the service"
sed "s|@PORT@|$PORT|g; s|@APP@|$APP_DIR|g; s|@DATA@|$DATA_DIR|g" \
  "$SRC/systemd/footage-harbor.service" > /etc/systemd/system/footage-harbor.service
systemctl daemon-reload
systemctl enable --now footage-harbor >/dev/null 2>&1
systemctl restart footage-harbor

sleep 2
systemctl is-active --quiet footage-harbor || fail "Service did not start. Check: journalctl -u footage-harbor"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
URL="http://${IP:-$(hostname)}"
[ "$PORT" = "80" ] || URL="$URL:$PORT"
echo
say "Footage Harbor is running."
echo "    Open $URL in a browser on any computer in your network."
echo
