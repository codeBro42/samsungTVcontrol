#!/usr/bin/env python3
"""Probe the TV's remote-control websocket on both ports and log raw events."""
import base64
import json
import ssl
import time
import warnings

warnings.filterwarnings("ignore")
import websocket

IP = "192.168.100.84"
NAME = base64.b64encode(b"MacControl").decode()

for port, scheme in ((8002, "wss"), (8001, "ws")):
    url = f"{scheme}://{IP}:{port}/api/v2/channels/samsung.remote.control?name={NAME}"
    print(f"\n=== {url}")
    start = time.time()
    try:
        ws = websocket.create_connection(
            url, timeout=40, sslopt={"cert_reqs": ssl.CERT_NONE}
        )
        print(f"[{time.time()-start:5.1f}s] TCP/WS connected, waiting for events...")
        while True:
            msg = ws.recv()
            t = time.time() - start
            try:
                ev = json.loads(msg)
                print(f"[{t:5.1f}s] event: {ev.get('event')}  data: {json.dumps(ev.get('data'))[:300]}")
                if ev.get("event") in ("ms.channel.connect", "ms.channel.unauthorized", "ms.channel.timeOut"):
                    break
            except json.JSONDecodeError:
                print(f"[{t:5.1f}s] raw: {msg[:200]}")
        ws.close()
    except Exception as e:
        print(f"[{time.time()-start:5.1f}s] {type(e).__name__}: {e}")
