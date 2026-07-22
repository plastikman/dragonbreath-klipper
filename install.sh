#!/usr/bin/env bash
# Install openbreath.py into Klipper's extras directory (symlink) and restart
# Klipper so the [openbreath] config section becomes available.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

home_dir="${HOME}"
klipper_dir="${KLIPPER_DIR:-${home_dir}/klipper}"
service_name="${KLIPPER_SERVICE:-klipper}"
do_restart=1
do_copy=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

  --klipper-dir PATH   Klipper checkout (default: ${klipper_dir})
  --service NAME       systemd service to restart (default: ${service_name})
  --copy               copy the module instead of symlinking
  --no-restart         don't restart Klipper afterwards
  -h, --help           show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --klipper-dir) klipper_dir="$2"; shift 2 ;;
    --service)     service_name="$2"; shift 2 ;;
    --copy)        do_copy=1; shift ;;
    --no-restart)  do_restart=0; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

extras_dir="${klipper_dir}/klippy/extras"
if [[ ! -d "$extras_dir" ]]; then
  echo "ERROR: Klipper extras dir not found: ${extras_dir}" >&2
  echo "       Pass --klipper-dir /path/to/klipper" >&2
  exit 1
fi

src="${repo_dir}/openbreath.py"
dst="${extras_dir}/openbreath.py"

if [[ $do_copy -eq 1 ]]; then
  echo "Copying openbreath.py -> ${dst}"
  cp -f "$src" "$dst"
else
  echo "Linking openbreath.py -> ${dst}"
  ln -sf "$src" "$dst"
fi

if [[ $do_restart -eq 1 ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    echo "Restarting ${service_name}..."
    sudo systemctl restart "${service_name}"
  else
    echo "systemctl not found — restart Klipper manually."
  fi
fi

cat <<EOF

Installed. Add to your printer.cfg (see README.md):

  [openbreath]
  host: <your-openbreath-ip>

  [heater_generic openbreath]
  heater_pin: openbreath:pwm
  sensor_type: openbreath
  control: watermark
  max_delta: 2.0
  min_temp: 0
  max_temp: 75

Then restart Klipper / FIRMWARE_RESTART.
EOF
