"""tvhub.fleet - the product logic.

One :class:`Tv` per configured display, wrapping ``samsung.py``'s wire protocols
with the workarounds that were measured against real hardware, plus the
:class:`Fleet` that owns the roster and drives it.

Almost every unobvious choice in this file exists because the obvious one was
tried on real Samsung Tizen sets and did not work. Those choices are commented
with WHY. A future maintainer must not "simplify" them away:

  * A power command is judged by the resulting PowerState over REST, never by
    whether the WebSocket conversation ended tidily (the TV tears the channel
    down mid-command, so exceptions there are normal and meaningless).
  * ``KEY_POWEROFF`` is never sent by any path - it is silently ignored by some
    firmware. ``KEY_POWER`` works but is a TOGGLE, so it is never sent to a set
    that Wake-on-LAN already woke.
  * A Frame TV does not power off. PowerState reads "on" in both states, so its
    "off" is Art Mode, set explicitly through the art API and confirmed over
    DIAL when the art channel is unusable.
  * The art channel can block far past its socket timeout, so it always runs
    bounded and detached, and never while holding the per-TV lock.
  * "Playing" means the TV RECENTLY ASKED US FOR THE PAGE. "Browser running" is
    not enough: Tizen freezes a backgrounded page's JS timers while still
    reporting the app as running.
  * The TV cannot be told to navigate to a URL. Each TV's browser homepage is
    set once by hand to one shared address, and switching playlists repoints
    what that address serves.

Dependency direction (contract 0.5): this module imports ``store`` and
``samsung`` only. ``Slideshow`` arrives by injection so playlist pointers can be
resolved without importing it.
"""

from __future__ import annotations

import inspect
import ipaddress
import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import samsung
from . import store

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from .samsung import DeviceInfo
    from .slideshow import Slideshow
    from .store import Context, JobHandle

log = logging.getLogger("tvhub")

__all__ = ["Result", "Tv", "Fleet", "VERBS", "HEAL_VERBS", "STATES", "explain"]


# --------------------------------------------------------------------------- #
# 1. Vocabulary (contract 2.x)
# --------------------------------------------------------------------------- #

VERBS: Tuple[str, ...] = (
    "on", "off", "toggle", "wake", "status", "show", "stop", "reopen",
    "fullscreen", "key", "keys", "macro", "app", "volume", "mute", "pair",
    "verify",
)

#: Self-healing is triggered by a WHITELIST of verbs. A blacklist was tried and
#: let an ordinary keypress trigger a heal, which drove TVs in circles.
HEAL_VERBS: frozenset = frozenset({"on", "show"})

#: In the exact order Fleet.status tests them (contract 7.6).
STATES: Tuple[str, ...] = ("busy", "offline", "standby", "art", "closed", "idle", "playing")

#: Dashboard order: whatever needs attention first (contract 9.6). "art" is not
#: in the contract's list; it sits with "standby" because it is a Frame's off
#: state and needs no attention.
_STATE_ORDER: Tuple[str, ...] = ("busy", "offline", "idle", "closed", "standby", "art", "playing")

#: Verbs that cannot act without an argument.
_ARG_REQUIRED: frozenset = frozenset({"key", "keys", "macro", "app", "volume", "mute"})

# Shared vocabulary lives in store.py; fall back to the contract literals so
# this module is usable even if store has not landed its copy yet.
RESERVED_NAMES = getattr(store, "RESERVED_NAMES", None) or frozenset({
    "all", "tv", "group", "api", "ui", "slideshow", "x", "health",
    "playlist", "playlists", "identify", "reload", "homepages",
})
ALIAS_RE = getattr(store, "ALIAS_RE", None) or re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
GROUP_RE = getattr(store, "GROUP_RE", None) or ALIAS_RE
_KEY_RE = re.compile(r"^KEY_[A-Z0-9_]+$")
_APP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

NOT_PAIRED_TEXT = (
    "not paired - the TV rejected our token; pair it from the web interface and "
    "press ALLOW on that screen"
)
SMART_HUB_TEXT = (
    "Smart Hub is not signed in on this TV - until it is, the TV reports no apps "
    "at all and nothing can be launched."
)
_SUBNET_TEXT = (
    "Wake-on-LAN does not route between subnets - this host must be on the TV's "
    "subnet"
)

#: The hard defaults from contract 3.4 / DEFAULT_CONFIG, used as the last step of
#: Tv.opt's resolution ladder.
_HARD_DEFAULTS: Dict[str, Any] = {
    # per-TV options
    "interval_seconds": 10,
    "fit": "contain",
    "base_url": "",
    "browser_app_id": None,
    "open_with": "auto",
    "open_macro": [],
    "exit_macro": [],
    "fullscreen_key": "KEY_ENTER",
    "wake_delay_seconds": 8,
    "launch_wait_seconds": 30,
    "power_off_mode": "auto",
    "frame": None,
    # inherited from server / slideshow / healing
    "client_name": "TVHub",
    "http_port": 8899,
    "ws_timeout": 10.0,
    "shared_homepage": True,
    "default_playlist": "default",
    "heartbeat_fresh_seconds": 90,
    "status_refresh_seconds": 20,
    "auto_heal": True,
    "auto_heal_minutes": 10,
}

# Exceptions come from samsung.py; local stand-ins keep import-time safety.
NotPaired = getattr(samsung, "NotPaired", None) or type("NotPaired", (Exception,), {})
Unreachable = getattr(samsung, "Unreachable", None) or type("Unreachable", (Exception,), {})
#: samsung raises this when the art channel wedges; it means the same as a
#: run_bounded timeout - abandon the channel and blacklist it (I6).
ArtHung = getattr(samsung, "ArtHung", None) or type("ArtHung", (Exception,), {})

#: Browser app ids vary by firmware year. samsung.py owns the list; this is only
#: a fallback for the launch ladder.
BROWSER_APP_IDS: Tuple[str, ...] = tuple(
    getattr(samsung, "BROWSER_APP_IDS", None)
    or ("org.tizen.browser", "3202010022079", "3201907018784")
)


# --------------------------------------------------------------------------- #
# 2. Cross-module calling
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=512)
def _params_of(func: Any) -> Optional[Tuple[Tuple[str, bool], ...]]:
    """``((name, has_default), ...)`` for func's ordinary parameters, or None."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    out: List[Tuple[str, bool]] = []
    for name, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.kind is p.POSITIONAL_ONLY:
            return None  # cannot be filled by name; caller must pass its own order
        out.append((name, p.default is not inspect.Parameter.empty))
    return tuple(out)


def _invoke(_target: Callable[..., Any], /, **pool: Any) -> Any:
    """Call ``_target``, filling its parameters BY NAME from ``pool``.

    The target is POSITIONAL-ONLY (the ``/``). It has to be: a pool naturally
    carries synonyms like ``fn``/``func``/``name``, and an ordinary parameter
    here would collide with them - which it did, silently turning every Frame's
    first art call into "the art channel hung" and blacklisting it for good.

    WHY this indirection: the seven modules of this build are written in
    parallel against one frozen contract. The contract pins the wire protocol
    and the symbol names it states outright, but not every parameter name inside
    samsung.py. Filling by name from a generous pool of synonyms means a sibling
    that calls its argument ``name`` rather than ``client_name`` is still driven
    correctly, and an unexpected signature costs one explainable ERROR line
    instead of taking the service down.
    """
    params = _params_of(_target)
    if params is None:
        return _target(**pool)
    kwargs: Dict[str, Any] = {}
    for name, has_default in params:
        if name in pool:
            kwargs[name] = pool[name]
        elif not has_default:
            # A required parameter with no synonym we know. Pass None so the
            # failure happens inside the callee, where it can be explained,
            # rather than as a TypeError here.
            log.debug("_invoke: %s wants unknown parameter %r",
                      getattr(_target, "__name__", _target), name)
            kwargs[name] = None
    return _target(**kwargs)


def _first_attr(mod: Any, names: Sequence[str]) -> Any:
    for n in names:
        f = getattr(mod, n, None)
        if f is not None:
            return f
    return None


class _Sam:
    """Resolved samsung.py entry points (see :func:`_invoke` for the why)."""

    def __init__(self, mod: Any) -> None:
        self.device_info = _first_attr(mod, ("device_info", "get_device_info", "rest_device_info"))
        self.send_keys = _first_attr(mod, ("send_keys", "send_key_sequence"))
        self.remote = _first_attr(mod, ("RemoteChannel", "Remote", "RemoteControl",
                                        "ControlChannel", "open_control", "control_channel"))
        self.launch_url = _first_attr(mod, ("launch_url", "launch_browser_url", "open_url"))
        self.app_info = _first_attr(mod, ("app_installed", "app_info", "app_status", "get_app"))
        self.launch_app = _first_attr(mod, ("rest_app_launch", "launch_app", "start_app",
                                            "app_launch", "run_app"))
        self.close_app = _first_attr(mod, ("rest_app_close", "close_app", "stop_app",
                                           "app_close", "kill_app"))
        self.probe_browser = _first_attr(mod, ("probe_browser_app_id",))
        self.dial = _first_attr(mod, ("dial_browser_state", "dial_state", "browser_state", "dial"))
        self.wol = _first_attr(mod, ("wake_on_lan", "wol", "send_wol"))
        self.upnp_available = _first_attr(mod, ("upnp_available",))
        self.get_volume = _first_attr(mod, ("upnp_get_volume", "get_volume"))
        self.set_volume = _first_attr(mod, ("upnp_set_volume", "set_volume"))
        self.get_mute = _first_attr(mod, ("upnp_get_mute", "get_mute"))
        self.set_mute = _first_attr(mod, ("upnp_set_mute", "set_mute"))
        self.verify_by_effect = _first_attr(mod, ("verify_by_effect",))
        self.wait_control_port = _first_attr(mod, ("wait_control_port",))
        self.request_pairing = _first_attr(mod, ("request_pairing",))
        self.run_bounded = _first_attr(mod, ("run_bounded",))
        self.get_art = _first_attr(mod, ("art_get_mode", "get_art_mode",
                                         "get_artmode_status", "artmode_status"))
        self.set_art = _first_attr(mod, ("art_set_mode", "set_art_mode",
                                         "set_artmode_status", "set_artmode"))
        self.local_ip_toward = _first_attr(mod, ("local_ip_toward",))
        self.parse_keys = _first_attr(mod, ("parse_key_sequence",))
        self.normalize_key = _first_attr(mod, ("normalize_key",))
        self.sequence_duration = _first_attr(mod, ("sequence_duration",))


SAM = _Sam(samsung)

# local_ip_toward / normalize_mac / parse_key_sequence are shared vocabulary and
# may live in store or samsung depending on which sibling claimed them.
_STORE_LOCAL_IP = _first_attr(store, ("local_ip_toward",))
_STORE_MAC = _first_attr(store, ("normalize_mac", "normalise_mac"))
_STORE_IPS = _first_attr(store, ("local_ipv4_addresses",))
#: The key grammar is shared vocabulary; whichever sibling owns it wins over our
#: fallback copy, so there is exactly one grammar in the process (contract 2.2).
_KEYS_IMPL = SAM.parse_keys or _first_attr(store, ("parse_key_sequence",))
_NORMKEY_IMPL = SAM.normalize_key or _first_attr(store, ("normalize_key",))


def explain(exc: BaseException) -> str:
    """Human-readable cause for a Result (contract 7.12)."""
    if isinstance(exc, NotPaired):
        return NOT_PAIRED_TEXT
    if isinstance(exc, (Unreachable, socket.timeout, TimeoutError, ConnectionError, OSError)):
        return "%s: TV unreachable or in standby" % type(exc).__name__
    msg = str(exc).strip()
    return "%s: %s" % (type(exc).__name__, msg) if msg else type(exc).__name__


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a dataclass-ish or dict-ish result."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        got = obj.get(name, default)
    else:
        got = getattr(obj, name, default)
    return default if got is None else got


def local_ipv4_addresses() -> List[str]:
    """This host's non-loopback IPv4 addresses, best effort."""
    if _STORE_IPS is not None:
        try:
            got = [str(a) for a in _STORE_IPS()]
            if got:
                return got
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("store.local_ipv4_addresses failed: %s", exc)
    out: List[str] = []
    try:
        _host, _alias, addrs = socket.gethostbyname_ex(socket.gethostname())
        out = [a for a in addrs if not str(a).startswith("127.")]
    except OSError as exc:  # pragma: no cover - defensive
        log.debug("gethostbyname_ex failed: %s", exc)
    return out


def local_ip_toward(ip: str) -> str:
    """Which of our addresses faces ``ip``.

    No address is hard-coded here: the probe target is the TV we are asking
    about (a UDP connect sends nothing, it just asks the OS which interface
    would carry the traffic). With no TV to aim at, fall back to enumerating our
    own addresses rather than inventing a network.
    """
    for f in (_STORE_LOCAL_IP, SAM.local_ip_toward):
        if f is not None:
            try:
                got = f(ip)
                if got:
                    return str(got)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("local_ip_toward(%s) failed: %s", ip, exc)
    if ip:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((ip, 9))
            return str(s.getsockname()[0])
        except OSError as exc:
            log.debug("route probe toward %s failed: %s", ip, exc)
        finally:
            s.close()
    mine = local_ipv4_addresses()
    return mine[0] if mine else "127.0.0.1"


def normalize_mac(mac: str) -> str:
    """Lower-case colon form, or "" when it will not normalize."""
    if _STORE_MAC is not None:
        try:
            return str(_STORE_MAC(mac) or "")
        except Exception:
            return ""
    hexes = re.sub(r"[^0-9a-fA-F]", "", str(mac or ""))
    if len(hexes) != 12:
        return ""
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2)).lower()


