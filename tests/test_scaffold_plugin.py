"""Tests for scripts/scaffold_plugin.py.

Every file it writes is Python that has to import cleanly inside Calibre, so
the checks here are: the templates render with no markers left behind, the
result parses, and re-running the script never clobbers existing work.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


@pytest.fixture
def scaffold_in(scaffold_plugin, tmp_path, monkeypatch):
    """Point the script at a throwaway root instead of the real repo."""
    monkeypatch.setattr(scaffold_plugin, "REPO_ROOT", tmp_path)
    return tmp_path


def test_to_class_name(scaffold_plugin):
    assert scaffold_plugin._to_class_name("kobo-bridge") == "KoboBridge"
    assert scaffold_plugin._to_class_name("calibre_bridge") == "CalibreBridge"
    assert scaffold_plugin._to_class_name("bridge") == "Bridge"


def test_to_module_name(scaffold_plugin):
    assert scaffold_plugin._to_module_name("kobo-bridge") == "kobo_bridge"


def test_render_replaces_every_marker(scaffold_plugin):
    out = scaffold_plugin._render("%A% and %B%", {"A": "x", "B": 2})
    assert out == "x and 2"


def test_scaffold_writes_the_expected_files(scaffold_plugin, scaffold_in):
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    base = scaffold_in / "plugins" / "kobo-bridge"

    expected = [
        "__init__.py",
        "conftest.py",
        "README.md",
        "plugin/__init__.py",
        "plugin/action.py",
        "plugin/handlers.py",
        "plugin/config.py",
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/test_handlers.py",
    ]
    missing = [rel for rel in expected if not (base / rel).is_file()]
    assert missing == []


def test_scaffold_leaves_no_unrendered_markers(scaffold_plugin, scaffold_in):
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    base = scaffold_in / "plugins" / "kobo-bridge"

    leftovers = {}
    for path in base.rglob("*"):
        if path.is_file():
            text = path.read_text()
            marks = [line for line in text.splitlines() if "%" in line and "%s" not in line]
            if marks:
                leftovers[str(path.relative_to(base))] = marks
    assert leftovers == {}


def test_scaffolded_python_parses(scaffold_plugin, scaffold_in):
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    base = scaffold_in / "plugins" / "kobo-bridge"

    for path in sorted(base.rglob("*.py")):
        ast.parse(path.read_text(), filename=str(path))


def test_scaffolded_init_declares_the_plugin_calibre_expects(scaffold_plugin, scaffold_in):
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    init = (scaffold_in / "plugins" / "kobo-bridge" / "__init__.py").read_text()

    assert "class KoboBridge(InterfaceActionBase):" in init
    assert 'name = "Kobo Bridge"' in init
    assert "minimum_calibre_version = (6, 0, 0)" in init
    assert 'actual_plugin = "calibre_plugins.kobo_bridge.plugin:KoboBridgeAction"' in init


def test_port_is_threaded_into_the_config_module(scaffold_plugin, scaffold_in):
    scaffold_plugin.scaffold("kobo-bridge", 8123)
    config = (scaffold_in / "plugins" / "kobo-bridge" / "plugin" / "config.py").read_text()

    assert '"port": 8123,' in config
    tree = ast.parse(config)
    assert isinstance(tree, ast.Module)


def test_port_reaches_the_generated_readme(scaffold_plugin, scaffold_in):
    scaffold_plugin.scaffold("kobo-bridge", 8123)
    readme = (scaffold_in / "plugins" / "kobo-bridge" / "README.md").read_text()
    assert "8123" in readme
    assert "Kobo Bridge" in readme


def test_rerunning_does_not_clobber_edits(scaffold_plugin, scaffold_in, capsys):
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    handlers = scaffold_in / "plugins" / "kobo-bridge" / "plugin" / "handlers.py"
    handlers.write_text("# hand written\n")

    scaffold_plugin.scaffold("kobo-bridge", 8100)

    assert handlers.read_text() == "# hand written\n"
    assert "(0 files)" in capsys.readouterr().out


def test_missing_files_are_restored_on_a_rerun(scaffold_plugin, scaffold_in):
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    readme = scaffold_in / "plugins" / "kobo-bridge" / "README.md"
    readme.unlink()

    scaffold_plugin.scaffold("kobo-bridge", 8100)
    assert readme.is_file()


def test_main_parses_the_documented_invocation(scaffold_plugin, scaffold_in, monkeypatch):
    monkeypatch.setattr("sys.argv", ["scaffold_plugin.py", "kobo-bridge", "--port", "8100"])
    scaffold_plugin.main()
    assert (scaffold_in / "plugins" / "kobo-bridge" / "__init__.py").is_file()


def test_scaffolded_plugin_builds(scaffold_plugin, build_plugin, scaffold_in, tmp_path):
    """The two scripts have to agree: scaffold output must be buildable."""
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    plugin_dir: pathlib.Path = scaffold_in / "plugins" / "kobo-bridge"

    out_zip = build_plugin.build(plugin_dir, tmp_path / "dist")
    assert out_zip.name == "kobo-bridge-v0.1.0.zip"
