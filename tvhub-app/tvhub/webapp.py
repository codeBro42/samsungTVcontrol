"""tvhub.webapp - the HTTP surface.

One ``ThreadingHTTPServer``, the request/response plumbing, the four access
gates, bounded body reading, and ONE flat routing table (contract 9.5) covering
the controller text routes, the JSON API, the TV-facing slideshow routes and the
admin UI.

There is no device logic and no HTML in here. This module turns a parsed request
into a call on Fleet / Slideshow / UI and formats the reply.

Design rules that are load-bearing rather than stylistic:

* ``App.handle(Request) -> Response`` is a PURE function of (method, path
  segments, query, body). Nothing in it touches a socket, so the whole route
  table can be unit-tested with a hand-built ``Request`` - and it was, before
  fleet.py existed. ``make_handler`` and ``Server`` are the only socket-aware
  code, and they contain no routing.
* Every response carries Content-Length, Cache-Control, X-Content-Type-Options
  and ``Connection: close`` (9.1). The close is not laziness about keep-alive:
  some controllers (Loxone among them) mishandle a persistent HTTP/1.1
  connection and hang until their own timeout.
* A controller route returns 200 whenever the route and target exist, even when
  the action FAILED - the failure travels in the body as ``ERROR ...`` or
  ``WARNING ...`` (0.8, 9.4). Controllers treat a non-200 as a comms fault and
  retry it, which turns one failed TV into a retry storm.
* Slideshow routes are never gated and must never 500 or 404 for a TV that is
  simply pointed at a playlist we have lost track of. A dead page on a TV needs
  a human with a remote; a page that keeps polling recovers by itself.

Coupling to the rest of the package
-----------------------------------
``store`` and ``ui`` are imported for real - they sit below this module in the
dependency order (0.5) and their APIs are settled. ``fleet`` and ``slideshow``
are NOT imported: they arrive as constructor arguments, so this module can be
imported and its routing exercised without them. Where the frozen contract fixes
a wire shape but not the Python name behind it (Fleet's cached status rows,
Slideshow's page/manifest renderers, the ``Result`` type - which the contract
never assigns to a home module), the calls go through the small duck-typing
seams in the SEAMS section below, contract-preferred name first. That is
deliberate: a seam turns a sibling's naming choice into a clear 500 with a
message naming what was missing, instead of an AttributeError traceback in a
thread.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import logging
import os
import socket
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .store import (
    FIT_CHOICES,
    RESERVED_NAMES,
    ConfigError,
    local_ipv4_addresses,
    normalize_mac,
    valid_alias,
    valid_group,
    valid_ipv4,
    valid_playlist,
)

if TYPE_CHECKING:  # pragma: no cover - type checkers only, never at runtime
    from .fleet import Fleet
    from .slideshow import Slideshow
    from .store import Context
    from .ui import UI

try:  # pragma: no cover - trivial
    from . import __version__ as VERSION
except Exception:  # pragma: no cover - importable as a bare module in tests
    VERSION = "1.0.0"

log = logging.getLogger("tvhub")


# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

# 2.3 - the closed verb set. The value says what argument the verb takes, which
# is what turns a malformed argument into a 400 instead of a confusing 200 with
# an error in the body (9.4).
_VERB_ARG: Dict[str, str] = {
    "on": "none",
    "off": "none",
    "toggle": "none",
    "wake": "none",
    "status": "none",
    "show": "playlist?",
    "stop": "none",
    "reopen": "none",
    "fullscreen": "none",
    "key": "key",
    "keys": "keys",
    "macro": "name",
    "app": "name",
    "volume": "volume",
    "mute": "onoff",
    "pair": "int?",
    "verify": "none",
}
VERBS: Tuple[str, ...] = tuple(_VERB_ARG)

# 7.13 - the WHITELIST that may trigger self-healing. A blacklist once let an
# ordinary keypress kick off a heal.
HEAL_VERBS = frozenset({"on", "show"})

# 9.3 - path roots behind the CONTROL gate. Everything else is view/admin except
# the public set.
CONTROL_ROOTS = frozenset(
    {"tv", "group", "all", "playlist", "playlists", "identify", "reload", "homepages", "x"}
)
PUBLIC_ROOTS = frozenset({"slideshow", "health", "favicon.ico"})

# /x/ wraps a CONTROLLER path only. These are the controller routes that are not
# a command target, so /x/ runs them as-is instead of parsing them into a plan.
_X_GENERIC_ROOTS = frozenset({"playlist", "playlists", "identify", "reload", "homepages"})
# Refused outright behind /x/. "api" and "ui" matter for more than tidiness: /x/
# is behind the CONTROL gate, so letting it wrap an /api/ path would let a source
# in allow_from reach admin routes that admin_from is supposed to protect.
# "x" also stops /x/x/x/... nesting into itself.
_X_FORBIDDEN_ROOTS = frozenset({"api", "ui", "slideshow", "x", "health", "favicon.ico"})

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 9.6 - "busy, offline, idle, closed, standby, playing, then alias", so whatever
# needs attention is at the top. The contract's list omits "art"; it goes next to
# standby because a Frame in art mode IS a Frame that is off, and it is not a
# fault. Inserting it there leaves the given sequence intact.
_STATE_RANK: Dict[str, int] = {
    "busy": 0,
    "offline": 1,
    "idle": 2,
    "closed": 3,
    "standby": 4,
    "art": 5,
    "playing": 6,
}

# 8.5 - what a TV can actually display.
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
_EXT_FOR_KIND: Dict[str, str] = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "gif": ".gif",
    "bmp": ".bmp",
}
# Used only if slideshow.py does not export HEIC_BRANDS. iPhone photos are the
# single most common upload and land here as HEIC, so this must not silently
# accept them: the TV browser renders nothing at all.
_HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"})

_CONTENT_TYPES: Dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/vnd.microsoft.icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".woff2": "font/woff2",
}

TEXT_TYPE = "text/plain; charset=utf-8"
# Deliberately no charset: the payload is always ensure_ascii JSON, so there is
# nothing for a controller to get wrong about the encoding.
JSON_TYPE = "application/json"
HTML_TYPE = "text/html; charset=utf-8"

_IMAGE_CACHE = "max-age=3600"
_ASSET_CACHE = "max-age=300"
_NO_STORE = "no-store"

# The one URL a human types into every TV's browser as its homepage (I8). It is
# fixed forever; switching playlists repoints what it serves.
SHARED_HOMEPAGE_PATH = "/slideshow/live/all"

_HOMEPAGE_INSTRUCTIONS: Tuple[str, ...] = (
    "This step cannot be automated. Many Samsung firmwares accept a "
    "'launch the browser at this URL' command and then ignore the URL, so the "
    "homepage has to be set once by hand on each TV.",
    "On the TV, open Internet (the browser app) with the remote.",
    "Go to the address below, then use the browser's own menu to set it as the "
    "homepage / start page.",
    "Do this once per TV. The address never changes: choosing a different "
    "playlist repoints what that one address serves, so you never touch the TV "
    "again.",
    "Then press Test on that TV. It reports success only when the TV has "
    "actually fetched the page from this server.",
)

# 9.5, and the source for GET /api/routes and the README. <t> is expanded in the
# legend row rather than in 17 x 3 rows of near-identical text.
_ROUTES: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("-", "<t> = /tv/<alias> | /group/<name> | /all", "control", "note",
     "Every controller route below accepts GET and POST identically. A group "
     "route returns one '[alias] ...' line per member, in alias order."),
    ("GET", "/health", "public", "text", "Liveness: always 'ok'."),
    ("GET|POST", "<t>/on", "control",  "text",
     "Power on (a Frame leaves art mode), restore the current playlist, nudge fullscreen."),
    ("GET|POST", "<t>/off", "control", "text", "Standby, or Art Mode on a Frame."),
    ("GET|POST", "<t>/toggle", "control", "text", "On if off, off if on."),
    ("GET|POST", "<t>/wake", "control", "text", "Power only - does not touch the slideshow."),
    ("GET|POST", "<t>/status", "control", "text", "One line per TV: state, power, playlist, pairing."),
    ("GET|POST", "<t>/show", "control", "text", "Resume the playlist this TV already resolves to."),
    ("GET|POST", "<t>/show/<playlist>", "control", "text",
     "Point this TV (or group) at <playlist> and get it on screen."),
    ("GET|POST", "<t>/stop", "control", "text", "Leave the slideshow (the exit macro)."),
    ("GET|POST", "<t>/reopen", "control", "text", "Force the browser back to its homepage."),
    ("GET|POST", "<t>/fullscreen", "control", "text", "One real keypress per TV, which is what the Fullscreen API needs."),
    ("GET|POST", "<t>/key/<KEY>", "control", "text", "Fast single press; reuses the open control socket."),
    ("GET|POST", "<t>/keys/<seq>", "control", "text", "A sequence, e.g. KEY_UP,@500,KEY_ENTER*2."),
    ("GET|POST", "<t>/macro/<name>", "control", "text", "Run a named macro from config.macros."),
    ("GET|POST", "<t>/app/<app_id>", "control", "text", "Launch a Tizen app id."),
    ("GET|POST", "<t>/volume/<0-100|up|down>", "control", "text", "UPnP where port 9197 is open, else advises key/KEY_VOLUP."),
    ("GET|POST", "<t>/mute/<on|off>", "control", "text", "Mute over UPnP."),
    ("GET|POST", "<t>/pair", "control", "text", "Pair this TV; a human must press ALLOW on the screen."),
    ("GET|POST", "<t>/verify", "control", "text", "Prove pairing by effect (move the volume and read it back)."),
    ("GET|POST", "/playlist/<name>", "control", "text",
     "Fleet-wide switch. Pointer move only: no device I/O, returns instantly, screens follow within ~5 s."),
    ("GET|POST", "/playlists", "control", "text", "'<name>: <n> image(s)' per line."),
    ("GET|POST", "/identify/<on|off>", "control", "text", "Put a big number and alias on every screen."),
    ("GET|POST", "/homepages", "control", "text", "The ONE URL to set as every TV's browser homepage."),
    ("GET|POST", "/reload", "control", "text", "Re-read config.json; the running config survives a bad file."),
    ("GET|POST", "/x/<controller path>", "control", "text",
     "Fire-and-forget: starts a job and returns 'started <path> (job <id>)' immediately."),
    ("GET", "/slideshow/live/all", "public", "html", "THE homepage URL for every TV. Serves the shared pointer."),
    ("GET", "/slideshow/live/all/manifest.json", "public", "json", "Polled every 5 s; a changed playlist or page version acts on it."),
    ("GET", "/slideshow/live/all/img/<file>", "public", "image", "One photo, cached for an hour."),
    ("GET", "/slideshow/live/<alias>", "public", "html", "Per-TV pointer; same three shapes."),
    ("GET", "/slideshow/p/<playlist>", "public", "html", "A fixed playlist, for spot checks. Same three shapes."),
    ("GET", "/", "view", "html", "Dashboard; redirects to /ui/setup on a fresh install."),
    ("GET", "/ui/", "view", "html", "Dashboard."),
    ("GET", "/ui/setup", "view", "html", "The six-step first-run wizard."),
    ("GET", "/ui/photos", "view", "html", "Playlists, uploads, thumbnails."),
    ("GET", "/ui/tv/<alias>", "view", "html", "Per-TV detail, on-screen remote, macro recorder."),
    ("GET", "/ui/assets/<file>", "view", "asset", "css/js/svg, cached for 5 minutes."),
    ("GET", "/favicon.ico", "public", "image", "icon.svg, or 404."),
    ("GET", "/api/status", "view", "json", "The whole dashboard payload (9.6)."),
    ("GET", "/api/config", "view", "json", "The normalized config."),
    ("GET", "/api/routes", "view", "json", "This table."),
    ("GET", "/api/homepages", "view", "json", "homepage_url, per_tv, base_url_set, instructions."),
    ("GET", "/api/tvs", "view", "json", "The roster with pairing and homepage flags."),
    ("GET", "/api/tvs/<alias>", "view", "json", "One TV, plus its last status row."),
    ("GET", "/api/groups", "view", "json", "{'<name>': ['alias', ...]}, including the implicit 'all'."),
    ("GET", "/api/playlists", "view", "json", "Playlists with counts, sizes and the active pointer."),
    ("GET", "/api/playlists/<name>/images", "view", "json", "Filenames, sizes and thumbnail URLs."),
    ("GET", "/api/discover", "view", "json", "The most recent scan result and its job id."),
    ("GET", "/api/jobs", "view", "json", "Recent jobs, newest first."),
    ("GET", "/api/jobs/<id>", "view", "json", "One job (5.3 shape)."),
    ("GET", "/api/setup", "view", "json", "Wizard progress and the next step."),
    ("POST", "/api/reload", "admin", "json", "Re-read config.json."),
    ("POST", "/api/discover", "admin", "json", "Scan a /24 for TVs. Single-flight."),
    ("POST", "/api/tvs", "admin", "json", "Add a TV by alias and IP."),
    ("PATCH", "/api/tvs/<alias>", "admin", "json", "Rename, re-IP, relabel, or change options."),
    ("DELETE", "/api/tvs/<alias>", "admin", "json", "Remove a TV and forget its token."),
    ("POST", "/api/tvs/<alias>/pair", "admin", "json", "Pair; a human presses ALLOW within 90 s."),
    ("POST", "/api/tvs/<alias>/verify", "admin", "json", "Re-prove pairing by effect; also clears art_hung."),
    ("POST", "/api/tvs/<alias>/unpair", "admin", "json", "Discard the stored token."),
    ("POST", "/api/tvs/<alias>/key/<KEY>", "admin", "json", "Synchronous fast press, for the remote and the macro recorder."),
    ("POST", "/api/tvs/<alias>/action/<verb>[/<arg>]", "admin", "json", "Any verb, as a job."),
    ("POST", "/api/tvs/<alias>/homepage", "admin", "json", "Record that a human set this TV's homepage."),
    ("PUT", "/api/groups/<name>", "admin", "json", "Set a group's members."),
    ("DELETE", "/api/groups/<name>", "admin", "json", "Delete a group."),
    ("POST", "/api/group/<name>/action/<verb>[/<arg>]", "admin", "json", "Any verb on a group, as one job."),
    ("POST", "/api/playlists", "admin", "json", "Create an empty playlist."),
    ("DELETE", "/api/playlists/<name>", "admin", "json", "Delete a playlist unless it is in use."),
    ("POST", "/api/playlists/<name>/activate", "admin", "json", "Make it the fleet-wide pointer."),
    ("POST", "/api/playlists/<name>/images", "admin", "json", "multipart/form-data upload, field name 'files'."),
    ("DELETE", "/api/playlists/<name>/images/<filename>", "admin", "json", "Delete one image."),
    ("POST", "/api/identify", "admin", "json", "{'on': bool}"),
    ("POST", "/api/heal", "admin", "json", "Fix idle/closed TVs. Single-flight."),
    ("POST", "/api/setup", "admin", "json", "{'wizard_done'?, 'base_url'?}"),
)


# --------------------------------------------------------------------------- #
# SEAMS - duck typing across the module boundary
# --------------------------------------------------------------------------- #
#
# The contract freezes wire shapes and behaviour, but for fleet.py and
# slideshow.py it does not always name the Python attribute behind them (and it
# never says which module owns `Result`). These four helpers are the only place
# that ambiguity lives. Contract-preferred names come first in every list.


def _attr(obj: Any, *names: str) -> Optional[Callable[..., Any]]:
    """The first callable attribute of ``obj`` named in ``names``, else None."""
    for name in names:
        found = getattr(obj, name, None)
        if callable(found):
            return found
    return None


def _flex_call(func: Callable[..., Any], values: Dict[str, Any], positional: Sequence[Any] = ()) -> Any:
    """Call ``func``, passing whichever of ``values`` its signature accepts.

    Sibling modules were written in parallel against the same contract, so the
    ARGUMENTS are known (a playlist name, a base path, an interval) while the
    parameter names may differ. Passing by keyword makes parameter ORDER
    irrelevant; the positional fallback covers a signature that renamed
    everything or takes positional-only parameters.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # builtins and C callables
        return func(*positional)

    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return func(**values)

    accepted: Dict[str, Any] = {}
    missing = False
    slots = 0
    for name, param in params.items():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            # Cannot be filled by keyword at all - go positional for the lot.
            return func(*positional[: len(params)])
        slots += 1
        if name in values:
            accepted[name] = values[name]
        elif param.default is inspect.Parameter.empty:
            missing = True
    if missing:
        return func(*positional[:slots])
    return func(**accepted)


