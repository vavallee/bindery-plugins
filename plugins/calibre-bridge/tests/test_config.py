"""Tests for config.py (ConfigWidget, load_config) and __init__.py lifecycle."""

import importlib
import importlib.util
import logging
import pathlib
import sys
import threading
import types
from unittest.mock import MagicMock, patch

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin"


def _make_qt_stubs():
    """Return module stubs sufficient for config.py to import."""
    qt = types.ModuleType("qt")
    qt_core = types.ModuleType("qt.core")

    # QWidget must be a real class so `class ConfigWidget(QWidget)` works
    class _QWidget:
        def __init__(self, parent=None):
            pass

    qt_core.QWidget = _QWidget

    for name in ("QFormLayout", "QHBoxLayout", "QSpinBox"):
        cls = MagicMock(name=name)
        setattr(qt_core, name, cls)

    for name in ("QLineEdit", "QPushButton"):
        cls = MagicMock(name=name)
        cls.EchoMode = MagicMock()
        cls.EchoMode.Password = 0
        cls.EchoMode.Normal = 1
        setattr(qt_core, name, cls)

    calibre = types.ModuleType("calibre")
    calibre_utils = types.ModuleType("calibre.utils")
    calibre_utils_config = types.ModuleType("calibre.utils.config")

    mock_prefs_storage = {}
    mock_prefs = MagicMock()
    mock_prefs.defaults = {}
    mock_prefs.get = MagicMock(side_effect=lambda k, v=None: mock_prefs_storage.get(k, v))
    mock_prefs.__getitem__ = MagicMock(side_effect=lambda k: mock_prefs_storage.get(k, ""))
    mock_prefs.__setitem__ = MagicMock(
        side_effect=lambda k, v: mock_prefs_storage.__setitem__(k, v)
    )
    calibre_utils_config.JSONConfig = MagicMock(return_value=mock_prefs)

    stubs = {
        "qt": qt,
        "qt.core": qt_core,
        "calibre": calibre,
        "calibre.utils": calibre_utils,
        "calibre.utils.config": calibre_utils_config,
        "_mock_prefs": mock_prefs,
        "_mock_prefs_storage": mock_prefs_storage,
    }
    return stubs


def _load_config_module(stubs):
    sys.path.insert(0, str(_PLUGIN_DIR))
    for name, mod in stubs.items():
        if not name.startswith("_"):
            sys.modules[name] = mod
    sys.modules.pop("config", None)
    try:
        return importlib.import_module("config")
    finally:
        sys.path.pop(0)


def _cleanup(stubs):
    for name in list(stubs.keys()):
        if not name.startswith("_"):
            sys.modules.pop(name, None)
    sys.modules.pop("config", None)


# ── load_config ───────────────────────────────────────────────────────────────


def test_load_config_returns_defaults():
    stubs = _make_qt_stubs()
    config = _load_config_module(stubs)
    try:
        result = config.load_config()
        assert result["port"] == 8099
        assert result["bind_host"] == "0.0.0.0"
        assert result["api_key"] == ""
        assert result["ingest_root"] == ""
        assert result["max_body_bytes"] == 64 * 1024 * 1024
    finally:
        _cleanup(stubs)


# ── ConfigWidget methods — call directly on a mock self ───────────────────────


def test_config_widget_toggle_show():
    stubs = _make_qt_stubs()
    config = _load_config_module(stubs)
    try:
        self = MagicMock()
        config.ConfigWidget._toggle_visibility(self, True)
        self.api_key_input.setEchoMode.assert_called_with(config.QLineEdit.EchoMode.Normal)
        self._show_btn.setText.assert_called_with("Hide")
    finally:
        _cleanup(stubs)


def test_config_widget_toggle_hide():
    stubs = _make_qt_stubs()
    config = _load_config_module(stubs)
    try:
        self = MagicMock()
        config.ConfigWidget._toggle_visibility(self, False)
        self.api_key_input.setEchoMode.assert_called_with(config.QLineEdit.EchoMode.Password)
        self._show_btn.setText.assert_called_with("Show")
    finally:
        _cleanup(stubs)


def test_config_widget_generate_key_sets_text_and_shows():
    stubs = _make_qt_stubs()
    config = _load_config_module(stubs)
    try:
        self = MagicMock()
        config.ConfigWidget._generate_key(self)
        call_args = self.api_key_input.setText.call_args
        assert call_args is not None
        key_text = call_args[0][0]
        assert len(key_text) == 64  # 32 bytes as hex
        self._show_btn.setChecked.assert_called_with(True)
    finally:
        _cleanup(stubs)


def test_config_widget_commit_saves_values():
    stubs = _make_qt_stubs()
    config = _load_config_module(stubs)
    try:
        self = MagicMock()
        self.port_input.value.return_value = 9090
        self.bind_host_input.text.return_value = "127.0.0.1"
        self.ingest_root_input.text.return_value = "/srv/ingest"
        self.api_key_input.text.return_value = "mykey"

        real_prefs = {}
        with patch.object(config, "prefs", real_prefs):
            config.ConfigWidget.commit(self)

        assert real_prefs["port"] == 9090
        assert real_prefs["bind_host"] == "127.0.0.1"
        assert real_prefs["ingest_root"] == "/srv/ingest"
        assert real_prefs["api_key"] == "mykey"
    finally:
        _cleanup(stubs)


