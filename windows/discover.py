#!/usr/bin/env python3
"""Scan the LAN for Samsung TVs and report which are not yet in config.json.

    C:\\tvbridge\\discover.bat

Probes port 8002 (the control channel) across the local /24 - some sets answer
there but not on 8001 - then reads each one's identity over REST. Prints a
ready-to-paste config block for anything new. Read-only: changes nothing.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings("ignore")

import tvbridge as T


def local_prefix() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 9))
        return s.getsockname()[0].rsplit(".", 1)[0] + "."
    finally:
        s.close()


def alive(ip: str) -> str | None:
    for port in (8002, 8001):
        s = socket.socket()
        s.settimeout(1.0)
        try:
            s.connect((ip, port))
            return ip
        except Exception:
            continue
        finally:
            s.close()
    return None


def identify(ip: str) -> tuple[str, dict]:
    for scheme, port in (("http", 8001), ("https", 8002)):
        try:
            url = f"{scheme}://{ip}:{port}/api/v2/"
            ctx = None
            if scheme == "https":
                import ssl
                ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(url, timeout=5, context=ctx) as r:
                d = json.load(r).get("device", {})
            if d:
                return ip, d
        except Exception:
            continue
    return ip, {}


def main() -> int:
    prefix = sys.argv[1] + "." if len(sys.argv) > 1 else local_prefix()
    cfg = T.Config(T.CONFIG_PATH)
    known = {spec["ip"]: alias for alias, spec in cfg["tvs"].items()}

    print(f"scanning {prefix}0/24 for Samsung TVs ...")
    ips = [f"{prefix}{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as ex:
        live = [r for r in ex.map(alive, ips) if r]

    found = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for ip, d in ex.map(identify, live):
            if d.get("modelName") and str(d.get("type", "")).lower().find("tv") >= 0:
                found.append((ip, d))

    import html as H
    print(f"\n{len(found)} Samsung TV(s) responding:\n")
    new = []
    for ip, d in sorted(found, key=lambda x: int(x[0].split(".")[-1])):
        alias = known.get(ip)
        tag = f"in config as '{alias}'" if alias else "*** NOT IN CONFIG ***"
        print(f"  {ip:<16} {d.get('modelName','?'):<22} "
              f"{H.unescape(d.get('name','?'))[:24]:<26} {d.get('networkType','?'):<9} "
              f"power={d.get('PowerState','?'):<8} {tag}")
        if not alias:
            new.append((ip, d))

    if not new:
        print("\nNothing new - every responding TV is already in config.json.")
        return 0

    print(f"\n--- paste into config.json \"tvs\" ({len(new)} new) ---")
    for ip, d in new:
        mac = d.get("wifiMac", "") or ""
        print(json.dumps({
            "ip": ip,
            "mac": mac.lower(),
            "label": f"{H.unescape(d.get('name','?'))} ({d.get('modelName','?')}) - "
                     f"{d.get('networkType','?')}",
            "photos": {"method": "browser", "interval_seconds": 10, "fit": "contain"},
        }, indent=2))
    print("\nRemember to add each new alias to the 'home' group too, or pair.bat "
          "and /home/... will skip it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
