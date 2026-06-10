"""Pytest fixtures and helpers for testing Calibre plugin HTTP handlers."""

from __future__ import annotations

import importlib
import pathlib
import sys
import types
from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest


def make_calibre_stub() -> dict[str, Any]:
    """Return a minimal ``sys.modules`` patch dict that satisfies Calibre imports.

    Provides stubs for:
    - ``calibre.customize`` (``InterfaceActionBase``)
    - ``calibre.gui2.actions`` (``InterfaceAction``)
    - ``calibre.constants`` (``numeric_version``)
    - ``calibre.ebooks.metadata.meta`` (``get_metadata``)
    - ``calibre.utils.config`` (``JSONConfig``)
    - ``qt.core`` (common Qt widget classes)

    Returns a dict suitable for use with :func:`patch_calibre_modules`.
    """
    calibre = types.ModuleType("calibre")

    customize = types.ModuleType("calibre.customize")
    customize.InterfaceActionBase = object  # type: ignore[attr-defined]

    constants = types.ModuleType("calibre.constants")
    constants.numeric_version = (9, 0, 0)  # type: ignore[attr-defined]

    gui2 = types.ModuleType("calibre.gui2")
    gui2_actions = types.ModuleType("calibre.gui2.actions")
    gui2_actions.InterfaceAction = object  # type: ignore[attr-defined]

    ebooks = types.ModuleType("calibre.ebooks")
    metadata = types.ModuleType("calibre.ebooks.metadata")
    meta = types.ModuleType("calibre.ebooks.metadata.meta")
    meta.get_metadata = MagicMock(return_value=MagicMock(name="Metadata"))  # type: ignore[attr-defined]

    calibre_utils = types.ModuleType("calibre.utils")
    calibre_utils_config = types.ModuleType("calibre.utils.config")
    calibre_utils_date = types.ModuleType("calibre.utils.date")
    mock_prefs_storage: dict[str, Any] = {}
    mock_prefs = MagicMock()
    mock_prefs.defaults = {}
    mock_prefs.get = MagicMock(side_effect=lambda k, v=None: mock_prefs_storage.get(k, v))
    mock_prefs.__getitem__ = MagicMock(
        side_effect=lambda k: mock_prefs_storage.get(k, "")
    )
    mock_prefs.__setitem__ = MagicMock(
        side_effect=lambda k, v: mock_prefs_storage.__setitem__(k, v)
    )
    calibre_utils_config.JSONConfig = MagicMock(return_value=mock_prefs)
    calibre_utils_date.parse_date = MagicMock(side_effect=lambda value: value)

    qt = types.ModuleType("qt")
    qt_core = types.ModuleType("qt.core")

    class _QWidget:
        def __init__(self, parent: Any = None) -> None:
            pass

    qt_core.QWidget = _QWidget  # type: ignore[attr-defined]

    for name in ("QFormLayout", "QHBoxLayout", "QSpinBox", "QDialog",
                 "QDialogButtonBox", "QVBoxLayout", "QPushButton"):
        setattr(qt_core, name, MagicMock(name=name))

    ql = MagicMock(name="QLineEdit")
    ql.EchoMode = MagicMock()
    ql.EchoMode.Password = 0
    ql.EchoMode.Normal = 1
    qt_core.QLineEdit = ql  # type: ignore[attr-defined]

    return {
        "calibre": calibre,
        "calibre.customize": customize,
        "calibre.constants": constants,
        "calibre.gui2": gui2,
        "calibre.gui2.actions": gui2_actions,
        "calibre.ebooks": ebooks,
        "calibre.ebooks.metadata": metadata,
        "calibre.ebooks.metadata.meta": meta,
        "calibre.utils": calibre_utils,
        "calibre.utils.config": calibre_utils_config,
        "calibre.utils.date": calibre_utils_date,
        "qt": qt,
        "qt.core": qt_core,
        "_mock_prefs": mock_prefs,
        "_mock_prefs_storage": mock_prefs_storage,
    }


def patch_calibre_modules(stubs: dict[str, Any]) -> None:
    """Install *stubs* into ``sys.modules`` (skipping private ``_`` keys)."""
    for name, mod in stubs.items():
        if not name.startswith("_"):
            sys.modules[name] = mod


def unpatch_calibre_modules(stubs: dict[str, Any]) -> None:
    """Remove all stub entries from ``sys.modules``."""
    for name in stubs:
        if not name.startswith("_"):
            sys.modules.pop(name, None)


@pytest.fixture()
def calibre_stubs() -> Generator[dict[str, Any], None, None]:
    """Fixture: installs and tears down minimal Calibre + Qt stubs."""
    stubs = make_calibre_stub()
    patch_calibre_modules(stubs)
    yield stubs
    unpatch_calibre_modules(stubs)


def load_plugin_module(plugin_dir: pathlib.Path, name: str) -> Any:
    """Import *name* from *plugin_dir*, bypassing the Calibre package hierarchy.

    Adds *plugin_dir* to ``sys.path`` temporarily, removes any cached module,
    then imports fresh.  Useful in tests that need the real module, not a stub.
    """
    sys.path.insert(0, str(plugin_dir))
    sys.modules.pop(name, None)
    try:
        return importlib.import_module(name)
    finally:
        sys.path.pop(0)
