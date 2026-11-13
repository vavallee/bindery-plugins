"""The plugin version is written once and has to survive the trip into the zip.

`version = (X, Y, Z)` in `plugins/calibre-bridge/__init__.py` is the single
source. `plugin/handlers.py` derives `PLUGIN_VERSION` from it by importing the
plugin class from `calibre_plugins.bindery_bridge`, the name Calibre gives the
zip. The plugin's own test suite cannot prove that import works, because it
loads `handlers.py` by path with that package stubbed, which sends the lookup
down its fallback. So this builds the real zip and imports from it under the
Calibre name, in a fresh interpreter that can see nothing else of this
repository.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "calibre-bridge"

# A stand in for calibre.customize.zipplugin, which is how Calibre imports a
# plugin: the zip is never unpacked or put on sys.path. A finder serves
# `calibre_plugins.<import name>` and everything below it straight out of the
# archive, and the modules it creates have no file on disk behind `__file__`.
# It is not Calibre, it reproduces the three properties the version lookup
# depends on: the package name, source that exists only inside the zip, and a
# `__file__` that leads nowhere.
LOADER = r"""
import importlib
import importlib.abc
import importlib.util
import json
import sys
import types
import zipfile

zip_path = sys.argv[1]
archive = zipfile.ZipFile(zip_path)
names = set(archive.namelist())
(marker,) = [n for n in names if n.startswith("plugin-import-name-") and n.endswith(".txt")]
package = "calibre_plugins." + marker[len("plugin-import-name-") : -len(".txt")]


class ZipPluginFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def _member(self, fullname):
        if fullname != package and not fullname.startswith(package + "."):
            return None
        inside = fullname[len(package) :].lstrip(".").replace(".", "/")
        as_package = (inside + "/__init__.py") if inside else "__init__.py"
        if as_package in names:
            return as_package, True
        if inside and inside + ".py" in names:
            return inside + ".py", False
        return None

    def find_spec(self, fullname, path=None, target=None):
        found = self._member(fullname)
        if found is None:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=found[1])

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        member, _ = self._member(module.__name__)
        module.__file__ = "<inside " + package + ">"
        exec(compile(archive.read(member), module.__name__, "exec"), module.__dict__)


class CalibreStubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    # Calibre and Qt themselves: any module, any attribute, usable as a base class.
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in ("calibre", "qt"):
            return importlib.util.spec_from_loader(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        def stub(name):
            if name.startswith("__"):
                raise AttributeError(name)
            return object

        module.__getattr__ = stub


namespace = types.ModuleType("calibre_plugins")
namespace.__path__ = []
sys.modules["calibre_plugins"] = namespace
sys.meta_path[:0] = [ZipPluginFinder(), CalibreStubFinder()]

top = importlib.import_module(package)
handlers = importlib.import_module(package + ".plugin.handlers")
(plugin_class,) = [
    value
    for value in vars(top).values()
    if isinstance(value, type) and isinstance(vars(value).get("version"), tuple)
]
print(
    json.dumps(
        {
            "package": package,
            "class_version": list(plugin_class.version),
            "plugin_version": handlers.PLUGIN_VERSION,
            "handlers_file": handlers.__file__,
        }
    )
)
"""


def import_from_zip(zip_path: pathlib.Path, cwd: pathlib.Path) -> dict:
    # -I: no PYTHONPATH, no user site, and the working directory is not on the
    # path, so the zip is the only place the plugin can come from.
    result = subprocess.run(
        [sys.executable, "-I", "-c", LOADER, str(zip_path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def plugin_copy(tmp_path):
    """The real plugin, copied, so its version can be changed for one test."""
    copy = tmp_path / "src" / "calibre-bridge"
    shutil.copytree(
        PLUGIN_DIR, copy, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests")
    )
    return copy


def set_version(plugin_dir: pathlib.Path, version: tuple[int, int, int]) -> None:
    init_file = plugin_dir / "__init__.py"
    text, count = re.subn(
        r"^(\s+version = )\([^)]*\)$", rf"\g<1>{version!r}", init_file.read_text(), flags=re.M
    )
    assert count == 1, "the version tuple moved, update this test"
    init_file.write_text(text)


def test_the_version_in_the_zip_follows_the_plugin_class(build_plugin, plugin_copy, tmp_path):
    # A version no release has had. If handlers.py spelled the number out, or
    # found it anywhere but the class Calibre reads, this would not come back.
    set_version(plugin_copy, (97, 98, 99))
    out_zip = build_plugin.build(plugin_copy, tmp_path / "dist")
    assert out_zip.name == "calibre-bridge-v97.98.99.zip"

    # Remove the source the zip was built from. Nothing is left on disk for a
    # lookup by path to fall back on.
    shutil.rmtree(plugin_copy.parent)
    empty = tmp_path / "empty"
    empty.mkdir()

    loaded = import_from_zip(out_zip, empty)

    assert loaded["package"] == "calibre_plugins.bindery_bridge"
    assert loaded["class_version"] == [97, 98, 99]
    assert loaded["plugin_version"] == "97.98.99"
    assert not pathlib.Path(loaded["handlers_file"]).exists()


def test_the_released_zip_reports_the_declared_version(build_plugin, tmp_path):
    out_zip = build_plugin.build(PLUGIN_DIR, tmp_path / "dist")
    empty = tmp_path / "empty"
    empty.mkdir()

    loaded = import_from_zip(out_zip, empty)

    assert loaded["plugin_version"] == build_plugin.read_version(PLUGIN_DIR / "__init__.py")
    assert out_zip.name == f"calibre-bridge-v{loaded['plugin_version']}.zip"


def test_only_the_plugin_class_spells_the_version_out():
    """The version literal lives in `__init__.py` and nowhere else in the zip."""
    declared = re.compile(r"""^\s*(PLUGIN_VERSION|version)\s*=\s*["'(]\s*\d""", re.M)
    offenders = [
        path.relative_to(PLUGIN_DIR).as_posix()
        for path in sorted(PLUGIN_DIR.rglob("*.py"))
        if "tests" not in path.relative_to(PLUGIN_DIR).parts
        and path != PLUGIN_DIR / "__init__.py"
        and declared.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
