import logging
import threading
from http.server import ThreadingHTTPServer

from calibre_plugins.bindery_bridge.plugin.handlers import make_handler

_log = logging.getLogger(__name__)


class BridgeServer:
    def __init__(self):
        self._httpd = None
        self._thread = None

    def start(self, port: int, bind_host: str, api_key: str, get_db, get_gui=None):
        handler_cls = make_handler(api_key=api_key, get_db=get_db, get_gui=get_gui)
        self._httpd = ThreadingHTTPServer((bind_host, port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="bindery-bridge-http",
            daemon=True,
        )
        self._thread.start()
        _log.info("calibre-bridge listening on %s:%d", bind_host, port)

    def stop(self):
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