def render_result(value: Any) -> str:
    """0.8 - the one text convention for controller routes and CLI lines.

    ``text`` when ok, ``WARNING <text>`` at warn level, ``ERROR <text>`` at
    error level. ``Result`` is duck-typed rather than imported: the contract
    describes it (2.5) without assigning it to a module, and both fleet.py and
    slideshow.py return one.
    """
    if value is None:
        return "ok"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "ok" if value else "ERROR failed"

    # (ok, message), as Config.reload() returns.
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool):
        ok, message = value
        return str(message) if ok else "ERROR %s" % (message,)

    if isinstance(value, dict):
        text = value.get("text", value.get("message", ""))
        level = value.get("level") or ("ok" if value.get("ok", True) else "error")
    else:
        text = getattr(value, "text", None)
        if text is None:
            return str(value)
        level = getattr(value, "level", None)
        if not level:
            level = "ok" if getattr(value, "ok", True) else "error"

    text = "" if text is None else str(text)
    level = str(level or "ok").lower()
    if level == "warn":
        return "WARNING %s" % (text,)
    if level == "error":
        return "ERROR %s" % (text,)
    return text


def result_payload(value: Any) -> Dict[str, Any]:
    """The same Result as JSON for the UI: ok/level/message plus any detail."""
    rendered = render_result(value)
    level = "ok"
    ok = True
    detail: Any = None
    if isinstance(value, dict):
        ok = bool(value.get("ok", True))
        level = str(value.get("level") or ("ok" if ok else "error"))
        detail = value.get("detail")
    elif isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool):
        ok = bool(value[0])
        level = "ok" if ok else "error"
    elif not isinstance(value, (str, bool, type(None))):
        ok = bool(getattr(value, "ok", True))
        level = str(getattr(value, "level", None) or ("ok" if ok else "error"))
        detail = getattr(value, "detail", None)
    elif isinstance(value, bool):
        ok = value
        level = "ok" if value else "error"

    # The prefix belongs to the text protocol; JSON callers have `level`.
    message = rendered
    for prefix in ("WARNING ", "ERROR "):
        if message.startswith(prefix):
            message = message[len(prefix):]
            break
    payload: Dict[str, Any] = {"ok": ok, "level": level, "message": message, "text": rendered}
    if detail not in (None, {}, []):
        payload["detail"] = detail
    return payload


def _module_attr(obj: Any, name: str) -> Any:
    """A module-level attribute of the module that defines ``type(obj)``.

    slideshow.py's ``parse_multipart`` / ``classify_upload`` / ``safe_filename``
    / ``PAGE_VERSION`` are specified as module-level names (8.4, 8.5, 8.8f), not
    as methods, and webapp is handed an instance. This reaches them without
    importing slideshow, which would break routing tests before it exists.
    """
    found = getattr(obj, name, None)
    if found is not None:
        return found
    module = sys.modules.get(getattr(type(obj), "__module__", ""), None)
    return getattr(module, name, None) if module is not None else None


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #


class Response:
    """One HTTP reply. Always fully buffered, so Content-Length is always known."""

    __slots__ = ("status", "body", "content_type", "cache", "headers")

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        content_type: str = TEXT_TYPE,
        cache: str = _NO_STORE,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status = int(status)
        self.body = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        self.content_type = content_type
        self.cache = cache
        self.headers: Dict[str, str] = dict(headers or {})

    # -- constructors ------------------------------------------------------- #

    @classmethod
    def text(cls, body: str, status: int = 200) -> "Response":
        """A controller reply. Always ends in a newline (9.5)."""
        payload = "" if body is None else str(body)
        if not payload.endswith("\n"):
            payload += "\n"
        return cls(status, payload.encode("utf-8"), TEXT_TYPE, _NO_STORE)

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        # ensure_ascii so a photo filename in any script survives a controller
        # that ignores charset; default=str so a Path or a Result cannot 500 a
        # read-only endpoint.
        blob = json.dumps(payload, ensure_ascii=True, default=str, sort_keys=False)
        return cls(status, blob.encode("ascii"), JSON_TYPE, _NO_STORE)

    @classmethod
    def html(cls, body: str, status: int = 200, cache: str = _NO_STORE) -> "Response":
        return cls(status, str(body).encode("utf-8"), HTML_TYPE, cache)

    @classmethod
    def file(cls, path: Path, content_type: str, cache: str) -> "Response":
        """Serve a file from disk, or 404 when it has gone."""
        try:
            body = Path(path).read_bytes()
        except (OSError, ValueError):
            return cls.error(404, "not found")
        return cls(200, body, content_type, cache)

    @classmethod
    def redirect(cls, location: str) -> "Response":
        return cls(302, b"", TEXT_TYPE, _NO_STORE, {"Location": str(location)})

    @classmethod
    def error(
        cls,
        status: int,
        message: str,
        as_json: bool = False,
        headers: Optional[Dict[str, str]] = None,
    ) -> "Response":
        if as_json:
            blob = json.dumps({"error": str(message)}, ensure_ascii=True)
            return cls(status, blob.encode("ascii"), JSON_TYPE, _NO_STORE, headers)
        return cls(status, (str(message) + "\n").encode("utf-8"), TEXT_TYPE, _NO_STORE, headers)

    # -- wire --------------------------------------------------------------- #

    def header_items(self) -> List[Tuple[str, str]]:
        """9.1 - the headers EVERY response carries, plus any extras."""
        items = [
            ("Content-Type", self.content_type),
            ("Content-Length", str(len(self.body))),
            ("Cache-Control", self.cache),
            ("X-Content-Type-Options", "nosniff"),
            ("Connection", "close"),
        ]
        for key, value in self.headers.items():
            if key.lower() not in ("content-type", "content-length", "cache-control", "connection"):
                items.append((key, str(value)))
        return items


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #


@dataclass
class Request:
    """A parsed request. Deliberately socket-free so routing is unit-testable."""

    method: str
    path: str
    segments: List[str]
    raw_segments: List[str]
    query: Dict[str, List[str]] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    client_ip: str = "127.0.0.1"
    body: bytes = b""

    @classmethod
    def from_target(
        cls,
        method: str,
        target: str,
        headers: Optional[Mapping[str, str]] = None,
        client_ip: str = "127.0.0.1",
        body: bytes = b"",
    ) -> "Request":
        """Parse a request target ("/tv/a%2Fb/on?x=1") into a Request.

        9.2 - the path is split FIRST and each segment unquoted separately.
        Unquoting the whole path first would let an encoded slash inside a
        playlist name or filename split into two segments and reach a different
        route than the one the client asked for.
        """
        split = urlsplit(target or "/")
        raw_path = split.path or "/"
        raw_segments = [s for s in raw_path.split("/") if s]
        segments = [unquote(s) for s in raw_segments]
        query = parse_qs(split.query, keep_blank_values=True) if split.query else {}
        return cls(
            method=(method or "GET").upper(),
            path=raw_path,
            segments=segments,
            raw_segments=raw_segments,
            query=query,
            headers=headers if headers is not None else {},
            client_ip=client_ip or "",
            body=body or b"",
        )

    def q(self, key: str, default: str = "") -> str:
        values = self.query.get(key)
        if not values:
            return default
        return values[0]

    def json_body(self) -> dict:
        """The body as a JSON object; {} when absent, malformed, or not an object."""
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def header(self, name: str, default: str = "") -> str:
        try:
            value = self.headers.get(name)
        except AttributeError:  # pragma: no cover - defensive
            return default
        if value is None:
            # email.message.Message is case-insensitive but a plain dict is not.
            lowered = name.lower()
            for key, candidate in dict(self.headers).items():
                if str(key).lower() == lowered:
                    return str(candidate)
            return default
        return str(value)


