import threading

from calibre.gui2.actions import InterfaceAction

from calibre_plugins.bindery_bridge.plugin.config import load_config
from calibre_plugins.bindery_bridge.plugin.server import BridgeServer


class BinderyBridgeAction(InterfaceAction):
    name = 'Bindery Bridge'
    action_spec = ('Bindery Bridge', None, 'Configure the Bindery Bridge HTTP API', None)

    def genesis(self):
        self._db_ready = True
        self._server = None
        self._start_lock = threading.Lock()
        self.qaction.triggered.connect(self.show_dialog)
        self._start_server()

    def _get_gui(self):
        return self.gui

    def _start_server(self):
        with self._start_lock:
            if self._server is not None:
                return
            cfg = load_config()
            self._server = BridgeServer()
            try:
                self._server.start(
                    port=int(cfg['port']),
                    bind_host=cfg['bind_host'],
                    api_key=cfg['api_key'],
                    get_db=self._get_db,
                    get_gui=self._get_gui,
                )
                self.gui.status_bar.show_message(
                    'Bindery Bridge listening on %s:%s' % (cfg['bind_host'], cfg['port']),
                    5000,
                )
            except Exception as exc:
                self._server = None
                self.gui.status_bar.show_message('Bindery Bridge failed to start: %s' % exc, 5000)

    def _get_db(self):
        if not getattr(self, '_db_ready', False):
            return None
        try:
            return self.gui.current_db
        except Exception:
            return None

    def library_changed(self, db):
        self._db_ready = False
        try:
            pass
        finally:
            self._db_ready = True

    def shutting_down(self):
        try:
            if self._server is not None:
                self._server.stop()
        except Exception:
            pass
        return True

    def show_dialog(self):
        from calibre.gui2 import show_restart_warning
        from PyQt5.Qt import QDialog, QDialogButtonBox, QVBoxLayout

        from calibre_plugins.bindery_bridge.plugin.config import ConfigWidget

        dlg = QDialog(self.gui)
        dlg.setWindowTitle('Bindery Bridge')
        layout = QVBoxLayout(dlg)
        widget = ConfigWidget()
        layout.addWidget(widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec_() == QDialog.Accepted:
            widget.commit()
            show_restart_warning('Restart Calibre for Bindery Bridge settings to take effect.')
