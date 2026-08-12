#!/bin/sh
# TVHub - install as a systemd service on Linux.
#
# Run as root, from the directory this script lives in:
#
#     sudo ./install.sh
#
# This is a thin wrapper. It installs the two dependencies SYSTEM-WIDE and then
# hands over to `python3 -m tvhub install`, which writes the unit, enables it and
# starts it, and prints exactly what it did. Everything the installer knows about
# ports, paths and the firewall is printed by that step, not by this script.
#
# Why system-wide and not --user: the unit runs as root. A `pip install --user`
# done by your login account is invisible to it, and the service then starts and
# dies on ImportError every 5 seconds. `python3 -m tvhub doctor` reports exactly
# this if it happens.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

PYTHON=${PYTHON:-python3}

if [ "$(id -u)" != "0" ]; then
    echo "ERROR this needs root - run: sudo ./install.sh" >&2
    exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "ERROR $PYTHON not found. Install Python 3.9 or newer, or set PYTHON=/path/to/python3." >&2
    exit 1
fi

# 3.9 is the floor the whole codebase is written against (contract 0.1).
"$PYTHON" - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 9):
    sys.stderr.write("ERROR Python 3.9 or newer is required, found %d.%d\n" % sys.version_info[:2])
    raise SystemExit(1)
EOF

echo "== installing dependencies system-wide"
# --break-system-packages is needed on Debian/Ubuntu's externally-managed
# Python (PEP 668); older pips reject the flag, so try without it as well.
"$PYTHON" -m pip install --upgrade -r requirements.txt --break-system-packages 2>/dev/null \
    || "$PYTHON" -m pip install --upgrade -r requirements.txt

echo
echo "== registering the service"
# TVHUB_HOME pins the machine-wide state folder for this and every child
# process, so a root service and a user CLI resolve the same tokens (contract 1).
TVHUB_HOME="$HERE" "$PYTHON" -m tvhub install

echo
echo "== diagnosis"
TVHUB_HOME="$HERE" "$PYTHON" -m tvhub doctor || true

cat <<EOF

Next steps
  1. Open the web interface and work through the wizard:
       http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(TVHUB_HOME="$HERE" "$PYTHON" -c 'import json,sys;print(json.load(open("config.json"))["server"]["http_port"])' 2>/dev/null || echo 8899)/ui/setup
  2. Set server.base_url to this host's RESERVED address. That string is typed
     into every TV's browser homepage by hand, so it must never change.
  3. This host must sit on the TVs' own subnet: Wake-on-LAN broadcasts, pairing
     and the UPnP volume read-back do not route.

  systemctl status tvhub      journalctl -u tvhub -f
  sudo ./uninstall.sh         removes the service, keeps config/state/photos
EOF
