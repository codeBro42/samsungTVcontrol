"""tvhub.service - running TVHub as a service, and the command line.

This is the only module that touches the operating system outside the install
directory (scheduled tasks, systemd units, firewall rules) and the only one that
ever needs elevation. Nothing imports it (contract 0.5); it imports everything.

What lives here:

    setup_logging   one logger, a rotating file in state/ plus stdout (0.9)
    build           wire Context -> UI -> Fleet -> Slideshow -> App
    cmd_run         the service entry point: background loops + HTTP server
    cmd_doctor      the one command that answers "why is that TV blank?"
    cmd_pair        terminal pairing, deliberately ONE TV AT A TIME
    cmd_scan        terminal discovery
    cmd_learn       probe and cache what a TV can actually do
    install         Windows scheduled task as SYSTEM at boot; Linux systemd unit

Two themes run through the whole file and explain most of its odd corners.

1.  A service and a human run the same code but resolve paths differently. State
    MUST come from the machine-wide folder next to the install and never from a
    per-user directory (contract 1, invariant I14): a service running as SYSTEM
    and a CLI run by a logged-in user once resolved %LOCALAPPDATA% differently,
    which silently hid fourteen valid pairing tokens from the service. So the
    root is resolved once, exported as $TVHUB_HOME so every module and every
    child process agrees, and printed by `doctor`.

2.  `python -m tvhub run` only imports when the package is importable. A task
    launched by the Windows scheduler starts in the system directory, and a
    package pip-installed with --user is invisible to SYSTEM. `install` therefore
    MEASURES importability before choosing the command line it registers, rather
    than assuming, and says which form it used.

This module deliberately tolerates small differences in the constructor and
method names of its sibling modules (see "cross-module calls" below). The frozen
contract pins the wire protocols, the route table and the file formats, but it
does not pin, for example, whether the HTTP server is reached through
`App.serve_forever()` or `webapp.make_server(ctx, app)`. Since those siblings are
written in parallel against the same contract, service.py resolves such calls by
signature instead of guessing one spelling and failing at runtime. Every one of
those lookups is small, local, and commented.
"""

from __future__ import annotations

import ctypes
import errno
import importlib
import inspect
import logging
import logging.handlers
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:  # imported for annotations only - see the dependency note above
    from tvhub.fleet import Fleet
    from tvhub.slideshow import Slideshow
    from tvhub.store import Context
    from tvhub.ui import UI
    from tvhub.webapp import App

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

COMMANDS: Tuple[str, ...] = (
    "run",
    "install",
    "uninstall",
    "doctor",
    "pair",
    "scan",
    "show",
    "playlist",
    "learn",
    "version",
    "help",
)

TASK_NAME: str = "TVHub"
UNIT_NAME: str = "tvhub.service"

#: Windows firewall rule name (contract 12.2). Deleted before it is added so a
#: re-install cannot stack duplicate rules - netsh will happily create three.
FIREWALL_RULE_NAME: str = "TVHub HTTP"

#: Where the systemd unit goes. A plain string, not a Path: it is only ever
#: written on Linux and a Path would render with backslashes if this module were
#: ever inspected on Windows.
UNIT_PATH: str = "/etc/systemd/system/" + UNIT_NAME

#: The closed verb set from contract 2.3, mirrored here so a mistyped one-shot
#: path is refused before it reaches a TV. Sourced from store.VERBS when that
#: module exports it, so the two can never drift.
VERBS: Tuple[str, ...] = (
    "on",
    "off",
    "toggle",
    "wake",
    "status",
    "show",
    "stop",
    "reopen",
    "fullscreen",
    "key",
    "keys",
    "macro",
    "app",
    "volume",
    "mute",
    "pair",
    "verify",
)

#: Verbs that carry an argument in the path, e.g. ``key/KEY_UP``.
_ARG_VERBS: Tuple[str, ...] = ("show", "key", "keys", "macro", "app", "volume", "mute")

#: Verbs that cannot do anything useful without their argument.
_ARG_REQUIRED: Tuple[str, ...] = ("key", "keys", "macro", "app", "volume", "mute")

#: Mirrors the displayable set in contract 8.5. Only used to count images for
#: `doctor` when slideshow.py is not reachable for the question.
_IMAGE_EXTS: Tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

LOG_FILENAME: str = "tvhub.log"
LOG_MAX_BYTES: int = 2 * 1024 * 1024
LOG_BACKUPS: int = 3

#: The one logger (contract 0.9). getLogger returns the same object every time,
#: so this is a handle, not mutable module state.
log = logging.getLogger("tvhub")

_DEFAULT_PORT = 8899
_DEFAULT_CLIENT_NAME = "TVHub"
_DEFAULT_PAIR_WAIT = 90


class _DependencyError(RuntimeError):
    """A third-party package named in requirements.txt is missing or wrong."""


# --------------------------------------------------------------------------
# cross-module calls
#
# The helpers below let this module call a sibling whose exact spelling the
# contract leaves open, without guessing. They are used ONLY for that; anything
# the contract pins by name (samsung.device_info, Config.reload, Fleet.status,
# Fleet.pair, slideshow.activate, ...) is called directly.
# --------------------------------------------------------------------------


class _NoOp:
    """A callable that does nothing and is falsy.

    Handed out for unknown members of the terminal job handle below. Falsy
    matters: a caller testing ``if handle.cancelled:`` must see "no", while a
    caller doing ``handle.whatever(...)`` must not crash a diagnostic command.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<no-op>"


_NOOP = _NoOp()


def _pick(obj: Any, *names: str) -> Optional[Any]:
    """Return the first callable attribute of `obj` from `names`, else None."""
    for name in names:
        attr = getattr(obj, name, None)
        if callable(attr):
            return attr
    return None


def _call_fitting(fn: Any, *args: Any) -> Any:
    """Call `fn` with as many leading `args` as its signature will accept."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins and odd callables
        return fn(*args)
    kinds = [p.kind for p in sig.parameters.values()]
    if inspect.Parameter.VAR_POSITIONAL in kinds:
        return fn(*args)
    slots = sum(
        1
        for k in kinds
        if k in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    return fn(*args[:slots])


def _call_with(fn: Any, offers: Sequence[Tuple[Any, Tuple[str, ...]]]) -> Any:
    """Call `fn`, passing each offered value under whichever name it declares.

    `offers` is a sequence of ``(value, candidate_parameter_names)``. Matching by
    name rather than by position is what makes this safe: a constructor written
    as ``__init__(self, ctx, ui)`` and one written as ``__init__(self, ctx)`` both
    get exactly what they asked for, and neither is handed an argument meant for
    the other. If the callee ends up with a required parameter we could not name,
    we fall back to positional order - which is the conventional spelling anyway.
    """
    values = [value for value, _ in offers]
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*values)

    params = sig.parameters
    kwargs: Dict[str, Any] = {}
    for value, names in offers:
        for name in names:
            param = params.get(name)
            if param is None or name in kwargs:
                continue
            if param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                kwargs[name] = value
                break

    unfilled = [
        p
        for p in params.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and p.name not in kwargs
    ]
    if unfilled:
        return _call_fitting(fn, *values)
    return fn(**kwargs)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a mapping or an object, whichever we were handed.

    Status rows are specified as JSON (contract 9.6) but may reach us as a
    dataclass; a diagnostic command must not care which.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    value = getattr(obj, key, default)
    if callable(value) and key not in ("render",):
        try:
            return value()
        except TypeError:
            return default
    return value


def _render(result: Any) -> str:
    """Render a Result the one way the whole product renders one (contract 0.8).

    ok -> text, warn -> "WARNING text", error -> "ERROR text".
    """
    if result is None:
        return "ERROR no result"
    if isinstance(result, str):
        return result
    own = _pick(result, "render", "as_text", "line")
    if own is not None:
        try:
            rendered = own()
            if isinstance(rendered, str) and rendered:
                return rendered
        except TypeError:
            pass
    text = _get(result, "text", None)
    if text is None:
        text = _get(result, "message", None)
    text = "" if text is None else str(text)
    level = str(_get(result, "level", "") or "").lower()
    if level == "error":
        return "ERROR " + text
    if level == "warn":
        return "WARNING " + text
    if bool(_get(result, "ok", True)):
        return text
    # No level, not ok: still must not read as success.
    return "ERROR " + text


def _failed(line: str) -> bool:
    return line.startswith("ERROR") or line.startswith("WARNING") or " ERROR " in line or " WARNING " in line


# --------------------------------------------------------------------------
# root, paths and config
# --------------------------------------------------------------------------


def _resolve_root(root: Optional[Any] = None) -> Path:
    """Resolve the install root and export it as $TVHUB_HOME.

    Contract 1: root is $TVHUB_HOME, else the parent of the package directory.
    Exporting it is not cosmetic - every sibling module resolves the root for
    itself, and the installers hand it to a child process that starts in a
    different working directory. One authoritative value, set before anything
    else is constructed, is what keeps a `--root` run from reading one config and
    writing another state file.
    """
    if root:
        path = Path(str(root)).expanduser()
    else:
        env = os.environ.get("TVHUB_HOME", "").strip()
        path = Path(env).expanduser() if env else Path(__file__).resolve().parent.parent
    try:
        path = path.resolve()
    except OSError:  # pragma: no cover - unresolvable path stays as given
        pass
    os.environ["TVHUB_HOME"] = str(path)
    return path


def _root_of(ctx: Any) -> Path:
    """The install root, ALWAYS as an absolute path.

    Absolute is not a nicety here. This value is written into a scheduled task's
    command line, into a systemd unit's WorkingDirectory and $TVHUB_HOME, and
    into every path `doctor` prints. A relative root would silently resolve
    against whatever directory the service happened to start in - which is the
    SYSTEM-versus-user divergence that once hid fourteen pairing tokens from the
    service (contract 1, invariant I14). So whatever the Context reports, anchor
    it before it escapes this module.
    """
    for name in ("root", "home", "base_dir"):
        value = getattr(ctx, name, None)
        if value:
            path = Path(str(value))
            if path.is_absolute():
                return path
            try:
                return path.resolve()
            except OSError:  # pragma: no cover - unresolvable path
                return Path(os.path.abspath(str(path)))
    return _resolve_root(None)


def _abs(path: Path, ctx: Any) -> Path:
    """Anchor a path the Context handed us against the install root.

    Same reason as _root_of: these strings end up in a service command line, a
    unit file and every diagnostic, where a relative path resolves against the
    wrong directory and is very hard to spot.
    """
    if path.is_absolute():
        return path
    return _root_of(ctx) / path


def _state_dir(ctx: Any) -> Path:
    for name in ("state_dir", "state_path", "state_file"):
        value = getattr(ctx, name, None)
        if value:
            path = Path(str(value))
            # state_path may point at state.json itself.
            return _abs(path.parent if path.suffix else path, ctx)
    return _root_of(ctx) / "state"


def _config_path(ctx: Any) -> Path:
    for name in ("config_path", "config_file"):
        value = getattr(ctx, name, None)
        if value:
            return _abs(Path(str(value)), ctx)
    cfg = getattr(ctx, "config", None)
    for name in ("path", "file"):
        value = getattr(cfg, name, None)
        if value:
            return _abs(Path(str(value)), ctx)
    return _root_of(ctx) / "config.json"


