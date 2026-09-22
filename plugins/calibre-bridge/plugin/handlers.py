import hmac
import json
import logging
import re
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

PLUGIN_VERSION = "0.6.0"

# Optional protocol features. A client that understands none of them still
# works: everything 0.6.0 adds is a new endpoint, a new response field or a
# new response header, and 0.4.0 and 0.5.0 clients ignore all three.
CAPABILITIES = [
    "book_metadata",
    "cover",
    "path_probe",
    "metadata_update",
    "error_codes",
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
CODE_BAD_FORMAT = "bad_format"
CODE_BODY_TOO_LARGE = "body_too_large"
CODE_NOT_FOUND = "not_found"
CODE_INTERNAL = "internal"

# Hint for the client's 503 backoff. protocol.md asks for exponential backoff
# up to about 30 seconds; a library swap is normally over in well under one.
RETRY_AFTER_SECONDS = 2

_BOOK_ID_PATH = re.compile(r"^/v1/books/(?P<book_id>[^/]+)$")

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


def make_handler(
    api_key: str,
    get_db: Callable[[], Any],
    get_gui: Callable[[], Any] | None = None,
    ingest_root: str = "",
    max_body_bytes: int = 64 * 1024 * 1024,
) -> type:
    from calibre_plugins.bindery_bridge.plugin import adder as adder_mod

    class Handler(BaseHTTPRequestHandler):
        server_version = "BinderyBridge/" + PLUGIN_VERSION

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
            self._send_error_json(401, CODE_UNAUTHORIZED, "unauthorized")

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

        def _check_auth(self) -> bool:
            if not api_key:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                _log.warning("auth failure: missing Bearer token from %s", self.address_string())
                return False
            if not _tokens_match(header[len("Bearer ") :].strip(), api_key):
                _log.warning("auth failure: invalid token from %s", self.address_string())
                return False
            return True

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
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                return (json.loads(raw.decode("utf-8")) if raw else {}), True
            except ValueError:
                self._send_error_json(400, CODE_INVALID_JSON, "invalid json")
                return None, False

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
            parsed = urlparse(self.path)
            if parsed.path == "/v1/health":
                self._handle_health()
                return
            if parsed.path == "/v1/paths":
                self._handle_path_probe(parsed.query)
                return
            self._send_not_found()

        def _handle_health(self) -> None:
            db = get_db()
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
            if urlparse(self.path).path != "/v1/books":
                self._send_not_found()
                return
            if not self._check_auth():
                self._send_unauthorized()
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
            try:
                gui = get_gui() if get_gui is not None else None
                result = adder_mod.add_book_detailed(
                    db, path, gui=gui, metadata=metadata, ingest_root=ingest_root
                )
            except (FileNotFoundError, ValueError) as exc:
                self._send_path_error(exc)
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
            # Only present when the request actually carried a coverPath, so a
            # 0.5.0 client keeps seeing the exact response shape it knows.
            if result.cover_applied is not None:
                body["cover_applied"] = result.cover_applied
                if not result.cover_applied:
                    _log.warning("cover not applied for id=%d path=%r", coerced_id, path)
            self._send_json(409 if result.duplicate else 201, body)

        # -- PATCH ---------------------------------------------------------

        def do_PATCH(self) -> None:  # noqa: N802
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


def make_degraded_handler(reason: str) -> type:
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

    class DegradedHandler(BaseHTTPRequestHandler):
        server_version = "BinderyBridge/" + PLUGIN_VERSION

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
