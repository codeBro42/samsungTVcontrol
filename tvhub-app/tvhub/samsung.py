"""tvhub.samsung - the Samsung Tizen wire protocols, and nothing else.

Five separate protocols live on one TV, and each answers a different question:

    REST   :8001  /api/v2/                 power, model, MAC, Frame flag, Smart Hub
    WS     :8002  samsung.remote.control    key presses, app launch (paired)
    WS     :8002  com.samsung.art-app       Frame art mode (paired)
    DIAL   :8080  /ws/app/WebBrowser        is the browser up? (no pairing, cannot hang)
    UPnP   :9197  /upnp/control/...         volume and mute (no pairing)
    WoL    :9/:7  UDP magic packet          wake a set with no open TCP ports

This module is pure protocol: no config, no state, no aliases, no policy. It
never writes a file (a token issued by a TV is handed back to the caller through
``RemoteChannel.issued_token``), and every function is safe to call against a TV
that is off, asleep, on another subnet or simply not there - it reports that
instead of raising, wherever a caller could reasonably carry on.

It is also the ONLY module allowed to import ``websocket-client`` or
``requests`` (contract 0.3). ``samsungtvws`` is deliberately not used: its
control channel treats a benign ``ms.remote.touchDisable`` frame as fatal, which
broke power-off at exactly the moment the slideshow was on screen.

Almost every unobvious choice below was measured against real hardware and is
commented with the measurement. Those comments are the point - a "simplification"
that removes one of them reintroduces a bug that cost days to find.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable
from urllib.parse import quote

import requests
import websocket

log = logging.getLogger("tvhub")

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

REST_PORT = 8001
WS_PORT = 8002
DIAL_PORT = 8080
UPNP_PORT = 9197

#: Browser app id by firmware year. The id is NOT stable across models, so this
#: is a probe list, not a lookup - see ``probe_browser_app_id``. Newer sets carry
#: unpublished ids that appear in none of these, which is why a caller may pass
#: an ``extra`` candidate and why only POSITIVE probes are ever cached.
BROWSER_APP_IDS = ("org.tizen.browser", "3202010022079", "3201907018784")

#: Tizen drops remote keys that arrive back-to-back, so a sequence spaces them.
DEFAULT_KEY_GAP = 0.35

#: The art-app channel name, on the same host and port as the remote control.
ART_CHANNEL = "com.samsung.art-app"
REMOTE_CHANNEL = "samsung.remote.control"

#: Keys this module refuses to put on the wire, with the reason.
#:
#: KEY_POWEROFF is accepted by the channel and then silently ignored by some
#: firmware - measured: no effect 61 s after a clean send. Anything that "works"
#: on one model and no-ops on another is worse than an error, because the caller
#: reports success and the TV stays on. KEY_POWER (a toggle) is the working key,
#: and contract I2 makes this a hard invariant: no code path, ever. Enforcing it
#: at the one chokepoint every key passes through also covers hand-recorded
#: macros, where a human can otherwise type it in. Read-only by construction.
REFUSED_KEYS = MappingProxyType({
    "KEY_POWEROFF": "silently ignored by some firmware (measured: no effect "
                    "after 61s) - use KEY_POWER",
})

#: Guard against a typo like ``KEY_UP*99999`` pinning a TV for half an hour.
MAX_KEY_REPEAT = 100

_UPNP_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"

_HEX = re.compile(r"^[0-9a-f]{12}$")
_KEY_OK = re.compile(r"^KEY_[A-Z0-9_]+$")
_DIAL_STATE = re.compile(r"<state>([^<]*)</state>", re.IGNORECASE)
_CUR_VOLUME = re.compile(r"<CurrentVolume>\s*(\d+)\s*</CurrentVolume>", re.IGNORECASE)
_CUR_MUTE = re.compile(r"<CurrentMute>\s*([^<]*?)\s*</CurrentMute>", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class SamsungError(Exception):
    """Base class for every error raised by this module."""


class NotPaired(SamsungError):
    """The TV refused our identity, or has never been told to trust it.

    Raised both from the handshake (``ms.channel.unauthorized`` /
    ``ms.channel.timeOut``) and from ``drain_for_auth_error`` - a bad token still
    completes the WebSocket handshake, so a rejection often only shows up as an
    ``ms.error`` frame after the first command.
    """


class Unreachable(SamsungError):
    """The TV did not answer at all: off, asleep, or not on this network."""


class ArtHung(SamsungError):
    """A Frame's art channel blocked past its wall-clock bound and was abandoned.

    Deliberately a distinct error rather than a plain ``None``: contract 11.2 /
    I6 require the caller to mark that TV's art channel dead and never call it
    again until an explicit verify. One art call that hung while a lock was held
    wedged a TV permanently and turned a 14-TV group command into 25 minutes, so
    "unknown" and "wedged" cannot share a return value.

    A caller that does not need the distinction can treat it as "unknown".
    """


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def normalize_mac(value: Any) -> str:
    """``AA-BB-CC-DD-EE-FF`` / ``aabb.ccdd.eeff`` -> ``aa:bb:cc:dd:ee:ff``.

    Returns "" for anything that is not a 48-bit address, so a mistyped MAC in
    config becomes "no MAC" - which callers already handle - rather than an
    exception from deep inside a wake attempt.
    """
    text = str(value or "").strip().lower()
    for junk in (":", "-", ".", " ", "_"):
        text = text.replace(junk, "")
    if not _HEX.match(text):
        return ""
    return ":".join(text[i:i + 2] for i in range(0, 12, 2))


def _tri_bool(value: Any) -> bool | None:
    """True / False / None from a field a TV may send as bool, string or absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _safe_call(callback: Callable[..., Any] | None, *args: Any) -> None:
    """Fire a progress callback without letting it break a device conversation."""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception as exc:  # a UI callback must never abort a macro mid-way
        log.debug("progress callback failed (%s: %s)", type(exc).__name__, exc)


