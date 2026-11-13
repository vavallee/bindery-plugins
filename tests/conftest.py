"""Shared helpers for the release-tooling tests.

The scripts under scripts/ are standalone files, not an importable package.
Some import a sibling (`release_guard` reads the version through
`build_plugin`), which resolves when one is run as `python scripts/x.py`
because Python puts the script's own directory first on the path. The tests
do the same, so each script is one module here exactly as it is there.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
from collections.abc import Callable
from types import ModuleType

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__ or "").resolve() == SCRIPTS_DIR / f"{name}.py"
    return module


@pytest.fixture(scope="session")
def build_plugin() -> ModuleType:
    return _load("build_plugin")


@pytest.fixture(scope="session")
def scaffold_plugin() -> ModuleType:
    return _load("scaffold_plugin")


@pytest.fixture(scope="session")
def release_guard() -> ModuleType:
    return _load("release_guard")


@pytest.fixture(scope="session")
def bump_chart() -> ModuleType:
    return _load("bump_chart")


@pytest.fixture(scope="session")
def check_versions() -> ModuleType:
    return _load("check_versions")


PLUGIN_INIT_TEMPLATE = """\
from calibre.customize import InterfaceActionBase


class DemoBridge(InterfaceActionBase):
    name = "Demo Bridge"
    version = ({version})
    minimum_calibre_version = (6, 0, 0)
"""

CHANGELOG_TEMPLATE = """\
# Changelog

## calibre-bridge

### [Unreleased]

### [1.2.3] - 2026-02-03

#### Fixed

- **The third fix.** It fixes the third thing.

### [1.2.2] - 2026-02-02

#### Fixed

- **The second fix.** It fixes the second thing.
"""

CHART_TEMPLATE = """\
apiVersion: v2
name: calibre-plugin-installer
type: application
version: 0.2.7
appVersion: "{version}"
"""

VALUES_TEMPLATE = """\
pluginVersion: "{version}"
# pluginName must match the plugin class.
pluginName: "Bindery Bridge"
"""

ARGOCD_TEMPLATE = """\
spec:
  generators:
    - list:
        elements:
          - cluster: homelab
            # moves with targetRevision
            pluginVersion: "{version}"
  template:
    spec:
      source:
        repoURL: https://github.com/vavallee/bindery-plugins
        targetRevision: v-calibre-bridge-{version}
        helm:
          valuesObject:
            pluginVersion: "{{{{pluginVersion}}}}"
"""


def write_release_tree(
    root: pathlib.Path, *, plugin: str = "1.2.3", chart: str = "1.2.3"
) -> pathlib.Path:
    """A stand in for the repository: every file that names a version."""
    plugin_dir = root / "plugins" / "calibre-bridge"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        PLUGIN_INIT_TEMPLATE.format(version=plugin.replace(".", ", "))
    )
    (root / "CHANGELOG.md").write_text(CHANGELOG_TEMPLATE)
    chart_dir = root / "charts" / "calibre-plugin-installer"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(CHART_TEMPLATE.format(version=chart))
    (chart_dir / "values.yaml").write_text(VALUES_TEMPLATE.format(version=chart))
    (root / "argocd").mkdir()
    (root / "argocd" / "application.yaml").write_text(ARGOCD_TEMPLATE.format(version=chart))
    return root


@pytest.fixture
def make_release_tree() -> Callable[..., pathlib.Path]:
    return write_release_tree


@pytest.fixture
def release_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    return write_release_tree(tmp_path / "repo")
