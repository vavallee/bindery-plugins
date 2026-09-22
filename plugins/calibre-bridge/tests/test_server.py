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
        status_mod = _il.import_module("status")
        bplugin.adder = adder_mod
        bplugin.status = status_mod
        sys.modules.update(
            {
                "calibre_plugins": calibre_plugins,
                "calibre_plugins.bindery_bridge": bbridge,
                "calibre_plugins.bindery_bridge.plugin": bplugin,
                "calibre_plugins.bindery_bridge.plugin.adder": adder_mod,
                "calibre_plugins.bindery_bridge.plugin.handlers": handlers_mod,
                "calibre_plugins.bindery_bridge.plugin.status": status_mod,
            }
        )
        sys.modules.pop("server", None)
        yield _il.import_module("server")
    finally:
        sys.path.pop(0)
        for name in ("server", "status", "handlers", "adder"):
            sys.modules.pop(name, None)
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


def test_is_loopback_helper(server_module):
    assert server_module._is_loopback("127.0.0.1")
    assert server_module._is_loopback("localhost")
    assert server_module._is_loopback("::1")
    assert server_module._is_loopback(" 127.0.0.1 ")
    assert not server_module._is_loopback("0.0.0.0")
    assert not server_module._is_loopback("192.168.1.10")


def test_non_loopback_without_api_key_refuses_to_start(server_module):
    """Fail closed: binding to a non-loopback host with no api_key would
    expose the unauthenticated add endpoint, so start() must refuse."""
    from unittest.mock import MagicMock

    db = MagicMock()
    srv = server_module.BridgeServer()
    with pytest.raises(ValueError, match="api_key"):
        srv.start(port=_free_port(), bind_host="0.0.0.0", api_key="", get_db=lambda: db)
    # Nothing should have been left listening.
    assert srv._httpd is None


def test_non_loopback_with_api_key_starts(server_module):
    from unittest.mock import MagicMock

    db = MagicMock()
    db.library_path = "/tmp/lib"
    srv = server_module.BridgeServer()
    port = _free_port()
    # An api_key is set, so a non-loopback bind is allowed.
    srv.start(port=port, bind_host="0.0.0.0", api_key="secret", get_db=lambda: db)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
    finally:
        srv.stop()


def test_loopback_without_api_key_allowed(server_module):
    """Local dev: loopback bind with no key is still permitted."""
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


def test_bridge_server_double_stop(server_module):
    from unittest.mock import MagicMock

    db = MagicMock()
    db.library_path = "/tmp/lib"
    srv = server_module.BridgeServer()
    port = _free_port()
    srv.start(port=port, bind_host="127.0.0.1", api_key="", get_db=lambda: db)
    srv.stop()
    srv.stop()  # second stop must not raise


# ── the degraded server (a refused start must stay discoverable) ──────────────


def _get_json(port, path):
    import json

    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_start_degraded_serves_health_with_the_reason(server_module):
    srv = server_module.BridgeServer()
    port = _free_port()
    srv.start_degraded(port=port, bind_host="127.0.0.1", reason="no api_key set")
    try:
        status, payload = _get_json(port, "/v1/health")
        assert status == 200
        assert payload["status"] == "degraded"
        assert payload["error"] == "no api_key set"
        assert payload["capabilities"] == []
        assert payload["library"] == ""
    finally:
        srv.stop()


def test_degraded_server_answers_adds_with_401_and_the_reason(server_module):
    """401 is the status Bindery already renders as "check api_key in Settings"."""
    import json
    import urllib.error

    srv = server_module.BridgeServer()
    port = _free_port()
    reason = "refusing to bind to non-loopback host without an api_key"
    srv.start_degraded(port=port, bind_host="127.0.0.1", reason=reason)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=b'{"path": "/x.epub"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 401
        payload = json.loads(exc_info.value.read())
        assert payload["error"] == reason
        assert payload["code"] == "unauthorized"
    finally:
        srv.stop()


def test_degraded_server_answers_patch_with_401(server_module):
    import json
    import urllib.error

    srv = server_module.BridgeServer()
    port = _free_port()
    srv.start_degraded(port=port, bind_host="127.0.0.1", reason="nope")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books/1", data=b"{}", method="PATCH"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 401
        assert json.loads(exc_info.value.read())["code"] == "unauthorized"
    finally:
        srv.stop()


def test_degraded_server_404s_anything_else(server_module):
    import json
    import urllib.error

    srv = server_module.BridgeServer()
    port = _free_port()
    srv.start_degraded(port=port, bind_host="127.0.0.1", reason="nope")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/paths?path=/etc", timeout=5)
        assert exc_info.value.code == 404
        assert json.loads(exc_info.value.read())["code"] == "not_found"
    finally:
        srv.stop()


def test_status_tracks_the_server_lifecycle(server_module):
    from unittest.mock import MagicMock

    status = sys.modules["calibre_plugins.bindery_bridge.plugin.status"]
    db = MagicMock()
    db.library_path = "/tmp/lib"
    srv = server_module.BridgeServer()
    port = _free_port()
    srv.start(port=port, bind_host="127.0.0.1", api_key="k", get_db=lambda: db)
    assert status.current()["status"] == status.RUNNING
    assert status.summary() == f"Listening on 127.0.0.1:{port}"
    srv.stop()
    assert status.current()["status"] == status.STOPPED

    degraded = server_module.BridgeServer()
    port = _free_port()
    degraded.start_degraded(port=port, bind_host="127.0.0.1", reason="no api_key set")
    assert status.current()["status"] == status.DEGRADED
    assert "no api_key set" in status.summary()
    assert f"127.0.0.1:{port}" in status.summary()
    degraded.stop()
    assert status.summary() == "Not running: no api_key set"


def test_status_defaults_before_any_start(server_module):
    status = sys.modules["calibre_plugins.bindery_bridge.plugin.status"]
    status.set_stopped()
    assert status.summary() == "Not running"
    status.set_degraded("reason with no endpoint")
    assert status.summary() == "Not listening for books: reason with no endpoint"


def test_stop_survives_a_broken_shutdown(server_module):
    from unittest.mock import MagicMock

    srv = server_module.BridgeServer()
    broken = MagicMock()
    broken.shutdown.side_effect = RuntimeError("already gone")
    srv._httpd = broken
    srv.stop()
    assert srv._httpd is None