# --------------------------------------------------------------------------- #
# REST on :8001 - the only place power is judged
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeviceInfo:
    """What ``GET http://<ip>:8001/api/v2/`` tells us. No pairing needed."""

    reachable: bool = False
    power: str = "unreachable"          # "on" | "standby" | "unreachable"
    model: str = ""
    name: str = ""
    mac: str = ""
    network: str = ""
    is_frame: bool = False
    smart_hub: bool | None = None
    firmware: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def offline(cls) -> "DeviceInfo":
        """The answer for a TV that did not respond at all."""
        return cls()


def device_info(ip: str, timeout: float = 4.0) -> DeviceInfo:
    """Read the device document. Never raises.

    ``PowerState`` from this endpoint is the ONLY trustworthy power reading. In
    particular it must never be inferred from whether a WebSocket conversation
    ended tidily: the TV tears the control channel down mid-command as it changes
    power state, so exceptions there are normal and mean nothing.

    Note for Frames: PowerState reads "on" both in art mode and while showing
    input, so this cannot answer "is the Frame off" - that is DIAL's job (6.9)
    and the caller's policy.
    """
    try:
        resp = requests.get("http://%s:%d/api/v2/" % (ip, REST_PORT), timeout=timeout)
    except Exception as exc:  # requests.RequestException plus socket/ssl oddities
        log.debug("%s: device_info unreachable (%s: %s)", ip, type(exc).__name__, exc)
        return DeviceInfo.offline()

    raw: dict = {}
    if resp.status_code == 200:
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                raw = parsed
        except ValueError:
            log.debug("%s: device_info returned non-JSON", ip)
    else:
        # TCP answered, so the set is on the network and merely unhelpful; that
        # is "standby", not "gone".
        log.debug("%s: device_info HTTP %s", ip, resp.status_code)

    dev = raw.get("device")
    if not isinstance(dev, dict):
        dev = {}

    state = str(dev.get("PowerState") or "").strip().lower()
    # Reachable but no PowerState field => standby (contract 6.1). Anything that
    # is not literally "on" collapses to standby so the tri-state promise of
    # power_state() holds; the untouched document stays in .raw.
    power = "on" if state == "on" else "standby"

    return DeviceInfo(
        reachable=True,
        power=power,
        model=str(dev.get("modelName") or ""),
        # The TV HTML-escapes its own name, so a set called "Drew's TV" arrives
        # as "Drew&#39;s TV" and would be shown that way in the UI.
        name=html.unescape(str(dev.get("name") or "")),
        mac=normalize_mac(dev.get("wifiMac")),
        network=str(dev.get("networkType") or ""),
        # FrameTVSupport arrives as the STRING "true" on every set that has it.
        is_frame=str(dev.get("FrameTVSupport")).strip().lower() == "true",
        # Smart Hub signed out => the TV reports no apps at all and nothing can
        # be launched. Callers surface this rather than chasing launch failures.
        smart_hub=_tri_bool(dev.get("smartHubAgreement")),
        firmware=str(dev.get("firmwareVersion") or raw.get("version") or ""),
        raw=raw,
    )


def power_state(ip: str, timeout: float = 4.0) -> str:
    """"on" | "standby" | "unreachable" - judged only from REST (contract I3)."""
    return device_info(ip, timeout=timeout).power


# --------------------------------------------------------------------------- #
# ports and waking
# --------------------------------------------------------------------------- #


def port_open(ip: str, port: int, timeout: float = 1.5) -> bool:
    """One TCP connect. Used for liveness probes and the network scan."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_control_port(
    ip: str,
    seconds: float = 30.0,
    on_tick: Callable[[float], None] | None = None,
) -> bool:
    """Wait for :8002 to answer. Returns True as soon as it does.

    Standby and art mode both CLOSE the control port, so a key sent immediately
    after leaving either is refused and silently lost - the command looks sent
    and nothing happens. Every ladder that leaves standby or art mode must come
    through here first (contract 6.15).

    ``on_tick`` receives the seconds remaining, so the caller can publish a
    countdown instead of an unexplained pause.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        # Time the whole cycle, not just the sleep: a host that silently drops
        # SYNs burns the full connect timeout, and without this the "every 1.5s"
        # poll would quietly become every 3 s and the countdown would lie.
        next_probe = time.monotonic() + 1.5
        if port_open(ip, WS_PORT, 1.5):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _safe_call(on_tick, max(0.0, remaining))
        time.sleep(max(0.0, min(next_probe - time.monotonic(), remaining)))


def wake_on_lan(mac: str, ip: str, bursts: int = 6, spacing: float = 0.5) -> int:
    """Broadcast magic packets. Returns the number of datagrams actually sent.

    Sent as a burst over ~3 s to three targets on two ports because a set in
    deep standby samples the wire intermittently and ignores a single packet
    (measured: ~5 tries needed on one model). The directed broadcast reaches sets
    that ignore the global one; the unicast reaches a set whose ARP entry is
    still alive.

    WoL does not route between subnets and is unreliable over Wi-Fi, so the
    caller must say that when it fails rather than reporting a mystery. A return
    of 0 means nothing left this host - almost always a missing or unusable MAC.
    """
    clean = normalize_mac(mac).replace(":", "")
    if not clean:
        log.warning("wake_on_lan: %r is not a usable MAC - nothing sent", mac)
        return 0

    packet = b"\xff" * 6 + bytes.fromhex(clean) * 16

    targets: list[str] = []
    host = str(ip or "").strip()
    if host.count(".") == 3:
        targets.append(host.rsplit(".", 1)[0] + ".255")   # directed broadcast
    targets.append("255.255.255.255")                     # global broadcast
    if host:
        targets.append(host)                              # unicast

    sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        rounds = max(1, int(bursts))
        for burst in range(rounds):
            for target in targets:
                for port in (9, 7):
                    try:
                        sock.sendto(packet, (target, port))
                        sent += 1
                    except OSError:
                        # A host with no route for 255.255.255.255 refuses just
                        # that target; the others still go out.
                        pass
            if burst < rounds - 1:
                time.sleep(max(0.0, spacing))
    finally:
        sock.close()

    log.debug("wake_on_lan: %s -> %s, %d datagram(s)", clean, targets, sent)
    return sent