def test_config_widget_commit_uses_default_for_empty_bind_host():
    stubs = _make_qt_stubs()
    config = _load_config_module(stubs)
    try:
        self = MagicMock()
        self.port_input.value.return_value = 8099
        self.bind_host_input.text.return_value = "   "
        self.api_key_input.text.return_value = ""

        real_prefs = {}
        with patch.object(config, "prefs", real_prefs):
            config.ConfigWidget.commit(self)

        assert real_prefs["bind_host"] == "0.0.0.0"
    finally:
        _cleanup(stubs)


# ── BinderyBridgeAction lifecycle ─────────────────────────────────────────────


def _make_action_stubs():
    stubs = _make_qt_stubs()
    calibre_gui2 = types.ModuleType("calibre.gui2")
    calibre_gui2_actions = types.ModuleType("calibre.gui2.actions")
    calibre_gui2_actions.InterfaceAction = object
    stubs["calibre.gui2"] = calibre_gui2
    stubs["calibre.gui2.actions"] = calibre_gui2_actions
    calibre_plugins = types.ModuleType("calibre_plugins")
    bbridge = types.ModuleType("calibre_plugins.bindery_bridge")
    bplugin = types.ModuleType("calibre_plugins.bindery_bridge.plugin")
    stubs["calibre_plugins"] = calibre_plugins
    stubs["calibre_plugins.bindery_bridge"] = bbridge
    stubs["calibre_plugins.bindery_bridge.plugin"] = bplugin
    return stubs


def _load_init_module(stubs):
    sys.path.insert(0, str(_PLUGIN_DIR))
    for name, mod in stubs.items():
        if not name.startswith("_"):
            sys.modules[name] = mod
    # Stub server and config imports that __init__.py will resolve lazily
    mock_server_cls = MagicMock()
    mock_server_inst = MagicMock()
    mock_server_cls.return_value = mock_server_inst
    mock_load_config = MagicMock(
        return_value={"port": "8099", "bind_host": "127.0.0.1", "api_key": "key"}
    )
    stubs["_mock_server_cls"] = mock_server_cls
    stubs["_mock_server_inst"] = mock_server_inst
    stubs["_mock_load_config"] = mock_load_config

    spec = importlib.util.spec_from_file_location(
        "bindery_bridge_init", _PLUGIN_DIR / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.pop(0)
    return mod, mock_server_cls, mock_server_inst, mock_load_config


def _cleanup_action(stubs):
    for name in list(stubs.keys()):
        if not name.startswith("_"):
            sys.modules.pop(name, None)
    for name in ("config", "server", "handlers", "adder", "bindery_bridge_init"):
        sys.modules.pop(name, None)


def _make_action(mod, mock_server_cls, mock_load_config):
    action = mod.BinderyBridgeAction.__new__(mod.BinderyBridgeAction)
    action._start_lock = threading.Lock()
    action._server = None
    action.gui = MagicMock()
    action._BridgeServer = mock_server_cls
    action._load_config = mock_load_config
    action._get_db = MagicMock()
    action._get_gui = MagicMock()
    return action


def test_start_server_threading_lock_idempotent():
    """_start_server called twice starts the server only once."""
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    try:
        action = _make_action(mod, mock_server_cls, mock_cfg)
        action._start_server()
        action._start_server()
        assert mock_server_cls.call_count == 1
    finally:
        _cleanup_action(stubs)


def test_restart_server_stops_old_and_creates_new():
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    try:
        action = _make_action(mod, mock_server_cls, mock_cfg)
        action._server = mock_server_inst
        action._restart_server()
        mock_server_inst.stop.assert_called_once()
        assert mock_server_cls.call_count == 1
    finally:
        _cleanup_action(stubs)


def test_shutting_down_suppresses_stop_exception():
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    try:
        action = _make_action(mod, mock_server_cls, mock_cfg)
        action._server = mock_server_inst
        mock_server_inst.stop.side_effect = RuntimeError("crash")
        result = action.shutting_down()
        assert result is True
    finally:
        _cleanup_action(stubs)


def test_shutting_down_with_no_server():
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    try:
        action = _make_action(mod, mock_server_cls, mock_cfg)
        action._server = None
        result = action.shutting_down()
        assert result is True
    finally:
        _cleanup_action(stubs)


def test_start_server_logs_error_on_failure():
    """_start_server calls log when server.start() raises."""
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    try:
        action = _make_action(mod, mock_server_cls, mock_cfg)
        mock_server_inst.start.side_effect = OSError("port in use")
        with patch.object(logging.getLogger("__init__"), "error"):
            action._start_server()
        assert action._server is None
    finally:
        _cleanup_action(stubs)
