#!/bin/bash
# Install the filament manager on a QIDI printer.
#
#   bash install.sh user@printer
#   bash install.sh
#
# With user@printer, the files are copied over SSH and installed there.
# SSH asks for the password once per connection, then sudo asks again.
# With no host, install on this machine. No password is stored.

set -euo pipefail
umask 022

SCRIPT=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
SOURCE=$(cd "$(dirname "$0")" && pwd)

FILES=(
  catalog.py
  server.py
  index.html
  favicon.png
  fennec.svg
  install.sh
  uninstall.sh
  README.md
  filament-manager.service
)

usage() {
  echo "Usage: bash install.sh [user@printer]"
  echo "  user@printer   copy this directory to that machine and install it"
  echo "  no argument    install on this machine"
}

discover_paths() {
  PYTHONPATH="$SOURCE" python3 -c 'import catalog, sys
layout = catalog.discover()
if layout is None:
    sys.stderr.write(catalog.missing_catalog_message() + "\n")
    sys.exit(1)
print(layout.home)
print(layout.catalog)
'
}

local_install() {
  if [[ "$(id -u)" -ne 0 ]]; then
    discover_paths >/dev/null
    echo "Installing the service. sudo will ask for your password."
    echo "This prompt is expected. It is the same password again, not a rejection."
    exec sudo /bin/bash -- "$SCRIPT" --local
  fi

  local discovered home_dir catalog_path owner group python_bin
  local app_dir port unit_path sudoers tmp existing
  if ! discovered=$(discover_paths); then
    exit 1
  fi
  home_dir=$(printf '%s\n' "$discovered" | sed -n '1p')
  catalog_path=$(printf '%s\n' "$discovered" | sed -n '2p')
  owner=$(stat -c '%U' "$catalog_path")
  case "$owner" in
    qidi|mks) ;;
    *)
      echo "The catalog is owned by ${owner}. Expected qidi or mks." >&2
      exit 1
      ;;
  esac
  if [[ "$home_dir" != "/home/$owner" ]]; then
    echo "The catalog is in ${home_dir}, which does not match owner ${owner}." >&2
    exit 1
  fi
  if ! id "$owner" >/dev/null 2>&1; then
    echo "No login account for catalog owner ${owner}." >&2
    exit 1
  fi
  group=$(id -gn "$owner")
  python_bin=$(command -v python3)
  if [[ "$python_bin" != /* ]]; then
    echo "python3 was not found on PATH." >&2
    exit 1
  fi

  app_dir="/home/${owner}/filament-manager"
  port=8091
  unit_path=/etc/systemd/system/filament-manager.service
  if [[ -f "$unit_path" ]]; then
    existing=$(sed -n 's/^Environment=FILAMENT_MANAGER_PORT=\([0-9][0-9]*\).*/\1/p' "$unit_path" | head -n 1)
    if [[ -n "${existing}" ]]; then
      port=$existing
    fi
  fi

  mkdir -p "$app_dir/state"
  local file
  for file in "${FILES[@]}"; do
    if [[ ! -f "$SOURCE/$file" ]]; then
      echo "Missing ${SOURCE}/${file}" >&2
      exit 1
    fi
    install -o "$owner" -g "$group" -m 0644 "$SOURCE/$file" "$app_dir/$file"
  done
  chmod 0755 "$app_dir/install.sh" "$app_dir/uninstall.sh"
  chown -R "$owner:$group" "$app_dir"
  chmod 0755 "$app_dir" "$app_dir/state"

  tmp=$(mktemp)
  sed \
    -e "s|__USER__|${owner}|g" \
    -e "s|__GROUP__|${group}|g" \
    -e "s|__DIR__|${app_dir}|g" \
    -e "s|__PYTHON__|${python_bin}|g" \
    -e "s|__PORT__|${port}|g" \
    "$SOURCE/filament-manager.service" > "$tmp"
  install -m 0644 "$tmp" "$unit_path"
  rm -f "$tmp"

  sudoers=/etc/sudoers.d/filament-manager
  if [[ ! -x /bin/systemctl ]]; then
    echo "/bin/systemctl was not found. The page cannot restart the touchscreen or reboot the printer." >&2
    rm -f "$sudoers"
  else
    rules=""
    if systemctl cat qidi-client.service >/dev/null 2>&1; then
      rules="${owner} ALL=(root) NOPASSWD: /bin/systemctl restart qidi-client.service"$'\n'
    else
      echo "qidi-client.service was not found. The page will not offer a touchscreen restart." >&2
    fi
    rules="${rules}${owner} ALL=(root) NOPASSWD: /bin/systemctl reboot"$'\n'
    if ! command -v visudo >/dev/null 2>&1; then
      echo "visudo is not available, so the sudoers rules were not installed." >&2
      exit 1
    fi
    printf '%s' "$rules" > "$sudoers"
    chmod 0440 "$sudoers"
    if ! visudo -cf "$sudoers" >/dev/null; then
      rm -f "$sudoers"
      echo "The sudoers rules did not pass visudo, so they were not installed." >&2
      exit 1
    fi
  fi

  systemctl daemon-reload
  systemctl enable filament-manager.service
  systemctl restart filament-manager.service
  if ! systemctl is-active --quiet filament-manager.service; then
    systemctl --no-pager --full status filament-manager.service || true
    echo "filament-manager.service did not stay running." >&2
    exit 1
  fi

  echo "Installed filament-manager for ${owner} in ${app_dir}."
  echo "Open http://<printer-address>:${port}/ on the local network."
}

remote_install() {
  local target=$1
  if [[ ! "$target" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$ ]]; then
    echo "Expected user@printer, for example qidi@printer.local" >&2
    exit 1
  fi
  local file
  local -a sources=()
  for file in "${FILES[@]}"; do
    if [[ ! -f "$SOURCE/$file" ]]; then
      echo "Missing ${SOURCE}/${file}" >&2
      exit 1
    fi
    sources+=("$SOURCE/$file")
  done
  echo "Connecting to ${target}."
  echo "The password is asked more than once. Each prompt is the next step, not a rejected password."
  echo "A wrong password is reported as incorrect."
  echo
  echo "Creating a temporary directory on the printer. Enter the SSH password."
  ssh "$target" "rm -rf /tmp/filament-manager-install && mkdir -p /tmp/filament-manager-install"
  echo
  echo "Copying the app. Enter the same password again."
  scp "${sources[@]}" "$target:/tmp/filament-manager-install/"
  echo
  echo "Running the installer on the printer. Enter the same password again."
  echo "After you are logged in, sudo asks for it once more."
  ssh -t "$target" 'bash /tmp/filament-manager-install/install.sh --local; status=$?; rm -rf /tmp/filament-manager-install; exit $status'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "${1:-}" == "--local" ]]; then
  local_install
  exit 0
fi
if [[ $# -gt 1 ]]; then
  usage >&2
  exit 1
fi
if [[ $# -eq 1 ]]; then
  remote_install "$1"
  exit 0
fi
local_install