class RequestError(Exception):
    """A routing/validation failure that maps straight onto a status code."""

    def __init__(self, status: int, message: str, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.headers = dict(headers or {})


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


class _Plan:
    """A resolved controller command: which TVs, which verb, which argument."""

    __slots__ = ("kind", "name", "aliases", "verb", "arg")

    def __init__(self, kind: str, name: str, aliases: List[str], verb: str, arg: Optional[str]) -> None:
        self.kind = kind
        self.name = name
        self.aliases = aliases
        self.verb = verb
        self.arg = arg

    @property
    def single(self) -> bool:
        return self.kind == "tv"


class App:
    """Routing and reply formatting. No sockets, no device I/O, no HTML."""

    def __init__(self, ctx: "Context", fleet: "Fleet", slideshow: "Slideshow", ui: "UI") -> None:
        self.ctx = ctx
        self.fleet = fleet
        self.slideshow = slideshow
        self.ui = ui
        self._started = time.monotonic()
        # The job id of the most recent scan, for GET /api/discover. Instance
        # state, not a module global (0.4).
        self._scan_job: Optional[str] = None

    # ------------------------------------------------------------------ gates

    def gate(self, request: Request) -> str:
        """9.3 - which allow-list applies to this path."""
        segments = request.segments
        if not segments:
            return "view"  # "/" is the dashboard
        head = segments[0]
        if head in PUBLIC_ROOTS:
            return "public"
        if head == "api":
            # Reads are a VIEW; anything that mutates is ADMIN. Splitting on the
            # method rather than on a route list means a new write route cannot
            # accidentally ship ungated.
            return "view" if request.method in ("GET", "HEAD") else "admin"
        if head == "ui":
            return "view"
        if head in CONTROL_ROOTS:
            return "control"
        return "view"

    def permitted(self, request: Request, gate: str) -> bool:
        """Empty list = any source. Loopback always passes (3.3)."""
        if gate == "public":
            return True
        ip = _normalize_ip(request.client_ip)
        # 3.3 - loopback is never refused, so a wrong allow_from entry can be
        # fixed with a local /reload instead of a service restart.
        if ip in ("127.0.0.1", "::1") or ip.startswith("127."):
            return True
        config = self.ctx.config
        patterns = config.allow_from() if gate == "control" else config.admin_from()
        return _ip_matches(ip, patterns)

    # ----------------------------------------------------------------- routing

    def handle(self, request: Request) -> Response:
        """The whole route table. Pure: no socket, no global state."""
        as_json = request.segments[:1] == ["api"]
        try:
            gate = self.gate(request)
            if not self.permitted(request, gate):
                log.warning(
                    "refused %s %s from %s (%s gate)",
                    request.method,
                    request.path,
                    request.client_ip,
                    gate,
                )
                return Response.error(403, "forbidden", as_json)
            return self._dispatch(request)
        except RequestError as exc:
            return Response.error(exc.status, exc.message, as_json, exc.headers or None)
        except ConfigError as exc:
            # A rejected config edit is the caller's fault, not a server fault.
            return Response.error(400, str(exc), as_json)
        except Exception:
            log.error("unhandled error for %s %s\n%s", request.method, request.path, traceback.format_exc())
            return Response.error(500, "internal error - see state/tvhub.log", as_json)

    def _dispatch(self, request: Request) -> Response:
        segments = request.segments
        if not segments:
            return self._ui_root(request)
        head = segments[0]

        if head == "health":
            self._require(request, "GET", "POST")
            return Response.text("ok")
        if head == "favicon.ico":
            self._require(request, "GET")
            return self._asset("icon.svg", _ASSET_CACHE)
        if head == "slideshow":
            return self._slideshow(request)
        if head == "api":
            return self._api(request)
        if head == "ui":
            return self._ui(request)
        if head in ("tv", "group", "all"):
            return self._controller(request)
        if head == "playlist":
            return self._route_playlist(request)
        if head == "playlists":
            self._require(request, "GET", "POST")
            return Response.text(self._playlists_text())
        if head == "identify":
            return self._route_identify(request)
        if head == "homepages":
            self._require(request, "GET", "POST")
            return Response.text(self._homepages_text(request))
        if head == "reload":
            self._require(request, "GET", "POST")
            return Response.text(render_result(self._reload()))
        if head == "x":
            return self._route_x(request)
        raise RequestError(404, "no such route: %s %s" % (request.method, request.path))

    @staticmethod
    def _require(request: Request, *methods: str) -> None:
        """405 for a known path with the wrong method, with an Allow header."""
        allowed = set(methods)
        if "GET" in allowed:
            allowed.add("HEAD")
        if request.method in allowed:
            return
        raise RequestError(
            405,
            "method %s is not allowed here" % (request.method,),
            {"Allow": ", ".join(sorted(allowed))},
        )

    def route_table(self) -> List[dict]:
        """9.5 as data - serves GET /api/routes and the README."""
        return [
            {"method": method, "path": path, "gate": gate, "returns": returns, "summary": summary}
            for method, path, gate, returns, summary in _ROUTES
        ]

    # ------------------------------------------------------- controller routes

    def _controller(self, request: Request) -> Response:
        """`/tv/<alias>/<verb>`, `/group/<name>/<verb>`, `/all/<verb>`."""
        self._require(request, "GET", "POST")
        plan = self._plan(request.segments)
        return Response.text(self._run_plan(plan))

    def _plan(self, segments: Sequence[str], allow_bare: bool = False) -> _Plan:
        """Resolve a controller path into (targets, verb, arg), validating both.

        Raises RequestError(404) for an unknown route/alias/group/playlist and
        RequestError(400) for a malformed argument, so a typo is reported at the
        HTTP layer instead of arriving at a TV.
        """
        segments = list(segments)
        if not segments:
            raise RequestError(404, "no target given")
        head = segments[0]

        if head == "all":
            kind, name, rest = "group", "all", segments[1:]
        elif head in ("tv", "group"):
            if len(segments) < 2:
                raise RequestError(404, "no %s named in the path" % ("TV" if head == "tv" else "group",))
            kind, name, rest = head, segments[1], segments[2:]
        elif allow_bare:
            # 7.11 - a bare target, as the CLI and /x/ accept. A TV alias ALWAYS
            # beats a group of the same name (I12), which is what Fleet.resolve
            # guarantees; naming a TV "office" must not command an "office" group
            # somewhere else.
            kind, name, rest = "bare", head, segments[1:]
        else:
            raise RequestError(404, "no such route: /%s" % ("/".join(segments),))

        if not rest:
            raise RequestError(404, "no verb given - expected one of: %s" % (", ".join(sorted(VERBS)),))
        verb = rest[0].lower()
        if verb not in _VERB_ARG:
            raise RequestError(404, "unknown verb %r - expected one of: %s" % (rest[0], ", ".join(sorted(VERBS))))
        arg = "/".join(rest[1:]) if len(rest) > 1 else None
        arg = self._check_arg(verb, arg)

        if kind == "tv":
            if self.ctx.config.tv(name) is None:
                raise RequestError(404, "unknown TV %r" % (name,))
            return _Plan("tv", name, [name], verb, arg)
        if kind == "group":
            aliases = self._group_members(name)
            return _Plan("group", name, aliases, verb, arg)

        aliases = self._resolve_bare(name)
        # A single alias resolved from a bare target behaves like /tv/<alias>.
        kind = "tv" if aliases == [name] and self.ctx.config.tv(name) is not None else "group"
        return _Plan(kind, name, aliases, verb, arg)

    def _check_arg(self, verb: str, arg: Optional[str]) -> Optional[str]:
        """Validate a verb's argument. 400 on malformed, 404 on unknown playlist."""
        spec = _VERB_ARG[verb]
        optional = spec.endswith("?")
        base = spec[:-1] if optional else spec

        if base == "none":
            if arg:
                raise RequestError(404, "%s takes no argument" % (verb,))
            return None
        if arg is None or arg == "":
            if optional:
                return None
            raise RequestError(400, "%s needs an argument" % (verb,))

        if base == "volume":
            low = arg.strip().lower()
            if low in ("up", "down"):
                return low
            try:
                level = int(arg.strip())
            except ValueError:
                raise RequestError(400, "volume must be 0-100, up or down")
            if not 0 <= level <= 100:
                raise RequestError(400, "volume must be 0-100, up or down")
            return str(level)
        if base == "onoff":
            low = arg.strip().lower()
            if low not in ("on", "off"):
                raise RequestError(400, "expected on or off")
            return low
        if base == "key":
            _check_key_sequence(arg, single=True)
            return arg
        if base == "keys":
            _check_key_sequence(arg, single=False)
            return arg
        if base == "playlist":
            if not valid_playlist(arg):
                raise RequestError(404, "unknown playlist %r" % (arg,))
            if not self._playlist_exists(arg):
                raise RequestError(404, "unknown playlist %r" % (arg,))
            return arg
        if base == "int":
            try:
                return str(int(arg.strip()))
            except ValueError:
                raise RequestError(400, "%s expects a number of seconds" % (verb,))
        # "name": a macro or app id. Unknown names are the module's to report,
        # since a macro may be per-TV and an app id is only known to the TV.
        if not arg.strip():
            raise RequestError(400, "%s needs an argument" % (verb,))
        return arg

    def _run_plan(self, plan: _Plan) -> str:
        """Execute a plan synchronously and render it per 0.8."""
        if plan.single:
            act = _attr(self.fleet, "act")
            if act is None:
                raise RequestError(500, "fleet.act is missing")
            return render_result(
                _flex_call(
                    act,
                    # "tv"/"args" as well as "alias"/"arg", because the argument
                    # must reach the verb by SOME accepted name or a route like
                    # show/<playlist> silently degrades into a bare show.
                    # "args" is a LIST: a receiver that normalises with
                    # list(args or []) turns the bare string "winter" into
                    # ['w','i','n',...] and then selects a playlist called "w".
                    {
                        "alias": plan.aliases[0],
                        "tv": plan.aliases[0],
                        "verb": plan.verb,
                        "arg": plan.arg,
                        "args": _arg_list(plan.arg),
                    },
                    positional=(plan.aliases[0], plan.verb, plan.arg),
                )
            )
        if not plan.aliases:
            return "WARNING %s has no enabled TVs" % (
                "group %r" % (plan.name,) if plan.name != "all" else "the fleet",
            )
        return self._render_group(plan)

    def _render_group(self, plan: _Plan) -> str:
        """One `[alias] <rendered>` line per TV, in alias order (0.8)."""
        run = _attr(self.fleet, "run")
        if run is None:
            raise RequestError(500, "fleet.run is missing")
        results = _flex_call(
            run,
            {
                "aliases": plan.aliases,
                "verb": plan.verb,
                "arg": plan.arg,
                "args": _arg_list(plan.arg),
            },
            # A fan-out runner mirrors run(aliases, verb, args), so the
            # positional form gets the list too.
            positional=(plan.aliases, plan.verb, _arg_list(plan.arg)),
        )
        by_alias = _as_alias_map(plan.aliases, results)
        # Fleet's own renderer when it has one, so group formatting has a single
        # source. It produces the same "[alias] <rendered>" in alias order (0.8).
        renderer = _attr(self.fleet, "render")
        if renderer is not None and isinstance(results, dict):
            try:
                rendered = renderer(results)
                if isinstance(rendered, str) and rendered:
                    return rendered
            except Exception:
                log.debug("fleet.render failed; formatting here instead", exc_info=True)
        return "\n".join("[%s] %s" % (alias, render_result(by_alias[alias])) for alias in sorted(by_alias))

    # ---------------------------------------------------------- /playlist, /x/

    def _route_playlist(self, request: Request) -> Response:
        self._require(request, "GET", "POST")
        segments = request.segments
        if len(segments) < 2:
            raise RequestError(404, "no playlist named in the path")
        name = "/".join(segments[1:])
        if not valid_playlist(name) or not self._playlist_exists(name):
            raise RequestError(404, "unknown playlist %r" % (name,))
        return Response.text(render_result(self._activate(name)))

    def _route_identify(self, request: Request) -> Response:
        self._require(request, "GET", "POST")
        segments = request.segments
        if len(segments) < 2 or segments[1].lower() not in ("on", "off"):
            raise RequestError(404, "expected /identify/on or /identify/off")
        on = segments[1].lower() == "on"
        return Response.text(render_result(self._set_identify(on)))

    def _route_x(self, request: Request) -> Response:
        """9.7 - fire-and-forget. Returns as soon as the job is registered."""
        self._require(request, "GET", "POST")
        raw = "/".join(request.raw_segments[1:])
        if not raw:
            raise RequestError(404, "no command given after /x/")
        inner_path = "/".join(request.segments[1:])
        inner_segments = request.segments[1:]

        head = inner_segments[0]
        if head in _X_FORBIDDEN_ROOTS:
            raise RequestError(404, "/x/ takes a controller path, e.g. /x/tv/<alias>/on")
        plan: Optional[_Plan] = None
        if head not in _X_GENERIC_ROOTS:
            # Pre-validate so /x/tv/typo/on 404s NOW instead of failing silently
            # inside a job nobody is watching.
            plan = self._plan(inner_segments, allow_bare=True)

        # Carry the query string through, so /x/tv/a/show?s=30 behaves like the
        # direct route it wraps.
        query = _query_string(request.query)
        inner_target = "/" + raw + (("?" + query) if query else "")
        captured_plan = plan
        method = request.method
        client_ip = request.client_ip
        headers = dict(request.headers) if request.headers else {}
        body = request.body

        def work(handle: Any) -> Any:
            handle.step(inner_path)
            if captured_plan is not None:
                text = self._run_plan(captured_plan)
            else:
                # handle(), not _dispatch(): the wrapped route is re-gated
                # against the SAME client, so /x/ can never widen what an
                # allow-list permits.
                inner = Request.from_target(method, inner_target, headers, client_ip, body)
                text = self.handle(inner).body.decode("utf-8", "replace").rstrip("\n")
            for line in text.splitlines():
                handle.log(line)
            handle.set_result(text)
            if captured_plan is not None:
                healed = self._chain_heal(captured_plan.aliases, captured_plan.verb)
                if healed:
                    handle.step("healing (job %s)" % healed)
            return text

        # Plain start, not start_exclusive: a controller that retries an
        # identical command must not be told "already running" when the previous
        # attempt is what it is retrying. Per-TV locks in fleet serialise the
        # device I/O anyway.
        job_id = self.ctx.jobs.start("cmd", inner_path, work)
        return Response.text("started %s (job %s)" % (inner_path, job_id))

    # -------------------------------------------------------- slideshow routes

    def _slideshow(self, request: Request) -> Response:
        """The TV-facing routes. Public, and never gated (9.3).

        Locking the bridge down to one controller must not blank every screen,
        so allow_from/admin_from are not consulted here.
        """
        self._require(request, "GET")
        segments = request.segments
        if len(segments) < 3:
            raise RequestError(404, "no such route: %s" % (request.path,))
        kind, who, rest = segments[1], segments[2], segments[3:]
        alias: Optional[str] = None

        if kind == "live":
            if who == "all":
                playlist = self._shared_playlist()
            else:
                if self.ctx.config.tv(who) is None:
                    raise RequestError(404, "unknown TV %r" % (who,))
                alias = who
                playlist = self._resolve_for(who)
        elif kind == "p":
            # A spot check names its playlist, so 404 is the honest answer here -
            # unlike the live routes, where a TV is depending on us.
            if not valid_playlist(who) or not self._playlist_exists(who):
                raise RequestError(404, "unknown playlist %r" % (who,))
            playlist = who
        else:
            raise RequestError(404, "no such route: %s" % (request.path,))

        base = "/" + "/".join(request.raw_segments[:3]) + "/"
        wants_image = bool(rest) and rest[0] == "img" and len(rest) == 2
        if rest and rest != ["manifest.json"] and not wants_image:
            raise RequestError(404, "no such route: %s" % (request.path,))

        # 5.1/I7 - a fetch here is the ONLY evidence a TV is displaying the
        # slideshow, so it is recorded for every one of the three real shapes,
        # including an image (a TV asking for photos has the page running).
        # Deliberately AFTER the route is known to be one of them: crediting a
        # 404 from a stale URL would make a TV showing the browser's own error
        # page read as "playing" for the next 90 seconds.
        self.ctx.heartbeat.note(request.client_ip)

        if not rest:
            return self._slideshow_page(request, playlist, alias, base)
        if rest == ["manifest.json"]:
            return self._slideshow_manifest(request, playlist, alias, base)
        return self._slideshow_image(playlist, rest[1])

    def _slideshow_page(self, request: Request, playlist: str, alias: Optional[str], base: str) -> Response:
        page = _attr(self.slideshow, "page_html", "page", "render_page", "render", "html")
        if page is None:
            raise RequestError(500, "slideshow.page_html is missing - cannot render the TV page")
        seconds = self._interval(alias, request)
        fit = self._fit(alias, request)
        rendered = _flex_call(
            page,
            {
                "playlist": playlist,
                "name": playlist,
                "base": base,
                "base_path": base,
                "seconds": seconds,
                "interval": seconds,
                "interval_seconds": seconds,
                "fit": fit,
                "title": playlist,
                "alias": alias,
            },
            positional=(playlist, base, seconds, fit),
        )
        return Response.html(rendered, cache=_NO_STORE)

    def _slideshow_manifest(self, request: Request, playlist: str, alias: Optional[str], base: str) -> Response:
        seconds = self._interval(alias, request)
        fit = self._fit(alias, request)
        payload: Any = None
        manifest = _attr(self.slideshow, "manifest", "manifest_for", "manifest_json")
        if manifest is not None:
            # 7.15 - identify is keyed off the REQUESTING IP, because every TV
            # shares one homepage URL and that is the only thing distinguishing
            # them. The map lives on fleet, which slideshow may not import (0.5),
            # so webapp is what carries it across. Passing None means "identify
            # is off" and renders a null.
            payload = _flex_call(
                manifest,
                {
                    "playlist": playlist,
                    "name": playlist,
                    "base": base,
                    "client_ip": request.client_ip,
                    "ip": request.client_ip,
                    "identify_map": self._identify_map() if self._identify_on() else None,
                    "seconds": seconds,
                    "interval": seconds,
                    "interval_seconds": seconds,
                    "fit": fit,
                    "alias": alias,
                },
                positional=(playlist, request.client_ip, None),
            )
        if not isinstance(payload, dict):
            payload = {
                "playlist": playlist,
                "images": self._manifest_images(playlist),
            }
        payload.setdefault("playlist", playlist)
        payload.setdefault("images", [])
        # Overridden rather than defaulted: these are the only per-TV values in
        # the document, and a manifest renderer that takes just a playlist name
        # cannot know the alias, its interval_seconds/fit options, or a ?s=/?fit=
        # spot-check override. The page reads the interval from HERE, so without
        # this a per-TV interval would silently do nothing.
        payload["interval"] = seconds
        payload["fit"] = fit
        # 8.8f - a string, and the page reloads itself when it changes.
        payload["page"] = str(payload.get("page") or self._page_version())
        # Set unconditionally, even though the renderer above was handed the map:
        # this is the value webapp knows to be right for THIS requesting IP, it is
        # identical to what a renderer that used the map would produce, and it
        # also corrects one that accepted the map and ignored it.
        payload["identify"] = self._identify_entry(request.client_ip)
        payload["server_time"] = float(payload.get("server_time") or time.time())
        return Response.json(payload)

    def _slideshow_image(self, playlist: str, filename: str) -> Response:
        path = None
        finder = _attr(self.slideshow, "image_path", "image", "resolve_image")
        if finder is not None:
            path = _flex_call(
                finder,
                {"playlist": playlist, "name": playlist, "filename": filename, "file": filename},
                positional=(playlist, filename),
            )
        if path is None:
            folder = self._playlist_dir(playlist)
            path = _safe_child(folder, filename) if folder is not None else None
        if path is None:
            raise RequestError(404, "no such image")
        path = Path(path)
        if path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
            raise RequestError(404, "no such image")
        ctype = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return Response.file(path, ctype, _IMAGE_CACHE)

    def _manifest_images(self, playlist: str) -> List[str]:
        """8.7 - paths RELATIVE to the page's base, percent-encoded."""
        ready = _attr(self.slideshow, "images")
        if ready is not None:
            try:
                rows = _flex_call(ready, {"name": playlist, "playlist": playlist}, positional=(playlist,))
                if isinstance(rows, list) and all(isinstance(r, str) for r in rows):
                    return list(rows)
            except Exception:
                log.debug("slideshow.images(%r) failed", playlist, exc_info=True)
        return ["img/" + quote(name, safe="") for name in self._image_names(playlist)]

    def _image_names(self, playlist: str) -> List[str]:
        """RAW filenames, for building thumbnail URLs and delete links.

        Deliberately NOT slideshow.images(): that one returns page-relative,
        percent-encoded "img/<file>" paths for the manifest. Feeding those into a
        URL builder double-prefixes them, which breaks every thumbnail and every
        per-image delete on the photos page. Anything that still arrives looking
        like a path is reduced back to its filename.
        """
        listing = _attr(self.slideshow, "image_names", "list_images", "filenames")
        names: List[str] = []
        if listing is not None:
            try:
                rows = _flex_call(listing, {"playlist": playlist, "name": playlist}, positional=(playlist,))
            except Exception:  # a missing folder must not break a polling TV
                rows = None
            for row in rows or []:
                if isinstance(row, str):
                    names.append(row)
                elif isinstance(row, dict):
                    candidate = row.get("filename") or row.get("name")
                    if candidate:
                        names.append(str(candidate))
                else:
                    candidate = getattr(row, "filename", None) or getattr(row, "name", None)
                    if candidate:
                        names.append(str(candidate))
        if names:
            return [unquote(n.rsplit("/", 1)[-1]) for n in names]
        folder = self._playlist_dir(playlist)
        if folder is None:
            return []
        try:
            entries = [p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
        except OSError:
            return []
        # Filename order is the display order the photos page advertises.
        return sorted(entries, key=lambda n: (n.lower(), n))

    def _page_version(self) -> str:
        value = _module_attr(self.slideshow, "PAGE_VERSION")
        return str(value) if value not in (None, "") else "1"

    def _interval(self, alias: Optional[str], request: Request) -> int:
        """?s wins, then the per-TV option, then the fleet default."""
        raw = request.q("s")
        if raw:
            try:
                # Clamped, never rejected: a 400 here would leave a TV on a dead
                # page it cannot navigate away from without a human and a remote.
                return max(2, int(float(raw)))
            except ValueError:
                pass
        value = self.ctx.config.tv_option(alias or "", "interval_seconds", 10)
        try:
            return max(2, int(value))
        except (TypeError, ValueError):
            return 10

    def _fit(self, alias: Optional[str], request: Request) -> str:
        raw = (request.q("fit") or "").strip().lower()
        if raw in FIT_CHOICES:
            return raw
        value = self.ctx.config.tv_option(alias or "", "fit", "contain")
        return value if value in FIT_CHOICES else "contain"

    # --------------------------------------------------------------- UI routes

    def _ui_root(self, request: Request) -> Response:
        self._require(request, "GET")
        info = self._setup_info()
        # 9.5 - a fresh install lands on the wizard, but only while it is fresh:
        # once a human has finished (or dismissed) it, "/" is the dashboard even
        # with no TVs, or the wizard becomes impossible to leave.
        if info["tv_count"] == 0 and not info["wizard_done"]:
            return Response.redirect("/ui/setup")
        return self._page("dashboard", request)

    def _ui(self, request: Request) -> Response:
        segments = request.segments[1:]
        if not segments:
            self._require(request, "GET")
            return self._page("dashboard", request)
        head = segments[0]
        if head == "assets":
            self._require(request, "GET")
            if len(segments) != 2:
                raise RequestError(404, "no such asset")
            return self._asset(segments[1], _ASSET_CACHE)
        if head in ("setup", "photos"):
            self._require(request, "GET")
            return self._page(head, request)
        if head == "tv":
            self._require(request, "GET")
            if len(segments) < 2:
                raise RequestError(404, "no TV named in the path")
            alias = segments[1]
            if self.ctx.config.tv(alias) is None:
                raise RequestError(404, "unknown TV %r" % (alias,))
            return self._page("remote", request, alias=alias)
        raise RequestError(404, "no such route: %s" % (request.path,))

    def _asset(self, name: str, cache: str) -> Response:
        """Assets come from UI, which owns tvhub/web and its traversal checks."""
        loader = _attr(self.ui, "asset")
        if loader is not None:
            got = loader(name)
            if got:
                body, ctype = got[0], got[1]
                return Response(200, body, ctype, cache)
            raise RequestError(404, "no such asset")
        web_dir = Path(getattr(self.ctx.paths, "web_dir", Path(__file__).resolve().parent / "web"))
        path = _safe_child(web_dir, name)
        if path is None or not path.is_file():
            raise RequestError(404, "no such asset")
        return Response.file(path, _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"), cache)

    def _page(self, name: str, request: Request, alias: Optional[str] = None) -> Response:
        """Render one admin page. All page HTML lives in ui.py (10.1)."""
        page = _attr(self.ui, "page")
        if page is None:
            raise RequestError(500, "ui.page is missing - cannot render the admin UI")
        boot = self._boot(name, request, alias)
        rendered = _flex_call(page, {"name": name, "boot": boot, "page": name}, positional=(name, boot))
        return Response.html(rendered)

    def _boot(self, name: str, request: Request, alias: Optional[str]) -> Dict[str, Any]:
        """Hints embedded as window.BOOT. Pages re-fetch everything anyway, so
        this only has to make the first paint useful."""
        homepages = self._homepages(request)
        boot: Dict[str, Any] = {
            "page": name,
            "homepage_url": homepages["homepage_url"],
            "base_url": homepages["base_url"],
            "base_url_set": homepages["base_url_set"],
        }
        if name == "dashboard":
            boot["status"] = self._status_payload()
        elif name == "setup":
            boot["setup"] = self._setup_payload()
            boot["homepages"] = homepages
            boot["local_ips"] = local_ipv4_addresses()
            boot["playlists"] = self._playlists_payload()
        elif name == "photos":
            boot["playlists"] = self._playlists_payload()
        elif name == "remote" and alias:
            boot["alias"] = alias
            spec = self.ctx.config.tv(alias) or {}
            boot["label"] = spec.get("label") or alias
            boot["tv"] = self._tv_detail(alias)
            boot["macros"] = sorted(self.ctx.config.macros().keys())
            boot["homepage_url"] = homepages["per_tv"].get(alias, homepages["homepage_url"])
        return boot

    # -------------------------------------------------------------- JSON API

    def _api(self, request: Request) -> Response:
        segments = request.segments[1:]
        if not segments:
            raise RequestError(404, "no such route: %s" % (request.path,))
        head, rest = segments[0], segments[1:]

        if head == "status":
            self._require(request, "GET")
            return Response.json(self._status_payload())
        if head == "config":
            self._require(request, "GET")
            return Response.json(_strip_secrets(self.ctx.config.snapshot()))
        if head == "routes":
            self._require(request, "GET")
            return Response.json(self.route_table())
        if head == "homepages":
            self._require(request, "GET")
            return Response.json(self._homepages(request))
        if head == "setup":
            if request.method == "POST":
                return self._api_setup_post(request)
            self._require(request, "GET", "POST")
            return Response.json(self._setup_payload())
        if head == "reload":
            self._require(request, "POST")
            ok, message = _as_ok_message(self._reload())
            return Response.json({"ok": ok, "message": message})
        if head == "discover":
            return self._api_discover(request)
        if head == "identify":
            self._require(request, "POST")
            body = request.json_body()
            if "on" not in body:
                raise RequestError(400, "expected {'on': true|false}")
            result = self._set_identify(bool(body.get("on")))
            payload = result_payload(result)
            payload["identify"] = self._identify_on()
            return Response.json(payload)
        if head == "heal":
            self._require(request, "POST")
            return self._api_heal(request)
        if head == "jobs":
            self._require(request, "GET")
            if not rest:
                return Response.json(self.ctx.jobs.recent(40))
            job = self.ctx.jobs.get(rest[0])
            if job is None:
                raise RequestError(404, "unknown job %r" % (rest[0],))
            return Response.json(job)
        if head == "tvs":
            return self._api_tvs(request, rest)
        if head == "groups":
            return self._api_groups(request, rest)
        if head == "group":
            return self._api_group_action(request, rest)
        if head == "playlists":
            return self._api_playlists(request, rest)
        raise RequestError(404, "no such route: %s" % (request.path,))

    # -- /api/tvs ----------------------------------------------------------- #

    def _api_tvs(self, request: Request, rest: List[str]) -> Response:
        if not rest:
            if request.method == "POST":
                return self._api_tv_add(request)
            self._require(request, "GET", "POST")
            return Response.json([self._tv_roster_row(alias) for alias in self._aliases()])

        alias = rest[0]
        tail = rest[1:]
        known = self.ctx.config.tv(alias) is not None

        if not tail:
            # _require raises for anything else, so this branch always returns.
            self._require(request, "GET", "PATCH", "DELETE")
            if not known:
                raise RequestError(404, "unknown TV %r" % (alias,))
            if request.method in ("GET", "HEAD"):
                return Response.json(self._tv_detail(alias))
            if request.method == "PATCH":
                return self._api_tv_patch(request, alias)
            return Response.json(result_payload(self._call_fleet("remove_tv", alias)))

        if not known:
            raise RequestError(404, "unknown TV %r" % (alias,))
        action = tail[0]

        if action == "pair":
            self._require(request, "POST")
            wait = _as_int(request.json_body().get("wait"), 90, low=5, high=600)
            job_id, started = self._start_exclusive(
                "pair:%s" % alias, "pair", "pair %s" % alias,
                # job=/handle= so pairing progress ("press ALLOW on the TV") lands
                # in the job the wizard is polling; _call_fleet drops whichever
                # name this Fleet does not take.
                lambda handle: self._call_fleet("pair", alias, wait=wait, job=handle, handle=handle),
            )
            return Response.json({"job": job_id, "started": started})
        if action == "verify":
            self._require(request, "POST")
            job_id, started = self._start_exclusive(
                "verify:%s" % alias, "verify", "verify %s" % alias,
                lambda handle: self._act(alias, "verify", None),
            )
            return Response.json({"job": job_id, "started": started})
        if action == "unpair":
            self._require(request, "POST")
            return Response.json(result_payload(self._unpair(alias)))
        if action == "homepage":
            self._require(request, "POST")
            body = request.json_body()
            if "confirmed" not in body:
                raise RequestError(400, "expected {'confirmed': true|false}")
            confirmed = bool(body.get("confirmed"))
            self.ctx.state.set_homepage_confirmed(alias, confirmed)
            return Response.json(
                {
                    "ok": True,
                    "message": "homepage %s for %s" % ("confirmed" if confirmed else "cleared", alias),
                    "confirmed": self.ctx.state.homepage_confirmed(alias),
                }
            )
        if action == "key":
            self._require(request, "POST")
            if len(tail) < 2:
                raise RequestError(400, "no key given")
            key = "/".join(tail[1:])
            _check_key_sequence(key, single=True)
            # Synchronous on purpose: the on-screen remote and the macro recorder
            # need the press to have happened before the next tap (7.9, 10.6).
            return Response.json(result_payload(self._act(alias, "key", key)))
        if action == "action":
            self._require(request, "POST")
            if len(tail) < 2:
                raise RequestError(400, "no verb given")
            verb = tail[1].lower()
            if verb not in _VERB_ARG:
                raise RequestError(400, "unknown verb %r" % (tail[1],))
            arg = self._check_arg(verb, "/".join(tail[2:]) if len(tail) > 2 else None)
            plan = _Plan("tv", alias, [alias], verb, arg)
            job_id = self._start_command(plan)
            return Response.json({"job": job_id})
        raise RequestError(404, "no such route: %s" % (request.path,))

    def _api_tv_add(self, request: Request) -> Response:
        body = request.json_body()
        alias = str(body.get("alias") or "").strip().lower()
        ip = str(body.get("ip") or "").strip()
        if not valid_alias(alias):
            # valid_alias already excludes RESERVED_NAMES, so the reason has to
            # name both conditions - a name like "all" matches the pattern and is
            # still refused, and blaming the pattern sends people in circles.
            raise RequestError(400, _bad_name("alias", alias))
        if not valid_ipv4(ip):
            raise RequestError(400, "ip must be a dotted quad")
        mac = normalize_mac(str(body.get("mac") or ""))
        label = str(body.get("label") or "").strip()
        result = self._call_fleet("add_tv", alias, ip, mac=mac, label=label)
        payload = result_payload(result)
        payload["alias"] = alias
        return Response.json(payload, 201 if payload.get("ok") else 400)

    def _api_tv_patch(self, request: Request, alias: str) -> Response:
        body = request.json_body()
        if not body:
            raise RequestError(400, "no fields to change")
        messages: List[str] = []
        ok = True

        if "ip" in body or "mac" in body:
            spec = self.ctx.config.tv(alias) or {}
            ip = str(body.get("ip", spec.get("ip", ""))).strip()
            if not valid_ipv4(ip):
                raise RequestError(400, "ip must be a dotted quad")
            mac_raw = body.get("mac", spec.get("mac", ""))
            mac = normalize_mac(str(mac_raw or ""))
            if mac_raw and not mac:
                raise RequestError(400, "mac must be six hex pairs, or empty")
            # 7.16 - keeps the token and re-verifies. Correcting a DHCP drift
            # must not force a re-pair.
            result = self._call_fleet("set_tv_ip", alias, ip, mac=mac)
            messages.append(render_result(result))
            ok = ok and _is_ok(result)

        if "label" in body or "options" in body:
            messages.append(render_result(self._patch_spec(alias, body)))

        new_alias = str(body.get("alias") or "").strip().lower()
        if new_alias and new_alias != alias:
            if not valid_alias(new_alias):
                raise RequestError(400, _bad_name("alias", new_alias))
            if new_alias in RESERVED_NAMES:
                raise RequestError(400, "%r is a reserved name" % (new_alias,))
            # Renaming last keeps the alias key stable while the edits above land.
            result = self._call_fleet("rename_tv", alias, new_alias)
            messages.append(render_result(result))
            ok = ok and _is_ok(result)
            alias = new_alias

        text = "; ".join(m for m in messages if m) or "nothing changed"
        return Response.json({"ok": ok, "message": text, "alias": alias})

    def _patch_spec(self, alias: str, body: Dict[str, Any]) -> Any:
        """Label/option edits go through Config.save, which validates them (7.16)."""
        label = body.get("label")
        options = body.get("options")
        if options is not None and not isinstance(options, dict):
            raise RequestError(400, "options must be an object")

        # Fleet owns option validation and the reload that follows it.
        setter = _attr(self.fleet, "set_tv_options")
        if options and setter is not None and label is None:
            return _flex_call(
                setter,
                {"alias": alias, "options": options},
                positional=(alias, options),
            )

        def mutate(document: Dict[str, Any]) -> None:
            spec = document.setdefault("tvs", {}).get(alias)
            if not isinstance(spec, dict):
                raise ConfigError("unknown TV %r" % (alias,))
            if label is not None:
                spec["label"] = str(label)
            if options:
                current = spec.setdefault("options", {})
                current.update(options)

        warnings = self.ctx.config.save(mutate)
        self._reload_fleet()
        text = "updated %s" % alias
        if warnings:
            return {"ok": True, "level": "warn", "text": "%s (%s)" % (text, "; ".join(warnings))}
        return text

    # -- /api/groups -------------------------------------------------------- #

    def _api_groups(self, request: Request, rest: List[str]) -> Response:
        if not rest:
            self._require(request, "GET")
            groups = dict(self.ctx.config.groups())
            groups["all"] = self._enabled_aliases()
            return Response.json(groups)
        name = rest[0].strip().lower()
        # A named group is only ever set or deleted; it is read through
        # GET /api/groups. HEAD is not folded in here, or it would fall past both
        # branches with nothing to return.
        self._require(request, "PUT", "DELETE")
        if request.method == "PUT":
            body = request.json_body()
            aliases = body.get("aliases")
            if not isinstance(aliases, list):
                raise RequestError(400, "expected {'aliases': [...]}")
            if not valid_group(name):
                # Same trap as the alias case: "all" matches the pattern and is
                # still refused, because the implicit group is every enabled TV
                # and must not be storable (3.5).
                raise RequestError(400, _bad_name("group name", name))
            members = [str(a).strip().lower() for a in aliases]
            return Response.json(result_payload(self._call_fleet("set_group", name, members)))
        if self.ctx.config.group(name) is None:
            raise RequestError(404, "unknown group %r" % (name,))
        return Response.json(result_payload(self._remove_group(name)))

    def _api_group_action(self, request: Request, rest: List[str]) -> Response:
        self._require(request, "POST")
        if len(rest) < 3 or rest[1] != "action":
            raise RequestError(404, "no such route: %s" % (request.path,))
        name = rest[0]
        verb = rest[2].lower()
        if verb not in _VERB_ARG:
            raise RequestError(400, "unknown verb %r" % (rest[2],))
        arg = self._check_arg(verb, "/".join(rest[3:]) if len(rest) > 3 else None)
        aliases = self._group_members(name)
        plan = _Plan("group", name, aliases, verb, arg)
        return Response.json({"job": self._start_command(plan)})

    # -- /api/playlists ----------------------------------------------------- #

    def _api_playlists(self, request: Request, rest: List[str]) -> Response:
        if not rest:
            if request.method == "POST":
                body = request.json_body()
                name = str(body.get("name") or "").strip()
                if not valid_playlist(name):
                    raise RequestError(400, "playlist names may use letters, digits, space, _ and -")
                result = self._create_playlist(name)
                payload = result_payload(result)
                payload["name"] = name
                return Response.json(payload, 201 if payload.get("ok") else 400)
            self._require(request, "GET", "POST")
            return Response.json(self._playlists_payload())

        name = rest[0]
        tail = rest[1:]
        if not valid_playlist(name):
            raise RequestError(404, "unknown playlist %r" % (name,))

        if not tail:
            self._require(request, "DELETE")
            if not self._playlist_exists(name):
                raise RequestError(404, "unknown playlist %r" % (name,))
            return Response.json(result_payload(self._delete_playlist(name)))
        if tail == ["activate"]:
            self._require(request, "POST")
            if not self._playlist_exists(name):
                raise RequestError(404, "unknown playlist %r" % (name,))
            result = self._activate(name)
            payload = result_payload(result)
            payload["shared"] = self.ctx.state.shared_playlist() or ""
            return Response.json(payload)
        if tail[0] == "images":
            if len(tail) == 1:
                if request.method == "POST":
                    return self._api_upload(request, name)
                self._require(request, "GET", "POST")
                if not self._playlist_exists(name):
                    raise RequestError(404, "unknown playlist %r" % (name,))
                return Response.json(self._images_payload(name))
            if len(tail) == 2:
                self._require(request, "DELETE")
                return Response.json(result_payload(self._delete_image(name, tail[1])))
        raise RequestError(404, "no such route: %s" % (request.path,))

    def _api_upload(self, request: Request, playlist: str) -> Response:
        """multipart/form-data upload, field name "files" (9.5, 8.4, 8.5)."""
        max_bytes = self.ctx.config.max_upload_bytes()
        if len(request.body) > max_bytes:
            raise RequestError(413, "upload is larger than the %d MB limit" % (max_bytes // (1024 * 1024)))
        ctype = request.header("Content-Type")
        if "multipart/form-data" not in ctype.lower():
            raise RequestError(400, "expected multipart/form-data with a 'files' field")
        if not self._playlist_exists(playlist):
            raise RequestError(404, "unknown playlist %r" % (playlist,))

        parse = _module_attr(self.slideshow, "parse_multipart")
        if not callable(parse):
            raise RequestError(500, "slideshow.parse_multipart is missing - cannot accept uploads")
        try:
            parts = _flex_call(
                parse,
                {"body": request.body, "content_type": ctype, "max_bytes": max_bytes},
                positional=(request.body, ctype, max_bytes),
            )
        except Exception as exc:  # LibraryError and friends: the client's fault
            raise RequestError(400, "upload could not be read (%s)" % (exc,))

        # Preferred: hand the parsed parts to slideshow, which owns the library.
        # It classifies, applies the collision suffix and does the staged write,
        # all of which must match what the rest of the photo library expects.
        adder = _attr(self.slideshow, "add_uploads", "add_images", "store_parts", "save_uploads")
        if adder is not None:
            try:
                out = _flex_call(
                    adder,
                    {"name": playlist, "playlist": playlist, "parts": parts},
                    positional=(playlist, parts),
                )
            except Exception as exc:
                raise RequestError(400, "upload rejected (%s)" % (exc,))
            if isinstance(out, dict) and ("added" in out or "rejected" in out):
                out.setdefault("added", [])
                out.setdefault("rejected", [])
                out.setdefault("count", len(self._image_names(playlist)))
                out.setdefault("added_count", len(out["added"]))
                return Response.json(out)

        folder = self._playlist_dir(playlist)
        if folder is None:
            raise RequestError(404, "unknown playlist %r" % (playlist,))
        added: List[str] = []
        rejected: List[Dict[str, str]] = []
        advice: List[str] = []
        for part in parts or []:
            filename, data = _part_fields(part)
            if not filename:
                continue  # an ordinary form field, not a file
            verdict, message, note = self._classify(filename, data, max_bytes)
            if note and note not in advice:
                advice.append(note)
            if verdict != "ok":
                rejected.append({"name": filename, "reason": verdict, "message": message})
                continue
            try:
                stored = self._store_image(folder, filename, data)
            except OSError as exc:
                rejected.append({"name": filename, "reason": "write-failed", "message": str(exc)})
                continue
            added.append(stored)

        if added:
            log.info("added %d image(s) to playlist %r", len(added), playlist)
        order = _module_attr(self.slideshow, "ORDER_ADVICE")
        if isinstance(order, str) and order and order not in advice:
            advice.append(order)
        return Response.json(
            {
                "added": added,
                "rejected": rejected,
                # The playlist's new total, which is what the photos page shows.
                "count": len(self._image_names(playlist)),
                "added_count": len(added),
                "advice": advice,
            }
        )

    def _classify(self, filename: str, data: bytes, max_bytes: int) -> Tuple[str, str, Optional[str]]:
        """(verdict, message, advice) - slideshow's classifier when it exists."""
        classify = _module_attr(self.slideshow, "classify_upload")
        if callable(classify):
            try:
                out = _flex_call(
                    classify,
                    {"filename": filename, "name": filename, "data": data, "max_bytes": max_bytes},
                    positional=(filename, data, max_bytes),
                )
            except Exception:
                out = None
            verdict, message, advice = _classify_fields(out)
            if verdict:
                return verdict, message, advice
        return _classify_locally(filename, data, max_bytes, self.slideshow)

    def _store_image(self, folder: Path, filename: str, data: bytes) -> str:
        """Write via state/tmp then os.replace, so a half-upload is never served."""
        kind = _sniff_image(data)
        final_name = self._safe_name(folder, filename, kind)
        tmp_dir = Path(getattr(self.ctx.paths, "tmp_dir", folder))
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            tmp_dir = folder
        staging = tmp_dir / ("upload-%s.part" % uuid.uuid4().hex)
        with open(staging, "wb") as handle:
            handle.write(data)
        try:
            os.replace(str(staging), str(folder / final_name))
        except OSError:
            try:
                staging.unlink()
            except OSError:
                pass
            raise
        return final_name

    def _safe_name(self, folder: Path, filename: str, kind: Optional[str]) -> str:
        helper = _module_attr(self.slideshow, "safe_filename")
        if callable(helper):
            try:
                out = _flex_call(
                    helper,
                    {
                        "filename": filename,
                        "name": filename,
                        "folder": folder,
                        "directory": folder,
                        "dest": folder,
                        "kind": kind,
                    },
                    positional=(filename, folder, kind),
                )
            except Exception:
                out = None
            if isinstance(out, str) and out:
                return out
        return _safe_filename_locally(folder, filename, kind)

    def _images_payload(self, name: str) -> Dict[str, Any]:
        folder = self._playlist_dir(name)
        rows: List[Dict[str, Any]] = []
        for filename in self._image_names(name):
            size = 0
            if folder is not None:
                try:
                    size = (folder / filename).stat().st_size
                except OSError:
                    size = 0
            rows.append(
                {
                    "filename": filename,
                    "bytes": size,
                    "url": "/slideshow/p/%s/img/%s" % (quote(name), quote(filename)),
                }
            )
        return {"name": name, "count": len(rows), "images": rows}

    def _playlists_payload(self) -> Dict[str, Any]:
        return {
            "playlists": self._playlists(),
            "shared": self.ctx.state.shared_playlist() or "",
            "default": str(self.ctx.config.slideshow().get("default_playlist") or "default"),
        }

    def _playlists(self) -> List[Dict[str, Any]]:
        listing = _attr(self.slideshow, "playlists", "list_playlists", "all_playlists")
        if listing is not None:
            try:
                rows = listing()
            except Exception:
                rows = None
            out: List[Dict[str, Any]] = []
            for row in rows or []:
                if isinstance(row, str):
                    out.append({"name": row, "count": len(self._image_names(row)), "bytes": 0, "modified": 0.0})
                elif isinstance(row, dict) and row.get("name"):
                    out.append(
                        {
                            "name": str(row["name"]),
                            "count": int(row.get("count", 0) or 0),
                            "bytes": int(row.get("bytes", 0) or 0),
                            "modified": float(row.get("modified", 0.0) or 0.0),
                        }
                    )
            if out:
                return out
        return self._scan_playlists()

    def _scan_playlists(self) -> List[Dict[str, Any]]:
        root = self.ctx.config.photo_root()
        rows: List[Dict[str, Any]] = []
        try:
            entries = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            return rows
        for folder in entries:
            if not valid_playlist(folder.name):
                continue
            count = 0
            total = 0
            newest = 0.0
            try:
                for item in folder.iterdir():
                    if item.is_file() and item.suffix.lower() in IMAGE_EXTS:
                        count += 1
                        try:
                            stat = item.stat()
                        except OSError:
                            continue
                        total += stat.st_size
                        newest = max(newest, stat.st_mtime)
            except OSError:
                pass
            rows.append({"name": folder.name, "count": count, "bytes": total, "modified": newest})
        return rows

    def _playlists_text(self) -> str:
        rows = self._playlists()
        if not rows:
            return "WARNING no playlists yet - upload some photos first"
        return "\n".join("%s: %d image(s)" % (row["name"], row["count"]) for row in rows)

    # -- /api/discover, /api/heal, /api/setup ------------------------------- #

    def _api_discover(self, request: Request) -> Response:
        if request.method == "POST":
            cidr = str(request.json_body().get("cidr") or "").strip() or None
            scan = _attr(self.fleet, "scan")
            if scan is None:
                raise RequestError(500, "fleet.scan is missing")

            def work(handle: Any) -> Any:
                handle.step("scanning")
                return _flex_call(
                    scan,
                    {"cidr": cidr, "handle": handle, "job": handle, "progress": handle},
                    positional=(cidr, handle),
                )

            # 5.3 - single-flight: two overlapping sweeps of the same /24 just
            # slow each other down.
            job_id, started = self.ctx.jobs.start_exclusive("scan", "discover", "find TVs", work)
            self._scan_job = job_id
            return Response.json({"job": job_id, "started": started})

        self._require(request, "GET", "POST")
        job_id = self._scan_job
        job = self.ctx.jobs.get(job_id) if job_id else None
        if job is None:
            return Response.json({"job": None, "state": None, "rows": [], "found": 0})
        rows = job.get("result")
        rows = rows if isinstance(rows, list) else []
        return Response.json(
            {
                "job": job.get("id"),
                "state": job.get("state"),
                "started_at": job.get("started_at"),
                "ended_at": job.get("ended_at"),
                "done": job.get("done"),
                "total": job.get("total"),
                "error": job.get("error"),
                "rows": rows,
                "found": len(rows),
            }
        )

    def _api_heal(self, request: Request) -> Response:
        body = request.json_body()
        aliases = body.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list):
                raise RequestError(400, "expected {'aliases': [...]}")
            targets = [str(a).strip().lower() for a in aliases]
            unknown = [a for a in targets if self.ctx.config.tv(a) is None]
            if unknown:
                raise RequestError(400, "unknown TV(s): %s" % ", ".join(unknown))
        else:
            targets = self._enabled_aliases()
        job_id, started = _job_ref(self._heal(targets))
        return Response.json({"job": job_id, "started": started})

    def _api_setup_post(self, request: Request) -> Response:
        body = request.json_body()
        if not body:
            raise RequestError(400, "no fields to change")
        messages: List[str] = []
        if "base_url" in body:
            base = str(body.get("base_url") or "").strip().rstrip("/")
            if base and not (base.startswith("http://") or base.startswith("https://")):
                raise RequestError(400, "base_url must start with http:// or https://")

            def mutate(document: Dict[str, Any]) -> None:
                document.setdefault("server", {})["base_url"] = base

            self.ctx.config.save(mutate)
            self._reload_fleet()
            # Kept honest against the config, so clearing base_url brings the
            # wizard step back.
            self.ctx.state.set_setup(base_url_set=bool(base))
            messages.append("server address set to %s" % base if base else "server address cleared")
        if "wizard_done" in body:
            done = bool(body.get("wizard_done"))
            self.ctx.state.set_setup(wizard_done=done)
            messages.append("setup marked %s" % ("done" if done else "unfinished"))
        payload = self._setup_payload()
        payload["ok"] = True
        payload["message"] = "; ".join(messages) or "nothing changed"
        return Response.json(payload)

    # ------------------------------------------------------------- payloads

    def _status_payload(self) -> Dict[str, Any]:
        """9.6 - the dashboard payload, exact keys."""
        config = self.ctx.config
        rows_by_alias, age = self._snapshot()
        identify_map = self._identify_map()
        rows = [self._tv_row(alias, rows_by_alias.get(alias), identify_map) for alias in self._aliases()]
        rows.sort(key=lambda row: (_STATE_RANK.get(str(row.get("state")), 7), row["alias"]))
        playlists = self._playlists()
        info = self._setup_info(playlists)
        groups = dict(config.groups())
        groups["all"] = self._enabled_aliases()
        warnings = list(getattr(config, "warnings", []) or [])
        for text in getattr(self.ctx.state, "warnings", []) or []:
            if text not in warnings:
                warnings.append(text)
        return {
            "server": {
                "version": VERSION,
                "base_url": config.base_url(),
                "base_url_set": config.base_url_configured(),
                "http_port": config.http_port(),
                "host": _hostname(),
                "platform": _platform(),
                "uptime_seconds": round(self._uptime(), 1),
                "config_warnings": warnings,
            },
            "setup": {
                "tv_count": info["tv_count"],
                "paired_count": info["paired_count"],
                "homepages_confirmed": info["homepages_confirmed"],
                "playlist_count": info["playlist_count"],
                "wizard_done": info["wizard_done"],
                "needs_setup": info["needs_setup"],
                "next_step": info["next_step"],
            },
            "playlist": {
                "shared": self.ctx.state.shared_playlist() or "",
                "default": str(config.slideshow().get("default_playlist") or "default"),
                "available": [{"name": row["name"], "count": row["count"]} for row in playlists],
            },
            "identify": self._identify_on(),
            "age_seconds": None if age is None else round(age, 1),
            "refresh_seconds": _as_int(config.healing().get("status_refresh_seconds"), 20, low=1, high=3600),
            "tvs": rows,
            "groups": groups,
            "jobs": [
                {
                    "id": job.get("id"),
                    "kind": job.get("kind"),
                    "title": job.get("title"),
                    "state": job.get("state"),
                    "step": job.get("step"),
                    "done": job.get("done"),
                    "total": job.get("total"),
                }
                for job in self.ctx.jobs.recent(12)
            ],
        }

    def _tv_row(
        self,
        alias: str,
        raw: Optional[Dict[str, Any]] = None,
        identify_map: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One /api/status.tvs row. Exact keys, whatever the snapshot lacks."""
        config = self.ctx.config
        state = self.ctx.state
        spec = config.tv(alias) or {}
        row = dict(raw) if isinstance(raw, dict) else {}
        ip = str(spec.get("ip") or row.get("ip") or "")
        client_name = config.client_name()

        busy_text, busy_left = self.ctx.activity.get(alias)
        status = str(row.get("state") or "") or None
        detail = row.get("detail")
        if busy_text:
            # 7.6 rule 1. Applied here as well as in the sweep so the dashboard
            # goes "busy" the moment a command starts, instead of looking frozen
            # until the next 20 s refresh (10.4).
            status = "busy"
            detail = busy_text
        heartbeat_age = row.get("heartbeat_age")
        if heartbeat_age is None and ip:
            heartbeat_age = self.ctx.heartbeat.age(ip)

        number = None
        if identify_map is not None:
            entry = identify_map.get(ip)
            number = _identify_number(entry)

        return {
            "alias": alias,
            "label": str(spec.get("label") or alias),
            "ip": ip,
            "mac": str(spec.get("mac") or row.get("mac") or ""),
            "power": row.get("power"),
            "model": row.get("model"),
            "frame": row.get("frame"),
            "paired": bool(state.is_paired(alias, client_name)),
            "verified_how": state.pairing(alias).get("verified_how"),
            "browser": row.get("browser"),
            "heartbeat_age": None if heartbeat_age is None else round(float(heartbeat_age), 1),
            "playlist": self._resolve_for(alias),
            "state": status,
            "detail": detail,
            "busy": busy_text,
            "busy_left": busy_left,
            "identify_number": number,
            "homepage_confirmed": bool(state.homepage_confirmed(alias)),
            "enabled": bool(spec.get("enabled", True)),
        }

    def _tv_roster_row(self, alias: str) -> Dict[str, Any]:
        """GET /api/tvs - the roster shape (9.5), which carries `options` and no
        status. Kept separate from the /api/status row on purpose: this one does
        no snapshot lookup, so the setup wizard can list TVs before the first
        sweep has run.
        """
        spec = self.ctx.config.tv(alias) or {}
        state = self.ctx.state
        return {
            "alias": alias,
            "ip": str(spec.get("ip") or ""),
            "mac": str(spec.get("mac") or ""),
            "label": str(spec.get("label") or alias),
            "options": spec.get("options", {}),
            "paired": bool(state.is_paired(alias, self.ctx.config.client_name())),
            "verified_how": state.pairing(alias).get("verified_how"),
            "homepage_confirmed": bool(state.homepage_confirmed(alias)),
            "enabled": bool(spec.get("enabled", True)),
        }

    def _tv_detail(self, alias: str) -> Dict[str, Any]:
        spec = self.ctx.config.tv(alias) or {}
        rows_by_alias, _ = self._snapshot()
        pairing = self.ctx.state.pairing(alias)
        detail = {
            "alias": alias,
            "ip": str(spec.get("ip") or ""),
            "mac": str(spec.get("mac") or ""),
            "label": str(spec.get("label") or alias),
            "enabled": bool(spec.get("enabled", True)),
            "options": spec.get("options", {}),
            "paired": bool(self.ctx.state.is_paired(alias, self.ctx.config.client_name())),
            "verified_how": pairing.get("verified_how"),
            "paired_at": pairing.get("paired_at"),
            "homepage_confirmed": bool(self.ctx.state.homepage_confirmed(alias)),
            "playlist": self._resolve_for(alias),
            "learned": self.ctx.state.learned(alias),
            "status": rows_by_alias.get(alias),
        }
        reason = _attr(self.ctx.state, "unpaired_reason")
        if reason is not None and not detail["paired"]:
            detail["unpaired_reason"] = reason(alias, self.ctx.config.client_name())
        return detail

    def _setup_info(self, playlists: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """`playlists` is accepted so /api/status - polled every 5 s by every open
        dashboard - scans the photo library once per request instead of twice."""
        config = self.ctx.config
        state = self.ctx.state
        client_name = config.client_name()
        aliases = self._aliases()
        confirmed = {alias: bool(state.homepage_confirmed(alias)) for alias in aliases}
        paired = sum(1 for alias in aliases if state.is_paired(alias, client_name))
        rows = self._playlists() if playlists is None else playlists
        playlists = [row for row in rows if row["count"] > 0]
        setup = state.setup()
        base_url_set = config.base_url_configured()
        info = {
            "tv_count": len(aliases),
            "paired_count": paired,
            "homepage_confirmed": confirmed,
            "homepages_confirmed": sum(1 for flag in confirmed.values() if flag),
            "playlist_count": len(playlists),
            "wizard_done": bool(setup.get("wizard_done")),
            "base_url_set": bool(base_url_set),
        }
        info["next_step"] = self._next_step(info)
        info["needs_setup"] = (not info["wizard_done"]) and info["next_step"] != "done"
        return info

    @staticmethod
    def _next_step(info: Dict[str, Any]) -> str:
        """10.5 - the wizard is resumable, so the server decides where to land."""
        if not info["base_url_set"]:
            return "base_url"
        if info["tv_count"] == 0:
            return "discover"
        if info["paired_count"] < info["tv_count"]:
            return "pair"
        if info["playlist_count"] == 0:
            return "photos"
        if info["homepages_confirmed"] < info["tv_count"]:
            return "homepage"
        if not info["wizard_done"]:
            return "groups"
        return "done"

    def _setup_payload(self) -> Dict[str, Any]:
        info = self._setup_info()
        return {
            "wizard_done": info["wizard_done"],
            "base_url_set": info["base_url_set"],
            "tv_count": info["tv_count"],
            "paired_count": info["paired_count"],
            "homepage_confirmed": info["homepage_confirmed"],
            "playlist_count": info["playlist_count"],
            "next_step": info["next_step"],
        }

    def _homepages(self, request: Optional[Request] = None) -> Dict[str, Any]:
        config = self.ctx.config
        # Fleet knows each Tv's resolved base_url (a multi-homed host can need a
        # different one per TV), so prefer its answer and only fill the gaps.
        provider = _attr(self.fleet, "homepages")
        if provider is not None:
            try:
                got = provider()
            except Exception:
                log.debug("fleet.homepages failed", exc_info=True)
                got = None
            if isinstance(got, dict) and got.get("homepage_url"):
                got.setdefault("base_url", config.base_url())
                got.setdefault("base_url_set", config.base_url_configured())
                got.setdefault("per_tv", {})
                got.setdefault("shared_homepage", bool(config.slideshow().get("shared_homepage", True)))
                if not got.get("instructions"):
                    got["instructions"] = list(_HOMEPAGE_INSTRUCTIONS)
                return got
        configured = config.base_url_configured()
        base = config.base_url()
        shared = bool(config.slideshow().get("shared_homepage", True))
        per_tv: Dict[str, str] = {}
        for alias in self._aliases():
            tv_base = config.tv_option(alias, "base_url", "") or ""
            tv_base = str(tv_base).strip().rstrip("/")
            if not tv_base:
                ip = str((config.tv(alias) or {}).get("ip") or "")
                tv_base = config.base_url(ip or None)
            path = SHARED_HOMEPAGE_PATH if shared else "/slideshow/live/%s" % quote(alias)
            per_tv[alias] = tv_base + path
        return {
            "homepage_url": base + SHARED_HOMEPAGE_PATH,
            "base_url": base,
            "base_url_set": configured,
            "shared_homepage": shared,
            "per_tv": per_tv,
            "instructions": list(_HOMEPAGE_INSTRUCTIONS),
        }

    def _homepages_text(self, request: Request) -> str:
        info = self._homepages(request)
        lines = [info["homepage_url"]]
        if not info["base_url_set"]:
            lines.append(
                "WARNING server.base_url is not set - the address above is a guess from this "
                "host's network interface. Pin it to a reserved address BEFORE setting any TV's "
                "homepage, because that string is typed into each TV by hand."
            )
        lines.extend(info["instructions"])
        if not info["shared_homepage"]:
            lines.append("per-TV homepages (slideshow.shared_homepage is off):")
            for alias in sorted(info["per_tv"]):
                lines.append("  %s: %s" % (alias, info["per_tv"][alias]))
        return "\n".join(lines)

    # ------------------------------------------------------- fleet/state seams

    def _aliases(self) -> List[str]:
        return sorted(self.ctx.config.tvs().keys())

    def _enabled_aliases(self) -> List[str]:
        tvs = self.ctx.config.tvs()
        return sorted(a for a, spec in tvs.items() if bool(spec.get("enabled", True)))

    def _group_members(self, name: str) -> List[str]:
        """The enabled members of a group, in alias order.

        A disabled TV is dropped from group fan-out - "enabled": false means
        "leave this set alone" - but /tv/<alias>/... still commands it, because
        naming it is explicit intent.
        """
        if name == "all":
            return self._enabled_aliases()
        members = self.ctx.config.group(name)
        if members is None:
            raise RequestError(404, "unknown group %r" % (name,))
        tvs = self.ctx.config.tvs()
        return sorted(a for a in members if a in tvs and bool(tvs[a].get("enabled", True)))

    def _resolve_bare(self, target: str) -> List[str]:
        """7.11 - alias, then group, then 'all'. Fleet owns the precedence."""
        resolve = _attr(self.fleet, "resolve")
        if resolve is not None:
            aliases = resolve(target)
            if aliases:
                return [str(a) for a in aliases]
            raise RequestError(404, "unknown TV or group %r" % (target,))
        if self.ctx.config.tv(target) is not None:
            return [target]
        if target == "all" or self.ctx.config.group(target) is not None:
            return self._group_members(target)
        raise RequestError(404, "unknown TV or group %r" % (target,))

    def _snapshot(self) -> Tuple[Dict[str, Dict[str, Any]], Optional[float]]:
        """The status loop's cached rows plus their age (7.14).

        Deliberately never probes: /api/status is polled every 5 s by every open
        dashboard, and a synchronous sweep there would put the whole fleet's
        REST/DIAL latency in the request path.
        """
        getter = _attr(self.fleet, "status_snapshot", "snapshot", "status_all", "status_rows", "cached_status")
        raw: Any = None
        if getter is not None:
            try:
                raw = getter()
            except Exception:
                log.debug("fleet status snapshot unavailable", exc_info=True)
                raw = None
        else:
            raw = getattr(self.fleet, "rows", None)

        age: Optional[float] = None
        if isinstance(raw, tuple) and len(raw) == 2:
            raw, age = raw[0], raw[1]

        # Fleet.snapshot() hands back a WRAPPER - {"tvs": [row, ...],
        # "age_seconds": n, ...} - not an alias->row map. Unwrap it first, or
        # every value looks like a row and the dashboard renders with no data.
        if isinstance(raw, dict):
            for key in ("tvs", "rows", "status"):
                inner = raw.get(key)
                if isinstance(inner, (list, tuple)):
                    for age_key in ("age_seconds", "age"):
                        if age is None and isinstance(raw.get(age_key), (int, float)):
                            age = float(raw[age_key])
                    raw = inner
                    break

        if age is None:
            stamp = getattr(self.fleet, "status_at", None)
            if isinstance(stamp, (int, float)) and stamp:
                age = max(0.0, time.monotonic() - float(stamp))

        rows: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw, dict):
            for alias, row in raw.items():
                if isinstance(row, dict):
                    rows[str(alias)] = row
                elif row is not None and not isinstance(row, (str, int, float, bool)):
                    rows[str(alias)] = _to_dict(row)
        elif isinstance(raw, (list, tuple)):
            for row in raw:
                item = row if isinstance(row, dict) else _to_dict(row)
                alias = item.get("alias")
                if alias:
                    rows[str(alias)] = item
        return rows, age

    def _act(self, alias: str, verb: str, arg: Optional[str]) -> Any:
        act = _attr(self.fleet, "act")
        if act is None:
            raise RequestError(500, "fleet.act is missing")
        return _flex_call(act, {"alias": alias, "verb": verb, "arg": arg}, positional=(alias, verb, arg))

    def _call_fleet(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a named Fleet roster method (7.16), or explain that it is missing."""
        func = _attr(self.fleet, name)
        if func is None:
            raise RequestError(500, "fleet.%s is missing" % (name,))
        values = dict(kwargs)
        try:
            params = list(inspect.signature(func).parameters)
        except (TypeError, ValueError):
            params = []
        # Drop keywords this implementation does not take (e.g. a pair() that
        # reports through the job handle rather than accepting one).
        if params:
            values = {k: v for k, v in values.items() if k in params}
        return func(*args, **values)

    def _start_command(self, plan: _Plan) -> str:
        """Run a plan as a job, chaining a heal exactly where 7.13 allows one."""
        title = "%s %s" % (plan.verb, plan.name)

        def work(handle: Any) -> Any:
            handle.step(title)
            text = self._run_plan(plan)
            for line in text.splitlines():
                handle.log(line)
            handle.set_result(text)
            healed = self._chain_heal(plan.aliases, plan.verb)
            if healed:
                handle.step("healing (job %s)" % healed)
            return text

        return self.ctx.jobs.start("action", title, work)

    def _start_exclusive(self, key: str, kind: str, title: str, fn: Callable[[Any], Any]) -> Tuple[str, bool]:
        return self.ctx.jobs.start_exclusive(key, kind, title, fn)

    def _chain_heal(self, aliases: Sequence[str], verb: str) -> Optional[str]:
        """Chain a heal after a command, if 7.13 allows this verb to trigger one.

        Delegates to fleet.maybe_heal, which is the single place the HEAL_VERBS
        whitelist and the auto_heal switch are checked - duplicating that policy
        here is how a blacklist once let an ordinary keypress start a heal.
        """
        maybe = _attr(self.fleet, "maybe_heal")
        if maybe is not None:
            try:
                job = _flex_call(
                    maybe,
                    {"aliases": list(aliases), "verb": verb},
                    positional=(list(aliases), verb),
                )
                return str(job) if job else None
            except Exception:
                log.debug("maybe_heal failed", exc_info=True)
                return None
        if verb not in HEAL_VERBS or not bool(self.ctx.config.healing().get("auto_heal", True)):
            return None
        return _job_ref(self._heal(aliases))[0]

    def _heal(self, aliases: Sequence[str]) -> Any:
        heal = _attr(self.fleet, "heal")
        if heal is None:
            return None
        try:
            return _flex_call(heal, {"aliases": list(aliases)}, positional=(list(aliases),))
        except Exception:
            log.debug("heal failed", exc_info=True)
            return None

    def _reload(self) -> Any:
        """/reload - re-read config.json AND rebuild the fleet's TV objects."""
        result = self._reload_fleet()
        if result is not None:
            return result
        return self.ctx.reload()

    def _reload_fleet(self) -> Any:
        reload_fn = _attr(self.fleet, "reload")
        if reload_fn is None:
            return None
        try:
            return reload_fn()
        except TypeError:
            return None

    def _unpair(self, alias: str) -> Any:
        unpair = _attr(self.fleet, "unpair")
        if unpair is not None:
            return unpair(alias)
        self.ctx.state.clear_token(alias)
        return "unpaired %s - the stored token is gone; pair it again to control it" % alias

    def _remove_group(self, name: str) -> Any:
        remover = _attr(self.fleet, "remove_group", "delete_group")
        if remover is not None:
            return remover(name)

        def mutate(document: Dict[str, Any]) -> None:
            document.setdefault("groups", {}).pop(name, None)

        self.ctx.config.save(mutate)
        self._reload_fleet()
        return "removed group %s" % name

    def _identify_map(self) -> Dict[str, Any]:
        getter = _attr(self.fleet, "identify_map")
        if getter is None:
            return {}
        try:
            got = getter()
        except Exception:
            return {}
        return dict(got) if isinstance(got, dict) else {}

    def _identify_on(self) -> bool:
        getter = _attr(self.fleet, "identify_on", "is_identify", "identify_enabled")
        if getter is not None:
            try:
                return bool(getter())
            except Exception:
                return False
        value = getattr(self.fleet, "identify", None)
        return bool(value) if isinstance(value, bool) else False

    def _set_identify(self, on: bool) -> Any:
        setter = _attr(self.fleet, "set_identify", "identify_set", "set_identify_mode")
        if setter is None:
            raise RequestError(500, "fleet has no identify control")
        # 7.15 - fleet schedules fullscreen_all(delay=7.0) when identify goes
        # off, so the pages have polled the overlay away before the key lands.
        return setter(bool(on))

    def _identify_entry(self, ip: str) -> Optional[Dict[str, Any]]:
        if not self._identify_on():
            return None
        entry = self._identify_map().get(ip)
        if entry is None:
            # 8.7 - an unrecognised IP still gets an overlay, so a TV nobody
            # configured is visibly the odd one out rather than silently blank.
            return {"n": "?", "alias": ip}
        number = _identify_number(entry)
        alias = _identify_alias(entry) or ip
        return {"n": number if number is not None else "?", "alias": alias}

    # -- playlists ---------------------------------------------------------- #

    def _playlist_dir(self, name: str) -> Optional[Path]:
        finder = _module_attr(self.slideshow, "playlist_dir")
        if callable(finder):
            try:
                got = _flex_call(finder, {"name": name, "playlist": name}, positional=(name,))
            except Exception:
                got = None
            return Path(got) if got else None
        if not valid_playlist(name):
            return None
        return _safe_child(self.ctx.config.photo_root(), name, must_be_file=False)

    def _playlist_exists(self, name: str) -> bool:
        folder = self._playlist_dir(name)
        return folder is not None and folder.is_dir()

    def _shared_playlist(self) -> str:
        """8.3 for the shared homepage: pointer -> config default -> first
        non-empty -> "default". Slideshow owns the chain when it accepts None."""
        resolver = _attr(self.slideshow, "resolve_for")
        if resolver is not None:
            try:
                got = resolver(None)
                if got:
                    return str(got)
            except Exception:
                log.debug("resolve_for(None) unsupported", exc_info=True)
        default = str(self.ctx.config.slideshow().get("default_playlist") or "default")
        for candidate in (self.ctx.state.shared_playlist(), default):
            if candidate and self._playlist_exists(candidate):
                return candidate
        for row in self._playlists():
            if row["count"] > 0:
                return str(row["name"])
        return default or "default"

    def _resolve_for(self, alias: str) -> str:
        resolver = _attr(self.slideshow, "resolve_for")
        if resolver is not None:
            try:
                got = resolver(alias)
                if got:
                    return str(got)
            except Exception:
                log.debug("resolve_for(%s) failed", alias, exc_info=True)
        per_tv = self.ctx.state.tv_playlist(alias)
        if per_tv and self._playlist_exists(per_tv):
            return per_tv
        return self._shared_playlist()

    def _activate(self, name: str) -> Any:
        activate = _attr(self.slideshow, "activate")
        if activate is not None:
            return activate(name)
        # 8.2/I11 - validate BEFORE persisting. An unvalidated name once blanked
        # every screen and survived a restart.
        if not valid_playlist(name) or not self._playlist_exists(name):
            raise RequestError(404, "unknown playlist %r" % (name,))
        if not self._image_names(name):
            return {"ok": False, "level": "error", "text": "playlist %r has no displayable images" % (name,)}
        self.ctx.state.set_shared_playlist(name)
        return "playlist is now %s - screens follow within about 5 seconds" % name

    def _create_playlist(self, name: str) -> Any:
        creator = _attr(self.slideshow, "create", "create_playlist", "new_playlist")
        if creator is not None:
            return creator(name)
        folder = self._playlist_dir(name)
        if folder is None:
            raise RequestError(400, "%r is not a usable playlist name" % (name,))
        if folder.is_dir():
            return {"ok": False, "level": "warn", "text": "playlist %r already exists" % (name,)}
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "level": "error", "text": "cannot create %s (%s)" % (name, exc)}
        return "created playlist %s - now upload some photos" % name

    def _delete_playlist(self, name: str) -> Any:
        deleter = _attr(self.slideshow, "delete", "delete_playlist")
        if deleter is not None:
            return deleter(name)
        # 8.6 - refuse while it is pointed at, and say what to do instead.
        in_use = _attr(self.ctx.state, "playlists_in_use")
        used = list(in_use() or []) if in_use is not None else []
        if name in used:
            return {
                "ok": False,
                "level": "error",
                "text": "playlist %r is in use - switch playlists first, then delete it" % (name,),
            }
        folder = self._playlist_dir(name)
        if folder is None or not folder.is_dir():
            raise RequestError(404, "unknown playlist %r" % (name,))
        try:
            for item in folder.iterdir():
                if item.is_file():
                    item.unlink()
            folder.rmdir()
        except OSError as exc:
            return {"ok": False, "level": "error", "text": "cannot delete %s (%s)" % (name, exc)}
        return "deleted playlist %s" % name

    def _delete_image(self, playlist: str, filename: str) -> Any:
        deleter = _attr(self.slideshow, "delete_image", "remove_image")
        if deleter is not None:
            return _flex_call(
                deleter,
                {"playlist": playlist, "name": playlist, "filename": filename, "file": filename},
                positional=(playlist, filename),
            )
        folder = self._playlist_dir(playlist)
        path = _safe_child(folder, filename) if folder is not None else None
        if path is None or path.suffix.lower() not in IMAGE_EXTS or not path.is_file():
            raise RequestError(404, "no such image")
        try:
            path.unlink()
        except OSError as exc:
            return {"ok": False, "level": "error", "text": "cannot delete %s (%s)" % (filename, exc)}
        return "deleted %s from %s" % (filename, playlist)

    # -- misc --------------------------------------------------------------- #

    def _uptime(self) -> float:
        getter = _attr(self.ctx, "uptime_seconds")
        if getter is not None:
            try:
                return float(getter())
            except Exception:
                pass
        return max(0.0, time.monotonic() - self._started)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _query_string(query: Mapping[str, List[str]]) -> str:
    """Re-encode a parsed query, for /x/ handing a path to its inner route."""
    parts: List[str] = []
    for key, values in (query or {}).items():
        for value in values:
            parts.append("%s=%s" % (quote(str(key), safe=""), quote(str(value), safe="")))
    return "&".join(parts)


def _bad_name(what: str, value: str) -> str:
    """Explain a refused alias / group name, naming the REAL reason.

    ``valid_alias`` and ``valid_group`` fail for two different reasons - the
    pattern, and the reserved-word list - and a message that only mentions the
    pattern is actively misleading for a name like "all" or "status", which
    matches it perfectly. Both are stated, and the reserved case is named first
    because that is the one nobody guesses.
    """
    text = str(value or "")
    if text.lower() in RESERVED_NAMES:
        return (
            "%s %r is a reserved word - it is used by the route table (%s)"
            % (what, text, ", ".join(sorted(RESERVED_NAMES)))
        )
    return (
        "%s must match ^[a-z0-9][a-z0-9-]{0,31}$ (lower-case letters, digits and "
        "'-', starting with a letter or digit) and must not be a reserved word" % what
    )


def _normalize_ip(ip: str) -> str:
    """Strip the IPv4-mapped IPv6 prefix so allow-lists compare as written."""
    text = str(ip or "").strip()
    if text.startswith("::ffff:") and text.count(".") == 3:
        return text[len("::ffff:"):]
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1]
    return text


def _ip_matches(ip: str, patterns: Iterable[str]) -> bool:
    """Exact address, CIDR, or a trailing-* prefix. Empty list = any source."""
    items = [str(p).strip() for p in (patterns or []) if str(p).strip()]
    if not items:
        return True
    for pattern in items:
        if pattern in ("*", "0.0.0.0/0", "::/0"):
            return True
        if pattern == ip:
            return True
        if "/" in pattern:
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(pattern, strict=False):
                    return True
            except ValueError:
                log.debug("ignoring unparseable allow-list entry %r", pattern)
            continue
        if pattern.endswith("*") and ip.startswith(pattern[:-1]):
            return True
    return False


def _check_key_sequence(text: str, single: bool = False) -> None:
    """2.2 syntax check, so a typo is a 400 rather than a lost keypress.

    Only the SHAPE is checked here. The authoritative expansion is
    parse_key_sequence, which lives with the protocol in samsung.py; duplicating
    its table here would be one more thing to keep in step.
    """
    raw = str(text or "").strip()
    if not raw:
        raise RequestError(400, "no key given")
    tokens = [t.strip() for t in raw.replace("+", ",").split(",")]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise RequestError(400, "no key given")
    if single and len(tokens) != 1:
        raise RequestError(400, "expected one key, not a sequence")
    for token in tokens:
        if token.startswith("@"):
            if single:
                raise RequestError(400, "expected a key, not a wait")
            if not token[1:].isdigit():
                raise RequestError(400, "bad wait %r - expected @<milliseconds>" % (token,))
            continue
        name = token
        if "*" in token:
            name, _, count = token.partition("*")
            if not count.isdigit() or int(count) < 1:
                raise RequestError(400, "bad repeat %r - expected KEY_X*<n>" % (token,))
        name = name.strip()
        if not name or not name.replace("_", "").isalnum():
            raise RequestError(400, "bad key name %r" % (token,))


def _as_alias_map(aliases: Sequence[str], results: Any) -> Dict[str, Any]:
    """Normalise whatever Fleet.run returned into {alias: Result}."""
    if isinstance(results, dict):
        return {str(k): v for k, v in results.items()}
    out: Dict[str, Any] = {}
    if isinstance(results, (list, tuple)):
        pairs = [
            item
            for item in results
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str)
        ]
        if len(pairs) == len(results) and results:
            for alias, result in pairs:
                out[str(alias)] = result
            return out
        for alias, result in zip(aliases, results):
            out[str(alias)] = result
        return out
    for alias in aliases:
        out[alias] = results
    return out


def _arg_list(arg: Optional[str]) -> List[str]:
    """A verb argument in list form, which is how a fan-out runner expects it."""
    return [arg] if arg not in (None, "") else []


def _is_ok(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("ok", True))
    if isinstance(value, bool):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool):
        return value[0]
    if isinstance(value, str) or value is None:
        return True
    return bool(getattr(value, "ok", True))


def _job_ref(value: Any) -> Tuple[Optional[str], bool]:
    """Normalise a job reference: (id, started_now)."""
    if isinstance(value, tuple) and len(value) == 2:
        return (str(value[0]) if value[0] else None), bool(value[1])
    if isinstance(value, str):
        return value, True
    if isinstance(value, dict):
        job = value.get("job") or value.get("id")
        return (str(job) if job else None), bool(value.get("started", True))
    return None, False


def _to_dict(value: Any) -> Dict[str, Any]:
    """Best-effort dict view of a status row object."""
    for name in ("as_dict", "to_dict", "_asdict"):
        func = getattr(value, name, None)
        if callable(func):
            try:
                got = func()
                if isinstance(got, dict):
                    return dict(got)
            except Exception:
                pass
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not k.startswith("_")}
    slots = getattr(type(value), "__slots__", None)
    if slots:
        return {name: getattr(value, name, None) for name in slots}
    return {}