def normalize_key(name: str) -> Optional[str]:
    """``volup`` -> ``KEY_VOLUP``; None when it is not a legal key name."""
    if _NORMKEY_IMPL is not None:
        try:
            got = _NORMKEY_IMPL(str(name or ""))
            return str(got) if got else None
        except (ValueError, TypeError) as exc:
            # A refused key (KEY_POWEROFF is refused centrally) or a bad name.
            log.debug("normalize_key(%r) refused: %s", name, exc)
            return None
    raw = str(name or "").strip().replace("-", "_").replace(" ", "_")
    if not raw:
        return None
    up = raw.upper()
    if not up.startswith("KEY_"):
        up = "KEY_" + up
    return up if _KEY_RE.match(up) else None


def parse_key_sequence(spec: Any) -> List[str]:
    """One key grammar everywhere (contract 2.2).

    ``KEY_LEFT*3`` expands to three entries; ``@500`` is kept as a wait token.
    Raises ValueError on a bad key name so the HTTP layer can answer 400.
    """
    if _KEYS_IMPL is not None:
        try:
            return list(_KEYS_IMPL(spec))
        except ValueError:
            raise
        except Exception as exc:  # pragma: no cover - fall through to our copy
            log.debug("shared parse_key_sequence failed on %r (%s); using local copy", spec, exc)
    items: List[str] = []
    raw = spec if isinstance(spec, (list, tuple)) else [spec]
    for chunk in raw:
        for tok in re.split(r"[,+]", str(chunk or "")):
            tok = tok.strip()
            if tok:
                items.append(tok)
    out: List[str] = []
    for tok in items:
        if tok.startswith("@"):
            digits = tok[1:].strip()
            if not digits.isdigit():
                raise ValueError("'%s' is not a wait like @500" % tok)
            out.append("@" + str(int(digits)))
            continue
        count = 1
        if "*" in tok:
            base, _, mult = tok.partition("*")
            mult = mult.strip()
            if not mult.isdigit():
                raise ValueError("'%s' is not a repeat like KEY_UP*3" % tok)
            count = max(1, min(50, int(mult)))
            tok = base.strip()
        key = normalize_key(tok)
        if key is None:
            raise ValueError("'%s' is not a valid key name" % tok)
        out.extend([key] * count)
    return out


def _macro_seconds(seq: Sequence[str]) -> float:
    """How long a macro will take, for the Activity countdown."""
    if SAM.sequence_duration is not None:
        try:
            return float(SAM.sequence_duration(list(seq))) + 1.0
        except Exception as exc:  # pragma: no cover - fall through to our copy
            log.debug("sequence_duration failed: %s", exc)
    waits = sum(int(t[1:]) / 1000.0 for t in seq if str(t).startswith("@"))
    return waits + 0.35 * sum(1 for t in seq if not str(t).startswith("@")) + 1.0


# --------------------------------------------------------------------------- #
# 3. Context helpers - Heartbeat / Activity / Jobs / State
# --------------------------------------------------------------------------- #

def _hb(ctx: "Context") -> Any:
    return getattr(ctx, "heartbeat", None)


def _hb_forget(ctx: "Context", ip: str) -> None:
    """Invalidate the heartbeat. MUST happen on every power and art-mode change,
    or a stale record makes a blank TV read as playing (contract I7)."""
    h = _hb(ctx)
    if h is None:
        return
    try:
        h.forget(ip)
    except Exception as exc:  # pragma: no cover
        log.debug("heartbeat.forget(%s) failed: %s", ip, exc)


def _hb_fresh(ctx: "Context", ip: str, within: Optional[float] = None) -> bool:
    h = _hb(ctx)
    if h is None:
        return False
    try:
        if within is None:
            return bool(h.fresh(ip))
        return bool(h.fresh(ip, within))
    except Exception as exc:  # pragma: no cover
        log.debug("heartbeat.fresh(%s) failed: %s", ip, exc)
        return False


def _hb_since(ctx: "Context", ip: str, t0: float) -> bool:
    """Has this TV asked us for the page since ``t0``?"""
    h = _hb(ctx)
    if h is None:
        return False
    try:
        f = getattr(h, "since", None)
        if callable(f):
            return bool(f(ip, t0))
    except Exception as exc:  # pragma: no cover
        log.debug("heartbeat.since(%s) failed: %s", ip, exc)
    age = _hb_age(ctx, ip)
    if age is None:
        return False
    return (time.monotonic() - age) >= t0


def _hb_age(ctx: "Context", ip: str) -> Optional[float]:
    """Seconds since this IP last fetched the page, or None."""
    h = _hb(ctx)
    if h is None:
        return None
    for name in ("age", "age_of", "last_age"):
        f = getattr(h, name, None)
        if callable(f):
            try:
                got = f(ip)
                return None if got is None else float(got)
            except Exception:
                break
    for name in ("last", "last_seen", "last_request"):
        f = getattr(h, name, None)
        if callable(f):
            try:
                got = f(ip)
                return None if got is None else max(0.0, time.monotonic() - float(got))
            except Exception:
                break
    return None


def _activity_set(ctx: "Context", alias: str, text: str, seconds: float) -> None:
    """Publish a bounded wait so a pause is explainable, never mysterious."""
    a = getattr(ctx, "activity", None)
    if a is None:
        return
    try:
        a.set(alias, text, float(seconds))
    except Exception as exc:  # pragma: no cover
        log.debug("activity.set(%s) failed: %s", alias, exc)


def _activity_clear(ctx: "Context", alias: str) -> None:
    a = getattr(ctx, "activity", None)
    if a is None:
        return
    for name in ("clear", "done", "finish"):
        f = getattr(a, name, None)
        if callable(f):
            try:
                f(alias)
                return
            except Exception as exc:  # pragma: no cover
                log.debug("activity.%s(%s) failed: %s", name, alias, exc)
                return


def _activity_get(ctx: "Context", alias: str) -> Tuple[Optional[str], float]:
    a = getattr(ctx, "activity", None)
    if a is None:
        return None, 0.0
    try:
        got = a.get(alias)
    except Exception as exc:  # pragma: no cover
        log.debug("activity.get(%s) failed: %s", alias, exc)
        return None, 0.0
    if got is None:
        return None, 0.0
    if isinstance(got, (tuple, list)):
        text = got[0] if got else None
        left = got[1] if len(got) > 1 else 0.0
        return (str(text) if text else None), float(left or 0.0)
    if isinstance(got, dict):
        text = got.get("text") or got.get("activity")
        left = got.get("left", got.get("seconds_left", 0.0))
        return (str(text) if text else None), float(left or 0.0)
    return (str(got) or None), 0.0


def _state(ctx: "Context") -> Any:
    return getattr(ctx, "state", None)


def _state_data(ctx: "Context") -> Dict[str, Any]:
    st = _state(ctx)
    for name in ("data", "_data"):
        got = getattr(st, name, None)
        if isinstance(got, dict):
            return got
    f = getattr(st, "raw", None)
    if callable(f):
        try:
            got = f()
            if isinstance(got, dict):
                return got
        except Exception:
            pass
    return {}


def _learned(ctx: "Context", alias: str) -> Dict[str, Any]:
    """Learned facts for one TV. A cache, never a requirement (contract 4.4)."""
    st = _state(ctx)
    for name in ("learned_for", "get_learned", "learned_of"):
        f = getattr(st, name, None)
        if callable(f):
            try:
                got = f(alias)
                if isinstance(got, dict):
                    return got
            except Exception:
                break
    got = getattr(st, "learned", None)
    if callable(got):
        try:
            val = got(alias)
            if isinstance(val, dict):
                return val
        except Exception:
            pass
    elif isinstance(got, dict):
        val = got.get(alias)
        if isinstance(val, dict):
            return val
    val = _state_data(ctx).get("learned", {})
    if isinstance(val, dict) and isinstance(val.get(alias), dict):
        return val[alias]
    return {}


@lru_cache(maxsize=256)
def _takes_kwargs(func: Any) -> bool:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values())


def _learn(ctx: "Context", alias: str, key: str, value: Any) -> None:
    """Remember one learned fact.

    Handles both shapes a store may offer: ``learn(alias, **facts)`` (the fact
    arrives as a keyword) and ``learn(alias, key, value)``. Getting this wrong is
    not cosmetic - ``art_hung`` MUST persist, or a Frame whose art channel wedged
    is retried forever, which is the 25-minute group-command failure (4.5 / I6).
    """
    st = _state(ctx)
    for name in ("learn", "set_learned", "remember", "learn_set"):
        f = getattr(st, name, None)
        if not callable(f):
            continue
        try:
            if _takes_kwargs(f):
                f(alias, **{key: value})
            else:
                _invoke(f, alias=alias, key=key, name=key, value=value)
        except Exception as exc:
            log.debug("state.%s(%s, %s) failed: %s", name, alias, key, exc)
        return
    log.debug("no state.learn(); not caching %s=%r for %s", key, value, alias)


def _token_record(ctx: "Context", alias: str) -> Dict[str, Any]:
    st = _state(ctx)
    for name in ("token_record", "token_info", "pairing"):
        f = getattr(st, name, None)
        if callable(f):
            try:
                got = f(alias)
                if isinstance(got, dict):
                    return got
            except Exception:
                break
    rec = _state_data(ctx).get("tokens", {})
    got = rec.get(alias) if isinstance(rec, dict) else None
    return got if isinstance(got, dict) else {}


def _homepage_confirmed(ctx: "Context", alias: str) -> bool:
    st = _state(ctx)
    f = getattr(st, "homepage_confirmed", None)
    if callable(f):
        try:
            return bool(f(alias))
        except Exception:
            pass
    setup = _state_data(ctx).get("setup", {})
    flags = setup.get("homepage_confirmed", {}) if isinstance(setup, dict) else {}
    return bool(flags.get(alias)) if isinstance(flags, dict) else False


def _set_progress(job: Any, name: str, value: Any) -> None:
    attr = getattr(job, name, None)
    if callable(attr):
        attr(value)
    else:
        setattr(job, name, value)


def _progress(job: Any, *, done: Any = None, total: Any = None,
              step: Any = None, line: Any = None) -> None:
    """Report through a JobHandle without caring which shape it has."""
    if job is None:
        return
    try:
        if done is not None or total is not None:
            pair = getattr(job, "progress", None)
            if callable(pair):
                # A handle that exposes progress(done, total) wants both at once,
                # so remember whichever half we were not given this time.
                d = done if done is not None else getattr(job, "_tvhub_done", 0)
                t = total if total is not None else getattr(job, "_tvhub_total", 0)
                try:
                    job._tvhub_done, job._tvhub_total = d, t
                except Exception:  # pragma: no cover - a slotted handle
                    pass
                pair(d, t)
            else:
                if total is not None:
                    _set_progress(job, "total", total)
                if done is not None:
                    _set_progress(job, "done", done)
        if step is not None:
            _set_progress(job, "step", step)
        if line is not None:
            for name in ("log", "line", "add_line", "append", "say"):
                f = getattr(job, name, None)
                if callable(f):
                    f(line)
                    break
    except Exception as exc:  # pragma: no cover
        log.debug("job progress failed: %s", exc)


def _job_id(job: Any) -> Optional[str]:
    got = getattr(job, "id", None)
    return str(got) if got else None


def _run_bounded_local(fn: Callable[[], Any], bound: float, name: str) -> Tuple[bool, Any]:
    """Fallback for samsung.run_bounded: run detached, hard wall-clock bound.

    A thread still alive at the deadline is ABANDONED and never joined again -
    the art channel can block far past its socket timeout, and waiting on it
    once turned a 14-TV group command into 25 minutes.
    """
    box: List[Any] = [None]

    def wrapper() -> None:
        try:
            box[0] = fn()
        except Exception as exc:
            log.debug("%s raised: %s", name, exc)
            box[0] = None

    t = threading.Thread(target=wrapper, name=name, daemon=True)
    t.start()
    t.join(bound)
    if t.is_alive():
        log.warning("%s exceeded its %.0fs bound - abandoning the thread", name, bound)
        return False, None
    return True, box[0]


# --------------------------------------------------------------------------- #
# 4. Result
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    """The single return type of every action (contract 0.8 / 2.5)."""

    ok: bool
    text: str
    level: str = "ok"
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        if self.level == "warn":
            return "WARNING " + self.text
        if self.level == "error":
            return "ERROR " + self.text
        return self.text

    def render(self) -> str:
        """Alias for __str__, so a caller written against either convention works."""
        return str(self)

    def as_json(self) -> dict:
        return {
            "ok": self.ok,
            "text": self.text,
            "level": self.level,
            "detail": dict(self.detail),
            "rendered": str(self),
        }

    @classmethod
    def good(cls, text: str, **detail: Any) -> "Result":
        return cls(True, text, "ok", dict(detail))

    @classmethod
    def warn(cls, text: str, **detail: Any) -> "Result":
        # ok is False for both warn and error (contract 2.5).
        return cls(False, text, "warn", dict(detail))

    @classmethod
    def error(cls, text: str, **detail: Any) -> "Result":
        return cls(False, text, "error", dict(detail))


# --------------------------------------------------------------------------- #
# 5. Tv
# --------------------------------------------------------------------------- #

