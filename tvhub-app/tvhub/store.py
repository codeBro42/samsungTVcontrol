"""tvhub.store - every piece of durable or cross-request state, and nothing else.

This module owns:
  * install paths (root, config.json, state/, photos/, the web asset folder),
  * config.json: load / validate / atomic save / hot reload,
  * state/state.json: pairing tokens, playlist pointers, learned per-TV facts,
    wizard progress,
  * the slideshow heartbeat registry (the ONLY evidence a TV is displaying),
  * the per-TV activity/progress registry (so a bounded wait is explainable),
  * the background job registry,
  * ``Context``, the dependency-injection container every other module takes as
    its first constructor argument, so no module reaches for a global.

It imports nothing from the ``tvhub`` package: it is the bottom of the dependency
graph (store <- samsung <- fleet, store <- ui <- slideshow, ... <- webapp <-
service).

Two hard-won plumbing rules are encoded here and MUST NOT be "simplified":

  * State lives in a MACHINE-WIDE folder next to the install, never in
    %LOCALAPPDATA% / %APPDATA% / ~/.config. A service running as SYSTEM/root and
    a CLI run by a logged-in user resolve those per-user paths differently, which
    once hid 14 valid pairing tokens from the service.
  * A stored token is NOT proof of pairing (``is_paired`` additionally requires
    ``verified_at``), and tokens are keyed by ALIAS, not by IP, so a DHCP drift
    does not orphan a pairing.
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = [
    "__version__",
    "APP_NAME",
    "CONFIG_VERSION",
    "STATE_VERSION",
    "LOGGER_NAME",
    "DEFAULT_CONFIG",
    "TV_OPTION_DEFAULTS",
    "FIT_CHOICES",
    "OPEN_WITH_CHOICES",
    "POWER_OFF_MODES",
    "ALIAS_RE",
    "GROUP_RE",
    "PLAYLIST_RE",
    "MAC_RE",
    "RESERVED_NAMES",
    "ConfigError",
    "StateError",
    "Paths",
    "default_config",
    "default_tv_options",
    "validate_config",
    "normalize_mac",
    "valid_ipv4",
    "valid_alias",
    "valid_group",
    "valid_playlist",
    "local_ip_toward",
    "local_ipv4_addresses",
    "setup_logging",
    "Config",
    "State",
    "Heartbeat",
    "Activity",
    "JobHandle",
    "Jobs",
    "Context",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

# Kept in step with tvhub/__init__.py by hand: store.py must import nothing from
# the package it lives in (it is the bottom of the dependency graph).
__version__ = "1.0.0"

APP_NAME = "TVHub"
CONFIG_VERSION = 1
STATE_VERSION = 1
LOGGER_NAME = "tvhub"

# 2.1 - one grammar for names, everywhere.
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PLAYLIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
# MAC_RE matches the NORMALIZED form only; normalize_mac() does the accepting.
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")

RESERVED_NAMES = frozenset(
    {
        "all",
        "tv",
        "group",
        "api",
        "ui",
        "slideshow",
        "x",
        "health",
        "playlist",
        "playlists",
        "identify",
        "reload",
        "homepages",
    }
)

FIT_CHOICES = ("contain", "cover")
OPEN_WITH_CHOICES = ("auto", "api", "macro", "homepage")
POWER_OFF_MODES = ("auto", "key", "art")

# 3. config.json schema - the exact literal from the contract. Treat as read-only;
# use default_config() to get a copy you may mutate.
DEFAULT_CONFIG = {
    "version": 1,
    "server": {
        "http_port": 8899,
        "bind": "0.0.0.0",
        "base_url": "",
        "client_name": "TVHub",
        "allow_from": [],
        "admin_from": [],
        "ws_timeout": 10.0,
        "max_upload_mb": 64,
    },
    "slideshow": {
        "default_playlist": "default",
        "interval_seconds": 10,
        "fit": "contain",
        "shared_homepage": True,
        "fullscreen_key": "KEY_ENTER",
    },
    "healing": {
        "auto_heal": True,
        "auto_heal_minutes": 10,
        "status_refresh_seconds": 20,
        "heartbeat_fresh_seconds": 90,
    },
    "paths": {"photo_root": "photos"},
    "tvs": {},
    "groups": {},
    "macros": {"exit": ["KEY_RETURN", "@600", "KEY_EXIT"]},
}

# 3.4 - per-TV options. A null value means "inherit"; the others are real
# defaults. Kept as data so validate_config(), the UI and add_tv agree.
TV_OPTION_DEFAULTS = {
    "interval_seconds": None,
    "fit": None,
    "base_url": None,
    "browser_app_id": None,
    "open_with": "auto",
    "open_macro": [],
    "exit_macro": [],
    "fullscreen_key": None,
    "wake_delay_seconds": 8,
    "launch_wait_seconds": 30,
    "power_off_mode": "auto",
    "frame": None,
}

# Which global config section supplies the fallback for an inheritable option
# (7.1: per-TV options.<name> -> config.<section>.<name> -> hard default).
_OPTION_SECTIONS = ("slideshow", "server", "healing")

_TOP_LEVEL_KEYS = ("version", "server", "slideshow", "healing", "paths", "tvs", "groups", "macros")

_LOG_MAX_BYTES = 2 * 1024 * 1024  # 0.9 - 2 MB x 3
_LOG_BACKUPS = 3

# Heartbeat logging throttle (5.1). Measured against the last LOG time, never the
# last fetch: a TV polling every 5 s would otherwise log once and then look dead.
_HEARTBEAT_LOG_EVERY = 60.0

# Activity self-healing bounds (see Activity.get). A leaked entry must not pin a
# TV to "busy" forever, but a genuine overrun must not flip it to idle either.
_ACTIVITY_OVERRUN_GRACE = 120.0
_ACTIVITY_HARD_TTL = 300.0


class ConfigError(Exception):
    """config.json is unusable (not an object, or a TV without a valid ip)."""


class StateError(Exception):
    """A programming error against state.json (illegal name, unserializable fact)."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def default_config() -> Dict[str, Any]:
    """A mutable deep copy of DEFAULT_CONFIG."""
    return copy.deepcopy(DEFAULT_CONFIG)


def default_tv_options() -> Dict[str, Any]:
    """A mutable deep copy of the 3.4 per-TV option defaults."""
    return copy.deepcopy(TV_OPTION_DEFAULTS)


def normalize_mac(value: str) -> str:
    """'aa:bb:cc:dd:ee:ff' or '' if it is not a MAC.

    Accepts every form a human or a TV's REST payload produces: colons, dashes,
    dots, Cisco 'aabb.ccdd.eeff', or bare hex. An all-zero address is rejected
    because Wake-on-LAN to it can never work, and a stored '00:00:...' would make
    power_on() take the WoL branch and then fail confusingly (7.3).
    """
    if value is None:
        return ""
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", str(value))
    if len(hex_only) != 12:
        return ""
    out = ":".join(hex_only[i : i + 2] for i in range(0, 12, 2)).lower()
    if out == "00:00:00:00:00:00":
        return ""
    return out


def valid_ipv4(value: str) -> bool:
    """True for a dotted-quad literal. Purely syntactic (0.0.0.0 passes)."""
    if not isinstance(value, str):
        return False
    parts = value.strip().split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part or len(part) > 3 or not part.isdigit():
            return False
        if int(part) > 255:
            return False
    return True


def valid_alias(name: Any) -> bool:
    """2.1 - a TV alias: matches ALIAS_RE and is not a reserved route word."""
    return isinstance(name, str) and bool(ALIAS_RE.match(name)) and name not in RESERVED_NAMES


def valid_group(name: Any) -> bool:
    """2.1 - a group name. Collision with a TV alias is checked by the caller."""
    return isinstance(name, str) and bool(GROUP_RE.match(name)) and name not in RESERVED_NAMES


def valid_playlist(name: Any) -> bool:
    """2.1 - a playlist name; '.' and '..' can never be one (path traversal)."""
    if not isinstance(name, str) or name in (".", ".."):
        return False
    return bool(PLAYLIST_RE.match(name))


def local_ipv4_addresses() -> List[str]:
    """Every local IPv4 address, best first, loopback/link-local excluded.

    Offered by the setup wizard as candidates for server.base_url and printed by
    doctor. No address literal is hard-coded here (0.6): the routable address is
    discovered by asking the kernel which interface it would use, and the rest
    come from resolving this host's own name.
    """
    found: List[str] = []

    def add(candidate: Any) -> None:
        if isinstance(candidate, str) and valid_ipv4(candidate) and candidate not in found:
            found.append(candidate)

    # The address of the default-route interface, without sending a packet. The
    # symbolic "<broadcast>" avoids hard-coding any real address; SO_BROADCAST is
    # required or connect() is refused on some kernels.
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        sock.connect(("<broadcast>", 9))
        add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        try:
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                add(info[4][0])
        except OSError:
            pass
        try:
            add(socket.gethostbyname(hostname))
        except OSError:
            pass

    routable = [ip for ip in found if not ip.startswith("127.") and not ip.startswith("169.254.")]
    if routable:
        return routable
    # Better to show the loopback than nothing at all in doctor output.
    return found