def _strip_secrets(value: Any) -> Any:
    """Belt and braces: tokens live in state.json, never in config.json (3.2)."""
    if isinstance(value, dict):
        return {
            key: ("***" if "token" in str(key).lower() else _strip_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def _as_ok_message(value: Any) -> Tuple[bool, str]:
    if isinstance(value, tuple) and len(value) == 2:
        return bool(value[0]), str(value[1])
    rendered = render_result(value)
    return not rendered.startswith("ERROR "), rendered


def _as_int(value: Any, default: int, low: Optional[int] = None, high: Optional[int] = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return out


def _identify_number(entry: Any) -> Optional[int]:
    """7.15 - identify_map values are (n, alias)."""
    if entry is None:
        return None
    if isinstance(entry, (list, tuple)) and entry:
        try:
            return int(entry[0])
        except (TypeError, ValueError):
            return None
    if isinstance(entry, dict):
        try:
            return int(entry.get("n"))
        except (TypeError, ValueError):
            return None
    try:
        return int(entry)
    except (TypeError, ValueError):
        return None


def _identify_alias(entry: Any) -> Optional[str]:
    if isinstance(entry, (list, tuple)) and len(entry) > 1:
        return str(entry[1])
    if isinstance(entry, dict):
        alias = entry.get("alias")
        return str(alias) if alias else None
    return None


def _safe_child(folder: Optional[Path], name: str, must_be_file: bool = True) -> Optional[Path]:
    """Resolve ``name`` strictly inside ``folder``, or None.

    Any separator or dot-dot is refused outright rather than normalised, and the
    resolved path is re-checked against the resolved parent so a symlink cannot
    lead out of the photo library either.
    """
    if folder is None or not name or name in (".", ".."):
        return None
    if "/" in name or "\\" in name or "\x00" in name:
        return None
    try:
        parent = Path(folder).resolve()
        candidate = (parent / name).resolve()
        candidate.relative_to(parent)
    except (OSError, ValueError):
        return None
    if must_be_file and not candidate.is_file():
        return None
    return candidate


def _part_fields(part: Any) -> Tuple[str, bytes]:
    """(filename, data) out of whatever parse_multipart returned."""
    if isinstance(part, dict):
        name = part.get("filename") or part.get("name") or ""
        data = part.get("data")
        if data is None:
            data = part.get("content", part.get("body", part.get("value", b"")))
        return str(name or ""), _as_bytes(data)
    if isinstance(part, (list, tuple)):
        if len(part) >= 3:
            return str(part[1] or ""), _as_bytes(part[2])
        if len(part) == 2:
            return str(part[0] or ""), _as_bytes(part[1])
        return "", b""
    name = getattr(part, "filename", None) or getattr(part, "name", "") or ""
    data = getattr(part, "data", None)
    if data is None:
        data = getattr(part, "content", None) or getattr(part, "body", b"")
    return str(name), _as_bytes(data)


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return b""


def _classify_fields(value: Any) -> Tuple[Optional[str], str, Optional[str]]:
    """(verdict, message, advice) out of whatever classify_upload returned."""
    if value is None:
        return None, "", None
    if isinstance(value, str):
        return value, "", None
    if isinstance(value, dict):
        verdict = value.get("verdict") or value.get("reason")
        return (str(verdict) if verdict else None), str(value.get("message") or ""), value.get("advice")
    if isinstance(value, (list, tuple)):
        verdict = str(value[0]) if value and value[0] else None
        message = str(value[1]) if len(value) > 1 and value[1] else ""
        advice = value[2] if len(value) > 2 else None
        # For an "ok" verdict the second element is ADVICE, not an error - it is
        # shown next to a success, so it must not be reported as a rejection.
        if verdict == "ok" and advice is None and message:
            return verdict, "", message
        return verdict, message, advice
    verdict = getattr(value, "verdict", None) or getattr(value, "reason", None)
    return (
        (str(verdict) if verdict else None),
        str(getattr(value, "message", "") or ""),
        getattr(value, "advice", None),
    )


def _sniff_image(data: bytes) -> Optional[str]:
    """Magic bytes, because an extension is a claim and not evidence (8.5)."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "heic"
    return None


def _classify_locally(
    filename: str, data: bytes, max_bytes: int, slideshow: Any
) -> Tuple[str, str, Optional[str]]:
    """8.5's verdicts, used only when slideshow's classifier is unavailable."""
    if not data:
        return "empty", "That file was empty.", None
    if len(data) > max_bytes:
        return "too-big", "That file is larger than the %d MB limit." % (max_bytes // (1024 * 1024)), None

    brands = _module_attr(slideshow, "HEIC_BRANDS")
    brands = brands if isinstance(brands, (set, frozenset, tuple, list)) else _HEIC_BRANDS
    kind = _sniff_image(data)
    if kind is None and len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in brands:
        kind = "heic"
    ext = Path(filename).suffix.lower()
    if kind == "heic" or (kind is None and ext in (".heic", ".heif")):
        message = _module_attr(slideshow, "HEIC_MESSAGE")
        if not isinstance(message, str) or not message:
            message = (
                "That is an iPhone HEIC photo, which the TV browser cannot display. "
                "Export or share it as JPEG first, then upload it again."
            )
        return "heic", message, None
    if kind is None:
        return (
            "unsupported",
            "Only JPEG, PNG, WebP, GIF and BMP images can be shown on the TVs - %s looks like %s."
            % (filename, ext.lstrip(".") or "something else"),
            None,
        )
    advice = _module_attr(slideshow, "SIZE_ADVICE")
    return "ok", "", advice if isinstance(advice, str) and advice else None


def _safe_filename_locally(folder: Path, filename: str, kind: Optional[str]) -> str:
    """8.5's naming rules, used only when safe_filename is unavailable."""
    base = str(filename or "").replace("\\", "/").split("/")[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    cleaned = "".join(ch for ch in stem if ch.isalnum() or ch in "._ -")
    cleaned = " ".join(cleaned.split()).strip() or "image"
    cleaned = cleaned[:80].strip() or "image"
    ext = _EXT_FOR_KIND.get(kind or "", "")
    if not ext:
        suffix = Path(base).suffix.lower()
        ext = suffix if suffix in IMAGE_EXTS else ".jpg"
    candidate = cleaned + ext
    counter = 2
    while (folder / candidate).exists():
        candidate = "%s-%d%s" % (cleaned, counter, ext)
        counter += 1
    return candidate


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover
        return ""


def _platform() -> str:
    import platform as _p

    try:
        return "%s %s (python %s)" % (_p.system(), _p.release(), _p.python_version())
    except Exception:  # pragma: no cover
        return sys.platform


# --------------------------------------------------------------------------- #
# body reading
# --------------------------------------------------------------------------- #


def read_body(handler: Any, max_bytes: int) -> bytes:
    """Read a request body using Content-Length only.

    Chunked is refused with 411 rather than implemented: nothing in this system
    sends it (browsers use Content-Length for form posts, controllers send no
    body at all), and a hand-rolled dechunker on a public port is a liability.
    Oversize is refused with 413 WITHOUT reading the body - every response closes
    the connection, so there is nothing to drain.
    """
    headers = getattr(handler, "headers", None)
    if headers is None:
        return b""
    encoding = str(headers.get("Transfer-Encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        raise RequestError(411, "chunked request bodies are not supported - send Content-Length")
    raw = headers.get("Content-Length")
    if raw is None or str(raw).strip() == "":
        return b""
    try:
        length = int(str(raw).strip())
    except ValueError:
        raise RequestError(400, "bad Content-Length")
    if length < 0:
        raise RequestError(400, "bad Content-Length")
    limit = max(0, int(max_bytes))
    if length > limit:
        raise RequestError(413, "body is larger than the %d MB limit" % (limit // (1024 * 1024)))
    if length == 0:
        return b""

    chunks: List[bytes] = []
    remaining = length
    stream = handler.rfile
    while remaining > 0:
        chunk = stream.read(min(remaining, 65536))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining > 0:
        raise RequestError(400, "request body ended after %d of %d bytes" % (length - remaining, length))
    return b"".join(chunks)


# --------------------------------------------------------------------------- #
# the socket layer
# --------------------------------------------------------------------------- #


def make_handler(app: App, ctx: "Context") -> type:
    """Build the BaseHTTPRequestHandler subclass that feeds ``app.handle``.

    Contains no routing: it parses, calls, and writes. Every method funnels into
    one place so a new verb cannot pick up different plumbing (9.1).
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "TVHub/%s" % VERSION
        # Suppress the Python version banner: it is free reconnaissance on a
        # port that is open to a whole building's network.
        sys_version = ""
        # A TV or controller that opens a socket and stalls must not hold a
        # worker thread forever.
        timeout = 30

        def do_GET(self) -> None:
            self._serve("GET")

        def do_HEAD(self) -> None:
            self._serve("HEAD")

        def do_POST(self) -> None:
            self._serve("POST")

        def do_PUT(self) -> None:
            self._serve("PUT")

        def do_PATCH(self) -> None:
            self._serve("PATCH")

        def do_DELETE(self) -> None:
            self._serve("DELETE")

        # -- plumbing ------------------------------------------------------- #

        def _serve(self, method: str) -> None:
            self.close_connection = True
            as_json = str(self.path or "").startswith("/api/")
            try:
                limit = ctx.config.max_upload_bytes()
            except Exception:
                limit = 64 * 1024 * 1024
            try:
                body = read_body(self, limit) if method in _BODY_METHODS else b""
            except RequestError as exc:
                response = Response.error(exc.status, exc.message, as_json, exc.headers or None)
            else:
                # X-Forwarded-For is deliberately ignored: the allow-lists are
                # the only access control here, and a header anyone can set
                # would make them decorative.
                request = Request.from_target(
                    method, self.path, self.headers, _normalize_ip(self.client_address[0]), body
                )
                response = app.handle(request)
            self._write(response, include_body=(method != "HEAD"))

        def _write(self, response: Response, include_body: bool = True) -> None:
            try:
                self.send_response(response.status)
                for key, value in response.header_items():
                    self.send_header(key, value)
                self.end_headers()
                if include_body and response.body:
                    self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError) as exc:
                # Routine: a TV reboots, a controller times out mid-poll.
                log.debug("client %s went away: %s", self.client_address[0], exc)
                self.close_connection = True

        # 0.9 - per-request HTTP logging is DEBUG. At INFO the heartbeat polling
        # of a dozen TVs every 5 s would bury everything worth reading.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("%s %s", self.client_address[0], format % args)

        def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("%s %s", self.client_address[0], format % args)

    return Handler


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The base class prints a traceback to stderr for every dropped socket.
        log.debug("error serving %s\n%s", client_address, traceback.format_exc())


class Server:
    """The listening socket. Owns no routing."""

    def __init__(self, ctx: "Context", app: App) -> None:
        self.ctx = ctx
        self.app = app
        bind = ctx.config.bind() or "0.0.0.0"
        port = int(ctx.config.http_port())
        server_class = _ThreadingServer
        if ":" in bind:
            # An IPv6 bind needs the family set before the socket is created.
            class _V6(_ThreadingServer):
                address_family = socket.AF_INET6

            server_class = _V6
        try:
            self._httpd = server_class((bind, port), make_handler(app, ctx))
        except OSError as exc:
            raise OSError(
                "cannot bind %s:%d (%s) - another process is probably holding the port; "
                "stop it (or the old service) and try again" % (bind, port, exc)
            )
        address = self._httpd.server_address
        self.bound: Tuple[str, int] = (str(address[0]), int(address[1]))
        self._closed = False
        log.info("HTTP listening on %s:%d", self.bound[0], self.bound[1])

    def serve_forever(self) -> None:
        try:
            self._httpd.serve_forever(poll_interval=0.5)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Idempotent, and safe to call from another thread."""
        if self._closed:
            return
        self._closed = True
        try:
            self._httpd.shutdown()
        except Exception:  # pragma: no cover - already dead
            log.debug("shutdown raised", exc_info=True)
        finally:
            try:
                self._httpd.server_close()
            except Exception:  # pragma: no cover
                pass
        log.info("HTTP stopped")
