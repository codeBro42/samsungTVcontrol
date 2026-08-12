#!/usr/bin/env python3
"""tvbridge - control Samsung Tizen TVs from anything that can send an IP packet.

Runs as a long-lived service on a Windows PC that sits on the TVs' subnet and
listens on three transports at once, so almost any controller can drive it:

    HTTP   GET/POST http://<pc>:8899/<target>/<action>      (Loxone, curl, browser)
    TCP    one command per line to <pc>:8900                (Crestron, Q-SYS, Loxone tcp://)
    UDP    one command per datagram to <pc>:8900            (fire-and-forget)

The command grammar is identical on all three - "<target>/<action>" or
"<target> <action>", where target is a TV alias or a group name:

    business/on            crystal/off          office/toggle       all/status
    lounge/key/KEY_VOLUP   bar/volume/35        bar/mute/on
    business/photos        crystal/photos/lobby business/photos/off
    business/source/hdmi1  business/macro/usb-photos
    business/app/3201907018784

Photo playlists (see README) run one of three ways per TV, set by
`photos.method` in config.json:

    browser  this PC serves a slideshow web page and the TV's browser is
             pointed at it - deterministic, no USB stick, recommended
    art      Frame TVs only: native Art Mode slideshow over the art API
    usb      blind remote-key macro into the USB media player - works, but
             has to be calibrated per model with `tvbridge.py learn`

Setup:
    py -m pip install -r requirements.txt
    py tvbridge.py pair business        # once per TV, press ALLOW on the screen
    py tvbridge.py run                  # or install.bat to run it at boot
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# Machine-wide, NOT per-user. %LOCALAPPDATA% resolves to a different folder for
# the SYSTEM service than for a CLI run by a logged-in user, so pairing from a
# terminal wrote tokens the service could never see - it reported every TV as
# unpaired while 14 valid tokens sat in the user's profile.
STATE_DIR = HERE / "state"

# Where tokens used to land, so an existing install keeps its pairings.
LEGACY_STATE_DIRS = [
    Path(os.environ["LOCALAPPDATA"]) / "SamsungTVControl"
    if os.environ.get("LOCALAPPDATA") else None,
    Path(r"C:\Windows\System32\config\systemprofile\AppData\Local\SamsungTVControl"),
    Path(r"C:\Users\dream\AppData\Local\SamsungTVControl"),
]


def migrate_state() -> None:
    """Adopt tokens from the old per-user locations, newest wins. Idempotent."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("cannot create state dir %s: %s", STATE_DIR, exc)
        return
    moved = 0
    for legacy in LEGACY_STATE_DIRS:
        if not legacy or legacy == STATE_DIR or not legacy.is_dir():
            continue
        for src in list(legacy.glob("token-*.txt")) + list(legacy.glob("state.json")):
            dst = STATE_DIR / src.name
            try:
                if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                    continue
                if not src.read_text(encoding="utf-8").strip():
                    continue  # never adopt an empty token
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                moved += 1
            except OSError as exc:
                log.warning("could not adopt %s: %s", src, exc)
    if moved:
        log.info("adopted %d token(s) from previous state folders", moved)

log = logging.getLogger("tvbridge")

_UNSET = object()  # "not probed yet", distinct from a probed result of None


class NotPaired(Exception):
    """TV refused the token - a human has to press Allow on the screen."""


# What each TV is doing right now, so the dashboard can show "waiting for the
# page, 12s left" instead of an unexplained pause. alias -> (text, deadline|None)
_activity: dict[str, tuple[str, float | None]] = {}
_activity_lock = threading.Lock()


def progress(alias: str, text: str, seconds: float | None = None) -> None:
    """Publish what this TV is doing; `seconds` marks a bounded wait."""
    with _activity_lock:
        _activity[alias] = (text, time.monotonic() + seconds if seconds else None)


def progress_done(alias: str) -> None:
    with _activity_lock:
        _activity.pop(alias, None)


def progress_of(alias: str) -> tuple[str | None, float | None]:
    with _activity_lock:
        entry = _activity.get(alias)
    if not entry:
        return None, None
    text, until = entry
    left = max(0.0, until - time.monotonic()) if until else None
    return text, left


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "http_port": 8899,
    "tcp_port": 8900,
    "udp_port": 8900,
    "client_name": "MacControl",  # tokens are bound to this string - do not change
    "ws_timeout": 10,
    "photo_root": "photos",
    "log_file": "tvbridge.log",
    # Blank = work out this PC's own address per TV. Set it once the host has a
    # fixed IP, so the URLs the TVs are pointed at never change.
    "base_url": "",
    "allow_from": [],  # empty = any source may send commands
    "tvs": {},
    "groups": {},
    "macros": {},
}


class Config:
    """config.json, reloadable at runtime via /reload."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in raw.items() if not k.startswith("_")})
        self.data = merged

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def save(self, data: dict) -> None:
        """Write config.json atomically, keeping the _comment keys intact."""
        raw = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw.update(data)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def photo_root(self) -> Path:
        root = Path(self.data["photo_root"])
        return root if root.is_absolute() else HERE / root


# --------------------------------------------------------------------------- #
# low-level network helpers
# --------------------------------------------------------------------------- #

def local_ip_toward(host: str) -> str:
    """Source IP this PC would use to reach `host` - correct on multi-homed PCs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))  # UDP connect sends nothing
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def wake_on_lan(mac: str, ip: str, bursts: int = 6) -> None:
    """Broadcast a magic packet. Directed + global broadcast + unicast, ports 9 and 7.

    A fully-asleep Tizen set has no open TCP ports, so WoL is the only way in;
    it needs this PC to be on the TV's subnet. Sent as a burst over ~3 s because
    a set in deep standby samples the wire intermittently and ignores a single
    packet - measured needing ~5 tries on a 2026 model.
    """
    clean = mac.replace(":", "").replace("-", "").strip()
    packet = b"\xff" * 6 + bytes.fromhex(clean) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    targets = (ip.rsplit(".", 1)[0] + ".255", "255.255.255.255", ip)
    for burst in range(bursts):
        for target in targets:
            for port in (9, 7):
                try:
                    s.sendto(packet, (target, port))
                except OSError:
                    pass
        if burst < bursts - 1:
            time.sleep(0.5)
    s.close()


