"""Photo library, playlist pointers, the manifest, and the page the TVs display.

This module owns everything between a JPEG on disk and pixels on a Samsung
screen, and nothing about TVs beyond an alias string used to look up a pointer.

Two facts from the field shape the whole design and must not be "simplified":

  * Samsung's local API cannot tell a TV to show a picture, and many firmwares
    accept "launch the browser at this URL" and then ignore the URL. So every
    TV's browser homepage is set ONCE by hand to ONE shared address, and
    switching playlists REPOINTS what that one address serves. That is why the
    page polls `manifest.json` every 5 seconds: the poll, not any command we
    send, is how a playlist switch reaches a screen that is already open.

  * The page therefore has to survive being the only moving part. It carries a
    PAGE_VERSION and reloads itself when the server reports a different one,
    which is how a page fix reaches a fleet without anyone walking to a screen.

Contract references in comments are to TVHUB FROZEN INTERFACE CONTRACT v1.
"""

from __future__ import annotations

import hashlib
import html as _html
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from urllib.parse import quote, unquote

from .store import Context

# Result is shared vocabulary (contract 0.8 / 2.5) and belongs at the bottom of
# the dependency graph next to Context. If store.py exports one, that is the
# single source of truth and we use it. Otherwise this module defines an
# identical one rather than importing fleet: contract 0.5 makes the direction
# store <- ui <- slideshow, and reaching sideways into fleet would invert the
# layering (and drag websocket-client into a module that must not need it).
# The surface here matches fleet.Result exactly - note that detail arrives as
# KEYWORDS, so Result.good("done", playlist="x"), never detail={...}.
try:  # pragma: no cover - satisfied once Result moves into store.py
    from .store import Result  # type: ignore[attr-defined]
except ImportError:
    @dataclass
    class Result:  # type: ignore[no-redef]
        """The single return type of every action (contract 0.8 / 2.5)."""

        ok: bool
        text: str
        level: str = "ok"
        detail: dict = dc_field(default_factory=dict)

        def __str__(self) -> str:
            if self.level == "warn":
                return "WARNING " + self.text
            if self.level == "error":
                return "ERROR " + self.text
            return self.text

        def as_json(self) -> dict:
            return {
                "ok": self.ok,
                "text": self.text,
                "level": self.level,
                "detail": dict(self.detail),
                "rendered": str(self),
            }

        @classmethod
        def good(cls, text: str, **detail) -> "Result":
            return cls(True, text, "ok", dict(detail))

        @classmethod
        def warn(cls, text: str, **detail) -> "Result":
            # ok is False for both warn and error (contract 2.5).
            return cls(False, text, "warn", dict(detail))

        @classmethod
        def error(cls, text: str, **detail) -> "Result":
            return cls(False, text, "error", dict(detail))

# The page template lives at tvhub/web/slideshow.html and ui.py owns the web
# folder. The import is guarded because rendering the slideshow is the one job
# in this process that must never fail: an install whose ui module is missing,
# renamed or mid-edit should still light up the TVs from the built-in copy of
# the template below rather than leaving every screen black.
try:  # pragma: no cover - present in a complete install
    from . import ui as _ui
except Exception:  # noqa: BLE001 - any import failure falls back to _DEFAULT_PAGE
    _ui = None

LOG = logging.getLogger("tvhub")

# Bump whenever the page template changes: the page compares this against its
# own baked-in copy and reloads itself when they differ (contract 8.8f).
# page_version() additionally mixes in a hash of an on-disk template, so an edit
# to tvhub/web/slideshow.html propagates even if someone forgets to bump this.
PAGE_VERSION: str = "1"

IMAGE_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

# ISO-BMFF brands that mean "HEIC/HEIF" in bytes 8:12, right after the "ftyp"
# box type. iPhones produce these by default and the Tizen browser renders none
# of them, so they are rejected at the door with an explanation rather than
# becoming black slides nobody can explain.
HEIC_BRANDS: tuple[bytes, ...] = (
    b"heic", b"heix", b"hevc", b"hevx", b"heim",
    b"heis", b"hevm", b"hevs", b"mif1", b"msf1",
)

HEIC_MESSAGE: str = (
    "HEIC/HEIF photos will not render in the Samsung TV browser. Convert to "
    "JPEG first (Windows: Photos app, Save As; macOS: Preview, Export as JPEG) "
    "and upload again."
)
ORDER_ADVICE: str = (
    "Filenames set the running order - prefix with 01-, 02- if it matters."
)
SIZE_ADVICE: str = (
    "Resize to 3840px on the long edge; larger files only slow the TV browser down."
)

# Contract 2.1. The leading character is alphanumeric, which already excludes
# "." and "..", but 8.1 asks for both checks and they cost nothing.
PLAYLIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")

# Mirrors the alias grammar in contract 2.1. store.py owns the authoritative
# validation (it also holds RESERVED_NAMES); this is only here so a malformed
# alias cannot be written into the per-TV pointer map by a careless caller.
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

_FIT_VALUES = ("contain", "cover")
_MIN_INTERVAL = 2  # contract 3.7 / 9.5: anything below 2 s is clamped up

# Canonical extension per sniffed kind, used to force an honest extension onto
# an uploaded file (contract 8.5).
_KIND_EXT = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
}
_KIND_OK_EXTS = {
    "jpeg": (".jpg", ".jpeg"),
    "png": (".png",),
    "gif": (".gif",),
    "webp": (".webp",),
    "bmp": (".bmp",),
}

_FILENAME_KEEP = re.compile(r"[^A-Za-z0-9._ -]+")
_WS_RUN = re.compile(r"\s+")


class LibraryError(Exception):
    """A malformed request against the photo library (bad multipart, bad name)."""


@dataclass
class UploadPart:
    """One part of a multipart/form-data body.

    `filename` is "" for an ordinary form field; callers that want files only
    should test it. `data` is the raw bytes with the part's trailing CRLF
    already removed.
    """

    name: str
    filename: str
    content_type: str
    data: bytes


# ---------------------------------------------------------------------------
# multipart/form-data
# ---------------------------------------------------------------------------

