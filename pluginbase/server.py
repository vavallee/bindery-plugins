"""Generic ThreadingHTTPServer wrapper for plugin HTTP servers."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_log = logging.getLogger(__name__)


class PluginServer:
    """Runs a :class:`BaseHTTPRequestHandler` subclass in a daemon thread.

    Usage::

        srv = PluginServer()
        srv.start(port=8099, bind_host="0.0.0.0", handler_cls=MyHandler)
        # ... later ...
        srv.stop()
    """

    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(
        self,
        port: int,
        bind_host: str,
        handler_cls: type[BaseHTTPRequestHandler],
        thread_name: str = "plugin-http",
    ) -> None:
        self._httpd = ThreadingHTTPServer((bind_host, port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()
        _log.info("%s listening on %s:%d", thread_name, bind_host, port)

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
                _log.info("plugin server stopped")
            except Exception as exc:
                _log.error("error stopping plugin server: %s", exc)
            finally:
                self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._httpd is not None