def local_ip_toward(host: str) -> str:
    """The local address this host would use to reach ``host``.

    Used only when server.base_url is empty, which 3.1 marks as a testing-only
    mode: the base URL is embedded in the browser homepage a human types into
    every TV, so in production it must be a fixed, reserved address instead.
    """
    host = (host or "").strip()
    if host:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            # UDP connect() only fixes the route; nothing is transmitted.
            sock.connect((host, 9))
            ip = sock.getsockname()[0]
            if valid_ipv4(ip) and not ip.startswith("0."):
                return ip
        except OSError:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    addresses = local_ipv4_addresses()
    return addresses[0] if addresses else "127.0.0.1"


def _write_json_atomic(path: Path, data: Any) -> None:
    """Serialize to <path>.tmp, fsync, then os.replace onto <path>.

    A half-written config.json or state.json must never be readable: os.replace
    is atomic on POSIX and on Windows for a same-directory target.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(path))


def _read_json(path: Path) -> Any:
    with open(str(path), "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# 1. on-disk layout
# --------------------------------------------------------------------------- #


class Paths:
    """The install layout. Machine-wide, next to the package - never per-user."""

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            env_root = os.environ.get("TVHUB_HOME", "").strip()
            if env_root:
                root = Path(env_root)
            else:
                # The parent of the tvhub package directory. Deliberately derived
                # from __file__ rather than importing tvhub: store.py imports
                # nothing from its own package (0.5).
                root = Path(__file__).resolve().parent.parent
        self.root = Path(root).expanduser()
        try:
            self.root = self.root.resolve()
        except OSError:  # pragma: no cover - unresolvable path, keep it as given
            pass
        self.package_dir = Path(__file__).resolve().parent
        self.config_file = self.root / "config.json"
        self.state_dir = self.root / "state"
        self.state_file = self.state_dir / "state.json"
        self.bad_state_file = self.state_dir / "state.json.bad"
        self.log_file = self.state_dir / "tvhub.log"
        self.tmp_dir = self.state_dir / "tmp"
        self.default_photo_root = self.root / "photos"
        self.web_dir = self.package_dir / "web"
        self.requirements_file = self.root / "requirements.txt"

    def ensure(self) -> None:
        """Create the folders this install needs. Never clears anything."""
        for folder in (self.root, self.state_dir, self.tmp_dir, self.default_photo_root):
            folder.mkdir(parents=True, exist_ok=True)

    def clear_tmp(self) -> int:
        """Delete leftover upload staging files (1: 'cleared at startup').

        Called by Context.create, NOT by ensure(): clearing while an upload is
        staging would delete a live part-file.
        """
        removed = 0
        try:
            entries = list(self.tmp_dir.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
                    removed += 1
            except OSError:
                pass
        return removed

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Paths(root=%r)" % (str(self.root),)


# --------------------------------------------------------------------------- #
# logging (0.9)
# --------------------------------------------------------------------------- #


def setup_logging(paths: Paths, verbose: bool = False, to_stdout: bool = True) -> logging.Logger:
    """Configure the single ``tvhub`` logger: rotating file + stdout.

    Idempotent - calling it again only adjusts the level, never stacks a second
    handler (the CLI creates a Context inside an already-running process during
    tests, and duplicated handlers double every line).
    """
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False
    if getattr(log, "_tvhub_configured", False):
        return log

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        # delay=True so merely importing/creating a Context does not hold the log
        # file open; the service and a CLI run may both target it.
        file_handler = logging.handlers.RotatingFileHandler(
            str(paths.log_file),
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except OSError as exc:
        # No log file is survivable; refusing to start is not.
        sys.stderr.write("tvhub: cannot open log file %s (%s)\n" % (paths.log_file, exc))

    if to_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        log.addHandler(stream)

    if not log.handlers:  # pragma: no cover - keeps logging.lastResort quiet
        log.addHandler(logging.NullHandler())

    setattr(log, "_tvhub_configured", True)
    return log


# --------------------------------------------------------------------------- #
# 3.7 validation
# --------------------------------------------------------------------------- #


def _warn(warnings: List[str], text: str) -> None:
    if text not in warnings:
        warnings.append(text)


def _as_bool(value: Any, default: bool, warnings: List[str], label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    _warn(warnings, "%s: %r is not true/false - using %r" % (label, value, default))
    return default


def _as_int(
    value: Any,
    default: int,
    warnings: List[str],
    label: str,
    low: Optional[int] = None,
    high: Optional[int] = None,
) -> int:
    if isinstance(value, bool):  # bool is an int subclass; almost never intended
        _warn(warnings, "%s: %r is not a whole number - using %r" % (label, value, default))
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        _warn(warnings, "%s: %r is not a whole number - using %r" % (label, value, default))
        return default
    if low is not None and out < low:
        _warn(warnings, "%s: %d is below the minimum %d - using %d" % (label, out, low, low))
        return low
    if high is not None and out > high:
        _warn(warnings, "%s: %d is above the maximum %d - using %d" % (label, out, high, high))
        return high
    return out


def _as_float(
    value: Any,
    default: float,
    warnings: List[str],
    label: str,
    low: Optional[float] = None,
    high: Optional[float] = None,
) -> float:
    if isinstance(value, bool):
        _warn(warnings, "%s: %r is not a number - using %r" % (label, value, default))
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        _warn(warnings, "%s: %r is not a number - using %r" % (label, value, default))
        return default
    if out != out:  # NaN
        _warn(warnings, "%s: %r is not a number - using %r" % (label, value, default))
        return default
    if low is not None and out < low:
        _warn(warnings, "%s: %g is below the minimum %g - using %g" % (label, out, low, low))
        return low
    if high is not None and out > high:
        _warn(warnings, "%s: %g is above the maximum %g - using %g" % (label, out, high, high))
        return high
    return out


def _as_str(value: Any, default: str, warnings: List[str], label: str, allow_empty: bool = True) -> str:
    if isinstance(value, str):
        out = value.strip()
        if out or allow_empty:
            return out
        _warn(warnings, "%s: must not be empty - using %r" % (label, default))
        return default
    _warn(warnings, "%s: %r is not text - using %r" % (label, default if value is None else value, default))
    return default


def _as_choice(value: Any, choices: Tuple[str, ...], default: str, warnings: List[str], label: str) -> str:
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    _warn(
        warnings,
        "%s: %r is not one of %s - using %r" % (label, value, "/".join(choices), default),
    )
    return default


def _as_str_list(value: Any, warnings: List[str], label: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # A single string where a list belongs is a common hand-edit; for key
        # sequences the separators are "," and "+" (2.2). Repeat tokens such as
        # KEY_LEFT*3 and waits such as @500 are left intact for
        # parse_key_sequence to expand - that grammar lives in samsung.py.
        parts = [part.strip() for part in re.split(r"[,+]", value)]
        out = [part for part in parts if part]
        _warn(warnings, "%s: was text, read as %d item(s)" % (label, len(out)))
        return out
    if not isinstance(value, list):
        _warn(warnings, "%s: %r is not a list - using an empty list" % (label, value))
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif item is None or (isinstance(item, str) and not item.strip()):
            continue
        else:
            _warn(warnings, "%s: dropped non-text entry %r" % (label, item))
    return out


def _clean_url(value: Any, warnings: List[str], label: str) -> str:
    """Trim a base URL. 3.1 - never store a trailing slash."""
    text = _as_str(value, "", warnings, label)
    return text.rstrip("/")


def _section(
    out: Dict[str, Any],
    name: str,
    warnings: List[str],
) -> Dict[str, Any]:
    """Fetch a config section, replacing a non-object with the defaults."""
    value = out.get(name, None)
    if value is None:
        value = copy.deepcopy(DEFAULT_CONFIG[name])
    elif not isinstance(value, dict):
        _warn(warnings, "%s: not an object - using defaults" % name)
        value = copy.deepcopy(DEFAULT_CONFIG[name])
    out[name] = value
    return value


def _note_unknown(container: Dict[str, Any], known: Tuple[str, ...], warnings: List[str], label: str) -> None:
    """Warn about unknown keys. They are PRESERVED so a save never eats them,
    and any key starting with '_' is a comment and is silently kept."""
    for key in container:
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if key not in known:
            _warn(warnings, "%s: unknown key %r ignored (kept in the file)" % (label, key))


def _validate_options(raw: Any, alias: str, warnings: List[str]) -> Dict[str, Any]:
    label = "tvs.%s.options" % alias
    if raw is None:
        options: Dict[str, Any] = {}
    elif isinstance(raw, dict):
        options = dict(raw)
    else:
        _warn(warnings, "%s: not an object - using defaults" % label)
        options = {}

    _note_unknown(options, tuple(TV_OPTION_DEFAULTS.keys()), warnings, label)

    result: Dict[str, Any] = {}
    for key, fallback in TV_OPTION_DEFAULTS.items():
        result[key] = options.get(key, copy.deepcopy(fallback))
    # Preserve comments and unknown keys so Config.save round-trips them.
    for key, value in options.items():
        if key not in result:
            result[key] = value

    # null always means "inherit" (7.1), so only non-null values are coerced.
    if result["interval_seconds"] is not None:
        result["interval_seconds"] = _as_int(
            result["interval_seconds"], 10, warnings, label + ".interval_seconds", low=2
        )
    if result["fit"] is not None:
        result["fit"] = _as_choice(result["fit"], FIT_CHOICES, "contain", warnings, label + ".fit")
    if result["base_url"] is not None:
        result["base_url"] = _clean_url(result["base_url"], warnings, label + ".base_url")
    if result["browser_app_id"] is not None:
        # App ids are opaque strings that differ by firmware year (6.10); never
        # coerce to int - some are numeric, some are reverse-DNS names.
        result["browser_app_id"] = _as_str(result["browser_app_id"], "", warnings, label + ".browser_app_id")
        if not result["browser_app_id"]:
            result["browser_app_id"] = None
    if result["fullscreen_key"] is not None:
        # "" is meaningful: it disables the fullscreen nudge (7.8).
        result["fullscreen_key"] = _as_str(result["fullscreen_key"], "", warnings, label + ".fullscreen_key")
    if result["frame"] is not None:
        result["frame"] = _as_bool(result["frame"], False, warnings, label + ".frame")

    result["open_with"] = _as_choice(
        result["open_with"], OPEN_WITH_CHOICES, "auto", warnings, label + ".open_with"
    )
    result["power_off_mode"] = _as_choice(
        result["power_off_mode"], POWER_OFF_MODES, "auto", warnings, label + ".power_off_mode"
    )
    result["open_macro"] = _as_str_list(result["open_macro"], warnings, label + ".open_macro")
    result["exit_macro"] = _as_str_list(result["exit_macro"], warnings, label + ".exit_macro")
    result["wake_delay_seconds"] = _as_float(
        result["wake_delay_seconds"], 8.0, warnings, label + ".wake_delay_seconds", low=0.0, high=300.0
    )
    result["launch_wait_seconds"] = _as_float(
        result["launch_wait_seconds"], 30.0, warnings, label + ".launch_wait_seconds", low=1.0, high=600.0
    )
    return result


def validate_config(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(normalized_copy, warnings)``.

    3.7 - raises ConfigError ONLY when the document is not a JSON object, when
    tvs/groups/macros are not objects, or when a TV entry has no valid ip.
    Everything else is coerced or dropped with a warning, because a bridge that
    refuses to start over a typo is worse than one that starts and complains.
    Unknown keys and "_" comment keys are preserved so Config.save round-trips
    the file a human hand-edited.
    """
    if not isinstance(data, dict):
        raise ConfigError("config.json must contain a JSON object")

    warnings: List[str] = []
    out = copy.deepcopy(data)

    _note_unknown(out, _TOP_LEVEL_KEYS, warnings, "config")

    out["version"] = _as_int(out.get("version", CONFIG_VERSION), CONFIG_VERSION, warnings, "version", low=1)
    if out["version"] > CONFIG_VERSION:
        _warn(
            warnings,
            "config version %d is newer than this build understands (%d) - unknown settings are ignored"
            % (out["version"], CONFIG_VERSION),
        )

    # ---- server ----------------------------------------------------------- #
    server = _section(out, "server", warnings)
    defaults = DEFAULT_CONFIG["server"]
    _note_unknown(server, tuple(defaults.keys()), warnings, "server")
    server["http_port"] = _as_int(
        server.get("http_port", defaults["http_port"]),
        defaults["http_port"],
        warnings,
        "server.http_port",
        low=1,
        high=65535,
    )
    server["bind"] = _as_str(server.get("bind", defaults["bind"]), defaults["bind"], warnings, "server.bind", allow_empty=False)
    server["base_url"] = _clean_url(server.get("base_url", ""), warnings, "server.base_url")
    # 3.2 - tokens are bound to this exact string, not to this machine. Changing
    # it invalidates every token, so it must never be silently blanked.
    server["client_name"] = _as_str(
        server.get("client_name", defaults["client_name"]),
        defaults["client_name"],
        warnings,
        "server.client_name",
        allow_empty=False,
    )
    server["allow_from"] = _as_str_list(server.get("allow_from", []), warnings, "server.allow_from")
    server["admin_from"] = _as_str_list(server.get("admin_from", []), warnings, "server.admin_from")
    server["ws_timeout"] = _as_float(
        server.get("ws_timeout", defaults["ws_timeout"]),
        defaults["ws_timeout"],
        warnings,
        "server.ws_timeout",
        low=1.0,
        high=120.0,
    )
    server["max_upload_mb"] = _as_int(
        server.get("max_upload_mb", defaults["max_upload_mb"]),
        defaults["max_upload_mb"],
        warnings,
        "server.max_upload_mb",
        low=1,
        high=4096,
    )

    # ---- slideshow -------------------------------------------------------- #
    show = _section(out, "slideshow", warnings)
    defaults = DEFAULT_CONFIG["slideshow"]
    _note_unknown(show, tuple(defaults.keys()), warnings, "slideshow")
    candidate = show.get("default_playlist", defaults["default_playlist"])
    if not valid_playlist(candidate):
        _warn(
            warnings,
            "slideshow.default_playlist: %r is not a usable playlist name - using %r"
            % (candidate, defaults["default_playlist"]),
        )
        candidate = defaults["default_playlist"]
    show["default_playlist"] = candidate
    show["interval_seconds"] = _as_int(
        show.get("interval_seconds", defaults["interval_seconds"]),
        defaults["interval_seconds"],
        warnings,
        "slideshow.interval_seconds",
        low=2,
        high=86400,
    )
    show["fit"] = _as_choice(show.get("fit", defaults["fit"]), FIT_CHOICES, "contain", warnings, "slideshow.fit")
    show["shared_homepage"] = _as_bool(
        show.get("shared_homepage", defaults["shared_homepage"]),
        defaults["shared_homepage"],
        warnings,
        "slideshow.shared_homepage",
    )
    # "" disables the fullscreen nudge; anything else is a key name.
    show["fullscreen_key"] = _as_str(
        show.get("fullscreen_key", defaults["fullscreen_key"]),
        defaults["fullscreen_key"],
        warnings,
        "slideshow.fullscreen_key",
    )

    # ---- healing ---------------------------------------------------------- #
    heal = _section(out, "healing", warnings)
    defaults = DEFAULT_CONFIG["healing"]
    _note_unknown(heal, tuple(defaults.keys()), warnings, "healing")
    heal["auto_heal"] = _as_bool(
        heal.get("auto_heal", defaults["auto_heal"]), defaults["auto_heal"], warnings, "healing.auto_heal"
    )
    heal["auto_heal_minutes"] = _as_int(
        heal.get("auto_heal_minutes", defaults["auto_heal_minutes"]),
        defaults["auto_heal_minutes"],
        warnings,
        "healing.auto_heal_minutes",
        low=0,  # 0 disables the periodic sweep (7.13)
        high=1440,
    )
    heal["status_refresh_seconds"] = _as_int(
        heal.get("status_refresh_seconds", defaults["status_refresh_seconds"]),
        defaults["status_refresh_seconds"],
        warnings,
        "healing.status_refresh_seconds",
        low=5,  # below this the sweep hammers every TV's REST endpoint
        high=3600,
    )
    heal["heartbeat_fresh_seconds"] = _as_int(
        heal.get("heartbeat_fresh_seconds", defaults["heartbeat_fresh_seconds"]),
        defaults["heartbeat_fresh_seconds"],
        warnings,
        "healing.heartbeat_fresh_seconds",
        # The page polls every 5 s (8.8e), but Tizen freezes a backgrounded
        # page's timers, so a too-tight window makes a live TV flicker to idle.
        low=15,
        high=3600,
    )

    # ---- paths ------------------------------------------------------------ #
    paths_section = _section(out, "paths", warnings)
    _note_unknown(paths_section, ("photo_root",), warnings, "paths")
    paths_section["photo_root"] = _as_str(
        paths_section.get("photo_root", "photos"), "photos", warnings, "paths.photo_root", allow_empty=False
    )

    # ---- tvs -------------------------------------------------------------- #
    raw_tvs = out.get("tvs", {})
    if raw_tvs is None:
        raw_tvs = {}
    if not isinstance(raw_tvs, dict):
        raise ConfigError("config.json: 'tvs' must be an object of alias -> settings")
    tvs: Dict[str, Any] = {}
    seen_ips: Dict[str, str] = {}
    for alias in list(raw_tvs.keys()):
        spec = raw_tvs[alias]
        if isinstance(alias, str) and alias.startswith("_"):
            tvs[alias] = spec  # a comment inside the tvs object
            continue
        if not valid_alias(alias):
            _warn(
                warnings,
                "tvs: dropped %r - an alias must be lower-case letters, digits or '-' (max 32) and not a "
                "reserved word" % (alias,),
            )
            continue
        if not isinstance(spec, dict):
            _warn(warnings, "tvs.%s: not an object - dropped" % alias)
            continue
        spec = dict(spec)
        _note_unknown(spec, ("ip", "mac", "label", "enabled", "options"), warnings, "tvs.%s" % alias)
        ip = spec.get("ip", "")
        ip = ip.strip() if isinstance(ip, str) else ip
        if not valid_ipv4(ip) or ip in ("0.0.0.0", "255.255.255.255"):
            # The one TV-level failure the contract makes fatal: without an
            # address nothing about this TV can work, and guessing would send
            # commands to whatever now answers on the wrong address.
            raise ConfigError("config.json: tv %r has no valid ip address (%r)" % (alias, spec.get("ip")))
        raw_mac = spec.get("mac", "")
        mac = normalize_mac(raw_mac if raw_mac is not None else "")
        if raw_mac not in (None, "", mac) and not mac:
            _warn(warnings, "tvs.%s.mac: %r is not a MAC address - Wake-on-LAN is disabled" % (alias, raw_mac))
        label = spec.get("label", "")
        label = label.strip() if isinstance(label, str) else ""
        clean = {
            "ip": ip,
            "mac": mac,
            "label": label or alias,
            "enabled": _as_bool(spec.get("enabled", True), True, warnings, "tvs.%s.enabled" % alias),
            "options": _validate_options(spec.get("options"), alias, warnings),
        }
        for key, value in spec.items():
            if key not in clean:
                clean[key] = value
        if ip in seen_ips:
            _warn(
                warnings,
                "tvs.%s and tvs.%s share the address %s - commands to one will land on the other"
                % (seen_ips[ip], alias, ip),
            )
        else:
            seen_ips[ip] = alias
        tvs[alias] = clean
    out["tvs"] = tvs
    real_aliases = set(a for a in tvs if not a.startswith("_"))

    # ---- groups ----------------------------------------------------------- #
    raw_groups = out.get("groups", {})
    if raw_groups is None:
        raw_groups = {}
    if not isinstance(raw_groups, dict):
        raise ConfigError("config.json: 'groups' must be an object of name -> [alias, ...]")
    groups: Dict[str, Any] = {}
    for name in list(raw_groups.keys()):
        members = raw_groups[name]
        if isinstance(name, str) and name.startswith("_"):
            groups[name] = members
            continue
        if not valid_group(name):
            # "all" is reserved: the implicit group is every enabled TV and MUST
            # NOT be storable (3.5).
            _warn(warnings, "groups: dropped %r - not a usable group name (or it is a reserved word)" % (name,))
            continue
        if name in real_aliases:
            # I12 - an alias always beats a group of the same name, so a group
            # named after a TV could never be addressed.
            _warn(warnings, "groups: dropped %r - a TV already uses that name" % (name,))
            continue
        if not isinstance(members, list):
            _warn(warnings, "groups.%s: not a list - dropped" % name)
            continue
        clean_members: List[str] = []
        for member in members:
            if not isinstance(member, str):
                _warn(warnings, "groups.%s: dropped non-text member %r" % (name, member))
                continue
            member = member.strip()
            if member not in real_aliases:
                _warn(warnings, "groups.%s: dropped unknown TV %r" % (name, member))
                continue
            if member not in clean_members:
                clean_members.append(member)
        groups[name] = clean_members
    out["groups"] = groups

    # ---- macros ----------------------------------------------------------- #
    raw_macros = out.get("macros", {})
    if raw_macros is None:
        raw_macros = {}
    if not isinstance(raw_macros, dict):
        raise ConfigError("config.json: 'macros' must be an object of name -> [key, ...]")
    macros: Dict[str, Any] = {}
    for name in list(raw_macros.keys()):
        sequence = raw_macros[name]
        if isinstance(name, str) and name.startswith("_"):
            macros[name] = sequence
            continue
        if not isinstance(name, str) or not name.strip():
            _warn(warnings, "macros: dropped %r - a macro name must be text" % (name,))
            continue
        macros[name.strip()] = _as_str_list(sequence, warnings, "macros.%s" % name)
    for name, sequence in DEFAULT_CONFIG["macros"].items():
        # The 'stop' verb falls back to macros.exit, so it must exist.
        if name not in macros:
            macros[name] = list(sequence)
    out["macros"] = macros

    return out, warnings