# --------------------------------------------------------------------------- #
# DIAL on :8080 - the liveness proxy that cannot hang
# --------------------------------------------------------------------------- #


def dial_browser_state(ip: str, timeout: float = 4.0) -> str:
    """"running" | "stopped" | "unknown" for the TV's browser.

    Plain HTTP, no pairing, and - unlike the art channel - it cannot hang. This
    is how "is the browser up" gets answered, and on a Frame it is how art mode
    is told apart from the slideshow (entering art mode stops the browser).

    Two things it is NOT: proof the page is being displayed (Tizen keeps a
    backgrounded browser loaded while freezing its JS timers, so only a fresh
    page request proves that), and a launcher - DIAL POST returns 200 because
    org.tizen.webserver echoes the body back through CGI, not because anything
    started.
    """
    try:
        resp = requests.get(
            "http://%s:%d/ws/app/WebBrowser" % (ip, DIAL_PORT), timeout=timeout)
    except Exception as exc:
        log.debug("%s: DIAL unreachable (%s: %s)", ip, type(exc).__name__, exc)
        return "unknown"
    if resp.status_code != 200:
        return "unknown"
    match = _DIAL_STATE.search(resp.text or "")
    if not match:
        return "unknown"
    state = match.group(1).strip().lower()
    return state if state in ("running", "stopped") else "unknown"


# --------------------------------------------------------------------------- #
# app endpoints on :8001
# --------------------------------------------------------------------------- #


def _app_url(ip: str, app_id: str) -> str:
    return "http://%s:%d/api/v2/applications/%s" % (
        ip, REST_PORT, quote(str(app_id), safe=""))


def app_installed(ip: str, app_id: str, timeout: float = 4.0) -> bool | None:
    """True / False / None (unknown) for "does this TV expose that app".

    None is a genuine third answer, not a failure to try: an unpaired TV answers
    401, and on some firmware this endpoint 404s for EVERY app id - including
    Netflix - while still happily accepting a launch. So None/False is a hint,
    never a verdict, and only True is worth caching.
    """
    try:
        resp = requests.get(_app_url(ip, app_id), timeout=timeout)
    except Exception as exc:
        log.debug("%s: app_installed(%s) unreachable (%s)", ip, app_id, type(exc).__name__)
        return None
    if resp.status_code == 404:
        return False
    if resp.status_code != 200:
        # 401 before pairing, 403, 5xx: unknown. Must NOT be cached as "no app".
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("id"):
        return True
    return None


def probe_browser_app_id(
    ip: str, extra: str | None = None, timeout: float = 4.0
) -> str | None:
    """First app id this TV admits to having, or None.

    ``extra`` (a previously learned or hand-configured id) is tried first so a
    known-good answer costs one request. Only a True is returned, so the caller
    may cache the result; a None must not be cached as "no browser", because an
    unpaired TV cannot answer this question at all.
    """
    candidates: list[str] = []
    for candidate in (extra,) + BROWSER_APP_IDS:
        text = str(candidate or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    for candidate in candidates:
        if app_installed(ip, candidate, timeout=timeout) is True:
            log.info("%s: browser app id is %s", ip, candidate)
            return candidate
    log.debug("%s: no browser app id confirmed (tried %s)", ip, ", ".join(candidates))
    return None


def rest_app_launch(ip: str, app_id: str, timeout: float = 6.0) -> bool:
    """POST with an empty body = "start this app".

    With no URL the browser lands on its homepage, which is the whole point:
    relaunching it is how a TV is put back on the one shared slideshow address.
    Note that a running browser treats this as a no-op and stays on whatever page
    it was on, so a caller that needs a reload must close it first.
    """
    try:
        resp = requests.post(_app_url(ip, app_id), data=b"", timeout=timeout)
    except Exception as exc:
        log.debug("%s: launch %s failed (%s: %s)", ip, app_id, type(exc).__name__, exc)
        return False
    ok = 200 <= resp.status_code < 300
    log.debug("%s: launch %s -> HTTP %s", ip, app_id, resp.status_code)
    return ok


def rest_app_close(ip: str, app_id: str, timeout: float = 6.0) -> bool:
    """DELETE = "close this app"."""
    try:
        resp = requests.delete(_app_url(ip, app_id), timeout=timeout)
    except Exception as exc:
        log.debug("%s: close %s failed (%s: %s)", ip, app_id, type(exc).__name__, exc)
        return False
    ok = 200 <= resp.status_code < 300
    log.debug("%s: close %s -> HTTP %s", ip, app_id, resp.status_code)
    return ok


# --------------------------------------------------------------------------- #
# UPnP RenderingControl on :9197 - volume and mute without pairing
# --------------------------------------------------------------------------- #


def upnp_available(ip: str, timeout: float = 1.5) -> bool:
    """Is :9197 open? Many models ship with it closed.

    Cheap on purpose: it gates both the volume routes and - more importantly -
    ``verify_by_effect``, which uses volume as its proof of pairing.
    """
    return port_open(ip, UPNP_PORT, timeout)


def _upnp_call(
    ip: str, action: str, extra_xml: str = "", timeout: float = 4.0
) -> str | None:
    """One SOAP round trip. Returns the response body, or None on any failure.

    Argument order inside the action element is fixed by the service description
    (InstanceID, Channel, then Desired*) and a reordered envelope is rejected, so
    callers pass only the trailing arguments.
    """
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        '<u:%s xmlns:u="%s">'
        "<InstanceID>0</InstanceID><Channel>Master</Channel>%s"
        "</u:%s>"
        "</s:Body></s:Envelope>"
    ) % (action, _UPNP_SERVICE, extra_xml, action)

    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": '"%s#%s"' % (_UPNP_SERVICE, action),
        "Connection": "close",
    }
    try:
        resp = requests.post(
            "http://%s:%d/upnp/control/RenderingControl1" % (ip, UPNP_PORT),
            data=envelope.encode("utf-8"), headers=headers, timeout=timeout)
    except Exception as exc:
        log.debug("%s: UPnP %s unreachable (%s: %s)", ip, action, type(exc).__name__, exc)
        return None
    if resp.status_code != 200:
        log.debug("%s: UPnP %s -> HTTP %s", ip, action, resp.status_code)
        return None
    return resp.text or ""