def _boundary_of(content_type: str) -> str:
    """Pull the boundary out of a Content-Type header.

    Hand-rolled on purpose: `cgi` was removed in Python 3.13 and the contract
    allows no third-party parser, so uploads must not depend on either.
    """
    if not content_type:
        raise LibraryError("This upload arrived with no Content-Type header.")
    parts = content_type.split(";")
    if "multipart/form-data" not in parts[0].strip().lower():
        raise LibraryError(
            "Expected a multipart/form-data upload, got "
            + parts[0].strip()
            + "."
        )
    for param in parts[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() != "boundary":
            continue
        value = value.strip()
        # Browsers may quote the boundary; RFC 2046 allows it either way.
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        if value:
            return value
    raise LibraryError("This upload had no boundary in its Content-Type header.")


def _decode_header(raw: bytes) -> str:
    # Part headers are nominally ASCII, but browsers put raw UTF-8 filenames in
    # Content-Disposition. Decode leniently: a mangled character in a filename
    # must not fail an otherwise good upload (safe_filename cleans it anyway).
    return raw.decode("utf-8", "replace")


def _split_params(value: str) -> "list[str]":
    """Split a header value on semicolons that are not inside double quotes."""
    out: "list[str]" = []
    buf: "list[str]" = []
    in_quote = False
    escaped = False
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_quote:
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
            continue
        if ch == ";" and not in_quote:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def _unquote_param(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
        value = value.replace('\\"', '"').replace("\\\\", "\\")
    return value


def _disposition_params(value: str) -> "dict[str, str]":
    """Parse Content-Disposition parameters, quoted and RFC 5987 forms alike."""
    params: "dict[str, str]" = {}
    for chunk in _split_params(value)[1:]:
        key, sep, raw = chunk.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        if key.endswith("*"):
            # RFC 5987: filename*=UTF-8''photo%20one.jpg
            key = key[:-1]
            raw = _unquote_param(raw)
            bits = raw.split("'")
            charset = bits[0] or "utf-8"
            encoded = bits[2] if len(bits) >= 3 else raw
            try:
                params[key] = unquote(encoded, encoding=charset, errors="replace")
            except (LookupError, ValueError):
                params[key] = unquote(encoded, errors="replace")
            continue
        # A plain `filename=` must not clobber a `filename*=` already seen: the
        # extended form is the one with the charset, so it wins.
        if key not in params:
            params[key] = _unquote_param(raw)
    return params


def parse_multipart(
    body: bytes, content_type: str, *, max_bytes: int
) -> "list[UploadPart]":
    """Split a multipart/form-data body into parts. Raises LibraryError.

    Contract 8.4. `max_bytes` bounds the WHOLE body; per-file limits are
    classify_upload's job.
    """
    if max_bytes and len(body) > max_bytes:
        raise LibraryError(
            "That upload is larger than the " + _mb_text(max_bytes) + " MB limit."
        )
    boundary = _boundary_of(content_type)
    sep = b"--" + boundary.encode("utf-8", "replace")
    chunks = body.split(sep)
    parts: "list[UploadPart]" = []
    # chunks[0] is the preamble and the final chunk starts with "--" (the
    # epilogue); both are discarded.
    for chunk in chunks[1:]:
        if chunk[:2] == b"--":
            break
        if not chunk.strip():
            continue
        if chunk[:2] == b"\r\n":
            chunk = chunk[2:]
        head, sepfound, data = chunk.partition(b"\r\n\r\n")
        if not sepfound:
            # A part with no header/body separator is malformed; skip it rather
            # than failing the whole upload, so one odd part cannot cost the
            # user the other nineteen photos they selected.
            continue
        # Exactly one trailing CRLF belongs to the boundary, not to the file.
        # Stripping more would corrupt a JPEG that genuinely ends in 0x0d 0x0a.
        if data[-2:] == b"\r\n":
            data = data[:-2]
        name = ""
        filename = ""
        ctype = ""
        for line in head.split(b"\r\n"):
            if not line:
                continue
            key, _, value = _decode_header(line).partition(":")
            key = key.strip().lower()
            if key == "content-disposition":
                params = _disposition_params(value)
                name = params.get("name", "")
                filename = params.get("filename", "")
            elif key == "content-type":
                ctype = value.strip()
        if not filename and not name:
            continue
        # Some clients send only `name` for a file input; fall back to it so the
        # photo still gets a usable name instead of being dropped. The part's
        # own Content-Type is the discriminator: browsers always send one for a
        # file part and never for an ordinary text field, so this cannot promote
        # a form field into a photo the user then sees "rejected".
        if not filename and ctype and data:
            filename = name
        parts.append(
            UploadPart(name=name, filename=filename, content_type=ctype, data=data)
        )
    if not parts:
        raise LibraryError("That upload contained no files.")
    return parts


# ---------------------------------------------------------------------------
# sniffing and naming
# ---------------------------------------------------------------------------

def sniff_kind(data: bytes) -> str:
    """Identify an image by magic bytes: the extension is never trusted."""
    if len(data) < 4:
        return "unknown"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[4:8] == b"ftyp" and bytes(data[8:12]) in HEIC_BRANDS:
        return "heic"
    return "unknown"


def _describe(data: bytes, filename: str) -> str:
    """A short human noun phrase for a file we cannot show, for the message."""
    head = data[:16]
    if head[:5] == b"%PDF-":
        return "a PDF"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "a TIFF image"
    if data[4:8] == b"ftyp":
        brand = bytes(data[8:12])
        if brand in (b"avif", b"avis"):
            return "an AVIF image"
        return "a video file"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "a video file"
    if head[:2] == b"PK":
        return "a zip archive"
    if head[:3] == b"ID3" or head[:4] == b"fLaC":
        return "an audio file"
    if b"<svg" in data[:512].lower():
        return "an SVG, which the TV browser will not scale reliably"
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext:
        return "a ." + ext + " file"
    return "something the TV browser cannot display"


def _mb_text(nbytes: int) -> str:
    mb = float(nbytes) / (1024.0 * 1024.0)
    if abs(mb - round(mb)) < 0.05:
        return str(int(round(mb)))
    return "%.1f" % mb


def classify_upload(
    filename: str, data: bytes, *, max_bytes: int
) -> "tuple[str, str]":
    """Verdict plus message for one uploaded file (contract 8.5).

    Returns ('ok'|'heic'|'unsupported'|'empty'|'too-big', message). For 'ok' the
    message is advice, never an error - the caller shows it alongside a success.
    """
    if not data:
        return "empty", "That file was empty."
    if max_bytes and len(data) > max_bytes:
        return "too-big", (
            "That file is larger than the " + _mb_text(max_bytes) + " MB limit."
        )
    kind = sniff_kind(data)
    ext = os.path.splitext(filename or "")[1].lower()
    # Magic wins over extension, so a HEIC renamed to .jpg is still rejected;
    # and a .heic/.heif extension is rejected on its own, which is the safe side
    # of the trade (a black slide on a wall of TVs is far worse than one file
    # the user has to re-save).
    if kind == "heic" or ext in (".heic", ".heif"):
        return "heic", HEIC_MESSAGE
    if kind == "unknown":
        return "unsupported", (
            "Only JPEG, PNG, WebP, GIF and BMP images can be shown on the TVs - "
            + (filename or "that file")
            + " looks like "
            + _describe(data, filename or "")
            + "."
        )
    return "ok", SIZE_ADVICE


def safe_filename(name: str) -> str:
    """Reduce a client-supplied filename to something safe to write.

    Strips any directory component, keeps [A-Za-z0-9._ -], collapses runs of
    whitespace, caps the stem at 80 characters and lower-cases the extension.
    Collision handling and forcing the extension to match the sniffed kind need
    a directory and the file's bytes, so they live in Slideshow._final_name -
    this function keeps the single-argument signature the contract froze.
    """
    raw = (name or "").replace("\\", "/")
    raw = raw.split("/")[-1]
    raw = raw.replace("\x00", "")
    raw = _FILENAME_KEEP.sub("", raw)
    raw = _WS_RUN.sub(" ", raw).strip()
    # Leading dots would create a hidden file (and "." / ".." would escape the
    # folder entirely); leading dashes confuse command lines.
    raw = raw.lstrip(". -")
    stem, ext = os.path.splitext(raw)
    ext = ext.lower()
    stem = stem.strip()
    if len(stem) > 80:
        stem = stem[:80].strip()
    if not stem:
        stem = "photo"
    return stem + ext


def valid_playlist_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if name in (".", ".."):
        return False
    return bool(PLAYLIST_RE.match(name))


def _js_string(value: str) -> str:
    """Escape a value for use inside a single-quoted JavaScript string."""
    out = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\r", "")
        .replace("\n", " ")
    )
    # "</" would end the <script> element early no matter where it appears.
    return out.replace("</", "<\\/")


class Slideshow:
    """The photo library, the playlist pointers, the manifest and the page."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        # Template cache lives on the instance, not in a module global: the
        # contract forbids module-level mutable state, and one Slideshow per
        # process makes an instance cache exactly as effective.
        self._tpl_lock = threading.Lock()
        self._tpl_key: "tuple[str, float] | None" = None
        self._tpl_text: str = ""
        self._tpl_warned = False

    # -- configuration and paths -------------------------------------------

    def _conf(self, section: str, key: str, default):
        data = getattr(self.ctx.config, "data", None)
        if not isinstance(data, dict):
            return default
        sec = data.get(section)
        if isinstance(sec, dict) and sec.get(key) is not None:
            return sec.get(key)
        return default

    def _install_root(self) -> Path:
        """The machine-wide root: <root>/photos, <root>/state live under it."""
        paths = getattr(self.ctx, "paths", None)
        r = getattr(paths, "root", None) or getattr(self.ctx, "root", None)
        if r:
            return Path(str(r))
        env = os.environ.get("TVHUB_HOME")
        if env:
            return Path(env)
        # Contract 1: else the parent of the tvhub package directory. Never a
        # per-user path - a service running as SYSTEM and a CLI run by a user
        # resolve those differently, which once hid 14 valid tokens.
        return Path(__file__).resolve().parent.parent

    def root(self) -> Path:
        # Config.photo_root() already resolves a relative setting against the
        # install root (never the working directory - a service starts in an
        # unpredictable one), so prefer it and keep one implementation.
        getter = getattr(self.ctx.config, "photo_root", None)
        p = None
        if callable(getter):
            try:
                p = Path(getter())
            except Exception as exc:  # noqa: BLE001
                LOG.debug("config.photo_root() failed: %s", exc)
        if p is None:
            raw = self._conf("paths", "photo_root", "photos") or "photos"
            p = Path(str(raw)).expanduser()
            if not p.is_absolute():
                p = self._install_root() / p
        if not p.is_dir():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                LOG.warning("photo root %s is not usable (%s)", p, exc)
        return p

    def _tmp_dir(self) -> Path:
        paths = getattr(self.ctx, "paths", None)
        p = getattr(paths, "tmp_dir", None)
        p = Path(str(p)) if p else self._install_root() / "state" / "tmp"
        if not p.is_dir():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                LOG.warning("upload staging folder %s is not usable (%s)", p, exc)
        return p

    def clear_tmp(self) -> int:
        """Drop staged uploads left behind by a crash (contract 1, at startup).

        Delegates to Paths.clear_tmp when the Context provides it so there is one
        implementation; the local sweep is the fallback. Do not call this while an
        upload is in flight - it would delete a live staging file.
        """
        paths = getattr(self.ctx, "paths", None)
        delegate = getattr(paths, "clear_tmp", None)
        if callable(delegate):
            try:
                return int(delegate())
            except Exception as exc:  # noqa: BLE001
                LOG.debug("paths.clear_tmp() failed: %s", exc)
        removed = 0
        tmp = self._tmp_dir()
        try:
            entries = list(tmp.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if entry.is_file():
                    entry.unlink()
                    removed += 1
                elif entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass
        return removed

    def playlist_dir(self, name: str) -> "Path | None":
        """The folder for `name`, or None if the name is invalid or escapes.

        Every filesystem entry point in this module goes through here, so a
        crafted name can never reach a path outside the photo root.
        """
        if not valid_playlist_name(name):
            return None
        root = self.root()
        candidate = root / name
        try:
            resolved = candidate.resolve()
            root_resolved = root.resolve()
        except OSError:
            return None
        if resolved == root_resolved:
            return None
        if root_resolved not in resolved.parents:
            # Covers symlinks pointing out of the tree as well as traversal.
            return None
        return candidate

    def exists(self, name: str) -> bool:
        d = self.playlist_dir(name)
        return bool(d and d.is_dir())

    # -- listing ------------------------------------------------------------

    @staticmethod
    def _order_key(name: str):
        # Case-insensitive so "Apple.jpg" sorts near "apple.jpg", with the exact
        # name as the tiebreak so the order is stable across platforms.
        return (name.lower(), name)

    def image_names(self, name: str) -> "list[str]":
        d = self.playlist_dir(name)
        if not d or not d.is_dir():
            return []
        out: "list[str]" = []
        try:
            entries = list(d.iterdir())
        except OSError as exc:
            LOG.warning("cannot read playlist %s (%s)", name, exc)
            return []
        for entry in entries:
            fn = entry.name
            if fn.startswith("."):
                continue  # hidden files and our own staging leftovers
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
                continue
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            out.append(fn)
        out.sort(key=self._order_key)
        return out

    def images(self, name: str) -> "list[str]":
        """Page-relative, percent-encoded image paths for the manifest."""
        # safe="" so spaces and anything structural are encoded: these strings
        # are concatenated onto BASE by the page with no further escaping.
        return ["img/" + quote(fn, safe="") for fn in self.image_names(name)]

    def _stats(self, name: str) -> "tuple[int, int, float]":
        d = self.playlist_dir(name)
        if not d or not d.is_dir():
            return 0, 0, 0.0
        total = 0
        newest = 0.0
        try:
            newest = d.stat().st_mtime
        except OSError:
            newest = 0.0
        count = 0
        for fn in self.image_names(name):
            try:
                st = (d / fn).stat()
            except OSError:
                continue
            count += 1
            total += int(st.st_size)
            if st.st_mtime > newest:
                newest = st.st_mtime
        return count, total, newest

    def playlists(self) -> "list[dict]":
        root = self.root()
        rows: "list[dict]" = []
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            LOG.warning("cannot read the photo root %s (%s)", root, exc)
            return rows
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            if not valid_playlist_name(entry.name):
                continue  # not addressable through the HTTP surface, so hide it
            count, nbytes, modified = self._stats(entry.name)
            rows.append(
                {
                    "name": entry.name,
                    "count": count,
                    "bytes": nbytes,
                    # A human-facing timestamp, so time.time()-based (0.10).
                    "modified": modified,
                }
            )
        rows.sort(key=lambda r: self._order_key(r["name"]))
        return rows

    def image_path(self, name: str, filename: str) -> "Path | None":
        """Resolve one image for serving or deleting, or None.

        Re-checks containment and the extension even though the caller already
        went through playlist_dir: this is the one path that takes a filename
        straight off the wire.
        """
        d = self.playlist_dir(name)
        if not d or not d.is_dir():
            return None
        if not filename or filename in (".", ".."):
            return None
        if "/" in filename or "\\" in filename or "\x00" in filename:
            return None
        if os.path.splitext(filename)[1].lower() not in IMAGE_EXTS:
            return None
        candidate = d / filename
        try:
            resolved = candidate.resolve()
            parent = d.resolve()
        except OSError:
            return None
        if resolved.parent != parent:
            return None
        if not resolved.is_file():
            return None
        return candidate

    # -- playlist lifecycle -------------------------------------------------

    def create(self, name: str) -> Result:
        if not valid_playlist_name(name):
            return Result.error(
                "'" + str(name) + "' is not a usable playlist name - use letters, "
                "numbers, spaces, dashes and underscores, starting with a letter "
                "or number."
            )
        d = self.playlist_dir(name)
        if d is None:
            return Result.error("'" + name + "' is not a usable playlist name.")
        if d.is_dir():
            return Result.warn(
                "playlist '" + name + "' already exists",
                playlist=name,
            )
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Result.error("could not create '" + name + "': " + str(exc))
        LOG.info("created playlist %s", name)
        return Result.good(
            "created playlist '" + name + "' - now upload some photos",
            playlist=name, count=0,
        )

    def _pointer_users(self, name: str) -> "tuple[bool, list[str]]":
        shared, per_tv = self._pointers()
        aliases = sorted(a for a, v in per_tv.items() if v == name)
        return shared == name, aliases

    def delete(self, name: str) -> Result:
        d = self.playlist_dir(name)
        if d is None:
            return Result.error("'" + str(name) + "' is not a usable playlist name.")
        if not d.is_dir():
            return Result.error("there is no playlist called '" + name + "'")
        # Contract 8.6: refuse while anything still points at it. Deleting the
        # active playlist would blank every screen that follows it, and the
        # pointer would survive a restart still naming a folder that is gone.
        is_shared, aliases = self._pointer_users(name)
        if is_shared or aliases:
            who = "the shared playlist" if is_shared else ""
            if aliases:
                per = "used by " + ", ".join(aliases)
                who = (who + " and " + per) if who else per
            return Result.error(
                "'" + name + "' is " + who + " - switch to another playlist first, "
                "then delete it",
                playlist=name, shared=is_shared, aliases=aliases,
            )
        try:
            # The containment check in playlist_dir is what makes this safe; do
            # not replace it with a bare path join.
            shutil.rmtree(d)
        except OSError as exc:
            return Result.error("could not delete '" + name + "': " + str(exc))
        LOG.info("deleted playlist %s", name)
        return Result.good("deleted playlist '" + name + "'", playlist=name)

    def rename(self, old: str, new: str) -> Result:
        src = self.playlist_dir(old)
        dst = self.playlist_dir(new)
        if src is None:
            return Result.error("'" + str(old) + "' is not a usable playlist name.")
        if dst is None:
            return Result.error(
                "'" + str(new) + "' is not a usable playlist name - use letters, "
                "numbers, spaces, dashes and underscores."
            )
        if not src.is_dir():
            return Result.error("there is no playlist called '" + old + "'")
        if old == new:
            return Result.warn("that is already its name", playlist=new)
        # On macOS and Windows the filesystem is case-insensitive, so "holiday"
        # -> "Holiday" makes dst.exists() true while still being a real rename.
        same_file = False
        if dst.exists():
            try:
                # samefile compares device and inode. Path.resolve() cannot be
                # used here: on macOS it preserves whatever case was typed, so
                # two names for one folder still compare unequal.
                same_file = os.path.samefile(str(src), str(dst))
            except OSError:
                same_file = False
            if not same_file:
                return Result.error("a playlist called '" + new + "' already exists")
        try:
            os.rename(str(src), str(dst))
        except OSError as exc:
            return Result.error("could not rename '" + old + "': " + str(exc))
        # The folder now exists under the new name, so repointing is safe (I11:
        # never persist a pointer that has not been validated). Repointing
        # rather than refusing keeps the screens alive through a rename; the
        # pages notice the new name on their next 5 s poll and restart cleanly.
        moved: "list[str]" = []
        is_shared, aliases = self._pointer_users(old)
        if is_shared and self._set_shared(new):
            moved.append("the shared playlist")
        for alias in aliases:
            if self._set_for_tv_pointer(alias, new):
                moved.append(alias)
        LOG.info("renamed playlist %s to %s", old, new)
        text = "renamed '" + old + "' to '" + new + "'"
        if moved:
            text += " and repointed " + ", ".join(moved)
        return Result.good(text, playlist=new, repointed=moved)

    # -- uploads ------------------------------------------------------------

    def _final_name(self, folder: Path, raw_name: str, kind: str) -> str:
        """Sanitize, force an honest extension, and dodge collisions."""
        base = safe_filename(raw_name)
        stem, ext = os.path.splitext(base)
        # An extension that lies about the contents means the TV browser guesses
        # from the Content-Type we serve and often guesses wrong, so the sniffed
        # kind decides. A .jpeg holding a JPEG keeps its .jpeg.
        if ext not in _KIND_OK_EXTS.get(kind, ()):
            ext = _KIND_EXT.get(kind, ".jpg")
        candidate = stem + ext
        n = 2
        while (folder / candidate).exists():
            candidate = stem + "-" + str(n) + ext
            n += 1
            if n > 9999:  # pathological, but never loop forever
                candidate = stem + "-" + uuid.uuid4().hex[:8] + ext
                break
        return candidate

    def _install_file(self, staged: Path, dest: Path) -> None:
        """Move a fully-written staged file into place atomically."""
        try:
            os.replace(str(staged), str(dest))
            return
        except OSError:
            # state/tmp and the photo root can sit on different filesystems when
            # photo_root is pointed at another volume, and os.replace cannot
            # cross devices. Copy into the destination folder under a hidden
            # name first, so the rename that publishes it is still atomic and a
            # half-copied file is never served.
            pass
        near = dest.parent / ("." + dest.name + ".part-" + uuid.uuid4().hex[:8])
        try:
            shutil.copyfile(str(staged), str(near))
            os.replace(str(near), str(dest))
        finally:
            for leftover in (near, staged):
                try:
                    if leftover.exists():
                        leftover.unlink()
                except OSError:
                    pass

    def add_uploads(self, name: str, parts: "list[UploadPart]") -> dict:
        d = self.playlist_dir(name)
        if d is None:
            raise LibraryError("'" + str(name) + "' is not a usable playlist name.")
        if not d.is_dir():
            raise LibraryError("There is no playlist called '" + name + "'.")
        getter = getattr(self.ctx.config, "max_upload_bytes", None)
        max_bytes = 0
        if callable(getter):
            try:
                max_bytes = int(getter())
            except Exception as exc:  # noqa: BLE001
                LOG.debug("config.max_upload_bytes() failed: %s", exc)
        if max_bytes <= 0:
            max_bytes = int(self._conf("server", "max_upload_mb", 64) or 64) * 1024 * 1024
        tmp = self._tmp_dir()
        added: "list[str]" = []
        rejected: "list[dict]" = []
        for part in parts:
            if not part.filename:
                continue  # an ordinary form field, not a photo
            label = part.filename
            verdict, message = classify_upload(
                label, part.data, max_bytes=max_bytes
            )
            if verdict != "ok":
                rejected.append(
                    {"name": label, "reason": verdict, "message": message}
                )
                LOG.info("rejected upload %s (%s)", label, verdict)
                continue
            kind = sniff_kind(part.data)
            final = self._final_name(d, label, kind)
            staged = tmp / (uuid.uuid4().hex + "-" + final)
            try:
                with open(str(staged), "wb") as fh:
                    fh.write(part.data)
                    fh.flush()
                    os.fsync(fh.fileno())
                self._install_file(staged, d / final)
            except OSError as exc:
                try:
                    if staged.exists():
                        staged.unlink()
                except OSError:
                    pass
                rejected.append(
                    {
                        "name": label,
                        "reason": "write-failed",
                        "message": "Could not save that file: " + str(exc),
                    }
                )
                continue
            added.append(final)
        if added:
            LOG.info("added %d image(s) to playlist %s", len(added), name)
        total = len(self.image_names(name))
        return {
            "playlist": name,
            "added": added,
            "rejected": rejected,
            # The image count of the playlist now, matching what /api/playlists
            # reports for the same field. added_count is the per-request number.
            "count": total,
            "added_count": len(added),
            "advice": [ORDER_ADVICE, SIZE_ADVICE] if added else [],
        }

    def delete_image(self, name: str, filename: str) -> Result:
        path = self.image_path(name, filename)
        if path is None:
            return Result.error(
                "no image called '" + str(filename) + "' in '" + str(name) + "'"
            )
        try:
            path.unlink()
        except OSError as exc:
            return Result.error("could not delete that image: " + str(exc))
        left = len(self.image_names(name))
        LOG.info("deleted %s from playlist %s", filename, name)
        return Result.good(
            "deleted " + path.name + " - " + str(left) + " image(s) left in '"
            + name + "'",
            playlist=name, count=left,
        )

    # -- pointers -----------------------------------------------------------

    def _pointers(self) -> "tuple[str, dict]":
        """(shared pointer, {alias: playlist}).

        Prefers State's own accessors: they take the state lock, whereas reading
        .data directly races a concurrent write. .data is the fallback so this
        module still works against a leaner State.
        """
        state = self.ctx.state
        shared = ""
        per_tv: dict = {}
        got_shared = False
        got_per_tv = False
        getter = getattr(state, "shared_playlist", None)
        if callable(getter):
            try:
                shared = str(getter() or "")
                got_shared = True
            except Exception as exc:  # noqa: BLE001
                LOG.debug("state.shared_playlist() failed: %s", exc)
        getter = getattr(state, "per_tv_playlists", None)
        if callable(getter):
            try:
                value = getter()
                per_tv = dict(value) if isinstance(value, dict) else {}
                got_per_tv = True
            except Exception as exc:  # noqa: BLE001
                LOG.debug("state.per_tv_playlists() failed: %s", exc)
        if got_shared and got_per_tv:
            return shared, per_tv
        data = getattr(state, "data", None)
        pl = data.get("playlist") if isinstance(data, dict) else None
        if not isinstance(pl, dict):
            return shared, per_tv
        if not got_shared:
            shared = str(pl.get("shared") or "")
        if not got_per_tv:
            raw = pl.get("per_tv")
            per_tv = dict(raw) if isinstance(raw, dict) else {}
        return shared, per_tv

    def _try_state_calls(self, names: "tuple[str, ...]", *args) -> bool:
        """Call the first State method that exists and accepts these arguments.

        store.py owns the pointer mutators and the contract fixes the on-disk
        shape (4.x) without fixing their names, so the write is attempted
        through the plausible names and then CONFIRMED by reading the pointer
        back. Verify by effect, never by "the call did not raise" - the same
        rule the TV protocol taught us.
        """
        for meth_name in names:
            meth = getattr(self.ctx.state, meth_name, None)
            if not callable(meth):
                continue
            try:
                meth(*args)
            except TypeError:
                continue  # a same-named method with a different signature
            except Exception as exc:  # noqa: BLE001
                LOG.debug("state.%s failed: %s", meth_name, exc)
                continue
            return True
        return False

    def _generic_state_write(self, mutate) -> bool:
        for meth_name in ("save", "mutate", "edit", "update", "write"):
            meth = getattr(self.ctx.state, meth_name, None)
            if not callable(meth):
                continue
            try:
                meth(mutate)
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                LOG.debug("state.%s failed: %s", meth_name, exc)
                continue
            return True
        return False

    def _set_shared(self, name: str) -> bool:
        # I11 restated at the last gate before persistence. Callers already
        # pre-check, but store's setter refuses a bad name by raising, and the
        # generic fallback below has no validation of its own - without this an
        # unvalidated name could slip through that back door.
        if not valid_playlist_name(name):
            LOG.error("refusing to store %r as the shared playlist", name)
            return False
        if self._try_state_calls(
            ("set_shared_playlist", "set_playlist_shared", "set_playlist"), name
        ) and self._pointers()[0] == name:
            return True

        def mutate(doc):
            pl = doc.setdefault("playlist", {})
            pl["shared"] = name

        if self._generic_state_write(mutate) and self._pointers()[0] == name:
            return True
        if self._pointers()[0] == name:
            return True
        LOG.error("could not persist the shared playlist pointer (%s)", name)
        return False

    def _set_for_tv_pointer(self, alias: str, name: str) -> bool:
        # An empty name is legitimate here: it clears the override so the TV
        # follows the shared pointer again. Anything else must be a valid name.
        if name and not valid_playlist_name(name):
            LOG.error("refusing to store %r as %s's playlist", name, alias)
            return False
        want = name or None
        # Deliberately NOT "set_playlist" here, even though it is tried for the
        # shared pointer: a store exposing set_playlist(name, alias=None) would
        # accept set_playlist("office", "holiday") and quietly write the ALIAS
        # into the shared pointer, which is exactly the mistake that blanks every
        # screen at once. Only unambiguously per-TV names are attempted.
        if self._try_state_calls(
            ("set_tv_playlist", "set_playlist_for_tv", "set_playlist_for"),
            alias,
            name,
        ):
            if self._pointers()[1].get(alias) == want:
                return True

        def mutate(doc):
            pl = doc.setdefault("playlist", {})
            per = pl.setdefault("per_tv", {})
            if name:
                per[alias] = name
            else:
                per.pop(alias, None)

        if self._generic_state_write(mutate):
            if self._pointers()[1].get(alias) == want:
                return True
        if self._pointers()[1].get(alias) == want:
            return True
        LOG.error("could not persist the playlist pointer for %s (%s)", alias, name)
        return False

    def resolve_for(self, alias: "str | None") -> str:
        """Which playlist this TV should be showing (contract 8.3).

        per-TV pointer -> shared pointer -> config default -> first non-empty
        playlist -> "default". Falling back to the SHARED pointer before the
        config default matters: a TV added after a playlist was chosen must
        inherit the fleet selection, or a bare `show` on it resolves to
        "default" and then clobbers the shared pointer for everyone.
        """
        shared, per_tv = self._pointers()
        if alias:
            mine = per_tv.get(alias)
            # A pointer is honoured when its folder is present even if it is
            # momentarily empty: someone chose it, and silently showing another
            # playlist's photos would be worse than showing none.
            if mine and self.exists(mine):
                return str(mine)
        if shared and self.exists(shared):
            return shared
        default = str(self._conf("slideshow", "default_playlist", "default") or "default")
        if valid_playlist_name(default) and self.image_names(default):
            return default
        for row in self.playlists():
            if row["count"]:
                return str(row["name"])
        # Nothing usable anywhere; the page will say so rather than go black.
        return "default"

    def activate(self, name: str) -> Result:
        """Point the whole fleet at `name`, validating BEFORE persisting."""
        problem = self._pointer_precheck(name)
        if problem is not None:
            return problem
        if not self._set_shared(name):
            return Result.error(
                "could not save the playlist pointer - check that state/ is "
                "writable by the account running the service"
            )
        LOG.info("shared playlist is now %s", name)
        return Result.good(
            "showing '" + name + "' on every TV that follows the shared playlist"
            " - the screens pick it up within about 5 seconds",
            shared=name, count=len(self.image_names(name)),
        )

    def set_for_tv(self, alias: str, name: str) -> Result:
        if not alias or not _ALIAS_RE.match(str(alias)):
            return Result.error("'" + str(alias) + "' is not a valid TV alias")
        if name == "":
            # An empty name clears the override so the TV follows the fleet again.
            if not self._set_for_tv_pointer(alias, ""):
                return Result.error("could not clear the playlist for " + alias)
            return Result.good(
                alias + " now follows the shared playlist ('"
                + self.resolve_for(alias) + "')",
                alias=alias, playlist=self.resolve_for(alias),
            )
        problem = self._pointer_precheck(name)
        if problem is not None:
            return problem
        if not self._set_for_tv_pointer(alias, name):
            return Result.error(
                "could not save the playlist pointer for " + alias
                + " - check that state/ is writable by the account running the service"
            )
        LOG.info("playlist for %s is now %s", alias, name)
        return Result.good(
            alias + " is now showing '" + name + "'",
            alias=alias, playlist=name, count=len(self.image_names(name)),
        )

    def _pointer_precheck(self, name: str) -> "Result | None":
        """Every gate a name must pass BEFORE it is written to state (I11).

        An unvalidated typo once blanked every TV and survived a restart, so a
        playlist becomes a pointer only when it is legal, present, and has at
        least one image the TV browser can actually render.
        """
        if not valid_playlist_name(name):
            return Result.error(
                "'" + str(name) + "' is not a usable playlist name - use letters, "
                "numbers, spaces, dashes and underscores, starting with a letter "
                "or number."
            )
        if not self.exists(name):
            available = ", ".join(r["name"] for r in self.playlists()) or "none yet"
            return Result.error(
                "there is no playlist called '" + name + "' (have: " + available + ")"
            )
        if not self.image_names(name):
            return Result.error(
                "'" + name + "' has no images the TVs can show - upload some "
                "JPEGs first, or the screens would just go black"
            )
        return None

    # -- manifest and page --------------------------------------------------

    def _interval(self) -> int:
        try:
            secs = int(self._conf("slideshow", "interval_seconds", 10) or 10)
        except (TypeError, ValueError):
            secs = 10
        return max(_MIN_INTERVAL, secs)

    def _fit(self) -> str:
        fit = str(self._conf("slideshow", "fit", "contain") or "contain")
        return fit if fit in _FIT_VALUES else "contain"

    def manifest(
        self,
        playlist: str,
        client_ip: str,
        identify_map: "dict[str, tuple[int, str]] | None",
    ) -> dict:
        """The document the page polls every 5 seconds (contract 8.7).

        This poll is the whole switching mechanism, and it is also the ONLY
        proof that a TV is really displaying the slideshow - "browser running"
        is not enough, because Tizen keeps a backgrounded browser loaded while
        freezing its JS timers. So the heartbeat is recorded here, at the point
        where the TV demonstrably asked us for fresh content.
        """
        hb = getattr(self.ctx, "heartbeat", None)
        if hb is not None and client_ip:
            try:
                hb.note(client_ip)
            except Exception as exc:  # noqa: BLE001 - never fail a TV's poll
                LOG.debug("heartbeat.note(%s) failed: %s", client_ip, exc)

        identify = None
        if identify_map is not None:
            hit = identify_map.get(client_ip)
            if hit:
                identify = {"n": hit[0], "alias": hit[1]}
            else:
                # An unrecognised caller still gets a badge so a screen showing
                # "?" plus its IP tells you exactly which TV is unaccounted for.
                identify = {"n": "?", "alias": client_ip or "unknown"}
        return {
            "playlist": playlist,
            "page": self.page_version(),
            "interval": self._interval(),
            "fit": self._fit(),
            "images": self.images(playlist),
            "identify": identify,
            # Shown to humans / used for drift checks, so wall clock (0.10).
            "server_time": time.time(),
        }

    def _template(self) -> "tuple[str, bool]":
        """(template text, came_from_disk), mtime-cached.

        Prefers tvhub/web/slideshow.html so a page fix is a file edit, and falls
        back to the built-in copy. A template with no __PAGEVER__ placeholder is
        refused: it could never tell a TV to reload itself, which would strand
        the fleet on a stale page with no way to fix it remotely.
        """
        path = None
        for holder in (_ui, None):
            if holder is None:
                path = Path(__file__).resolve().parent / "web" / "slideshow.html"
                break
            web = getattr(holder, "WEB_DIR", None) or getattr(holder, "WEB", None)
            if web:
                path = Path(str(web)) / "slideshow.html"
                break
        if path is None:  # pragma: no cover - defensive
            path = Path(__file__).resolve().parent / "web" / "slideshow.html"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return _DEFAULT_PAGE, False
        key = (str(path), mtime)
        with self._tpl_lock:
            if self._tpl_key == key and self._tpl_text:
                return self._tpl_text, True
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            LOG.warning("cannot read %s (%s) - using the built-in page", path, exc)
            return _DEFAULT_PAGE, False
        if "__PAGEVER__" not in text or "__BASE__" not in text:
            with self._tpl_lock:
                warned = self._tpl_warned
                self._tpl_warned = True
            if not warned:
                LOG.warning(
                    "%s is missing __BASE__/__PAGEVER__ - using the built-in page "
                    "so the TVs can still be updated remotely",
                    path,
                )
            return _DEFAULT_PAGE, False
        with self._tpl_lock:
            self._tpl_key = key
            self._tpl_text = text
        return text, True

    def page_version(self) -> str:
        """The version the page compares against its own baked-in copy.

        When the template is the built-in one this is PAGE_VERSION exactly. When
        it comes from disk, a short content hash is appended so editing
        tvhub/web/slideshow.html reaches every TV even if whoever edited it
        forgot to bump PAGE_VERSION - the version is only useful if it is
        impossible to forget.
        """
        text, from_disk = self._template()
        if not from_disk:
            return PAGE_VERSION
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
        return PAGE_VERSION + "-" + digest

    def page_html(
        self, base_path: str, playlist: str, interval: int, fit: str
    ) -> str:
        text, _ = self._template()
        base = str(base_path or "/")
        if not (base.startswith("http://") or base.startswith("https://")):
            if not base.startswith("/"):
                base = "/" + base
        # BASE must end in "/": the page URL itself has no trailing slash, so a
        # relative "manifest.json" would resolve one directory too high and lose
        # the playlist name entirely.
        if not base.endswith("/"):
            base += "/"
        try:
            secs = int(interval)
        except (TypeError, ValueError):
            secs = self._interval()
        secs = max(_MIN_INTERVAL, secs)
        fit_value = fit if fit in _FIT_VALUES else "contain"
        name = str(playlist or "")
        values = {
            "BASE": _js_string(base),
            "PLAYLIST": _js_string(name),
            "TITLE": _html.escape(name or "slideshow"),
            "SECS": str(secs),
            "FIT": fit_value,
            "PAGEVER": _js_string(self.page_version()),
        }
        # One pass, not a chain of str.replace calls: a playlist name may legally
        # contain underscores, so a playlist called "__SECS__" would otherwise be
        # substituted a second time and corrupt the page.
        return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)


_PLACEHOLDER_RE = re.compile(r"__(BASE|PLAYLIST|TITLE|SECS|FIT|PAGEVER)__")


# ---------------------------------------------------------------------------
# The page. Every item here was measured on real Samsung hardware; the comments
# say why, because each one looks removable and none of them is.
#
# Kept in this module as well as on disk so a fresh install shows photos before
# anyone has touched tvhub/web/. If you edit the file on disk, page_version()
# notices and every TV reloads itself within 5 seconds.
# ---------------------------------------------------------------------------
_DEFAULT_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  html{background:#000}
  /* Deliberately taller than the viewport. The Tizen browser only auto-hides
     its address and menu bars once the page CAN scroll, and some Frames never
     hide them otherwise. The image layers are fixed, so scrolling moves no
     pixels - the page is scrollable but visually still. */
  body{margin:0;padding:0;background:#000;cursor:none;min-height:130vh;
       overflow-x:hidden;-webkit-user-select:none;user-select:none}
  /* The page must be scrollable, but the scrollbar itself would sit on show
     over the photos. */
  html,body{scrollbar-width:none;-ms-overflow-style:none}
  html::-webkit-scrollbar,body::-webkit-scrollbar{width:0;height:0;display:none;
       background:transparent}
  /* Longhand top/right/bottom/left offsets rather than the inset shorthand:
     these pages run on Tizen browser builds as old as 2019, which do not
     understand it. (No backticks anywhere in this file - ES5 only, and a
     stray one would mean a template literal had crept in.) */
  .f{position:fixed;top:0;right:0;bottom:0;left:0;background-repeat:no-repeat;
     background-position:center center;background-size:__FIT__;opacity:0;
     transition:opacity 1.2s ease-in-out}
  .f.on{opacity:1}
  #msg{position:fixed;top:0;right:0;bottom:0;left:0;display:-webkit-box;
       display:flex;-webkit-box-align:center;align-items:center;
       -webkit-box-pack:center;justify-content:center;color:#6b7280;
       font:400 2vw/1.5 system-ui,-apple-system,sans-serif;text-align:center;
       padding:4vw;z-index:5}
  /* Identify: every TV shares one homepage URL, so the only way to label each
     screen is per client IP from the manifest. */
  #ident{position:fixed;top:0;right:0;bottom:0;left:0;display:none;
         -webkit-box-align:center;align-items:center;-webkit-box-pack:center;
         justify-content:center;background:#0b1020;color:#fff;text-align:center;
         z-index:9}
  #ident.show{display:-webkit-box;display:flex}
  #identnum{display:block;font:800 44vh/1 system-ui,-apple-system,sans-serif;
            letter-spacing:-.02em}
  #identname{display:block;margin-top:2vh;color:#7fd1ff;font-style:normal;
             font:600 4vh/1.2 system-ui,-apple-system,sans-serif}
</style></head><body>
<div class="f" id="a"></div><div class="f" id="b"></div>
<div id="msg">Loading __TITLE__ ...</div>
<div id="ident"><div><span id="identnum">0</span><em id="identname"></em></div></div>
<script>
/* ES5 only: var, XMLHttpRequest, no arrow functions, no fetch, no template
   literals, no optional chaining. This runs on 2019-vintage Tizen builds. */
(function () {
  "use strict";

  /* BASE is absolute and ends in "/": the page URL has no trailing slash, so
     relative paths would resolve one directory too high and lose the playlist. */
  var BASE = '__BASE__';
  var PAGEVER = '__PAGEVER__';
  var PLAYLIST = '__PLAYLIST__';
  var SECS = __SECS__;

  var LIST = [];
  var i = -1;
  var front = 0;
  var slideTimer = null;
  var loading = false;
  var errorRun = 0;
  var seenInterval = null;
  var seenFit = null;

  var L = [document.getElementById('a'), document.getElementById('b')];
  var msg = document.getElementById('msg');
  var ident = document.getElementById('ident');
  var identNum = document.getElementById('identnum');
  var identName = document.getElementById('identname');

  function say(text) {
    if (!msg) { return; }
    msg.textContent = text || '';
    msg.style.display = text ? '' : 'none';
  }

  /* Push the page down so the browser treats it as scrolled and hides its
     address and menu bars. Never scroll back to the very top - offset zero
     re-reveals them, which is why the only scroll call in this file is the
     one below and it always passes 90. */
  function hideChrome() {
    try { window.scrollTo(0, 90); } catch (e) {}
  }

  /* The Fullscreen API only fires from a genuine user gesture: a synthesised
     click is rejected, but a real remote keypress arrives here as a keydown, so
     the server sends one KEY_ENTER after the page loads. Never call this from a
     timer - it would burn the gesture and some builds then ignore later ones. */
  function goFullscreen() {
    var el = document.documentElement;
    var already = document.fullscreenElement || document.webkitFullscreenElement ||
                  document.webkitIsFullScreen;
    if (already) { return; }
    var fn = el.requestFullscreen || el.webkitRequestFullscreen ||
             el.webkitRequestFullScreen || el.mozRequestFullScreen ||
             el.msRequestFullscreen;
    if (!fn) { return; }
    try { fn.call(el); } catch (e) {}
  }

  function onGesture() { goFullscreen(); hideChrome(); }
  document.addEventListener('keydown', onGesture, false);
  document.addEventListener('click', onGesture, false);
  document.addEventListener('touchstart', onGesture, false);

  function paint(url) {
    var back = L[1 - front];
    back.style.zIndex = '2';
    L[front].style.zIndex = '1';
    back.style.backgroundImage = 'url("' + url.replace(/"/g, '%22') + '")';
    back.className = 'f on';
    L[front].className = 'f';
    front = 1 - front;
  }

  function advance() {
    if (slideTimer) { clearTimeout(slideTimer); slideTimer = null; }
    if (!LIST.length) {
      /* Stop cleanly rather than spinning: the 5 s manifest poll restarts the
         run the moment photos appear. */
      loading = false;
      say('No photos in this playlist yet.');
      L[0].className = 'f';
      L[1].className = 'f';
      return;
    }
    i = (i + 1) % LIST.length;
    var url = BASE + LIST[i];
    loading = true;
    var pre = new Image();
    /* Swap only once the bytes are decoded, so a slow read never shows a
       half-painted frame. */
    pre.onload = function () {
      loading = false;
      errorRun = 0;
      say('');
      paint(url);
      slideTimer = setTimeout(advance, Math.max(2, SECS) * 1000);
    };
    pre.onerror = function () {
      loading = false;
      errorRun = errorRun + 1;
      /* A file can vanish mid-run when someone prunes the playlist from their
         phone, so skip on instead of freezing on a broken frame. If the whole
         list fails, back off rather than hammering the server. */
      if (errorRun > LIST.length + 1) {
        errorRun = 0;
        say('Waiting for photos.');
        slideTimer = setTimeout(advance, 5000);
      } else {
        slideTimer = setTimeout(advance, 200);
      }
    };
    pre.src = url;
  }

  function setIdentify(info) {
    if (!ident) { return; }
    if (!info) { ident.className = ''; return; }
    /* textContent, not innerHTML: the alias is data, not markup. */
    identNum.textContent = (info.n === 0 || info.n) ? String(info.n) : '?';
    identName.textContent = info.alias ? String(info.alias) : '';
    ident.className = 'show';
  }

  function apply(m) {
    /* A page fix reaches every TV by bumping the version: the page reloads
       itself, so nobody has to walk to a screen with a remote. */
    if (m.page && String(m.page) !== PAGEVER) {
      try { location.reload(true); } catch (e) { location.reload(); }
      return;
    }
    setIdentify(m.identify);

    /* Follow the manifest for interval and fit only once the server-side value
       CHANGES. Seeding on the first poll means a page opened with ?s= or ?fit=
       keeps its own override until someone really changes the setting. */
    if (typeof m.fit === 'string' && (m.fit === 'contain' || m.fit === 'cover')) {
      if (seenFit === null) {
        seenFit = m.fit;
      } else if (m.fit !== seenFit) {
        seenFit = m.fit;
        L[0].style.backgroundSize = m.fit;
        L[1].style.backgroundSize = m.fit;
      }
    }
    if (typeof m.interval === 'number' && m.interval >= 2) {
      if (seenInterval === null) { seenInterval = m.interval; }
      else if (m.interval !== seenInterval) { seenInterval = m.interval; SECS = m.interval; }
    }

    var next = m.images && m.images.length ? m.images : [];
    var name = typeof m.playlist === 'string' && m.playlist ? m.playlist : PLAYLIST;
    if (name !== PLAYLIST) {
      /* A different playlist restarts from its first image, immediately. This
         5 s poll is how a switch reaches a page that is already open, which is
         the whole architecture on firmware that will not take a URL. */
      PLAYLIST = name;
      LIST = next;
      i = -1;
      errorRun = 0;
      advance();
      return;
    }
    if (next.join('|') !== LIST.join('|')) {
      /* Same playlist, changed folder: update the list in place so the run does
         not jump back to the first photo every time someone adds one. */
      LIST = next;
      if (i >= LIST.length) { i = LIST.length - 1; }
      if (!slideTimer && !loading && LIST.length) { advance(); }
    }
  }

  function poll() {
    var x = new XMLHttpRequest();
    var done = false;
    function again() {
      if (done) { return; }
      done = true;
      setTimeout(poll, 5000);
    }
    try {
      x.open('GET', BASE + 'manifest.json?t=' + (new Date()).getTime(), true);
    } catch (e) {
      again();
      return;
    }
    x.onreadystatechange = function () {
      if (x.readyState !== 4) { return; }
      if (x.status >= 200 && x.status < 300) {
        var m = null;
        try { m = JSON.parse(x.responseText); } catch (e) { m = null; }
        if (m) { apply(m); }
      } else if (!LIST.length) {
        say('Waiting for the photo server.');
      }
      again();
    };
    x.onerror = again;
    x.ontimeout = again;
    try { x.send(null); } catch (e) { again(); }
  }

  hideChrome();
  setTimeout(hideChrome, 1500);      /* again once layout has settled */
  setInterval(hideChrome, 15000);    /* and keep it hidden for good */
  poll();
})();
</script></body></html>
"""
