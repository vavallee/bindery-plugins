"""Tests for the release tooling in scripts/.

Neither script had a test before 0.6.0, which is how `scaffold_plugin.py`
came to generate plugins that import a package `build_plugin.py` never puts
in the zip.
"""

import importlib.util
import pathlib
import subprocess
import sys
import zipfile

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(f"_script_{name}", _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def build_plugin():
    return _load_script("build_plugin")


@pytest.fixture
def scaffold_plugin():
    return _load_script("scaffold_plugin")


# ── build_plugin.py ───────────────────────────────────────────────────────────


def _fake_plugin(root, version="(1, 2, 3)"):
    plugin = root / "demo-plugin"
    (plugin / "plugin").mkdir(parents=True)
    (plugin / "tests").mkdir()
    (plugin / "__init__.py").write_text(
        "from calibre.customize import InterfaceActionBase\n\n\n"
        "class Demo(InterfaceActionBase):\n"
        "    name = 'Demo'\n"
        f"    version = {version}\n"
    )
    (plugin / "plugin" / "handlers.py").write_text("X = 1\n")
    (plugin / "tests" / "test_demo.py").write_text("def test_x():\n    assert True\n")
    (plugin / "plugin" / "__pycache__").mkdir()
    (plugin / "plugin" / "__pycache__" / "handlers.pyc").write_bytes(b"\x00")
    (plugin / "plugin" / "stray.pyo").write_bytes(b"\x00")
    return plugin


def test_read_version_finds_the_class_attribute(build_plugin, tmp_path):
    plugin = _fake_plugin(tmp_path)
    assert build_plugin.read_version(plugin / "__init__.py") == "1.2.3"


def test_read_version_raises_without_a_version(build_plugin, tmp_path):
    init = tmp_path / "__init__.py"
    init.write_text("class Demo:\n    name = 'Demo'\n")
    with pytest.raises(RuntimeError, match="could not find version tuple"):
        build_plugin.read_version(init)


def test_plugin_zip_name_uses_the_slug(build_plugin, tmp_path):
    assert build_plugin.plugin_zip_name(tmp_path / "kobo_bridge", "0.6.0") == (
        "kobo-bridge-v0.6.0.zip"
    )


def test_build_excludes_tests_and_bytecode(build_plugin, tmp_path):
    plugin = _fake_plugin(tmp_path)
    out = build_plugin.build(plugin, tmp_path / "dist")
    names = sorted(zipfile.ZipFile(out).namelist())
    assert names == ["__init__.py", "plugin/handlers.py"]


def test_build_writes_a_matching_sha256(build_plugin, tmp_path):
    import hashlib

    plugin = _fake_plugin(tmp_path)
    out = build_plugin.build(plugin, tmp_path / "dist")
    digest_file = out.parent / (out.name + ".sha256")
    recorded = digest_file.read_text().split()[0]
    assert recorded == hashlib.sha256(out.read_bytes()).hexdigest()
    assert digest_file.read_text().strip().endswith(out.name)


def test_build_refuses_a_directory_without_an_init(build_plugin, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(SystemExit):
        build_plugin.build(empty, tmp_path / "dist")


def test_build_zip_contains_only_the_plugin_directory():
    """The rule that killed pluginbase: nothing outside plugins/<name> ships."""
    import tempfile

    with tempfile.TemporaryDirectory() as out_dir:
        build_plugin = _load_script("build_plugin")
        out = build_plugin.build(_REPO_ROOT / "plugins" / "calibre-bridge", pathlib.Path(out_dir))
        names = zipfile.ZipFile(out).namelist()
    assert "plugin/handlers.py" in names
    assert not [n for n in names if n.startswith("pluginbase")]


# ── scaffold_plugin.py ────────────────────────────────────────────────────────


def test_name_helpers(scaffold_plugin):
    assert scaffold_plugin._to_class_name("kobo-bridge") == "KoboBridge"
    assert scaffold_plugin._to_module_name("kobo-bridge") == "kobo_bridge"
    assert scaffold_plugin._render("%A% and %B%", {"A": "x", "B": 2}) == "x and 2"


def test_scaffold_writes_the_expected_files(scaffold_plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold_plugin, "REPO_ROOT", tmp_path)
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    base = tmp_path / "plugins" / "kobo-bridge"
    written = sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())
    assert written == [
        "README.md",
        "__init__.py",
        "conftest.py",
        "plugin/__init__.py",
        "plugin/action.py",
        "plugin/config.py",
        "plugin/handlers.py",
        "plugin/server.py",
        "tests/__init__.py",
        "tests/conftest.py",
        "tests/test_handlers.py",
    ]


def test_scaffold_skips_files_that_already_exist(scaffold_plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold_plugin, "REPO_ROOT", tmp_path)
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    marker = tmp_path / "plugins" / "kobo-bridge" / "README.md"
    marker.write_text("mine\n")
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    assert marker.read_text() == "mine\n"


def test_scaffold_substitutes_the_name_and_port(scaffold_plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold_plugin, "REPO_ROOT", tmp_path)
    scaffold_plugin.scaffold("kobo-bridge", 8123)
    base = tmp_path / "plugins" / "kobo-bridge"
    init = (base / "__init__.py").read_text()
    assert "class KoboBridge(InterfaceActionBase)" in init
    assert 'actual_plugin = "calibre_plugins.kobo_bridge.plugin:KoboBridgeAction"' in init
    assert '"port": 8123,' in (base / "plugin" / "config.py").read_text()
    assert "%" not in init


def test_scaffolded_plugin_imports_nothing_from_the_repo_root(
    scaffold_plugin, tmp_path, monkeypatch
):
    """The regression that removing pluginbase closes.

    build_plugin.py zips only plugins/<name>, so a generated plugin that
    imports a repo root package passes pytest here and then fails to load
    inside Calibre. Assert on the generated source directly: an import of a
    top level name this repo owns is the bug.
    """
    monkeypatch.setattr(scaffold_plugin, "REPO_ROOT", tmp_path)
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    base = tmp_path / "plugins" / "kobo-bridge"
    for path in base.rglob("*.py"):
        source = path.read_text()
        assert "pluginbase" not in source, f"{path} imports a package the zip will not contain"


def test_scaffolded_plugin_compiles_and_its_test_passes(scaffold_plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold_plugin, "REPO_ROOT", tmp_path)
    scaffold_plugin.scaffold("kobo-bridge", 8100)
    base = tmp_path / "plugins" / "kobo-bridge"

    for path in sorted(base.rglob("*.py")):
        compile(path.read_text(), str(path), "exec")

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(base / "tests"), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=str(base.parent.parent),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_scaffold_main_parses_arguments(scaffold_plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(scaffold_plugin, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["scaffold_plugin.py", "kobo-bridge", "--port", "8155"])
    scaffold_plugin.main()
    config = (tmp_path / "plugins" / "kobo-bridge" / "plugin" / "config.py").read_text()
    assert '"port": 8155,' in config


def test_build_plugin_main_builds(build_plugin, tmp_path, monkeypatch):
    plugin = _fake_plugin(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["build_plugin.py", str(plugin), "--output-dir", str(tmp_path / "out")]
    )
    assert build_plugin.main() == 0
    assert (tmp_path / "out" / "demo-plugin-v1.2.3.zip").is_file()
