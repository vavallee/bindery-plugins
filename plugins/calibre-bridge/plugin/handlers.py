import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from typing import Any

PLUGIN_VERSION = "0.4.0"

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


def make_handler(
    api_key: str,
    get_db: Callable[[], Any],
    get_gui: Callable[[], Any] | None = None,
) -> type:
    from calibre_plugins.bindery_bridge.plugin.adder import add_book

    class Handler(BaseHTTPRequestHandler):
        server_version = "BinderyBridge/" + PLUGIN_VERSION

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            _log.debug(format, *args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            if not api_key:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                _log.warning("auth failure: missing Bearer token from %s", self.address_string())
                return False
            if header[len("Bearer ") :].strip() != api_key:
                _log.warning("auth failure: invalid token from %s", self.address_string())
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/health":
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
                    },
                )
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/books":
                self._send_json(404, {"error": "not found"})
                return
            if not self._check_auth():
                self._send_json(401, {"error": "unauthorized"})
                return
            db = get_db()
            if db is None:
                _log.warning("library not ready — rejecting POST /v1/books")
                self._send_json(503, {"error": "library not ready"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                self._send_json(400, {"error": "invalid json"})
                return
            path = payload.get("path")
            if not path or not isinstance(path, str):
                self._send_json(400, {"error": "path required"})
                return
            try:
                gui = get_gui() if get_gui is not None else None
                book_id, duplicate = add_book(db, path, gui=gui)
            except FileNotFoundError as exc:
                _log.warning("add_book file not found: %s", exc)
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:  # pragma: no cover - defensive
                _log.error("add_book unexpected error path=%r: %s", path, exc)
                self._send_json(500, {"error": str(exc)})
                return
            coerced_id = _coerce_book_id(book_id)
            if duplicate:
                _log.info("add_book duplicate detected id=%d path=%r", coerced_id, path)
            else:
                _log.info("add_book success id=%d path=%r", coerced_id, path)
            status = 409 if duplicate else 201
            self._send_json(status, {"id": coerced_id, "duplicate": bool(duplicate)})

    return Handler
