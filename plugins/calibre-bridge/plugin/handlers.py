import contextlib
import hmac
import http.client
import io
import ipaddress
import json
import logging
import math
import re
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse


def _plugin_version() -> str:
    """The plugin version, read from the one place it is written.

    ``version = (X, Y, Z)`` on the plugin class in the top level
    ``__init__.py`` is what Calibre itself reads, so nothing else spells the
    number out. Inside Calibre that file is the ``calibre_plugins.bindery_bridge``
    package, which Calibre imports before it loads anything under ``plugin/``.
    The test suite loads this file by path with that package stubbed or absent,
    so there the same file is parsed from disk, one directory up.
    """
    try:
        from calibre_plugins.bindery_bridge import BinderyBridge

        version = BinderyBridge.version
    except ImportError as import_error:
        import ast
        import pathlib

        init_file = pathlib.Path(__file__).resolve().parent.parent / "__init__.py"
        try:
            source = init_file.read_text(encoding="utf-8")
        except OSError as read_error:
            why_parse = f"{init_file} could not be read ({read_error})"
        else:
            match = re.search(r"^[ \t]+version\s*=\s*(\([^)]*\))", source, re.M)
            if match is not None:
                return ".".join(str(part) for part in ast.literal_eval(match.group(1)))
            why_parse = f"{init_file} has no version tuple"
        raise RuntimeError(
            "The plugin version could not be determined. Two strategies were tried. "
            "(1) Importing BinderyBridge from calibre_plugins.bindery_bridge failed "
            f"({import_error}). "
            f"(2) Parsing the package __init__.py next to this file failed: {why_parse}. "
            "This happens inside a zip, where __file__ is not a real path. "
            "The package __init__ must not import plugin.* at module level, because "
            "that makes the import in (1) circular."
        ) from import_error
    return ".".join(str(part) for part in version)


PLUGIN_VERSION = _plugin_version()

# Optional protocol features. A client that understands none of them still
# works: everything 0.6.0 adds is a new endpoint, a new response field or a
# new response header, and 0.4.0 and 0.5.0 clients ignore all three.
CAPABILITIES = [
    "book_metadata",
    "cover",
    "path_probe",
    "metadata_update",
    "error_codes",
    # 0.7.0: ``addFormat`` on POST /v1/books puts a second file of the same
    # Bindery book on the row the first one made, instead of a 409.
    "add_format",
    # 0.8.0: the plugin can fetch deliveries from Bindery's /bridge/v1
    # routes. Advertised here and in X-Bridge-Capabilities on every pull
    # request, so both sides see the same list.
    "pull",
]

# Machine readable error codes, sent alongside the human readable ``error``
# string that 0.4.0 and 0.5.0 clients already read. The motivating case: a
# wrong container mount produced a 400 that the Go client could not tell from
# bad metadata, so it logged "metadata payload rejected" and re-sent the whole
# request. path_not_found and invalid_metadata are now distinguishable.
CODE_UNAUTHORIZED = "unauthorized"
CODE_DB_UNAVAILABLE = "db_unavailable"
CODE_INVALID_JSON = "invalid_json"
CODE_INVALID_METADATA = "invalid_metadata"
CODE_PATH_NOT_FOUND = "path_not_found"
CODE_PATH_FORBIDDEN = "path_forbidden"
# 0.6.3: a file that is there but cannot be opened, and a failure inside
# Calibre's own copy into the library. Both used to surface as ``internal``.
CODE_PATH_UNREADABLE = "path_unreadable"
CODE_COPY_FAILED = "copy_failed"
CODE_BAD_FORMAT = "bad_format"
CODE_BODY_TOO_LARGE = "body_too_large"
CODE_NOT_FOUND = "not_found"
CODE_INTERNAL = "internal"
# Requests the server refuses on sight, before any auth or body read.
CODE_FORBIDDEN = "forbidden"
CODE_UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
CODE_RATE_LIMITED = "rate_limited"

# Hint for the client's 503 backoff. protocol.md asks for exponential backoff
# up to about 30 seconds; a library swap is normally over in well under one.
RETRY_AFTER_SECONDS = 2