# --------------------------------------------------------------------------- #
# 3. Config
# --------------------------------------------------------------------------- #


class Config:
    """config.json: load, validate, atomic save, hot reload.

    ``data`` is the live normalized document. Treat it as read-only - use the
    accessors, which hand out copies (11.3), and Config.save() to change it.
    """

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._lock = threading.RLock()
        # A usable default so nothing crashes before load(); load() does the disk
        # work, __init__ deliberately touches nothing.
        self.data, _ = validate_config(default_config())
        self.warnings: List[str] = []
        self.loaded_at = 0.0  # wall clock: shown to a human, not used for durations
        self._log = logging.getLogger(LOGGER_NAME)

    # -- loading ------------------------------------------------------------ #

    def load(self) -> List[str]:
        """Read config.json, creating it from DEFAULT_CONFIG when absent."""
        with self._lock:
            if not self.paths.config_file.exists():
                starter = default_config()
                _write_json_atomic(self.paths.config_file, starter)
                self._log.info("wrote a starter config.json at %s", self.paths.config_file)
                raw: Any = starter
            else:
                try:
                    raw = _read_json(self.paths.config_file)
                except ValueError as exc:  # JSONDecodeError
                    raise ConfigError("config.json is not valid JSON: %s" % (exc,))
                except OSError as exc:
                    raise ConfigError("cannot read %s: %s" % (self.paths.config_file, exc))
            data, warnings = validate_config(raw)
            self.data = data
            self.warnings = list(warnings)
            self.loaded_at = time.time()
        for text in warnings:
            self._log.warning("config: %s", text)
        return list(warnings)

    def reload(self) -> Tuple[bool, str]:
        """Re-read config.json. On any failure the LIVE config is kept (3.8)."""
        with self._lock:
            if not self.paths.config_file.exists():
                # Deliberately NOT recreated here: writing DEFAULT_CONFIG over a
                # running fleet would drop every TV from a live system. load()
                # may create it at startup; reload() must never blank a fleet.
                message = "config.json not loaded (file is missing) - previous config still active"
                self._log.error(message)
                return False, message
            try:
                raw = _read_json(self.paths.config_file)
                data, warnings = validate_config(raw)
            except (ValueError, OSError, ConfigError) as exc:
                message = "config.json not loaded (%s) - previous config still active" % (exc,)
                self._log.error(message)
                return False, message
            self.data = data
            self.warnings = list(warnings)
            self.loaded_at = time.time()
        for text in warnings:
            self._log.warning("config: %s", text)
        count = len(self.data.get("tvs", {}))
        message = "config.json reloaded - %d TV(s)" % count
        if warnings:
            message += ", %d warning(s)" % len(warnings)
        self._log.info(message)
        return True, message

    def save(self, mutate: Callable[[Dict[str, Any]], None]) -> List[str]:
        """Re-read from disk, apply ``mutate``, validate, then replace atomically.

        Re-reading first means a hand edit made since we loaded is not lost, and
        it preserves "_" comments. On a validation failure BOTH the file and the
        live data are left untouched (3.8).
        """
        with self._lock:
            if self.paths.config_file.exists():
                try:
                    disk = _read_json(self.paths.config_file)
                except (ValueError, OSError) as exc:
                    raise ConfigError("cannot save: config.json is unreadable (%s)" % (exc,))
            else:
                disk = copy.deepcopy(self.data)
            if not isinstance(disk, dict):
                raise ConfigError("cannot save: config.json does not contain a JSON object")
            candidate = copy.deepcopy(disk)
            mutate(candidate)
            data, warnings = validate_config(candidate)  # raises ConfigError -> nothing written
            _write_json_atomic(self.paths.config_file, data)
            self.data = data
            self.warnings = list(warnings)
            self.loaded_at = time.time()
        for text in warnings:
            self._log.warning("config: %s", text)
        return list(warnings)

    def add_warning(self, text: str) -> None:
        """Publish a config-level warning (surfaced in /api/status)."""
        with self._lock:
            if text not in self.warnings:
                self.warnings.append(text)

    # -- accessors (every one hands out a copy) ----------------------------- #

    def server(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data.get("server", {}))

    def slideshow(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data.get("slideshow", {}))

    def healing(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data.get("healing", {}))

    def tvs(self) -> Dict[str, Dict[str, Any]]:
        """Configured TVs by alias, in file order, comments excluded."""
        with self._lock:
            raw = self.data.get("tvs", {})
            return dict(
                (alias, copy.deepcopy(spec))
                for alias, spec in raw.items()
                if isinstance(alias, str) and not alias.startswith("_") and isinstance(spec, dict)
            )

    def tv(self, alias: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            spec = self.data.get("tvs", {}).get(alias)
            if not isinstance(spec, dict):
                return None
            return copy.deepcopy(spec)

    def groups(self) -> Dict[str, List[str]]:
        with self._lock:
            raw = self.data.get("groups", {})
            return dict(
                (name, list(members))
                for name, members in raw.items()
                if isinstance(name, str) and not name.startswith("_") and isinstance(members, list)
            )

    def group(self, name: str) -> Optional[List[str]]:
        with self._lock:
            members = self.data.get("groups", {}).get(name)
            if not isinstance(members, list):
                return None
            return list(members)

    def macros(self) -> Dict[str, List[str]]:
        with self._lock:
            raw = self.data.get("macros", {})
            return dict(
                (name, list(sequence))
                for name, sequence in raw.items()
                if isinstance(name, str) and not name.startswith("_") and isinstance(sequence, list)
            )

    def macro(self, name: str) -> Optional[List[str]]:
        with self._lock:
            sequence = self.data.get("macros", {}).get(name)
            if not isinstance(sequence, list):
                return None
            return list(sequence)

    def photo_root(self) -> Path:
        """The photo library root, absolute. A relative setting is resolved
        against the install root, never against the current directory - a service
        starts in an unpredictable one."""
        with self._lock:
            raw = self.data.get("paths", {}).get("photo_root", "photos")
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.paths.root / candidate
        try:
            return candidate.resolve()
        except OSError:  # pragma: no cover
            return candidate

    def client_name(self) -> str:
        with self._lock:
            return str(self.data.get("server", {}).get("client_name", APP_NAME))

    def ws_timeout(self) -> float:
        with self._lock:
            return float(self.data.get("server", {}).get("ws_timeout", 10.0))

    def http_port(self) -> int:
        with self._lock:
            return int(self.data.get("server", {}).get("http_port", 8899))

    def bind(self) -> str:
        with self._lock:
            return str(self.data.get("server", {}).get("bind", "0.0.0.0"))

    def max_upload_bytes(self) -> int:
        with self._lock:
            return int(self.data.get("server", {}).get("max_upload_mb", 64)) * 1024 * 1024

    def allow_from(self) -> List[str]:
        with self._lock:
            return list(self.data.get("server", {}).get("allow_from", []))

    def admin_from(self) -> List[str]:
        with self._lock:
            return list(self.data.get("server", {}).get("admin_from", []))

    def base_url_configured(self) -> bool:
        with self._lock:
            return bool(str(self.data.get("server", {}).get("base_url", "") or "").strip())

    def base_url(self, toward_ip: Optional[str] = None) -> str:
        """The address TVs use to reach this server, with no trailing slash.

        When server.base_url is empty this GUESSES from the local interface. That
        is a testing-only convenience (3.1): the string ends up inside the
        browser homepage a human types into every TV by hand, so a real install
        must pin it to a reserved address that never changes.
        """
        with self._lock:
            server = self.data.get("server", {})
            configured = str(server.get("base_url", "") or "").strip().rstrip("/")
            port = int(server.get("http_port", 8899))
        if configured:
            return configured
        if toward_ip:
            host = local_ip_toward(toward_ip)
        else:
            addresses = local_ipv4_addresses()
            host = addresses[0] if addresses else "127.0.0.1"
        return "http://%s:%d" % (host, port)

    def tv_option(self, alias: str, name: str, fallback: Any = None) -> Any:
        """7.1 - per-TV options.<name> (when not null) -> the matching key in
        slideshow/server/healing -> the hard default from 3.4 -> ``fallback``.

        Only ``None`` counts as "not set": an empty string is a real value
        (base_url "" means 'work it out', fullscreen_key "" disables the nudge).
        """
        with self._lock:
            spec = self.data.get("tvs", {}).get(alias)
            if isinstance(spec, dict):
                options = spec.get("options")
                if isinstance(options, dict) and options.get(name, None) is not None:
                    return copy.deepcopy(options[name])
            for section in _OPTION_SECTIONS:
                block = self.data.get(section, {})
                if isinstance(block, dict) and name in block and block[name] is not None:
                    return copy.deepcopy(block[name])
        if name in TV_OPTION_DEFAULTS and TV_OPTION_DEFAULTS[name] is not None:
            return copy.deepcopy(TV_OPTION_DEFAULTS[name])
        return fallback

    def snapshot(self) -> Dict[str, Any]:
        """The whole normalized document (a copy). Contains no secrets - tokens
        live in state.json, never here."""
        with self._lock:
            return copy.deepcopy(self.data)


# --------------------------------------------------------------------------- #
# 4. State
# --------------------------------------------------------------------------- #


def _empty_state() -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "tokens": {},
        "playlist": {"shared": None, "per_tv": {}},
        "learned": {},
        "setup": {"wizard_done": False, "base_url_set": False, "homepage_confirmed": {}},
    }


