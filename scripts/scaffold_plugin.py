#!/usr/bin/env python3
"""Scaffold a new Calibre plugin skeleton under plugins/<name>.

Usage:
    python scripts/scaffold_plugin.py kobo-bridge
    python scripts/scaffold_plugin.py kobo-bridge --port 8100
"""

from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Templates use %NAME% markers instead of {name} to avoid brace-escaping issues.
#
# Every template is self contained. build_plugin.py zips only the plugin's own
# directory, so anything a generated plugin imports from the repo root would be
# missing at runtime and the plugin would fail to load inside Calibre.

PLUGIN_INIT = """\
from calibre.customize import InterfaceActionBase


class %CLASS_NAME%(InterfaceActionBase):
    name = "%DISPLAY_NAME%"
    description = "TODO: describe your plugin"
    supported_platforms = ["windows", "osx", "linux"]
    author = "vavallee"
    version = (0, 1, 0)
    minimum_calibre_version = (6, 0, 0)

    actual_plugin = "calibre_plugins.%MODULE_NAME%.plugin:%ACTION_CLASS%"

    def is_customizable(self) -> bool:
        return True

    def config_widget(self):
        from calibre_plugins.%MODULE_NAME%.plugin.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget) -> None:
        config_widget.commit()
"""

PLUGIN_ACTION = """\
import contextlib
import logging
import threading

from calibre.gui2.actions import InterfaceAction

_log = logging.getLogger(__name__)


class %ACTION_CLASS%(InterfaceAction):
    name = "%DISPLAY_NAME%"
    action_spec = ("%DISPLAY_NAME%", None, "Configure %DISPLAY_NAME%", None)

    def genesis(self) -> None:
        from calibre_plugins.%MODULE_NAME%.plugin.config import load_config
        from calibre_plugins.%MODULE_NAME%.plugin.handlers import make_handler
        from calibre_plugins.%MODULE_NAME%.plugin.server import PluginServer

        self._server = PluginServer()
        self._start_lock = threading.Lock()
        self._make_handler = make_handler
        self._load_config = load_config
        self.qaction.triggered.connect(self.show_dialog)
        self._start_server()

    def _get_db(self):
        try:
            return self.gui.current_db
        except Exception:
            return None

    def _start_server(self) -> None:
        _log.debug("_start_server called")
        with self._start_lock:
            if self._server.is_running:
                return
            cfg = self._load_config()
            handler_cls = self._make_handler(api_key=cfg["api_key"], get_db=self._get_db)
            try:
                self._server.start(
                    port=int(cfg["port"]),
                    bind_host=cfg["bind_host"],
                    handler_cls=handler_cls,
                    thread_name="%MODULE_NAME%-http",
                )
                self.gui.status_bar.show_message(
                    f"%DISPLAY_NAME% listening on {cfg['bind_host']}:{cfg['port']}",
                    5000,
                )
            except Exception as exc:
                _log.error("%MODULE_NAME% failed to start: %s", exc)
                self.gui.status_bar.show_message(f"%DISPLAY_NAME% failed to start: {exc}", 5000)

    def shutting_down(self) -> bool:
        _log.info("%MODULE_NAME% shutting down")
        with contextlib.suppress(Exception):
            self._server.stop()
        return True

    def show_dialog(self) -> None:
        from calibre_plugins.%MODULE_NAME%.plugin.config import ConfigWidget
        from qt.core import QDialog, QDialogButtonBox, QVBoxLayout

        dlg = QDialog(self.gui)
        dlg.setWindowTitle("%DISPLAY_NAME%")
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
            self._server.stop()
            self._start_server()
"""

PLUGIN_SERVER = """\
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_log = logging.getLogger(__name__)


class PluginServer:
    \"\"\"Runs a BaseHTTPRequestHandler subclass in a daemon thread.\"\"\"

    def __init__(self) -> None:
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(
        self,
        port: int,
        bind_host: str,
        handler_cls: type[BaseHTTPRequestHandler],
        thread_name: str = "plugin-http",
    ) -> None:
        self._httpd = ThreadingHTTPServer((bind_host, port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name=thread_name, daemon=True
        )
        self._thread.start()
        _log.info("%s listening on %s:%d", thread_name, bind_host, port)

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception as exc:
                _log.error("error stopping plugin server: %s", exc)
            finally:
                self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._httpd is not None
"""

