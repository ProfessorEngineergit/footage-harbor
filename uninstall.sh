#!/usr/bin/env bash
# Removes the service and app. Your archive and /var/lib/footage-harbor stay unless you pass --purge.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run as root: sudo bash uninstall.sh"; exit 1; }
systemctl disable --now footage-harbor footage-harbor-updater 2>/dev/null || true
rm -f /etc/systemd/system/footage-harbor.service /etc/systemd/system/footage-harbor-updater.service
systemctl daemon-reload
rm -rf /opt/footage-harbor
if [ "${1:-}" = "--purge" ]; then rm -rf /var/lib/footage-harbor; echo "Removed app and station data."; else echo "Removed app. Station data kept in /var/lib/footage-harbor."; fi