def upnp_get_volume(ip: str, timeout: float = 4.0) -> int | None:
    """0..100, or None when :9197 is closed or the TV would not answer."""
    body = _upnp_call(ip, "GetVolume", timeout=timeout)
    if body is None:
        return None
    match = _CUR_VOLUME.search(body)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def upnp_set_volume(ip: str, level: int, timeout: float = 4.0) -> bool:
    """Set absolute volume. Out-of-range values are clamped, never sent raw."""
    try:
        wanted = int(level)
    except (TypeError, ValueError):
        return False
    wanted = max(0, min(100, wanted))
    body = _upnp_call(
        ip, "SetVolume", "<DesiredVolume>%d</DesiredVolume>" % wanted, timeout=timeout)
    return body is not None


def upnp_get_mute(ip: str, timeout: float = 4.0) -> bool | None:
    """True / False, or None when unavailable."""
    body = _upnp_call(ip, "GetMute", timeout=timeout)
    if body is None:
        return None
    match = _CUR_MUTE.search(body)
    if not match:
        return None
    # Firmware sends either 0/1 or false/true here.
    return _tri_bool(match.group(1))


def upnp_set_mute(ip: str, on: bool, timeout: float = 4.0) -> bool:
    body = _upnp_call(
        ip, "SetMute", "<DesiredMute>%d</DesiredMute>" % (1 if on else 0),
        timeout=timeout)
    return body is not None


# --------------------------------------------------------------------------- #
# key grammar
# --------------------------------------------------------------------------- #


def normalize_key(key: str) -> str:
    """``volup`` -> ``KEY_VOLUP``. Raises ValueError on anything unusable.

    One grammar is used by routes, macros, config and the UI, so this is the
    single place a bare name becomes a real Tizen key name.
    """
    text = str(key or "").strip().replace(" ", "_").replace("-", "_")
    if not text:
        raise ValueError("empty key name")
    text = text.upper()
    if not text.startswith("KEY_"):
        text = "KEY_" + text
    if not _KEY_OK.match(text):
        raise ValueError("not a valid remote key: %r" % (key,))
    return text


def parse_key_sequence(spec: str | Iterable[str]) -> list[str]:
    """``'KEY_UP,@500,KEY_ENTER*3'`` -> flat list, ``@`` waits preserved.

    Grammar (contract 2.2): a bare name is upper-cased and KEY_-prefixed, ``*N``
    repeats, ``@N`` waits N milliseconds, and "," or "+" separate tokens inside
    one string. Repeats are expanded here so every consumer - sender, duration
    estimator, UI - walks the same flat list.

    An empty spec is an empty sequence, not an error: ``fullscreen_key: ""``
    means "do not send the nudge" (contract 3.4).
    """
    parts: list[str]
    if isinstance(spec, str):
        parts = [spec]
    else:
        parts = [] if spec is None else list(spec)

    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        for token in re.split(r"[,+]", str(part)):
            token = token.strip()
            if not token:
                continue
            base, _, count_text = token.partition("*")
            base = base.strip()
            if not base:
                raise ValueError("bad key token %r" % (token,))
            count = 1
            count_text = count_text.strip()
            if count_text:
                if not count_text.isdigit():
                    raise ValueError("bad repeat count in %r" % (token,))
                count = int(count_text)
                if count < 1 or count > MAX_KEY_REPEAT:
                    raise ValueError(
                        "repeat count in %r must be 1..%d" % (token, MAX_KEY_REPEAT))
            if base.startswith("@"):
                digits = base[1:].strip()
                if not digits.isdigit():
                    raise ValueError("bad wait token %r - use @<milliseconds>" % (base,))
                item = "@" + str(int(digits))
            else:
                item = normalize_key(base)
            out.extend([item] * count)
    return out


def _plan_sequence(tokens: list[str], gap: float) -> Iterable[tuple[str, Any]]:
    """Yield ``("wait", seconds)`` / ``("key", "KEY_X")`` steps.

    Shared by the sender and the duration estimator so a countdown shown to a
    human cannot drift from what the sender actually does.

    An explicit ``@N`` absorbs the implicit inter-key gap instead of stacking on
    top of it (``KEY_A,@500,KEY_B`` waits 500 ms, not 850) - but only up to the
    gap: a short wait like ``@100`` is still topped up to ``gap``, because keys
    closer together than that get dropped by Tizen. There is no trailing gap
    after the last key.
    """
    owed = 0.0
    for token in tokens:
        if token.startswith("@"):
            wait = int(token[1:]) / 1000.0
            if wait > 0:
                yield ("wait", wait)
            owed = max(0.0, owed - wait)
            continue
        if owed > 0:
            yield ("wait", owed)
        yield ("key", token)
        owed = max(0.0, gap)


def sequence_duration(keys: list[str], gap: float = DEFAULT_KEY_GAP) -> float:
    """How long ``send_sequence`` will sleep for, in seconds (waits only)."""
    total = 0.0
    for kind, value in _plan_sequence(parse_key_sequence(keys), gap):
        if kind == "wait":
            total += float(value)
    return total