_BOOK_ID_PATH = re.compile(r"^/v1/books/(?P<book_id>[^/]+)$")

# The Server header names the plugin and its version, not the Python and
# BaseHTTP versions.
SERVER_NAME = "BinderyBridge/" + PLUGIN_VERSION

# Seconds a connection has in total to send its request line and headers (a
# hard deadline, not a per read timeout, so a byte a second cannot keep it
# open), and the longest it may sit idle between reads while the body arrives.
# The body has its own hard deadline, see _read_body.
DEFAULT_HEADER_TIMEOUT = 10.0
DEFAULT_BODY_TIMEOUT = 30.0
# Slowest sustained body upload the total deadline allows for, in bytes/second.
# A body may take body_timeout plus length / BODY_MIN_RATE seconds in all, so
# at the 64 MiB default size cap a connection is held for 94 seconds at most.
# Bindery's own bodies are a few hundred bytes.
BODY_MIN_RATE = 1024 * 1024
# Most bytes and header lines a request line plus headers may take. Bindery
# sends well under 1 KiB and a reverse proxy adds a few lines.
MAX_HEAD_BYTES = 16 * 1024
MAX_HEADERS = 64

# Failed credential attempts per client address: this many inside the window
# earns a block of the same length.
AUTH_FAIL_LIMIT = 5
AUTH_FAIL_WINDOW = 60.0
AUTH_THROTTLE_MAX_ENTRIES = 1024

_log = logging.getLogger(__name__)


def _calibre_version() -> str:
    try:
        from calibre.constants import numeric_version

        return ".".join(str(p) for p in numeric_version[:3])
    except Exception:
        return "unknown"


