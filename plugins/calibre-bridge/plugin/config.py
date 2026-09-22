import os

from calibre.utils.config import JSONConfig
from qt.core import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

DEFAULTS = {
    "port": 8099,
    "bind_host": "0.0.0.0",  # nosec B104 — user-configurable default, not a hardcoded binding
    "api_key": "",
    # Optional ingest-root restriction. When non-empty, only files whose
    # resolved real path lives inside this directory may be added. Empty
    # (the default) preserves the historical behaviour of no root restriction.
    "ingest_root": "",
    # Upper bound on request body size to avoid a remote OOM via a large
    # Content-Length. 64 MiB is comfortably above any metadata payload.
    "max_body_bytes": 64 * 1024 * 1024,
}

prefs = JSONConfig("plugins/bindery_bridge")
for k, v in DEFAULTS.items():
    prefs.defaults[k] = v


def load_config() -> dict:
    return {k: prefs.get(k, v) for k, v in DEFAULTS.items()}


def _status_summary() -> str:
    """One line describing what the bridge server is doing right now.

    Imported lazily: Calibre can open this dialog from Preferences before the
    interface action's genesis has ever run, and a config dialog must not fail
    to open because the server module is not loaded yet.
    """
    try:
        from calibre_plugins.bindery_bridge.plugin import status

        return str(status.summary())
    except Exception:
        return "Not running"


class ConfigWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)

        # A persistent report of what the server is actually doing. The start
        # failure used to be a five second status bar toast and nothing else,
        # so on a headless install nobody ever saw it and Bindery only got
        # connection refused. This line survives until the next start attempt.
        self.status_label = QLabel(_status_summary(), self)
        self.status_label.setWordWrap(True)
        layout.addRow("Status:", self.status_label)

        self.port_input = QSpinBox(self)
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(int(prefs.get("port", DEFAULTS["port"])))
        layout.addRow("Listen port:", self.port_input)

        self.bind_host_input = QLineEdit(str(prefs.get("bind_host", DEFAULTS["bind_host"])), self)
        layout.addRow("Bind host:", self.bind_host_input)

        self.ingest_root_input = QLineEdit(
            str(prefs.get("ingest_root", DEFAULTS["ingest_root"])), self
        )
        self.ingest_root_input.setPlaceholderText("Leave empty to allow any path")
        layout.addRow("Ingest root:", self.ingest_root_input)

        key_row = QHBoxLayout()
        self.api_key_input = QLineEdit(str(prefs.get("api_key", DEFAULTS["api_key"])), self)
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.api_key_input)

        self._show_btn = QPushButton("Show", self)
        self._show_btn.setCheckable(True)
        self._show_btn.setFixedWidth(50)
        self._show_btn.toggled.connect(self._toggle_visibility)
        key_row.addWidget(self._show_btn)

        gen_btn = QPushButton("Generate", self)
        gen_btn.clicked.connect(self._generate_key)
        key_row.addWidget(gen_btn)

        layout.addRow("API key:", key_row)

    def _toggle_visibility(self, checked: bool) -> None:
        if checked:
            self.api_key_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_btn.setText("Hide")
        else:
            self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_btn.setText("Show")

    def _generate_key(self) -> None:
        self.api_key_input.setText(os.urandom(32).hex())
        self._show_btn.setChecked(True)

    def commit(self) -> None:
        prefs["port"] = int(self.port_input.value())
        prefs["bind_host"] = self.bind_host_input.text().strip() or DEFAULTS["bind_host"]
        prefs["ingest_root"] = self.ingest_root_input.text().strip()
        prefs["api_key"] = self.api_key_input.text().strip()
