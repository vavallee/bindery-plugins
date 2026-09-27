import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from calibre.gui2.actions import InterfaceAction

_log = logging.getLogger(__name__)


def _gui_thread_dispatcher(fn: Callable[[int], None]) -> Callable[[int], None] | None:
    """Wrap ``fn`` so a call from any thread runs it on the GUI thread.

    Must be called on the GUI thread. ``calibre.gui2.Dispatcher`` is a QObject
    whose call emits a queued signal, so the slot runs on the thread that
    created it. A ``QTimer.singleShot`` from the bridge's HTTP thread, which
    is what 0.6.2 and earlier used, queues onto that thread's event loop, and
    it has none, so the refresh never ran. Returns None when calibre.gui2 is
    not importable (the test stubs), which leaves the adder on its fallback.
    """
    try:
        from calibre.gui2 import Dispatcher
    except Exception as exc:
        _log.debug("calibre.gui2.Dispatcher unavailable, no GUI refresh: %s", exc)
        return None
    return Dispatcher(fn)  # type: ignore[no-any-return]


class BinderyBridgeAction(InterfaceAction):
    name = "Bindery Bridge"
    action_spec = ("Bindery Bridge", None, "Configure the Bindery Bridge HTTP API", None)

    def genesis(self) -> None:
        from calibre_plugins.bindery_bridge.plugin import status
        from calibre_plugins.bindery_bridge.plugin.config import load_config
        from calibre_plugins.bindery_bridge.plugin.server import BridgeServer

        self._BridgeServer = BridgeServer
        self._load_config = load_config
        self._status = status
        self._server = None
        self._start_lock = threading.Lock()
        # Built here because genesis runs on the GUI thread, which is the
        # thread the Dispatcher delivers to. Held on self so it is not
        # collected while the server still calls it.
        self._on_added = _gui_thread_dispatcher(self._refresh_gui)
        self.qaction.triggered.connect(self.show_dialog)
        self._start_server()

    def _get_gui(self) -> Any:
        return self.gui

    def _refresh_gui(self, count: int) -> None:
        """Show ``count`` newly added books. Runs on the GUI thread."""
        try:
            self.gui.library_view.model().books_added(count)
            self.gui.tags_view.recount()
        except Exception as exc:
            _log.debug("Calibre GUI refresh failed: %s", exc)

    def _start_server(self) -> None:
        _log.debug("_start_server called")
        with self._start_lock:
            if self._server is not None:
                return
            cfg = self._load_config()
            server = self._BridgeServer()
            self._server = server
            try:
                server.start(
                    port=int(cfg["port"]),
                    bind_host=cfg["bind_host"],
                    api_key=cfg["api_key"],
                    get_db=self._get_db,
                    get_gui=self._get_gui,
                    ingest_root=cfg.get("ingest_root", ""),
                    max_body_bytes=int(cfg.get("max_body_bytes", 64 * 1024 * 1024)),
                    on_added=self._on_added,
                )
                self.gui.status_bar.show_message(
                    f"Bindery Bridge listening on {cfg['bind_host']}:{cfg['port']}",
                    5000,
                )
            except Exception as exc:
                _log.error("calibre-bridge failed to start: %s", exc)
                self._server = None
                self.gui.status_bar.show_message(f"Bindery Bridge failed to start: {exc}", 5000)
                self._start_degraded(server, cfg, str(exc))

    def _start_degraded(self, server: Any, cfg: dict, reason: str) -> None:
        """Keep the port answering so the failure is discoverable.

        A five second status bar toast is invisible on a headless or KasmVNC
        deployment, and a closed port tells Bindery only "connection refused".
        The degraded server explains itself over the same port, and the
        config dialog reads the same state out of ``status``.
        """
        self._status.set_degraded(reason)
        try:
            server.start_degraded(port=int(cfg["port"]), bind_host=cfg["bind_host"], reason=reason)
        except Exception as degraded_exc:
            _log.error("calibre-bridge degraded server also failed: %s", degraded_exc)
            self._status.set_degraded(reason)
            return
        self._server = server

    def _restart_server(self) -> None:
        with self._start_lock:
            if self._server is not None:
                with contextlib.suppress(Exception):
                    self._server.stop()
                self._server = None
        self._start_server()

    def _get_db(self) -> Any | None:
        try:
            return self.gui.current_db
        except Exception:
            return None

    def library_changed(self, db: Any) -> None:
        pass

    def shutting_down(self) -> bool:
        _log.info("calibre-bridge shutting down")
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.stop()
        return True

    def show_dialog(self) -> None:
        from calibre_plugins.bindery_bridge.plugin.config import ConfigWidget
        from qt.core import QDialog, QDialogButtonBox, QVBoxLayout

        dlg = QDialog(self.gui)
        dlg.setWindowTitle("Bindery Bridge")
        layout = QVBoxLayout(dlg)
        widget = ConfigWidget()
        layout.addWidget(widget)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec_() == QDialog.DialogCode.Accepted:
            widget.commit()
            self._restart_server()