def upnp_soap(ip: str, action: str, body_xml: str, timeout: float = 4.0) -> bool:
    """Raw SOAP to the TV's UPnP RenderingControl on :9197.

    Volume/mute are the one thing Tizen exposes without the paired WebSocket,
    which makes them usable even before a TV has been paired.
    """
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body_xml}</s:Body></s:Envelope>"
    ).encode()
    request = (
        f"POST /upnp/control/RenderingControl1 HTTP/1.1\r\n"
        f"Host: {ip}:9197\r\n"
        f'Content-Type: text/xml; charset="utf-8"\r\n'
        f'SOAPACTION: "urn:schemas-upnp-org:service:RenderingControl:1#{action}"\r\n'
        f"Content-Length: {len(envelope)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + envelope
    try:
        with socket.create_connection((ip, 9197), timeout=timeout) as s:
            s.sendall(request)
            reply = s.recv(2048)
        return b"200 OK" in reply
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# one TV
# --------------------------------------------------------------------------- #

class Tv:
    def __init__(self, alias: str, spec: dict, cfg: Config):
        self.alias = alias
        self.cfg = cfg
        self.ip: str = spec["ip"]
        self.mac: str | None = spec.get("mac")
        self.label: str = spec.get("label", alias)
        self.photos_cfg: dict = spec.get("photos", {})
        self.macros: dict = spec.get("macros", {})
        self.seed_token: str | None = spec.get("token")
        self._browser_id = _UNSET
        self._is_frame = _UNSET
        self._art_broken = False
        self._ws = None            # kept-open control socket for the web remote
        self._ws_until = 0.0
        self.lock = threading.Lock()  # one WS conversation per TV at a time

    # -- connection ------------------------------------------------------- #

    @property
    def token_file(self) -> Path:
        return STATE_DIR / f"token-{self.ip}.txt"

    def connect(self, timeout: float | None = None):
        from samsungtvws import SamsungTVWS

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Tokens are bound to the client name, not to a machine, so a token
        # granted elsewhere can be seeded here instead of re-pairing.
        if self.seed_token and not self.token_file.exists():
            self.token_file.write_text(self.seed_token, encoding="utf-8")
            log.info("%s: seeded token from config", self.alias)
        return SamsungTVWS(
            host=self.ip,
            port=8002,
            token_file=str(self.token_file),  # string path = persisted; token= is RAM-only
            name=self.cfg["client_name"],
            timeout=timeout or self.cfg["ws_timeout"],
        )

    def control_ws(self, timeout: float | None = None):
        """Open the remote-control channel ourselves.

        samsungtvws treats any frame that is not ms.channel.connect as a fatal
        ConnectionFailure, and the TV sends ms.remote.touchDisable whenever the
        browser is on screen - which made power-off fail exactly while the
        slideshow was running. Read past benign events instead.
        """
        import base64
        import ssl

        import websocket

        timeout = timeout or self.cfg["ws_timeout"]
        token = ""
        if self.token_file.exists():
            token = self.token_file.read_text(encoding="utf-8").strip()
        token = token or (self.seed_token or "")

        name = base64.b64encode(self.cfg["client_name"].encode()).decode()
        url = f"wss://{self.ip}:8002/api/v2/channels/samsung.remote.control?name={name}"
        if token:
            url += f"&token={token}"

        ws = websocket.create_connection(
            url, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = ws.recv()
            except Exception as exc:          # socket timeout or reset
                ws.close()
                raise TimeoutError(f"{self.alias}: {type(exc).__name__} waiting for channel") from exc
            if not raw:                       # peer closing - do not spin on it
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            event = msg.get("event")
            if event == "ms.channel.connect":
                issued = str((msg.get("data") or {}).get("token") or "")
                if issued and issued != token:
                    self.token_file.parent.mkdir(parents=True, exist_ok=True)
                    self.token_file.write_text(issued, encoding="utf-8")
                    log.info("%s: token updated by TV", self.alias)
                return ws
            if event in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                ws.close()
                raise NotPaired(f"{self.alias}: {event}")
            log.debug("%s: ignoring pre-connect event %s", self.alias, event)
        ws.close()
        raise TimeoutError(f"{self.alias}: no ms.channel.connect")

    def _drain_for_auth_error(self, ws, seconds: float) -> None:
        """Raise NotPaired if the TV answers `ms.error: No Authorized`.

        Must drain, not peek: the TV interleaves ms.remote.touchEnable /
        touchDisable frames, so reading a single frame usually returns one of
        those and the real error goes unnoticed - which made a rejected token
        report as "sent".
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ws.settimeout(max(0.2, deadline - time.monotonic()))
            try:
                raw = ws.recv()
            except Exception:
                return
            if not raw:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if (msg.get("event") == "ms.error"
                    and "authorized" in str(msg.get("data", {})).lower()):
                raise NotPaired(f"{self.alias}: TV rejected our token")

    @staticmethod
    def _click(ws, key: str) -> None:
        ws.send(json.dumps({"method": "ms.remote.control", "params": {
            "Cmd": "Click", "DataOfCmd": key,
            "Option": "false", "TypeOfRemote": "SendRemoteKey"}}))

    def power_state(self) -> str:
        """'on', 'standby', or 'unreachable' (fully asleep). Needs no pairing."""
        try:
            info = self.connect(timeout=4).rest_device_info()
        except Exception:
            return "unreachable"
        state = info.get("device", {}).get("PowerState")
        return state or "standby"

    # -- actions ---------------------------------------------------------- #

    def status(self) -> str:
        try:
            d = self.connect(timeout=4).rest_device_info().get("device", {})
        except Exception:
            return f"power=unreachable ip={self.ip} (asleep or off the network)"
        return (
            f"power={d.get('PowerState', 'unknown')} "
            f"model={d.get('modelName', '?')} "
            f"name={html.unescape(d.get('name', '?'))} "
            f"net={d.get('networkType', '?')} ip={self.ip} "
            f"paired={'yes' if self.token_file.exists() else 'no'}"
        )

    @staticmethod
    def explain_art(exc: Exception) -> str:
        if isinstance(exc, NotPaired) or "Unauthorized" in type(exc).__name__:
            return "not paired - run: py tvbridge.py pair <alias> and press ALLOW"
        return f"{type(exc).__name__}: {exc}"

    def is_frame(self) -> bool:
        """True for a Frame. Cached, and read over REST so it needs no pairing.

        Frames matter here because their power button does not go to standby - it
        toggles Art Mode, and PowerState stays "on" in both states.
        """
        if self._is_frame is not _UNSET:
            return self._is_frame
        result = False
        try:
            with urllib.request.urlopen(f"http://{self.ip}:8001/api/v2/", timeout=4) as r:
                d = json.load(r).get("device", {})
            result = str(d.get("FrameTVSupport", "")).lower() == "true"
        except Exception:
            return False  # unreachable: don't cache, fall back to key behaviour
        self._is_frame = result
        return result

    def _drop_ws(self) -> None:
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._ws_until = 0.0

    def send_key_fast(self, key: str) -> str:
        """Low-latency single keypress, for the on-screen remote.

        Holds the control socket open between presses and skips the auth
        read-back. A fresh handshake plus a 0.6 s pre-check and a 2 s post-check
        per press made the web remote feel three seconds behind every tap; an
        interactive user sees the TV respond, so the read-back buys nothing here.
        Macros still use send_keys, which keeps the checks and the inter-key gap.
        """
        if not key.startswith("KEY_"):
            key = "KEY_" + key.upper()
        with self.lock:
            last = None
            for attempt in (1, 2):
                try:
                    if self._ws is None or time.monotonic() > self._ws_until:
                        self._drop_ws()
                        self._ws = self.control_ws()
                    self._click(self._ws, key)
                    self._ws_until = time.monotonic() + 30
                    return f"sent {key}"
                except Exception as exc:
                    last = exc
                    self._drop_ws()   # stale socket: rebuild once and retry
            raise last if last else RuntimeError("send failed")

    def control_ready(self, seconds: float = 30.0) -> bool:
        """Wait until the TV accepts control connections on 8002.

        Art mode and standby close that port, so a key sent immediately after
        leaving either is refused with WinError 10061 and silently lost - which
        is why the slideshow sometimes never loaded.
        """
        deadline = time.monotonic() + seconds
        progress(self.alias, "waiting for the TV to accept commands", seconds)
        while time.monotonic() < deadline:
            sk = socket.socket()
            sk.settimeout(1.5)
            try:
                sk.connect((self.ip, 8002))
                return True
            except Exception:
                time.sleep(1.5)
            finally:
                sk.close()
        return False

    def browser_state(self) -> str:
        """'running' / 'stopped' / 'unknown' from DIAL. Plain HTTP, so unlike the
        art channel it cannot hang, which makes it a usable proxy for whether a
        Frame is in art mode: entering art mode closes the browser."""
        try:
            with urllib.request.urlopen(
                    f"http://{self.ip}:8080/ws/app/WebBrowser", timeout=4) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception:
            return "unknown"
        m = re.search(r"<state>([^<]*)</state>", body)
        return m.group(1) if m else "unknown"

    def art_set_and_confirm(self, want_on: bool, seconds: float = 12.0) -> bool | None:
        """Set Art Mode and confirm it. True / False / None(unreachable)."""
        want = "on" if want_on else "off"
        progress(self.alias, f"setting art mode {want}", seconds + 4)

        def work():
            try:
                art = self.connect(timeout=8).art()
                if str(art.get_artmode()) == want:
                    return True
                art.set_artmode(want_on)
            except Exception as exc:
                log.debug("%s: art mode unreachable: %s", self.alias, exc)
                return None
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                time.sleep(1.5)
                try:
                    if str(art.get_artmode()) == want:
                        return True
                except Exception:
                    break
            return False

        return self._bounded_art(work, seconds + 12)

    def _bounded_art(self, fn, bound: float):
        """Run one art-channel operation detached, with a hard wall-clock bound.

        The art channel can block far past its socket timeout on some Frames.
        Doing that while holding Tv.lock wedged the TV permanently and stalled
        whole group commands, so nothing here holds the lock while it waits.
        """
        if self._art_broken:
            return None
        if not self.lock.acquire(timeout=3):
            return None
        box = {}
        try:
            t = threading.Thread(target=lambda: box.__setitem__("r", fn()),
                                 name=f"art:{self.alias}", daemon=True)
            t.start()
            t.join(bound)
        finally:
            self.lock.release()
        if t.is_alive():
            log.warning("%s: art channel hangs, abandoning and not retrying",
                        self.alias)
            self._art_broken = True
            return None
        return box.get("r")

    def art_mode_state(self) -> str | None:
        def work():
            try:
                return str(self.connect(timeout=8).art().get_artmode())
            except Exception as exc:
                log.debug("%s: get_artmode failed: %s", self.alias, exc)
                return None
        return self._bounded_art(work, 12)

    def await_art(self, want_on: bool, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        want = "on" if want_on else "off"
        while time.monotonic() < deadline:
            time.sleep(1.5)
            if self.art_mode_state() == want:
                return True
        return False

    def await_state(self, want_on: bool, seconds: float, what: str = "") -> bool:
        """Poll until the TV reports the wanted power state. Returns success."""
        progress(self.alias, what or ("waiting for power on" if want_on
                                      else "waiting for standby"), seconds)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(2)
            if (self.power_state() == "on") == want_on:
                return True
        return False

    def on(self) -> str:
        """Wake, then confirm. Reports the state the TV actually reached.

        A power command is judged by the resulting PowerState, never by whether
        the WebSocket conversation completed tidily - the TV tears the channel
        down as it changes power state, so protocol errors here are normal and
        say nothing about whether the key landed.
        """
        # A Frame's "on" means out of Art Mode, not out of standby.
        if self.is_frame():
            # Already showing the slideshow? Then it is not in art mode, and the
            # art call is both pointless and the slowest thing we could do.
            if fetched_since(self.ip, time.monotonic() - 90):
                return "already on (slideshow on screen)"
            forget_fetch(self.ip)
            if self.power_state() == "unreachable" and self.mac:
                wake_on_lan(self.mac, self.ip)
                self.await_state(True, 25)
            got = self.art_set_and_confirm(False)
            if got is True:
                return "on (art mode off)"
            if got is None:
                return "art mode not reachable - carrying on to the slideshow anyway"
            return "WARNING asked the Frame to leave art mode but it still reports art mode on"

        if self.power_state() == "on":
            return "already on"
        forget_fetch(self.ip)
        started = time.monotonic()

        # Wake-on-LAN first, and confirm before touching the remote at all:
        # KEY_POWER is a TOGGLE on this hardware, so sending it to a TV that WoL
        # already woke would switch it straight back off.
        if self.mac:
            progress(self.alias, "sending Wake-on-LAN", 4)
            wake_on_lan(self.mac, self.ip)
            if self.await_state(True, 20, "waiting for Wake-on-LAN"):
                return f"on (confirmed in {time.monotonic() - started:.0f}s)"
        else:
            log.warning("%s: no MAC in config - cannot wake from deep standby", self.alias)

        # WoL did not do it - try the remote, which works from light standby.
        progress(self.alias, "sending power key")
        try:
            self.send_keys(["KEY_POWER"])
        except Exception as exc:
            log.debug("%s: power key failed (expected while fully asleep): %s", self.alias, exc)
        if self.await_state(True, 20, "waiting for power on (after key)"):
            return f"on (confirmed in {time.monotonic() - started:.0f}s)"

        if not self.mac:
            return "WARNING no MAC set, so no Wake-on-LAN - cannot wake this TV"
        return ("WARNING power-on sent but the TV is still not responding. Wake-on-LAN "
                "does not route between subnets - this PC must be on the TV's subnet.")

    def off(self) -> str:
        # On a Frame, "off" is Art Mode: the panel shows artwork rather than going
        # black, and PowerState reports "on" throughout. Judging this one by
        # PowerState produced a false "still reports on" warning.
        if self.is_frame():
            if self.power_state() == "unreachable":
                return "already off"
            forget_fetch(self.ip)
            got = None if self._art_broken else self.art_set_and_confirm(True, seconds=6)
            if got is True:
                return "art mode on (a Frame's off state - PowerState stays 'on')"

            # The art channel is unusable on some Frames (it hangs), so fall back
            # to the power key - which on a Frame toggles art mode - and verify by
            # watching the browser close, over DIAL rather than the art channel.
            if self.browser_state() == "stopped":
                return "already in art mode (browser closed)"
            progress(self.alias, "art channel unusable - using the power key", 20)
            try:
                self.send_keys(["KEY_POWER"])
            except Exception as exc:
                return f"ERROR could not send the power key: {self.explain_art(exc)}"
            for _ in range(10):
                time.sleep(1.5)
                if self.browser_state() == "stopped":
                    forget_fetch(self.ip)   # stop a stale heartbeat reading as playing
                    return "art mode on (via power key - browser closed)"
            return ("WARNING sent the power key but this Frame's browser is still "
                    "running, so it may still be showing the slideshow")

        if self.power_state() != "on":
            return "already off"
        forget_fetch(self.ip)
        # KEY_POWER only. KEY_POWEROFF is silently ignored by this firmware
        # (measured: no effect after 61 s), and sending it first only wasted the
        # confirmation window before the key that actually works.
        progress(self.alias, "sending power key")
        try:
            self.send_keys(["KEY_POWER"])
        except Exception as exc:
            # Expected: the TV drops the channel as it powers down.
            log.debug("%s: power key raised (often benign): %s", self.alias, exc)
        if self.await_state(False, 15, "waiting for standby"):
            return "standby (confirmed)"
        return "WARNING power-off sent but the TV still reports on"

    def toggle(self) -> str:
        return self.off() if self.power_state() == "on" else self.on()

    def send_keys(self, keys: list[str]) -> str:
        """Send a key sequence. '@250' waits 250 ms; 'KEY_X*3' repeats 3 times."""
        sent = 0
        with self.lock:
            ws = self.control_ws()
            try:
                # A bad token still completes the handshake; the TV only objects
                # once you send something. Reporting "sent" on a rejected token is
                # what made a failed power-off look like a success.
                self._drain_for_auth_error(ws, 0.6)
                ws.settimeout(self.cfg["ws_timeout"])
                for token in keys:
                    if token.startswith("@"):
                        time.sleep(int(token[1:]) / 1000.0)
                        continue
                    key, _, count = token.partition("*")
                    for _ in range(int(count) if count else 1):
                        self._click(ws, key)
                        sent += 1
                        time.sleep(0.35)  # Tizen drops keys sent back-to-back
                # Read back: the TV answers ms.error "No Authorized" rather than
                # refusing the connection, so this is the only way to know.
                self._drain_for_auth_error(ws, 2.0)
            finally:
                ws.close()
        return f"sent {sent} key(s)"

    def volume(self, level: int) -> str:
        level = max(0, min(100, level))
        body = (
            '<u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            "<InstanceID>0</InstanceID><Channel>Master</Channel>"
            f"<DesiredVolume>{level}</DesiredVolume></u:SetVolume>"
        )
        return f"volume {level}" if upnp_soap(self.ip, "SetVolume", body) else "volume failed (TV asleep?)"

    def mute(self, on: bool) -> str:
        body = (
            '<u:SetMute xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            "<InstanceID>0</InstanceID><Channel>Master</Channel>"
            f"<DesiredMute>{1 if on else 0}</DesiredMute></u:SetMute>"
        )
        word = "mute on" if on else "mute off"
        return word if upnp_soap(self.ip, "SetMute", body) else f"{word} failed (TV asleep?)"

    def app(self, app_id: str) -> str:
        with self.lock:
            self.connect().rest_app_run(app_id)
        return f"launched {app_id}"

    # Candidate app IDs for the Internet browser. Which one exists depends on
    # the firmware year: org.tizen.browser on older sets, a numeric id on newer.
    BROWSER_IDS = ("org.tizen.browser", "3202010022079", "3201907018784")

    def browser_app_id(self) -> str | None:
        """First browser app id this TV actually reports, or None if it has no
        browser. Cached; `visible: false` still counts as installed."""
        if self._browser_id is not _UNSET:
            return self._browser_id
        found = None
        for app_id in (self.photos_cfg.get("browser_app_id"),) + self.BROWSER_IDS:
            if not app_id:
                continue
            try:
                url = f"http://{self.ip}:8001/api/v2/applications/{app_id}"
                with urllib.request.urlopen(url, timeout=4) as r:
                    if json.load(r).get("id"):
                        found = app_id
                        break
            except Exception:
                continue
        # Only a positive result is cached. Some sets (the Frame) answer 401 on
        # this endpoint until they are paired, and caching that "no browser"
        # would stick for the life of the service even after pairing succeeds.
        if found:
            self._browser_id = found
        log.info("%s: browser app id = %s", self.alias, found or "none found (unpaired?)")
        return found

    def open_url(self, url: str) -> str:
        """Launch the browser at a URL over the WebSocket (appId + NATIVE_LAUNCH
        + metaTag). Some 2026 firmware accepts this and then ignores the URL, so
        callers should confirm with fetched_since() rather than trust it."""
        app_id = self.browser_app_id() or "org.tizen.browser"
        with self.lock:
            ws = self.control_ws()
            try:
                ws.send(json.dumps({"method": "ms.channel.emit", "params": {
                    "event": "ed.apps.launch", "to": "host",
                    "data": {"appId": app_id, "action_type": "NATIVE_LAUNCH",
                             "metaTag": url}}}))
            finally:
                ws.close()
        return f"browser({app_id}) -> {url}"

    def launch_browser(self) -> str:
        """Start the browser so it lands on its configured homepage.

        Closed first on purpose: launching an already-running browser is a no-op
        and leaves whatever page it was on, so without this a second /photos
        would never return to the homepage.
        """
        # Detection is only a hint. Some firmware (the 75in Frame) 404s the
        # applications endpoint for EVERY app - including Netflix - yet still
        # accepts a launch, so when detection finds nothing, just try the
        # candidates and let the outcome decide.
        detected = self.browser_app_id()
        candidates = [detected] if detected else list(self.BROWSER_IDS)
        last = "no candidates"
        for app_id in candidates:
            try:
                with self.lock:
                    tv = self.connect()
                    try:
                        tv.rest_app_close(app_id)
                        time.sleep(2.5)  # let Tizen tear it down before relaunching
                    except Exception as exc:
                        log.debug("%s: browser close failed (may not be running): %s",
                                  self.alias, exc)
                    result = tv.rest_app_run(app_id)
                if isinstance(result, dict) and str(result.get("status")) == "404":
                    last = f"{app_id} -> 404"
                    continue
                self._browser_id = app_id  # remember whichever id actually took
                return f"launched Internet ({app_id}) on its homepage"
            except Exception as exc:
                last = f"{app_id} -> {type(exc).__name__}: {exc}"
        return f"ERROR could not launch a browser on this TV ({last})"

    def macro(self, name: str, cfg_macros: dict) -> str:
        keys = self.macros.get(name) or cfg_macros.get(name)
        if not keys:
            known = sorted(set(self.macros) | set(cfg_macros))
            return f"ERROR no macro '{name}' (known: {', '.join(known) or 'none'})"
        return self.send_keys(list(keys))

    # -- photo playlists -------------------------------------------------- #

    def base_url(self, cfg: Config) -> str:
        """Where this TV should reach us. Per-TV override, then the global
        base_url, then auto-detect. Whatever a TV's homepage is set to has to
        keep resolving, so a fixed base_url is what you want in production."""
        return (self.photos_cfg.get("base_url")
                or cfg.get("base_url")
                or f"http://{local_ip_toward(self.ip)}:{cfg['http_port']}")

    def live_url(self, cfg: Config) -> str:
        """The stable address to set as this TV's browser homepage.

        Defaults to the one shared URL so every TV can have the same homepage and
        switch together. Set shared_homepage false for per-TV addresses.
        """
        if cfg.get("shared_homepage", True):
            return f"{self.base_url(cfg)}/slideshow/live/all"
        return f"{self.base_url(cfg)}/slideshow/live/{urllib.parse.quote(self.alias)}"

    def photos(self, playlist: str, cfg: Config) -> str:
        method = self.photos_cfg.get("method", "browser")
        if playlist in ("off", "stop"):
            return self.photos_off(method)
        if method == "browser":
            base = self.base_url(cfg)
            secs = int(self.photos_cfg.get("interval_seconds", 10))
            fit = self.photos_cfg.get("fit", "contain")
            # The same stable URL the homepage is set to, so the launch attempt
            # and the homepage fallback agree. Shared by default so every TV can
            # carry one identical homepage.
            stable = self.live_url(cfg)
            url = f"{stable}?s={secs}&fit={fit}"
            if self.power_state() != "on":
                self.on()
                time.sleep(int(self.photos_cfg.get("wake_delay_seconds", 8)))
            return self.photos_browser(playlist, url)
        if method == "art":
            return self.photos_art(playlist)
        if method == "usb":
            if self.power_state() != "on":
                self.on()
                time.sleep(int(self.photos_cfg.get("wake_delay_seconds", 8)))
            keys = self.photos_cfg.get("usb_macro") or self.macros.get("usb-photos")
            if not keys:
                return ("ERROR photos.method is 'usb' but no usb_macro is set - "
                        "record one with: py tvbridge.py learn " + self.alias)
            return self.send_keys(list(keys))
        return f"ERROR unknown photos.method '{method}' (browser|art|usb)"

    def nudge_fullscreen(self) -> None:
        """Send one real remote keypress so the page can go fullscreen.

        The Fullscreen API only fires from a genuine user gesture - a click
        synthesised in JavaScript is rejected - but a real remote key arrives at
        the page as a keydown, which its handler uses to request fullscreen.
        Set photos.fullscreen_key to "" to disable.
        """
        key = self.photos_cfg.get("fullscreen_key", "KEY_ENTER")
        if not key:
            return
        try:
            progress(self.alias, "waiting 2s, then fullscreen key", 2.0)
            time.sleep(2.0)  # let the page load and bind its listeners
            progress(self.alias, f"sending {key} for fullscreen")
            self.send_keys([key])
            log.debug("%s: sent %s to trigger fullscreen", self.alias, key)
        except Exception as exc:
            log.debug("%s: fullscreen nudge failed: %s", self.alias, exc)

    def photos_browser(self, playlist: str, url: str) -> str:
        """Get the slideshow on screen, whichever way this firmware allows.

        A launch is acknowledged even when the TV then drops it, so success is
        defined as the TV actually requesting the page from us - not as the
        command being accepted.
        """
        def landed(since: float, wait: float = 6.0, what: str = "waiting for the TV to load the page") -> bool:
            progress(self.alias, what, wait)
            for _ in range(int(wait / 0.5)):
                time.sleep(0.5)
                if fetched_since(self.ip, since):
                    return True
            return False

        # On a Frame, Art Mode paints over the browser while leaving it running
        # underneath, so simply leaving art mode brings the slideshow back - no
        # relaunch, and it works even when the browser cannot be launched by API.
        if self.is_frame() and self.art_mode_state() == "on":
            started = time.monotonic()
            self.art_set_and_confirm(False, seconds=6)
            self.control_ready(20)
            if landed(started, 12.0):
                self.nudge_fullscreen()
                return f"playing {playlist} (left art mode)"

        # 0) If the page is already up, the playlist pointer has changed and the
        #    page will follow on its next poll. Nothing to launch.
        #    Checked BEFORE requiring a launchable browser: a TV can be happily
        #    showing the slideshow (homepage set by hand) on firmware that will
        #    not report or launch its browser over the API.
        #    The short wait matters: the page throttles its timers while it is
        #    covered (art mode, or another app in front), so a fetch can be a few
        #    seconds away rather than already recorded.
        if (fetched_since(self.ip, time.monotonic() - 30)
                or landed(time.monotonic(), 8.0, "checking if the page is already up")):
            return f"switched to {playlist} (slideshow already on screen)"

        open_macro = (self.photos_cfg.get("open_macro")
                      or self.macros.get("open-browser")
                      or self.cfg["macros"].get("open-browser"))

        # Frames do not expose their browser in the app registry, so an app-launch
        # request makes the TV put a "command not available" box on screen and
        # achieves nothing. With a recorded key macro available, skip both API
        # launch attempts and drive the remote instead. Override per TV with
        # photos.open_with = "api" or "macro".
        mode = self.photos_cfg.get("open_with", "auto")
        keys_only = (mode == "macro"
                     or (mode == "auto" and self.is_frame() and bool(open_macro)))
        if keys_only:
            started = time.monotonic()
            macro_secs = sum(int(t[1:]) / 1000.0 for t in open_macro
                             if str(t).startswith("@")) + 0.35 * len(open_macro)
            if not self.control_ready():
                return ("WARNING the TV never opened its control port, so no keys "
                        "could be sent - it may be fully asleep")
            sent_ok = False
            for attempt in (1, 2):
                progress(self.alias, "opening the browser with remote keys", macro_secs)
                try:
                    self.send_keys(list(open_macro))
                    sent_ok = True
                    break
                except Exception as exc:
                    log.warning("%s: open_macro attempt %d failed: %s",
                                self.alias, attempt, exc)
                    self.control_ready(12)   # port may have dropped again
            if not sent_ok:
                return ("WARNING could not send the key macro - the TV refused the "
                        "control connection twice")
            if landed(started, float(self.photos_cfg.get("launch_wait_seconds", 30)),
                      "waiting for the page after the key macro"):
                self.nudge_fullscreen()
                return f"playing {playlist} (opened the browser by remote keys)"
            return ("WARNING ran the key macro but the TV never requested the page - "
                    f"the sequence may need re-recording: learn.bat {self.alias}")

        # 1) Ask the browser to open the URL directly. Works where metaTag is honoured.
        started = time.monotonic()
        progress(self.alias, "asking the browser to open the URL")
        try:
            self.open_url(url)
        except Exception as exc:
            log.warning("%s: URL launch failed: %s", self.alias, exc)
        if landed(started, 6.0, "waiting after URL launch"):
            self.nudge_fullscreen()
            return f"playing {playlist}"

        # 2) Firmware ignored the URL. Launching with no URL lands on the
        #    browser's homepage, so a homepage set to this slideshow still works.
        started = time.monotonic()
        progress(self.alias, "relaunching the browser on its homepage")
        launched = ""
        try:
            launched = self.launch_browser()
        except Exception as exc:
            log.warning("%s: browser launch failed: %s", self.alias, exc)
            launched = f"ERROR {exc}"
        # Don't burn the wait when the launch was outright rejected (no browser
        # app id exists on this firmware) - fall through to the key macro.
        wait = float(self.photos_cfg.get("launch_wait_seconds", 30))
        # A cold browser start plus loading 4K photos takes a while - measured
        # over 10 s on a Wi-Fi Frame, which used to be reported as a failure.
        if not launched.startswith("ERROR") and landed(started, wait,
                "waiting for the browser to load the slideshow"):
            self.nudge_fullscreen()
            return f"playing {playlist} (via the browser homepage)"

        # 3) Last resort: drive the remote. Some firmware (2026 Frames) has no
        #    working network launch at all - REST applications does not list the
        #    browser, ed.apps.launch is a no-op, and DIAL only echoes - but remote
        #    keys work fine, so a recorded sequence can open it.
        #    Record one per model with: py tvbridge.py learn <alias>
        if open_macro:
            started = time.monotonic()
            macro_secs = sum(int(t[1:]) / 1000.0 for t in open_macro
                             if str(t).startswith("@")) + 0.35 * len(open_macro)
            progress(self.alias, "opening the browser with remote keys", macro_secs)
            try:
                self.send_keys(list(open_macro))
            except Exception as exc:
                log.warning("%s: open_macro failed: %s", self.alias, exc)
            if landed(started, float(self.photos_cfg.get("launch_wait_seconds", 30)),
                      "waiting for the page after the key macro"):
                self.nudge_fullscreen()
                return f"playing {playlist} (opened the browser by remote keys)"
            return ("WARNING ran the open_macro but the TV never requested the page - "
                    "the key sequence probably needs recalibrating: "
                    f"py tvbridge.py learn {self.alias}")

        return ("WARNING this TV's firmware launches the browser but ignores the "
                f"URL. One-time fix, once per TV, with the remote: open Internet, "
                f"go to {url} and set it as the homepage. That URL never changes - "
                "/photos/<playlist> switches what it shows - so this is a one-off. "
                "Alternatively use photos.method 'usb'.")

    def photos_art(self, playlist: str) -> str:
        """Frame TVs: upload the playlist folder once, then start Art Mode slideshow."""
        secs = int(self.photos_cfg.get("interval_seconds", 600))
        minutes = max(1, round(secs / 60))
        with self.lock:
            art = self.connect(timeout=30).art()
            if not art.supported():
                return "ERROR this TV does not support Art Mode - use photos.method 'browser'"
            last = "installed samsungtvws has no slideshow API"
            # 2024+ Frames use slideshow_status, ~2020-21 only auto_rotation_status.
            for setter in ("set_slideshow_status", "set_auto_rotation_status"):
                fn = getattr(art, setter, None)
                if fn is None:
                    continue
                try:
                    fn(duration=minutes, type=True, category=2)  # category 2 = My Photos
                    art.set_artmode(True)
                    return f"art-mode slideshow every {minutes} min ({setter})"
                except Exception as exc:  # older firmware only has one of the two
                    last = exc
            return f"ERROR art slideshow failed: {last}"

    def photos_off(self, method: str) -> str:
        if method == "art":
            with self.lock:
                self.connect(timeout=30).art().set_artmode(False)
            return "art mode off"
        keys = self.photos_cfg.get("exit_macro") or ["KEY_RETURN", "@600", "KEY_EXIT"]
        return self.send_keys(list(keys))


# --------------------------------------------------------------------------- #
# slideshow page served to the TV browser
# --------------------------------------------------------------------------- #

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# Bump whenever SLIDESHOW_HTML changes. The page polls this and reloads itself when
# it differs, so a page fix reaches every TV without anyone visiting a screen.
PAGE_VERSION = "5"

# ip -> monotonic time of that client's last slideshow request. Lets `photos`
# tell "the TV is really showing it" apart from "the TV ignored the command",
# which matters because a launch the TV drops is acknowledged as success.
_last_fetch: dict[str, float] = {}
_last_log: dict[str, float] = {}
_fetch_lock = threading.Lock()


def note_fetch(ip: str) -> None:
    """Record a slideshow request and log it at INFO, throttled per client.

    Logged at INFO on purpose: confirming "did that TV actually pick up the
    slideshow" is the main thing you need during a rollout, and the per-request
    HTTP log only exists at DEBUG.
    """
    now = time.monotonic()
    with _fetch_lock:
        # Throttle against the last LOG, not the last fetch. Comparing against
        # the last fetch meant a TV polling every 5 s never aged past 60 s, so it
        # logged once and then went silent - which made a healthy fleet look
        # completely dead in the log.
        due = now - _last_log.get(ip, -1e9) > 60
        _last_fetch[ip] = now
        if due:
            _last_log[ip] = now
    if due:
        log.info("slideshow being fetched by %s", ip)


def fetched_since(ip: str, since: float) -> bool:
    with _fetch_lock:
        return _last_fetch.get(ip, 0.0) >= since


def forget_fetch(ip: str) -> None:
    """Drop a TV's fetch record. Called on every power change: after a power
    cycle the browser may not be on screen any more, and a stale record would
    make `photos` skip the relaunch it actually needs."""
    with _fetch_lock:
        _last_fetch.pop(ip, None)

SLIDESHOW_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  html{background:#000}
  /* Deliberately taller than the viewport: the Tizen browser only auto-hides its
     address/menu bars once a page can actually scroll, and some Frames never hide
     them otherwise. The layers below are fixed, so scrolling moves no pixels. */
  body{margin:0;background:#000;cursor:none;min-height:130vh;overflow-x:hidden}
  /* Scrollable but with no visible scrollbar: the page must be able to scroll for
     the browser to auto-hide its bars, but the scrollbar itself would be on show
     over the photos. */
  html,body{scrollbar-width:none;-ms-overflow-style:none}
  html::-webkit-scrollbar,body::-webkit-scrollbar{width:0;height:0;display:none;
     background:transparent}
  .f{position:fixed;inset:0;background-repeat:no-repeat;background-position:center;
     background-size:__FIT__;opacity:0;transition:opacity 1.2s ease-in-out}
  .f.on{opacity:1}
  #msg{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
       color:#666;font:400 2vw/1.5 system-ui,sans-serif;text-align:center;padding:4vw}
  #ident{position:fixed;inset:0;display:none;align-items:center;justify-content:center;
         background:#0b1020;color:#fff;text-align:center;z-index:9}
  #ident.show{display:flex}
  #identnum{display:block;font:800 44vh/1 system-ui,sans-serif;letter-spacing:-.02em}
  #identname{display:block;margin-top:2vh;font:600 4vh/1.2 system-ui,sans-serif;
             color:#7fd1ff;font-style:normal}
</style></head><body>
<div class="f" id="a"></div><div class="f" id="b"></div>
<div id="ident"><div><span id="identnum">0</span><em id="identname"></em></div></div>
<div id="msg">Loading __TITLE__ ...</div>
<script>
// BASE is absolute: the page URL has no trailing slash, so relative paths
// would resolve one directory too high and lose the playlist name.
var BASE = '__BASE__', SECS = __SECS__, PLAYLIST = '__PLAYLIST__',
    PAGEVER = '__PAGEVER__', LIST = [], i = -1, front = 0,
    L = [document.getElementById('a'), document.getElementById('b')],
    msg = document.getElementById('msg');

function manifest(then){
  var x = new XMLHttpRequest();
  x.open('GET', BASE + 'manifest.json?t=' + Date.now(), true);
  x.onreadystatechange = function(){
    if (x.readyState !== 4) return;
    var next = [], name = PLAYLIST;
    try {
      var j = JSON.parse(x.responseText);
      next = j.images || []; name = j.playlist || PLAYLIST;
      // The server is serving a newer page than we are running - pick it up.
      if (j.page && j.page !== PAGEVER) { location.reload(true); return; }
      var idn = document.getElementById('ident');
      if (j.identify) {
        document.getElementById('identnum').textContent = j.identify.n;
        document.getElementById('identname').textContent = j.identify.alias;
        idn.className = 'show';
      } else {
        idn.className = '';
      }
    } catch(e) {}
    // A different playlist restarts from its first image; a changed folder
    // within the same playlist just updates the list mid-run.
    if (name !== PLAYLIST) {
      PLAYLIST = name; LIST = next; i = -1;
      if (!then) show();                 // a poll saw a switch: jump to it now
    } else if (next.join('|') !== LIST.join('|')) {
      LIST = next;
    }
    if (then) then();
  };
  x.send();
}

function show(){
  if (!LIST.length){
    msg.textContent = 'No images in this playlist folder yet.';
    L[0].className = L[1].className = 'f';
    return;
  }
  msg.style.display = 'none';
  i = (i + 1) % LIST.length;
  var back = L[1 - front], url = BASE + LIST[i];
  var pre = new Image();
  // Swap only once decoded, so a slow read never shows a half-painted frame.
  pre.onload = function(){
    back.style.backgroundImage = 'url("' + url + '")';
    back.className = 'f on';
    L[front].className = 'f';
    front = 1 - front;
  };
  pre.onerror = function(){ setTimeout(show, 200); };  // file vanished mid-run
  pre.src = url;
}

// Push the page down a little so the browser treats it as scrolled and hides its
// address/menu bars. Never scroll back to 0 - that makes them reappear.
function hideChrome(){ try { window.scrollTo(0, 90); } catch(e) {} }

// Real fullscreen if this build allows it. Usually gated behind a user gesture,
// so also attempt it on any remote keypress: send one with /<tv>/key/KEY_ENTER.
function goFullscreen(){
  var el = document.documentElement;
  var fn = el.requestFullscreen || el.webkitRequestFullscreen ||
           el.mozRequestFullScreen || el.msRequestFullscreen;
  if (fn && !(document.fullscreenElement || document.webkitFullscreenElement)) {
    try { fn.call(el); } catch(e) {}
  }
}
window.addEventListener('keydown', function(){ goFullscreen(); hideChrome(); });
window.addEventListener('click',   function(){ goFullscreen(); hideChrome(); });

manifest(function(){ show(); setInterval(show, SECS * 1000); });
// Short poll so a /photos/<playlist> switch lands quickly on a page that is
// already open - which is how switching works when the firmware won't take a URL.
setInterval(function(){ manifest(null); }, 5000);
// Also keeps timers alive on a browser that throttles idle pages.
setInterval(hideChrome, 15000);
goFullscreen(); hideChrome();
setTimeout(hideChrome, 1500);   // again once layout has settled
</script></body></html>
"""


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TV status</title>
<style>
  :root{color-scheme:dark light}
  body{margin:0;font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
       background:#14161a;color:#e8eaed}
  header{padding:18px 20px 10px;display:flex;flex-wrap:wrap;gap:16px;align-items:baseline}
  h1{margin:0;font-size:19px;font-weight:600}
  .sub{color:#9aa0a6;font-size:13px}
  .tiles{display:flex;flex-wrap:wrap;gap:10px;padding:0 20px 14px}
  .tile{background:#1e2126;border:1px solid #2c3037;border-radius:10px;
        padding:10px 14px;min-width:104px}
  .tile b{display:block;font-size:26px;font-weight:650;line-height:1.15}
  .tile span{color:#9aa0a6;font-size:12px}
  .wrap{overflow-x:auto;padding:0 20px 28px}
  table{border-collapse:collapse;width:100%;min-width:660px}
  th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.05em;
     color:#9aa0a6;font-weight:600;padding:8px 12px;border-bottom:1px solid #2c3037}
  td{padding:9px 12px;border-bottom:1px solid #23262c;white-space:nowrap}
  tr:hover td{background:#1a1d22}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
        font-weight:600}
  .playing  {background:#123524;color:#5ddc9a;border:1px solid #1c5138}
  .idle     {background:#3a2c10;color:#f0be5a;border:1px solid #5b451a}
  .off      {background:#20242b;color:#98a2b3;border:1px solid #333844}
  .busy     {background:#132a3f;color:#6ab7f5;border:1px solid #1d4468}
  .offline  {background:#3a1720;color:#f2879b;border:1px solid #5c2130}
  .muted{color:#8b9099}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;
       background:#5ddc9a;margin-right:7px;vertical-align:middle}
  .stale{background:#f0be5a}
  code{background:#1e2126;padding:1px 6px;border-radius:5px;font-size:12px}
  .bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:0 20px 12px}
  .btn{background:#242830;color:#e8eaed;border:1px solid #363b45;border-radius:8px;
       padding:8px 15px;font:inherit;font-weight:600;cursor:pointer}
  .btn:hover{background:#2c313a}
  .btn.go{background:#14512f;border-color:#1e6b41;color:#8ef0b6}
  .btn.go:hover{background:#1a6039}
  .btn.stop{background:#4a1f27;border-color:#6b2b36;color:#f7a8b8}
  .btn.stop:hover{background:#5a262f}
  .fix{padding:4px 11px;font-size:12px;border-radius:7px}
  .note{color:#9aa0a6;font-size:12px}
  .left{color:#6ab7f5;font-size:12px;font-variant-numeric:tabular-nums}
  .num{font-weight:700;color:#7fd1ff;font-variant-numeric:tabular-nums}
  .rc{position:fixed;inset:0;background:rgba(0,0,0,.62);display:none;
      align-items:center;justify-content:center;z-index:50;padding:16px}
  .rc.show{display:flex}
  .rcbox{background:#1a1d22;border:1px solid #333844;border-radius:16px;
         padding:16px;min-width:270px;max-width:340px;width:100%}
  .rchead{display:flex;justify-content:space-between;align-items:center;
          gap:12px;margin-bottom:14px}
  .rchead b{font-size:16px}
  .pad{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
  .pad button,.rcrow button{background:#262b34;color:#e8eaed;border:1px solid #3a404b;
       border-radius:11px;padding:18px 0;font:inherit;font-weight:650;font-size:16px;
       cursor:pointer;touch-action:manipulation;-webkit-tap-highlight-color:transparent;
       -webkit-user-select:none;user-select:none}
  .btn{-webkit-tap-highlight-color:transparent;touch-action:manipulation}
  .rcbox{max-height:92vh;overflow-y:auto}
  .pad button:active,.rcrow button:active{background:#3a4150}
  .pad button.ok{background:#14512f;border-color:#1e6b41;color:#8ef0b6}
  .pad .sp{visibility:hidden}
  .rcrow{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:12px}
  @media (prefers-color-scheme: light){
    .rcbox{background:#fff;border-color:#d8dbe0}
    .pad button,.rcrow button{background:#f1f3f5;border-color:#d8dbe0;color:#1f2328}
  }
  .pl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:0 20px 14px}
  .pl b{font-size:12px;color:#9aa0a6;text-transform:uppercase;letter-spacing:.05em;
        margin-right:2px}
  .chip{background:#242830;color:#e8eaed;border:1px solid #363b45;border-radius:999px;
        padding:6px 14px;font:inherit;font-size:13px;cursor:pointer}
  .chip:hover{background:#2c313a}
  .chip.on{background:#123524;border-color:#1c5138;color:#5ddc9a;font-weight:650}
  .chip small{opacity:.65;margin-left:6px}
  .api{margin:0 20px 18px;background:#1a1d22;border:1px solid #2c3037;border-radius:10px}
  .api summary{cursor:pointer;padding:10px 14px;font-size:13px;color:#9aa0a6;
               font-weight:600}
  .api div{padding:0 14px 12px}
  .api p{margin:8px 0;font-size:12px;color:#9aa0a6}
  .api code{display:block;padding:7px 10px;margin-top:3px;color:#cfd4da;
            word-break:break-all;white-space:normal;font-size:12px}
  @media (prefers-color-scheme: light){
    .chip{background:#fff;border-color:#d8dbe0;color:#1f2328}
    .api{background:#fff;border-color:#d8dbe0}
  }
  @media (prefers-color-scheme: light){
    body{background:#f6f7f9;color:#1f2328}
    .tile{background:#fff;border-color:#d8dbe0}
    th{color:#57606a;border-bottom-color:#d8dbe0}
    td{border-bottom-color:#e8eaed}
    tr:hover td{background:#f0f2f5}
    code{background:#eaecef}
  }
</style></head><body>
<header>
  <h1>TV status</h1>
  <span class="sub"><span class="dot" id="dot"></span><span id="meta">loading…</span></span>
</header>
<div class="bar">
  <button type="button" class="btn go"   data-cmd="home/on">All on</button>
  <button type="button" class="btn stop" data-cmd="home/off">All off</button>
  <button type="button" class="btn"      data-cmd="frames/on">Frames on</button>
  <button type="button" class="btn"      data-cmd="minis/on">Minis on</button>
  <button type="button" class="btn"      data-cmd="identify/on">Identify screens</button>
  <button type="button" class="btn"      data-cmd="identify/off">Identify off</button>
  <button type="button" class="btn"      data-cmd="fullscreen">Fullscreen all</button>
  <span class="note" id="note"></span>
</div>
<div class="pl" id="pl"></div>
<div class="tiles" id="tiles"></div>
<div id="rc" class="rc"><div class="rcbox">
  <div class="rchead"><b id="rcname">TV</b><button type="button" class="btn" id="rcclose">Close</button></div>
  <div class="pad" id="rcpad"></div>
  <div class="rcrow" id="rcextra"></div>
</div></div>
<details class="api" id="mg"><summary>Manage TVs (add / remove / rename / pair)</summary>
  <div>
    <p><button type="button" class="btn" id="scanbtn">Scan the network for TVs</button>
       <span class="note" id="scanmsg"></span></p>
    <div id="scanout"></div>
  </div>
</details>
<details class="api"><summary>API commands</summary><div id="apis"></div></details>
<div class="wrap">
<table><thead><tr>
  <th>#</th><th>TV</th><th>Status</th><th></th><th>Playlist</th><th>Last seen</th>
  <th>Power</th><th>Browser</th><th>IP</th>
</tr></thead><tbody id="rows"></tbody></table>
</div>
<script>
function ago(s){
  if (s === null || s === undefined) return 'never';
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s/60) + 'm';
  if (s < 86400) return Math.round(s/3600) + 'h';
  return Math.round(s/86400) + 'd';
}
function draw(d){
  var counts = {playing:0, idle:0, off:0, offline:0};
  var rows = '';
  d.tvs.forEach(function(t){
    counts[t.cls] = (counts[t.cls]||0) + 1;
    rows += '<tr>'
      + '<td class="num">' + (t.number || '') + '</td>'
      + '<td><b>' + t.alias + '</b></td>'
      + '<td><span class="pill ' + t.cls + '">' + t.status + '</span>'
      + (t.doing_left !== null && t.doing_left !== undefined
           ? ' <span class="left" data-left="' + t.doing_left + '">'
             + Math.ceil(t.doing_left) + 's</span>' : '') + '</td>'
      + '<td><button type="button" class="btn fix" data-cmd="' + t.alias + '/on">Fix me</button>'
      + ' <button type="button" class="btn fix" data-remote="' + t.alias + '">Remote</button>'
      + ' <button type="button" class="btn fix" data-manage="' + t.alias + '">Manage</button></td>'
      + '<td>' + (t.playlist || '<span class="muted">-</span>') + '</td>'
      + '<td class="muted">' + ago(t.last_seen) + '</td>'
      + '<td class="muted">' + t.power + '</td>'
      + '<td class="muted">' + t.browser + '</td>'
      + '<td class="muted">' + t.ip + '</td>'
      + '</tr>';
  });
  document.getElementById('rows').innerHTML = rows;
  drawPlaylists(d); drawApis(d);
  if (RCTV) document.getElementById('rc').className = 'rc show';
  document.getElementById('tiles').innerHTML =
      tile(counts.playing||0, 'playing')
    + tile(counts.idle||0,    'on, not playing')
    + tile(counts.off||0,     'off / art mode')
    + tile(counts.offline||0, 'offline');
  var age = d.age_seconds;
  document.getElementById('meta').textContent =
      d.tvs.length + ' TVs · checked ' + ago(age) + ' ago · refreshes every '
      + d.refresh_seconds + 's';
  document.getElementById('dot').className = 'dot' + (age > d.refresh_seconds*3 ? ' stale' : '');
  if (d.last_command) document.getElementById('note').textContent = d.last_command;
}
// One delegated handler for every [data-cmd] control. Inline onclick needed
// nested quotes inside an HTML attribute inside a JS string, which is exactly
// where the escaping broke and took the whole script down.
var LASTTAP = 0;
function handleTap(e){
  // touchstart fires first, then click - ignore the click that follows a tap
  var now = Date.now();
  if (e.type === 'touchstart') { LASTTAP = now; }
  else if (now - LASTTAP < 700) { return; }

  var t = e.target;
  if (t && t.id === 'scanbtn') { scan(); return; }
  if (t && t.id === 'rcclose') { closeRemote(); return; }
  if (t && t.id === 'rc') { closeRemote(); return; }
  var el = t;
  while (el && el.getAttribute) {
    var key = el.getAttribute('data-key');
    if (key) {
      if (e.cancelable) e.preventDefault();   // stop the 300ms tap delay
      flash(el);
      if (RCTV) run(RCTV + '/key/' + key);
      return;
    }
    var who = el.getAttribute('data-remote');
    if (who) { openRemote(who); return; }
    var mg = el.getAttribute('data-manage');
    if (mg) { manage(mg); return; }
    var ad = el.getAttribute('data-add');
    if (ad) { addTv(ad); return; }
    var cmd = el.getAttribute('data-cmd');
    if (cmd) { flash(el); run(cmd); return; }
    el = el.parentNode;
  }
}
function flash(el){
  try {
    el.style.background = '#4a8cff';
    setTimeout(function(){ el.style.background = ''; }, 130);
  } catch(e) {}
}
document.addEventListener('touchstart', handleTap, {passive: false});
document.addEventListener('click', handleTap);
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') closeRemote();
});
function scan(){
  var m = document.getElementById('scanmsg');
  m.textContent = 'scanning the whole subnet, ~20s ...';
  var x = new XMLHttpRequest();
  x.open('GET', 'admin/scan', true);
  x.onreadystatechange = function(){
    if (x.readyState !== 4) return;
    var d;
    try { d = JSON.parse(x.responseText); } catch(e) {
      m.textContent = 'scan failed: ' + x.responseText.slice(0, 90); return; }
    m.textContent = d.found.length + ' TV(s) on the network, ' + d.new + ' not configured';
    var h = '<table><thead><tr><th>IP</th><th>Model</th><th>Name</th><th>Power</th>'
          + '<th>In config</th><th></th></tr></thead><tbody>';
    d.found.forEach(function(r){
      h += '<tr><td>' + r.ip + '</td><td class="muted">' + r.model + '</td>'
        + '<td class="muted">' + r.name + '</td><td class="muted">' + r.power + '</td>'
        + '<td>' + (r.alias ? '<b>' + r.alias + '</b>'
                            : '<span class="num">NEW</span>') + '</td>'
        + '<td>' + (r.alias ? ''
            : '<button type="button" class="btn fix" data-add="' + r.ip + '|' + r.mac + '">Add</button>')
        + '</td></tr>';
    });
    document.getElementById('scanout').innerHTML = h + '</tbody></table>';
  };
  x.send();
}
function manage(alias){
  var what = prompt('Manage ' + alias + '  --  1 rename, 2 change IP, '
                    + '3 pair now, 4 remove.  Enter 1-4:');
  if (!what) return;
  what = what.trim();
  if (what === '1') {
    var nn = prompt('New name for ' + alias + ' (letters, digits, dashes):', alias);
    if (nn) run('admin/rename/' + alias + '/' + encodeURIComponent(nn.trim()));
  } else if (what === '2') {
    var ip = prompt('New IP address for ' + alias + ':');
    if (!ip) return;
    var mac = prompt('New MAC for ' + alias + ' (leave blank to keep):', '');
    run('admin/setip/' + alias + '/' + encodeURIComponent(ip.trim())
        + (mac && mac.trim() ? '/' + encodeURIComponent(mac.trim()) : ''));
  } else if (what === '3') {
    run('admin/pair/' + alias);
    alert('Go to ' + alias + ' and press ALLOW on that screen within 90 seconds.');
  } else if (what === '4') {
    if (confirm('Remove ' + alias + ' from the system?')) {
      run('admin/remove/' + alias);
    }
  }
}
function addTv(payload){
  var bits = payload.split('|');
  var ip = bits[0], mac = bits[1] || '';
  var alias = prompt('Room name for the TV at ' + ip + ' (e.g. sitting-area):');
  if (!alias) return;
  run('admin/add/' + encodeURIComponent(alias.trim()) + '/' + ip
      + (mac ? '/' + mac : ''));
  alert('Added. Now use Manage then 3 to pair it, and press ALLOW on that screen.');
}
var RCTV = null;
var PAD = [
  ['',        'UP',    ''       ],
  ['LEFT',    'ENTER', 'RIGHT'  ],
  ['',        'DOWN',  ''       ]
];
var EXTRA = [['HOME','Home'],['RETURN','Back'],['EXIT','Exit'],['MENU','Menu'],
             ['VOLUP','Vol +'],['VOLDOWN','Vol -'],['MUTE','Mute'],['POWER','Power']];
function openRemote(alias){
  RCTV = alias;
  document.getElementById('rcname').textContent = alias;
  var pad = '';
  for (var r = 0; r < PAD.length; r++) {
    for (var c = 0; c < 3; c++) {
      var k = PAD[r][c];
      if (!k) { pad += '<button type="button" class="sp"></button>'; continue; }
      var label = k === 'ENTER' ? 'OK'
                : k === 'UP' ? '\u25B2' : k === 'DOWN' ? '\u25BC'
                : k === 'LEFT' ? '\u25C0' : '\u25B6';
      pad += '<button type="button" class="' + (k === 'ENTER' ? 'ok' : '')
           + '" data-key="KEY_' + k + '">' + label + '</button>';
    }
  }
  document.getElementById('rcpad').innerHTML = pad;
  var ex = '';
  EXTRA.forEach(function(e){
    ex += '<button type="button" data-key="KEY_' + e[0] + '">' + e[1] + '</button>';
  });
  document.getElementById('rcextra').innerHTML = ex;
  document.getElementById('rc').className = 'rc show';
}
function closeRemote(){ RCTV = null; document.getElementById('rc').className = 'rc'; }
function run(cmd){
  var n = document.getElementById('note');
  n.textContent = 'sent ' + cmd + ' ...';
  var x = new XMLHttpRequest();
  x.open('GET', 'x/' + cmd, true);
  x.onreadystatechange = function(){ if (x.readyState === 4) setTimeout(poll, 400); };
  x.send();
}
function drawPlaylists(d){
  var h = '<b>Playlist</b>';
  (d.playlists || []).forEach(function(p){
    h += '<button type="button" class="chip' + (p.name === d.playing ? ' on' : '') + '"'
       + ' data-cmd="playlist/' + encodeURIComponent(p.name) + '">'
       + p.name + '<small>' + p.count + '</small></button>';
  });
  document.getElementById('pl').innerHTML = h;
}
function drawApis(d){
  var base = d.base || (location.protocol + '//' + location.host);
  var rows = [
    ['All on',            base + '/home/on'],
    ['All off',           base + '/home/off'],
    ['Frames on',         base + '/frames/on'],
    ['Minis on',          base + '/minis/on'],
    ['One TV on',         base + '/mini-led/on'],
    ['One TV off',        base + '/mini-led/off'],
    ['Change playlist',   base + '/playlist/' + (d.playing || 'dream-home')],
    ['Identify screens',  base + '/identify/on'],
    ['Identify off',      base + '/identify/off'],
    ['Fullscreen all',    base + '/fullscreen'],
    ['Scan for TVs',      base + '/admin/scan'],
    ['Pair one TV',       base + '/admin/pair/<tv>'],
    ['Remove a TV',       base + '/admin/remove/<tv>'],
    ['Status (JSON)',     base + '/api/status'],
    ['Homepage for TVs',  base + '/slideshow/live/all']
  ];
  var h = '<p>Plain GET - usable from Loxone, curl, a browser, anything. '
        + 'Prefix the path with /x/ to fire and return instantly '
        + 'instead of waiting for it to finish.</p>';
  rows.forEach(function(r){
    h += '<p>' + r[0] + '<code>' + r[1] + '</code></p>';
  });
  document.getElementById('apis').innerHTML = h;
}
function tile(n, label){
  return '<div class="tile"><b>' + n + '</b><span>' + label + '</span></div>';
}
function poll(){
  var x = new XMLHttpRequest();
  x.open('GET', 'api/status?t=' + Date.now(), true);
  x.onreadystatechange = function(){
    if (x.readyState === 4 && x.status === 200) {
      try { draw(JSON.parse(x.responseText)); } catch(e) {}
    }
  };
  x.send();
}
poll();
setInterval(poll, 2000);
setInterval(function(){            // tick the countdowns between polls
  var els = document.querySelectorAll('.left');
  for (var i = 0; i < els.length; i++) {
    var v = parseFloat(els[i].getAttribute('data-left')) - 1;
    if (v < 0) v = 0;
    els[i].setAttribute('data-left', v);
    els[i].textContent = Math.ceil(v) + 's';
  }
}, 1000);
</script></body></html>
"""


def playlist_dir(cfg: Config, playlist: str) -> Path | None:
    """Resolve a playlist name to a folder, refusing anything outside photo_root."""
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", playlist) or playlist in (".", ".."):
        return None
    root = cfg.photo_root.resolve()
    target = (root / playlist).resolve()
    if target != root and root not in target.parents:
        return None
    return target


def playlist_images(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    names = sorted(
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    return [f"img/{urllib.parse.quote(n)}" for n in names]


# --------------------------------------------------------------------------- #
# command dispatch
# --------------------------------------------------------------------------- #

class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tvs: dict[str, Tv] = {}
        self.current: dict[str, str] = {}  # alias -> playlist its live URL serves
        self.shared: str = ""              # playlist that /slideshow/live/all serves
        self._status: list[dict] = []
        self._status_at: float = 0.0
        self._last_command: str = ""
        self._playlists: list[dict] = []
        self._heal_lock = threading.Lock()
        self.identify: bool = False
        self._status_lock = threading.Lock()
        self.reload()
        self.load_state()

    # Which playlist is showing has to outlive the process: after a service
    # restart or a power cut the TVs must come back to what was chosen, not to
    # whatever the default happens to be.
    @property
    def state_file(self) -> Path:
        return STATE_DIR / "state.json"

    def load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        shared = data.get("shared") or ""
        if shared and playlist_dir(self.cfg, shared):
            self.shared = shared
        for alias, name in (data.get("current") or {}).items():
            if alias in self.tvs and playlist_dir(self.cfg, name):
                self.current[alias] = name
        if self.shared or self.current:
            log.info("restored playlist state: shared=%s", self.shared or "(default)")

    def save_state(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"shared": self.shared, "current": self.current}),
                encoding="utf-8")
        except OSError as exc:
            log.warning("could not save playlist state: %s", exc)

    def current_playlist(self, alias: str) -> str:
        """This TV's playlist: its own pointer, else the fleet-wide one, else the
        configured default.

        Falling back to `shared` before the default matters: a TV added after a
        playlist was chosen must inherit the fleet selection, otherwise a bare
        /photos on it resolves to "default" and then clobbers `shared` for
        everyone.
        """
        return (self.current.get(alias) or self.shared
                or self.cfg.get("default_playlist", "default"))

    def shared_playlist(self) -> str:
        """What the one shared URL serves. Set by any photos command, so TVs whose
        homepage is the shared URL all change together."""
        return self.shared or self.cfg.get("default_playlist", "default")

    def reload(self) -> str:
        """Re-read config.json. A broken file leaves the running config in place."""
        try:
            self.cfg.load()
        except (json.JSONDecodeError, OSError) as exc:
            if not self.tvs:
                raise  # nothing to fall back to at startup - fail loudly
            log.error("config reload failed, keeping previous config: %s", exc)
            return f"ERROR config.json not loaded ({exc}) - previous config still active"
        self.tvs = {a: Tv(a, s, self.cfg) for a, s in self.cfg["tvs"].items()}
        clash = sorted(set(self.cfg["groups"]) & set(self.tvs))
        if clash:
            log.warning("group name(s) shadowed by a TV alias, TV wins: %s",
                        " ".join(clash))
        return f"reloaded: {len(self.tvs)} TVs, {len(self.cfg['groups'])} groups"

    # -- status dashboard --------------------------------------------------- #

    STATUS_REFRESH = 20  # seconds between background sweeps

    def probe_one(self, alias: str) -> dict:
        """Cheap, HTTP-only probe of one TV - no WebSocket, so a sweep of the
        whole fleet stays fast. Power from REST, browser state from DIAL."""
        tv = self.tvs[alias]
        power, model, browser = "unreachable", "", "-"
        try:
            with urllib.request.urlopen(f"http://{tv.ip}:8001/api/v2/", timeout=4) as r:
                d = json.load(r).get("device", {})
            power = d.get("PowerState") or "standby"
            model = d.get("modelName", "")
        except Exception:
            pass
        if power != "unreachable":
            try:
                with urllib.request.urlopen(
                        f"http://{tv.ip}:8080/ws/app/WebBrowser", timeout=4) as r:
                    body = r.read().decode("utf-8", "replace")
                m = re.search(r"<state>([^<]*)</state>", body)
                browser = m.group(1) if m else "-"
            except Exception:
                browser = "-"

        with _fetch_lock:
            seen = _last_fetch.get(tv.ip)
        age = (time.monotonic() - seen) if seen else None

        # A TV counts as playing only if it recently asked us for the page.
        # "Browser running" is not enough: Tizen keeps a backgrounded browser
        # loaded but freezes its timers, so it stops polling while still
        # reporting running.
        if power == "unreachable":
            status, cls = "offline", "offline"
        elif power != "on":
            status, cls = "off", "off"
        elif browser == "stopped":
            # Decisive, and checked BEFORE the heartbeat: last_seen freezes at the
            # moment the browser closed, so a Frame correctly sitting in art mode
            # kept reading as "playing" for another 90 s.
            status, cls = ("art mode / off" if self.tvs[alias].is_frame()
                           else "on, browser closed"), "off"
        elif age is not None and age < 90:
            status, cls = "playing", "playing"
        else:
            status, cls = "on, not playing", "idle"

        doing, left = progress_of(alias)
        if doing:
            status, cls = doing, "busy"
        number = self.identify_numbers().get(tv.ip, (None, None))[0]
        return {"alias": alias, "ip": tv.ip, "power": power, "model": model,
                "number": number,
                "browser": browser, "last_seen": age, "status": status, "cls": cls,
                "doing": doing, "doing_left": round(left, 1) if left is not None else None,
                "playlist": self.current_playlist(alias) if cls == "playing" else ""}

    def nudge_all_fullscreen(self, delay: float = 7.0) -> None:
        """Send every TV a real keypress so its page can re-enter fullscreen.

        Runs detached after a delay: the pages only drop the identify overlay on
        their next 5 s poll, and a keypress sent before that would be spent on the
        wrong screen.
        """
        aliases = [a for a in (self.cfg["groups"].get("home") or list(self.tvs))
                   if a in self.tvs]

        def work() -> None:
            time.sleep(delay)
            def one(alias: str) -> None:
                tv = self.tvs.get(alias)
                if not tv:
                    return
                try:
                    progress(alias, "sending fullscreen key", 3)
                    tv.send_keys([tv.photos_cfg.get("fullscreen_key", "KEY_ENTER")])
                except Exception as exc:
                    log.debug("%s: fullscreen nudge failed: %s", alias, exc)
                finally:
                    progress_done(alias)
            with ThreadPoolExecutor(max_workers=min(14, max(1, len(aliases)))) as pool:
                list(pool.map(one, aliases))
            log.info("sent fullscreen keypress to %d TV(s)", len(aliases))

        threading.Thread(target=work, name="fullscreen-all", daemon=True).start()

    def identify_numbers(self) -> dict[str, tuple[int, str]]:
        """ip -> (number, alias) for identify mode.

        Every TV shares one homepage URL, so the only way to show each screen a
        different thing is to key off the IP that asked for the page.
        """
        aliases = [a for a in (self.cfg["groups"].get("home") or list(self.tvs))
                   if a in self.tvs]
        return {self.tvs[a].ip: (i, a) for i, a in enumerate(sorted(aliases), 1)}

    def playlists(self) -> list[dict]:
        root = self.cfg.photo_root
        if not root.is_dir():
            return []
        out = []
        for d in sorted(root.iterdir()):
            if d.is_dir():
                out.append({"name": d.name, "count": len(playlist_images(d))})
        return out

    def refresh_status(self) -> None:
        aliases = self.cfg["groups"].get("home") or list(self.tvs)
        aliases = [a for a in aliases if a in self.tvs]
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(aliases)))) as pool:
            rows = list(pool.map(self.probe_one, aliases))
        order = {"busy": 0, "offline": 1, "idle": 2, "off": 3, "playing": 4}
        rows.sort(key=lambda r: (order.get(r["cls"], 9), r["alias"]))
        lists = self.playlists()
        with self._status_lock:
            self._status = rows
            self._playlists = lists
            self._status_at = time.monotonic()

    def status_snapshot(self) -> dict:
        with self._status_lock:
            rows, at = list(self._status), self._status_at
        with self._status_lock:
            note = self._last_command
        with self._status_lock:
            lists = list(self._playlists)
        return {"tvs": rows,
                "age_seconds": round(time.monotonic() - at, 1) if at else None,
                "refresh_seconds": self.STATUS_REFRESH,
                "last_command": note,
                "playlists": lists,
                "playing": self.shared_playlist(),
                "base": self.cfg.get("base_url") or ""}

    def status_loop(self) -> None:
        last_heal = 0.0
        while True:
            try:
                self.refresh_status()
            except Exception as exc:
                log.warning("status sweep failed: %s", exc)
            mins = float(self.cfg.get("auto_heal_minutes", 10) or 0)
            if mins and time.monotonic() - last_heal > mins * 60:
                with self._status_lock:
                    rows = list(self._status)
                # Only 'idle' - powered on but nothing on screen. A TV that is
                # off was probably turned off on purpose; don't wake it.
                stuck = [r["alias"] for r in rows if r["cls"] == "idle"
                         and r["alias"] in self.tvs
                         and self.tvs[r["alias"]].token_file.exists()]
                if stuck:
                    last_heal = time.monotonic()
                    log.info("periodic auto-heal: %s", " ".join(stuck))
                    if self._heal_lock.acquire(blocking=False):
                        def _sweep():
                            try:
                                self.fan_out(stuck, "on", [])
                            finally:
                                self._heal_lock.release()
                        threading.Thread(target=_sweep, name="auto-heal",
                                         daemon=True).start()
                else:
                    last_heal = time.monotonic()
            time.sleep(self.STATUS_REFRESH)

    def run_async(self, command: str) -> str:
        """Run a command on a worker thread so a dashboard button returns at once -
        a 14-TV `on` takes minutes and must not block the page."""
        def work() -> None:
            with self._status_lock:
                self._last_command = f"{command}: running..."
            out = self.dispatch(command)
            summary = " | ".join(l.strip() for l in out.splitlines() if l.strip())
            with self._status_lock:
                self._last_command = f"{command}: {summary[:400]}"
            try:
                self.refresh_status()
            except Exception:
                pass
            # Auto-heal: after anything meant to put pictures on screen, scan and
            # retry whatever did not land. Never after an 'off'.
            parts_ = [p for p in command.lower().split("/") if p]
            verb = parts_[1] if len(parts_) > 1 else ""
            # Whitelist: only commands whose whole point is to put pictures up.
            # This used to be a blacklist, so /key/KEY_VOLUP triggered a heal.
            if (self.cfg.get("auto_heal", True) and verb in ("on", "photos")
                    and "off" not in command.lower()):
                aliases = self.resolve(parts_[0]) or []
                if aliases:
                    fixed = self.heal(aliases)
                    with self._status_lock:
                        self._last_command = (f"{command}: {summary[:280]}"
                                              f"  [auto-heal {fixed}]")
        threading.Thread(target=work, name=f"cmd:{command}", daemon=True).start()
        return command

    def scan_lan(self) -> list[dict]:
        """Find every Samsung TV on this /24 and say whether it is in the config.

        Probes 8002 first: some sets answer the control port but not 8001, which
        is how two Frames were missed the first time round.
        """
        import socket as _s
        base = local_ip_toward(next(iter(self.tvs.values())).ip
                               if self.tvs else "192.168.1.1").rsplit(".", 1)[0]
        known = {spec["ip"]: alias for alias, spec in self.cfg["tvs"].items()}

        def alive(i: int) -> str | None:
            ip = f"{base}.{i}"
            for port in (8002, 8001):
                sk = _s.socket(); sk.settimeout(0.8)
                try:
                    sk.connect((ip, port)); return ip
                except Exception:
                    continue
                finally:
                    sk.close()
            return None

        with ThreadPoolExecutor(max_workers=64) as pool:
            live = [ip for ip in pool.map(alive, range(1, 255)) if ip]

        def ident(ip: str) -> dict | None:
            try:
                with urllib.request.urlopen(f"http://{ip}:8001/api/v2/", timeout=5) as r:
                    d = json.load(r).get("device", {})
            except Exception:
                return None
            if not d.get("modelName"):
                return None
            return {"ip": ip, "model": d.get("modelName", ""),
                    "name": html.unescape(d.get("name", "")),
                    "mac": (d.get("wifiMac") or "").lower(),
                    "net": d.get("networkType", ""),
                    "power": d.get("PowerState", ""),
                    "alias": known.get(ip, "")}

        with ThreadPoolExecutor(max_workers=12) as pool:
            found = [r for r in pool.map(ident, live) if r]
        found.sort(key=lambda r: int(r["ip"].rsplit(".", 1)[1]))
        return found

    def admin(self, parts: list[str]) -> str:
        """/admin/... - edit the fleet from the web interface. Writes config.json."""
        action = parts[0].lower() if parts else ""
        tvs = dict(self.cfg["tvs"])
        groups = {k: list(v) for k, v in self.cfg["groups"].items()}

        def valid_ip(v: str) -> bool:
            parts_ = v.split(".")
            return (len(parts_) == 4
                    and all(x.isdigit() and 0 <= int(x) <= 255 and
                            (x == "0" or not x.startswith("0")) for x in parts_))

        if action == "scan":
            rows = self.scan_lan()
            new = [r for r in rows if not r["alias"]]
            return json.dumps({"found": rows, "new": len(new)})

        if action == "add" and len(parts) >= 3:
            alias = re.sub(r"[^a-z0-9-]", "-", parts[1].lower()).strip("-")
            ip = parts[2]
            mac = parts[3].lower() if len(parts) > 3 else ""
            if not alias or alias != parts[1].lower():
                return (f"ERROR bad alias '{parts[1]}' - use letters, digits and "
                        f"dashes only")
            if not valid_ip(ip):
                return f"ERROR bad ip '{ip}'"
            if alias in tvs:
                return f"ERROR '{alias}' already exists"
            if any(v["ip"] == ip for v in tvs.values()):
                return f"ERROR {ip} is already configured"
            tvs[alias] = {"ip": ip, "mac": mac, "label": f"{alias} ({ip})",
                          "photos": {"method": "browser", "interval_seconds": 10,
                                     "fit": "contain"}}
            groups.setdefault("home", []).append(alias)
            self.cfg.save({"tvs": tvs, "groups": groups})
            self.reload()
            return f"added {alias} at {ip} - now pair it: /admin/pair/{alias}"

        if action == "remove" and len(parts) >= 2:
            alias = parts[1].lower()
            if alias not in tvs:
                return f"ERROR no TV '{alias}'"
            tvs.pop(alias)
            for name in list(groups):
                groups[name] = [a for a in groups[name] if a != alias]
            self.cfg.save({"tvs": tvs, "groups": groups})
            self.current.pop(alias, None)
            self.save_state()
            self.reload()
            return f"removed {alias}"

        if action == "rename" and len(parts) >= 3:
            old = parts[1].lower()
            new = re.sub(r"[^a-z0-9-]", "-", parts[2].lower()).strip("-")
            if old not in tvs:
                return f"ERROR no TV '{old}'"
            if not new or new in tvs:
                return f"ERROR bad or duplicate alias '{new}'"
            spec = tvs.pop(old)
            spec["label"] = spec.get("label", "").replace(old, new) or new
            tvs[new] = spec
            for name in list(groups):
                groups[name] = [new if a == old else a for a in groups[name]]
            self.cfg.save({"tvs": tvs, "groups": groups})
            if old in self.current:
                self.current[new] = self.current.pop(old)
                self.save_state()
            self.reload()
            return f"renamed {old} -> {new}"

        if action == "setip" and len(parts) >= 3:
            alias, ip = parts[1].lower(), parts[2]
            if alias not in tvs:
                return f"ERROR no TV '{alias}'"
            if not valid_ip(ip):
                return f"ERROR bad ip '{ip}'"
            mac = parts[3].lower() if len(parts) > 3 else ""
            tvs[alias] = dict(tvs[alias], ip=ip)
            if mac:
                tvs[alias]["mac"] = mac
            tvs[alias].pop("token", None)   # a swapped set needs a fresh pairing
            self.cfg.save({"tvs": tvs, "groups": groups})
            self.reload()
            return (f"{alias} now {ip} - token cleared, pair it: /admin/pair/{alias}")

        if action == "pair" and len(parts) >= 2:
            alias = parts[1].lower()
            if alias not in self.tvs:
                return f"ERROR no TV '{alias}'"
            tv = self.tvs[alias]

            def work() -> None:
                with self._status_lock:
                    self._last_command = f"pair {alias}: press ALLOW on that TV..."
                tv.token_file.unlink(missing_ok=True)
                try:
                    ws = tv.control_ws(timeout=90)
                    ws.close()
                except Exception as exc:
                    msg = f"pair {alias}: FAILED {type(exc).__name__}"
                else:
                    time.sleep(2)
                    try:
                        tv.send_keys(["KEY_VOLUP"])
                        tv.send_keys(["KEY_VOLDOWN"])
                        msg = f"pair {alias}: PAIRED and verified"
                    except Exception as exc:
                        msg = f"pair {alias}: token stored but rejected ({self.explain(exc)})"
                with self._status_lock:
                    self._last_command = msg
                log.info(msg)

            threading.Thread(target=work, name=f"pair:{alias}", daemon=True).start()
            return f"pairing {alias} - press ALLOW on that screen within 90s"

        return ("ERROR usage: /admin/scan | /admin/add/<alias>/<ip>[/<mac>] | "
                "/admin/remove/<alias> | /admin/rename/<old>/<new> | "
                "/admin/setip/<alias>/<ip>[/<mac>] | /admin/pair/<alias>")

    def heal(self, aliases: list[str], rounds: int = 2, settle: float = 25.0) -> str:
        """Only one heal runs at a time, fleet-wide."""
        if not self._heal_lock.acquire(blocking=False):
            return "skipped, a heal is already running"
        try:
            return self._heal(aliases, rounds, settle)
        finally:
            self._heal_lock.release()

    def _heal(self, aliases: list[str], rounds: int = 2, settle: float = 25.0) -> str:
        """Re-run `on` for any TV in `aliases` that is not actually playing.

        Runs after a sequence finishes: a TV can accept every command and still
        end up not showing anything, and until now that needed a human to press
        Fix me.
        """
        out = []
        for round_no in range(1, rounds + 1):
            time.sleep(settle)
            try:
                self.refresh_status()
            except Exception as exc:
                log.warning("heal: status sweep failed: %s", exc)
                break
            with self._status_lock:
                rows = list(self._status)
            bad = [r["alias"] for r in rows
                   if r["alias"] in aliases and r["cls"] != "playing"
                   and r["power"] != "unreachable"
                   # A TV we hold no token for can never come good; retrying it
                   # just walks its menus around forever.
                   and self.tvs[r["alias"]].token_file.exists()]
            if not bad:
                out.append(f"round {round_no}: all playing")
                break
            log.info("auto-heal round %d, retrying: %s", round_no, " ".join(bad))
            out.append(f"round {round_no}: retried {' '.join(bad)}")
            self.fan_out(bad, "on", [])
        return "; ".join(out)

    def permitted(self, ip: str) -> bool:
        """allow_from gate. Loopback is always allowed so that a wrong entry in
        allow_from can still be fixed with /reload instead of a service restart."""
        allow = self.cfg.get("allow_from") or []
        return not allow or ip in allow or ip in ("127.0.0.1", "::1")

    def resolve(self, target: str) -> list[str] | None:
        """A TV alias beats a group of the same name.

        Renaming a TV to 'office' silently collided with the existing 'office'
        group, so /office/key/... went to two TVs at another site instead.
        """
        target = target.lower()
        if target in self.tvs:
            return [target]
        if target in self.cfg["groups"]:
            return [a for a in self.cfg["groups"][target] if a in self.tvs]
        if target == "all":
            return list(self.tvs)
        return None

    def help_text(self) -> str:
        groups = self.cfg["groups"]
        return "\n".join([
            "tvbridge - Samsung TV IP control",
            "",
            "  /<target>/on            /<target>/off           /<target>/toggle",
            "  /<target>/wake          power on only, no slideshow",
            "  /<target>/status        /<target>/key/KEY_VOLUP  /<target>/keys/KEY_UP,@500,KEY_ENTER",
            "  /<target>/volume/35     /<target>/mute/on        /<target>/mute/off",
            "  /<target>/photos        /<target>/photos/<playlist>   /<target>/photos/off",
            "  /<target>/source/<name> /<target>/macro/<name>   /<target>/app/<app_id>",
            "  /<target>/url/<encoded-url>",
            "  /playlist/<name>        change what ALL TVs show (no TV named)",
            "  /identify/on|off        show a big number on each screen",
            "  /fullscreen             keypress every TV so its page goes fullscreen",
            "  /dashboard              live status of every TV in a browser",
            "  /reload    /playlists   /homepages   /health",
            "",
            "TVs:    " + " ".join(sorted(self.tvs)),
            "Groups: " + " ".join(sorted(groups)) + " all",
        ])

    def dispatch(self, raw: str) -> str:
        """Run one command string. Never raises - controllers get text back."""
        raw = raw.strip()
        # A URL argument holds '/' and ':', so pull it off the raw string before
        # the path gets split into tokens.
        url_cmd = re.match(r"^([A-Za-z0-9_.-]+)[/\s]+url[/\s]+(.+)$", raw, re.I)
        if url_cmd:
            aliases = self.resolve(url_cmd.group(1))
            if aliases:
                url = urllib.parse.unquote(url_cmd.group(2).strip())
                return self.fan_out(aliases, "url", [url])

        parts = [urllib.parse.unquote(p) for p in re.split(r"[/\s]+", raw) if p]
        if not parts or parts[0] in ("help", "?"):
            return self.help_text()

        if parts[0] == "reload":
            return self.reload()
        if parts[0] == "health":
            return "ok"
        # /playlist/<name> - change what every TV shows, without naming a TV.
        # Pure pointer move: no TV I/O, so it returns instantly and screens that
        # are already showing the slideshow pick it up on their next poll.
        if parts[0] in ("playlist", "show") and len(parts) >= 2:
            name = " ".join(parts[1:])
            folder = playlist_dir(self.cfg, name)
            if folder is None:
                return f"ERROR bad playlist name '{name}'"
            if not folder.is_dir():
                have = [d.name for d in sorted(self.cfg.photo_root.iterdir())
                        if d.is_dir()] if self.cfg.photo_root.is_dir() else []
                return f"ERROR no playlist '{name}' - have: {', '.join(have) or 'none'}"
            count = len(playlist_images(folder))
            if not count:
                return f"ERROR playlist '{name}' has no images the TVs can display"
            self.shared = name
            for alias in self.tvs:
                self.current[alias] = name
            self.save_state()
            return f"all TVs -> {name} ({count} images); screens change within ~5s"

        if parts[0] == "admin":
            return self.admin(parts[1:])

        if parts[0] == "fullscreen":
            self.nudge_all_fullscreen(delay=0.5)
            return "sending a fullscreen keypress to every TV"

        if parts[0] == "identify":
            want = not (len(parts) > 1 and parts[1].lower() in ("off", "0", "false"))
            self.identify = want
            if not want:
                self.nudge_all_fullscreen()
                return ("identify off - screens back to the slideshow within ~5s, "
                        "then a fullscreen keypress goes to every TV")
            lines = ["identify ON - each screen shows its number within ~5s:"]
            for ip, (n, alias) in sorted(self.identify_numbers().items(),
                                         key=lambda kv: kv[1][0]):
                lines.append(f"  {n:>3}  {alias:<12} {ip}")
            lines.append("")
            lines.append("Tell me which number is in which room, then /identify/off")
            return "\n".join(lines)

        if parts[0] == "homepages":
            base = (self.cfg.get("base_url")
                    or f"http://{local_ip_toward(next(iter(self.tvs.values())).ip)}:{self.cfg['http_port']}"
                    if self.tvs else "")
            lines = [
                "SET THIS ONE URL AS THE BROWSER HOMEPAGE ON EVERY TV:",
                "",
                f"    {base}/slideshow/live/all",
                "",
                "Then change what they all show with:",
                "",
                f"    {base}/playlist/<name>",
                "",
                f"Playlists: {', '.join(sorted(d.name for d in self.cfg.photo_root.iterdir() if d.is_dir())) if self.cfg.photo_root.is_dir() else 'none'}",
            ]
            if not self.cfg.get("base_url"):
                lines.append("")
                lines.append("NOTE base_url is unset, so these use this PC's current address. "
                             "Set base_url in config.json to this host's fixed address "
                             "BEFORE setting homepages, or they break when the IP changes.")
            return "\n".join(lines)
        if parts[0] == "playlists":
            root = self.cfg.photo_root
            if not root.is_dir():
                return f"no photo root at {root}"
            return "\n".join(
                f"{d.name}: {len(playlist_images(d))} image(s)"
                for d in sorted(root.iterdir()) if d.is_dir()
            ) or f"no playlist folders in {root}"

        aliases = self.resolve(parts[0])
        if aliases is None:
            return (f"ERROR unknown target '{parts[0]}'\n"
                    f"TVs: {' '.join(sorted(self.tvs))}\n"
                    f"Groups: {' '.join(sorted(self.cfg['groups']))} all")
        if not aliases:
            return f"ERROR group '{parts[0]}' has no known TVs"

        action, args = (parts[1].lower() if len(parts) > 1 else "status"), parts[2:]
        return self.fan_out(aliases, action, args)

    def fan_out(self, aliases: list[str], action: str, args: list[str]) -> str:
        """Run one action against every alias in parallel, prefixing each result."""
        def one(alias: str) -> str:
            try:
                return f"[{alias}] {self.act(self.tvs[alias], action, args)}"
            except Exception as exc:
                return f"[{alias}] ERROR {self.explain(exc)}"
            finally:
                progress_done(alias)

        if len(aliases) == 1:
            return one(aliases[0])
        with ThreadPoolExecutor(max_workers=len(aliases)) as pool:
            return "\n".join(pool.map(one, aliases))

    def act(self, tv: Tv, action: str, args: list[str]) -> str:
        if action == "on":
            # "on" is the whole wake-to-slideshow sequence, not just power:
            # power/art-mode, then open the browser on the slideshow, then a real
            # keypress so the page can go fullscreen. Set on_restores_slideshow
            # to false for power only.
            powered = tv.on()
            if (tv.photos_cfg.get("method", "browser") == "browser"
                    and self.cfg.get("on_restores_slideshow", True)):
                shown = tv.photos(self.current_playlist(tv.alias), self.cfg)
                return f"{powered}; {shown}"
            return powered
        if action == "wake":
            return tv.on()  # power only - no browser, no slideshow
        if action in ("off", "toggle", "status"):
            return getattr(tv, action)()
        if action == "key":
            # Interactive single press: use the low-latency path.
            return tv.send_key_fast(args[0]) if args else "ERROR key needs a KEY_ code"
        if action == "keys":
            return tv.send_keys([t for t in re.split(r"[,+]", args[0]) if t]) if args else "ERROR keys needs a list"
        if action == "volume":
            return tv.volume(int(args[0])) if args else "ERROR volume needs 0-100"
        if action == "mute":
            return tv.mute(not args or args[0].lower() in ("on", "1", "true", "yes"))
        if action == "reopen":
            # Force the browser back to its homepage: closes and relaunches, so
            # the TV picks up a changed page or recovers from a stuck one.
            forget_fetch(tv.ip)
            tv.launch_browser()
            for _ in range(24):
                time.sleep(0.5)
                if fetched_since(tv.ip, 0.0):
                    tv.nudge_fullscreen()
                    return "reopened - slideshow fetched"
            return "WARNING reopened the browser but the TV did not request the page"
        if action == "app":
            return tv.app(args[0]) if args else "ERROR app needs an app id"
        if action == "url":
            return tv.open_url(urllib.parse.unquote("/".join(args))) if args else "ERROR url needs a URL"
        if action == "photos":
            # Bare /photos resumes whatever is currently selected rather than
            # snapping back to the default - otherwise a morning power-on routine
            # would silently drop the playlist someone chose.
            playlist = args[0] if args else self.current_playlist(tv.alias)
            if playlist not in ("off", "stop"):
                # Point this TV's live URL at the requested playlist before
                # launching, so a page that is already open switches to it.
                self.current[tv.alias] = playlist
                # Also move the shared pointer, so TVs set to the shared URL
                # follow along. Harmless for TVs on their own per-TV URL.
                self.shared = playlist
                self.save_state()
            return tv.photos(playlist, self.cfg)
        if action in ("source", "macro"):
            if not args:
                return f"ERROR {action} needs a name"
            name = args[0].lower()
            return tv.macro(f"source-{name}" if action == "source" else name, self.cfg["macros"])
        if action == "pair":
            return pair(tv)
        return f"ERROR unknown action '{action}' - try /help"

    @staticmethod
    def explain(exc: Exception) -> str:
        name = type(exc).__name__
        if isinstance(exc, NotPaired) or "Unauthorized" in name:
            return ("not paired - the TV rejected our token. Run: "
                    "pair.bat <alias>  and press ALLOW on that screen")
        if isinstance(exc, (ConnectionRefusedError, TimeoutError, socket.timeout, OSError)):
            return f"{name}: TV unreachable or in standby ({exc})"
        return f"{name}: {exc}"


# --------------------------------------------------------------------------- #
# servers
# --------------------------------------------------------------------------- #

def make_http_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tvbridge"
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
                  cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            # Some controllers (Loxone among them) mishandle keep-alive on a
            # 1.1 response, so every reply closes its connection.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path)
            raw_parts = [p for p in path.path.split("/") if p]

            # Slideshow assets are read by the TVs, not commands from a
            # controller, so allow_from must not apply to them.
            if raw_parts and raw_parts[0] == "slideshow":
                note_fetch(self.client_address[0])
                parts = [urllib.parse.unquote(p) for p in raw_parts[1:]]
                return self.slideshow(parts, urllib.parse.parse_qs(path.query))
            if raw_parts and raw_parts[0] == "favicon.ico":
                return self._send(404, b"")

            # Read-only monitoring, exempt from allow_from like the slideshow:
            # locking the bridge down to a controller should not also lock you
            # out of looking at it.
            if raw_parts and raw_parts[0] in ("dashboard", "status.html"):
                return self._send(200, DASHBOARD_HTML.encode("utf-8"),
                                  "text/html; charset=utf-8")
            if raw_parts and raw_parts[0] == "x" and len(raw_parts) > 1:
                if not bridge.permitted(self.client_address[0]):
                    return self._send(403, b"forbidden\n")
                cmd = "/".join(urllib.parse.unquote(p) for p in raw_parts[1:])
                bridge.run_async(cmd)
                return self._send(200, json.dumps({"started": cmd}).encode(),
                                  "application/json")
            if len(raw_parts) == 2 and raw_parts[0] == "api" and raw_parts[1].split("?")[0] == "status":
                body = json.dumps(bridge.status_snapshot()).encode("utf-8")
                return self._send(200, body, "application/json")

            if not bridge.permitted(self.client_address[0]):
                log.warning("rejected command from %s (not in allow_from)",
                            self.client_address[0])
                return self._send(403, b"forbidden\n")

            # Pass the path still percent-encoded; dispatch decodes per token so
            # an encoded URL argument survives the split.
            out = bridge.dispatch("/".join(raw_parts) or "help")
            code = 400 if out.startswith("ERROR") else 200
            self._send(code, (out + "\n").encode("utf-8"))

        do_POST = do_GET
        do_HEAD = do_GET

        # /slideshow/<playlist>[/...]  or  /slideshow/live/<tv>[/...]
        def slideshow(self, parts: list[str], query: dict):
            if not parts:
                return self._send(404, b"usage: /slideshow/<playlist>\n")

            # A TV's live URL never changes; what it resolves to does. That is
            # what makes playlist switching work on firmware that ignores URLs:
            # the browser stays on one page and the page follows the pointer.
            if parts[0] == "live":
                if len(parts) < 2:
                    return self._send(404, b"usage: /slideshow/live/all or /slideshow/live/<tv-alias>\n")
                # "all" is the one URL every TV can share, so they stay in sync.
                if parts[1] == "all":
                    return self.render(bridge.shared_playlist(), parts[2:], query,
                                       "/slideshow/live/all/")
                if parts[1] not in bridge.tvs:
                    return self._send(404, b"usage: /slideshow/live/all or /slideshow/live/<tv-alias>\n")
                alias = parts[1]
                playlist = bridge.current_playlist(alias)
                base = f"/slideshow/live/{urllib.parse.quote(alias)}/"
                return self.render(playlist, parts[2:], query, base)

            base = f"/slideshow/{urllib.parse.quote(parts[0])}/"
            return self.render(parts[0], parts[1:], query, base)

        def render(self, playlist: str, rest: list[str], query: dict, base: str):
            """Serve the page, its manifest, or one image, for `playlist`."""
            folder = playlist_dir(bridge.cfg, playlist)
            if folder is None:
                return self._send(400, b"bad playlist name\n")

            if not rest:
                secs = max(2, int((query.get("s") or ["10"])[0] or 10))
                fit = (query.get("fit") or ["contain"])[0]
                fit = fit if fit in ("contain", "cover") else "contain"
                page = (SLIDESHOW_HTML
                        .replace("__BASE__", base)
                        .replace("__PAGEVER__", PAGE_VERSION)
                        .replace("__PLAYLIST__", playlist.replace("'", ""))
                        .replace("__TITLE__", html.escape(playlist))
                        .replace("__SECS__", str(secs))
                        .replace("__FIT__", fit))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

            if rest[0] == "manifest.json":
                payload = {"playlist": playlist, "page": PAGE_VERSION,
                           "images": playlist_images(folder)}
                if bridge.identify:
                    who = bridge.identify_numbers().get(self.client_address[0])
                    payload["identify"] = ({"n": who[0], "alias": who[1]} if who
                                           else {"n": "?", "alias": self.client_address[0]})
                return self._send(200, json.dumps(payload).encode("utf-8"),
                                  "application/json")

            if rest[0] == "img" and len(rest) == 2:
                target = (folder / rest[1]).resolve()
                ok = (target.is_file()
                      and folder.resolve() in target.parents
                      and target.suffix.lower() in IMAGE_EXTS)
                if not ok:
                    return self._send(404, b"no such image\n")
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), ctype, cache="max-age=3600")

            self._send(404, b"not found\n")

        def log_message(self, fmt, *a):
            log.debug("http %s - %s", self.client_address[0], fmt % a)

    return Handler


def make_tcp_handler(bridge: Bridge):
    class Handler(socketserver.StreamRequestHandler):
        timeout = 120

        def handle(self):
            if not bridge.permitted(self.client_address[0]):
                return
            for line in self.rfile:
                cmd = line.decode("utf-8", "replace").strip()
                if not cmd:
                    continue
                if cmd.lower() in ("quit", "exit", "bye"):
                    return
                log.info("tcp %s: %s", self.client_address[0], cmd)
                self.wfile.write((bridge.dispatch(cmd) + "\r\n").encode("utf-8"))

    return Handler


def udp_loop(bridge: Bridge, port: int) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    log.info("UDP listening on 0.0.0.0:%d", port)
    while True:
        try:
            data, addr = s.recvfrom(2048)
        except OSError:
            continue
        if not bridge.permitted(addr[0]):
            continue
        for cmd in data.decode("utf-8", "replace").splitlines():
            if not cmd.strip():
                continue
            log.info("udp %s: %s", addr[0], cmd.strip())
            reply = bridge.dispatch(cmd.strip())
            try:
                s.sendto((reply + "\n").encode("utf-8"), addr)
            except OSError:
                pass


class ReuseTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def setup_logging(cfg: Config, verbose: bool) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    logfile = Path(cfg["log_file"])
    if not logfile.is_absolute():
        logfile = STATE_DIR / logfile
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]


def pair(tv: Tv) -> str:
    """Trigger the one-time ALLOW prompt and persist the token the TV issues."""
    tv.token_file.parent.mkdir(parents=True, exist_ok=True)
    ws = tv.control_ws(timeout=60)  # writes the issued token on connect
    ws.close()
    if not tv.token_file.exists():
        return "connected but the TV issued no token - it may already be paired"
    return f"paired - token saved to {tv.token_file}"


def cmd_pair(bridge: Bridge, argv: list[str]) -> int:
    targets = bridge.resolve(argv[0]) if argv else list(bridge.tvs)
    if not targets:
        print(f"unknown target '{argv[0]}'")
        return 1
    for alias in targets:
        tv = bridge.tvs[alias]
        print(f"\n== {alias} ({tv.ip}) ==")
        print("The TV must be ON. Watch the screen and choose ALLOW with the remote (60s).")
        try:
            print(pair(tv))
        except Exception as exc:
            print(f"FAILED: {Bridge.explain(exc)}")
    return 0


def cmd_learn(bridge: Bridge, argv: list[str]) -> int:
    """Interactive helper for building a USB-photos key macro by hand.

    Samsung exposes no API for picking the USB source, so the only route is a
    blind remote-key sequence. Send keys one at a time, watch the screen, and
    this prints the finished macro to paste into config.json.
    """
    if not argv or argv[0] not in bridge.tvs:
        print(f"usage: tvbridge.py learn <alias>   ({' '.join(sorted(bridge.tvs))})")
        return 1
    tv = bridge.tvs[argv[0]]
    recorded: list[str] = []
    print(f"Recording a macro for {tv.alias} ({tv.ip}).")
    print("Type a key (KEY_SOURCE, KEY_RIGHT, KEY_ENTER, KEY_PLAY ...), or:")
    print("  @500   insert a 500 ms wait      u  undo last     p  replay from scratch")
    print("  d      done, print the macro     q  quit without printing")
    print("Tip: on most Tizen sets the sequence is KEY_SOURCE, arrows to the USB tile,")
    print("     KEY_ENTER, arrows to the photo folder, KEY_ENTER, then KEY_PLAY.\n")
    while True:
        try:
            entry = input(f"[{len(recorded)}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not entry:
            continue
        low = entry.lower()
        if low == "q":
            return 0
        if low == "d":
            print("\nPaste this into the TV's photos block in config.json:\n")
            print('      "usb_macro": ' + json.dumps(recorded))
            return 0
        if low == "u":
            if recorded:
                print("removed", recorded.pop())
            continue
        if low == "p":
            print(tv.send_keys(recorded))
            continue
        if entry.startswith("@"):
            recorded.append(entry)
            continue
        key = entry.upper()
        if not key.startswith("KEY_"):
            key = "KEY_" + key
        try:
            print("  ", tv.send_keys([key]))
            recorded.append(key)
        except Exception as exc:
            print("   FAILED:", Bridge.explain(exc))
    return 0


def cmd_doctor(bridge: Bridge) -> int:
    """Pre-flight: is each TV reachable, paired, and are the playlists populated?"""
    print(f"config     {CONFIG_PATH}")
    print(f"state dir  {STATE_DIR}")
    print(f"photo root {bridge.cfg.photo_root}")
    try:
        import samsungtvws  # noqa: F401
        print("samsungtvws installed   OK")
    except ImportError:
        print("samsungtvws MISSING     run: py -m pip install -r requirements.txt")
    print()
    for alias, tv in sorted(bridge.tvs.items()):
        method = tv.photos_cfg.get("method", "browser")
        print(f"{alias:<12} {tv.ip:<16} photos={method:<8} {tv.status()}")
        if method == "browser":
            playlist = bridge.current_playlist(alias)
            folder = playlist_dir(bridge.cfg, playlist)
            n = len(playlist_images(folder)) if folder else 0
            print(f"{'':<12} homepage to set on this TV: {tv.live_url(bridge.cfg)}")
            print(f"{'':<12} serving playlist '{playlist}' ({n} image(s)); "
                  f"browser app {tv.browser_app_id() or 'NOT FOUND'}")
    return 0


def cmd_run(bridge: Bridge) -> int:
    cfg = bridge.cfg
    http = ThreadingHTTPServer(("0.0.0.0", cfg["http_port"]), make_http_handler(bridge))
    http.daemon_threads = True
    tcp = ReuseTCPServer(("0.0.0.0", cfg["tcp_port"]), make_tcp_handler(bridge))

    log.info("HTTP listening on 0.0.0.0:%d", cfg["http_port"])
    log.info("TCP  listening on 0.0.0.0:%d", cfg["tcp_port"])
    for name, fn in (("tcp", tcp.serve_forever),
                     ("udp", lambda: udp_loop(bridge, cfg["udp_port"])),
                     ("status", bridge.status_loop)):
        threading.Thread(target=fn, name=name, daemon=True).start()

    log.info("%d TVs: %s", len(bridge.tvs), " ".join(sorted(bridge.tvs)))
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    return 0


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    argv = [a for a in argv if a != "-v"]
    cmd = argv[0].lower() if argv else "run"
    rest = argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cfg = Config(CONFIG_PATH)
    setup_logging(cfg, verbose)
    migrate_state()   # before Bridge(), which seeds and reads tokens
    bridge = Bridge(cfg)

    if cmd == "run":
        return cmd_run(bridge)
    if cmd == "pair":
        return cmd_pair(bridge, rest)
    if cmd == "learn":
        return cmd_learn(bridge, rest)
    if cmd == "doctor":
        return cmd_doctor(bridge)
    # anything else is treated as a one-shot command, same grammar as the network
    print(bridge.dispatch(" ".join(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
