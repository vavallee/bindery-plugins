import logging
import threading
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from typing import Any

from calibre_plugins.bindery_bridge.plugin import status
from calibre_plugins.bindery_bridge.plugin.handlers import (
    DEFAULT_BODY_TIMEOUT,
    DEFAULT_HEADER_TIMEOUT,
    SERVER_NAME,
    _throttle_key,
    make_degraded_handler,
    make_handler,
)

_log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# A key shorter than this is refused on a network bind. The Generate button in
# the settings window makes 64 hex characters.
MIN_NETWORK_KEY_LENGTH = 16

# Connections handled at once. Over the cap a new connection gets a 503 and is
# closed, never queued, so stalled sockets cannot pin threads without limit.
DEFAULT_MAX_CONNECTIONS = 32

# Connections one peer address may hold at once, inside the cap above, so a
# single source cannot take every slot. Keyed like the failure throttle: an IPv6
# peer by its /64, an IPv4 mapped one as the IPv4 address. Bindery opens its
# requests one after another, so 8 is well above what it needs, including when
# every container on a Docker bridge arrives from one address.
DEFAULT_MAX_CONNECTIONS_PER_ADDRESS = 8

_BUSY_BODY = b'{"error": "too many connections", "code": "busy"}'
_BUSY_RESPONSE = (
    b"HTTP/1.0 503 Service Unavailable\r\n"
    b"Server: " + SERVER_NAME.encode() + b"\r\n"
    b"Content-Type: application/json\r\n"
    b"Retry-After: 2\r\n"
    b"Connection: close\r\n"
    b"Content-Length: " + str(len(_BUSY_BODY)).encode() + b"\r\n"
    b"\r\n" + _BUSY_BODY
)


class _BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with daemon threads and caps on connections in flight.

    There is one cap for the server and one for each peer address. Over either,
    the connection gets a 503 and is closed, never queued.
    """

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type,
        max_connections: int,
        max_per_address: int = DEFAULT_MAX_CONNECTIONS_PER_ADDRESS,
    ) -> None:
        self._slots = threading.BoundedSemaphore(max_connections)
        self._max_per_address = max_per_address
        self._per_address: dict[str, int] = {}
        self._per_address_lock = threading.Lock()
        super().__init__(address, handler)

    @staticmethod
    def _peer_key(client_address: Any) -> str:
        try:
            return str(_throttle_key(str(client_address[0])))
        except (TypeError, IndexError, KeyError):
            return str(client_address)

    def _take_address(self, key: str) -> bool:
        with self._per_address_lock:
            count = self._per_address.get(key, 0)
            if count >= self._max_per_address:
                return False
            self._per_address[key] = count + 1
            return True

    def _drop_address(self, key: str) -> None:
        with self._per_address_lock:
            count = self._per_address.get(key, 0) - 1
            if count > 0:
                self._per_address[key] = count
            else:
                self._per_address.pop(key, None)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self._refuse(request)
            return
        key = self._peer_key(client_address)
        if not self._take_address(key):
            self._slots.release()
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._drop_address(key)
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._drop_address(self._peer_key(client_address))
            self._slots.release()

    def _refuse(self, request: Any) -> None:
        try:
            request.settimeout(1)
            request.sendall(_BUSY_RESPONSE)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)


def _is_loopback(bind_host: str) -> bool:
    return bind_host.strip().lower() in _LOOPBACK_HOSTS


class BridgeServer:
    def __init__(
        self,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_per_address: int = DEFAULT_MAX_CONNECTIONS_PER_ADDRESS,
    ) -> None:
        self._max_connections = max_connections
        self._max_per_address = max_per_address
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
        on_added: Callable[[int], Any] | None = None,
        on_updated: Callable[[int], Any] | None = None,
        header_timeout: float = DEFAULT_HEADER_TIMEOUT,
        body_timeout: float = DEFAULT_BODY_TIMEOUT,
    ) -> None:
        # Fail closed on network exposure: a non-loopback bind with no API key
        # would expose the unauthenticated add endpoint to the network, and a
        # short key is guessable over the network. Refuse to start rather than
        # silently listening. Loopback binds may use no key or a short one.
        if not api_key and not _is_loopback(bind_host):
            raise ValueError(
                f"refusing to bind to non-loopback host {bind_host!r} without an api_key. "
                "Set an api_key in the Bindery Bridge config (press Generate), or bind to "
                "127.0.0.1. A bridge with no api_key is for local use only, and setting a key is "
                "recommended either way."
            )
        if api_key and len(api_key) < MIN_NETWORK_KEY_LENGTH and not _is_loopback(bind_host):
            raise ValueError(
                f"refusing to bind to non-loopback host {bind_host!r} with an api_key shorter "
                f"than {MIN_NETWORK_KEY_LENGTH} characters. Press Generate in the Bindery "
                "Bridge config for a strong key, or bind to 127.0.0.1"
            )
        handler_cls = make_handler(
            api_key=api_key,
            get_db=get_db,
            get_gui=get_gui,
            ingest_root=ingest_root,
            max_body_bytes=max_body_bytes,
            on_added=on_added,
            on_updated=on_updated,
            header_timeout=header_timeout,
            body_timeout=body_timeout,
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
        self._httpd = _BoundedHTTPServer(
            (bind_host, port), handler_cls, self._max_connections, self._max_per_address
        )
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
