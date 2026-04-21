import threading
from http.server import ThreadingHTTPServer

from calibre_plugins.bindery_bridge.plugin.handlers import make_handler


class BridgeServer:
    def __init__(self):
        self._httpd = None
        self._thread = None

    def start(self, port: int, bind_host: str, api_key: str, get_db):
        handler_cls = make_handler(api_key=api_key, get_db=get_db)
        self._httpd = ThreadingHTTPServer((bind_host, port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="bindery-bridge-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            finally:
                self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