def _cfg(ctx: Any, section: str) -> Dict[str, Any]:
    """Read one section of config.json, tolerating a half-built Context."""
    data = getattr(getattr(ctx, "config", None), "data", None)
    if not isinstance(data, dict):
        return {}
    value = data.get(section)
    return value if isinstance(value, dict) else {}


def _photo_root(ctx: Any) -> Path:
    for name in ("photo_root", "photos_dir"):
        value = getattr(ctx, name, None)
        if value:
            return _abs(Path(str(value)), ctx)
    raw = str(_cfg(ctx, "paths").get("photo_root") or "photos").strip() or "photos"
    return _abs(Path(raw), ctx)


def _http_port(ctx: Any) -> int:
    try:
        port = int(_cfg(ctx, "server").get("http_port", _DEFAULT_PORT))
    except (TypeError, ValueError):
        return _DEFAULT_PORT
    return port if 1 <= port <= 65535 else _DEFAULT_PORT


def _client_name(ctx: Any) -> str:
    return str(_cfg(ctx, "server").get("client_name") or _DEFAULT_CLIENT_NAME)


def _tv_specs(ctx: Any) -> Dict[str, Dict[str, Any]]:
    tvs = _cfg(ctx, "tvs")
    return {alias: spec for alias, spec in tvs.items() if isinstance(spec, dict)}


def _enabled_aliases(ctx: Any) -> List[str]:
    """The implicit group "all": every enabled TV in alias order (contract 3.5)."""
    return sorted(a for a, spec in _tv_specs(ctx).items() if spec.get("enabled", True))


def _tv_options(ctx: Any, alias: str) -> Dict[str, Any]:
    opts = _tv_specs(ctx).get(alias, {}).get("options")
    return opts if isinstance(opts, dict) else {}


def _learned(ctx: Any, alias: str) -> Dict[str, Any]:
    """The learned-facts cache for one TV. Always optional (contract 4.4)."""
    state = getattr(ctx, "state", None)
    for name in ("learned_for", "learned", "facts"):
        attr = getattr(state, name, None)
        if callable(attr):
            try:
                value = _call_fitting(attr, alias)
            except Exception:  # a cache must never break a caller
                value = None
            if isinstance(value, dict):
                return value
        elif isinstance(attr, dict):
            value = attr.get(alias)
            if isinstance(value, dict):
                return value
    data = getattr(state, "data", None)
    if isinstance(data, dict):
        value = (data.get("learned") or {}).get(alias)
        if isinstance(value, dict):
            return value
    return {}


def _remember(ctx: Any, alias: str, facts: Dict[str, Any]) -> bool:
    """Cache learned facts through State, if it will take them.

    Returns False when no mutator could be found, which is not an error: the
    learned block is a cache and every consumer must work with it empty
    (contract 4.4). `learn` reports honestly instead of pretending it persisted.
    """
    state = getattr(ctx, "state", None)
    if state is None or not facts:
        return False
    for name in ("learn", "set_learned", "remember", "note_learned", "update_learned"):
        fn = getattr(state, name, None)
        if not callable(fn):
            continue
        try:
            fn(alias, dict(facts))
            return True
        except TypeError:
            pass
        try:
            for key, value in facts.items():
                fn(alias, key, value)
            return True
        except TypeError:
            continue
        except Exception:
            return False
    return False


def _is_paired(ctx: Any, alias: str) -> bool:
    """True only when a token exists AND was verified by effect (I1, 4.2)."""
    state = getattr(ctx, "state", None)
    fn = _pick(state, "is_paired")
    if fn is None:
        return False
    try:
        return bool(_call_fitting(fn, alias, _client_name(ctx)))
    except Exception:
        return False


# --------------------------------------------------------------------------
# network helpers
# --------------------------------------------------------------------------


def _store_helper(*names: str) -> Optional[Any]:
    """Borrow a helper from store when it exports one, else None.

    `local_ipv4_addresses` and `local_ip_toward` are named by the contract (3.1,
    10.5) but not assigned to a module. Preferring store's copy keeps the address
    this banner prints identical to the one the setup wizard offers; the local
    fallbacks below exist so `doctor` still works if store spells them
    differently.
    """
    try:
        store = importlib.import_module("tvhub.store")
    except Exception:
        return None
    return _pick(store, *names)


def _local_ipv4_addresses() -> List[str]:
    fn = _store_helper("local_ipv4_addresses", "local_ips")
    if fn is not None:
        try:
            found = [str(ip) for ip in (fn() or [])]
            if found:
                return found
        except Exception:
            pass
    found: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    routable = [ip for ip in found if not ip.startswith("127.")]
    return routable or found


