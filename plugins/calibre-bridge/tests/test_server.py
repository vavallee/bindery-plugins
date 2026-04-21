"""Tests for BridgeServer lifecycle."""

import importlib
import pathlib
import socket
import sys
import time
import types
import urllib.request

import pytest


@pytest.fixture
def server_module():
    import importlib as _il
    import pathlib as _pl

    plugin_dir = _pl.Path(__file__).resolve().parent.parent / "plugin"
    sys.path.insert(0, str(plugin_dir))
    try:
        calibre_plugins = types.ModuleType("calibre_plugins")
        bbridge = types.ModuleType("calibre_plugins.bindery_bridge")
        bplugin = types.ModuleType("calibre_plugins.bindery_bridge.plugin")
        adder_mod = _il.import_module("adder")
        handlers_mod = _il.import_module("handlers")
        bplugin.adder = adder_mod
        sys.modules.update(
            {
                "calibre_plugins": calibre_plugins,
                "calibre_plugins.bindery_bridge": bbridge,
                "calibre_plugins.bindery_bridge.plugin": bplugin,
                "calibre_plugins.bindery_bridge.plugin.adder": adder_mod,
                "calibre_plugins.bindery_bridge.plugin.handlers": handlers_mod,
            }
        )
        if "server" in sys.modules:
            del sys.modules["server"]
        yield _il.import_module("server")
    finally:
        sys.path.pop(0)
        for name in list(sys.modules):
            if name.startswith("calibre_plugins"):
                sys.modules.pop(name, None)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bridge_server_start_stop(server_module):
    from unittest.mock import MagicMock

    db = MagicMock()
    db.library_path = "/tmp/lib"
    srv = server_module.BridgeServer()
    port = _free_port()
    srv.start(port=port, bind_host="127.0.0.1", api_key="", get_db=lambda: db)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=5) as resp:
            assert resp.status == 200
    finally:
        srv.stop()

    # After stop(), the port should be released (connection refused).
    time.sleep(0.05)
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1)


def test_bridge_server_stop_when_not_started(server_module):
    srv = server_module.BridgeServer()
    srv.stop()  # must not raise


def test_bridge_server_double_stop(server_module):
    from unittest.mock import MagicMock

    db = MagicMock()
    db.library_path = "/tmp/lib"
    srv = server_module.BridgeServer()
    port = _free_port()
    srv.start(port=port, bind_host="127.0.0.1", api_key="", get_db=lambda: db)
    srv.stop()
    srv.stop()  # second stop must not raise
