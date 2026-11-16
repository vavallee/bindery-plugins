"""A fake Kavita on a real socket, standard library only.

It implements just the routes the bridge calls, with the verbs, parameters and
auth rules read from Kavita's source (see the plugin README). It is the
bridge's idea of Kavita, not Kavita: that is the limit of these tests.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

AUTH_KEY = "KavitaAuthKey0123456789abcd"


class FakeKavita:
    def __init__(self) -> None:
        self.libraries: list[dict[str, Any]] = [
            {"id": 1, "name": "Books", "folders": ["/data/books"]},
            {"id": 2, "name": "Comics", "folders": ["/data/comics", "/data/manga"]},
        ]
        self.admin = True
        self.valid_key = AUTH_KEY
        self.requests: list[dict[str, Any]] = []
        self.issued: list[str] = []
        self.valid_tokens: set[str] = set()
        self.fail_scans_with: int | None = None
        self.hang = False
        self._release = threading.Event()
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with fake._lock:
                    fake.requests.append(
                        {
                            "method": self.command,
                            "path": url.path,
                            "query": query,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                        }
                    )
                if fake.hang:
                    fake._release.wait(30)
                status, body = fake.route(self.command, url.path, query, self.headers)
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )

    # -- behaviour -----------------------------------------------------------

    def authenticated(self, headers: Any) -> bool:
        if headers.get("x-api-key") == self.valid_key:
            return True
        auth = headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in self.valid_tokens

    def route(self, method: str, path: str, query: dict[str, str], headers: Any) -> tuple[int, Any]:
        path = path.lower()
        if (method, path) == ("POST", "/api/plugin/authenticate"):
            if query.get("apiKey") != self.valid_key:
                return 401, None
            token = f"jwt-token-{len(self.issued) + 1}-secretjwt"
            self.issued.append(token)
            self.valid_tokens.add(token)
            return 200, {"username": "bridge", "token": token, "apiKey": self.valid_key}
        if not self.authenticated(headers):
            return 401, None
        if (method, path) == ("GET", "/api/library/libraries"):
            return 200, self.libraries
        if (method, path) == ("GET", "/api/library/list"):
            return (200, []) if self.admin else (403, None)
        if method == "POST" and path in ("/api/library/scan", "/api/library/scan-all"):
            if not self.admin:
                return 403, None
            if self.fail_scans_with:
                return self.fail_scans_with, None
            return 200, None
        return 404, None

    # -- helpers for tests ---------------------------------------------------

    def scans(self) -> list[tuple[str, str | None]]:
        """(path, libraryId) of every scan call, in order."""
        with self._lock:
            return [
                (r["path"].lower(), r["query"].get("libraryId"))
                for r in self.requests
                if r["path"].lower() in ("/api/library/scan", "/api/library/scan-all")
            ]

    def revoke_tokens(self) -> None:
        self.valid_tokens.clear()

    def start(self) -> FakeKavita:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._release.set()
        self.server.shutdown()
        self.server.server_close()