class State:
    """state/state.json - pairing tokens, playlist pointers, learned facts, wizard.

    Every mutator writes through immediately (4.3): a token that only lived in
    memory would be lost on the next restart and the TV would need re-pairing.
    """

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._lock = threading.RLock()
        self.data = _empty_state()
        self.warnings: List[str] = []
        self.loaded_at = 0.0
        self._log = logging.getLogger(LOGGER_NAME)

    # -- loading ------------------------------------------------------------ #

    def load(self) -> None:
        """Read state.json. NEVER raises.

        A corrupt file is moved aside to state.json.bad (overwriting any previous
        one) and replaced with defaults, with a warning that surfaces in
        /api/status.server.config_warnings - losing tokens is bad, but refusing
        to start with a blank screen wall is worse.
        """
        with self._lock:
            self.warnings = []
            raw: Any = None
            if self.paths.state_file.exists():
                try:
                    raw = _read_json(self.paths.state_file)
                except (ValueError, OSError) as exc:
                    self._quarantine(str(exc))
                    raw = None
                else:
                    if not isinstance(raw, dict):
                        self._quarantine("the file does not contain a JSON object")
                        raw = None
            if raw is None:
                self.data = _empty_state()
                self._write_locked()
            else:
                self.data = self._normalize(raw)
                self._write_locked()
            self.loaded_at = time.time()
        for text in self.warnings:
            self._log.warning("state: %s", text)

    def _quarantine(self, reason: str) -> None:
        text = "state.json was unreadable (%s) - it has been kept as %s and replaced with defaults; " \
               "pairing tokens and the playlist pointer were lost" % (reason, self.paths.bad_state_file.name)
        try:
            self.paths.bad_state_file.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(self.paths.state_file), str(self.paths.bad_state_file))
        except OSError as exc:  # pragma: no cover
            text += " (could not keep a copy: %s)" % (exc,)
        if text not in self.warnings:
            self.warnings.append(text)

    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        out = _empty_state()
        try:
            out["version"] = int(raw.get("version", STATE_VERSION))
        except (TypeError, ValueError):
            out["version"] = STATE_VERSION

        tokens = raw.get("tokens")
        if isinstance(tokens, dict):
            for alias, entry in tokens.items():
                if not valid_alias(alias) or not isinstance(entry, dict):
                    self.warnings.append("dropped a pairing entry with an unusable alias %r" % (alias,))
                    continue
                token = entry.get("token")
                if not isinstance(token, (str, int)) or not str(token).strip():
                    self.warnings.append("dropped the pairing entry for %r - no token" % (alias,))
                    continue
                out["tokens"][alias] = {
                    "token": str(token).strip(),
                    "client_name": str(entry.get("client_name") or ""),
                    "paired_at": _maybe_float(entry.get("paired_at")),
                    # 4.2 / I1 - a stored token is not proof of pairing. Anything
                    # other than a real timestamp means "not verified".
                    "verified_at": _maybe_float(entry.get("verified_at")),
                    "verified_how": entry.get("verified_how") if entry.get("verified_how") in ("upnp", "drain") else None,
                }
                if out["tokens"][alias]["verified_at"] is None:
                    out["tokens"][alias]["verified_how"] = None

        playlist = raw.get("playlist")
        if isinstance(playlist, dict):
            shared = playlist.get("shared")
            if isinstance(shared, str) and valid_playlist(shared):
                out["playlist"]["shared"] = shared
            elif shared:
                # I11 - an unvalidated playlist name once blanked every screen and
                # survived a restart. Refuse to load one back in.
                self.warnings.append("ignored the stored playlist pointer %r - not a usable name" % (shared,))
            per_tv = playlist.get("per_tv")
            if isinstance(per_tv, dict):
                for alias, name in per_tv.items():
                    if valid_alias(alias) and isinstance(name, str) and valid_playlist(name):
                        out["playlist"]["per_tv"][alias] = name
                    elif name:
                        self.warnings.append(
                            "ignored the playlist pointer for %r (%r) - not a usable name" % (alias, name)
                        )

        learned = raw.get("learned")
        if isinstance(learned, dict):
            for alias, facts in learned.items():
                if valid_alias(alias) and isinstance(facts, dict):
                    out["learned"][alias] = dict(facts)

        setup = raw.get("setup")
        if isinstance(setup, dict):
            out["setup"]["wizard_done"] = bool(setup.get("wizard_done", False))
            out["setup"]["base_url_set"] = bool(setup.get("base_url_set", False))
            confirmed = setup.get("homepage_confirmed")
            if isinstance(confirmed, dict):
                for alias, flag in confirmed.items():
                    if valid_alias(alias):
                        out["setup"]["homepage_confirmed"][alias] = bool(flag)
            for key, value in setup.items():
                if key not in out["setup"]:
                    out["setup"][key] = value
        return out

    def _write_locked(self) -> None:
        """Persist. Called with the lock held, by every mutator.

        A failure here is logged and recorded, never raised: the caller is usually
        mid-pairing or mid-power-change and the in-memory value is still correct.
        """
        try:
            _write_json_atomic(self.paths.state_file, self.data)
        except OSError as exc:
            text = "could not write %s (%s) - this session's tokens and pointers will not survive a restart" % (
                self.paths.state_file,
                exc,
            )
            self._log.error("state: %s", text)
            if text not in self.warnings:
                self.warnings.append(text)

    # -- tokens ------------------------------------------------------------- #

    def token(self, alias: str, client_name: str) -> Optional[str]:
        """The stored token ONLY when it was granted under ``client_name``.

        3.2 - tokens are bound to the base64 client NAME, not to a machine, so a
        token issued on another host under the same name works here, and changing
        the name invalidates all of them.
        """
        with self._lock:
            entry = self.data["tokens"].get(alias)
            if not entry:
                return None
            if str(entry.get("client_name") or "") != str(client_name):
                return None
            return str(entry.get("token") or "") or None

    def raw_token(self, alias: str) -> Optional[str]:
        """The stored token whatever name it was granted under (for diagnostics)."""
        with self._lock:
            entry = self.data["tokens"].get(alias)
            if not entry:
                return None
            return str(entry.get("token") or "") or None

    def pairing(self, alias: str) -> Dict[str, Any]:
        with self._lock:
            entry = self.data["tokens"].get(alias)
            return copy.deepcopy(entry) if isinstance(entry, dict) else {}

    def set_token(self, alias: str, token: str, client_name: str) -> None:
        """Store a freshly issued token. Verification starts over: a new token is
        not proof of anything until verify_by_effect moves the volume (I1)."""
        with self._lock:
            self.data["tokens"][alias] = {
                "token": str(token),
                "client_name": str(client_name),
                "paired_at": time.time(),  # persisted + shown to a human
                "verified_at": None,
                "verified_how": None,
            }
            self._write_locked()
        self._log.info("stored a pairing token for %s (client name %r)", alias, client_name)

    def mark_verified(self, alias: str, how: str) -> None:
        """Record that pairing was proven BY EFFECT ('upnp' or 'drain')."""
        with self._lock:
            entry = self.data["tokens"].get(alias)
            if not isinstance(entry, dict):
                self._log.warning("state: cannot mark %s verified - no token is stored", alias)
                return
            entry["verified_at"] = time.time()
            entry["verified_how"] = str(how)
            self._write_locked()
        self._log.info("%s verified by effect (%s)", alias, how)

    def clear_verification(self, alias: str) -> None:
        """Keep the token but drop the proof - used after an IP change (7.16)."""
        with self._lock:
            entry = self.data["tokens"].get(alias)
            if not isinstance(entry, dict):
                return
            entry["verified_at"] = None
            entry["verified_how"] = None
            self._write_locked()

    def clear_token(self, alias: str) -> None:
        with self._lock:
            if self.data["tokens"].pop(alias, None) is not None:
                self._write_locked()
                self._log.info("cleared the pairing token for %s", alias)

    def is_paired(self, alias: str, client_name: str) -> bool:
        """I1 - a token under the right client name AND a successful verification."""
        with self._lock:
            entry = self.data["tokens"].get(alias)
            if not entry:
                return False
            if str(entry.get("client_name") or "") != str(client_name):
                return False
            if not str(entry.get("token") or ""):
                return False
            return entry.get("verified_at") is not None

    def unpaired_reason(self, alias: str, client_name: str) -> Optional[str]:
        """Why a TV reads as unpaired, in words a human can act on. None when it
        is paired."""
        with self._lock:
            entry = self.data["tokens"].get(alias)
            if not entry or not str(entry.get("token") or ""):
                return "never paired"
            stored_name = str(entry.get("client_name") or "")
            if stored_name != str(client_name):
                return "paired under client name %r" % (stored_name,)
            if entry.get("verified_at") is None:
                return "a token is stored but has not been proven to work"
            return None

    def token_client_names(self) -> Dict[str, str]:
        """alias -> the client name each stored token was granted under."""
        with self._lock:
            return dict(
                (alias, str(entry.get("client_name") or ""))
                for alias, entry in self.data["tokens"].items()
                if isinstance(entry, dict)
            )

    # -- playlist pointers -------------------------------------------------- #

    def shared_playlist(self) -> Optional[str]:
        with self._lock:
            value = self.data["playlist"].get("shared")
            return value if isinstance(value, str) and value else None

    def set_shared_playlist(self, name: str) -> None:
        """Move the fleet-wide pointer. Belt-and-braces validation for I11: the
        caller (slideshow.activate) has already checked the folder exists, but a
        bad name must never reach persisted state - one did, and it blanked every
        screen until someone found the file."""
        if not valid_playlist(name):
            raise StateError("refusing to store %r as the shared playlist - not a usable name" % (name,))
        with self._lock:
            self.data["playlist"]["shared"] = name
            self._write_locked()
        self._log.info("shared playlist pointer is now %r", name)

    def tv_playlist(self, alias: str) -> Optional[str]:
        with self._lock:
            value = self.data["playlist"].get("per_tv", {}).get(alias)
            return value if isinstance(value, str) and value else None

    def set_tv_playlist(self, alias: str, name: str) -> None:
        """Point one TV at its own playlist. A falsy name removes the override so
        the TV follows the shared pointer again."""
        with self._lock:
            per_tv = self.data["playlist"].setdefault("per_tv", {})
            if not name:
                if per_tv.pop(alias, None) is None:
                    return
            else:
                if not valid_playlist(name):
                    raise StateError("refusing to store %r as %s's playlist - not a usable name" % (name, alias))
                per_tv[alias] = name
            self._write_locked()
        self._log.info("playlist pointer for %s is now %r", alias, name or "(shared)")

    def per_tv_playlists(self) -> Dict[str, str]:
        with self._lock:
            return dict(self.data["playlist"].get("per_tv", {}))

    def playlists_in_use(self) -> List[str]:
        """Every playlist a pointer refers to. slideshow.delete() refuses these
        (8.6) - deleting the active playlist blanks screens."""
        with self._lock:
            names = []
            shared = self.data["playlist"].get("shared")
            if isinstance(shared, str) and shared:
                names.append(shared)
            for name in self.data["playlist"].get("per_tv", {}).values():
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
            return names

    # -- learned facts ------------------------------------------------------ #

    def learned(self, alias: str) -> Dict[str, Any]:
        """The per-TV fact cache. ALWAYS a cache, never a requirement (4.4):
        every consumer must work with it empty."""
        with self._lock:
            facts = self.data["learned"].get(alias)
            return copy.deepcopy(facts) if isinstance(facts, dict) else {}

    def learn(self, alias: str, **facts: Any) -> None:
        """Merge facts for one TV, e.g. ``learn(a, browser_app_id="320...")``.

        Only POSITIVE probe results belong here (4.4): a 401/404 from a TV that
        is not paired yet must NOT be cached as "no browser". ``art_hung`` is
        deliberately persisted (4.5) so a Frame whose art channel wedged once is
        not retried after a restart either; only an explicit verify clears it.
        """
        if not facts:
            return
        try:
            json.dumps(facts)
        except (TypeError, ValueError) as exc:
            raise StateError("learned facts for %s must be JSON-serializable: %s" % (alias, exc))
        with self._lock:
            current = self.data["learned"].setdefault(alias, {})
            current.update(facts)
            self._write_locked()
        self._log.debug("learned for %s: %s", alias, facts)

    def forget_learned(self, alias: str, *names: str) -> None:
        """Drop specific cached facts (used by verify to clear art_hung)."""
        with self._lock:
            current = self.data["learned"].get(alias)
            if not isinstance(current, dict):
                return
            changed = False
            for name in names or tuple(current.keys()):
                if current.pop(name, None) is not None:
                    changed = True
            if changed:
                self._write_locked()

    # -- wizard progress ---------------------------------------------------- #

    def setup(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data["setup"])

    def set_setup(self, **fields: Any) -> None:
        if not fields:
            return
        with self._lock:
            setup = self.data["setup"]
            for key, value in fields.items():
                if key in ("wizard_done", "base_url_set"):
                    setup[key] = bool(value)
                elif key == "homepage_confirmed":
                    if isinstance(value, dict):
                        setup["homepage_confirmed"] = dict(
                            (alias, bool(flag)) for alias, flag in value.items() if valid_alias(alias)
                        )
                else:
                    setup[key] = value
            self._write_locked()

    def homepage_confirmed(self, alias: str) -> bool:
        with self._lock:
            return bool(self.data["setup"].get("homepage_confirmed", {}).get(alias, False))

    def set_homepage_confirmed(self, alias: str, value: bool) -> None:
        """Record that a human has set this TV's browser homepage by hand. There
        is no way to check it from here (I8), so the human's word is the record."""
        with self._lock:
            confirmed = self.data["setup"].setdefault("homepage_confirmed", {})
            if value:
                confirmed[alias] = True
            else:
                confirmed.pop(alias, None)
            self._write_locked()

    # -- roster changes ----------------------------------------------------- #

    def rename(self, old: str, new: str) -> None:
        """Move the token, the playlist pointer, the learned facts and the
        homepage flag together (4.1) - they are all keyed by alias, and leaving
        any behind silently un-pairs a working TV."""
        if not valid_alias(new):
            raise StateError("%r is not a usable alias" % (new,))
        if old == new:
            return
        with self._lock:
            entry = self.data["tokens"].pop(old, None)
            if entry is not None:
                self.data["tokens"][new] = entry
            per_tv = self.data["playlist"].setdefault("per_tv", {})
            pointer = per_tv.pop(old, None)
            if pointer is not None:
                per_tv[new] = pointer
            facts = self.data["learned"].pop(old, None)
            if facts is not None:
                self.data["learned"][new] = facts
            confirmed = self.data["setup"].setdefault("homepage_confirmed", {})
            flag = confirmed.pop(old, None)
            if flag is not None:
                confirmed[new] = flag
            self._write_locked()
        self._log.info("renamed %s to %s in state", old, new)

    def forget(self, alias: str) -> None:
        """Remove everything remembered about one TV (token, pointer, facts, flag)."""
        with self._lock:
            changed = False
            if self.data["tokens"].pop(alias, None) is not None:
                changed = True
            if self.data["playlist"].setdefault("per_tv", {}).pop(alias, None) is not None:
                changed = True
            if self.data["learned"].pop(alias, None) is not None:
                changed = True
            if self.data["setup"].setdefault("homepage_confirmed", {}).pop(alias, None) is not None:
                changed = True
            if changed:
                self._write_locked()
                self._log.info("forgot all stored state for %s", alias)

    def snapshot(self) -> Dict[str, Any]:
        """A deep copy with every token string replaced by True/False, so it can
        be logged, shown in doctor output or returned by an API without leaking
        the secret that controls a TV."""
        with self._lock:
            out = copy.deepcopy(self.data)
        for entry in out.get("tokens", {}).values():
            if isinstance(entry, dict):
                entry["token"] = bool(entry.get("token"))
        return out


