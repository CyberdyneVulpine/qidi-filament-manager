#!/bin/bash
# Remove the filament manager service and its sudoers rule.
#
#   bash uninstall.sh user@printer
#   bash uninstall.sh
#
# The app directory and the filament catalog are left in place.

set -euo pipefail

SCRIPT=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")

usage() {
  echo "Usage: bash uninstall.sh [user@printer]"
  echo "  user@printer   remove the service on that machine"
  echo "  no argument    remove the service on this machine"
}

local_uninstall() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Removing the service. sudo will ask for your password."
    echo "This prompt is expected. It is the same password again, not a rejection."
    exec sudo /bin/bash -- "$SCRIPT" --local
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now filament-manager.service >/dev/null 2>&1 || true
  fi
  rm -f /etc/systemd/system/filament-manager.service
  rm -f /etc/sudoers.d/filament-manager
  if [[ -d /etc/systemd/system/filament-manager.service.d ]]; then
    rm -rf /etc/systemd/system/filament-manager.service.d
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
  fi
  echo "Removed filament-manager.service and the sudoers rule."
  local dir
  for dir in /home/qidi/filament-manager /home/mks/filament-manager; do
    if [[ -d "$dir" ]]; then
      echo "Left ${dir} in place. Delete that directory if you want the app gone."
    fi
  done
  echo "The filament catalog was not changed."
}

remote_uninstall() {
  local target=$1
  if [[ ! "$target" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$ ]]; then
    echo "Expected user@printer, for example qidi@printer.local" >&2
    exit 1
  fi
  echo "Connecting to ${target}."
  echo "The password is asked more than once. Each prompt is the next step, not a rejected password."
  echo
  echo "Copying the uninstaller. Enter the SSH password."
  scp "$SCRIPT" "$target:/tmp/filament-manager-uninstall.sh"
  echo
  echo "Removing the service on the printer. Enter the same password again."
  echo "After you are logged in, sudo asks for it once more."
  ssh -t "$target" 'bash /tmp/filament-manager-uninstall.sh --local; status=$?; rm -f /tmp/filament-manager-uninstall.sh; exit $status'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--local" ]]; then
  local_uninstall
  exit 0
fi
if [[ $# -gt 1 ]]; then
  usage >&2
  exit 1
fi
if [[ $# -eq 1 ]]; then
  remote_uninstall "$1"
  exit 0
fi
local_uninstall