def _local_ip_toward(target: Optional[str] = None) -> str:
    """The source address this host would use to reach `target`.

    A connected UDP socket sends nothing; it just makes the kernel pick a route,
    which is the only reliable way to name the right interface on a multi-homed
    server. No address is hard-coded anywhere: without a target we fall back to
    the host's own resolvable addresses.
    """
    fn = _store_helper("local_ip_toward")
    if fn is not None and target:
        try:
            value = fn(target)
            if value:
                return str(value)
        except Exception:
            pass
    if target:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 9))
            return str(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()
    addresses = _local_ipv4_addresses()
    return addresses[0] if addresses else "127.0.0.1"


def _report_bind_failure(bind: str, port: int, exc: OSError) -> None:
    """Explain a failure to listen, in the terms that lead to a fix.

    Diagnosed from the real error rather than by pre-testing the port with a
    throwaway bind. A pre-flight test cannot tell "someone else has it" from "our
    own App already bound it in its constructor", and it reports a port held only
    by lingering TIME_WAIT connections as taken when allow_reuse_address would
    have bound it anyway. Both false alarms stop a healthy service from starting.
    """
    log.error("cannot listen on %s:%s (%s)", bind, port, exc)
    code = getattr(exc, "errno", None)
    if code == errno.EADDRINUSE:
        sys.stderr.write(
            "ERROR port %d is already in use - another TVHub or a stray python is holding it.\n"
            % port
        )
        if os.name == "nt":
            for pid in _windows_port_squatters(port):
                sys.stderr.write("      pid %s is listening on it\n" % pid)
            sys.stderr.write("      'tvhub install' kills a stray holder for you.\n")
    elif code in (errno.EACCES, errno.EPERM):
        sys.stderr.write(
            "ERROR not allowed to listen on port %d - ports below 1024 need root,\n"
            "      or a local policy is blocking it.\n" % port
        )
    elif code == errno.EADDRNOTAVAIL:
        sys.stderr.write(
            "ERROR %s is not an address on this host - check server.bind.\n" % bind
        )
    elif code is None and str(exc).strip():
        # webapp raises a re-worded OSError with no errno and a better message
        # than anything we could synthesise; pass it through rather than wrap it.
        sys.stderr.write("ERROR %s\n" % str(exc).strip())
    else:
        sys.stderr.write("ERROR cannot listen on %s:%d (%s)\n" % (bind, port, exc))


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


def setup_logging(ctx: "Context", verbose: bool = False, to_console: bool = True) -> None:
    """Install the one logger: state/tvhub.log (2 MB x 3) plus stdout.

    INFO is the level at which the log answers "did that TV pick up the
    slideshow" (contract 0.9); per-request HTTP logging is DEBUG and only appears
    under -v. Repeated calls replace our own handlers rather than adding a second
    set, so a CLI command that builds a context and then runs the server does not
    print every line twice.
    """
    level = logging.DEBUG if verbose else logging.INFO
    log.setLevel(level)
    # Without this the root logger's handlers (anything a sibling or a test set
    # up) would emit a duplicate of every line.
    log.propagate = False

    # Take exclusive ownership of this logger: contract 0.9 specifies ONE file
    # handler plus stdout, and service.py is where that is decided. Clearing
    # everything (not just handlers we added) matters because a sibling that
    # configures logging when its Context is built - store.py does - leaves its
    # own file and stream handlers attached. Adding ours beside them wrote every
    # line twice to the console and twice into state/tvhub.log, which also halves
    # the rotation budget the contract sets aside. Measured, not theoretical.
    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    state_dir = _state_dir(ctx)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            str(state_dir / LOG_FILENAME),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError as exc:
        # A service that cannot write its log must still run and still control
        # TVs; say so on stderr and carry on with the console handler.
        sys.stderr.write("WARNING cannot write %s (%s) - logging to console only\n" % (state_dir / LOG_FILENAME, exc))
    else:
        handler.setFormatter(fmt)
        handler.setLevel(level)
        handler._tvhub = True  # type: ignore[attr-defined]
        log.addHandler(handler)

    if to_console:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        console.setLevel(level)
        console._tvhub = True  # type: ignore[attr-defined]
        log.addHandler(console)

    # websocket-client traces every frame at DEBUG. Useful when chasing a Samsung
    # handshake, unbearable otherwise.
    logging.getLogger("websocket").setLevel(logging.DEBUG if verbose else logging.WARNING)


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------


def check_dependencies() -> List[Tuple[str, bool, str]]:
    """Check the two allowed third-party packages (contract 0.2)."""
    rows: List[Tuple[str, bool, str]] = []

    try:
        import websocket  # type: ignore
    except Exception as exc:
        rows.append(("websocket-client", False, "missing (%s)" % exc.__class__.__name__))
    else:
        # There is an unrelated PyPI package also called "websocket" that imports
        # under the same name and has no create_connection. Installing it instead
        # of websocket-client fails much later, inside the control channel.
        if hasattr(websocket, "create_connection"):
            rows.append(("websocket-client", True, str(getattr(websocket, "__version__", "?"))))
        else:
            rows.append(
                (
                    "websocket-client",
                    False,
                    "the wrong 'websocket' package is installed - "
                    "pip uninstall websocket, then pip install websocket-client",
                )
            )

    try:
        import requests  # type: ignore
    except Exception as exc:
        rows.append(("requests", False, "missing (%s)" % exc.__class__.__name__))
    else:
        rows.append(("requests", True, str(getattr(requests, "__version__", "?"))))

    return rows


def _missing_dependency(exc: BaseException) -> Optional[str]:
    """Name the third-party package an ImportError was really about."""
    name = str(getattr(exc, "name", "") or "")
    text = str(exc).lower()
    for module_name, package in (("websocket", "websocket-client"), ("requests", "requests")):
        if name == module_name or ("'%s'" % module_name) in text or (module_name + " ") in text:
            return package
    return None


def _module(name: str) -> Any:
    """Import a sibling module, translating a missing dependency into advice."""
    try:
        return importlib.import_module("tvhub." + name)
    except ImportError as exc:
        package = _missing_dependency(exc)
        if package:
            raise _DependencyError(package) from exc
        raise


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------


def _make_context(store: Any, root: Path) -> Any:
    """Build the Context, whichever way store.py spells it.

    Tried in order: a factory on Context, a module-level factory, Context(root),
    and finally Context() - which still lands on the right root because
    _resolve_root exported $TVHUB_HOME first (contract 1).
    """
    attempts: List[Tuple[str, Any]] = []
    context_cls = getattr(store, "Context", None)
    if context_cls is not None:
        for factory in ("create", "open", "for_root", "build", "load"):
            fn = getattr(context_cls, factory, None)
            if callable(fn):
                attempts.append(("Context.%s" % factory, fn))
    for factory in ("build_context", "make_context", "create_context", "context"):
        fn = getattr(store, factory, None)
        if callable(fn):
            attempts.append((factory, fn))
    if context_cls is not None:
        attempts.append(("Context", context_cls))

    root_names = ("root", "path", "home", "base", "base_dir")
    errors: List[str] = []
    for label, fn in attempts:
        for offers in ([(root, root_names)], []):
            try:
                ctx = _call_with(fn, offers)
            except TypeError as exc:
                errors.append("%s: %s" % (label, exc))
                continue
            # Only accept something that really is a Context.
            if getattr(ctx, "config", None) is not None and getattr(ctx, "state", None) is not None:
                return ctx
            errors.append("%s: returned %r" % (label, type(ctx).__name__))
    raise RuntimeError(
        "tvhub.store exposes no usable Context (tried: %s)" % "; ".join(errors[:6] or ["nothing"])
    )


def _light_context(root: Optional[Any] = None) -> Any:
    """A Context built from store alone.

    store.py imports no third-party package (contract 0.3), so install,
    uninstall and a degraded `doctor` still work on a machine where
    requirements.txt has not been installed yet - which is exactly the machine
    someone is about to run `install` on.
    """
    root_path = _resolve_root(root)
    store = _module("store")
    ctx = _make_context(store, root_path)
    _ensure_dirs(ctx)
    return ctx


def _ensure_dirs(ctx: Any) -> None:
    """Create the on-disk layout from contract 1. Never clears anything."""
    for path in (_root_of(ctx), _state_dir(ctx), _state_dir(ctx) / "tmp", _photo_root(ctx)):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create %s (%s)", path, exc)


def build(root: Optional[Path] = None) -> Tuple["Context", "Fleet", "Slideshow", "UI", "App"]:
    """Wire the whole application up, in dependency order (contract 0.5).

    UI first because slideshow.py renders its page template; Fleet before
    Slideshow so a Slideshow that wants the fleet for identify numbering can have
    it; App last because it needs all four.
    """
    ctx = _light_context(root)

    ui_mod = _module("ui")
    fleet_mod = _module("fleet")
    slideshow_mod = _module("slideshow")
    webapp_mod = _module("webapp")

    ctx_names = ("ctx", "context")
    ui = _call_with(ui_mod.UI, [(ctx, ctx_names)])
    fleet = _call_with(fleet_mod.Fleet, [(ctx, ctx_names)])
    slideshow = _call_with(
        slideshow_mod.Slideshow,
        [(ctx, ctx_names), (ui, ("ui", "pages", "templates")), (fleet, ("fleet",))],
    )

    # Fleet is constructed first (Slideshow may want it for identify numbering),
    # so the Slideshow it needs cannot be a constructor argument - it has to be
    # injected here, once it exists. This is not optional wiring: Fleet resolves
    # "which playlist should this TV be showing" through Slideshow.resolve_for
    # (contract 8.3), and with it missing every lookup silently falls back to
    # config.slideshow.default_playlist. The visible symptom is that after
    # activating a playlist, `on` and a bare `show` restore the DEFAULT one
    # instead - i.e. choosing a playlist appears to work, the screens follow it,
    # and then the next power-on quietly puts them all back.
    for attr in ("slideshow", "library", "shows"):
        if hasattr(fleet, attr) and getattr(fleet, attr) is None:
            setattr(fleet, attr, slideshow)
            break

    app = _call_with(
        webapp_mod.App,
        [
            (ctx, ctx_names),
            (fleet, ("fleet",)),
            (slideshow, ("slideshow", "library", "shows")),
            (ui, ("ui", "pages")),
        ],
    )
    return ctx, fleet, slideshow, ui, app


def _version() -> str:
    try:
        package = importlib.import_module("tvhub")
    except Exception:
        return "?"
    return str(getattr(package, "__version__", "?"))


# --------------------------------------------------------------------------
# URLs and the banner
# --------------------------------------------------------------------------


def _base_url(ctx: Any, alias: Optional[str] = None) -> Tuple[str, bool]:
    """Return (base_url, configured).

    A per-TV override wins, for a multi-homed host (contract 3.4). When nothing
    is configured we work one out for the TV, which contract 3.1 allows for
    testing only: this string is typed by a human into every TV's browser
    homepage, so a guessed address that changes on the next DHCP lease would
    blank every screen.
    """
    server = _cfg(ctx, "server")
    per_tv = ""
    tv_ip = None
    if alias:
        options = _tv_options(ctx, alias)
        per_tv = str(options.get("base_url") or "").strip()
        tv_ip = str(_tv_specs(ctx).get(alias, {}).get("ip") or "") or None
    configured = (per_tv or str(server.get("base_url") or "")).strip().rstrip("/")
    if configured:
        return configured, True
    if tv_ip is None:
        first = _enabled_aliases(ctx)
        if first:
            tv_ip = str(_tv_specs(ctx).get(first[0], {}).get("ip") or "") or None
    return "http://%s:%d" % (_local_ip_toward(tv_ip), _http_port(ctx)), False


def homepage_url(ctx: "Context", alias: Optional[str] = None) -> str:
    """The ONE address a human types into a TV's browser as its homepage.

    Invariant I8: Samsung's local API cannot be made to navigate a TV to a URL -
    many firmwares acknowledge the launch and ignore the URL - so every TV points
    at this single address forever and switching playlists repoints what it
    serves. Which is why nothing here may ever include a playlist name.
    """
    base, _ = _base_url(ctx, alias)
    shared = bool(_cfg(ctx, "slideshow").get("shared_homepage", True))
    if shared or not alias:
        return base + "/slides"
    return base + "/slideshow/live/" + alias


def access_banner(ctx: "Context") -> str:
    """The addresses a human needs, printed by `run` (contract 12.6)."""
    base, configured = _base_url(ctx)
    lines = [
        "TVHub %s" % _version(),
        "",
        "  phone / controller   %s/" % base,
        "  dashboard            %s/ui/" % base,
        "",
        "  TV browser homepage  %s" % homepage_url(ctx),
        "    Set this once, by hand, with the remote, on every TV. It never",
        "    changes: switching playlists repoints what this address serves.",
    ]
    if not configured:
        lines += [
            "",
            "WARNING server.base_url is not set, so the address above was guessed",
            "        from this host's routing table. Because it gets typed into",
            "        every TV, set it to a reserved address first (dashboard ->",
            "        setup, step 1) or the screens will blank when the lease moves.",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# a job handle for the terminal
# --------------------------------------------------------------------------


class _TerminalHandle:
    """Stands in for store's JobHandle so CLI commands can drive Fleet code.

    Fleet.scan reports progress through a JobHandle (contract 7.18). On the
    terminal there is no job registry to report into, so this prints instead.
    Unknown members resolve to a no-op that is also falsy, because a diagnostic
    command must never die of a handle method it did not anticipate.
    """

    def __init__(self, stream: Any = None, quiet: bool = False) -> None:
        object.__setattr__(self, "stream", stream or sys.stdout)
        object.__setattr__(self, "quiet", quiet)
        object.__setattr__(self, "_last_tick", 0.0)
        object.__setattr__(self, "done", 0)
        object.__setattr__(self, "total", 0)
        object.__setattr__(self, "step", "")
        object.__setattr__(self, "lines", [])
        object.__setattr__(self, "result", None)
        object.__setattr__(self, "error", None)

    # -- the parts of the protocol we can name -------------------------------

    def line(self, text: Any = "") -> None:
        text = str(text)
        self.lines.append(text)
        self._emit("  " + text)

    log = line
    note = line
    say = line
    append = line
    add_line = line

    def progress(self, done: Any = None, total: Any = None, step: Any = None) -> None:
        if total is not None:
            object.__setattr__(self, "total", total)
        if done is not None:
            object.__setattr__(self, "done", done)
        if step:
            self.set_step(step)
        else:
            self._tick(force=done is not None and total is not None and done == total)

    def set_step(self, text: Any = "") -> None:
        self.step = str(text or "")

    def set_total(self, total: Any) -> None:
        self.progress(total=total)

    def advance(self, count: int = 1) -> None:
        self.progress(done=(self.done or 0) + count)

    # -- attribute plumbing --------------------------------------------------

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name == "step" and value:
            self._emit("  " + str(value))
        elif name in ("done", "total"):
            self._tick(force=(name == "done" and self.total and value == self.total))

    def __getattr__(self, name: str) -> Any:
        # Never answer for dunders: something copying or pickling this object
        # would otherwise get a no-op where it expected a real protocol.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _NOOP

    def _tick(self, force: bool = False) -> None:
        total = self.total or 0
        if not total:
            return
        now = time.monotonic()
        if not force and (now - self._last_tick) < 0.4:
            return
        object.__setattr__(self, "_last_tick", now)
        self._emit("  %s/%s" % (self.done, total), overwrite=True)

    def _emit(self, text: str, overwrite: bool = False) -> None:
        log.debug("job: %s", text.strip())
        if self.quiet:
            return
        try:
            if overwrite and self.stream.isatty():
                self.stream.write("\r" + text.ljust(40))
            else:
                self.stream.write(("\r" if overwrite else "") + text + "\n")
            self.stream.flush()
        except Exception:  # a closed or redirected stream must not stop a scan
            pass


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _clear_state_tmp(ctx: Any) -> None:
    """Empty state/tmp (contract 1) - upload staging, cleared at startup.

    Only on startup, never from a one-shot CLI command: deleting staging files
    out from under a running service would lose an in-flight upload.
    """
    tmp = _state_dir(ctx) / "tmp"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        for entry in tmp.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError:
                pass
    except OSError as exc:
        log.warning("cannot clear %s (%s)", tmp, exc)


def _discard_server(candidate: Any) -> None:
    """Release a candidate we are not going to use.

    Constructing a server binds the listening socket, so a candidate that turns
    out to have no serve_forever must be closed or it holds the port against the
    one we do use.
    """
    for name in ("shutdown", "server_close", "close"):
        closer = _pick(candidate, name)
        if closer is not None:
            try:
                closer()
            except Exception:  # pragma: no cover - best effort
                pass


def _obtain_server(ctx: Any, app: Any) -> Tuple[Any, Any, Any]:
    """Return (server_or_None, serve_callable, stop_callable) for the HTTP app.

    webapp.py owns the ThreadingHTTPServer (contract 9.1) but the contract does
    not pin how it hands it over. A server OBJECT is preferred over a blocking
    call because it can be shut down from a signal handler, which is how systemd
    stops us. Tried in order: a Server class, a module-level factory, one on the
    App, an already-built app.server, then a plain blocking serve call.
    """
    webapp = _module("webapp")
    offers = [(ctx, ("ctx", "context")), (app, ("app", "application", "handler"))]

    candidates: List[Tuple[str, Any]] = []
    for name in ("Server", "HttpServer", "HTTPServer", "WebServer", "Listener"):
        attr = getattr(webapp, name, None)
        if isinstance(attr, type):
            candidates.append(("webapp.%s" % name, attr))
    for name in ("make_server", "create_server", "build_server", "http_server", "server_for"):
        attr = getattr(webapp, name, None)
        if callable(attr):
            candidates.append(("webapp.%s" % name, attr))
    for name in ("make_server", "create_server", "build_server"):
        attr = _pick(app, name)
        if attr is not None:
            candidates.append(("App.%s" % name, attr))

    for label, factory in candidates:
        server = _call_with(factory, offers)
        if hasattr(server, "serve_forever"):
            log.debug("serving through %s", label)
            return server, server.serve_forever, _pick(server, "shutdown", "stop", "close")
        _discard_server(server)

    existing = getattr(app, "server", None)
    if hasattr(existing, "serve_forever"):
        return existing, existing.serve_forever, _pick(existing, "shutdown", "stop", "close")

    for owner, owner_name in ((app, "App"), (webapp, "webapp")):
        serve = _pick(owner, "serve_forever", "serve", "run_forever", "run")
        if serve is not None:
            log.debug("serving through %s.%s", owner_name, getattr(serve, "__name__", "serve"))
            stop = _pick(app, "shutdown", "stop", "close") or _pick(webapp, "shutdown", "stop")
            if owner is webapp:
                def bound(_serve: Any = serve) -> Any:
                    return _call_with(_serve, offers)
            else:
                def bound(_serve: Any = serve) -> Any:
                    return _call_fitting(_serve)
            return None, bound, stop

    raise RuntimeError(
        "tvhub.webapp exposes no way to serve - expected webapp.Server(ctx, app), "
        "webapp.make_server(ctx, app) or App.serve_forever()"
    )


def cmd_run(ctx: "Context", fleet: "Fleet", app: "App") -> int:
    """Run the service: background loops, then the HTTP server until stopped."""
    _clear_state_tmp(ctx)

    banner = access_banner(ctx)
    sys.stdout.write(banner + "\n\n")
    sys.stdout.flush()
    for line in banner.splitlines():
        if line.strip():
            log.info("%s", line.strip())

    port = _http_port(ctx)
    bind = str(_cfg(ctx, "server").get("bind") or "0.0.0.0")
    aliases = _enabled_aliases(ctx)
    log.info(
        "starting: root=%s bind=%s:%s tvs=%d (%s)",
        _root_of(ctx),
        bind,
        port,
        len(aliases),
        ", ".join(aliases) or "none configured",
    )

    # Bind before starting the background loops: a status sweep that begins and
    # is then abandoned because the port was taken wakes every TV for nothing.
    try:
        server, serve, stop = _obtain_server(ctx, app)
    except OSError as exc:
        _report_bind_failure(bind, port, exc)
        return 1

    started = _pick(fleet, "start_background")
    if started is not None:
        started()  # the status sweep and the periodic heal (contract 11.6)
    else:
        log.warning("fleet exposes no start_background() - no status sweep, no auto-heal")

    def _signal_stop(signum: int, _frame: Any) -> None:
        """Ask the listener to stop, from a thread that is not serving.

        systemd sends SIGTERM, and the handler runs on the MAIN thread - the one
        currently inside serve_forever(). ThreadingHTTPServer.shutdown() waits for
        that loop to finish before it returns, so calling it here directly
        deadlocks: the loop cannot exit until shutdown() returns and shutdown()
        cannot return until the loop exits. Measured: `systemctl stop` hung until
        its 90 s timeout escalated to SIGKILL. Hand it to a separate thread and
        return immediately so serve_forever() can unwind normally.
        """
        log.info("signal %s received - shutting down", signum)
        if stop is None:
            return
        threading.Thread(target=_safe_stop, name="tvhub-shutdown", daemon=True).start()

    def _safe_stop() -> None:
        try:
            stop()
        except Exception as exc:  # pragma: no cover - shutdown is best-effort
            log.debug("shutdown call failed: %s", exc)

    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signum = getattr(signal, signame, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _signal_stop)
        except (ValueError, OSError):  # not the main thread, or unsupported here
            pass

    code = 0
    try:
        serve()
    except KeyboardInterrupt:
        log.info("interrupted - shutting down")
    except OSError as exc:
        _report_bind_failure(bind, port, exc)
        code = 1
    finally:
        # Deliberately NOT calling stop() here: serve() has already returned, and
        # BaseServer.shutdown() waits on an event that only serve_forever() ever
        # sets - so calling it on a server whose loop never started blocks for
        # good. Closing the socket is the part that actually needs doing.
        for name in ("server_close", "close"):
            closer = _pick(server, name) or _pick(app, name)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
                break
        stopper = _pick(fleet, "stop_background", "stop", "shutdown")
        if stopper is not None:
            try:
                stopper()
            except Exception as exc:
                log.debug("stopping background loops failed: %s", exc)
        log.info("stopped")
    return code


# --------------------------------------------------------------------------
# the one-shot controller path
# --------------------------------------------------------------------------


def _playlists(ctx: Any, slideshow: Any) -> List[Tuple[str, int]]:
    """(name, image count) for every playlist, in name order."""
    lister = _pick(slideshow, "playlists", "list_playlists", "library")
    if lister is not None:
        try:
            rows = lister() or []
        except Exception as exc:
            log.debug("playlist listing failed: %s", exc)
            rows = []
        out: List[Tuple[str, int]] = []
        if isinstance(rows, dict):
            rows = rows.get("playlists") or []
        for row in rows:
            if isinstance(row, str):
                out.append((row, _playlist_count(ctx, slideshow, row)))
            else:
                name = _get(row, "name", "")
                if name:
                    count = _get(row, "count", None)
                    out.append(
                        (str(name), int(count) if isinstance(count, int) else _playlist_count(ctx, slideshow, str(name)))
                    )
        if out:
            return sorted(out, key=lambda item: item[0])

    root = _photo_root(ctx)
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        names = []
    return [(name, _playlist_count(ctx, slideshow, name)) for name in names]


def _playlist_count(ctx: Any, slideshow: Any, name: str) -> int:
    for method in ("images", "list_images", "playlist_images", "image_names"):
        fn = _pick(slideshow, method)
        if fn is None:
            continue
        try:
            return len(list(_call_fitting(fn, name) or []))
        except Exception:
            continue
    folder: Optional[Path] = None
    resolver = _pick(slideshow, "playlist_dir")
    if resolver is not None:
        try:
            value = _call_fitting(resolver, name)
            folder = Path(str(value)) if value else None
        except Exception:
            folder = None
    if folder is None:
        folder = _photo_root(ctx) / name
    try:
        return sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXTS)
    except OSError:
        return 0


def _act(fleet: Any, aliases: Sequence[str], verb: str, arg: Optional[str]) -> List[Tuple[str, str]]:
    """Run one verb against one or more TVs and render each answer.

    Fleet.act is the entry point the contract names for a single TV (2.3), so it
    is the one trusted here. Fleet.run is used for a fan-out only when its
    signature really does take (aliases, verb, arg) - it owns the bounded pool,
    the Activity clearing and the exception wording from 7.12, so it is worth
    preferring; otherwise we fan out ourselves with the same bound of 8 (11.5).
    """
    aliases = list(aliases)
    if not aliases:
        return []

    act = _pick(fleet, "act")

    if len(aliases) > 1:
        run = _pick(fleet, "run")
        if run is not None:
            try:
                sig = inspect.signature(run)
                slots = sum(
                    1
                    for p in sig.parameters.values()
                    if p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                )
            except (TypeError, ValueError):
                slots = 0
            if slots >= 3:
                results = run(aliases, verb, arg)
                rendered = _normalise_results(aliases, results)
                if rendered:
                    return rendered

    if act is None:
        return [(alias, "ERROR fleet exposes no act() - cannot run '%s'" % verb) for alias in aliases]

    if len(aliases) == 1:
        alias = aliases[0]
        return [(alias, _render(_call_fitting(act, alias, verb, arg)))]

    out: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(aliases))) as pool:
        futures = {pool.submit(_call_fitting, act, alias, verb, arg): alias for alias in aliases}
        for future in futures:
            alias = futures[future]
            try:
                out[alias] = _render(future.result())
            except Exception as exc:
                out[alias] = "ERROR %s: %s" % (exc.__class__.__name__, exc)
    return [(alias, out.get(alias, "ERROR no answer")) for alias in aliases]