def _maybe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 5.1 Heartbeat
# --------------------------------------------------------------------------- #


class Heartbeat:
    """Which client IPs have recently fetched the slideshow page.

    This registry is the ONLY evidence that a TV is actually displaying the
    slideshow (I7). "Browser running" is not enough: Tizen freezes JS timers for
    a backgrounded app, so a loaded-but-hidden page stops polling while DIAL
    still reports the browser as running. Keyed by IP because every TV shares one
    homepage URL, so the requesting address is all we get.
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._log = logger or logging.getLogger(LOGGER_NAME)
        self._lock = threading.Lock()
        self._seen: Dict[str, float] = {}
        self._logged: Dict[str, float] = {}

    def note(self, ip: str) -> None:
        """Record a page/manifest/image request from ``ip``."""
        if not ip:
            return
        now = time.monotonic()
        with self._lock:
            self._seen[ip] = now
            last_log = self._logged.get(ip)
            # Throttle against the last LOG time, never the last fetch: a TV
            # polling every 5 s would otherwise log once and then look dead
            # forever in the log (5.1).
            should_log = last_log is None or (now - last_log) >= _HEARTBEAT_LOG_EVERY
            if should_log:
                self._logged[ip] = now
        if should_log:
            self._log.info("slideshow being fetched by %s", ip)

    def age(self, ip: str) -> Optional[float]:
        """Seconds since that IP last fetched anything, or None if never."""
        with self._lock:
            seen = self._seen.get(ip)
        if seen is None:
            return None
        return max(0.0, time.monotonic() - seen)

    def fresh(self, ip: str, within: float = 90.0) -> bool:
        """True when that IP fetched within ``within`` seconds. Callers pass
        healing.heartbeat_fresh_seconds."""
        age = self.age(ip)
        return age is not None and age < float(within)

    def since(self, ip: str, mono: float) -> bool:
        """True when that IP has fetched at or after the monotonic mark ``mono``.

        This is how 'show' proves success (7.7): the TV requested the page from
        us since we started, not merely that a command was accepted.
        """
        with self._lock:
            seen = self._seen.get(ip)
        return seen is not None and seen >= float(mono)

    def forget(self, ip: str) -> None:
        """Invalidate the heartbeat. MUST be called on every power change and
        every art-mode change (I7): the record freezes at the moment the browser
        stopped, so a stale one makes a blank TV read as playing."""
        if not ip:
            return
        with self._lock:
            self._seen.pop(ip, None)
            self._logged.pop(ip, None)

    def snapshot(self) -> Dict[str, float]:
        """{ip: seconds since it last fetched}. Ages, not raw clock values - a
        monotonic timestamp means nothing outside this process."""
        now = time.monotonic()
        with self._lock:
            items = list(self._seen.items())
        return dict((ip, round(max(0.0, now - seen), 3)) for ip, seen in items)


# --------------------------------------------------------------------------- #
# 5.2 Activity
# --------------------------------------------------------------------------- #


class Activity:
    """What a TV is busy doing, plus how long it may take.

    Every bounded wait in fleet.py publishes one of these and clears it in a
    ``finally`` (5.2). The dashboard shows the text and a countdown, so a 30 s
    pause is explainable rather than mysterious.
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._log = logger or logging.getLogger(LOGGER_NAME)
        self._lock = threading.Lock()
        # alias -> (text, deadline_monotonic|None, set_at_monotonic)
        self._items: Dict[str, Tuple[str, Optional[float], float]] = {}

    def set(self, alias: str, text: str, seconds: Optional[float] = None) -> None:
        now = time.monotonic()
        deadline = None if seconds is None else now + max(0.0, float(seconds))
        with self._lock:
            self._items[alias] = (str(text), deadline, now)
        self._log.debug("activity %s: %s", alias, text)

    def clear(self, alias: str) -> None:
        with self._lock:
            self._items.pop(alias, None)

    def get(self, alias: str) -> Tuple[Optional[str], Optional[float]]:
        """(text, seconds remaining) or (None, None).

        An overrunning wait keeps its text with 0 seconds left - the work really
        is still going. A LEAKED entry (a `finally` that never ran) is dropped
        after a hard TTL, so a crash cannot pin a TV to "busy" until restart.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(alias)
            if entry is None:
                return None, None
            if self._stale(now, entry):
                self._items.pop(alias, None)
                return None, None
            text, deadline, _ = entry
        left = None if deadline is None else max(0.0, round(deadline - now, 1))
        return text, left

    def all(self) -> Dict[str, Tuple[str, Optional[float]]]:
        now = time.monotonic()
        out: Dict[str, Tuple[str, Optional[float]]] = {}
        with self._lock:
            for alias in list(self._items.keys()):
                entry = self._items[alias]
                if self._stale(now, entry):
                    self._items.pop(alias, None)
                    continue
                text, deadline, _ = entry
                out[alias] = (text, None if deadline is None else max(0.0, round(deadline - now, 1)))
        return out

    @staticmethod
    def _stale(now: float, entry: Tuple[str, Optional[float], float]) -> bool:
        _, deadline, set_at = entry
        if deadline is None:
            return (now - set_at) > _ACTIVITY_HARD_TTL
        return now > (deadline + _ACTIVITY_OVERRUN_GRACE)


# --------------------------------------------------------------------------- #
# 5.3 Jobs
# --------------------------------------------------------------------------- #


class JobHandle:
    """The worker's side of a job: progress, log lines and a result."""

    def __init__(self, jobs: "Jobs", job_id: str, key: Optional[str], cancel_event: threading.Event) -> None:
        self.id = job_id
        self.key = key
        self._jobs = jobs
        self._cancel = cancel_event

    @property
    def cancelled(self) -> bool:
        """Cooperative cancellation - long jobs (scan, heal) should check this."""
        return self._cancel.is_set()

    def log(self, line: str) -> None:
        """Append one line. Stored verbatim so fleet's '[alias] text' lines
        (0.8) reach the UI unchanged."""
        self._jobs._append_line(self.id, str(line))

    def progress(self, done: int, total: int) -> None:
        self._jobs._set_fields(self.id, done=int(done), total=int(total))

    def step(self, text: str) -> None:
        self._jobs._set_fields(self.id, step=str(text))

    def set_result(self, value: Any) -> None:
        self._jobs._set_fields(self.id, result=value)


