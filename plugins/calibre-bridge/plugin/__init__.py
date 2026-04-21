import contextlib
import threading

from calibre.gui2.actions import InterfaceAction


class BinderyBridgeAction(InterfaceAction):
    name = "Bindery Bridge"
    action_spec = ("Bindery Bridge", None, "Configure the Bindery Bridge HTTP API", None)

    def genesis(self):
        from calibre_plugins.bindery_bridge.plugin.config import load_config
        from calibre_plugins.bindery_bridge.plugin.server import BridgeServer

        self._BridgeServer = BridgeServer
        self._load_config = load_config
        self._server = None
        self._start_lock = threading.Lock()
        self.qaction.triggered.connect(self.show_dialog)
        self._start_server()

    def _start_server(self):
        with self._start_lock:
            if self._server is not None:
                return
            cfg = self._load_config()
            self._server = self._BridgeServer()
            try:
                self._server.start(
                    port=int(cfg["port"]),
                    bind_host=cfg["bind_host"],
                    api_key=cfg["api_key"],
                    get_db=self._get_db,
                )
                self.gui.status_bar.show_message(
                    f"Bindery Bridge listening on {cfg['bind_host']}:{cfg['port']}",
                    5000,
                )
            except Exception as exc:
                self._server = None
                self.gui.status_bar.show_message(f"Bindery Bridge failed to start: {exc}", 5000)

    def _restart_server(self):
        with self._start_lock:
            if self._server is not None:
                with contextlib.suppress(Exception):
                    self._server.stop()
                self._server = None
        self._start_server()

    def _get_db(self):
        try:
            return self.gui.current_db
        except Exception:
            return None

    def library_changed(self, db):
        pass

    def shutting_down(self):
        try:
            if self._server is not None:
                self._server.stop()
        except Exception:
            pass
        return True

    def show_dialog(self):
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
