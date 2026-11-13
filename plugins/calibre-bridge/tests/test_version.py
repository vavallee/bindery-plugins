"""The plugin version has one source: the tuple on the plugin class.

``handlers.PLUGIN_VERSION`` is what ``GET /v1/health`` reports and what the
pull client sends as ``X-Bridge-Version``. It used to be a second literal that
a release had to remember to edit. It is now derived, and these tests hold the
two ways it is derived to the same answer.
"""

import importlib
import re
import sys
import types

import pytest

from .conftest import _PLUGIN_DIR, PLUGIN_VERSION


def _import_handlers(monkeypatch, bridge_package):
    """Import ``plugin/handlers.py`` with ``calibre_plugins.bindery_bridge`` set."""
    monkeypatch.syspath_prepend(str(_PLUGIN_DIR))
    monkeypatch.delitem(sys.modules, "handlers", raising=False)
    for name in ("calibre_plugins", "calibre_plugins.bindery_bridge"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    if bridge_package is not None:
        monkeypatch.setitem(sys.modules, "calibre_plugins", types.ModuleType("calibre_plugins"))
        monkeypatch.setitem(sys.modules, "calibre_plugins.bindery_bridge", bridge_package)
    try:
        return importlib.import_module("handlers")
    finally:
        sys.modules.pop("handlers", None)


def test_the_declared_version_is_three_numbers():
    assert re.fullmatch(r"\d+\.\d+\.\d+", PLUGIN_VERSION)


def test_handlers_reports_the_declared_version(bridge_handlers):
    assert bridge_handlers.PLUGIN_VERSION == PLUGIN_VERSION


def test_inside_calibre_the_version_comes_from_the_plugin_class(monkeypatch):
    # What Calibre provides: the top level __init__.py, imported as
    # calibre_plugins.bindery_bridge, holding the class whose tuple it reads.
    package = types.ModuleType("calibre_plugins.bindery_bridge")
    package.BinderyBridge = type("BinderyBridge", (), {"version": (97, 98, 99)})

    handlers = _import_handlers(monkeypatch, package)

    assert handlers.PLUGIN_VERSION == "97.98.99"


@pytest.mark.parametrize("stubbed", [True, False])
def test_without_calibre_the_version_comes_from_the_same_file(monkeypatch, stubbed):
    # The two shapes the suite loads handlers.py in: the package stubbed as an
    # empty module, and the package absent altogether.
    package = types.ModuleType("calibre_plugins.bindery_bridge") if stubbed else None

    handlers = _import_handlers(monkeypatch, package)

    assert handlers.PLUGIN_VERSION == PLUGIN_VERSION


def _exec_handlers_as(monkeypatch, fake_file):
    """Run handlers.py's source as if it lived at ``fake_file``, package import failing."""
    for name in ("calibre_plugins", "calibre_plugins.bindery_bridge"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    source = (_PLUGIN_DIR / "handlers.py").read_text(encoding="utf-8")
    namespace = {"__file__": fake_file, "__name__": "handlers_under_test"}
    exec(compile(source, fake_file, "exec"), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize(
    "fake_file",
    [
        # What Calibre gives a plugin loaded from its zip.
        "/home/user/.config/calibre/plugins/Bindery Bridge.zip/plugin/handlers.py",
        # A directory that does not exist at all.
        "/nonexistent-dir/plugin/handlers.py",
    ],
)
def test_an_unreadable_fallback_raises_one_clear_error(monkeypatch, fake_file):
    with pytest.raises(RuntimeError) as excinfo:
        _exec_handlers_as(monkeypatch, fake_file)

    message = str(excinfo.value)
    assert "plugin version could not be determined" in message
    assert "calibre_plugins.bindery_bridge" in message
    assert "__init__.py" in message
    assert "must not import plugin.*" in message
    assert "circular" in message
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_a_fallback_file_without_a_version_tuple_raises_the_same_error(monkeypatch, tmp_path):
    (tmp_path / "plugin").mkdir()
    (tmp_path / "__init__.py").write_text("class BinderyBridge:\n    pass\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="plugin version could not be determined") as excinfo:
        _exec_handlers_as(monkeypatch, str(tmp_path / "plugin" / "handlers.py"))

    assert "has no version tuple" in str(excinfo.value)


def test_a_real_directory_layout_still_parses_the_fallback(monkeypatch, tmp_path):
    (tmp_path / "plugin").mkdir()
    (tmp_path / "__init__.py").write_text(
        "class BinderyBridge:\n    version = (4, 5, 6)\n", encoding="utf-8"
    )

    namespace = _exec_handlers_as(monkeypatch, str(tmp_path / "plugin" / "handlers.py"))

    assert namespace["PLUGIN_VERSION"] == "4.5.6"


def test_health_and_the_server_header_carry_it(bridge_handlers, serve_bridge):
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: None))
    status, payload, headers = bridge.call("GET", "/v1/health")
    assert status == 200
    assert payload["plugin_version"] == PLUGIN_VERSION
    assert headers["Server"].startswith(f"BinderyBridge/{PLUGIN_VERSION}")