# --------------------------------------------------------------------------- #
# the control channel
# --------------------------------------------------------------------------- #


class RemoteChannel:
    """One open WebSocket to a TV's remote-control (or art) channel.

    Kept as an object rather than hidden inside a helper because the interactive
    path reuses one open socket across presses: a fresh handshake plus auth
    read-back per press made the on-screen remote feel three seconds behind
    every tap.

    This class never touches the filesystem. If the TV issues a token during the
    handshake it is exposed as ``issued_token`` for the caller to persist.
    """

    #: Subclasses (the art app) only change the channel name.
    CHANNEL = REMOTE_CHANNEL

    def __init__(
        self,
        ip: str,
        client_name: str,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.ip = ip
        self.client_name = client_name
        self.token: str | None = str(token).strip() if token else None
        self.issued_token: str | None = None
        self.timeout = float(timeout)
        #: The last channel event seen during the handshake, so a caller can tell
        #: "you refused us" from "the ALLOW prompt timed out".
        self.last_event: str = ""
        self._ws: Any = None

    # -- wire ------------------------------------------------------------- #

    def _url(self) -> str:
        """The channel URL.

        The base64 client name goes in RAW - not percent-encoded. Tokens are
        bound to the exact name string the TV received, so re-encoding the "+"
        and "=" that base64 can contain would silently invalidate every token in
        the field and re-pair every set. Changing ``client_name`` does the same
        thing, which is why it is a config-level warning elsewhere.
        """
        name = base64.b64encode(self.client_name.encode("utf-8")).decode("ascii")
        url = "wss://%s:%d/api/v2/channels/%s?name=%s" % (
            self.ip, WS_PORT, self.CHANNEL, name)
        if self.token:
            url += "&token=%s" % self.token
        return url

    def open(self, wait_seconds: float | None = None) -> "RemoteChannel":
        """Connect and complete the handshake.

        ``wait_seconds`` overrides the socket timeout, which is how pairing
        works: a long timeout HOLDS the Allow prompt on the TV screen while a
        human walks over and accepts it.

        Raises NotPaired (refused), TimeoutError (no answer in time) or
        Unreachable (:8002 would not connect, or closed on us).
        """
        limit = float(wait_seconds) if wait_seconds else self.timeout
        self.close()
        self.last_event = ""
        try:
            ws = websocket.create_connection(
                self._url(),
                # The TV serves a self-signed certificate; there is nothing to
                # verify it against and no alternative port.
                sslopt={"cert_reqs": ssl.CERT_NONE},
                timeout=limit,
                # The art channel can be abandoned mid-read by run_bounded while
                # another thread still holds a reference to this object.
                enable_multithread=True,
            )
        except (websocket.WebSocketTimeoutException, socket.timeout) as exc:
            raise TimeoutError(
                "%s: timed out connecting to the control channel" % self.ip) from exc
        except Exception as exc:
            raise Unreachable(
                "%s: cannot open port %d (%s: %s)"
                % (self.ip, WS_PORT, type(exc).__name__, exc)) from exc

        self._ws = ws
        try:
            self._handshake(limit)
        except Exception:
            self.close()
            raise
        # Drop back to the normal timeout: a 90 s pairing timeout left in place
        # would turn one lost frame into a 90 s stall on the next read.
        try:
            ws.settimeout(self.timeout)
        except Exception:
            pass
        return self

    def _handshake(self, limit: float) -> None:
        """Read frames until ``ms.channel.connect``.

        You MUST read PAST ``ms.remote.touchEnable`` / ``touchDisable`` and any
        other pre-connect event: reading a single frame usually returns one of
        those, so the real answer - including a refusal - goes unnoticed. That
        exact bug made power-off fail whenever the slideshow was on screen.
        """
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv()
            except (websocket.WebSocketTimeoutException, socket.timeout) as exc:
                raise TimeoutError(
                    "%s: no ms.channel.connect within %.0fs" % (self.ip, limit)) from exc
            except Exception as exc:
                raise Unreachable(
                    "%s: control channel died during the handshake (%s: %s)"
                    % (self.ip, type(exc).__name__, exc)) from exc

            if not raw:
                # An empty read means the peer is closing. Never spin on it.
                break

            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue

            event = str(msg.get("event") or "")
            self.last_event = event

            if event == "ms.channel.connect":
                data = msg.get("data")
                issued = ""
                if isinstance(data, dict):
                    issued = str(data.get("token") or "").strip()
                if issued and issued != (self.token or ""):
                    # The TV minted or rotated a token. We never write files;
                    # the caller persists it against the alias.
                    self.issued_token = issued
                return
            if event in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                raise NotPaired("%s: TV answered %s" % (self.ip, event))
            log.debug("%s: reading past pre-connect frame %r", self.ip, event)

        raise Unreachable(
            "%s: the TV closed the control channel before it was ready" % self.ip)

    def close(self) -> None:
        """Best effort. A TV that is changing power state closes on us first."""
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def alive(self) -> bool:
        return bool(self._ws is not None and getattr(self._ws, "connected", False))

    def _require(self) -> Any:
        """The live socket, connecting first if nobody has yet.

        The lazy open exists for the interactive fast path: that code holds one
        channel object across many presses and reuses it, so "is it open yet" is
        this object's business, not the caller's. It also means a channel handed
        around unopened fails with the real reason (NotPaired / Unreachable -
        which every command already raises) instead of a bookkeeping error.
        """
        if self._ws is None:
            self.open()
        return self._ws

    def _send(self, payload: dict, what: str) -> None:
        ws = self._require()
        try:
            ws.send(json.dumps(payload))
        except Exception as exc:
            # Raising is correct even though a torn-down channel is often
            # harmless: only the caller knows whether this was a power command
            # (where the teardown is expected) or a keypress worth retrying.
            raise Unreachable(
                "%s: sending %s failed (%s: %s)"
                % (self.ip, what, type(exc).__name__, exc)) from exc

    # -- commands --------------------------------------------------------- #

    def click(self, key: str) -> None:
        """Send one remote key. Accepts ``volup`` or ``KEY_VOLUP``."""
        name = normalize_key(key)
        reason = REFUSED_KEYS.get(name)
        if reason:
            log.warning("%s: refusing %s - %s", self.ip, name, reason)
            return
        self._send({"method": "ms.remote.control", "params": {
            "Cmd": "Click", "DataOfCmd": name,
            "Option": "false", "TypeOfRemote": "SendRemoteKey"}}, name)

    def send_sequence(
        self,
        keys: list[str],
        gap: float = DEFAULT_KEY_GAP,
        on_key: Callable[[str], None] | None = None,
    ) -> int:
        """Send a parsed or raw sequence. Returns the number of keys sent."""
        sent = 0
        for kind, value in _plan_sequence(parse_key_sequence(keys), gap):
            if kind == "wait":
                time.sleep(float(value))
                continue
            _safe_call(on_key, value)
            self.click(str(value))
            sent += 1
        return sent

    def drain_for_auth_error(self, seconds: float) -> None:
        """Raise NotPaired if the TV objects to our token. Otherwise return.

        A stored token is NOT proof of pairing: with a bad one the TV still
        completes the WebSocket handshake and only objects once you send
        something, with ``{"event":"ms.error","data":{"message":"No Authorized"}}``.

        Drain, do not peek. touchEnable/touchDisable frames are interleaved with
        the real answer, so reading a single frame usually returns one of those
        and a rejected token reports as "sent".
        """
        ws = self._require()
        try:
            previous = ws.gettimeout()
        except Exception:
            previous = None

        deadline = time.monotonic() + max(0.0, seconds)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    ws.settimeout(max(0.2, min(1.0, remaining)))
                except Exception:
                    pass
                try:
                    raw = ws.recv()
                except (websocket.WebSocketTimeoutException, socket.timeout):
                    continue          # silence is the good outcome here
                except Exception as exc:
                    # The channel died. Power commands do that by design, so it
                    # is not evidence either way about the token.
                    log.debug("%s: drain ended (%s: %s)", self.ip, type(exc).__name__, exc)
                    return
                if not raw:
                    return            # peer closing - never spin
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("event") == "ms.error" and \
                        "authorized" in str(msg.get("data")).lower():
                    raise NotPaired("%s: TV rejected our token (%s)"
                                    % (self.ip, str(msg.get("data"))[:120]))
                log.debug("%s: drained frame %r", self.ip, msg.get("event"))
        finally:
            if previous is not None:
                try:
                    ws.settimeout(previous)
                except Exception:
                    pass

    def launch_url(self, app_id: str, url: str) -> None:
        """Ask the TV to open ``url`` in ``app_id``. Never trust the result.

        Many firmwares ACKNOWLEDGE this and then ignore the URL entirely, so the
        caller must confirm by other means (the page actually being fetched from
        us). Do NOT add variants here: DEEP_LINK, "url" instead of metaTag,
        ``ms.application.start`` (which answers ``ms.error: unrecognized method
        value``), typing into the address bar with send_text, and DIAL POST were
        all exhaustively refuted on hardware, including after a firmware update.
        """
        self._send({"method": "ms.channel.emit", "params": {
            "event": "ed.apps.launch", "to": "host",
            "data": {"appId": str(app_id), "action_type": "NATIVE_LAUNCH",
                     "metaTag": str(url)}}}, "ed.apps.launch")

    # -- context manager -------------------------------------------------- #

    def __enter__(self) -> "RemoteChannel":
        if not self.alive():
            self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# one-shot key sending, pairing, and proof of pairing
# --------------------------------------------------------------------------- #


def send_keys(
    ip: str,
    client_name: str,
    token: str | None,
    keys: list[str],
    *,
    timeout: float = 10.0,
    gap: float = DEFAULT_KEY_GAP,
    verify: bool = True,
    on_key: Callable[[str], None] | None = None,
) -> int:
    """Open a channel, send a sequence, close. Returns the number of keys sent.

    ``verify=True`` drains 0.6 s before the first key - clearing the pre-connect
    frames so the post-send drain sees the real answer - and 2.0 s after the
    last, which is the only way a rejected token becomes NotPaired rather than a
    silent no-op. ``verify=False`` skips both: that is the interactive path,
    where 2.6 s of draining per tap is the difference between a remote that feels
    instant and one that feels broken.
    """
    tokens = parse_key_sequence(keys)
    if not tokens:
        return 0
    channel = RemoteChannel(ip, client_name, token, timeout=timeout)
    channel.open()
    try:
        if verify:
            channel.drain_for_auth_error(0.6)
        sent = channel.send_sequence(tokens, gap=gap, on_key=on_key)
        if verify:
            channel.drain_for_auth_error(2.0)
        return sent
    finally:
        channel.close()


def launch_url(
    ip: str,
    client_name: str,
    token: str | None,
    app_id: str,
    url: str,
    timeout: float = 10.0,
) -> bool:
    """One-shot "open this URL in that app". Returns True only if it was SENT.

    True means the TV accepted the frame, NOT that it did anything: many
    firmwares acknowledge this and then ignore the URL entirely, so the caller
    must confirm by the page actually being fetched from us. See
    ``RemoteChannel.launch_url`` for the list of variants already refuted on
    hardware - do not add them.

    A short drain follows so a rejected token surfaces as NotPaired rather than
    as a silent no-op that looks like the firmware quirk.
    """
    channel = RemoteChannel(ip, client_name, token, timeout=timeout)
    channel.open()
    try:
        channel.launch_url(app_id, url)
        channel.drain_for_auth_error(1.0)
        return True
    finally:
        channel.close()


def request_pairing(ip: str, client_name: str, wait_seconds: float = 90.0) -> str:
    """Ask the TV for a token and wait for a human to press ALLOW.

    Returns the issued token; the caller persists it. Raises TimeoutError when
    nobody accepted, NotPaired on an explicit refusal, Unreachable when :8002
    will not connect.

    The long socket timeout is what holds the Allow prompt on screen - there is
    no separate "wait for the user" call. Pairing is subnet-sensitive: a TV on
    another subnet rejects it instantly, so the error text says so rather than
    leaving someone to guess.
    """
    if not port_open(ip, WS_PORT, 3.0):
        raise Unreachable(
            "%s: port %d is not answering - the TV must be ON, and pairing only "
            "works from a host on the TV's own subnet" % (ip, WS_PORT))

    channel = RemoteChannel(ip, client_name, token=None, timeout=wait_seconds)
    try:
        try:
            channel.open(wait_seconds=wait_seconds)
        except NotPaired as exc:
            if channel.last_event == "ms.channel.timeOut":
                raise TimeoutError(
                    "%s: no ALLOW within %.0fs" % (ip, wait_seconds)) from exc
            raise
        issued = (channel.issued_token or "").strip()
    finally:
        channel.close()

    if not issued:
        raise TimeoutError(
            "%s: the channel opened but the TV issued no token - press ALLOW on "
            "the TV screen and try again" % ip)
    log.info("%s: paired under client name %r", ip, client_name)
    return issued


def verify_by_effect(
    ip: str, client_name: str, token: str, timeout: float = 10.0
) -> tuple[bool, str]:
    """The ONLY accepted proof that a token really works.

    Returns ``(ok, how)`` where how is "upnp" | "drain" | "rejected" |
    "unreachable" | "no-change".

    Why by effect: with a bad token the TV completes the handshake and then
    answers every command with ``ms.error: No Authorized``, so "connected" proves
    nothing. Where :9197 is open we nudge the volume and READ IT BACK over UPnP
    (which needs no pairing), then put it back. Where it is closed we fall back
    to sending two keys and draining for that error.
    """
    try:
        if upnp_available(ip):
            before = upnp_get_volume(ip)
            if before is not None:
                up, down = "KEY_VOLUP", "KEY_VOLDOWN"
                if before >= 99:
                    # A set pinned at maximum cannot go up, which would read as
                    # a false failure. Nudge down and restore instead.
                    up, down = down, up
                # verify=True here so a refused token reports "rejected" rather
                # than the vaguer "no-change".
                send_keys(ip, client_name, token, [up], timeout=timeout, verify=True)
                time.sleep(1.2)
                after = upnp_get_volume(ip)
                if after is not None:
                    if after != before:
                        # Put it back; nobody asked us to change the volume.
                        send_keys(ip, client_name, token, [down],
                                  timeout=timeout, verify=False)
                        return True, "upnp"
                    return False, "no-change"
                # UPnP went quiet mid-check: fall through to the drain proof
                # rather than reporting a failure we did not actually observe.
                log.debug("%s: UPnP went quiet mid-verify - falling back to drain", ip)

        channel = RemoteChannel(ip, client_name, token, timeout=timeout).open()
        try:
            channel.drain_for_auth_error(0.6)
            channel.click("KEY_VOLUP")
            time.sleep(DEFAULT_KEY_GAP)
            channel.click("KEY_VOLDOWN")
            channel.drain_for_auth_error(2.0)
        finally:
            channel.close()
        return True, "drain"
    except NotPaired:
        return False, "rejected"
    except (Unreachable, TimeoutError, OSError):
        return False, "unreachable"
    except Exception as exc:
        # Verification must always return a verdict, and a surprise here is not
        # evidence that the token is good.
        log.debug("%s: verify_by_effect surprise (%s: %s)", ip, type(exc).__name__, exc)
        return False, "unreachable"


# --------------------------------------------------------------------------- #
# wall-clock bounding
# --------------------------------------------------------------------------- #


def run_bounded(fn: Callable[[], Any], bound: float, name: str) -> tuple[bool, Any]:
    """Run ``fn`` on a daemon thread and give up after ``bound`` seconds.

    Returns ``(completed, value)``. An unfinished thread is ABANDONED - never
    joined again - because the thing this exists for (the Frame art channel) can
    block far past its own socket timeout, and joining it a second time would
    only move the stall somewhere else.

    MUST NOT be called while holding a per-TV lock. Doing that once wedged a TV
    permanently and turned a 14-TV group command into 25 minutes.
    """
    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    thread.join(max(0.1, float(bound)))

    if thread.is_alive():
        log.warning(
            "%s: still running after %.1fs - abandoning the thread (it is never "
            "joined again)", name, bound)
        return (False, None)
    if "error" in box:
        log.debug("%s: failed (%s: %s)", name, type(box["error"]).__name__, box["error"])
        return (True, None)
    return (True, box.get("value"))


# --------------------------------------------------------------------------- #
# the art channel (Frames only)
# --------------------------------------------------------------------------- #


class _ArtChannel(RemoteChannel):
    """The art-app channel: same host, same port, same handshake rules.

    Private because it must only ever be driven from inside ``run_bounded`` -
    see ``art_get_mode`` / ``art_set_mode``.
    """

    CHANNEL = ART_CHANNEL

    def art_request(self, request: str, **extra: Any) -> str:
        """Emit one art request and return the id to match its reply against.

        The payload is doubly encoded on purpose: ``params.data`` is a JSON
        STRING containing the real request object, which is how the art app
        expects it.
        """
        req_id = str(uuid.uuid4())
        inner: dict = {"request": request, "id": req_id}
        inner.update(extra)
        self._send({"method": "ms.channel.emit", "params": {
            "event": "art_app_request", "to": "host",
            "data": json.dumps(inner)}}, "art:%s" % request)
        return req_id

    def art_read_value(self, req_id: str | None, seconds: float) -> str | None:
        """Read until an art reply carries a value of "on"/"off". None if not.

        Matching is on the VALUE, not the event name: the inner ``event`` differs
        by firmware ("art_mode_status", "getting_art_mode_status",
        "art_mode_changed"), and an unsolicited change notification carries no id
        at all. A reply whose id belongs to a different request is skipped.
        """
        ws = self._require()
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ws.settimeout(max(0.2, min(1.0, remaining)))
            except Exception:
                pass
            try:
                raw = ws.recv()
            except (websocket.WebSocketTimeoutException, socket.timeout):
                continue
            except Exception as exc:
                log.debug("%s: art read ended (%s: %s)", self.ip, type(exc).__name__, exc)
                return None
            if not raw:
                return None
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("event") != "d2d_service_message":
                continue
            data = msg.get("data")
            if isinstance(data, str):
                try:
                    inner = json.loads(data)
                except ValueError:
                    continue
            elif isinstance(data, dict):
                inner = data      # tolerated: some firmware skips the encoding
            else:
                continue
            if not isinstance(inner, dict):
                continue
            value = str(inner.get("value") or "").strip().lower()
            if value not in ("on", "off"):
                continue
            got_id = inner.get("id")
            if req_id and got_id and str(got_id) != req_id:
                continue
            return value


def _art_get_blocking(
    ip: str, client_name: str, token: str | None, timeout: float
) -> str | None:
    """One get_artmode_status round trip. Never raises - None means unknown."""
    channel = _ArtChannel(ip, client_name, token, timeout=timeout)
    try:
        channel.open()
        req_id = channel.art_request("get_artmode_status")
        return channel.art_read_value(req_id, timeout)
    except Exception as exc:
        log.debug("%s: art get failed (%s: %s)", ip, type(exc).__name__, exc)
        return None
    finally:
        channel.close()


def _art_set_blocking(
    ip: str, client_name: str, token: str | None, on: bool, timeout: float
) -> bool | None:
    """Set art mode and CONFIRM by reading it back. None means unknown."""
    want = "on" if on else "off"
    channel = _ArtChannel(ip, client_name, token, timeout=timeout)
    deadline = time.monotonic() + max(1.0, timeout)
    seen: str | None = None
    try:
        channel.open()
        channel.art_request("set_artmode_status", value=want)
        # The set is effectively fire-and-forget: some firmware acknowledges
        # nothing at all, so the only proof is reading the mode back.
        while time.monotonic() < deadline:
            req_id = channel.art_request("get_artmode_status")
            got = channel.art_read_value(
                req_id, min(3.0, max(0.5, deadline - time.monotonic())))
            if got == want:
                return True
            if got is not None:
                seen = got
            time.sleep(0.5)
        return False if seen is not None else None
    except Exception as exc:
        log.debug("%s: art set failed (%s: %s)", ip, type(exc).__name__, exc)
        # Entering art mode can kill the very channel we were confirming on, so
        # one fresh read decides it instead of reporting a false unknown.
        left = deadline - time.monotonic()
        if left > 1.0:
            if _art_get_blocking(ip, client_name, token, min(timeout, left)) == want:
                return True
        return None
    finally:
        channel.close()


def art_get_mode(
    ip: str,
    client_name: str,
    token: str | None,
    timeout: float = 8.0,
    bound: float = 12.0,
) -> str | None:
    """"on" | "off" | None (unknown) for a Frame's art mode.

    Always bounded by wall clock, because this channel can block far past its
    socket timeout. Raises ArtHung when that happens, so the caller can mark the
    TV's art channel dead and stop retrying (contract 11.2 / I6); every other
    failure is a quiet None.
    """
    done, value = run_bounded(
        lambda: _art_get_blocking(ip, client_name, token, timeout),
        bound, "art-get-%s" % ip)
    if not done:
        raise ArtHung(
            "%s: the art channel did not answer within %.0fs and was abandoned"
            % (ip, bound))
    return value if value in ("on", "off") else None


def art_set_mode(
    ip: str,
    client_name: str,
    token: str | None,
    on: bool,
    timeout: float = 8.0,
    bound: float = 20.0,
) -> bool | None:
    """Turn art mode on/off explicitly and confirm it. True / False / None.

    Explicit, never the power key: on a Frame the power key TOGGLES art mode and
    PowerState reads "on" in both states, so a toggle plus a PowerState check is
    a coin flip reported as a fact.

    Raises ArtHung if the channel wedged; see ``art_get_mode``.
    """
    done, value = run_bounded(
        lambda: _art_set_blocking(ip, client_name, token, on, timeout),
        bound, "art-set-%s" % ip)
    if not done:
        raise ArtHung(
            "%s: the art channel did not answer within %.0fs and was abandoned"
            % (ip, bound))
    if value is None:
        return None
    return bool(value)


# --------------------------------------------------------------------------- #
# synonym aliases - DO NOT DELETE
# --------------------------------------------------------------------------- #
#
# The names above are this module's frozen public API and are what new code
# should use. The aliases below are additional names for the SAME objects,
# because the frozen contract pins the wire protocol and the behaviour of every
# call but does not spell out each of these symbol names - so a sibling module,
# written in parallel, resolves them by trying a list of plausible synonyms and
# quietly disables the feature when none match.
#
# Without these, an assembled build silently loses Frame art mode, volume/mute,
# the browser close-and-relaunch rung of the show ladder and the interactive
# socket reuse - each one degrading to "unavailable in this build" rather than to
# an error anybody would notice. They cost nothing and they are not dead code:
# they are the seam between two modules written at the same time.
ControlChannel = RemoteChannel     #: interactive socket reuse (contract 7.9 / I18)
get_art_mode = art_get_mode
set_art_mode = art_set_mode
get_volume = upnp_get_volume
set_volume = upnp_set_volume
get_mute = upnp_get_mute
set_mute = upnp_set_mute
launch_app = rest_app_launch
close_app = rest_app_close
