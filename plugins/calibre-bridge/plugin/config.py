import os

from calibre.utils.config import JSONConfig
from qt.core import QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QWidget

DEFAULTS = {
    'port': 8099,
    'bind_host': '0.0.0.0',
    'api_key': '',
}

prefs = JSONConfig('plugins/bindery_bridge')
for k, v in DEFAULTS.items():
    prefs.defaults[k] = v


def load_config() -> dict:
    return {k: prefs.get(k, v) for k, v in DEFAULTS.items()}


class ConfigWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)

        self.port_input = QSpinBox(self)
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(int(prefs.get('port', DEFAULTS['port'])))
        layout.addRow('Listen port:', self.port_input)

        self.bind_host_input = QLineEdit(str(prefs.get('bind_host', DEFAULTS['bind_host'])), self)
        layout.addRow('Bind host:', self.bind_host_input)

        key_row = QHBoxLayout()
        self.api_key_input = QLineEdit(str(prefs.get('api_key', DEFAULTS['api_key'])), self)
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.api_key_input)

        self._show_btn = QPushButton('Show', self)
        self._show_btn.setCheckable(True)
        self._show_btn.setFixedWidth(50)
        self._show_btn.toggled.connect(self._toggle_visibility)
        key_row.addWidget(self._show_btn)

        gen_btn = QPushButton('Generate', self)
        gen_btn.clicked.connect(self._generate_key)
        key_row.addWidget(gen_btn)

        layout.addRow('API key:', key_row)

    def _toggle_visibility(self, checked: bool) -> None:
        if checked:
            self.api_key_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_btn.setText('Hide')
        else:
            self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_btn.setText('Show')

    def _generate_key(self) -> None:
        self.api_key_input.setText(os.urandom(32).hex())
        self._show_btn.setChecked(True)

    def commit(self):
        prefs['port'] = int(self.port_input.value())
        prefs['bind_host'] = self.bind_host_input.text().strip() or DEFAULTS['bind_host']
        prefs['api_key'] = self.api_key_input.text().strip()
