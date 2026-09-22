"""Shared helpers for the release-tooling tests.

The scripts under scripts/ are standalone files, not an importable package, so
they are loaded by path.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(name: str) -> ModuleType:
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_scripts_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def build_plugin() -> ModuleType:
    return _load("build_plugin")


@pytest.fixture(scope="session")
def scaffold_plugin() -> ModuleType:
    return _load("scaffold_plugin")