class Tv:
    """One configured display."""

    def __init__(self, ctx: "Context", alias: str, spec: dict) -> None:
        self.ctx = ctx
        self.alias = alias
        self.spec: Dict[str, Any] = dict(spec or {})
        self.ip: str = str(self.spec.get("ip") or "")
        self.mac: str = normalize_mac(self.spec.get("mac") or "")
        self.label: str = str(self.spec.get("label") or alias)
        self.enabled: bool = bool(self.spec.get("enabled", True))
        #: Held ONLY for a single WS/REST conversation - never across a
        #: heartbeat wait, a long sleep, an art operation or a whole ladder.
        self.lock = threading.RLock()

        self._info_at = 0.0
        self._info: Any = None
        self._conn: Any = None          # the reused interactive control channel
        self._conn_used = 0.0
        #: Fleet injects the real resolver; the standalone default keeps a bare
        #: Tv usable in a test or a one-shot CLI call.
        self.playlist_for: Callable[[str], str] = self._config_playlist

    # -- configuration ---------------------------------------------------- #

    def update_spec(self, spec: dict) -> None:
        """Adopt a reloaded config entry in place, keeping caches where valid."""
        new_ip = str((spec or {}).get("ip") or "")
        if new_ip != self.ip:
            # A different address invalidates the open socket and the REST cache.
            self.drop_sockets()
            self._info = None
            self._info_at = 0.0
        self.spec = dict(spec or {})
        self.ip = new_ip
        self.mac = normalize_mac(self.spec.get("mac") or "")
        self.label = str(self.spec.get("label") or self.alias)
        self.enabled = bool(self.spec.get("enabled", True))

    @property
    def _cfg(self) -> Dict[str, Any]:
        data = getattr(self.ctx.config, "data", None)
        return data if isinstance(data, dict) else {}

    def opt(self, name: str, fallback: Any = None) -> Any:
        """per-TV options.<name> (when not null) -> config section -> hard
        default -> fallback (contract 7.1)."""
        options = self.spec.get("options")
        if isinstance(options, dict) and options.get(name) is not None:
            return options[name]
        cfg = self._cfg
        for section in ("slideshow", "server", "healing", "paths"):
            block = cfg.get(section)
            if isinstance(block, dict) and block.get(name) is not None:
                return block[name]
        if name in _HARD_DEFAULTS:
            got = _HARD_DEFAULTS[name]
            return list(got) if isinstance(got, list) else got
        return fallback

    def _config_playlist(self, alias: str = "") -> str:
        return str(self.opt("default_playlist", "default") or "default")

    @property
    def client_name(self) -> str:
        return str(self.opt("client_name", "TVHub") or "TVHub")

    @property
    def ws_timeout(self) -> float:
        try:
            return float(self.opt("ws_timeout", 10.0) or 10.0)
        except (TypeError, ValueError):
            return 10.0

    @property
    def fresh_seconds(self) -> float:
        try:
            return float(self.opt("heartbeat_fresh_seconds", 90) or 90)
        except (TypeError, ValueError):
            return 90.0

    def describe(self) -> dict:
        """The no-network view of this TV, for GET /api/tvs."""
        rec = _token_record(self.ctx, self.alias)
        return {
            "alias": self.alias,
            "ip": self.ip,
            "mac": self.mac,
            "label": self.label,
            "enabled": self.enabled,
            "options": dict(self.spec.get("options") or {}),
            "paired": self.paired(),
            "verified_how": rec.get("verified_how"),
            "homepage_confirmed": _homepage_confirmed(self.ctx, self.alias),
            "homepage_url": self.homepage_url(),
        }

    # -- identity / URLs -------------------------------------------------- #

    def base_url(self) -> str:
        raw = str(self.opt("base_url", "") or "").strip().rstrip("/")
        if raw:
            return raw
        # Nothing configured: guess the interface facing this TV. TESTING ONLY
        # (contract 3.1) - this string is what a human types into the TV, so it
        # must be a reserved address before any homepage is set.
        port = self.opt("http_port", 8899)
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 8899
        return "http://%s:%d" % (local_ip_toward(self.ip), port)

    def homepage_url(self) -> str:
        """The ONE address this TV's browser homepage is set to, by hand, once.

        It never changes: switching playlists repoints what it serves. That is
        the whole architecture - many firmwares accept "launch the browser at
        this URL" and then ignore the URL (contract I8).
        """
        if bool(self.opt("shared_homepage", True)):
            return self.base_url() + "/slideshow/live/all"
        return self.base_url() + "/slideshow/live/" + quote(self.alias, safe="")

    def slideshow_url(self, playlist_hint: str | None = None) -> str:
        if playlist_hint:
            return self.base_url() + "/slideshow/p/" + quote(str(playlist_hint), safe="")
        return self.homepage_url()

    # -- REST facts ------------------------------------------------------- #

    def info(self, max_age: float = 5.0) -> "DeviceInfo":
        """Cached REST device info. The ONLY source of truth for power."""
        now = time.monotonic()
        cached, at = self._info, self._info_at
        if cached is not None and max_age > 0 and (now - at) < max_age:
            return cached
        got = None
        if SAM.device_info is not None:
            try:
                # No lock: this is a plain 4 s HTTP GET, and a whole-fleet sweep
                # must not queue behind one busy TV's ladder.
                got = _invoke(SAM.device_info, ip=self.ip, host=self.ip,
                              address=self.ip, timeout=4.0)
            except Exception as exc:
                log.debug("%s: device_info failed: %s", self.alias, exc)
        if got is None:
            got = cached if cached is not None else {"power": "unreachable"}
            if cached is None:
                self._info, self._info_at = got, now
            return got
        self._info, self._info_at = got, now
        return got

    def power_state(self, fresh: bool = False) -> str:
        """"on" / "standby" / "unreachable", from REST only.

        Never judged from whether a WebSocket conversation ended tidily: the TV
        tears the channel down mid-command as it changes power state, so
        exceptions there are normal and say nothing (contract I3).
        """
        return str(_field(self.info(0.0 if fresh else 5.0), "power", "unreachable"))

    def is_frame(self) -> bool:
        """options.frame -> learned -> REST FrameTVSupport (contract 7.2)."""
        forced = (self.spec.get("options") or {}).get("frame")
        if forced is not None:
            return bool(forced)
        learned = _learned(self.ctx, self.alias)
        if "is_frame" in learned:
            return bool(learned.get("is_frame"))
        info = self.info()
        if str(_field(info, "power", "unreachable")) == "unreachable":
            # An unreachable probe proves nothing; cache nothing.
            return False
        frame = bool(_field(info, "is_frame", False))
        _learn(self.ctx, self.alias, "is_frame", frame)
        return frame

    def smart_hub(self) -> Optional[bool]:
        got = _field(self.info(), "smart_hub", None)
        return None if got is None else bool(got)

    def token(self) -> str | None:
        st = _state(self.ctx)
        if st is None:
            return None
        try:
            got = st.token(self.alias, self.client_name)
        except TypeError:
            try:
                got = st.token(self.alias)
            except Exception:
                return None
        except Exception:
            return None
        return str(got) if got else None

    def paired(self) -> bool:
        """A stored token is NOT proof of pairing; is_paired also requires a
        successful verify-by-effect (contract I1)."""
        st = _state(self.ctx)
        if st is None:
            return False
        f = getattr(st, "is_paired", None)
        if callable(f):
            try:
                return bool(_invoke(f, alias=self.alias, client_name=self.client_name,
                                    name=self.client_name))
            except Exception as exc:
                log.debug("%s: is_paired failed: %s", self.alias, exc)
        rec = _token_record(self.ctx, self.alias)
        return bool(rec.get("token")) and rec.get("verified_at") is not None \
            and rec.get("client_name") in (None, self.client_name)

    def browser_state(self) -> str:
        """"running" / "stopped" / "unknown" over DIAL.

        Plain HTTP, needs no pairing, and cannot hang - which is why it, and not
        the art channel, is the liveness proxy for "is the browser up", and how
        a Frame's art mode is detected (entering art mode closes the browser).
        """
        if SAM.dial is None:
            return "unknown"
        try:
            got = _invoke(SAM.dial, ip=self.ip, host=self.ip, address=self.ip, timeout=4.0)
        except Exception as exc:
            log.debug("%s: DIAL failed: %s", self.alias, exc)
            return "unknown"
        got = str(got or "unknown").strip().lower()
        return got if got in ("running", "stopped") else "unknown"

    def browser_app_id(self) -> str | None:
        """The browser app id this TV actually reports, or None.

        Only POSITIVE probes are cached: some sets answer 401/404 on this
        endpoint until they are paired, and caching that as "no browser" would
        stick for the life of the service (contract I10).
        """
        forced = (self.spec.get("options") or {}).get("browser_app_id")
        if forced:
            return str(forced)
        learned = _learned(self.ctx, self.alias).get("browser_app_id")
        if learned:
            return str(learned)
        if not self.paired() or SAM.probe_browser is None:
            # Probing before pairing only produces answers we must not cache.
            return None
        try:
            got = _invoke(SAM.probe_browser, ip=self.ip, host=self.ip,
                          client_name=self.client_name, name=self.client_name,
                          token=self.token(), extra=None, timeout=4.0)
        except Exception as exc:
            log.debug("%s: browser probe failed: %s", self.alias, exc)
            return None
        if got:
            _learn(self.ctx, self.alias, "browser_app_id", str(got))
            return str(got)
        return None

    # -- waiting, explained ------------------------------------------------ #

    def _activity(self, text: str, seconds: float) -> None:
        _activity_set(self.ctx, self.alias, text, seconds)

    def _clear_activity(self) -> None:
        _activity_clear(self.ctx, self.alias)

    def _wait(self, seconds: float, text: str) -> None:
        """A bounded, published pause (contract 5.2 / I17)."""
        if seconds <= 0:
            return
        self._activity(text, seconds)
        try:
            end = time.monotonic() + seconds
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    return
                time.sleep(min(0.5, left))
        finally:
            self._clear_activity()

    def _poll_power(self, wanted: str, seconds: float, text: str,
                    every: float = 2.0) -> Optional[float]:
        """Poll PowerState until it reads ``wanted``. Returns elapsed or None."""
        self._activity(text, seconds)
        start = time.monotonic()
        try:
            end = start + seconds
            while True:
                if self.power_state(fresh=True) == wanted:
                    return time.monotonic() - start
                left = end - time.monotonic()
                if left <= 0:
                    return None
                time.sleep(min(every, left))
        finally:
            self._clear_activity()

    def _poll_dial(self, wanted: str, seconds: float, text: str,
                   every: float = 1.5) -> bool:
        self._activity(text, seconds)
        try:
            end = time.monotonic() + seconds
            while True:
                if self.browser_state() == wanted:
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return False
                time.sleep(min(every, left))
        finally:
            self._clear_activity()

    def _landed(self, t0: float, wait: float,
                text: str = "waiting for the TV to load the page") -> bool:
        """Has the TV ASKED US for the page since t0?

        The only proof a TV is displaying the slideshow. A launch is
        acknowledged even when the firmware then drops it, and "browser running"
        is not enough because Tizen freezes a backgrounded page's JS timers
        while still reporting the app as running (contract I7).
        """
        self._activity(text, wait)
        try:
            end = time.monotonic() + wait
            while True:
                if _hb_since(self.ctx, self.ip, t0):
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return _hb_since(self.ctx, self.ip, t0)
                time.sleep(min(0.5, left))
        finally:
            self._clear_activity()

    def _wait_control_port(self, seconds: float = 20.0) -> bool:
        """Standby and art mode CLOSE port 8002, so a key sent immediately after
        leaving either is refused and silently lost (contract 6.15)."""
        if SAM.wait_control_port is None:
            self._wait(min(seconds, 3.0), "waiting for the TV to accept commands")
            return True
        self._activity("waiting for the TV to accept commands", seconds)
        try:
            return bool(_invoke(SAM.wait_control_port, ip=self.ip, host=self.ip,
                                address=self.ip, seconds=seconds, timeout=seconds))
        except Exception as exc:
            log.debug("%s: wait_control_port failed: %s", self.alias, exc)
            return False
        finally:
            self._clear_activity()

    # -- art mode (Frames only) -------------------------------------------- #

    def _art_dead(self) -> bool:
        """A Frame whose art channel wedged once is never retried - not even
        after a restart. Cleared only by an explicit verify (contract 4.5)."""
        return bool(_learned(self.ctx, self.alias).get("art_hung"))

    def _mark_art_dead(self) -> None:
        log.warning("%s: art channel hung - marking it dead until an explicit verify",
                    self.alias)
        _learn(self.ctx, self.alias, "art_hung", True)

    def _bounded(self, fn: Callable[[], Any], bound: float, name: str) -> Tuple[bool, Any]:
        """Run an art operation detached with a hard wall-clock bound.

        NEVER called while holding self.lock: doing that wedged a TV permanently
        and turned a 14-TV group command into 25 minutes (contract I6).
        """
        runner = SAM.run_bounded or _run_bounded_local
        try:
            got = _invoke(runner, fn=fn, bound=bound, seconds=bound,
                          timeout=bound, name=name)
        except ArtHung as exc:
            # samsung tells us directly that the channel wedged.
            log.warning("%s: %s", name, exc)
            return False, None
        except Exception as exc:
            log.debug("%s: bounded call failed: %s", name, exc)
            return False, None
        if isinstance(got, tuple) and len(got) == 2:
            return bool(got[0]), got[1]
        return True, got

    def _art_pool(self, bound: float) -> Dict[str, Any]:
        # `bound` is passed through as well: an art helper that bounds itself
        # should use the same wall clock we are enforcing from the outside,
        # rather than a longer default that would make our bound the only one.
        return {
            "ip": self.ip, "host": self.ip, "address": self.ip,
            "client_name": self.client_name, "name": self.client_name,
            "token": self.token(), "timeout": self.ws_timeout, "bound": bound,
        }

    def art_mode(self) -> str | None:
        """"on" / "off", or None when unknown. Never raises."""
        if not self.is_frame() or self._art_dead() or SAM.get_art is None:
            return None
        getter = SAM.get_art
        bound = max(8.0, self.ws_timeout)
        pool = self._art_pool(bound)
        ok, got = self._bounded(lambda: _invoke(getter, **pool),
                                bound, "art-get:" + self.alias)
        if not ok:
            self._mark_art_dead()
            return None
        got = str(got).strip().lower() if got is not None else ""
        return got if got in ("on", "off") else None

    def set_art_mode(self, on: bool, seconds: float = 12.0) -> bool | None:
        """Set art mode EXPLICITLY and confirm. True / False / None(unknown).

        An explicit set, never the power key: on a Frame the power key TOGGLES
        art mode, so a "turn art on" that arrives when art is already on turns
        the artwork off (contract 7.4 / 7.5).
        """
        if not self.is_frame() or self._art_dead() or SAM.set_art is None:
            return None
        setter = SAM.set_art
        want = "on" if on else "off"
        pool = self._art_pool(seconds)
        pool.update({"on": bool(on), "value": want, "state": want, "mode": want,
                     "artmode": want, "seconds": seconds})
        self._activity("setting art mode " + want, seconds + 4)
        try:
            ok, got = self._bounded(lambda: _invoke(setter, **pool),
                                    seconds + 12.0, "art-set:" + self.alias)
        finally:
            self._clear_activity()
        if not ok:
            self._mark_art_dead()
            return None
        # An art-mode change invalidates any heartbeat: the page stops polling
        # the moment art mode covers it, so a stale record would read as playing.
        _hb_forget(self.ctx, self.ip)
        if isinstance(got, bool):
            return got
        if got is None:
            return None
        text = str(got).strip().lower()
        if text in ("on", "off"):
            return text == want
        return None

    # -- power ------------------------------------------------------------- #

    def power_on(self) -> Result:
        if self.is_frame():
            return self._frame_on()
        return self._plain_on()

    def _frame_on(self) -> Result:
        """A Frame's "on" means LEAVING Art Mode, not leaving standby."""
        # Already showing the slideshow? Then it is not in art mode, and the art
        # call is both pointless and the slowest possible option.
        if _hb_fresh(self.ctx, self.ip, self.fresh_seconds):
            return Result.good("already on (slideshow on screen)")
        _hb_forget(self.ctx, self.ip)
        if self.power_state(fresh=True) == "unreachable" and self.mac:
            self._wol()
            self._poll_power("on", 25.0, "waiting for Wake-on-LAN")
        got = self.set_art_mode(False)
        if got is True:
            return Result.good("on (art mode off)")
        if got is None:
            return Result.warn("art mode not reachable - carrying on to the slideshow anyway")
        return Result.warn("asked the Frame to leave art mode but it still reports art mode on")

    def _plain_on(self) -> Result:
        if self.power_state(fresh=True) == "on":
            return Result.good("already on")
        _hb_forget(self.ctx, self.ip)

        if self.mac:
            # Wake-on-LAN first, and CONFIRM before touching the remote at all:
            # KEY_POWER is a TOGGLE, so sending it to a set WoL already woke
            # switches it straight back off (contract I2).
            self._wol()
            took = self._poll_power("on", 20.0, "waiting for Wake-on-LAN")
            if took is not None:
                return Result.good("on (confirmed in %.0fs)" % took)
        else:
            log.warning("%s: no MAC configured - cannot try Wake-on-LAN", self.alias)

        # WoL did not do it. The remote works from normal standby, because port
        # 8002 stays open there.
        self._activity("sending the power key", 4.0)
        try:
            self._send_verified(["KEY_POWER"])
        except Exception as exc:
            # Expected while the set is asleep, and meaningless either way.
            log.debug("%s: power key raised (often benign): %s", self.alias, exc)
        finally:
            self._clear_activity()
        took = self._poll_power("on", 20.0, "waiting for power on (after the key)")
        if took is not None:
            return Result.good("on (confirmed in %.0fs)" % took)

        if not self.mac:
            return Result.warn(
                "power-on sent but the TV is still not responding, and no MAC is "
                "configured for this TV so Wake-on-LAN could not be tried")
        return Result.warn("power-on sent but the TV is still not responding. " + _SUBNET_TEXT)

    def _wol(self) -> int:
        """Burst the magic packet. Returns the datagram count."""
        if not self.mac or SAM.wol is None:
            return 0
        self._activity("sending Wake-on-LAN", 4.0)
        try:
            got = _invoke(SAM.wol, mac=self.mac, ip=self.ip, host=self.ip,
                          address=self.ip, bursts=6)
            return int(got or 0)
        except Exception as exc:
            log.debug("%s: Wake-on-LAN failed: %s", self.alias, exc)
            return 0
        finally:
            self._clear_activity()

    def power_off(self) -> Result:
        mode = str(self.opt("power_off_mode", "auto") or "auto").lower()
        frame = self.is_frame()
        if mode == "art" or (mode == "auto" and frame):
            return self._frame_off()
        return self._plain_off(frame)

    def _frame_off(self) -> Result:
        """A Frame's "off" is Art Mode. Its power MUST NOT be judged by
        PowerState, which reads "on" in BOTH states (contract I4)."""
        if self.power_state(fresh=True) == "unreachable":
            return Result.good("already off")
        _hb_forget(self.ctx, self.ip)
        got = self.set_art_mode(True, seconds=8.0)
        if got is True:
            return Result.good("art mode on - a Frame's off state; PowerState stays 'on'")

        # The art channel is unusable or hung on some Frames. Fall back to the
        # power key (which on a Frame toggles art mode) and confirm by watching
        # the browser close over DIAL - never over the art channel.
        if self.browser_state() == "stopped":
            return Result.good("already in art mode")
        try:
            self._send_verified(["KEY_POWER"])
        except Exception as exc:
            return Result.error("could not send the power key: " + explain(exc))
        if self._poll_dial("stopped", 15.0, "waiting for the browser to close"):
            _hb_forget(self.ctx, self.ip)
            return Result.good("art mode on (confirmed by the browser closing)")
        return Result.warn(
            "sent the power key but this Frame's browser is still running, so it "
            "may still be showing the slideshow")

    def _plain_off(self, frame: bool = False) -> Result:
        if not frame and self.power_state(fresh=True) != "on":
            return Result.good("already off")
        _hb_forget(self.ctx, self.ip)
        self._activity("sending the power key", 4.0)
        try:
            # KEY_POWER only. KEY_POWEROFF is silently ignored by some firmware
            # (measured: no effect after 61 s) and must never be sent.
            self._send_verified(["KEY_POWER"])
        except Exception as exc:
            log.debug("%s: power key raised (often benign): %s", self.alias, exc)
        finally:
            self._clear_activity()
        if frame:
            # Asked for the key route on a Frame: PowerState cannot answer, so
            # DIAL does.
            if self._poll_dial("stopped", 15.0, "waiting for the browser to close"):
                _hb_forget(self.ctx, self.ip)
                return Result.good("art mode on (confirmed by the browser closing)")
            return Result.warn("power key sent but this Frame's browser is still running")
        if self._poll_power("standby", 15.0, "waiting for standby") is not None:
            return Result.good("standby (confirmed)")
        return Result.warn("power-off sent but the TV still reports on")

    def toggle(self) -> Result:
        if self.is_frame():
            # A Frame never leaves "on", so toggle ART, judged by the art channel
            # and then DIAL - never by PowerState.
            art = self.art_mode()
            if art == "on":
                return self.power_on()
            if art == "off":
                return self.power_off()
            return self.power_off() if self.browser_state() == "running" else self.power_on()
        return self.power_off() if self.power_state(fresh=True) == "on" else self.power_on()

    # -- keys -------------------------------------------------------------- #

    def _absorb_token(self, issued: Any) -> None:
        """Persist a token the TV issued mid-conversation (contract 6.2)."""
        if not isinstance(issued, str) or not issued.strip():
            return
        tok = issued.strip()
        if tok == (self.token() or ""):
            return
        st = _state(self.ctx)
        for name in ("save_token", "set_token", "put_token", "store_token", "add_token"):
            f = getattr(st, name, None)
            if callable(f):
                try:
                    _invoke(f, alias=self.alias, token=tok,
                            client_name=self.client_name, name=self.client_name)
                    log.info("%s: stored a freshly issued token", self.alias)
                except Exception as exc:
                    log.debug("%s: could not store issued token: %s", self.alias, exc)
                return

    def _send_verified(self, seq: Sequence[str]) -> None:
        """Send keys with the full checks: the auth drains and the inter-key gap.

        A bad token still completes the handshake - the TV only objects, with
        ms.error "No Authorized", once you send something - so samsung.send_keys
        drains for that and raises NotPaired. Reporting "sent" on a rejected
        token is what once made a failed power-off look like a success.
        """
        if SAM.send_keys is None:
            raise RuntimeError("samsung.send_keys is unavailable in this build")
        # Two channels to one set fight; give up the interactive socket first.
        self.drop_sockets()
        with self.lock:
            issued = _invoke(
                SAM.send_keys, ip=self.ip, host=self.ip, address=self.ip,
                client_name=self.client_name, name=self.client_name,
                token=self.token(), keys=list(seq), key=list(seq),
                sequence=list(seq), verify=True, gap=0.35, timeout=self.ws_timeout,
            )
        self._absorb_token(issued)

    def key(self, key: str) -> Result:
        """One keypress, fast (contract 7.9 / I18).

        Keeps the control socket open between presses with a 30 s idle expiry and
        skips the auth read-back: a fresh handshake plus a 0.6 s pre-drain and a
        2 s post-drain per press made the on-screen remote feel three seconds
        behind every tap. An interactive user sees the TV react, so the
        read-back buys nothing here. Macros keep the checks.
        """
        norm = normalize_key(key)
        if norm is None:
            return Result.error("'%s' is not a valid key name" % key)
        last: Optional[BaseException] = None
        for _attempt in (1, 2):
            try:
                with self.lock:
                    sent = self._press_fast(norm)
                if sent:
                    return Result.good("sent " + norm)
            except Exception as exc:
                last = exc
                if isinstance(exc, NotPaired):
                    self.drop_sockets()
                    return Result.error(NOT_PAIRED_TEXT)
                # A stale socket: drop it and rebuild once.
                self.drop_sockets()
        # No reusable channel in this build: fall back to the unverified send,
        # which is the same interactive path, just without socket reuse.
        try:
            self._send_unverified([norm])
            return Result.good("sent " + norm)
        except Exception as exc:
            last = exc
        return Result.error("%s failed: %s" % (norm, explain(last) if last else "unknown"))

    def _press_fast(self, norm: str) -> bool:
        conn = self._remote()
        if conn is None:
            return False
        for name in ("send_key", "press", "click", "key"):
            f = getattr(conn, name, None)
            if callable(f):
                _invoke(f, key=norm, name=norm, code=norm)
                self._conn_used = time.monotonic()
                return True
        raise RuntimeError("the control channel exposes no keypress method")

    def _remote(self) -> Any:
        """The reused interactive control channel, or None."""
        if SAM.remote is None:
            return None
        now = time.monotonic()
        if self._conn is not None and (now - self._conn_used) > 30.0:
            self.drop_sockets()
        alive = getattr(self._conn, "alive", None)
        if self._conn is not None and callable(alive):
            try:
                if not alive():
                    self.drop_sockets()   # the TV closed it while we were idle
            except Exception:
                self.drop_sockets()
        if self._conn is None:
            conn = _invoke(
                SAM.remote, ip=self.ip, host=self.ip, address=self.ip,
                client_name=self.client_name, name=self.client_name,
                token=self.token(), timeout=self.ws_timeout, ws_timeout=self.ws_timeout,
            )
            opener = getattr(conn, "open", None)
            if callable(opener):
                # A channel object is inert until opened; the handshake (and any
                # NotPaired) happens here.
                _invoke(opener, wait_seconds=self.ws_timeout, seconds=self.ws_timeout)
            self._conn = conn
            self._absorb_token(getattr(self._conn, "issued_token", None))
        self._conn_used = now
        return self._conn

    def _send_unverified(self, seq: Sequence[str]) -> None:
        if SAM.send_keys is None:
            raise RuntimeError("samsung.send_keys is unavailable in this build")
        with self.lock:
            issued = _invoke(
                SAM.send_keys, ip=self.ip, host=self.ip, address=self.ip,
                client_name=self.client_name, name=self.client_name,
                token=self.token(), keys=list(seq), key=list(seq),
                sequence=list(seq), verify=False, gap=0.35, timeout=self.ws_timeout,
            )
        self._absorb_token(issued)

    def keys(self, keys: list[str]) -> Result:
        try:
            seq = list(keys) if isinstance(keys, (list, tuple)) else parse_key_sequence(keys)
        except ValueError as exc:
            return Result.error(str(exc))
        if not seq:
            return Result.error("no keys to send")
        self._activity("sending %d key(s)" % len(seq), _macro_seconds(seq))
        try:
            self._send_verified(seq)
        except NotPaired:
            return Result.error(NOT_PAIRED_TEXT)
        except Exception as exc:
            return Result.error("keys failed: " + explain(exc))
        finally:
            self._clear_activity()
        return Result.good("sent " + " ".join(seq))

    def macro_keys(self, name: str) -> Optional[List[str]]:
        """Resolve a macro. Per-TV open_macro/exit_macro beat the shared macro of
        the same role (contract 3.6). Raises ValueError on a bad key name."""
        role = str(name or "").strip()
        raw: Any = None
        if role in ("open", "exit"):
            own = self.opt(role + "_macro")
            if own:
                raw = own
        if raw is None:
            macros = self._cfg.get("macros")
            if isinstance(macros, dict):
                raw = macros.get(role)
        if raw is None and role == "exit":
            raw = ["KEY_RETURN", "@600", "KEY_EXIT"]  # the 3.x default
        if raw is None:
            return None
        return parse_key_sequence(raw)

    def macro(self, name: str) -> Result:
        try:
            seq = self.macro_keys(name)
        except ValueError as exc:
            return Result.error("macro '%s' is not valid: %s" % (name, exc))
        if seq is None:
            known = sorted((self._cfg.get("macros") or {}).keys())
            return Result.error("no macro called '%s' (known: %s)"
                                % (name, ", ".join(known) or "none"))
        if not seq:
            return Result.warn("macro '%s' is empty - record one on this TV's page" % name)
        return self.keys(seq)

    def app(self, app_id: str) -> Result:
        app_id = str(app_id or "").strip()
        if not _APP_ID_RE.match(app_id):
            return Result.error("'%s' is not a valid app id" % app_id)
        if SAM.launch_app is None:
            return Result.error("launching apps is unavailable in this build")
        try:
            got = _invoke(SAM.launch_app, ip=self.ip, host=self.ip, address=self.ip,
                          app_id=app_id, appid=app_id, timeout=6.0)
        except Exception as exc:
            return Result.error("could not launch %s: %s" % (app_id, explain(exc)))
        if got is False:
            hint = ""
            if self.smart_hub() is False:
                hint = " " + SMART_HUB_TEXT
            return Result.warn(
                "the TV would not launch %s - an app it does not expose makes it "
                "show a 'command not available' box.%s" % (app_id, hint))
        return Result.good("launched " + app_id)

    # -- volume (UPnP, no pairing needed) ---------------------------------- #

    def _upnp(self) -> bool:
        if SAM.upnp_available is None:
            return False
        try:
            return bool(_invoke(SAM.upnp_available, ip=self.ip, host=self.ip,
                                address=self.ip, timeout=1.5))
        except Exception:
            return False

    def volume(self, level: int | str) -> Result:
        text = str(level).strip().lower()
        if text in ("up", "down"):
            key = "KEY_VOLUP" if text == "up" else "KEY_VOLDOWN"
            if self._upnp() and SAM.get_volume is not None and SAM.set_volume is not None:
                try:
                    now = _invoke(SAM.get_volume, ip=self.ip, host=self.ip,
                                  address=self.ip, timeout=4.0)
                    if now is not None:
                        want = max(0, min(100, int(now) + (5 if text == "up" else -5)))
                        _invoke(SAM.set_volume, ip=self.ip, host=self.ip, address=self.ip,
                                level=want, volume=want, value=want, timeout=4.0)
                        return Result.good("volume %d" % want)
                except Exception as exc:
                    log.debug("%s: UPnP volume %s failed: %s", self.alias, text, exc)
            return self.key(key)
        try:
            want = int(text)
        except ValueError:
            return Result.error("volume must be 0-100, 'up' or 'down'")
        if not 0 <= want <= 100:
            return Result.error("volume must be 0-100, 'up' or 'down'")
        if not self._upnp() or SAM.set_volume is None:
            # Many models have 9197 closed. Say what to use instead.
            return Result.warn(
                "this TV does not answer UPnP on port 9197, so the volume cannot "
                "be set to a number - use key/KEY_VOLUP or key/KEY_VOLDOWN")
        try:
            _invoke(SAM.set_volume, ip=self.ip, host=self.ip, address=self.ip,
                    level=want, volume=want, value=want, timeout=4.0)
        except Exception as exc:
            return Result.error("could not set the volume: " + explain(exc))
        return Result.good("volume %d" % want)

    def mute(self, on: bool) -> Result:
        want = bool(on)
        if self._upnp() and SAM.set_mute is not None:
            try:
                _invoke(SAM.set_mute, ip=self.ip, host=self.ip, address=self.ip,
                        on=want, mute=want, value=want, desired=want, timeout=4.0)
                return Result.good("mute " + ("on" if want else "off"))
            except Exception as exc:
                log.debug("%s: UPnP mute failed: %s", self.alias, exc)
        res = self.key("KEY_MUTE")
        if not res.ok:
            return res
        # KEY_MUTE is a toggle, so we cannot promise the state we were asked for.
        return Result.warn(
            "this TV does not answer UPnP on port 9197, so KEY_MUTE was sent "
            "instead - it is a toggle, so mute may now be either way")

    # -- the slideshow ----------------------------------------------------- #

    def nudge_fullscreen(self) -> None:
        """Send ONE real remote keypress so the page can go fullscreen.

        The Fullscreen API only fires from a genuine user gesture - a click
        synthesised in JavaScript is rejected - but a real remote key reaches the
        page as a keydown, which its handler uses to request fullscreen. An
        empty fullscreen_key disables the nudge (contract 7.8 / I16).
        """
        key = self.opt("fullscreen_key", "KEY_ENTER")
        if not key:
            return
        try:
            self._wait(2.0, "waiting 2s, then the fullscreen key")  # let the page bind listeners
            res = self.key(str(key))
            if not res.ok:
                log.debug("%s: fullscreen nudge: %s", self.alias, res.text)
        except Exception as exc:
            log.debug("%s: fullscreen nudge failed: %s", self.alias, exc)

    def show(self, playlist: str) -> Result:
        """Get the slideshow on screen, whichever way this firmware allows.

        Success is "the TV requested the page from us since we started", never
        "the command was accepted": many firmwares acknowledge a launch and then
        ignore it (contract 7.7).

        The caller is responsible for having moved the playlist pointer first -
        Fleet.act does that - because the page picks the change up from the
        manifest within ~5 s without anything being launched at all.
        """
        p = str(playlist or "")
        t0 = time.monotonic()
        detail = {"playlist": p, "homepage": self.homepage_url()}

        # 0) Not on? Wake it, then give it a moment to finish booting.
        if self.power_state(fresh=True) != "on":
            res = self.power_on()
            if res.level == "error":
                return res
            try:
                delay = float(self.opt("wake_delay_seconds", 8) or 0)
            except (TypeError, ValueError):
                delay = 8.0
            self._wait(delay, "giving the TV a moment to finish waking")

        frame = self.is_frame()

        # 1) A Frame in art mode: art mode PAINTS OVER a still-running browser,
        #    so simply leaving art mode brings the slideshow back - no relaunch,
        #    and it works on firmware that will neither report nor launch a
        #    browser.
        if frame and self.art_mode() == "on":
            t1 = time.monotonic()
            self.set_art_mode(False, seconds=8.0)
            self._wait_control_port(20.0)
            if self._landed(t1, 12.0, "waiting for the slideshow to come back"):
                self.nudge_fullscreen()
                return Result.good("playing %s (left art mode)" % p, route="art", **detail)

        # 2) Already up? Then the pointer has moved and the page will follow on
        #    its next poll. Checked BEFORE requiring a launchable browser: a TV
        #    can be happily showing the page from a hand-set homepage on
        #    firmware that will neither report nor launch its browser.
        #    The 8 s wait matters - a backgrounded page's timers are frozen, so
        #    its next fetch may be seconds away rather than already recorded.
        if _hb_fresh(self.ctx, self.ip, 30.0) or \
                self._landed(time.monotonic(), 8.0, "checking whether the page is already up"):
            return Result.good("switched to %s (slideshow already on screen)" % p,
                               route="pointer", **detail)

        mode = str(self.opt("open_with", "auto") or "auto").lower()
        try:
            open_macro = self.macro_keys("open") or []
        except ValueError as exc:
            log.warning("%s: open_macro is not valid (%s)", self.alias, exc)
            open_macro = []
        try:
            launch_wait = float(self.opt("launch_wait_seconds", 30) or 30)
        except (TypeError, ValueError):
            launch_wait = 30.0

        # 3) Frames do not expose their browser in the app registry, so an
        #    app-launch request only makes the TV show a "command not available"
        #    box. With a recorded macro available, SKIP the API launches entirely
        #    and drive the remote (contract I9).
        keys_only = (mode == "macro"
                     or (mode == "auto" and frame and bool(open_macro)))
        if keys_only:
            if not open_macro:
                return Result.warn(
                    "open_with is 'macro' but no open_macro is recorded for this TV - "
                    "record one on its page")
            t1 = time.monotonic()
            if not self._wait_control_port(20.0):
                return Result.warn(
                    "the TV never opened its control port, so no keys could be "
                    "sent - it may be fully asleep")
            sent = self._try_macro(open_macro)
            if not sent:
                # One retry, after giving the port longer: art mode and standby
                # close 8002 and a key sent too early is silently lost.
                if self._wait_control_port(12.0):
                    sent = self._try_macro(open_macro)
            if sent and self._landed(t1, launch_wait):
                self.nudge_fullscreen()
                return Result.good("playing %s (opened with the recorded key macro)" % p,
                                   route="macro", **detail)
            return self._manual_fix(detail)

        url = self.homepage_url() + "?s=%s&fit=%s" % (self.opt("interval_seconds", 10),
                                                      self.opt("fit", "contain"))
        detail["url"] = url

        # 4) URL launch. Never trusted - many firmwares ACK it and ignore the
        #    URL - so the heartbeat decides.
        if mode in ("auto", "api") and SAM.launch_url is not None:
            t1 = time.monotonic()
            try:
                _invoke(SAM.launch_url, ip=self.ip, host=self.ip, address=self.ip,
                        client_name=self.client_name, name=self.client_name,
                        token=self.token(), url=url, app_id=self.browser_app_id(),
                        timeout=self.ws_timeout)
            except Exception as exc:
                log.debug("%s: URL launch failed: %s", self.alias, exc)
            if self._landed(t1, 6.0, "waiting for the browser to open the page"):
                self.nudge_fullscreen()
                return Result.good("playing %s (browser launched at the URL)" % p,
                                   route="url", **detail)

        # 5) Close and relaunch with NO url so it lands on its homepage.
        #    Closed first on purpose: relaunching a running browser is a no-op
        #    and leaves it on whatever page it was already on.
        if mode in ("auto", "api", "homepage"):
            t1 = time.monotonic()
            if self._relaunch_browser():
                # A cold start plus 4K JPEGs was measured over 10 s.
                if self._landed(t1, launch_wait):
                    self.nudge_fullscreen()
                    return Result.good(
                        "playing %s (browser restarted onto its homepage)" % p,
                        route="homepage", **detail)

        # 6) The recorded macro as a last resort.
        if mode != "homepage" and open_macro:
            t1 = time.monotonic()
            if self._wait_control_port(12.0) and self._try_macro(open_macro):
                if self._landed(t1, launch_wait):
                    self.nudge_fullscreen()
                    return Result.good(
                        "playing %s (opened with the recorded key macro)" % p,
                        route="macro", **detail)

        # 7) Nothing worked. Give the exact one-time manual fix.
        return self._manual_fix(detail)

    def _try_macro(self, seq: Sequence[str]) -> bool:
        self._activity("opening the browser with remote keys", _macro_seconds(seq))
        try:
            self._send_verified(list(seq))
            return True
        except Exception as exc:
            log.debug("%s: open macro failed: %s", self.alias, exc)
            return False
        finally:
            self._clear_activity()

    def _relaunch_browser(self) -> bool:
        """Close then relaunch the browser so it lands on its homepage."""
        if SAM.launch_app is None:
            return False
        detected = self.browser_app_id()
        # Detection is only a hint: some firmware 404s the applications endpoint
        # for EVERY app id - including Netflix - while still accepting a launch.
        candidates = [detected] if detected else list(BROWSER_APP_IDS)
        for app_id in candidates:
            if not app_id:
                continue
            if SAM.close_app is not None:
                try:
                    _invoke(SAM.close_app, ip=self.ip, host=self.ip, address=self.ip,
                            app_id=app_id, appid=app_id, timeout=6.0)
                except Exception as exc:
                    log.debug("%s: browser close failed (may not be running): %s",
                              self.alias, exc)
            self._wait(2.5, "letting the browser shut down")  # Tizen needs the gap
            try:
                got = _invoke(SAM.launch_app, ip=self.ip, host=self.ip, address=self.ip,
                              app_id=app_id, appid=app_id, timeout=6.0)
            except Exception as exc:
                log.debug("%s: browser launch %s failed: %s", self.alias, app_id, exc)
                continue
            if got is False:
                continue
            # Remember whichever id actually took - a positive result only.
            _learn(self.ctx, self.alias, "browser_app_id", str(app_id))
            return True
        return False

    def _manual_fix(self, detail: Dict[str, Any]) -> Result:
        return Result.warn(
            "could not get the browser onto the slideshow. One-time manual fix, "
            "with the remote on this TV: open Internet, go to %s, then set that "
            "as the homepage. That address never changes - switching playlists "
            "repoints what it serves." % self.homepage_url(),
            route="manual", **detail)

    def stop(self) -> Result:
        """Leave the slideshow with the exit macro."""
        _hb_forget(self.ctx, self.ip)
        try:
            seq = self.macro_keys("exit")
        except ValueError as exc:
            return Result.error("the exit macro is not valid: %s" % exc)
        if not seq:
            return Result.warn("no exit macro is configured for this TV")
        res = self.keys(seq)
        if not res.ok:
            return res
        if self._poll_dial("stopped", 8.0, "waiting for the browser to close"):
            return Result.good("stopped (browser closed)")
        return Result.good(
            "exit keys sent - the browser may still be open on its own home screen")

    def reopen(self) -> Result:
        """Force the browser back to its homepage.

        This is how a page fix or a stuck browser is recovered without anyone
        visiting the screen (contract 7.10).
        """
        _hb_forget(self.ctx, self.ip)
        t0 = time.monotonic()
        if not self._relaunch_browser():
            return Result.warn(
                "this TV does not expose a launchable browser, so it cannot be "
                "reopened over the network - power it off and on, or use its "
                "recorded open macro via show")
        if self._landed(t0, 12.0, "waiting for the page to come back"):
            self.nudge_fullscreen()
            return Result.good("reopened - the TV fetched the page again")
        return Result.warn(
            "the browser was restarted but the TV has not fetched the page - "
            "check its homepage is set to " + self.homepage_url())

    def drop_sockets(self) -> None:
        conn, self._conn = self._conn, None
        self._conn_used = 0.0
        if conn is None:
            return
        for name in ("close", "disconnect", "shutdown"):
            f = getattr(conn, name, None)
            if callable(f):
                try:
                    f()
                except Exception:
                    pass
                return

    # -- status ------------------------------------------------------------ #

    def probe(self) -> dict:
        """One HTTP-only probe (REST + DIAL, no WebSocket) plus the contract 7.6
        classification, so a whole-fleet sweep stays fast."""
        act_text, act_left = _activity_get(self.ctx, self.alias)
        info = self.info(2.0)
        power = str(_field(info, "power", "unreachable"))
        frame = self.is_frame()
        browser = self.browser_state() if power == "on" else "unknown"
        age = _hb_age(self.ctx, self.ip)
        fresh_secs = self.fresh_seconds
        is_fresh = (age is not None and age < fresh_secs) or \
            (age is None and _hb_fresh(self.ctx, self.ip, fresh_secs))
        try:
            playlist = str(self.playlist_for(self.alias))
        except Exception as exc:
            log.debug("%s: playlist lookup failed: %s", self.alias, exc)
            playlist = ""

        # The order matters. Steps 4 and 5 MUST be tested BEFORE the heartbeat:
        # the heartbeat freezes at the moment the browser closed, so a Frame
        # correctly sitting in art mode would otherwise read as "playing" for
        # another 90 s. And "browser running" alone is NEVER enough to claim
        # playing, because Tizen keeps a backgrounded browser loaded while
        # freezing its JS timers.
        if act_text:
            state, detail = "busy", act_text
        elif power == "unreachable":
            state, detail = "offline", "no answer on the network"
        elif power != "on":
            state, detail = "standby", "power " + power
        elif browser == "stopped" and frame:
            state, detail = "art", "art mode (a Frame's off state)"
        elif browser == "stopped":
            state, detail = "closed", "on, browser closed"
        elif is_fresh:
            detail = "page fetched %.0fs ago" % age if age is not None else "page being fetched"
            state = "playing"
        else:
            state, detail = "idle", "on, not playing"

        rec = _token_record(self.ctx, self.alias)
        return {
            "alias": self.alias,
            "label": self.label,
            "ip": self.ip,
            "mac": self.mac,
            "power": power,
            "model": str(_field(info, "model", "") or ""),
            "frame": frame,
            "paired": self.paired(),
            "verified_how": rec.get("verified_how"),
            "smart_hub": self.smart_hub(),
            "browser": browser,
            "heartbeat_age": None if age is None else round(age, 1),
            "playlist": playlist,
            "state": state,
            "detail": detail,
            "busy": bool(act_text),
            "busy_left": round(float(act_left or 0.0), 1),
            "homepage_confirmed": _homepage_confirmed(self.ctx, self.alias),
            "enabled": self.enabled,
        }

    def status_line(self) -> Result:
        row = self.probe()
        state = row["state"]
        head = state
        if state == "playing" and row.get("playlist"):
            head = "playing " + str(row["playlist"])
        bits: List[str] = []
        if row.get("detail"):
            bits.append(str(row["detail"]))
        if state == "busy" and row.get("busy_left"):
            bits.append("%.0fs left" % row["busy_left"])
        if row.get("model"):
            bits.append(str(row["model"]))
        if not row.get("paired"):
            bits.append("NOT PAIRED")
        text = head + (" - " + ", ".join(bits) if bits else "")
        if state == "offline" or not row.get("paired"):
            return Result(False, text, "warn", row)
        return Result(True, text, "ok", row)


