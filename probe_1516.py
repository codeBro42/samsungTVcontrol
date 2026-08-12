#!/usr/bin/env python3
"""Probe TCP 1516 on the TVs: banner, then a read-only MDC status query."""
import socket
import sys
import time

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.100.189"


def hexdump(b):
    return b.hex(" ") + ("  |" + "".join(chr(c) if 32 <= c < 127 else "." for c in b) + "|" if b else "")


def probe(payload, label):
    print(f"\n--- {label}")
    try:
        s = socket.create_connection((IP, 1516), timeout=5)
        s.settimeout(4)
        try:
            banner = s.recv(256)
            print(f"banner: {hexdump(banner)}")
        except socket.timeout:
            print("banner: (none)")
        if payload:
            s.sendall(payload)
            print(f"sent:   {hexdump(payload)}")
            try:
                while True:
                    data = s.recv(256)
                    if not data:
                        print("reply:  (connection closed)")
                        break
                    print(f"reply:  {hexdump(data)}")
            except socket.timeout:
                print("reply:  (timeout, no more data)")
        s.close()
    except Exception as e:
        print(f"{type(e).__name__}: {e}")


probe(None, "connect only, wait for banner")
probe(bytes([0xAA, 0x00, 0x00, 0x00, 0x00]), "MDC status query (AA 00 00 00 00)")
probe(b"\r\n", "CRLF (text protocol check)")
