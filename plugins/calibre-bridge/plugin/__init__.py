import contextlib
import logging
import threading
from typing import Any

from calibre.gui2.actions import InterfaceAction

_log = logging.getLogger(__name__)


class BinderyBridgeAction(InterfaceAction):
    name = "Bindery Bridge"
    action_spec = ("Bindery Bridge", None, "Configure the Bindery Bridge HTTP API", None)

    def genesis(self) -> None:
        from calibre_plugins.bindery_bridge.plugin.config import load_config
        from calibre_plugins.bindery_bridge.plugin.server import BridgeServer

        self._BridgeServer = BridgeServer
        self._load_config = load_config
        self._server = None
        self._start_lock = threading.Lock()
        self.qaction.triggered.connect(self.show_dialog)
        self._start_server()

    def _get_gui(self) -> Any:
        return self.gui

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
                )
                self.gui.status_bar.show_message(
                    f"Bindery Bridge listening on {cfg['bind_host']}:{cfg['port']}",
                    5000,
                )
            except Exception as exc:
                _log.error("calibre-bridge failed to start: %s", exc)
                self._server = None
                self.gui.status_bar.show_message(f"Bindery Bridge failed to start: {exc}", 5000)

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