def _coerce_book_id(value: Any) -> int:
    """Defensively coerce add_book's return to an int.

    Early versions returned the raw (mi, format_map) tuple for the duplicate
    path, crashing the handler with TypeError on int() coercion and producing
    an empty TCP reply. The fix lives in adder.py; this guard ensures a future
    regression surfaces as id=0 rather than another EOF.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tokens_match(presented: str, expected: str) -> bool:
    """Constant time bearer token comparison.

    ``!=`` on str short circuits at the first differing byte, which leaks the
    length of the matching prefix to anyone who can time the response. Encode
    first because compare_digest rejects non-ASCII str inputs.
    """
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


_HOST_PORT = re.compile(r"(?P<host>[^\s:@/\\\[\]]+)(?::(?P<port>[0-9]{1,5}))?")
_HOST_PORT_V6 = re.compile(r"\[(?P<host>[0-9a-f:.]+)\](?::(?P<port>[0-9]{1,5}))?")


def _host_is_loopback(host_header: str) -> bool:
    """True when a Host header names this machine and is well formed.

    Accepts ``localhost``, any 127.0.0.0/8 literal and ``[::1]``, each with an
    optional numeric port from 1 to 65535. The header is parsed whole: userinfo,
    spaces, a port that is not a number and anything else that is not
    ``host``, ``host:port`` or ``[v6]:port`` is refused, as is a missing header.
    """
    text = host_header.strip().lower()
    match = _HOST_PORT_V6.fullmatch(text) if text.startswith("[") else _HOST_PORT.fullmatch(text)
    if match is None:
        return False
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        return False
    host = match.group("host")
    if host in ("localhost", "localhost.") and not text.startswith("["):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 4:
        return not text.startswith("[") and ip in ipaddress.ip_network("127.0.0.0/8")
    return ip == ipaddress.ip_address("::1")


def _is_json_content_type(value: str) -> bool:
    return value.split(";", 1)[0].strip().lower() == "application/json"


def _throttle_key(addr: str) -> str:
    """The table key for a client address.

    An IPv6 peer is keyed by its /64, since one host usually holds a whole
    prefix and could otherwise rotate addresses, and an IPv4 mapped IPv6
    address is keyed as the IPv4 address it carries.
    """
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return addr
    if ip.version == 6:
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class _FailureThrottle:
    """Per client address counter of failed credential attempts.

    ``limit`` failures inside ``window`` seconds block the address for
    ``window`` seconds. Memory is bounded. When the table is full, expired
    entries go first, then the oldest entry that is not blocked. If every entry
    is a live block the new address is not tracked at all, so a flood of other
    addresses can never release a block early.
    """

    def __init__(
        self,
        limit: int = AUTH_FAIL_LIMIT,
        window: float = AUTH_FAIL_WINDOW,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = AUTH_THROTTLE_MAX_ENTRIES,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._max = max_entries
        self._lock = threading.Lock()
        # key -> [window start, failures in window, blocked until]
        self._entries: dict[str, list[float]] = {}

    def retry_after(self, addr: str) -> int:
        """Whole seconds until ``addr`` may try again; 0 when it is not blocked."""
        with self._lock:
            entry = self._entries.get(_throttle_key(addr))
            if entry is None:
                return 0
            remaining = entry[2] - self._clock()
            return math.ceil(remaining) if remaining > 0 else 0

    def record_failure(self, addr: str) -> bool:
        """Count one failure. True when this one tripped the block."""
        key = _throttle_key(addr)
        with self._lock:
            now = self._clock()
            entry = self._entries.get(key)
            if entry is None or (now - entry[0] >= self._window and now >= entry[2]):
                if entry is None and len(self._entries) >= self._max and not self._make_room(now):
                    return False
                entry = [now, 0.0, 0.0]
                self._entries[key] = entry
            entry[1] += 1
            if entry[1] >= self._limit and entry[2] <= now:
                entry[2] = now + self._window
                return True
            return False

    def _make_room(self, now: float) -> bool:
        """Free one slot if that can be done without releasing a live block."""
        expired = [
            a for a, e in self._entries.items() if now >= e[2] and now - e[0] >= self._window
        ]
        for key in expired:
            del self._entries[key]
        if len(self._entries) < self._max:
            return True
        unblocked = [a for a, e in self._entries.items() if e[2] <= now]
        if not unblocked:
            return False
        del self._entries[min(unblocked, key=lambda a: self._entries[a][0])]
        return True


class _HeadTooLarge(http.client.HTTPException):
    """The request line and headers are bigger than MAX_HEAD_BYTES."""


class _DeadlineRaw(io.RawIOBase):
    """Raw socket reader whose every read respects a hard deadline.

    A socket timeout alone is per recv, so a client sending one byte a second
    never trips it. Here each recv is given the smaller of the idle timeout and
    the time left to the deadline, so the deadline bounds the whole read.
    """

    def __init__(self, sock: socket.socket, idle: float) -> None:
        super().__init__()
        self._sock = sock
        self._idle = idle
        self._deadline: float | None = None

    def arm(self, total: float | None, idle: float) -> None:
        self._idle = idle
        self._deadline = None if total is None else time.monotonic() + total

    def release(self, idle: float) -> None:
        """Drop the deadline; reads, and writes on the socket, idle for ``idle``."""
        self.arm(None, idle)
        with contextlib.suppress(OSError):
            self._sock.settimeout(idle)

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        timeout = self._idle
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("read deadline passed")
            timeout = min(timeout, left)
        # A recv that idles past ``timeout`` raises TimeoutError itself.
        self._sock.settimeout(timeout)
        return self._sock.recv_into(b)


class _RequestReader(io.BufferedReader):
    """Buffered request reader with a deadline and a cap on header bytes."""

    def __init__(self, sock: socket.socket, idle: float) -> None:
        self._deadline_raw = _DeadlineRaw(sock, idle)
        super().__init__(self._deadline_raw)
        self._head_budget: int | None = None

    def arm(self, total: float | None, idle: float, head_budget: int | None = None) -> None:
        self._head_budget = head_budget
        self._deadline_raw.arm(total, idle)

    def disarm(self, idle: float) -> None:
        """Drop the deadline and the byte cap; later reads idle for ``idle``."""
        self._head_budget = None
        self._deadline_raw.release(idle)

    def readline(self, size: int | None = -1) -> bytes:
        budget = self._head_budget
        if budget is None:
            return super().readline(size)
        limit = budget + 1 if size is None or size < 0 else min(size, budget + 1)
        line = super().readline(limit)
        self._head_budget = budget - len(line)
        if self._head_budget < 0:
            raise _HeadTooLarge(f"request headers are larger than {MAX_HEAD_BYTES} bytes")
        return line


class _BridgeHandlerBase(BaseHTTPRequestHandler):
    """Settings every bridge handler shares: deadlines, header limits, Server header."""

    # Total seconds for the request line and headers, and the idle timeout of
    # each read. Enforced by _RequestReader, not by the socket alone.
    timeout = DEFAULT_HEADER_TIMEOUT

    rfile: _RequestReader  # type: ignore[assignment]

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self.rfile = _RequestReader(self.connection, self.timeout)

    def handle_one_request(self) -> None:
        self.rfile.arm(self.timeout, self.timeout, head_budget=MAX_HEAD_BYTES)
        try:
            super().handle_one_request()
        except _HeadTooLarge:
            # Raised before the headers are parsed, so the request line is too big.
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_error(431, "Request header too large")
        finally:
            self.rfile.disarm(self.timeout)

    def parse_request(self) -> bool:
        ok = super().parse_request()
        # The headers are read: later reads (the body) set their own deadline.
        self.rfile.disarm(self.timeout)
        if not ok:
            return False
        if len(self.headers) > MAX_HEADERS:
            self.send_error(431, "Too many headers")
            return False
        if len(self.headers.get_all("Host") or []) > 1:
            self.send_error(400, "More than one Host header")
            return False
        return True

    def version_string(self) -> str:
        return SERVER_NAME


def make_handler(
    api_key: str,
    get_db: Callable[[], Any],
    get_gui: Callable[[], Any] | None = None,
    ingest_root: str = "",
    max_body_bytes: int = 64 * 1024 * 1024,
    on_added: Callable[[int], Any] | None = None,
    on_updated: Callable[[int], Any] | None = None,
    header_timeout: float = DEFAULT_HEADER_TIMEOUT,
    body_timeout: float = DEFAULT_BODY_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
) -> type:
    from calibre_plugins.bindery_bridge.plugin import adder as adder_mod

    throttle = _FailureThrottle(clock=clock)

    class Handler(_BridgeHandlerBase):
        timeout = header_timeout
        # Seconds the current request must wait before retrying; set by
        # _check_auth when the client address is blocked.
        _throttle_wait = 0

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            _log.debug(format, *args)

        def _send_json(
            self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(
            self,
            status: int,
            code: str,
            message: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            """Every error carries both shapes.

            ``error`` keeps its 0.4.0 meaning because old clients read it;
            ``code`` is the machine readable half new clients branch on.
            """
            self._send_json(status, {"error": message, "code": code}, headers=headers)

        def _send_unauthorized(self) -> None:
            if self._throttle_wait:
                self._send_error_json(
                    429,
                    CODE_RATE_LIMITED,
                    "too many failed attempts",
                    headers={"Retry-After": str(self._throttle_wait)},
                )
                return
            self._send_error_json(401, CODE_UNAUTHORIZED, "unauthorized")

        def _gate(self) -> bool:
            """Refuse requests a browser or a rebound hostname could be behind.

            Bindery never sends ``Origin``, and a browser always does on a
            cross origin request, so any ``Origin`` is refused on every route.
            With no api_key the bridge has no credential, so the Host header
            must also name this machine, which closes DNS rebinding.
            """
            if "Origin" in self.headers:
                self._send_error_json(403, CODE_FORBIDDEN, "cross-origin requests are refused")
                return False
            if not api_key and not _host_is_loopback(self.headers.get("Host", "")):
                self._send_error_json(
                    403, CODE_FORBIDDEN, "without an api_key only a loopback Host is accepted"
                )
                return False
            return True

        def _send_not_found(self) -> None:
            self._send_error_json(404, CODE_NOT_FOUND, "not found")

        def _send_db_unavailable(self) -> None:
            _log.warning("library not ready — rejecting %s %s", self.command, self.path)
            self._send_error_json(
                503,
                CODE_DB_UNAVAILABLE,
                "library not ready",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )

        def _check_auth(self, quiet: bool = False) -> bool:
            """True when the request may act. ``quiet`` skips the failure log.

            Health uses the quiet form: it is unauthenticated by design, so a
            probe without a token is not an auth failure worth a warning.
            """
            self._throttle_wait = 0
            if not api_key:
                return True
            addr = self.client_address[0]
            wait = throttle.retry_after(addr)
            if wait:
                self._throttle_wait = wait
                return False
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                if not quiet:
                    _log.warning(
                        "auth failure: missing Bearer token from %s", self.address_string()
                    )
                    self._count_failure(addr)
                return False
            if not _tokens_match(header[len("Bearer ") :].strip(), api_key):
                if not quiet:
                    _log.warning("auth failure: invalid token from %s", self.address_string())
                # Counted even on health: a wrong token there is still a guess.
                self._count_failure(addr)
                return False
            return True

        def _count_failure(self, addr: str) -> None:
            if throttle.record_failure(addr):
                _log.warning(
                    "too many auth failures from %s, blocking it for %d seconds",
                    addr,
                    int(AUTH_FAIL_WINDOW),
                )

        def _require_json_content_type(self) -> bool:
            """Every method that reads a body wants JSON. Sends the 415 when not."""
            if _is_json_content_type(self.headers.get("Content-Type", "")):
                return True
            self._send_error_json(
                415, CODE_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json"
            )
            return False

        def _read_json_body(self) -> tuple[Any, bool]:
            """Return ``(payload, ok)``. On failure the error has been sent."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_error_json(400, CODE_INVALID_JSON, "invalid Content-Length")
                return None, False
            if length > max_body_bytes:
                _log.warning(
                    "rejecting oversized body: %d > %d from %s",
                    length,
                    max_body_bytes,
                    self.address_string(),
                )
                self._send_error_json(413, CODE_BODY_TOO_LARGE, "payload too large")
                return None, False
            try:
                raw = self._read_body(length) if length > 0 else b""
            except TimeoutError:
                self.close_connection = True
                _log.warning("timed out reading the body from %s", self.address_string())
                self._send_error_json(408, CODE_INVALID_JSON, "timed out reading the body")
                return None, False
            try:
                return (json.loads(raw.decode("utf-8")) if raw else {}), True
            except (ValueError, RecursionError):
                # RecursionError: a body nested deeper than the parser allows.
                self._send_error_json(400, CODE_INVALID_JSON, "invalid json")
                return None, False

        def _read_body(self, length: int) -> bytes:
            """Read ``length`` bytes, or raise TimeoutError.

            Each read may idle for ``body_timeout`` seconds, and the whole body
            has ``body_timeout`` plus the time ``BODY_MIN_RATE`` would need, as a
            hard deadline enforced inside every recv. A large body on a fast
            link finishes, a trickle is cut off at the deadline.
            """
            total = body_timeout + length / BODY_MIN_RATE
            self.rfile.arm(total, body_timeout)
            try:
                data: bytes = self.rfile.read(length)
            finally:
                self.rfile.disarm(header_timeout)
            return data

        def _send_path_error(self, exc: Exception) -> None:
            """Map an adder exception onto the error code it earned."""
            if isinstance(exc, FileNotFoundError):
                _log.warning("path not found: %s", exc)
                self._send_error_json(400, CODE_PATH_NOT_FOUND, str(exc))
            elif isinstance(exc, adder_mod.PathForbidden):
                _log.warning("path forbidden: %s", exc)
                self._send_error_json(400, CODE_PATH_FORBIDDEN, str(exc))
            elif isinstance(exc, adder_mod.BadFormat):
                _log.warning("bad book format: %s", exc)
                self._send_error_json(400, CODE_BAD_FORMAT, str(exc))
            else:
                _log.warning("bad request: %s", exc)
                self._send_error_json(400, CODE_INVALID_METADATA, str(exc))

        # -- GET -----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if not self._gate():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/v1/health":
                self._handle_health()
                return
            if parsed.path == "/v1/paths":
                self._handle_path_probe(parsed.query)
                return
            self._send_not_found()

        def _handle_health(self) -> None:
            # Health stays reachable without a token, but the library path is
            # a filesystem layout detail, so only an authenticated caller (or
            # a loopback bridge with no api_key at all) gets it.
            db = get_db() if self._check_auth(quiet=True) else None
            library = ""
            if db is not None:
                try:
                    library = db.library_path
                except Exception:
                    library = ""
            self._send_json(
                200,
                {
                    "plugin_version": PLUGIN_VERSION,
                    "calibre_version": _calibre_version(),
                    "library": library,
                    "capabilities": CAPABILITIES,
                },
            )

        def _handle_path_probe(self, query: str) -> None:
            """Answer "can Calibre see this path" without opening it.

            Deliberately does not require a database: the whole point is to
            diagnose a mount before anything is imported, and during a library
            swap is exactly when an operator is looking.
            """
            if not self._check_auth():
                self._send_unauthorized()
                return
            values = parse_qs(query).get("path") or []
            path = values[0].strip() if values else ""
            if not path:
                self._send_error_json(400, CODE_INVALID_METADATA, "path required")
                return
            try:
                result = adder_mod.probe_path(path, ingest_root=ingest_root)
            except ValueError as exc:
                self._send_path_error(exc)
                return
            except Exception as exc:  # pragma: no cover - defensive
                _log.error("path probe failed path=%r: %s", path, exc)
                self._send_error_json(500, CODE_INTERNAL, str(exc))
                return
            self._send_json(200, result)

        # -- POST ----------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802
            if not self._gate():
                return
            if urlparse(self.path).path != "/v1/books":
                self._send_not_found()
                return
            if not self._check_auth():
                self._send_unauthorized()
                return
            if not self._require_json_content_type():
                return
            db = get_db()
            if db is None:
                self._send_db_unavailable()
                return
            payload, ok = self._read_json_body()
            if not ok:
                return
            if not isinstance(payload, dict):
                self._send_error_json(400, CODE_INVALID_METADATA, "body must be an object")
                return
            path = payload.get("path")
            if not path or not isinstance(path, str):
                self._send_error_json(400, CODE_INVALID_METADATA, "path required")
                return
            metadata = payload.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                self._send_error_json(400, CODE_INVALID_METADATA, "metadata must be an object")
                return
            add_format = payload.get("addFormat")
            if add_format is not None and not isinstance(add_format, bool):
                self._send_error_json(400, CODE_INVALID_METADATA, "addFormat must be a boolean")
                return
            try:
                gui = get_gui() if get_gui is not None else None
                result = adder_mod.add_book_detailed(
                    db,
                    path,
                    gui=gui,
                    metadata=metadata,
                    ingest_root=ingest_root,
                    on_added=on_added,
                    on_updated=on_updated,
                    add_format=bool(add_format),
                )
            except (FileNotFoundError, ValueError) as exc:
                self._send_path_error(exc)
                return
            except adder_mod.SourceUnreadable as exc:
                _log.warning("path unreadable: %s", exc)
                self._send_error_json(400, CODE_PATH_UNREADABLE, str(exc))
                return
            except adder_mod.CopyFailed as exc:
                _log.error("copy into the library failed: %s (%s)", exc, exc.__cause__)
                self._send_error_json(500, CODE_COPY_FAILED, str(exc))
                return
            except Exception as exc:  # pragma: no cover - defensive
                _log.error("add_book unexpected error path=%r: %s", path, exc)
                self._send_error_json(500, CODE_INTERNAL, str(exc))
                return
            coerced_id = _coerce_book_id(result.book_id)
            if result.duplicate:
                _log.info("add_book duplicate detected id=%d path=%r", coerced_id, path)
            else:
                _log.info("add_book success id=%d path=%r", coerced_id, path)
            body: dict[str, Any] = {"id": coerced_id, "duplicate": bool(result.duplicate)}
            # Only present when the file went onto a row that was already
            # there, so every other response keeps its 0.6 shape.
            if result.format_added:
                body["format_added"] = True
            # Only present when the request actually carried a coverPath, so a
            # 0.5.0 client keeps seeing the exact response shape it knows.
            if result.cover_applied is not None:
                body["cover_applied"] = result.cover_applied
                if not result.cover_applied:
                    _log.warning("cover not applied for id=%d path=%r", coerced_id, path)
            self._send_json(409 if result.duplicate else 201, body)

        # -- PATCH ---------------------------------------------------------

        def do_PATCH(self) -> None:  # noqa: N802
            if not self._gate():
                return
            match = _BOOK_ID_PATH.match(urlparse(self.path).path)
            if match is None:
                self._send_not_found()
                return
            if not self._check_auth():
                self._send_unauthorized()
                return
            try:
                book_id = int(match.group("book_id"))
            except ValueError:
                # An id that is not a number does not name a book, so this is
                # the same answer as an id that has been deleted.
                self._send_error_json(
                    404, CODE_NOT_FOUND, f"No such book: {match.group('book_id')!r}"
                )
                return
            if not self._require_json_content_type():
                return
            db = get_db()
            if db is None:
                self._send_db_unavailable()
                return
            payload, ok = self._read_json_body()
            if not ok:
                return
            if not isinstance(payload, dict):
                self._send_error_json(400, CODE_INVALID_METADATA, "metadata must be an object")
                return
            try:
                applied = adder_mod.update_book(db, book_id, payload)
            except adder_mod.BookNotFound as exc:
                _log.info("update_book miss id=%d", book_id)
                self._send_error_json(404, CODE_NOT_FOUND, str(exc))
                return
            except ValueError as exc:
                self._send_path_error(exc)
                return
            except Exception as exc:  # pragma: no cover - defensive
                _log.error("update_book unexpected error id=%d: %s", book_id, exc)
                self._send_error_json(500, CODE_INTERNAL, str(exc))
                return
            # ``updated`` is true whenever the patch was accepted and applied;
            # ``fields`` names what actually changed, which is empty when the
            # row already had everything (the fill only rule in update_book).
            self._send_json(200, {"id": book_id, "updated": True, "fields": applied})

    return Handler


