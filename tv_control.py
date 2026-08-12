#!/usr/bin/env python3
r"""
Samsung TV Power Control - interactive terminal menu.
Controls BOTH TVs (power on / power off) from one Windows PC.

ONE-TIME SETUP (Windows):
    py -m pip install samsungtvws
RUN (use run_tv.bat, or):
    py tv_control.py

Menu: 1=ON both, 2=OFF both, 3=Status, D=Diagnostics, P=Re-pair, Q=Quit

The pairing token is saved in a per-user folder (see TOKEN_DIR below) so it
PERSISTS after you close the window - you should only ever need to allow the
connection on the TV once.
"""

import os
import socket
import sys
import time
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

try:
    from samsungtvws import SamsungTVWS
except ImportError:
    print("\n*** Missing library 'samsungtvws'. Install it first with:")
    print("       py -m pip install samsungtvws\n")
    input("Press Enter to close...")
    sys.exit(1)

try:
    from samsungtvws.exceptions import UnauthorizedError
except Exception:  # older/newer layouts
    class UnauthorizedError(Exception):
        pass


def _is_auth_error(e):
    """True only for a genuine 'token rejected' failure - NOT for timeouts,
    connection-refused (TV in standby), or unreachable network. We must never
    discard a good token on a transient error."""
    if isinstance(e, UnauthorizedError):
        return True
    return "unauthor" in str(e).lower()

# ---------------------------------------------------------------------------
CLIENT_NAME = "MacControl"  # must match the name the tokens were issued to

TVS = [
    {"label": "Business TV (85in)", "ip": "192.168.100.84",  "mac": "b8:a0:b8:4c:21:96", "token": "44781245"},
    {"label": "Crystal UHD (85in)", "ip": "192.168.100.189", "mac": "00:7d:3b:59:01:51", "token": "16204609"},
]

# Persist tokens in a guaranteed-writable per-user folder (NOT next to the
# script, which on Windows may sit under Program Files / OneDrive / read-only).
TOKEN_DIR = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()) / "SamsungTVControl"
TOKEN_DIR.mkdir(parents=True, exist_ok=True)
# ---------------------------------------------------------------------------


def token_path(tv):
    return TOKEN_DIR / f"token_{tv['ip']}.txt"


def seed_token(tv):
    """First run: drop the known-good token into the file so no popup is needed.
    samsungtvws will overwrite it automatically if the TV issues a new one."""
    f = token_path(tv)
    if not f.exists():
        try:
            f.write_text(tv["token"])
        except OSError:
            pass


def client(tv, timeout=10):
    """Always use token_file so the token is READ and auto-SAVED across runs."""
    seed_token(tv)
    return SamsungTVWS(host=tv["ip"], port=8002, token_file=str(token_path(tv)),
                       name=CLIENT_NAME, timeout=timeout)


def power_state(tv):
    """'on', 'standby', or None if unreachable."""
    try:
        info = client(tv, timeout=5).rest_device_info()
        return info.get("device", {}).get("PowerState")
    except Exception:
        return None


def send_key(tv, key, pair_timeout=45):
    """Send a key. ONLY on a genuine auth rejection do we clear the token and
    re-pair (Allow popup) once. Transient errors (standby/timeout/unreachable)
    NEVER touch the saved token. Returns (ok, note)."""
    try:
        client(tv).send_key(key)
        return True, ""
    except Exception as e1:
        if not _is_auth_error(e1):
            return False, f"not reachable for control ({type(e1).__name__})"
        # token was rejected -> re-pair once and save the fresh one
        try:
            token_path(tv).unlink()
        except OSError:
            pass
        print(f"    -> {tv['label']}: authorize needed - press ALLOW on the TV "
              f"now (within {pair_timeout}s)...")
        try:
            SamsungTVWS(host=tv["ip"], port=8002, token_file=str(token_path(tv)),
                        name=CLIENT_NAME, timeout=pair_timeout).send_key(key)
            return True, "(paired + saved)"
        except Exception as e2:
            return False, f"pairing failed ({type(e2).__name__})"


def local_ip_for(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((ip, 80))
        addr = s.getsockname()[0]
        s.close()
        return addr
    except Exception:
        return None


def same_subnet(a, b):
    return bool(a and b and a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0])


def wake_on_lan(tv):
    mac = bytes.fromhex(tv["mac"].replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    subnet_bcast = tv["ip"].rsplit(".", 1)[0] + ".255"
    for target in ("255.255.255.255", subnet_bcast, tv["ip"]):
        for port in (9, 7):
            try:
                s.sendto(packet, (target, port))
            except OSError:
                pass
    s.close()


def power_on(tv):
    st = power_state(tv)
    if st == "on":
        return "already ON"
    wake_on_lan(tv)
    note = "WoL sent"
    if st is not None:  # reachable in standby -> power key works (routable)
        ok, n = send_key(tv, "KEY_POWER")
        note += "; power key " + ("sent " + n if ok else n)
    else:
        note += "; TV unreachable -> WoL only (needs same subnet)"
    time.sleep(5)
    final = power_state(tv)
    return f"{note}  ->  {'ON' if final == 'on' else 'still ' + (final or 'unreachable')}"


def power_off(tv):
    if power_state(tv) != "on":
        return "already off"
    ok, n = send_key(tv, "KEY_POWER")
    if not ok:
        return n
    time.sleep(3)
    return "OFF" if power_state(tv) != "on" else "sent " + n


def status(tv):
    st = power_state(tv)
    return {"on": "ON", "standby": "off (standby)"}.get(st, "UNREACHABLE")


def diagnostics(tv):
    lip = local_ip_for(tv["ip"])
    subnet = "same subnet (WoL ok)" if same_subnet(lip, tv["ip"]) else "DIFFERENT subnet (WoL won't cross)"
    st = power_state(tv)
    ctrl = "reachable" if st is not None else "NOT reachable on 8002"
    tok = "saved" if token_path(tv).exists() else "none"
    return f"thisPC={lip or '?'}, {subnet}; control {ctrl}; power={st or '?'}; token={tok}"


def repair(tv):
    try:
        token_path(tv).unlink()
    except OSError:
        pass
    print(f"    {tv['label']}: press ALLOW on the TV within ~40s...")
    ok, n = send_key(tv, "KEY_RETURN")
    return "paired OK" if ok else n


def run_all(fn, banner):
    print(f"\n{banner}")
    for tv in TVS:
        print(f"    {tv['label']:<22} {fn(tv)}")


MENU = """
==================================
    Samsung TV Power Control
==================================
  [1]  Power ON   (both TVs)
  [2]  Power OFF  (both TVs)
  [3]  Status
  [D]  Diagnostics
  [P]  Re-pair a TV
  [Q]  Quit
----------------------------------"""


def main():
    print(f"(tokens stored in: {TOKEN_DIR})")
    while True:
        print(MENU)
        try:
            choice = input("Select: ").strip().lower()
        except EOFError:
            return
        if choice == "1":
            run_all(power_on, "Turning both TVs ON...")
        elif choice == "2":
            run_all(power_off, "Turning both TVs OFF...")
        elif choice == "3":
            run_all(status, "Status:")
        elif choice == "d":
            run_all(diagnostics, "Diagnostics:")
        elif choice == "p":
            run_all(repair, "Re-pairing...")
        elif choice in ("q", "quit", "exit"):
            print("Bye.")
            return
        else:
            print("  ? Enter 1, 2, 3, D, P, or Q.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n*** Unexpected error:\n")
        traceback.print_exc()
        input("\nPress Enter to close...")
