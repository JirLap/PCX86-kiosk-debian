#!/bin/sh
set -eu

ARCHIVE_URL="https://github.com/JirLap/PCX86-kiosk-debian/archive/refs/heads/main.tar.gz"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this launcher as the normal XFCE desktop user, without sudo." >&2
    exit 1
fi

for required_command in wget tar mktemp; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        echo "Required command not found: $required_command" >&2
        exit 1
    fi
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required; installing it with apt."
    sudo apt-get update
    sudo apt-get install -y python3
fi

umask 077
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/pcx86-kiosk.XXXXXX")
trap 'rm -rf -- "$work_dir"' 0

echo "Downloading the PCX86-kiosk-debian source archive..."
wget -q -O "$work_dir/source.tar.gz" "$ARCHIVE_URL"
tar -xzf "$work_dir/source.tar.gz" -C "$work_dir"

source_dir="$work_dir/PCX86-kiosk-debian-main"
if [ ! -f "$source_dir/install.py" ] || [ ! -f "$source_dir/src/panel_agent.py" ] || [ ! -f "$source_dir/src/pcx86-power" ]; then
    echo "The downloaded archive is missing required installer files." >&2
    exit 1
fi

python3 "$source_dir/install.py"
