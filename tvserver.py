#!/usr/bin/env python3
"""Tiny HTTP shim so Loxone (or curl) can control the Samsung TVs.

Run it (stays running):
    cd ~/Documents/Claude/samsung-tv
    ./venv/bin/python tvserver.py            # listens on 0.0.0.0:8899

From any machine on the LAN (Loxone Virtual Output, curl, browser):
    curl localhost:8899/golf-right/on
    curl localhost:8899/bar/off               # group: the four bar TVs
    curl localhost:8899/venue/status          # all ten venue TVs
    curl localhost:8899/lounge/key/KEY_VOLUP  # any Samsung remote key
    curl localhost:8899/zach/toggle

Targets: a TV alias or a group name (see TVS / GROUPS below).
Actions: on, off, toggle, status, key/<KEY_CODE>, app/<app_id>

First on/off/key per TV pops an "Allow" prompt on that screen - accept it
once with the TV remote and the token is saved next to this script.
"""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = str(HERE / "venv" / "bin" / "python")
TVPY = str(HERE / "tv.py")
PORT = 8899

TVS = {
    # venue (192.168.1.x)
    "golf-right": "192.168.1.88",
    "golf-left":  "192.168.1.107",
    "bar-side":   "192.168.1.58",
    "back-bar":   "192.168.1.68",
    "over-bar":   "192.168.1.126",
    "bar-55":     "192.168.1.131",
    "lounge":     "192.168.1.69",
    "pavillion":  "192.168.1.163",
    "zach":       "192.168.1.153",
    "kitchen":    "192.168.1.127",
    # office (192.168.100.x, original site)
    "business": "192.168.100.84",
    "crystal":  "192.168.100.189",
}
GROUPS = {
    "golf":   ["golf-right", "golf-left"],
    "bar":    ["bar-side", "back-bar", "over-bar", "bar-55"],
    "venue":  ["golf-right", "golf-left", "bar-side", "back-bar", "over-bar",
               "bar-55", "lounge", "pavillion", "zach", "kitchen"],
    "office": ["business", "crystal"],
    "all":    list(TVS),
}
SIMPLE_ACTIONS = {"on", "off", "toggle", "status"}


def run(alias, args):
    try:
        p = subprocess.run(
            [PY, TVPY, *args],
            env={"TV_IP": TVS[alias], "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60,
        )
        out = (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        out = "timed out"
    return f"[{alias}] " + out.replace("\n", f"\n[{alias}] ")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = (body + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if not parts or parts[0] in ("help", ""):
            return self._send(200, __doc__)

        target = parts[0]
        aliases = GROUPS.get(target) or ([target] if target in TVS else None)
        if aliases is None:
            return self._send(404, "unknown TV '%s'\ntvs: %s\ngroups: %s"
                              % (target, " ".join(TVS), " ".join(GROUPS)))

        if len(parts) == 2 and parts[1] in SIMPLE_ACTIONS:
            args = [parts[1]]
        elif len(parts) == 3 and parts[1] == "key" and parts[2].startswith("KEY_"):
            args = ["key", parts[2]]
        elif len(parts) == 3 and parts[1] == "app":
            args = ["app", parts[2]]
        else:
            return self._send(
                400,
                "usage: /<tv|group>/<on|off|toggle|status>\n"
                "       /<tv|group>/key/<KEY_CODE>   e.g. /lounge/key/KEY_VOLUP\n"
                "       /<tv|group>/app/<app_id>",
            )

        with ThreadPoolExecutor(max_workers=len(aliases)) as pool:
            out = list(pool.map(lambda a: run(a, args), aliases))
        self._send(200, "\n".join(out))

    def do_POST(self):
        self.do_GET()

    def log_message(self, *a):
        pass  # quiet


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"TV control server on http://0.0.0.0:{PORT}")
    print(f"  curl localhost:{PORT}/venue/status   |   /golf-right/on   |   /bar/off")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        sys.exit(0)