PLUGIN_HANDLERS = """\
import hmac
import json
import logging
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from typing import Any

_log = logging.getLogger(__name__)

PLUGIN_VERSION = "0.1.0"


def check_bearer(headers: Any, api_key: str) -> bool:
    \"\"\"Constant time bearer check. Empty api_key means auth is disabled.

    compare_digest rather than ==, because == on str returns as soon as two
    bytes differ and that timing tells a caller how much of the key it guessed.
    \"\"\"
    if not api_key:
        return True
    header: str = headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    token = header[len("Bearer ") :].strip()
    return hmac.compare_digest(token.encode("utf-8"), api_key.encode("utf-8"))


def make_handler(api_key: str, get_db: Callable[[], Any]) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "%MODULE_NAME%/" + PLUGIN_VERSION

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            _log.debug(format, *args)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/health":
                self._send_json(200, {"version": PLUGIN_VERSION})
                return
            self._send_json(404, {"error": "not found", "code": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not check_bearer(self.headers, api_key):
                self._send_json(401, {"error": "unauthorized", "code": "unauthorized"})
                return
            db = get_db()
            if db is None:
                self._send_json(
                    503, {"error": "library not ready", "code": "db_unavailable"}
                )
                return
            # TODO: implement your endpoint logic here
            self._send_json(400, {"error": "not implemented", "code": "invalid_metadata"})

    return Handler
"""

PLUGIN_CONFIG = """\
import os
from typing import Any

from calibre.utils.config import JSONConfig
from qt.core import QFormLayout, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QWidget

DEFAULTS: dict[str, Any] = {
    "port": %PORT%,
    "bind_host": "0.0.0.0",  # nosec B104
    "api_key": "",
}

prefs = JSONConfig("plugins/%MODULE_NAME%")
for k, v in DEFAULTS.items():
    prefs.defaults[k] = v


def load_config() -> dict[str, Any]:
    return {k: prefs.get(k, v) for k, v in DEFAULTS.items()}


class ConfigWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)

        self.port_input = QSpinBox(self)
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(int(prefs.get("port", DEFAULTS["port"])))
        layout.addRow("Listen port:", self.port_input)

        self.bind_host_input = QLineEdit(str(prefs.get("bind_host", DEFAULTS["bind_host"])), self)
        layout.addRow("Bind host:", self.bind_host_input)

        key_row = QHBoxLayout()
        self.api_key_input = QLineEdit(str(prefs.get("api_key", DEFAULTS["api_key"])), self)
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.api_key_input)
        gen_btn = QPushButton("Generate", self)
        gen_btn.clicked.connect(self._generate_key)
        key_row.addWidget(gen_btn)
        layout.addRow("API key:", key_row)

    def _generate_key(self) -> None:
        self.api_key_input.setText(os.urandom(32).hex())

    def commit(self) -> None:
        \"\"\"Called by the Calibre plugin framework when the user clicks OK.\"\"\"
        prefs["port"] = int(self.port_input.value())
        prefs["bind_host"] = self.bind_host_input.text().strip() or DEFAULTS["bind_host"]
        prefs["api_key"] = self.api_key_input.text().strip()
"""

ROOT_CONFTEST = """\
\"\"\"conftest.py — stub calibre/Qt before pytest collects the plugin package.\"\"\"

import sys
import types


def _module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


class _JSONConfig(dict):
    def __init__(self, name):
        super().__init__()
        self.defaults = {}

    def get(self, key, default=None):
        return super().get(key, self.defaults.get(key, default))


_module("calibre")
_module("calibre.customize").InterfaceActionBase = object
_module("calibre.gui2")
_module("calibre.gui2.actions").InterfaceAction = object
_module("calibre.utils")
_module("calibre.utils.config").JSONConfig = _JSONConfig
_module("calibre.constants").numeric_version = (9, 0, 0)

_module("qt")
_qt_core = _module("qt.core")
for _name in (
    "QDialog",
    "QDialogButtonBox",
    "QFormLayout",
    "QHBoxLayout",
    "QLabel",
    "QLineEdit",
    "QPushButton",
    "QSpinBox",
    "QVBoxLayout",
    "QWidget",
):
    setattr(_qt_core, _name, object)
"""

