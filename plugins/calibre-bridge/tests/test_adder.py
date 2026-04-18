import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_calibre(monkeypatch):
    calibre = types.ModuleType('calibre')
    ebooks = types.ModuleType('calibre.ebooks')
    metadata = types.ModuleType('calibre.ebooks.metadata')
    meta = types.ModuleType('calibre.ebooks.metadata.meta')

    def get_metadata(stream, fmt):
        mi = MagicMock(name='Metadata')
        mi.title = 'Stub Title'
        return mi

    meta.get_metadata = get_metadata
    sys.modules['calibre'] = calibre
    sys.modules['calibre.ebooks'] = ebooks
    sys.modules['calibre.ebooks.metadata'] = metadata
    sys.modules['calibre.ebooks.metadata.meta'] = meta
    yield
    for name in [
        'calibre.ebooks.metadata.meta',
        'calibre.ebooks.metadata',
        'calibre.ebooks',
        'calibre',
    ]:
        sys.modules.pop(name, None)


def _load_adder():
    import importlib
    import pathlib

    plugin_dir = pathlib.Path(__file__).resolve().parent.parent / 'plugin'
    sys.path.insert(0, str(plugin_dir))
    try:
        if 'adder' in sys.modules:
            del sys.modules['adder']
        return importlib.import_module('adder')
    finally:
        sys.path.pop(0)


def test_add_book_happy_path(tmp_path):
    adder = _load_adder()
    book = tmp_path / 'book.epub'
    book.write_bytes(b'stub epub bytes')

    db = MagicMock()
    db.new_api.add_books.return_value = ([42], {})

    book_id, duplicate = adder.add_book(db, str(book))

    assert book_id == 42
    assert duplicate is False
    db.new_api.add_books.assert_called_once()


def test_add_book_duplicate(tmp_path):
    """Calibre's add_books returns dups as a list of (mi, format_map) tuples
    — the ORIGINAL inputs, not book ids. The adder must look up the existing
    library id via find_identical_books. v0.3.0 returned the raw tuple, which
    crashed the handler with TypeError on int() coercion and produced an
    empty TCP reply to the caller."""
    adder = _load_adder()
    book = tmp_path / 'book.epub'
    book.write_bytes(b'stub epub bytes')

    db = MagicMock()
    mi = MagicMock(name='Metadata')
    db.new_api.add_books.return_value = ([], [(mi, {'EPUB': str(book)})])
    db.new_api.find_identical_books.return_value = {7}

    book_id, duplicate = adder.add_book(db, str(book))

    assert book_id == 7
    assert duplicate is True


def test_add_book_duplicate_no_identical_match(tmp_path):
    """Fallback: if find_identical_books returns empty, return id=0 rather
    than raising — caller still sees a valid 409 response."""
    adder = _load_adder()
    book = tmp_path / 'book.epub'
    book.write_bytes(b'stub epub bytes')

    db = MagicMock()
    db.new_api.add_books.return_value = ([], [(MagicMock(), {'EPUB': str(book)})])
    db.new_api.find_identical_books.return_value = set()

    book_id, duplicate = adder.add_book(db, str(book))

    assert book_id == 0
    assert duplicate is True
