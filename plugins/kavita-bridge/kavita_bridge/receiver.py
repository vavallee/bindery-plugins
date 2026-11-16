"""The HTTP receiver Bindery's webhook notifier talks to.

    POST /v1/events   a Bindery notification, bearer token required
    GET  /v1/health   liveness, and Kavita status for a caller holding the token

Bindery sends the notification once, waits up to 10 seconds, logs a failure and
moves on: there is no retry (internal/notifier/notifier.go). So the receiver
answers as soon as it has queued the work and never makes Bindery wait for
Kavita. A Kavita failure is logged here and cannot become a 5xx.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import Config
from .service import Scanner, ScanService, printable

log = logging.getLogger("kavita_bridge")

IMPORT_EVENT = "bookImported"
MAX_BODY_BYTES = 256 * 1024
REQUEST_TIMEOUT = 10.0
HEALTH_CACHE_SECONDS = 15.0
MAX_CONNECTIONS = 32


class RedactFilter(logging.Filter):
    """Last line of defence: no known secret survives in a log line."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            for secret in self._secrets:
                message = message.replace(secret, "<redacted>")
            record.msg, record.args = message, None
        return True


def token_matches(supplied: str, expected: str) -> bool:
    """Constant time comparison of the presented token with the configured key."""
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class Receiver(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Config, client: Scanner, service: ScanService) -> None:
        self.config = config
        self.client = client
        self.service = service
        self._health_lock = threading.Lock()
        self._health: tuple[float, dict[str, bool]] | None = None
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__((config.bind_host, config.port), Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Past the cap a connection is closed unread, so a flood cannot grow
        # an unbounded number of threads.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def kavita_status(self) -> dict[str, bool]:
        """Cached for a few seconds, so health polling cannot hammer Kavita."""
        with self._health_lock:
            now = time.monotonic()
            if self._health is None or now - self._health[0] > HEALTH_CACHE_SECONDS:
                h = self.client.check()
                self._health = (
                    now,
                    {"reachable": h.reachable, "keyValid": h.key_valid, "canScan": h.admin},
                )
            return self._health[1]


class Handler(BaseHTTPRequestHandler):
    server: Receiver
    server_version = f"kavita-bridge/{__version__}"
    sys_version = ""
    timeout = REQUEST_TIMEOUT
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The default writes the full request line. Log the method and path only.
        log.debug("%s %s", self.command, printable(self.path.split("?", 1)[0]))

    def _reply(
        self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str, close: bool = False) -> None:
        if close:
            self.close_connection = True
        self._reply(status, {"code": code, "message": message})

    def _authorized(self) -> bool:
        key = self.server.config.api_key
        if not key:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        return scheme.lower() == "bearer" and token_matches(supplied.strip(), key)

    # -- routes --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Origin"):
            return self._error(403, "forbidden", "browser requests are not served")
        if self.path.split("?", 1)[0] != "/v1/health":
            return self._error(404, "not_found", "no such endpoint")
        body: dict[str, Any] = {"status": "ok", "version": __version__}
        # Only a caller holding the token learns whether Kavita is reachable.
        if self._authorized():
            body["kavita"] = self.server.kavita_status()
        self._reply(200, body)

    def do_POST(self) -> None:  # noqa: N802
        self._events()

    def do_PUT(self) -> None:  # noqa: N802
        # Bindery's webhook form offers POST and PUT.
        self._events()

    def _events(self) -> None:
        if self.headers.get("Origin"):
            return self._error(403, "forbidden", "browser requests are not served", close=True)
        if self.path.split("?", 1)[0] != "/v1/events":
            return self._error(404, "not_found", "no such endpoint", close=True)
        if not self._authorized():
            return self._error(401, "unauthorized", "bearer token missing or wrong", close=True)
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._error(415, "unsupported_media_type", "send application/json", close=True)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(400, "invalid_json", "Content-Length is required", close=True)
        if length < 0 or length > MAX_BODY_BYTES:
            return self._error(413, "body_too_large", "body too large", close=True)
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return self._error(400, "invalid_json", "body is not JSON", close=True)
        if not isinstance(payload, dict):
            return self._error(400, "invalid_json", "body must be a JSON object", close=True)

        event = payload.get("eventType")
        if event != IMPORT_EVENT:
            log.debug("ignoring event %s", printable(str(event)))
            return self._reply(200, {"status": "ignored"})

        path = payload.get("path")
        path = path if isinstance(path, str) and path.strip() else None
        log.info("bookImported received (path %s)", printable(path) if path else "absent")
        self.server.service.submit(path)
        self._reply(202, {"status": "queued"})