CONFTEST = """\
\"\"\"conftest.py — auto-loaded by pytest; provides the calibre_stubs fixture.\"\"\"

import pytest


@pytest.fixture
def calibre_stubs():
    \"\"\"The root conftest already installed the stubs; this is the handle.\"\"\"
    import sys

    return {name: mod for name, mod in sys.modules.items() if name.startswith("calibre")}
"""

TEST_HANDLERS = """\
\"\"\"Basic handler tests for %DISPLAY_NAME% plugin.\"\"\"
import pathlib
import sys

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin"


@pytest.fixture(autouse=True)
def _load_handlers(calibre_stubs):
    sys.path.insert(0, str(_PLUGIN_DIR))
    sys.modules.pop("handlers", None)
    yield
    sys.path.pop(0)
    sys.modules.pop("handlers", None)


def test_make_handler_returns_class(calibre_stubs):
    import importlib
    from unittest.mock import MagicMock
    handlers = importlib.import_module("handlers")
    db = MagicMock()
    cls = handlers.make_handler(api_key="", get_db=lambda: db)
    assert cls is not None
"""

README_PLUGIN = """\
# %DISPLAY_NAME%

TODO: describe what this plugin does.

## Configuration

| Field     | Default     | Description                    |
|-----------|-------------|--------------------------------|
| port      | %PORT%      | TCP port to listen on          |
| bind_host | `0.0.0.0`   | Interface to bind to           |
| api_key   | *(empty)*   | Bearer token (empty = no auth) |

## API

### `GET /v1/health`

Returns `200 OK` with `{"version": "<version>"}`.
"""


def _to_class_name(slug: str) -> str:
    return "".join(part.title() for part in slug.replace("-", "_").split("_"))


def _to_module_name(slug: str) -> str:
    return slug.replace("-", "_")


def _render(template: str, ctx: dict) -> str:
    result = template
    for key, value in ctx.items():
        result = result.replace(f"%{key}%", str(value))
    return result


def scaffold(slug: str, port: int) -> None:
    display_name = " ".join(p.title() for p in slug.replace("-", " ").split())
    class_name = _to_class_name(slug)
    module_name = _to_module_name(slug)
    action_class = f"{class_name}Action"

    ctx = {
        "DISPLAY_NAME": display_name,
        "CLASS_NAME": class_name,
        "MODULE_NAME": module_name,
        "ACTION_CLASS": action_class,
        "PORT": str(port),
    }

    base = REPO_ROOT / "plugins" / slug
    plugin_dir = base / "plugin"
    tests_dir = base / "tests"

    for d in (plugin_dir, tests_dir):
        d.mkdir(parents=True, exist_ok=True)

    files = {
        base / "__init__.py": _render(PLUGIN_INIT, ctx),
        base / "conftest.py": _render(ROOT_CONFTEST, ctx),
        plugin_dir / "__init__.py": f'"""Calibre plugin: {display_name}."""\n',
        plugin_dir / "action.py": _render(PLUGIN_ACTION, ctx),
        plugin_dir / "handlers.py": _render(PLUGIN_HANDLERS, ctx),
        plugin_dir / "server.py": _render(PLUGIN_SERVER, ctx),
        plugin_dir / "config.py": _render(PLUGIN_CONFIG, ctx),
        tests_dir / "__init__.py": "",
        tests_dir / "conftest.py": _render(CONFTEST, ctx),
        tests_dir / "test_handlers.py": _render(TEST_HANDLERS, ctx),
        base / "README.md": _render(README_PLUGIN, ctx),
    }

    written = []
    for path, content in files.items():
        if path.exists():
            print(f"  skip (exists): {path.relative_to(REPO_ROOT)}", file=sys.stderr)
            continue
        path.write_text(content)
        written.append(path.relative_to(REPO_ROOT))

    print(f"Scaffolded plugin '{slug}' ({len(written)} files):")
    for p in sorted(written):
        print(f"  {p}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="Plugin slug, e.g. kobo-bridge")
    parser.add_argument(
        "--port", type=int, default=8100, help="Default listen port (default: 8100)"
    )
    args = parser.parse_args()
    scaffold(args.name, args.port)


if __name__ == "__main__":
    main()
