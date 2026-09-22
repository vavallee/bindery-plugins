import logging
import threading
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from typing import Any

from calibre_plugins.bindery_bridge.plugin import status
from calibre_plugins.bindery_bridge.plugin.handlers import make_degraded_handler, make_handler

_log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(bind_host: str) -> bool:
    return bind_host.strip().lower() in _LOOPBACK_HOSTS


class BridgeServer:
    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.degraded_reason = ""

    def start(
        self,
        port: int,
        bind_host: str,
        api_key: str,
        get_db: Callable[[], Any],
        get_gui: Callable[[], Any] | None = None,
        ingest_root: str = "",
        max_body_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        # Fail closed on network exposure: a non-loopback bind with no API key
        # would expose the unauthenticated add endpoint to the network. Refuse
        # to start rather than silently listening. Loopback binds (local dev)
        # and any bind with an api_key set are unaffected.
        if not api_key and not _is_loopback(bind_host):
            raise ValueError(
                f"refusing to bind to non-loopback host {bind_host!r} without an api_key — "
                "set an api_key in the Bindery Bridge config, or bind to 127.0.0.1"
            )
        handler_cls = make_handler(
            api_key=api_key,
            get_db=get_db,
            get_gui=get_gui,
            ingest_root=ingest_root,
            max_body_bytes=max_body_bytes,
        )
        self._serve(handler_cls, port, bind_host)
        status.set_running(f"{bind_host}:{port}")
        _log.info("calibre-bridge listening on %s:%d", bind_host, port)

    def start_degraded(self, port: int, bind_host: str, reason: str) -> None:
        """Serve health only, explaining why the real bridge is not listening.

        A refusal to start used to reach the operator as a five second Calibre
        status bar toast and nothing else, so on a headless or KasmVNC install
        the only symptom was Bindery reporting connection refused, with neither
        side naming the api_key. This keeps the port answering: health reports
        ``status: "degraded"`` with the reason, and an add attempt gets a 401
        carrying it, which is the status Bindery already renders as "check
        api_key in Settings". No library access and no capabilities, so nothing
        the refusal protects is exposed.
        """
        self.degraded_reason = reason
        self._serve(make_degraded_handler(reason), port, bind_host)
        status.set_degraded(reason, f"{bind_host}:{port}")
        _log.error("calibre-bridge degraded on %s:%d: %s", bind_host, port, reason)

    def _serve(self, handler_cls: type, port: int, bind_host: str) -> None:
        self._httpd = ThreadingHTTPServer((bind_host, port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="bindery-bridge-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
                _log.info("calibre-bridge stopped")
            except Exception as exc:
                _log.error("error stopping calibre-bridge server: %s", exc)
            finally:
                self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        status.set_stopped(self.degraded_reason)
