#!/usr/bin/env python3
"""Pair TVs one at a time, waiting for you at each screen, and VERIFY each one.

    C:\\tvbridge\\pair.bat              the TVs that need it, Gym first
    C:\\tvbridge\\pair.bat frames       limit to a group
    C:\\tvbridge\\pair.bat office       a single TV

Why one at a time: pairing every screen at once meant tokens got stored for TVs
that never actually accepted the prompt. The TV still completes a WebSocket
handshake with a bad token - it just answers `ms.error: No Authorized` to every
command afterwards - so a stored token is NOT proof of pairing. This checks each
TV by sending a real command and watching for that error.
"""
from __future__ import annotations

import re
import socket
import sys
import urllib.request
import time
import warnings

warnings.filterwarnings("ignore")

import tvbridge as T

FIRST = "gym"      # Drew asked to start here
GRANT_WINDOW = 90  # seconds to hold one Allow prompt open


def upnp_volume(ip: str) -> int | None:
    """Read the TV's volume over UPnP. Ground truth, and needs no pairing."""
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
           ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           '<u:GetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
           '<InstanceID>0</InstanceID><Channel>Master</Channel>'
           '</u:GetVolume></s:Body></s:Envelope>').encode()
    req = (f"POST /upnp/control/RenderingControl1 HTTP/1.1\r\nHost: {ip}:9197\r\n"
           f'Content-Type: text/xml; charset="utf-8"\r\n'
           f'SOAPACTION: "urn:schemas-upnp-org:service:RenderingControl:1#GetVolume"\r\n'
           f"Content-Length: {len(env)}\r\nConnection: close\r\n\r\n").encode() + env
    try:
        with socket.create_connection((ip, 9197), timeout=4) as sk:
            sk.sendall(req)
            time.sleep(0.4)
            buf = b""
            while True:
                chunk = sk.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except Exception:
        return None
    m = re.search(rb"<CurrentVolume>(\d+)</CurrentVolume>", buf)
    return int(m.group(1)) if m else None


def authorized(tv: T.Tv) -> tuple[bool, str]:
    """Does this TV actually obey us? Judged by effect, not by protocol.

    Earlier this opened its own WebSocket right after pairing closed one, which
    the TV refuses - so a TV that was correctly paired reported "did NOT pair".
    Now it nudges the volume through the running service and reads the volume
    back over UPnP, which is the only unambiguous evidence.
    """
    if not tv.token_file.exists():
        return False, "no token stored"
    before = upnp_volume(tv.ip)
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:8899/{tv.alias}/key/KEY_VOLUP", timeout=45) as r:
            reply = r.read().decode("utf-8", "replace").strip()
    except Exception as exc:
        return False, f"service call failed: {type(exc).__name__}"
    low = reply.lower()
    if "not paired" in low or "rejected" in low:
        return False, "TV rejected our token"

    if before is None:
        # No UPnP on this model, so fall back to what the service reported.
        return ("sent" in low), (reply[:70] if "sent" not in low else "accepted")

    time.sleep(2.5)
    after = upnp_volume(tv.ip)
    if after is not None and after > before:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:8899/{tv.alias}/key/KEY_VOLDOWN", timeout=45).read()
        except Exception:
            pass
        return True, f"volume {before} -> {after}, command obeyed"
    return False, f"volume did not move ({before} -> {after}) - key ignored"


def grant(tv: T.Tv) -> bool:
    """Hold the Allow prompt open, then confirm the new token really works."""
    tv.token_file.unlink(missing_ok=True)   # force a fresh grant
    tv._browser_id = T._UNSET
    try:
        ws = tv.control_ws(timeout=GRANT_WINDOW)
        ws.close()
    except Exception as exc:
        print(f"      connection failed: {type(exc).__name__}: {exc}")
        return False
    if not tv.token_file.exists():
        print("      no token issued - the prompt was probably not accepted")
        return False
    token = tv.token_file.read_text(encoding="utf-8").strip()
    print(f"      token {token} received, verifying it is accepted ...")
    time.sleep(1.5)
    ok, why = authorized(tv)
    print(f'      {why}')
    return ok


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "home"
    cfg = T.Config(T.CONFIG_PATH)
    T.setup_logging(cfg, False)
    T.migrate_state()
    bridge = T.Bridge(cfg)

    aliases = bridge.resolve(target)
    if not aliases:
        print(f"unknown target '{target}'. groups: {' '.join(sorted(cfg['groups']))}")
        return 1

    print(f"Checking which of {len(aliases)} TV(s) actually accept commands ...")
    print("(a brief volume blip on each is the test - it is put straight back)\n")
    todo, good, offline = [], [], []
    for alias in sorted(aliases):
        tv = bridge.tvs[alias]
        if tv.power_state() == "unreachable":
            offline.append(alias)
            print(f"  {alias:<14} OFFLINE - turn it on, then run this again")
            continue
        ok, why = authorized(tv)
        if ok:
            good.append(alias)
            print(f"  {alias:<14} already paired and working  ({why})")
        else:
            todo.append(alias)
            print(f"  {alias:<14} NEEDS PAIRING  ({why})")

    if not todo:
        print("\nNothing to pair - every reachable TV accepts commands.")
        return 0

    # Gym first, as asked, then the rest alphabetically.
    todo.sort(key=lambda a: (a != FIRST, a))

    print(f"\n{len(todo)} TV(s) to pair, one at a time:")
    for i, alias in enumerate(todo, 1):
        print(f"  {i}. {alias:<14} {bridge.tvs[alias].label}")
    print("\nFor each one: go to that TV, then press Enter (or 'y').")
    print("  s = skip it    q = quit\n")

    done, failed, skipped = [], [], []
    for i, alias in enumerate(todo, 1):
        tv = bridge.tvs[alias]
        while True:
            print(f"[{i}/{len(todo)}]  {alias}   {tv.label}")
            print(f"          {tv.ip}")
            answer = ask(f"          Ready at {alias}? [Enter=go, s=skip, q=quit] ")
            if answer == "q":
                print("\nstopped.")
                return 0
            if answer == "s":
                skipped.append(alias)
                print()
                break
            print(f"      Allow prompt is now on {alias} - accept it with the remote "
                  f"(up to {GRANT_WINDOW}s) ...")
            if grant(tv):
                done.append(alias)
                print(f"      *** {alias} PAIRED AND VERIFIED ***\n")
                break
            print(f"      {alias} did NOT pair.")
            again = ask("      Try again? [Enter=retry, s=skip, q=quit] ")
            if again == "q":
                print("\nstopped.")
                return 0
            if again == "s":
                skipped.append(alias)
                print()
                break

    print("=" * 58)
    print(f"paired and verified : {len(done)}   {' '.join(done)}")
    if skipped:
        print(f"skipped             : {len(skipped)}   {' '.join(skipped)}")
    if offline:
        print(f"offline             : {len(offline)}   {' '.join(offline)}")
    print(f"already working     : {len(good)}   {' '.join(good)}")
    if done:
        print("\nNow test them:  curl http://192.168.1.246:8899/home/off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
