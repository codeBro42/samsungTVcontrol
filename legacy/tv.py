#!/usr/bin/env python3
"""Control the Samsung Business TV at 192.168.100.84 over IP.

Usage:
  tv.py pair            # first-time setup: triggers "Allow" popup on the TV
  tv.py status          # power state + device info (no pairing needed)
  tv.py on              # wake the TV (WoL + power key)
  tv.py off             # put the TV in standby
  tv.py key KEY_VOLUP   # send any remote key code
  tv.py apps            # list installed apps
  tv.py app <app_id>    # launch an app by id

Common keys: KEY_VOLUP KEY_VOLDOWN KEY_MUTE KEY_SOURCE KEY_HDMI
             KEY_HOME KEY_RETURN KEY_ENTER KEY_UP/DOWN/LEFT/RIGHT
             KEY_CHUP KEY_CHDOWN KEY_0..KEY_9
"""
import html
import json
import os
import socket
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

from samsungtvws import SamsungTVWS

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())
# Override target TV with:  TV_IP=192.168.100.189 tv.py <cmd>
if os.environ.get("TV_IP"):
    CONFIG["ip"] = os.environ["TV_IP"]
CONFIG["mac"] = CONFIG.get("macs", {}).get(CONFIG["ip"])
TOKEN_FILE = HERE / f"token-{CONFIG['ip']}.txt"


def connect(timeout=15):
    return SamsungTVWS(
        host=CONFIG["ip"],
        port=8002,
        token_file=str(TOKEN_FILE),
        name="MacControl",
        timeout=timeout,
    )


def wake_on_lan():
    mac = CONFIG.get("mac")
    if not mac:
        print("No MAC in config.json — power-on after >1 min standby won't work.")
        print("Get the Wired MAC from TV: Settings > Support > About This TV")
        return
    packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # TV is on a different subnet: try directed broadcast, global broadcast,
    # and unicast to its IP in case the router still has the ARP entry.
    for target in (CONFIG["ip"].rsplit(".", 1)[0] + ".255", "255.255.255.255", CONFIG["ip"]):
        for port in (9, 7):
            try:
                s.sendto(packet, (target, port))
            except OSError:
                pass
    s.close()
    print(f"WoL magic packets sent for {mac}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        info = connect().rest_device_info()
        d = info.get("device", {})
        print(f"Name:  {html.unescape(d.get('name', ''))}")
        print(f"Model: {d.get('modelName')}")
        print(f"Power: {d.get('PowerState', 'unreachable/off')}")
        print(f"Net:   {d.get('networkType')}  IP: {d.get('ip')}")
    elif cmd == "pair":
        print("The TV must be ON (screen lit). Watch it and choose ALLOW with the remote.")
        print("You have 60 seconds...")
        connect(timeout=60).send_key("KEY_RETURN")
        print(f"Paired. Token saved to {TOKEN_FILE}")
    elif cmd in ("on", "off", "toggle"):
        state = None
        try:
            state = connect().rest_device_info().get("device", {}).get("PowerState")
        except Exception:
            pass  # unreachable = fully asleep
        if cmd == "toggle":
            cmd = "off" if state == "on" else "on"
        if cmd == "on":
            if state == "on":
                print(f"{CONFIG['ip']}: already on")
            else:
                wake_on_lan()
                try:
                    connect().send_key("KEY_POWER")
                except Exception:
                    pass  # WS down when fully asleep; WoL handles it
                print(f"{CONFIG['ip']}: power-on sent")
        else:
            if state != "on":
                print(f"{CONFIG['ip']}: already off")
            else:
                connect().send_key("KEY_POWER")
                print(f"{CONFIG['ip']}: standby sent")
    elif cmd == "key":
        connect().send_key(sys.argv[2])
        print(f"Sent {sys.argv[2]}")
    elif cmd == "apps":
        for app in connect().app_list():
            print(f"{app['appId']:<20} {app['name']}")
    elif cmd == "app":
        connect().rest_app_run(sys.argv[2])
        print(f"Launched {sys.argv[2]}")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        print("Tips: TV must be ON for pair/off/key. If pairing, press ALLOW on the")
        print("TV within 60s. If a token exists but stopped working, delete token.txt")
        print("and pair again.")
        sys.exit(1)
