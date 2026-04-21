import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_calibre(monkeypatch):
    calibre = types.ModuleType("calibre")
    ebooks = types.ModuleType("calibre.ebooks")
    metadata = types.ModuleType("calibre.ebooks.metadata")
    meta = types.ModuleType("calibre.ebooks.metadata.meta")

    def get_metadata(stream, fmt):
        mi = MagicMock(name="Metadata")
        mi.title = "Stub Title"
        return mi

    meta.get_metadata = get_metadata
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
    import importlib
    import pathlib

    plugin_dir = pathlib.Path(__file__).resolve().parent.parent / "plugin"
    sys.path.insert(0, str(plugin_dir))
    try:
        if "adder" in sys.modules:
            del sys.modules["adder"]
        return importlib.import_module("adder")
    finally:
        sys.path.pop(0)


def test_add_book_happy_path(tmp_path):
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    db.new_api.add_books.return_value = ([42], {})

    book_id, duplicate = adder.add_book(db, str(book))

    assert book_id == 42
    assert duplicate is False
    db.new_api.add_books.assert_called_once()


def test_add_book_duplicate(tmp_path):
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    db.new_api.add_books.return_value = ([], {7: "dup"})

    book_id, duplicate = adder.add_book(db, str(book))

    assert book_id == 7
    assert duplicate is True