def _normalise_results(aliases: Sequence[str], results: Any) -> List[Tuple[str, str]]:
    """Turn whatever Fleet.run returned into (alias, rendered) in alias order."""
    if results is None:
        return []
    if isinstance(results, dict):
        return [(a, _render(results.get(a))) for a in aliases if a in results] or []
    if isinstance(results, (list, tuple)):
        if len(results) == len(aliases):
            first = results[0] if results else None
            if isinstance(first, (list, tuple)) and len(first) == 2:
                return [(str(a), _render(r)) for a, r in results]
            return [(a, _render(r)) for a, r in zip(aliases, results)]
        if results and isinstance(results[0], (list, tuple)) and len(results[0]) == 2:
            return [(str(a), _render(r)) for a, r in results]
    return []


def _controller(ctx: Any, fleet: Any, slideshow: Any, path: str) -> Tuple[str, int]:
    """Run a controller path from the terminal and return (text, exit code).

    The same paths a controller would GET (contract 9.5), rendered the same way
    (0.8), so a one-liner in the shell and a Loxone block see identical text.
    """
    segments = [s for s in str(path).split("/") if s]
    if not segments:
        return ("ERROR no command\n", 2)

    head = segments[0].lower()

    if head == "health":
        return ("ok\n", 0)

    if head == "reload":
        reload_fn = _pick(getattr(ctx, "config", None), "reload")
        if reload_fn is None:
            return ("ERROR config exposes no reload()\n", 1)
        answer = reload_fn()
        if isinstance(answer, tuple) and len(answer) == 2:
            ok, message = answer
            return (("%s\n" % message) if ok else ("ERROR %s\n" % message), 0 if ok else 1)
        line = _render(answer)
        return (line + "\n", 1 if _failed(line) else 0)

    if head == "playlists":
        rows = _playlists(ctx, slideshow)
        if not rows:
            return ("no playlists yet - add photos first\n", 1)
        return ("".join("%s: %d image(s)\n" % (name, count) for name, count in rows), 0)

    if head == "playlist":
        if len(segments) < 2:
            rows = _playlists(ctx, slideshow)
            return ("".join("%s: %d image(s)\n" % (n, c) for n, c in rows) or "no playlists yet\n", 0)
        name = segments[1]
        activate = _pick(slideshow, "activate")
        if activate is None:
            return ("ERROR slideshow exposes no activate()\n", 1)
        line = _render(_call_fitting(activate, name))
        # A fleet-wide switch is a pointer move only: no device I/O, and the
        # screens follow within about five seconds from their own polling.
        return (line + "\n", 1 if _failed(line) else 0)

    if head == "homepages":
        return (_homepages_text(ctx), 0)

    if head == "identify":
        if len(segments) < 2 or segments[1].lower() not in ("on", "off"):
            return ("ERROR identify needs on or off\n", 2)
        on = segments[1].lower() == "on"
        fn = _pick(fleet, "set_identify", "identify", "set_identify_mode", "identify_mode")
        if fn is None:
            return ("ERROR identify is not available\n", 1)
        answer = _call_fitting(fn, on)
        if answer is None:
            text = (
                "identify on - every TV shows its number and alias"
                if on
                else "identify off"
            )
        else:
            text = _render(answer)
        return (text + "\n", 1 if _failed(text) else 0)

    # ---- targeted verbs ------------------------------------------------
    grouped = False
    if head == "tv" and len(segments) >= 3:
        alias = segments[1]
        if alias not in _tv_specs(ctx):
            return ("ERROR unknown TV '%s'\n" % alias, 2)
        aliases = [alias]
        rest = segments[2:]
    elif head == "group" and len(segments) >= 3:
        name = segments[1]
        aliases = _resolve_group(ctx, fleet, name)
        if aliases is None:
            return ("ERROR unknown group '%s'\n" % name, 2)
        rest = segments[2:]
        grouped = True
    elif head == "all" and len(segments) >= 2:
        aliases = _enabled_aliases(ctx)
        rest = segments[1:]
        grouped = True
    else:
        target = segments[0]
        rest = segments[1:]
        known_tv = target in _tv_specs(ctx)
        known_group = target in _cfg(ctx, "groups") or target == "all"
        if known_tv or known_group:
            # A TV alias always beats a group of the same name (I12) - that
            # ordering lives in Fleet.resolve, so use it rather than repeat it.
            resolver = _pick(fleet, "resolve")
            if resolver is not None:
                aliases = [str(a) for a in (_call_fitting(resolver, target) or [])]
            else:
                aliases = [target] if known_tv else _resolve_group(ctx, fleet, target) or []
            grouped = not known_tv
        elif not rest and target.lower() in VERBS:
            # A bare verb means the whole fleet: `tvhub status`, `tvhub off`.
            aliases = _enabled_aliases(ctx)
            rest = [target]
            grouped = True
        else:
            # Deliberately NOT falling through to Fleet.resolve's "everything"
            # default: a mistyped alias must not power off the whole building.
            return ("ERROR unknown target '%s'\n" % target, 2)

    if not rest:
        return ("ERROR no verb - try one of: %s\n" % ", ".join(VERBS), 2)

    verb = rest[0].lower()
    arg = "/".join(rest[1:]) if len(rest) > 1 else None
    if verb not in VERBS:
        return ("ERROR unknown verb '%s' - try one of: %s\n" % (verb, ", ".join(VERBS)), 2)
    if arg is None and verb in _ARG_REQUIRED:
        return ("ERROR %s needs an argument, e.g. %s/<value>\n" % (verb, verb), 2)
    if arg is not None and verb not in _ARG_VERBS:
        return ("ERROR %s takes no argument\n" % verb, 2)
    if not aliases:
        return ("ERROR no TVs match\n", 1)

    rows = _act(fleet, aliases, verb, arg)
    if grouped or len(rows) > 1:
        text = "".join("[%s] %s\n" % (alias, line) for alias, line in rows)
    else:
        text = "".join(line + "\n" for _alias, line in rows)
    code = 1 if any(_failed(line) for _a, line in rows) else 0
    return (text, code)