class Jobs:
    """In-memory registry of background work.

    Jobs exist so every device action can return instantly with an id the UI
    polls, and so healing and scanning can be SINGLE-FLIGHT: concurrent heals
    once stacked up and drove a TV in circles (7.13/I13).
    """

    def __init__(self, logger: logging.Logger, keep: int = 40) -> None:
        self._log = logger
        self._keep = max(1, int(keep))
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._cancels: Dict[str, threading.Event] = {}

    # -- starting ----------------------------------------------------------- #

    def start(self, kind: str, title: str, fn: Callable[[JobHandle], Any]) -> str:
        job_id, _ = self._start(None, kind, title, fn)
        return job_id

    def start_exclusive(
        self, key: str, kind: str, title: str, fn: Callable[[JobHandle], Any]
    ) -> Tuple[str, bool]:
        """Start unless a job with this ``key`` is already running.

        Returns (job_id, started_now); (existing_id, False) when one is running.
        """
        with self._lock:
            existing = self._running_locked(key)
        if existing is not None:
            self._log.info("job %s (%s) is already running - not starting another", existing, key)
            return existing, False
        return self._start(key, kind, title, fn)

    def _start(
        self, key: Optional[str], kind: str, title: str, fn: Callable[[JobHandle], Any]
    ) -> Tuple[str, bool]:
        job_id = uuid.uuid4().hex[:12]
        cancel = threading.Event()
        job = {
            "id": job_id,
            "kind": str(kind),
            "title": str(title),
            "key": key,
            "state": "running",
            # Wall clock: these are shown to a human in the job strip (0.10).
            "started_at": time.time(),
            "ended_at": None,
            "done": 0,
            "total": 0,
            "step": "",
            "lines": [],
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._cancels[job_id] = cancel
            self._prune_locked()
        handle = JobHandle(self, job_id, key, cancel)
        thread = threading.Thread(
            target=self._run, args=(job_id, fn, handle), name="tvhub-job-%s-%s" % (kind, job_id), daemon=True
        )
        thread.start()
        self._log.debug("job %s started (%s: %s)", job_id, kind, title)
        return job_id, True

    def _run(self, job_id: str, fn: Callable[[JobHandle], Any], handle: JobHandle) -> None:
        try:
            value = fn(handle)
        except Exception as exc:  # a job thread must never die silently
            self._log.exception("job %s failed", job_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["state"] = "error"
                    job["error"] = str(exc) or exc.__class__.__name__
                    job["ended_at"] = time.time()
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                # A returned value is the result unless the job already set one.
                if value is not None and job.get("result") is None:
                    job["result"] = value
                job["state"] = "done"
                job["ended_at"] = time.time()
        self._log.debug("job %s finished", job_id)

    # -- mutation from a JobHandle ------------------------------------------ #

    def _append_line(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            lines = job["lines"]
            lines.append(line)
            if len(lines) > 200:  # 5.3 - cap at 200, oldest dropped
                del lines[: len(lines) - 200]
        self._log.info("%s", line)

    def _set_fields(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(fields)

    # -- reading ------------------------------------------------------------ #

    def running(self, key: str) -> Optional[str]:
        with self._lock:
            return self._running_locked(key)

    def _running_locked(self, key: str) -> Optional[str]:
        for job_id in reversed(self._order):
            job = self._jobs.get(job_id)
            if job is not None and job.get("key") == key and job.get("state") == "running":
                return job_id
        return None

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return _copy_job(job) if job is not None else None

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Newest first."""
        with self._lock:
            ids = list(reversed(self._order))[: max(0, int(limit))]
            return [_copy_job(self._jobs[i]) for i in ids if i in self._jobs]

    def cancel(self, job_id: str) -> bool:
        """Ask a job to stop. Cooperative: the worker checks handle.cancelled."""
        with self._lock:
            event = self._cancels.get(job_id)
            job = self._jobs.get(job_id)
            if event is None or job is None or job.get("state") != "running":
                return False
            event.set()
        self._log.info("job %s asked to stop", job_id)
        return True

    def _prune_locked(self) -> None:
        """Keep the newest ``keep`` jobs; a running job is never dropped."""
        while len(self._order) > self._keep:
            for index, job_id in enumerate(self._order):
                job = self._jobs.get(job_id)
                if job is None or job.get("state") != "running":
                    del self._order[index]
                    self._jobs.pop(job_id, None)
                    self._cancels.pop(job_id, None)
                    break
            else:
                return  # everything still running - keep them all


def _copy_job(job: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(job)
    out["lines"] = list(job.get("lines") or [])
    try:
        out["result"] = copy.deepcopy(job.get("result"))
    except Exception:  # pragma: no cover - a result should be JSON-ish
        out["result"] = job.get("result")
    return out


# --------------------------------------------------------------------------- #
# Context
# --------------------------------------------------------------------------- #


class Context:
    """The dependency-injection container every other module takes first.

    Nothing in tvhub reaches for a global: samsung, fleet, slideshow, ui, webapp
    and service are all handed this object (0.4).
    """

    def __init__(
        self,
        paths: Paths,
        config: Config,
        state: State,
        heartbeat: Heartbeat,
        activity: Activity,
        jobs: Jobs,
        log: logging.Logger,
    ) -> None:
        self.paths = paths
        self.config = config
        self.state = state
        self.heartbeat = heartbeat
        self.activity = activity
        self.jobs = jobs
        self.log = log
        # MONOTONIC (0.10): started_at is the base for uptime, which is a
        # duration. Use uptime_seconds(), or time.monotonic() - started_at.
        self.started_at = time.monotonic()

    @classmethod
    def create(cls, root: Optional[Path] = None) -> "Context":
        """Build everything: folders, logging, config.json, state.json.

        Raises ConfigError when config.json exists but is unusable (3.8/3.7);
        service.py catches that and prints it rather than starting on a guess.
        """
        paths = Paths(root)
        paths.ensure()
        log = setup_logging(paths)
        paths.clear_tmp()

        config = Config(paths)
        config.load()  # may raise ConfigError - the caller reports it

        state = State(paths)
        state.load()  # never raises
        for text in state.warnings:
            config.add_warning(text)

        ctx = cls(
            paths=paths,
            config=config,
            state=state,
            heartbeat=Heartbeat(log),
            activity=Activity(log),
            jobs=Jobs(log),
            log=log,
        )
        ctx._post_load_checks()
        return ctx

    # -- startup sanity ----------------------------------------------------- #

    def _post_load_checks(self) -> None:
        config, state, log = self.config, self.state, self.log

        # 3.2 - warn LOUDLY when client_name changed while tokens exist. The
        # tokens are bound to that exact string, so every TV would silently read
        # as unpaired; they are kept, not deleted, so putting the name back works.
        current = config.client_name()
        stale = {}
        for alias, name in state.token_client_names().items():
            if name != current:
                stale.setdefault(name or "(blank)", []).append(alias)
        for old, aliases in stale.items():
            text = (
                "server.client_name is now %r but %d pairing token(s) were granted under %r (%s) - "
                "those TVs will read as unpaired until they are paired again, or until client_name is set "
                "back to %r" % (current, len(aliases), old, ", ".join(sorted(aliases)), old)
            )
            config.add_warning(text)
            log.warning("config: %s", text)

        # setup.base_url_set is derived from the config, so keep it honest: a
        # human who cleared base_url must see the wizard step come back.
        configured = config.base_url_configured()
        if bool(state.setup().get("base_url_set")) != configured:
            state.set_setup(base_url_set=configured)

        # Make the photo library exist wherever it was configured, so the first
        # upload does not fail on a missing folder.
        photo_root = config.photo_root()
        try:
            photo_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            text = "photo library %s cannot be created (%s) - uploads and playlists will fail" % (photo_root, exc)
            config.add_warning(text)
            log.error("config: %s", text)

        if not configured and config.tvs():
            # Not fatal, but every homepage URL is a guess until this is pinned.
            text = (
                "server.base_url is empty - the homepage URL is being guessed from the local interface; "
                "set it to this host's reserved address before setting any TV's browser homepage"
            )
            config.add_warning(text)
            log.warning("config: %s", text)

        log.info(
            "%s %s ready - root %s, %d TV(s) configured, %d paired",
            APP_NAME,
            __version__,
            self.paths.root,
            len(config.tvs()),
            sum(1 for alias in config.tvs() if state.is_paired(alias, current)),
        )

    # -- conveniences ------------------------------------------------------- #

    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def heartbeat_fresh_seconds(self) -> float:
        """The configured freshness window, for callers that would otherwise
        hard-code 90."""
        return float(self.config.healing().get("heartbeat_fresh_seconds", 90))

    def reload(self) -> Tuple[bool, str]:
        """Hot-reload config.json and re-run the startup sanity checks."""
        ok, message = self.config.reload()
        if ok:
            self._post_load_checks()
        return ok, message

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Context(root=%r, tvs=%d)" % (str(self.paths.root), len(self.config.tvs()))
