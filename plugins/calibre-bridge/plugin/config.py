import os

from calibre.utils.config import JSONConfig
from qt.core import (
    QCheckBox,
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
    # Pull mode (0.8.0): the plugin connects out to Bindery and fetches queued
    # books, so Calibre needs no shared drive and no inbound port. Off by
    # default; the push server above keeps running either way.
    "pull_enabled": False,
    # Bindery's own address, including any URL base, e.g.
    # https://bindery.example.net or http://192.168.1.20:8787/bindery
    "bindery_url": "",
    # Optional PEM bundle trusted on top of the system store, for a Bindery
    # behind a private CA or a self signed certificate. There is no setting
    # that turns certificate checks off.
    "ca_file": "",
    "pull_interval_seconds": 60,
    # Hard cap on one downloaded book.
    "max_download_bytes": 1024 * 1024 * 1024,
    # Calibre library id recorded when pull is turned on. Pull pauses while
    # any other library is open, so books never land in the wrong one.
    "pull_library_id": "",
}

prefs = JSONConfig("plugins/bindery_bridge")
for k, v in DEFAULTS.items():
    prefs.defaults[k] = v


def load_config() -> dict:
    return {k: prefs.get(k, v) for k, v in DEFAULTS.items()}


def save_value(key: str, value: object) -> None:
    """Persist one setting outside the dialog (the pull library id)."""
    prefs[key] = value


def _pull_status_summary() -> str:
    try:
        from calibre_plugins.bindery_bridge.plugin import status

        return str(status.pull_summary())
    except Exception:
        return "Pull from Bindery is off"


def _notify_saved() -> None:
    """Let a running plugin pick up the new settings without a restart."""
    try:
        from calibre_plugins.bindery_bridge.plugin import status

        status.config_saved()
    # Nothing is running yet, so there is nothing to tell.
    except Exception:  # nosec B110
        pass


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

        # Pull mode. The API key above authenticates both directions.
        layout.addRow(QLabel("<b>Pull from Bindery</b>", self))
        self.pull_enabled_input = QCheckBox("Fetch books from Bindery (pull mode)", self)
        self.pull_enabled_input.setChecked(
            bool(prefs.get("pull_enabled", DEFAULTS["pull_enabled"]))
        )
        layout.addRow("Pull mode:", self.pull_enabled_input)

        self.bindery_url_input = QLineEdit(
            str(prefs.get("bindery_url", DEFAULTS["bindery_url"])), self
        )
        self.bindery_url_input.setPlaceholderText("https://bindery.example.net")
        layout.addRow("Bindery URL:", self.bindery_url_input)

        self.ca_file_input = QLineEdit(str(prefs.get("ca_file", DEFAULTS["ca_file"])), self)
        self.ca_file_input.setPlaceholderText("Optional PEM file for a private CA")
        layout.addRow("CA file:", self.ca_file_input)

        self.pull_status_label = QLabel(_pull_status_summary(), self)
        self.pull_status_label.setWordWrap(True)
        layout.addRow("Pull status:", self.pull_status_label)

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
        pull_enabled = bool(self.pull_enabled_input.isChecked())
        if pull_enabled and not bool(prefs.get("pull_enabled", False)):
            # Turned on just now: forget any earlier library so the plugin
            # binds pull to the library that is open at this moment.
            prefs["pull_library_id"] = ""
        prefs["pull_enabled"] = pull_enabled
        prefs["bindery_url"] = self.bindery_url_input.text().strip()
        prefs["ca_file"] = self.ca_file_input.text().strip()
        _notify_saved()