def _resolve_group(ctx: Any, fleet: Any, name: str) -> Optional[List[str]]:
    if name == "all":
        return _enabled_aliases(ctx)
    groups = _cfg(ctx, "groups")
    if name not in groups:
        return None
    resolver = _pick(fleet, "resolve")
    if resolver is not None:
        try:
            resolved = [str(a) for a in (_call_fitting(resolver, name) or [])]
            if resolved:
                return resolved
        except Exception:
            pass
    members = groups.get(name) or []
    enabled = set(_enabled_aliases(ctx))
    return sorted(a for a in members if a in enabled)


def _homepages_text(ctx: Any) -> str:
    base, configured = _base_url(ctx)
    lines = [
        "Set this as the browser homepage on every TV, by hand, with the remote:",
        "",
        "    " + homepage_url(ctx),
        "",
        "This cannot be automated. Many Samsung firmwares accept a 'launch the",
        "browser at this URL' command and then ignore the URL, so each TV keeps",
        "one homepage forever and switching playlists repoints what it serves.",
    ]
    if not bool(_cfg(ctx, "slideshow").get("shared_homepage", True)):
        lines += ["", "Per-TV homepages (shared_homepage is off):"]
        for alias in _enabled_aliases(ctx):
            lines.append("    %-16s %s" % (alias, homepage_url(ctx, alias)))
    if not configured:
        lines += [
            "",
            "WARNING server.base_url is not set - the address above is a guess and",
            "        will change with this host's IP. Set it before typing it in.",
        ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def _kv(key: str, value: Any) -> str:
    return "  %-16s %s" % (key, value)


def _yes(value: Any) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _doctor_row(ctx: Any, fleet: Any, slideshow: Any, samsung: Any, alias: str) -> Dict[str, Any]:
    spec = _tv_specs(ctx).get(alias, {})
    ip = str(spec.get("ip") or "")
    row: Dict[str, Any] = {
        "alias": alias,
        "ip": ip,
        "label": str(spec.get("label") or alias),
        "enabled": bool(spec.get("enabled", True)),
        "mac": str(spec.get("mac") or ""),
    }

    status: Any = {}
    fn = _pick(fleet, "status")
    if fn is not None:
        try:
            status = _call_fitting(fn, alias) or {}
        except Exception as exc:
            row["probe_error"] = "%s: %s" % (exc.__class__.__name__, exc)

    for key in ("power", "model", "frame", "paired", "verified_how", "browser", "state", "detail"):
        row[key] = _get(status, key, None)
    row["heartbeat_age"] = _get(status, "heartbeat_age", None)

    # smartHubAgreement is not part of the status row but decides whether ANY app
    # can be launched at all (I20), so ask REST for it directly.
    info: Any = {}
    if samsung is not None and ip:
        try:
            info = samsung.device_info(ip) or {}
        except Exception as exc:
            log.debug("device_info(%s) failed: %s", ip, exc)
    row["smart_hub"] = _get(info, "smart_hub", None)
    if row.get("power") is None:
        row["power"] = _get(info, "power", "unreachable")
    if not row.get("model"):
        row["model"] = _get(info, "model", "")
    if row.get("frame") is None:
        row["frame"] = _get(info, "is_frame", None)
    row["device_mac"] = str(_get(info, "mac", "") or "")

    if row.get("paired") is None:
        row["paired"] = _is_paired(ctx, alias)

    learned = _learned(ctx, alias)
    options = _tv_options(ctx, alias)
    row["browser_app_id"] = str(
        options.get("browser_app_id") or learned.get("browser_app_id") or ""
    )
    row["art_hung"] = bool(learned.get("art_hung", False))

    playlist = _get(status, "playlist", None)
    if not playlist:
        resolver = _pick(slideshow, "resolve_for")
        if resolver is not None:
            try:
                playlist = _call_fitting(resolver, alias)
            except Exception:
                playlist = None
    row["playlist"] = str(playlist or "")
    row["images"] = _playlist_count(ctx, slideshow, row["playlist"]) if row["playlist"] else 0
    row["homepage"] = homepage_url(ctx, alias)
    return row


def cmd_doctor(ctx: "Context", fleet: "Fleet", slideshow: "Slideshow") -> int:
    """Print everything needed to explain a blank TV (contract 12.5).

    Exits non-zero when any TV is unreachable or unpaired, or when base_url is
    empty while TVs exist. `fleet` and `slideshow` may be None: doctor is the one
    command that must still say something useful on a machine where
    requirements.txt has not been installed.
    """
    out: List[str] = []
    base, base_set = _base_url(ctx)
    server = _cfg(ctx, "server")
    specs = _tv_specs(ctx)
    aliases = sorted(specs)

    out.append("TVHub %s - doctor" % _version())
    out.append("")
    out.append(_kv("root", _root_of(ctx)))
    out.append(_kv("config", _config_path(ctx)))
    out.append(_kv("state", _state_dir(ctx)))
    out.append(_kv("state file", _state_dir(ctx) / "state.json"))
    out.append(_kv("log", _state_dir(ctx) / LOG_FILENAME))
    out.append(_kv("photos", _photo_root(ctx)))
    out.append("")
    out.append(_kv("base_url", "%s   (%s)" % (base, "set" if base_set else "NOT SET - guessed")))
    out.append(_kv("bind", "%s:%d" % (server.get("bind") or "0.0.0.0", _http_port(ctx))))
    out.append(_kv("client name", _client_name(ctx)))
    out.append(_kv("local IPv4", ", ".join(_local_ipv4_addresses()) or "none found"))
    out.append(_kv("platform", "%s %s / python %s" % (platform.system(), platform.release(), platform.python_version())))
    out.append(_kv("elevated", _yes(is_elevated())))

    missing_deps: List[str] = []
    for name, ok, detail in check_dependencies():
        out.append(_kv("dependency", "%-18s %s %s" % (name, "ok" if ok else "MISSING", detail)))
        if not ok:
            missing_deps.append(name)

    warnings = getattr(getattr(ctx, "config", None), "warnings", None)
    state_warnings = getattr(getattr(ctx, "state", None), "warnings", None)
    for group in (warnings, state_warnings):
        if isinstance(group, (list, tuple)):
            for warning in group:
                out.append(_kv("config warning", warning))

    out.append("")
    if not aliases:
        out.append("  no TVs configured yet - open %s/ui/setup" % base)
    out.append("")

    # A missing dependency is a fault in its own right: nothing can talk to a TV
    # without it, and doctor answering "all good" while an ERROR is on the screen
    # above would be worse than useless.
    bad = len(missing_deps)
    if missing_deps:
        out.append("  -> not installed: %s. Nothing can reach a TV until they are:" % ", ".join(missing_deps))
        out.append("     pip install -r %s" % (_root_of(ctx) / "requirements.txt"))
        out.append("")

    if aliases and fleet is None:
        out.append("  TVs not probed: a required package is missing (see above).")
        out.append("  Install requirements.txt, then run doctor again.")
        bad += 1
    elif aliases:
        rows: Dict[str, Dict[str, Any]] = {}
        samsung: Any = None
        try:
            samsung = _module("samsung")
        except Exception as exc:
            log.debug("samsung unavailable in doctor: %s", exc)
        # Same bound the status sweep uses (contract 11.5) so a fourteen-TV
        # fleet answers in one REST timeout rather than fourteen.
        with ThreadPoolExecutor(max_workers=min(16, len(aliases))) as pool:
            futures = {
                pool.submit(_doctor_row, ctx, fleet, slideshow, samsung, alias): alias
                for alias in aliases
            }
            for future in futures:
                alias = futures[future]
                try:
                    rows[alias] = future.result()
                except Exception as exc:
                    rows[alias] = {
                        "alias": alias,
                        "ip": str(specs.get(alias, {}).get("ip") or ""),
                        "probe_error": "%s: %s" % (exc.__class__.__name__, exc),
                    }

        for alias in aliases:
            row = rows.get(alias, {})
            power = str(row.get("power") or "unknown")
            paired = bool(row.get("paired"))
            out.append(
                "  %s   %s%s"
                % (alias, row.get("ip") or "no ip", "" if row.get("enabled", True) else "   (disabled)")
            )
            if row.get("probe_error"):
                out.append("      probe failed: %s" % row["probe_error"])
            out.append(
                "      power %-12s model %-18s frame %s"
                % (power, (row.get("model") or "?")[:18], _yes(row.get("frame")))
            )
            out.append(
                "      paired %-11s %-24s smart hub %s"
                % (
                    _yes(paired),
                    "verified by %s" % (row.get("verified_how") or "nothing yet"),
                    _yes(row.get("smart_hub")),
                )
            )
            age = row.get("heartbeat_age")
            age_text = "never" if age is None else "%.0fs ago" % float(age)
            out.append(
                "      browser %-10s heartbeat %-14s state %s"
                % (row.get("browser") or "unknown", age_text, row.get("state") or "unknown")
            )
            out.append(
                "      browser app %-18s playlist %s (%d image(s))"
                % (row.get("browser_app_id") or "not learned", row.get("playlist") or "none", row.get("images") or 0)
            )
            out.append("      homepage %s" % (row.get("homepage") or ""))

            if row.get("playlist") and not row.get("images"):
                # A TV faithfully showing an empty playlist is indistinguishable
                # from a broken one, and this is the cheapest cause to rule out.
                out.append(
                    "      -> playlist '%s' has no displayable images, so this TV has "
                    "nothing to show." % row["playlist"]
                )
            if power == "unreachable":
                bad += 1
                out.append("      -> unreachable. It may be in deep standby, on another subnet, or moved by DHCP.")
            if not paired:
                bad += 1
                out.append("      -> not paired, or paired under a different client name. Pair it and press ALLOW on the TV.")
            if row.get("smart_hub") is False:
                out.append(
                    "      -> Smart Hub is not signed in: until it is, this TV reports no apps "
                    "at all and nothing can be launched."
                )
            if row.get("art_hung"):
                out.append(
                    "      -> its art channel hung once and is blacklisted; clear it with a verify."
                )
            if row.get("mac") and row.get("device_mac") and row["mac"].lower() != row["device_mac"].lower():
                out.append(
                    "      -> configured MAC differs from the one the TV reports (%s); "
                    "Wake-on-LAN will not wake it." % row["device_mac"]
                )
            if not row.get("mac"):
                out.append("      -> no MAC configured, so Wake-on-LAN cannot be tried at all.")
            out.append("")

    if aliases and not base_set:
        bad += 1
        out.append("  server.base_url is empty while TVs are configured. It is embedded in")
        out.append("  every TV's browser homepage, so set it to this host's reserved address.")
        out.append("")
    elif base_set:
        # base_url is what a human typed into every TV; http_port is what we
        # actually listen on. If they disagree, every TV asks for a port nothing
        # answers on and every screen stays blank - with no error anywhere.
        stated = base.rsplit(":", 1)[-1]
        if stated.isdigit() and int(stated) != _http_port(ctx):
            bad += 1
            out.append(
                "  server.base_url names port %s but the server listens on %d. Every TV"
                % (stated, _http_port(ctx))
            )
            out.append("  would ask for a port nothing answers on. Make the two agree.")
            out.append("")

    out.append("  TV homepage to set on every TV:")
    out.append("      " + homepage_url(ctx))
    out.append("")
    out.append("  %s" % ("something needs attention (see -> lines above)" if bad else "all good"))

    sys.stdout.write("\n".join(out) + "\n")
    return 1 if bad else 0


# --------------------------------------------------------------------------
# pair, scan, learn
# --------------------------------------------------------------------------


def cmd_pair(ctx: "Context", fleet: "Fleet", targets: List[str]) -> int:
    """Pair TVs from the terminal, one at a time.

    Never in parallel (contract 10.5 step 3): the ALLOW prompt is modal on the TV
    and a human can only walk to one screen at a time. Pairing is also
    subnet-sensitive - a TV on another subnet refuses instantly - so this host
    has to be on the TVs' own subnet.
    """
    targets = [t for t in (targets or [])]
    wait = _DEFAULT_PAIR_WAIT
    if targets and targets[-1].isdigit():
        wait = max(5, int(targets[-1]))
        targets = targets[:-1]

    specs = _tv_specs(ctx)
    if not specs:
        sys.stdout.write("ERROR no TVs configured - add them from the setup wizard first\n")
        return 2

    aliases: List[str] = []
    if targets:
        for target in targets:
            if target in specs:
                chosen = [target]
            else:
                chosen = _resolve_group(ctx, fleet, target) or []
                if not chosen:
                    sys.stdout.write("ERROR unknown TV or group '%s'\n" % target)
                    return 2
            for alias in chosen:
                if alias not in aliases:
                    aliases.append(alias)
    else:
        aliases = [a for a in _enabled_aliases(ctx) if not _is_paired(ctx, a)]
        if not aliases:
            sys.stdout.write("every enabled TV is already paired and verified\n")
            return 0

    pair = _pick(fleet, "pair")
    if pair is None:
        sys.stdout.write("ERROR fleet exposes no pair()\n")
        return 1

    sys.stdout.write(
        "Pairing %d TV(s), one at a time. For each one: make sure it is ON, then\n"
        "choose ALLOW on the TV screen within %d seconds. This host must be on the\n"
        "same subnet as the TV.\n\n" % (len(aliases), wait)
    )
    failed = 0
    for index, alias in enumerate(aliases, 1):
        ip = specs.get(alias, {}).get("ip") or "?"
        sys.stdout.write("[%d/%d] %s (%s) - watch that screen for the ALLOW box\n" % (index, len(aliases), alias, ip))
        sys.stdout.flush()
        try:
            result = _call_with(pair, [(alias, ("alias", "name", "tv")), (wait, ("wait", "wait_seconds", "seconds", "timeout"))])
        except Exception as exc:
            line = "ERROR %s: %s" % (exc.__class__.__name__, exc)
        else:
            line = _render(result)
        sys.stdout.write("        %s\n\n" % line)
        sys.stdout.flush()
        if _failed(line):
            failed += 1
    sys.stdout.write("%d paired, %d failed\n" % (len(aliases) - failed, failed))
    return 1 if failed else 0


def cmd_scan(ctx: "Context", fleet: "Fleet", cidr: Optional[str]) -> int:
    """Discover Samsung sets on the network and print what answered."""
    scan = _pick(fleet, "scan")
    if scan is None:
        sys.stdout.write("ERROR fleet exposes no scan()\n")
        return 1

    handle = _TerminalHandle()
    sys.stdout.write("scanning %s ...\n" % (cidr or "this host's /24"))
    sys.stdout.flush()
    try:
        answer = _call_with(
            scan,
            [
                (cidr, ("cidr", "network", "subnet", "range")),
                (handle, ("handle", "job", "jh", "progress", "on_progress")),
            ],
        )
    except Exception as exc:
        sys.stdout.write("ERROR scan failed: %s: %s\n" % (exc.__class__.__name__, exc))
        return 1

    rows: Any = answer
    if isinstance(answer, dict):
        rows = answer.get("rows") or answer.get("found") or answer.get("tvs") or []
    if not isinstance(rows, (list, tuple)):
        rows = getattr(answer, "rows", None) or getattr(handle, "result", None) or []
    if not isinstance(rows, (list, tuple)):
        rows = []

    known = _tv_specs(ctx)
    known_ips = {str(spec.get("ip") or ""): alias for alias, spec in known.items()}

    sys.stdout.write("\n")
    if not rows:
        sys.stdout.write(
            "no Samsung sets answered.\n"
            "A set in deep standby answers nothing at all, and a scan does not cross\n"
            "subnets - run this from a host on the TVs' own subnet, or add the TV by IP.\n"
        )
        return 1

    header = "%-16s %-18s %-20s %-18s %-9s %-6s %-9s %s" % (
        "ip", "model", "name", "mac", "power", "frame", "smart hub", "alias",
    )
    sys.stdout.write(header + "\n" + "-" * len(header) + "\n")
    hub_warnings = 0
    for row in rows:
        ip = str(_get(row, "ip", "") or "")
        alias = str(_get(row, "alias", "") or "") or known_ips.get(ip, "")
        smart_hub = _get(row, "smart_hub", None)
        if smart_hub is False:
            hub_warnings += 1
        sys.stdout.write(
            "%-16s %-18s %-20s %-18s %-9s %-6s %-9s %s\n"
            % (
                ip,
                str(_get(row, "model", "") or "")[:18],
                str(_get(row, "name", "") or "")[:20],
                str(_get(row, "mac", "") or ""),
                str(_get(row, "power", "") or ""),
                _yes(_get(row, "frame", None)),
                _yes(smart_hub),
                alias or "-",
            )
        )

    sys.stdout.write("\n%d set(s) answered.\n" % len(rows))
    if hub_warnings:
        sys.stdout.write(
            "%d of them have Smart Hub not signed in: until someone signs in on the TV,\n"
            "it reports no apps at all and nothing can be launched.\n" % hub_warnings
        )
    base, _configured = _base_url(ctx)
    sys.stdout.write("Add the ones you want at %s/ui/setup (a scan does not add anything).\n" % base)
    return 0


def cmd_learn(ctx: "Context", fleet: "Fleet", alias: str) -> int:
    """Probe one TV and cache what it can actually do.

    Everything positive found here is a cache, never a requirement (contract
    4.4), and nothing is cached while the TV is unpaired: before pairing, the
    app endpoints answer 401/404 for every id, and storing that as "no browser"
    would permanently mislabel a perfectly good set (I10).
    """
    specs = _tv_specs(ctx)
    if alias not in specs:
        sys.stdout.write(
            "ERROR unknown TV '%s' - configured: %s\n" % (alias, ", ".join(sorted(specs)) or "none")
        )
        return 2

    try:
        samsung = _module("samsung")
    except _DependencyError as exc:
        sys.stdout.write("ERROR %s is missing - pip install -r requirements.txt\n" % exc)
        return 1

    ip = str(specs[alias].get("ip") or "")
    options = _tv_options(ctx, alias)
    client_name = _client_name(ctx)
    facts: Dict[str, Any] = {}

    sys.stdout.write("learning %s (%s)\n\n" % (alias, ip))

    info: Any = {}
    try:
        info = samsung.device_info(ip) or {}
    except Exception as exc:
        sys.stdout.write("  device info failed: %s: %s\n" % (exc.__class__.__name__, exc))
    power = str(_get(info, "power", "unreachable") or "unreachable")
    is_frame = _get(info, "is_frame", None)
    smart_hub = _get(info, "smart_hub", None)
    device_mac = str(_get(info, "mac", "") or "")

    sys.stdout.write(_kv("power", power) + "\n")
    sys.stdout.write(_kv("model", _get(info, "model", "") or "unknown") + "\n")
    sys.stdout.write(_kv("name", _get(info, "name", "") or "unknown") + "\n")
    sys.stdout.write(_kv("network", _get(info, "network", "") or "unknown") + "\n")
    sys.stdout.write(_kv("frame", _yes(is_frame)) + "\n")
    sys.stdout.write(_kv("smart hub", _yes(smart_hub)) + "\n")
    sys.stdout.write(_kv("mac (reported)", device_mac or "none") + "\n")

    if power == "unreachable":
        sys.stdout.write(
            "\n  nothing else can be learned while the TV does not answer on 8001.\n"
            "  It may be in deep standby, on another subnet, or its lease moved.\n"
        )
        return 1

    if is_frame is not None:
        facts["is_frame"] = bool(is_frame)
    # Prefer the fleet's own probe: it caches on a reachable answer and only then
    # (contract 7.2), which is the behaviour the rest of the product relies on.
    tv_getter = _pick(fleet, "tv", "get_tv")
    if tv_getter is not None:
        try:
            tv = _call_fitting(tv_getter, alias)
            frame_fn = _pick(tv, "is_frame")
            if frame_fn is not None:
                facts["is_frame"] = bool(frame_fn())
        except Exception as exc:
            log.debug("fleet frame probe failed for %s: %s", alias, exc)

    dial = _pick(samsung, "dial_browser_state", "dial_state", "browser_state", "dial")
    if dial is not None:
        try:
            state = _call_fitting(dial, ip)
            sys.stdout.write(_kv("browser (DIAL)", state) + "\n")
        except Exception as exc:
            sys.stdout.write(_kv("browser (DIAL)", "failed: %s" % exc) + "\n")

    upnp = _pick(samsung, "upnp_available")
    upnp_ok = None
    if upnp is not None:
        try:
            upnp_ok = bool(_call_fitting(upnp, ip))
        except Exception:
            upnp_ok = None
    sys.stdout.write(_kv("upnp 9197", _yes(upnp_ok)) + "\n")

    paired = _is_paired(ctx, alias)
    sys.stdout.write(_kv("paired", _yes(paired)) + "\n")

    if paired:
        probe = _pick(samsung, "probe_browser_app_id")
        if probe is not None:
            try:
                app_id = _call_with(
                    probe,
                    [
                        (ip, ("ip", "host", "address")),
                        (options.get("browser_app_id") or None, ("extra", "candidate", "first")),
                    ],
                )
            except Exception as exc:
                app_id = None
                log.debug("browser probe failed for %s: %s", alias, exc)
            if app_id:
                facts["browser_app_id"] = str(app_id)
                sys.stdout.write(_kv("browser app id", app_id) + "\n")
            else:
                sys.stdout.write(
                    _kv("browser app id", "not reported")
                    + "\n      This firmware answers 404 for every app id, sometimes even for\n"
                    "      apps it will happily launch, so this is a hint and not a verdict.\n"
                )
    else:
        sys.stdout.write(
            "\n  not probing for the browser app while unpaired: every app endpoint\n"
            "  answers 401/404 before pairing, and caching that as 'no browser' would\n"
            "  mislabel this TV permanently. Pair it first, then learn again.\n"
        )

    stored = _remember(ctx, alias, facts) if facts else False
    sys.stdout.write("\n")
    if facts:
        sys.stdout.write(
            _kv("cached", ", ".join("%s=%s" % (k, v) for k, v in sorted(facts.items())) if stored else "nothing (state has no learn method)")
            + "\n"
        )
    if smart_hub is False:
        sys.stdout.write(
            "\n  Smart Hub is not signed in on this TV. Until someone signs in on the\n"
            "  screen it reports no apps at all and nothing can be launched.\n"
        )
    sys.stdout.write(
        "\n  However this TV reports itself, the picture only ever reaches it one way:\n"
        "  its browser homepage is set once, by hand, to\n"
        "      %s\n"
        "  and switching playlists repoints what that address serves. If the browser\n"
        "  cannot be opened over the network at all, record an open macro on\n"
        "  %s/ui/tv/%s.\n" % (homepage_url(ctx, alias), _base_url(ctx)[0], alias)
    )
    return 0


# --------------------------------------------------------------------------
# install / uninstall
# --------------------------------------------------------------------------


def is_elevated() -> bool:
    """True when this process can install a service."""
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - no geteuid anywhere else
        return False


def service_python() -> str:
    """The interpreter to register.

    pythonw.exe when it sits beside this interpreter: the service must not pop a
    console window on a machine someone also uses as a workstation.
    """
    executable = Path(sys.executable)
    if os.name == "nt":
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(executable)


def _run(cmd: Sequence[str], timeout: int = 180) -> Tuple[int, str]:
    """Run an OS command and return (returncode, combined output)."""
    log.debug("run: %s", " ".join(str(c) for c in cmd))
    try:
        completed = subprocess.run(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            universal_newlines=True,
        )
    except FileNotFoundError:
        return (127, "%s not found on PATH" % cmd[0])
    except subprocess.TimeoutExpired:
        return (124, "%s timed out after %ds" % (cmd[0], timeout))
    except OSError as exc:
        return (1, "%s failed: %s" % (cmd[0], exc))
    return (completed.returncode, completed.stdout or "")


def _can_import(python: str, module: str) -> bool:
    """Can `python` import `module` from an unrelated working directory?

    Run from a neutral directory on purpose. `python -m tvhub` puts the CURRENT
    directory on sys.path, so testing from the install root would answer "yes"
    for a package that the scheduler - which starts in the system directory -
    could never import.
    """
    try:
        completed = subprocess.run(
            [python, "-c", "import " + module],
            cwd=tempfile.gettempdir(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except Exception:
        return False
    return completed.returncode == 0


def _windows_port_squatters(port: int) -> List[str]:
    """PIDs listening on `port`, from netstat. Windows only."""
    pids: List[str] = []
    _code, output = _run(["netstat", "-ano", "-p", "TCP"], timeout=60)
    mine = str(os.getpid())
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        pid = parts[4]
        if pid in ("0", "4", mine) or pid in pids or not pid.isdigit():
            continue
        pids.append(pid)
    return pids


def install(ctx: "Context", python: Optional[str] = None, open_firewall: bool = True) -> int:
    """Install the service for this platform. Idempotent (contract 12.1)."""
    if not is_elevated():
        if os.name == "nt":
            sys.stdout.write(
                "ERROR install needs elevation - open an Administrator command prompt\n"
                "      and run the same command again.\n"
            )
        else:
            sys.stdout.write("ERROR install needs root - run it again with sudo.\n")
        return 1

    python = python or service_python()
    if os.name == "nt":
        return install_windows(ctx, python, open_firewall)
    if sys.platform.startswith("linux"):
        return install_linux(ctx, python)

    sys.stdout.write(
        "ERROR no service installer for %s - only Windows (scheduled task) and\n"
        "      Linux (systemd) are supported. Run it in the foreground instead:\n"
        "          TVHUB_HOME=%s %s -m tvhub run\n"
        % (platform.system() or sys.platform, _root_of(ctx), sys.executable)
    )
    return 1


def uninstall(ctx: "Context") -> int:
    """Remove the service, leaving config.json, state/ and photos/ alone."""
    if not is_elevated():
        if os.name == "nt":
            sys.stdout.write("ERROR uninstall needs elevation - use an Administrator command prompt.\n")
        else:
            sys.stdout.write("ERROR uninstall needs root - run it again with sudo.\n")
        return 1
    if os.name == "nt":
        return uninstall_windows(ctx)
    if sys.platform.startswith("linux"):
        return uninstall_linux(ctx)
    sys.stdout.write("nothing to uninstall on %s - no service was ever installed here.\n" % (platform.system() or sys.platform))
    return 0


def _service_command(ctx: Any, python: str) -> Tuple[str, str]:
    """Build the command line to register, and say why it looks like that.

    Contract 12.2 registers `"<python>" -m tvhub run`. That only imports when the
    package is importable from wherever the scheduler starts the task, which is
    the system directory - and a package installed with `pip install --user` is
    invisible to SYSTEM anyway. So measure it: if the interpreter cannot import
    tvhub from a neutral directory, register a form that changes into the install
    root first. Same command, made to actually start.
    """
    root = _root_of(ctx)
    plain = '"%s" -m tvhub run' % python
    if _can_import(python, "tvhub"):
        return plain, "package is importable from any directory"
    wrapped = 'cmd /c cd /d "%s" && "%s" -m tvhub run' % (root, python)
    return (
        wrapped,
        "tvhub is not on this interpreter's path, so the task changes into %s first" % root,
    )


def install_windows(ctx: "Context", python: str, open_firewall: bool) -> int:
    """Scheduled task running as SYSTEM at boot, plus the firewall rule."""
    root = _root_of(ctx)
    port = _http_port(ctx)
    did: List[str] = []
    problems: List[str] = []

    # Delete first: /Create /F replaces a task, but removing it also clears a
    # stale one that was registered with a different interpreter path.
    code, _out = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    if code == 0:
        did.append("removed the previous %s task" % TASK_NAME)

    if open_firewall:
        _run(
            [
                "netsh", "advfirewall", "firewall", "delete", "rule",
                "name=" + FIREWALL_RULE_NAME,
            ]
        )
        code, out = _run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                "name=" + FIREWALL_RULE_NAME,
                "dir=in", "action=allow", "protocol=TCP", "localport=%d" % port,
            ]
        )
        if code == 0:
            did.append('opened TCP %d inbound as "%s"' % (port, FIREWALL_RULE_NAME))
        else:
            problems.append("firewall rule failed: %s" % out.strip())

    # A leftover process holding the port silently stops the real service from
    # binding, and the symptom is a bridge that answers nothing at all.
    for pid in _windows_port_squatters(port):
        code, out = _run(["taskkill", "/F", "/PID", pid])
        if code == 0:
            did.append("killed stray process %s that was holding port %d" % (pid, port))
        else:
            problems.append("could not kill process %s on port %d: %s" % (pid, port, out.strip()))

    if not _can_import(python, "websocket") or not _can_import(python, "requests"):
        problems.append(
            "this interpreter cannot import websocket-client and requests from a\n"
            "  neutral directory. If they were installed with 'pip install --user',\n"
            "  the SYSTEM account cannot see them - reinstall them system-wide:\n"
            "      \"%s\" -m pip install -r \"%s\"" % (python, root / "requirements.txt")
        )

    command, why = _service_command(ctx, python)
    code, out = _run(
        [
            "schtasks", "/Create", "/F",
            "/TN", TASK_NAME,
            "/SC", "ONSTART",
            "/RU", "SYSTEM",
            "/RL", "HIGHEST",
            "/TR", command,
        ]
    )
    if code != 0:
        sys.stdout.write("ERROR could not create the %s task: %s\n" % (TASK_NAME, out.strip()))
        return 1
    did.append("registered task %s to run at boot as SYSTEM" % TASK_NAME)
    did.append("command: %s" % command)
    did.append("  (%s)" % why)

    code, out = _run(["schtasks", "/Run", "/TN", TASK_NAME])
    if code == 0:
        did.append("started it")
    else:
        problems.append("task created but would not start: %s" % out.strip())

    _print_install_report(ctx, did, problems)
    sys.stdout.write(
        "\nNotes\n"
        "  Wake-on-LAN broadcasts and pairing only work from a host on the TVs'\n"
        "  own subnet, so this machine must sit on that subnet.\n"
        "  State lives in %s - machine-wide, next to the install, so the service\n"
        "  and a command line run by a user read the same tokens.\n"
        "  Manage it with:  schtasks /Query /TN %s   schtasks /End /TN %s\n"
        % (_state_dir(ctx), TASK_NAME, TASK_NAME)
    )
    return 1 if problems else 0


def uninstall_windows(ctx: "Context") -> int:
    did: List[str] = []
    code, _out = _run(["schtasks", "/End", "/TN", TASK_NAME])
    if code == 0:
        did.append("stopped the running task")
    code, out = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    if code == 0:
        did.append("deleted task %s" % TASK_NAME)
    else:
        did.append("no %s task was registered (%s)" % (TASK_NAME, out.strip() or "nothing to delete"))
    code, _out = _run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + FIREWALL_RULE_NAME])
    if code == 0:
        did.append('removed the "%s" firewall rule' % FIREWALL_RULE_NAME)
    _print_install_report(ctx, did, [])
    sys.stdout.write(
        "\nLeft alone: %s, %s and %s. Delete them by hand if you really mean to.\n"
        % (_config_path(ctx), _state_dir(ctx), _photo_root(ctx))
    )
    return 0


