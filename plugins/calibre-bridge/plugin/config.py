import contextlib
import logging
import os
import stat
from typing import Any

from calibre.utils.config import JSONConfig
from qt.core import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    Qt,
    QTimer,
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
    # Write everything the plugin logs, DEBUG included, to a rotating file in
    # Calibre's configuration folder. Off by default.
    "debug_log": False,
}

_log = logging.getLogger(__name__)

# Longest debug log failure reason the Status line shows.
MAX_PROBLEM_CHARS = 300

# How often the two status lines are read again while the window is open.
STATUS_REFRESH_MS = 3000

prefs = JSONConfig("plugins/bindery_bridge")
for k, v in DEFAULTS.items():
    prefs.defaults[k] = v


def _restrict_file_mode() -> None:
    """Make the settings file readable by its owner only (it holds the api_key).

    Calibre writes it 0644 inside its config directory, and rewrites it on
    every save, so this runs after every write. A symlink is left alone, not
    followed. A failure here, or a platform without POSIX modes, must never stop
    settings from loading or saving.
    """
    if os.name != "posix":
        return
    try:
        path = getattr(prefs, "file_path", None)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if not isinstance(path, str) or nofollow is None or not hasattr(os, "fchmod"):
            return
        try:
            fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return
        except OSError as exc:
            # ELOOP when the path is a symlink; not ours to chmod.
            _log.debug("settings file mode left as is (%s): %s", path, exc)
            return
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    # Best effort only; the config directory is already private to the user.
    except Exception as exc:  # nosec B110
        _log.debug("could not restrict the settings file mode: %s", exc)


def load_config() -> dict:
    _restrict_file_mode()
    return {k: prefs.get(k, v) for k, v in DEFAULTS.items()}


def save_value(key: str, value: object) -> None:
    """Persist one setting outside the dialog (the pull library id)."""
    prefs[key] = value
    _restrict_file_mode()


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


def _debuglog() -> Any:
    """The debug log module. Raises when it cannot be imported.

    Imported lazily for the same reason as the status module below, and every
    caller treats a failure as "no debug log" and carries on.
    """
    try:
        from calibre_plugins.bindery_bridge.plugin import debuglog
    except ImportError:  # the test suite loads the plugin files directly
        import debuglog  # type: ignore[no-redef]
    return debuglog


def _debug_log_path() -> str:
    try:
        return str(_debuglog().log_path())
    except Exception:
        return ""


def _debug_log_error() -> str:
    try:
        return str(_debuglog().last_error())
    except Exception:
        return ""


def apply_debug_log() -> str:
    """Turn the debug log on or off to match the saved setting. Never raises.

    Called when the plugin starts and after every save. Returns "" or the
    reason the log could not be opened, which the Status line then shows.
    """
    try:
        return str(
            _debuglog().apply(
                bool(prefs.get("debug_log", DEFAULTS["debug_log"])),
                secrets=[str(prefs.get("api_key", "") or "")],
            )
        )
    except Exception as exc:
        return str(exc) or type(exc).__name__


def _local_file_opener() -> Any:
    """Calibre's helper for showing a folder in the file manager, or None."""
    try:
        from calibre.gui2 import open_local_file
    except Exception:
        return None
    return open_local_file


def _status_summary() -> str:
    """One line describing what the bridge server is doing right now.

    Imported lazily: Calibre can open this dialog from Preferences before the
    interface action's genesis has ever run, and a config dialog must not fail
    to open because the server module is not loaded yet.
    """
    try:
        from calibre_plugins.bindery_bridge.plugin import status

        summary = str(status.summary())
    except Exception:
        summary = "Not running"
    problem = _debug_log_error()
    if problem:
        problem = " ".join(problem.split())[:MAX_PROBLEM_CHARS]
        summary = f"{summary.rstrip('.')}. The debug log could not be opened: {problem}"
    return summary


def _plain_label(parent: QWidget, text: str) -> QLabel:
    """A word wrapped label that shows ``text`` exactly as it is.

    The status lines can quote an error that came from a server. A QLabel
    guesses rich text from the content by default, so markup in such an error
    would be rendered. The format is set before the text so that guess is
    never made, not even for the first value.
    """
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setText(text)
    return label


class ConfigWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Size the dialog from the font, not from pixels, so it scales with the
        # display (125% and up on a 4K monitor) and the fields have room.
        self.setMinimumWidth(self.fontMetrics().horizontalAdvance("x") * 64)
        layout = QFormLayout(self)
        # Set the growth policy ourselves. Left to the style, a form on Windows
        # keeps every field at its size hint, so resizing the dialog moved the
        # window edge and nothing inside it, and the word wrapped status lines
        # were squeezed into a narrow column and clipped.
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # A persistent report of what the server is actually doing. The start
        # failure used to be a five second status bar toast and nothing else,
        # so on a headless install nobody ever saw it and Bindery only got
        # connection refused. This line survives until the next start attempt.
        self.status_label = _plain_label(self, _status_summary())
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

        self.pull_status_label = _plain_label(self, _pull_status_summary())
        layout.addRow("Pull status:", self.pull_status_label)

        # Troubleshooting. Without the file only warnings are visible, and
        # only to someone running calibre-debug -g.
        self.debug_log_input = QCheckBox("Write debug log", self)
        self.debug_log_input.setChecked(bool(prefs.get("debug_log", DEFAULTS["debug_log"])))
        layout.addRow("Debug log:", self.debug_log_input)

        log_row = QHBoxLayout()
        self.debug_log_path_label = _plain_label(self, _debug_log_path() or "Not available")
        self.debug_log_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        log_row.addWidget(self.debug_log_path_label, 1)
        self._open_local_file = _local_file_opener()
        if self._open_local_file is not None:
            open_btn = QPushButton("Open log folder", self)
            open_btn.clicked.connect(self._open_log_folder)
            log_row.addWidget(open_btn)
        layout.addRow("Log file:", log_row)

        # The status lines used to be read once, when the window opened, so a
        # pull that recovered a moment later still read as failed. The timer
        # runs only while the window is on screen: Calibre can keep a closed
        # settings dialog alive for as long as its main window.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(STATUS_REFRESH_MS)
        self._refresh_timer.timeout.connect(self.refresh_status)

    def refresh_status(self) -> None:
        """Read both status lines again. Never raises into the Qt event loop."""
        with contextlib.suppress(Exception):
            self.status_label.setText(_status_summary())
            self.pull_status_label.setText(_pull_status_summary())

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.refresh_status()
        self._refresh_timer.start()

    def hideEvent(self, event: Any) -> None:
        self._refresh_timer.stop()
        super().hideEvent(event)

    def _open_log_folder(self) -> None:
        opener = self._open_local_file
        path = _debug_log_path()
        if opener is None or not path:
            return
        # A file manager that will not start is not worth an error dialog.
        with contextlib.suppress(Exception):
            opener(os.path.dirname(path))

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
        prefs["debug_log"] = bool(self.debug_log_input.isChecked())
        _restrict_file_mode()
        # After the API key is saved, so the log redacts the new one.
        apply_debug_log()
        _notify_saved()
