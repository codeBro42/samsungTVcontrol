#!/bin/sh
# TVHub - remove the systemd service.
#
#     sudo ./uninstall.sh
#
# config.json, state/ (including the pairing tokens) and photos/ are deliberately
# left alone: uninstalling the service must never lose the pairings, because
# re-pairing means walking to every TV with the remote.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

PYTHON=${PYTHON:-python3}

if [ "$(id -u)" != "0" ]; then
    echo "ERROR this needs root - run: sudo ./uninstall.sh" >&2
    exit 1
fi

TVHUB_HOME="$HERE" "$PYTHON" -m tvhub uninstall