_UNIT_TEMPLATE = """[Unit]
Description=TVHub - Samsung TV slideshow bridge
Documentation=file://{root}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} -m tvhub run
# WorkingDirectory is what makes `-m tvhub` importable: systemd would otherwise
# start us in / and the package would not be on sys.path.
WorkingDirectory={root}
# The root is exported so every module and child process resolves the same
# machine-wide state folder, whoever runs them (contract 1).
Environment=TVHUB_HOME={root}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install_linux(ctx: "Context", python: str) -> int:
    """systemd unit, enabled and started (contract 12.3)."""
    root = _root_of(ctx)
    did: List[str] = []
    problems: List[str] = []

    unit = _UNIT_TEMPLATE.format(python=python, root=root)
    try:
        with open(UNIT_PATH, "w", encoding="utf-8") as handle:
            handle.write(unit)
        os.chmod(UNIT_PATH, 0o644)
    except OSError as exc:
        sys.stdout.write("ERROR could not write %s: %s\n" % (UNIT_PATH, exc))
        return 1
    did.append("wrote %s" % UNIT_PATH)

    if not _can_import(python, "websocket") or not _can_import(python, "requests"):
        problems.append(
            "this interpreter cannot import websocket-client and requests. The unit\n"
            "  runs as root, so a --user install will not be visible:\n"
            "      %s -m pip install -r %s" % (python, root / "requirements.txt")
        )

    for args, label in (
        (["daemon-reload"], "reloaded systemd"),
        (["enable", UNIT_NAME], "enabled %s at boot" % UNIT_NAME),
        (["restart", UNIT_NAME], "started %s" % UNIT_NAME),
    ):
        code, out = _run(["systemctl"] + args)
        if code == 0:
            did.append(label)
        else:
            problems.append("systemctl %s failed: %s" % (" ".join(args), out.strip()))

    _print_install_report(ctx, did, problems)
    sys.stdout.write(
        "\nNotes\n"
        "  Wake-on-LAN broadcasts, pairing and the UPnP volume read-back only work\n"
        "  from a host on the TVs' own subnet - this machine must sit on it.\n"
        "  Port %d is not opened here: if firewalld or ufw is running, add the rule\n"
        "  yourself (firewall-cmd --add-port=%d/tcp, or ufw allow %d/tcp). Editing\n"
        "  someone's firewall from an installer is not this program's business.\n"
        "  State lives in %s - machine-wide, so root and a user CLI agree.\n"
        "  Watch it with:  systemctl status %s   journalctl -u %s -f\n"
        % (_http_port(ctx), _http_port(ctx), _http_port(ctx), _state_dir(ctx), UNIT_NAME, UNIT_NAME)
    )
    return 1 if problems else 0


def uninstall_linux(ctx: "Context") -> int:
    did: List[str] = []
    for args, label in (
        (["stop", UNIT_NAME], "stopped %s" % UNIT_NAME),
        (["disable", UNIT_NAME], "disabled %s" % UNIT_NAME),
    ):
        code, _out = _run(["systemctl"] + args)
        if code == 0:
            did.append(label)
    try:
        os.remove(UNIT_PATH)
        did.append("removed %s" % UNIT_PATH)
    except FileNotFoundError:
        did.append("no unit at %s" % UNIT_PATH)
    except OSError as exc:
        sys.stdout.write("ERROR could not remove %s: %s\n" % (UNIT_PATH, exc))
        return 1
    _run(["systemctl", "daemon-reload"])
    did.append("reloaded systemd")
    _print_install_report(ctx, did, [])
    sys.stdout.write(
        "\nLeft alone: %s, %s and %s, and any firewall rule you added by hand.\n"
        % (_config_path(ctx), _state_dir(ctx), _photo_root(ctx))
    )
    return 0


def _print_install_report(ctx: Any, did: Sequence[str], problems: Sequence[str]) -> None:
    sys.stdout.write("done:\n")
    for line in did or ["nothing"]:
        sys.stdout.write("  - %s\n" % line)
    if problems:
        sys.stdout.write("\nneeds your attention:\n")
        for line in problems:
            sys.stdout.write("  ! %s\n" % line)
    sys.stdout.write("\n" + access_banner(ctx) + "\n")


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def _usage() -> str:
    return "\n".join(
        [
            "TVHub %s - Samsung TV slideshow bridge" % _version(),
            "",
            "usage: python -m tvhub [-v] [--root <path>] <command | path> [args]",
            "",
            "commands",
            "  run                     serve the web interface and the controller API",
            "  install                 install as a service (needs Administrator / root)",
            "  uninstall               remove the service, keeping config, state and photos",
            "  doctor                  full diagnosis, per TV; non-zero when something is wrong",
            "  pair [target ...] [secs]  pair TVs one at a time (default: every unpaired one)",
            "  scan [a.b.c.0/24]       find Samsung sets on the network",
            "  show [target] [playlist]  put the slideshow back on screen",
            "  playlist [name]         switch the whole fleet, or list playlists",
            "  learn <alias>           probe one TV and cache what it can do",
            "  version                 print the version",
            "  help                    this text",
            "",
            "options",
            "  -v, --verbose           DEBUG logging, including every HTTP request",
            "  --root <path>           install root (default: $TVHUB_HOME, else the",
            "                          directory containing the tvhub package)",
            "",
            "one-shot controller paths - anything the HTTP surface accepts, printed",
            "the same way a controller would receive it:",
            "  python -m tvhub tv/<alias>/on",
            "  python -m tvhub group/<name>/off",
            "  python -m tvhub all/show/<playlist>",
            "  python -m tvhub tv/<alias>/keys/KEY_UP,@500,KEY_ENTER",
            "  python -m tvhub playlist/<name>          switch every TV (pointer move only)",
            "  python -m tvhub homepages                the one URL to set on every TV",
            "  python -m tvhub status                   a bare verb means the whole fleet",
            "",
        ]
    )


def _dependency_advice(ctx: Any, package: str) -> str:
    return (
        "ERROR %s is required and not importable.\n"
        "      pip install -r %s\n" % (package, _root_of(ctx) / "requirements.txt")
    )


def main(argv: List[str]) -> int:
    """Parse the command line and run one command (contract 12.6)."""
    args = list(argv or [])
    verbose = False
    root: Optional[str] = None
    rest: List[str] = []

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-v", "--verbose"):
            verbose = True
        elif arg == "--root":
            index += 1
            if index >= len(args):
                sys.stderr.write("ERROR --root needs a path\n")
                return 2
            root = args[index]
        elif arg.startswith("--root="):
            root = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            rest.insert(0, "help")
        elif arg == "--version":
            rest.insert(0, "version")
        elif arg.startswith("-") and len(arg) > 1:
            sys.stderr.write("ERROR unknown option '%s'\n\n%s" % (arg, _usage()))
            return 2
        else:
            rest.append(arg)
        index += 1

    if not rest:
        sys.stdout.write(_usage())
        return 0

    command = rest[0]
    tail = rest[1:]

    if command == "help":
        sys.stdout.write(_usage())
        return 0
    if command == "version":
        sys.stdout.write("tvhub %s\n" % _version())
        return 0

    # install and uninstall must work before requirements.txt is installed, so
    # they use a Context built from store alone (which has no dependencies).
    if command in ("install", "uninstall"):
        try:
            ctx = _light_context(root)
        except Exception as exc:
            sys.stderr.write("ERROR cannot read the install root: %s\n" % exc)
            return 1
        setup_logging(ctx, verbose, to_console=verbose)
        return install(ctx) if command == "install" else uninstall(ctx)

    try:
        ctx, fleet, slideshow, _ui, app = build(root)
    except _DependencyError as exc:
        package = str(exc)
        if command == "doctor":
            # Doctor is the command someone runs when nothing works. It must
            # still print the paths and name the missing package.
            try:
                ctx = _light_context(root)
            except Exception as inner:
                sys.stderr.write("ERROR %s\n" % inner)
                return 1
            setup_logging(ctx, verbose, to_console=verbose)
            sys.stdout.write(_dependency_advice(ctx, package) + "\n")
            return cmd_doctor(ctx, None, None)  # type: ignore[arg-type]
        try:
            root_path = _resolve_root(root)
        except Exception:
            root_path = Path(".")
        sys.stderr.write(
            "ERROR %s is required and not importable.\n      pip install -r %s\n"
            % (package, root_path / "requirements.txt")
        )
        return 1
    except OSError as exc:
        # A webapp that binds its socket in App.__init__ reports "address already
        # in use" from build(), not from cmd_run - same cause, same advice.
        try:
            probe_ctx = _light_context(root)
            _report_bind_failure(
                str(_cfg(probe_ctx, "server").get("bind") or "0.0.0.0"), _http_port(probe_ctx), exc
            )
        except Exception:
            sys.stderr.write("ERROR cannot start: %s\n" % exc)
        return 1
    except Exception as exc:
        sys.stderr.write("ERROR cannot start: %s: %s\n" % (exc.__class__.__name__, exc))
        return 1

    setup_logging(ctx, verbose, to_console=(command == "run") or verbose)

    if command == "run":
        return cmd_run(ctx, fleet, app)
    if command == "doctor":
        return cmd_doctor(ctx, fleet, slideshow)
    if command == "pair":
        return cmd_pair(ctx, fleet, tail)
    if command == "scan":
        return cmd_scan(ctx, fleet, tail[0] if tail else None)
    if command == "learn":
        if not tail:
            sys.stderr.write("ERROR learn needs a TV alias\n")
            return 2
        return cmd_learn(ctx, fleet, tail[0])

    if command == "show":
        target, playlist = _show_arguments(ctx, tail)
        path = "%s/show/%s" % (target, playlist) if playlist else "%s/show" % target
    elif command == "playlist":
        path = ("playlist/" + tail[0]) if tail else "playlists"
    else:
        # Anything else is a one-shot controller path (contract 12.6).
        path = "/".join([command] + tail)

    try:
        text, code = _controller(ctx, fleet, slideshow, path)
    except Exception as exc:
        log.exception("controller path %r failed", path)
        sys.stderr.write("ERROR %s: %s\n" % (exc.__class__.__name__, exc))
        return 1
    sys.stdout.write(text)
    if code == 2:
        # A one-line pointer, not the whole usage block: these messages are read
        # in a terminal beside a wrong alias, and burying them under forty lines
        # of help is how a clear error becomes an unclear one.
        sys.stdout.write("try 'python -m tvhub help' for the commands and paths\n")
    return code


def _show_arguments(ctx: Any, tail: Sequence[str]) -> Tuple[str, Optional[str]]:
    """Work out whether `show <word>` named a target or a playlist.

    Checked against the roster rather than through Fleet.resolve, because
    resolve() falls back to "every TV" for an unknown name (contract 7.11) and
    that would turn a mistyped playlist into a fleet-wide command.
    """
    if not tail:
        return "all", None
    if len(tail) >= 2:
        return tail[0], "/".join(tail[1:])
    word = tail[0]
    if word in _tv_specs(ctx) or word in _cfg(ctx, "groups") or word == "all":
        return word, None
    return "all", word
