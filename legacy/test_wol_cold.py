#!/usr/bin/env python3
"""Verify Wake-on-LAN wakes a FULLY-asleep TV (WS server dead)."""
import socket, time, warnings
warnings.filterwarnings("ignore")
from samsungtvws import SamsungTVWS

IP, MAC = "192.168.100.189", "00:7d:3b:59:01:51"


def state():
    try:
        return SamsungTVWS(host=IP, port=8002, token="16204609", name="MacControl",
                           timeout=4).rest_device_info().get("device", {}).get("PowerState")
    except Exception:
        return None


def wol():
    pkt = b"\xff" * 6 + bytes.fromhex(MAC.replace(":", "")) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for tgt in ("255.255.255.255", "192.168.100.255", IP):
        for p in (9, 7):
            try: s.sendto(pkt, (tgt, p))
            except OSError: pass
    s.close()


print(f"start state: {state()}")
print("turning OFF...")
try:
    SamsungTVWS(host=IP, port=8002, token="16204609", name="MacControl", timeout=8).send_key("KEY_POWER")
except Exception as e:
    print(" off err:", e)

print("waiting for WS server to fully die (up to 150s)...")
for i in range(30):
    time.sleep(5)
    st = state()
    print(f"  +{(i+1)*5:>3}s  state={st}")
    if st is None:
        print("  -> TV is now fully asleep (unreachable). Proceeding to WoL-only wake.")
        break
else:
    print("  -> NOTE: TV stayed reachable the whole time (never went fully cold).")

print("\nsending WoL ONLY (no power key)...")
wol()
for i in range(18):
    time.sleep(5)
    st = state()
    print(f"  +{(i+1)*5:>3}s  state={st}")
    if st == "on":
        print("\nRESULT: WoL WORKS — cold TV woke from magic packet alone. ✅")
        break
else:
    print("\nRESULT: WoL did NOT wake the TV. ❌ (needs a TV setting or another method)")