def make_degraded_handler(reason: str, header_timeout: float = DEFAULT_HEADER_TIMEOUT) -> type:
    """A handler for when the real bridge refused to start.

    ``server.start`` fails closed when the configured bind host is not
    loopback and no api_key is set, because that would put an unauthenticated
    add endpoint on the network. Before 0.6.0 the only report of that was a
    five second Calibre status bar toast, which nobody sees on a headless or
    KasmVNC deployment: Bindery just got connection refused and neither side
    named the api_key.

    This serves ``GET /v1/health`` with ``status: "degraded"`` and the reason,
    and answers everything else 401 with the same reason, so the Go client
    prints its own "check api_key in Settings" message instead of a dial
    error. It advertises no capabilities and it never touches the library, so
    it exposes nothing the refusal was protecting.
    """

    class DegradedHandler(_BridgeHandlerBase):
        timeout = header_timeout

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            _log.debug(format, *args)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == "/v1/health":
                self._send(
                    200,
                    {
                        "plugin_version": PLUGIN_VERSION,
                        "calibre_version": _calibre_version(),
                        "library": "",
                        "capabilities": [],
                        "status": "degraded",
                        "error": reason,
                    },
                )
                return
            self._send(404, {"error": "not found", "code": CODE_NOT_FOUND})

        def do_POST(self) -> None:  # noqa: N802
            self._send(401, {"error": reason, "code": CODE_UNAUTHORIZED})

        def do_PATCH(self) -> None:  # noqa: N802
            self._send(401, {"error": reason, "code": CODE_UNAUTHORIZED})

    return DegradedHandler