# --------------------------------------------------------------------------- #
# 6. Fleet
# --------------------------------------------------------------------------- #

class Fleet:
    """The roster and everything that drives it."""

    def __init__(self, ctx: "Context", slideshow: "Slideshow" = None) -> None:
        # slideshow is injected (contract: "receives Slideshow by injection").
        # It carries a default only so a caller that builds Fleet(ctx) still
        # works - playlist lookups then fall back to config.default_playlist
        # rather than raising.
        self.ctx = ctx
        self.slideshow = slideshow
        self.tvs: Dict[str, Tv] = {}
        self.identify: bool = False
        self.last_scan: Dict[str, Any] = {"cidr": "", "at": 0.0, "rows": [], "job": None}
        self._lock = threading.RLock()
        self._rows: Dict[str, dict] = {}
        self._rows_at = 0.0
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._build()

    # -- roster ------------------------------------------------------------ #

    @property
    def _cfg(self) -> Dict[str, Any]:
        data = getattr(self.ctx.config, "data", None)
        return data if isinstance(data, dict) else {}

    def _playlist_for(self, alias: str) -> str:
        """The playlist this TV should be showing (contract 8.3 lives in
        slideshow; this is the injected lookup)."""
        f = getattr(self.slideshow, "resolve_for", None)
        if callable(f):
            try:
                got = f(alias)
                if got:
                    return str(got)
            except Exception as exc:
                log.debug("resolve_for(%s) failed: %s", alias, exc)
        block = self._cfg.get("slideshow") or {}
        return str(block.get("default_playlist") or "default")

    def _build(self) -> None:
        specs = self._cfg.get("tvs")
        specs = specs if isinstance(specs, dict) else {}
        with self._lock:
            for alias in list(self.tvs):
                if alias not in specs:
                    self.tvs.pop(alias).drop_sockets()
            for alias, spec in specs.items():
                existing = self.tvs.get(alias)
                if existing is None:
                    tv = Tv(self.ctx, alias, spec)
                    tv.playlist_for = self._playlist_for
                    self.tvs[alias] = tv
                else:
                    existing.update_spec(spec)
                    existing.playlist_for = self._playlist_for

    def reload(self) -> tuple[bool, str]:
        ok, msg = True, "config reloaded"
        f = getattr(self.ctx.config, "reload", None)
        if callable(f):
            try:
                got = f()
                if isinstance(got, tuple) and len(got) == 2:
                    ok, msg = bool(got[0]), str(got[1])
                elif got is not None:
                    ok = bool(got)
            except Exception as exc:
                return False, "config reload failed: " + explain(exc)
        # Rebuild either way: on a failure the previous config is still active,
        # so the roster must keep matching it.
        self._build()
        return ok, msg

    def tv(self, alias: str) -> Tv | None:
        return self.tvs.get(str(alias or ""))

    def aliases(self) -> list[str]:
        return sorted(self.tvs)

    def _enabled(self) -> List[str]:
        return [a for a in sorted(self.tvs) if self.tvs[a].enabled]

    def group_members(self, name: str) -> list[str] | None:
        name = str(name or "")
        if name == "all":
            # The implicit group: every ENABLED TV in alias order. It is never
            # storable in config.
            return self._enabled()
        groups = self._cfg.get("groups")
        members = groups.get(name) if isinstance(groups, dict) else None
        if members is None:
            return None
        return [a for a in members if a in self.tvs]

    def resolve(self, target: str) -> list[str] | None:
        """A TV alias ALWAYS beats a group of the same name, or naming a TV
        "office" silently sends commands to an "office" group (contract I12)."""
        name = str(target or "").strip()
        if not name:
            return None
        if name in self.tvs:
            return [name]
        members = self.group_members(name)
        if members is not None:
            return members
        return None

    # -- acting ------------------------------------------------------------ #

    def act(self, tv: Tv | str, verb: str, args: list[str] | str | None = None) -> Result:
        # Tolerant about its inputs on purpose: the contract's call is
        # act(Tv, verb, ["arg"]), but an alias string and a bare argument are
        # accepted too so a caller cannot trip over the difference.
        if not isinstance(tv, Tv):
            found = self.tvs.get(str(tv or ""))
            if found is None:
                return Result.error("no TV called '%s'" % tv)
            tv = found
        verb = str(verb or "").strip().lower()
        if args is None:
            args = []
        elif isinstance(args, str):
            args = [args]
        args = [a for a in args if a not in (None, "")]
        if verb not in VERBS:
            return Result.error("unknown action '%s'" % verb)
        if verb in _ARG_REQUIRED and not args:
            return Result.error("%s needs an argument" % verb)

        if verb == "on":
            return self._act_on(tv)
        if verb == "off":
            return tv.power_off()
        if verb == "toggle":
            return tv.toggle()
        if verb == "wake":
            return tv.power_on()
        if verb == "status":
            return tv.status_line()
        if verb == "show":
            return self._act_show(tv, args[0] if args else None)
        if verb == "stop":
            return tv.stop()
        if verb == "reopen":
            return tv.reopen()
        if verb == "fullscreen":
            key = tv.opt("fullscreen_key", "KEY_ENTER")
            if not key:
                return Result.warn("the fullscreen nudge is disabled for this TV")
            return tv.key(str(key))
        if verb == "key":
            return tv.key(args[0])
        if verb == "keys":
            try:
                seq = parse_key_sequence(args[0])
            except ValueError as exc:
                return Result.error(str(exc))
            return tv.keys(seq)
        if verb == "macro":
            return tv.macro(args[0])
        if verb == "app":
            return tv.app(args[0])
        if verb == "volume":
            return tv.volume(args[0])
        if verb == "mute":
            want = str(args[0]).strip().lower()
            if want not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
                return Result.error("mute takes 'on' or 'off'")
            return tv.mute(want in ("on", "true", "1", "yes"))
        if verb == "pair":
            return self.pair(tv.alias)
        if verb == "verify":
            return self.verify(tv.alias)
        return Result.error("unknown action '%s'" % verb)  # pragma: no cover

    def _act_on(self, tv: Tv) -> Result:
        """Power / leave art, then restore the current playlist and nudge
        fullscreen (contract 9.5)."""
        power = tv.power_on()
        if power.level == "error":
            return power
        shown = self._act_show(tv, None)
        if shown.ok:
            detail = dict(shown.detail)
            detail["power"] = power.text  # built explicitly: **detail could collide
            return Result(True, shown.text, "ok", detail)
        # Keep the power outcome visible when only the slideshow part failed.
        text = shown.text if power.ok else "%s; %s" % (power.text, shown.text)
        return Result(False, text, shown.level, dict(shown.detail))

    def _act_show(self, tv: Tv, playlist: Optional[str]) -> Result:
        """Move the pointer first, then get the page on screen.

        The pointer move is what actually changes the pictures - a page already
        open follows within ~5 s from the manifest - so it must land before we
        wait on a heartbeat, or a fetch would confirm the OLD playlist.
        """
        if playlist:
            f = getattr(self.slideshow, "set_for_tv", None)
            if callable(f):
                try:
                    got = _invoke(f, alias=tv.alias, name=playlist, playlist=playlist)
                except Exception as exc:
                    return Result.error("could not select '%s': %s" % (playlist, explain(exc)))
                if getattr(got, "ok", True) is False:
                    return Result(False, str(getattr(got, "text", "playlist refused")),
                                  str(getattr(got, "level", "error") or "error"), {})
            target = playlist
        else:
            target = self._playlist_for(tv.alias)
        return tv.show(target)

    def run(self, aliases: list[str], verb: str, args: list[str] | str | None = None, *,
            max_workers: int = 8) -> dict[str, Result]:
        """Fan out with BOUNDED parallelism (contract 7.12 / I17)."""
        if isinstance(aliases, str):
            aliases = [aliases]
        targets = [a for a in (aliases or []) if a in self.tvs]
        out: Dict[str, Result] = {}
        for a in (aliases or []):
            if a not in self.tvs:
                out[a] = Result.error("no TV called '%s'" % a)
        if not targets:
            return out

        def one(alias: str) -> Result:
            tv = self.tvs[alias]
            try:
                return self.act(tv, verb, list(args or []))
            except Exception as exc:
                log.exception("%s: %s failed", alias, verb)
                return Result.error(explain(exc))
            finally:
                # Never leave a countdown running on a dead action, or the
                # dashboard shows a TV as permanently "busy".
                _activity_clear(self.ctx, alias)

        workers = max(1, min(int(max_workers or 8), len(targets)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for alias, res in zip(targets, pool.map(one, targets)):
                out[alias] = res
        return out

    def render(self, results: dict[str, Result]) -> str:
        return "\n".join("[%s] %s" % (a, results[a]) for a in sorted(results))

    def maybe_heal(self, aliases: list[str], verb: str) -> Optional[str]:
        """Chain a heal after a command, if that verb is allowed to trigger one.

        The single place the whitelist and the auto_heal switch are checked, so a
        caller cannot accidentally let an ordinary keypress trigger healing.
        """
        if str(verb or "").lower() not in HEAL_VERBS:
            return None
        healing = self._cfg.get("healing") or {}
        if not bool(healing.get("auto_heal", True)):
            return None
        job_id, _started = self.heal(list(aliases or []))
        return job_id

    # -- status ------------------------------------------------------------ #

    def status(self, alias: str) -> dict:
        tv = self.tvs.get(alias)
        if tv is None:
            return {"alias": alias, "state": "offline", "detail": "not configured",
                    "power": "unreachable", "paired": False, "enabled": False}
        row = tv.probe()
        row["identify_number"] = self._number_of(alias)
        return row

    def _number_of(self, alias: str) -> Optional[int]:
        order = self._enabled()
        return order.index(alias) + 1 if alias in order else None

    def refresh(self, aliases: list[str] | None = None) -> None:
        targets = list(aliases) if aliases is not None else self._enabled()
        targets = [a for a in targets if a in self.tvs]
        if not targets:
            with self._lock:
                self._rows_at = time.monotonic()
            return
        workers = max(1, min(16, len(targets)))
        rows: Dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for alias, row in zip(targets, pool.map(self.status, targets)):
                rows[alias] = row
        with self._lock:
            self._rows.update(rows)
            for alias in list(self._rows):
                if alias not in self.tvs:
                    self._rows.pop(alias, None)
            self._rows_at = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            rows = [dict(r) for r in self._rows.values()]
            at = self._rows_at
        have = {r.get("alias") for r in rows}
        # Every configured TV gets a row, including disabled ones: the row schema
        # carries "enabled" so the dashboard can show them without driving them.
        # Only enabled TVs are ever probed, so a disabled one has no swept row.
        for alias in self.aliases():
            if alias not in have:
                tv = self.tvs[alias]
                off = not tv.enabled
                rows.append({
                    "alias": alias, "label": tv.label, "ip": tv.ip, "mac": tv.mac,
                    "power": "unknown", "model": "", "frame": False,
                    "paired": tv.paired(), "verified_how": None, "smart_hub": None,
                    "browser": "unknown", "heartbeat_age": None,
                    "playlist": "",
                    "state": "standby" if off else "busy",
                    "detail": ("disabled in config" if off
                               else "waiting for the first status sweep"),
                    "busy": not off, "busy_left": 0.0,
                    "homepage_confirmed": _homepage_confirmed(self.ctx, alias),
                    "enabled": tv.enabled,
                    "identify_number": self._number_of(alias),
                })
        rows.sort(key=lambda r: (_STATE_ORDER.index(r.get("state"))
                                 if r.get("state") in _STATE_ORDER else len(_STATE_ORDER),
                                 str(r.get("alias"))))
        healing = self._cfg.get("healing") or {}
        return {
            "tvs": rows,
            "groups": self._groups_view(),
            "identify": self.identify,
            "age_seconds": round(max(0.0, time.monotonic() - at), 1) if at else None,
            "refresh_seconds": healing.get("status_refresh_seconds", 20),
        }

    def _groups_view(self) -> Dict[str, List[str]]:
        groups = self._cfg.get("groups")
        out: Dict[str, List[str]] = {}
        if isinstance(groups, dict):
            for name, members in groups.items():
                out[name] = [a for a in (members or []) if a in self.tvs]
        out["all"] = self._enabled()
        return out

    # -- background loops -------------------------------------------------- #

    def start_background(self) -> None:
        self._stop.clear()
        if self._threads:
            return
        for name, target in (("tvhub-status", self._status_loop),
                             ("tvhub-heal", self._heal_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("background loops started (status sweep and periodic heal)")

    def stop_background(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    def _interval(self, section: str, key: str, default: float) -> float:
        block = self._cfg.get(section) or {}
        try:
            return float(block.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("status sweep failed")
            self._stop.wait(max(5.0, self._interval("healing", "status_refresh_seconds", 20.0)))

    def _heal_loop(self) -> None:
        while not self._stop.is_set():
            minutes = self._interval("healing", "auto_heal_minutes", 10.0)
            if minutes <= 0:  # 0 disables the periodic sweep
                self._stop.wait(60.0)
                continue
            if self._stop.wait(minutes * 60.0):
                return
            try:
                if bool((self._cfg.get("healing") or {}).get("auto_heal", True)):
                    self.heal(self._enabled())
            except Exception:
                log.exception("periodic heal failed")

    # -- self-healing ------------------------------------------------------ #

    def _healable(self, row: Optional[dict]) -> bool:
        """Only TVs a heal could actually fix (contract 7.13 / I13)."""
        if not row or not row.get("enabled", True):
            return False
        if not row.get("paired"):
            return False  # cannot possibly be fixed without pairing
        state = row.get("state")
        if state in ("offline", "standby", "art"):
            # A TV that is off was probably turned off on purpose.
            return False
        return state in ("idle", "closed")

    def heal(self, aliases: list[str] | None = None, rounds: int = 2,
             settle: float = 25.0) -> tuple[str, bool]:
        """Nudge stuck TVs back onto the slideshow.

        Single-flight fleet-wide: concurrent heals once stacked up and drove a TV
        in circles, so a second request joins the running job instead.
        """
        targets = [a for a in (aliases or []) if a in self.tvs] or self._enabled()

        def work(job: Any) -> dict:
            fixed: List[str] = []
            tried: List[str] = []
            for round_no in range(max(1, int(rounds))):
                if self._stop.is_set():
                    break
                _progress(job, step="checking which TVs need a nudge",
                          done=round_no, total=max(1, int(rounds)))
                self.refresh(targets)
                with self._lock:
                    picks = [a for a in targets if self._healable(self._rows.get(a))]
                if not picks:
                    _progress(job, line="nothing to fix")
                    break
                _progress(job, step="round %d: nudging %s" % (round_no + 1, ", ".join(picks)),
                          line="round %d: %s" % (round_no + 1, ", ".join(picks)))
                results = self.run(picks, "show", [])
                for alias in sorted(results):
                    _progress(job, line="[%s] %s" % (alias, results[alias]))
                    tried.append(alias)
                    if results[alias].ok:
                        fixed.append(alias)
                if round_no + 1 < max(1, int(rounds)):
                    _progress(job, step="letting the TVs settle")
                    if self._stop.wait(settle):
                        break
            _progress(job, done=max(1, int(rounds)), step="done")
            return {"tried": sorted(set(tried)), "fixed": sorted(set(fixed))}

        jobs = getattr(self.ctx, "jobs", None)
        if jobs is None:
            return "", False
        f = getattr(jobs, "start_exclusive", None) or getattr(jobs, "start", None)
        if f is None:
            return "", False
        got = self._start_job(f, "heal", "heal", "fix stuck TVs", work)
        if isinstance(got, tuple) and len(got) == 2:
            return str(got[0]), bool(got[1])
        return str(got or ""), True

    def _start_job(self, f: Callable[..., Any], key: str, kind: str,
                   title: str, work: Callable[[Any], Any]) -> Any:
        try:
            return _invoke(f, key=key, kind=kind, title=title, fn=work, func=work,
                           target=work)
        except TypeError as exc:
            log.debug("job start by name failed (%s); trying positional", exc)
        for args in ((key, kind, title, work), (kind, title, work), (key, work), (work,)):
            try:
                return f(*args)
            except TypeError:
                continue
        raise RuntimeError("cannot start a job: unknown Jobs API")

    # -- identify ---------------------------------------------------------- #

    def identify_map(self) -> dict[str, tuple[int, str]]:
        """``{ip: (n, alias)}`` numbered from 1 in alias order.

        Every TV shares one homepage URL, so keying off the CLIENT IP that asked
        for the page is the only way to show each screen something different.
        Empty while identify is off, so a page can call this unconditionally and
        never show a stale overlay.
        """
        if not self.identify:
            return {}
        out: Dict[str, Tuple[int, str]] = {}
        for n, alias in enumerate(self._enabled(), start=1):
            ip = self.tvs[alias].ip
            if ip:
                out[ip] = (n, alias)
        return out

    def set_identify(self, on: bool) -> Result:
        want = bool(on)
        self.identify = want
        if want:
            return Result.good("identify on - each TV shows its own number")
        # Turning it off: the pages need to poll the overlay away (5 s manifest
        # poll) BEFORE the keypress lands, or the number stays on screen.
        t = threading.Thread(target=self._fullscreen_later, name="tvhub-identify-off",
                             daemon=True)
        t.start()
        return Result.good("identify off - the numbers clear within a few seconds")

    def _fullscreen_later(self) -> None:
        try:
            self.fullscreen_all(delay=7.0)
        except Exception:
            log.exception("scheduled fullscreen after identify failed")

    def fullscreen_all(self, delay: float = 7.0) -> Result:
        if delay > 0:
            if self._stop.wait(delay):
                return Result.good("fullscreen skipped - shutting down")
        targets = self._enabled()
        if not targets:
            return Result.good("no TVs to nudge")
        results = self.run(targets, "fullscreen", [])
        ok = sum(1 for r in results.values() if r.ok)
        return Result.good("fullscreen nudged on %d of %d TV(s)" % (ok, len(results)),
                           results={a: str(r) for a, r in results.items()})

    # -- homepages --------------------------------------------------------- #

    def homepages(self) -> dict:
        server = self._cfg.get("server") or {}
        base = str(server.get("base_url") or "").strip().rstrip("/")
        shared = bool((self._cfg.get("slideshow") or {}).get("shared_homepage", True))
        per_tv = {a: self.tvs[a].homepage_url() for a in self.aliases()}
        if shared and self.tvs:
            homepage = next(iter(per_tv.values()))
        elif self.tvs:
            homepage = per_tv[self.aliases()[0]]
        else:
            homepage = (base or "http://<this server>") + "/slideshow/live/all"
        instructions = [
            "This step cannot be automated.",
            "Many Samsung firmwares accept a 'launch the browser at this URL' "
            "command and then ignore the URL, so each TV's homepage has to be set "
            "once by hand with the remote.",
            "On each TV: open Internet, go to the address above, then set it as "
            "the homepage.",
            "Switching playlists repoints what that one address serves, so this "
            "URL never has to change again.",
        ]
        if not base:
            instructions.insert(0, (
                "Set the server address first: this string is typed into every TV, "
                "so it must be a reserved address that will not change."))
        return {
            "homepage_url": homepage,
            "base_url": base,
            "per_tv": per_tv,
            "base_url_set": bool(base),
            "instructions": instructions,
        }

    # -- discovery --------------------------------------------------------- #

    def _default_cidr(self) -> str:
        first = ""
        for alias in self.aliases():
            if self.tvs[alias].ip:
                first = self.tvs[alias].ip
                break
        local = local_ip_toward(first)
        parts = local.split(".")
        if len(parts) != 4 or local.startswith("127."):
            # Better to ask than to invent a network and scan a stranger's LAN.
            raise ValueError(
                "cannot work out which network to scan - add a TV by IP first, or "
                "pass an explicit range like a.b.c.0/24")
        return "%s.%s.%s.0/24" % (parts[0], parts[1], parts[2])

    @staticmethod
    def _alive(ip: str) -> bool:
        """A TCP connect to 8002, then 8001.

        Both, in that order: some sets answer the control port but not 8001,
        which is how two Frames were missed by a discovery that only tried 8001.
        """
        for port in (8002, 8001):
            s = socket.socket()
            s.settimeout(0.8)
            try:
                s.connect((ip, port))
                return True
            except OSError:
                continue
            finally:
                s.close()
        return False

    def _known_ips(self) -> Dict[str, str]:
        return {self.tvs[a].ip: a for a in self.aliases() if self.tvs[a].ip}

    def _row_for(self, ip: str, info: Any, known: Dict[str, str]) -> dict:
        return {
            "ip": ip,
            "model": str(_field(info, "model", "") or ""),
            "name": str(_field(info, "name", "") or ""),
            "mac": normalize_mac(_field(info, "mac", "") or ""),
            "network": str(_field(info, "network", "") or ""),
            "power": str(_field(info, "power", "unreachable")),
            "frame": bool(_field(info, "is_frame", False)),
            "smart_hub": _field(info, "smart_hub", None),
            "alias": known.get(ip, ""),
        }

    def probe_ip(self, ip: str) -> dict:
        """One address, for a manual add or a setup re-check."""
        ip = str(ip or "").strip()
        info = None
        if SAM.device_info is not None:
            try:
                info = _invoke(SAM.device_info, ip=ip, host=ip, address=ip, timeout=4.0)
            except Exception as exc:
                log.debug("probe_ip(%s) failed: %s", ip, exc)
        return self._row_for(ip, info, self._known_ips())

    def scan(self, cidr: str | None = None, job: "JobHandle | None" = None,
             handle: "JobHandle | None" = None) -> list[dict]:
        job = job if job is not None else handle  # accept either name for the handle
        text = str(cidr or "").strip() or self._default_cidr()
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError("'%s' is not a network like a.b.c.0/24 (%s)" % (text, exc))
        if net.num_addresses > 256:
            raise ValueError("only a /24 is supported - '%s' is too large" % text)
        hosts = [str(h) for h in net.hosts()]
        _progress(job, total=len(hosts), done=0, step="looking for TVs on " + text)

        live: List[str] = []
        done = 0
        with ThreadPoolExecutor(max_workers=64) as pool:
            for ip, alive in zip(hosts, pool.map(self._alive, hosts)):
                done += 1
                if alive:
                    live.append(ip)
                if done % 16 == 0 or done == len(hosts):
                    _progress(job, done=done,
                              step="probed %d/%d, %d answering" % (done, len(hosts), len(live)))

        known = self._known_ips()
        rows: List[dict] = []
        if live:
            _progress(job, step="asking %d device(s) what they are" % len(live))
            workers = max(1, min(12, len(live)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                infos = list(pool.map(self._info_of, live))
            for ip, info in zip(live, infos):
                if not _field(info, "model", ""):
                    continue  # answering ports but not a Samsung TV
                rows.append(self._row_for(ip, info, known))
        rows.sort(key=lambda r: int(str(r["ip"]).split(".")[-1] or 0))
        _progress(job, done=len(hosts), step="found %d TV(s)" % len(rows),
                  line="found %d TV(s) on %s" % (len(rows), text))
        with self._lock:
            self.last_scan = {"cidr": text, "at": time.time(), "rows": rows,
                              "job": _job_id(job)}
        for row in rows:
            if row.get("smart_hub") is False:
                log.warning("%s: %s", row["ip"], SMART_HUB_TEXT)
        return rows

    @staticmethod
    def _info_of(ip: str) -> Any:
        if SAM.device_info is None:
            return None
        try:
            return _invoke(SAM.device_info, ip=ip, host=ip, address=ip, timeout=4.0)
        except Exception as exc:
            log.debug("device_info(%s) failed: %s", ip, exc)
            return None

    # -- roster edits ------------------------------------------------------ #

    def _save(self, mutate: Callable[[dict], None]) -> Optional[str]:
        """Write config.json then reload. Returns an error message, or None."""
        f = getattr(self.ctx.config, "save", None)
        if not callable(f):
            return "this build cannot write config.json"
        try:
            f(mutate)
        except Exception as exc:
            return explain(exc)
        ok, msg = self.reload()
        return None if ok else msg

    @staticmethod
    def _valid_ip(ip: str) -> bool:
        try:
            return ipaddress.ip_address(str(ip or "").strip()).version == 4
        except ValueError:
            return False

    def _check_alias(self, alias: str) -> Optional[str]:
        if not ALIAS_RE.match(str(alias or "")):
            return ("'%s' is not a valid name - use lower-case letters, digits and "
                    "hyphens, up to 32 characters" % alias)
        if alias in RESERVED_NAMES:
            return "'%s' is a reserved name" % alias
        return None

    @staticmethod
    def _default_options() -> Dict[str, Any]:
        return {
            "interval_seconds": None, "fit": None, "base_url": None,
            "browser_app_id": None, "open_with": "auto", "open_macro": [],
            "exit_macro": [], "fullscreen_key": None, "wake_delay_seconds": 8,
            "launch_wait_seconds": 30, "power_off_mode": "auto", "frame": None,
        }

    def add_tv(self, alias: str, ip: str, mac: str = "", label: str = "") -> Result:
        alias = str(alias or "").strip().lower()
        bad = self._check_alias(alias)
        if bad:
            return Result.error(bad)
        ip = str(ip or "").strip()
        if not self._valid_ip(ip):
            return Result.error("'%s' is not an IPv4 address" % ip)
        if alias in self.tvs:
            return Result.error("there is already a TV called '%s'" % alias)
        for other in self.aliases():
            if self.tvs[other].ip == ip:
                return Result.error("%s is already configured as '%s'" % (ip, other))
        spec = {
            "ip": ip,
            "mac": normalize_mac(mac),
            "label": str(label or alias),
            "enabled": True,
            "options": self._default_options(),
        }

        def mutate(cfg: dict) -> None:
            cfg.setdefault("tvs", {})[alias] = spec

        err = self._save(mutate)
        if err:
            return Result.error("could not save: " + err)
        return Result.good("added %s at %s - now pair it" % (alias, ip), alias=alias)

    def remove_tv(self, alias: str) -> Result:
        if alias not in self.tvs:
            return Result.error("no TV called '%s'" % alias)

        def mutate(cfg: dict) -> None:
            (cfg.get("tvs") or {}).pop(alias, None)
            groups = cfg.get("groups") or {}
            for name in list(groups):
                groups[name] = [a for a in (groups[name] or []) if a != alias]

        tv = self.tvs.get(alias)
        if tv is not None:
            tv.drop_sockets()
        err = self._save(mutate)
        if err:
            return Result.error("could not save: " + err)
        st = _state(self.ctx)
        f = getattr(st, "forget", None)
        if callable(f):
            try:
                f(alias)
            except Exception as exc:
                log.debug("state.forget(%s) failed: %s", alias, exc)
        with self._lock:
            self._rows.pop(alias, None)
        return Result.good("removed %s" % alias)

    def rename_tv(self, old: str, new: str) -> Result:
        if old not in self.tvs:
            return Result.error("no TV called '%s'" % old)
        new = str(new or "").strip().lower()
        bad = self._check_alias(new)
        if bad:
            return Result.error(bad)
        if new in self.tvs and new != old:
            return Result.error("there is already a TV called '%s'" % new)
        if new == old:
            return Result.good("nothing to change")

        def mutate(cfg: dict) -> None:
            tvs = cfg.setdefault("tvs", {})
            spec = tvs.pop(old, None)
            if spec is not None:
                tvs[new] = spec
            groups = cfg.get("groups") or {}
            for name in list(groups):
                groups[name] = [new if a == old else a for a in (groups[name] or [])]

        self.tvs[old].drop_sockets()
        err = self._save(mutate)
        if err:
            return Result.error("could not save: " + err)
        st = _state(self.ctx)
        f = getattr(st, "rename", None)
        if callable(f):
            try:
                # Moves the token, the playlist pointer, the learned facts and the
                # homepage flag together (contract 4.1).
                _invoke(f, old=old, new=new, alias=old, to=new)
            except Exception as exc:
                log.warning("%s: state.rename to %s failed: %s", old, new, exc)
        with self._lock:
            self._rows.pop(old, None)
        return Result.good("renamed %s to %s" % (old, new))

    def set_tv_ip(self, alias: str, ip: str, mac: str = "") -> Result:
        if alias not in self.tvs:
            return Result.error("no TV called '%s'" % alias)
        ip = str(ip or "").strip()
        if not self._valid_ip(ip):
            return Result.error("'%s' is not an IPv4 address" % ip)
        for other in self.aliases():
            if other != alias and self.tvs[other].ip == ip:
                return Result.error("%s is already configured as '%s'" % (ip, other))
        new_mac = normalize_mac(mac) if mac else None

        def mutate(cfg: dict) -> None:
            spec = (cfg.get("tvs") or {}).get(alias)
            if spec is None:
                return
            spec["ip"] = ip
            if new_mac is not None:
                spec["mac"] = new_mac

        self.tvs[alias].drop_sockets()
        err = self._save(mutate)
        if err:
            return Result.error("could not save: " + err)
        # The token is KEPT: correcting a DHCP drift must not force a re-pair.
        # Re-verify by effect straight away and say plainly if it no longer works.
        res = self.verify(alias)
        if res.ok:
            return Result.good("%s is now at %s (pairing still works)" % (alias, ip))
        return Result.warn(
            "%s is now at %s, but the stored token no longer verifies: %s"
            % (alias, ip, res.text))

    def set_tv_options(self, alias: str, options: dict) -> Result:
        if alias not in self.tvs:
            return Result.error("no TV called '%s'" % alias)
        if not isinstance(options, dict):
            return Result.error("options must be an object")
        patch = dict(options)

        def mutate(cfg: dict) -> None:
            spec = (cfg.get("tvs") or {}).get(alias)
            if spec is None:
                return
            merged = dict(spec.get("options") or {})
            merged.update(patch)
            spec["options"] = merged

        err = self._save(mutate)
        if err:
            return Result.error("could not save: " + err)
        self.tvs[alias].drop_sockets()
        return Result.good("options updated for %s" % alias,
                           changed=sorted(patch))

    def set_group(self, name: str, members: list[str]) -> Result:
        name = str(name or "").strip().lower()
        if not GROUP_RE.match(name):
            return Result.error("'%s' is not a valid group name" % name)
        if name in RESERVED_NAMES:
            return Result.error("'%s' is a reserved name" % name)
        if name in self.tvs:
            return Result.error("'%s' is already a TV alias" % name)
        wanted = [str(a).strip().lower() for a in (members or [])]
        keep = [a for a in wanted if a in self.tvs]
        dropped = [a for a in wanted if a not in self.tvs]

        def mutate(cfg: dict) -> None:
            cfg.setdefault("groups", {})[name] = keep

        err = self._save(mutate)
        if err:
            return Result.error("could not save: " + err)
        text = "group %s: %s" % (name, ", ".join(keep) or "empty")
        if dropped:
            return Result.warn(text + " (unknown and dropped: %s)" % ", ".join(dropped),
                               dropped=dropped)
        return Result.good(text, members=keep)

    def delete_group(self, name: str) -> Result:
        name = str(name or "").strip().lower()
        if name == "all":
            return Result.error("'all' is built in and cannot be changed")
        groups = self._cfg.get("groups") or {}
        if name not in groups:
            return Result.error("no group called '%s'" % name)

        def mutate(cfg: dict) -> None:
            (cfg.get("groups") or {}).pop(name, None)

        err = self._save(mutate)
        if err:
            return Result.error("could not save: " + err)
        return Result.good("deleted group %s" % name)

    # -- pairing ----------------------------------------------------------- #

    def _clear_token(self, alias: str) -> None:
        st = _state(self.ctx)
        for name in ("clear_token", "delete_token", "remove_token", "unpair", "forget_token"):
            f = getattr(st, name, None)
            if callable(f):
                try:
                    f(alias)
                except Exception as exc:
                    log.debug("state.%s(%s) failed: %s", name, alias, exc)
                return

    def pair(self, alias: str, wait: float = 90.0,
             job: "JobHandle | None" = None) -> Result:
        """One TV at a time, never in parallel: the TV holds an ALLOW prompt on
        screen and a human has to press it (contract 7.17)."""
        tv = self.tvs.get(alias)
        if tv is None:
            return Result.error("no TV called '%s'" % alias)
        if SAM.request_pairing is None:
            return Result.error("pairing is unavailable in this build")
        tv.drop_sockets()
        self._clear_token(alias)
        wait = max(5.0, float(wait or 90.0))
        _progress(job, step="press ALLOW on the TV screen", total=1, done=0)
        _activity_set(self.ctx, alias, "waiting for ALLOW on the TV screen", wait)
        try:
            token = _invoke(SAM.request_pairing, ip=tv.ip, host=tv.ip, address=tv.ip,
                            client_name=tv.client_name, name=tv.client_name,
                            wait_seconds=wait, timeout=wait)
        except NotPaired:
            return Result.warn("the TV refused pairing - choose ALLOW on that screen "
                               "and try again")
        except TimeoutError:
            return Result.warn("no ALLOW within %.0f s - try again and accept the "
                               "prompt on the TV" % wait)
        except Exception as exc:
            if isinstance(exc, (Unreachable, OSError)):
                return Result.warn(
                    "TV unreachable - it must be ON and on this subnet. Pairing is "
                    "subnet-sensitive: a TV on another subnet rejects it instantly.")
            return Result.error("pairing failed: " + explain(exc))
        finally:
            _activity_clear(self.ctx, alias)
            _progress(job, done=1)

        if not token:
            return Result.warn("the TV issued no token - choose ALLOW on that screen "
                               "and try again")
        tv._absorb_token(str(token))

        # A stored token is NOT proof of pairing: with a bad token the TV still
        # completes the handshake and only answers "No Authorized" once you send
        # something. Verification by EFFECT is the only accepted proof.
        _progress(job, step="checking the token actually works")
        time.sleep(2.0)  # the set needs a moment before it honours a new token
        res = self.verify(alias)
        extra = ""
        if tv.smart_hub() is False:
            extra = " " + SMART_HUB_TEXT
        if res.ok:
            how = str(res.detail.get("how") or "")
            proof = "volume moved" if how == "upnp" else "the TV accepted our keys"
            return Result.good("paired and verified (%s)%s" % (proof, extra))
        return Result.warn("token stored but the TV rejected it - %s%s" % (res.text, extra))

    def unpair(self, alias: str) -> Result:
        tv = self.tvs.get(alias)
        if tv is None:
            return Result.error("no TV called '%s'" % alias)
        tv.drop_sockets()
        self._clear_token(alias)
        return Result.good("token cleared for %s - pair it again to control it" % alias)

    def verify(self, alias: str) -> Result:
        """Proof by EFFECT: move the volume and read it back (contract 6.14)."""
        tv = self.tvs.get(alias)
        if tv is None:
            return Result.error("no TV called '%s'" % alias)
        # An explicit verify is the ONE thing that resurrects a Frame whose art
        # channel wedged (4.5), and the operator ASKING is the reset signal - it
        # must not depend on this verify succeeding, or a Frame that happens to be
        # in standby today can never be un-blacklisted. If the channel is still
        # broken the next art call re-blacklists it after one bounded attempt.
        if _learned(self.ctx, alias).get("art_hung"):
            _learn(self.ctx, alias, "art_hung", False)
            log.info("%s: art channel un-blacklisted by an explicit verify", alias)
        token = tv.token()
        if not token:
            rec = _token_record(self.ctx, alias)
            if rec.get("token"):
                return Result.warn(
                    "the stored token was paired under client name '%s', so it "
                    "cannot be used - pair this TV again"
                    % (rec.get("client_name") or "?"))
            return Result.warn("no token stored - pair this TV first")
        if SAM.verify_by_effect is None:
            return Result.error("verification is unavailable in this build")
        _activity_set(self.ctx, alias, "checking the TV accepts our token", 12.0)
        try:
            got = _invoke(SAM.verify_by_effect, ip=tv.ip, host=tv.ip, address=tv.ip,
                          client_name=tv.client_name, name=tv.client_name,
                          token=token, timeout=10.0)
        except Exception as exc:
            return Result.error("verification failed: " + explain(exc))
        finally:
            _activity_clear(self.ctx, alias)
        ok, how = (got if isinstance(got, tuple) and len(got) == 2 else (bool(got), None))
        st = _state(self.ctx)
        if ok:
            f = getattr(st, "mark_verified", None)
            if callable(f):
                try:
                    _invoke(f, alias=alias, how=str(how or "drain"), verified_how=str(how or "drain"))
                except Exception as exc:
                    log.debug("mark_verified(%s) failed: %s", alias, exc)
            return Result.good("verified by %s" % (how or "drain"), how=how)
        for name in ("clear_verification", "clear_verified", "unverify"):
            f = getattr(st, name, None)
            if callable(f):
                try:
                    f(alias)
                except Exception as exc:
                    log.debug("state.%s(%s) failed: %s", name, alias, exc)
                break
        reason = str(how or "")
        if reason == "no-change":
            return Result.warn(
                "the TV accepted the connection but the volume never moved, so the "
                "token is not really working - pair this TV again", how=how)
        if reason == "rejected":
            return Result.warn(
                "the TV rejected our token - pair it again and press ALLOW on that "
                "screen", how=how)
        return Result.warn("TV unreachable or in standby - turn it on and try again",
                           how=how)
