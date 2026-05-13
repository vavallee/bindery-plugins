import importlib
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_calibre():
    calibre = types.ModuleType("calibre")
    ebooks = types.ModuleType("calibre.ebooks")
    metadata = types.ModuleType("calibre.ebooks.metadata")
    meta = types.ModuleType("calibre.ebooks.metadata.meta")
    meta.get_metadata = MagicMock(return_value=MagicMock(name="Metadata"))
    sys.modules["calibre"] = calibre
    sys.modules["calibre.ebooks"] = ebooks
    sys.modules["calibre.ebooks.metadata"] = metadata
    sys.modules["calibre.ebooks.metadata.meta"] = meta
    yield
    for name in [
        "calibre.ebooks.metadata.meta",
        "calibre.ebooks.metadata",
        "calibre.ebooks",
        "calibre",
    ]:
        sys.modules.pop(name, None)


def _load_adder():
    plugin_dir = pathlib.Path(__file__).resolve().parent.parent / "plugin"
    sys.path.insert(0, str(plugin_dir))
    try:
        sys.modules.pop("adder", None)
        return importlib.import_module("adder")
    finally:
        sys.path.pop(0)


def _make_db():
    db = MagicMock()
    db.new_api.add_books.return_value = ([1], [])
    return db


def test_path_traversal_dotdot_rejected():
    adder = _load_adder()
    with pytest.raises(ValueError, match="traversal"):
        adder.add_book(_make_db(), "../etc/passwd")


def test_path_traversal_nested_dotdot_rejected():
    adder = _load_adder()
    with pytest.raises(ValueError, match="traversal"):
        adder.add_book(_make_db(), "books/../../etc/shadow")


def test_normal_path_not_rejected(tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"epub")
    adder = _load_adder()
    db = _make_db()
    try:
        adder.add_book(db, str(book))
    except ValueError as e:
        if "traversal" in str(e).lower():
            pytest.fail(f"Normal path incorrectly rejected: {e}")
    except Exception:
        pass
