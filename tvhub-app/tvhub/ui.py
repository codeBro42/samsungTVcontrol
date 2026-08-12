"""tvhub.ui - the browser front end.

This module owns everything under ``tvhub/web``:

    base.css        one stylesheet for every admin page
    app.js          one script: helpers, a delegated listener, and the four pages
    dashboard.html  the fleet view              (PAGES[0])
    setup.html      the six-step first-run wizard (PAGES[1])
    photos.html     playlists and uploads       (PAGES[2])
    remote.html     per-TV detail, on-screen remote, macro recorder (PAGES[3])
    slideshow.html  the TV-facing template that slideshow.py fills in
    icon.svg        favicon / brand mark

It is deliberately thin: an asset loader with a cache-busting version, an
mtime-cached template reader, and one substitution pass. Every page talks to the
server only through the documented JSON API (contract 9.5), so the whole front
end can be developed against fixtures before fleet.py exists.

Two rules from the contract are enforced by construction here rather than by
review:

* 10.2 - no inline ``onclick``. Escaping a quoted handler out of Python, into an
  HTML attribute, into a JS string broke this page twice. Every control in every
  template carries ``data-action`` / ``data-alias`` / ``data-arg`` attributes and
  ``app.js`` installs exactly ONE delegated listener, bound to both ``click`` and
  ``touchstart`` (a phone with no mouse otherwise gets a dead UI). The templates
  contain no script beyond the one ``window.BOOT`` assignment, which also means
  all of the JavaScript can be syntax-checked as a single real .js file.
* 8.8 - slideshow.html is a *template*, not a page: slideshow.py substitutes its
  placeholders. Anything about it that looks redundant (a 130vh body, a scroll to
  90 that never returns to 0, XMLHttpRequest instead of fetch) is a measured
  Tizen workaround and is commented as such in the file itself.

BOOT
----
``page(name, boot)`` embeds ``boot`` as ``window.BOOT``. The pages treat it as
hints only and re-fetch everything they render from the JSON API, so a caller
that passes ``{}`` still gets a working page. Keys that are used when present:

    page            str   - the page name; injected automatically
    asset_version   str   - injected automatically
    alias           str   - remote.html: which TV. Falls back to the last
                            segment of location.pathname when absent.
    label           str   - remote.html: nicer document title
    local_ips       list  - setup.html step 1: candidate server addresses
                            (webapp gets these from local_ipv4_addresses()).
                            When absent the page suggests the address the admin
                            is already using, which is always available.
    base_url        str   - setup.html step 1 prefill
    homepage_url    str   - setup.html step 5 / remote.html prefill

Nothing here reads config or state, and nothing here performs device I/O.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from .store import Context

log = logging.getLogger("tvhub")

# Bumped BY HAND whenever any file under tvhub/web changes. It is appended as
# ?v=<ASSET_VERSION> to every asset URL, so a browser that cached last week's
# app.js picks up the new one without anybody clearing anything. Note this is
# NOT the slideshow's PAGE_VERSION: that one lives in slideshow.py and must be
# bumped separately when slideshow.html changes (contract 8.8f), because it is
# what makes a TV already sitting on the page reload itself.
ASSET_VERSION = "1.0.0"

PAGES: Tuple[str, ...] = ("dashboard", "setup", "photos", "remote")

WEB_FILES: Tuple[str, ...] = (
    "base.css",
    "app.js",
    "dashboard.html",
    "setup.html",
    "photos.html",
    "remote.html",
    "slideshow.html",
    "icon.svg",
)

SLIDESHOW_TEMPLATE = "slideshow.html"

# The nav is identical on every page: (page name, href, label). The per-TV page
# is reached from the dashboard, so it highlights nothing.
NAV_ITEMS: Tuple[Tuple[str, str, str], ...] = (
    ("dashboard", "/ui/", "TVs"),
    ("photos", "/ui/photos", "Photos"),
    ("setup", "/ui/setup", "Setup"),
)

_PAGE_TITLES: Dict[str, str] = {
    "dashboard": "TVs",
    "setup": "Setup",
    "photos": "Photos",
    "remote": "TV",
}

# Only these are served through /ui/assets/<file>. .html is deliberately absent:
# a template is meaningless before substitution, and there is exactly one route
# per resource.
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
    ".woff2": "font/woff2",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

_PLACEHOLDERS = ("__BOOT__", "__ASSET_V__", "__TITLE__", "__NAV__")
_MARKER_RE = re.compile("|".join(re.escape(p) for p in _PLACEHOLDERS))


def _default_web_dir() -> Path:
    """The shipped asset directory, which lives inside the package.

    Deliberately derived from ``__file__`` and not from ``$TVHUB_HOME``: assets
    are part of the code, while config/state/photos are the machine's data
    (contract 1). An install that moved its state folder must still find its CSS.
    """
    return Path(__file__).resolve().parent / "web"


# Module-level so slideshow.py can find slideshow.html without constructing a
# UI: it reads `ui.WEB_DIR` and only falls back to a built-in copy of the page
# if that is missing. Two divergent copies of the TV page is exactly the sort of
# thing that strands a fleet on a stale page, so keep this exported.
WEB_DIR: Path = _default_web_dir()


class UI:
    """Loads and renders everything under tvhub/web."""

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        # `web_dir` on the Context is an escape hatch for tests; production
        # never sets it.
        override = getattr(ctx, "web_dir", None)
        base = Path(override) if override else WEB_DIR
        try:
            self._web = base.resolve()
        except OSError:  # pragma: no cover - unresolvable path
            self._web = base
        if not self._web.is_dir():
            # Worth saying once and loudly: every admin page and the TV page
            # itself come from here, so a missing folder is a broken install
            # rather than a cosmetic problem.
            log.warning("asset folder %s is missing - the web interface cannot render", self._web)
        self._lock = threading.Lock()
        # name -> (mtime, size, value). Two caches because one holds text for
        # substitution and the other bytes for the wire.
        self._text: Dict[str, Tuple[float, int, str]] = {}
        self._bytes: Dict[str, Tuple[float, int, bytes]] = {}
        self._warned: Dict[str, bool] = {}

    # ---------------------------------------------------------------- paths

    @property
    def web_dir(self) -> Path:
        return self._web

    def _resolve(self, name: str) -> Optional[Path]:
        """Map an asset name to a real file inside tvhub/web, or None.

        Assets are one flat directory, so any separator, NUL or dot-dot in the
        name is a traversal attempt and is refused outright - cheaper to reason
        about than normalising it. The resolved path is then re-checked against
        the resolved web dir so a symlink cannot lead out either.
        """
        if not name or name in (".", ".."):
            return None
        if "/" in name or "\\" in name or "\x00" in name:
            return None
        try:
            real = (self._web / name).resolve()
        except OSError:
            return None
        try:
            real.relative_to(self._web)
        except ValueError:
            return None
        if not real.is_file():
            return None
        return real

    @staticmethod
    def _filename(name: str) -> str:
        """Accept either a page name ('dashboard') or a file name."""
        name = (name or "").strip()
        if name and "." not in name:
            return name + ".html"
        return name

    # --------------------------------------------------------------- assets

    def asset(self, name: str) -> Optional[Tuple[bytes, str]]:
        """Return ``(body, content_type)`` for /ui/assets/<name>, else None."""
        path = self._resolve(name)
        if path is None:
            return None
        ctype = _CONTENT_TYPES.get(path.suffix.lower())
        if ctype is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        key = path.name
        stamp = (st.st_mtime, st.st_size)
        with self._lock:
            hit = self._bytes.get(key)
            if hit is not None and (hit[0], hit[1]) == stamp:
                return hit[2], ctype
        try:
            body = path.read_bytes()
        except OSError as exc:
            log.warning("cannot read asset %s: %s", key, exc)
            return None
        with self._lock:
            self._bytes[key] = (stamp[0], stamp[1], body)
        return body, ctype

    # ------------------------------------------------------------ templates

    def template(self, name: str) -> str:
        """Return the raw text of a template, mtime-cached.

        Accepts 'dashboard' or 'dashboard.html'. Raises FileNotFoundError when
        the file is missing or is not one of WEB_FILES - a broken install, which
        should surface as a 500 with a traceback rather than a blank page.
        """
        fname = self._filename(name)
        if fname not in WEB_FILES:
            raise FileNotFoundError(
                "unknown template %r - expected one of: %s" % (name, ", ".join(WEB_FILES))
            )
        path = self._resolve(fname)
        if path is None:
            raise FileNotFoundError(
                "missing asset %s in %s - the install is incomplete" % (fname, self._web)
            )
        try:
            st = path.stat()
        except OSError as exc:  # pragma: no cover - raced deletion
            raise FileNotFoundError("cannot stat %s: %s" % (path, exc))
        stamp = (st.st_mtime, st.st_size)
        with self._lock:
            hit = self._text.get(fname)
            if hit is not None and (hit[0], hit[1]) == stamp:
                return hit[2]
        text = path.read_text(encoding="utf-8")
        with self._lock:
            self._text[fname] = (stamp[0], stamp[1], text)
        return text

    def slideshow_template(self) -> str:
        """The TV-facing page template. slideshow.py substitutes its markers."""
        return self.template(SLIDESHOW_TEMPLATE)

    # ----------------------------------------------------------- rendering

    def page(self, name: str, boot: Dict[str, Any]) -> str:
        """Render one admin page.

        Substitutes __BOOT__, __ASSET_V__, __TITLE__ and __NAV__ by plain
        str.replace - no template engine, no dependency, and nothing that can
        reinterpret a photo filename as syntax.
        """
        if name not in PAGES:
            raise ValueError("unknown page %r - expected one of: %s" % (name, ", ".join(PAGES)))
        text = self.template(name)
        payload = dict(boot or {})
        # Injected rather than required, so UI.page("dashboard", {}) works: the
        # front end must never depend on the caller remembering to say which
        # page it is.
        payload.setdefault("page", name)
        payload.setdefault("asset_version", ASSET_VERSION)
        values = {
            "__BOOT__": self._boot_json(payload),
            "__ASSET_V__": ASSET_VERSION,
            "__TITLE__": html.escape(self._title(name, payload), quote=True),
            "__NAV__": self._nav(name),
        }
        self._check_template(name, text)
        # ONE pass over the template, not four chained str.replace calls:
        # substituted text is never rescanned, so a TV label or playlist name
        # that happens to contain "__BOOT__" cannot cause the boot blob to be
        # injected into the page title (or vice versa).
        return _MARKER_RE.sub(lambda m: values[m.group(0)], text)

    def version(self) -> str:
        """The value webapp appends as ?v= to asset URLs."""
        return ASSET_VERSION

    # ----------------------------------------------------------- internals

    @staticmethod
    def _boot_json(payload: Dict[str, Any]) -> str:
        """JSON for embedding in a <script> block.

        ensure_ascii keeps U+2028/U+2029 escaped (they are line terminators in
        JS but not in JSON). '</' is escaped because '</script' anywhere inside
        - including inside a photo filename - would end the block early;
        '<!--' likewise for older parsers. Both forms stay valid JSON/JS.
        """
        try:
            blob = json.dumps(payload, ensure_ascii=True, default=str)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            log.warning("boot payload is not JSON-serialisable (%s); sending {}", exc)
            blob = "{}"
        return blob.replace("</", "<\\/").replace("<!--", "<\\!--")

    @staticmethod
    def _nav(active: str) -> str:
        parts = []
        for page, href, label in NAV_ITEMS:
            cls = "navlink on" if page == active else "navlink"
            current = ' aria-current="page"' if page == active else ""
            parts.append(
                '<a class="%s" href="%s"%s>%s</a>' % (cls, href, current, html.escape(label))
            )
        return '<nav class="nav">%s</nav>' % "".join(parts)

    @staticmethod
    def _title(name: str, boot: Dict[str, Any]) -> str:
        explicit = boot.get("title")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        if name == "remote":
            who = boot.get("label") or boot.get("alias")
            if isinstance(who, str) and who.strip():
                return who.strip()
        return _PAGE_TITLES.get(name, name)

    def _check_template(self, name: str, text: str) -> None:
        """Warn once per template if it is missing a marker we substitute.

        A template that lost its __BOOT__ marker renders as a page whose
        JavaScript silently does nothing, which is a miserable thing to debug.
        Checked on the TEMPLATE, never on the rendered output: a TV label or
        playlist name is free to contain the literal text "__BOOT__", and that
        is not a fault.
        """
        missing = [p for p in _PLACEHOLDERS if p not in text]
        if not missing or self._warned.get(name):
            return
        self._warned[name] = True
        log.warning("%s.html has no %s marker", name, " or ".join(missing))
