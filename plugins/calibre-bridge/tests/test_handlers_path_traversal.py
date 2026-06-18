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


def test_absolute_path_outside_ingest_root_rejected(tmp_path):
    """An absolute path that contains no `..` still escapes a configured root.

    The old guard only blocked `..`, so `/etc/passwd` sailed through. With an
    ingest_root set, anything outside it (including absolute paths) is 400.
    """
    adder = _load_adder()
    root = tmp_path / "ingest"
    root.mkdir()
    with pytest.raises(ValueError, match="outside ingest root"):
        adder.add_book(_make_db(), "/etc/passwd", ingest_root=str(root))


def test_symlink_escape_outside_ingest_root_rejected(tmp_path):
    """A symlink that lives inside the root but resolves outside it must be
    rejected — that is why we resolve() the real path."""
    adder = _load_adder()
    root = tmp_path / "ingest"
    root.mkdir()
    secret = tmp_path / "secret.epub"
    secret.write_bytes(b"secret")
    link = root / "inside.epub"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="outside ingest root"):
        adder.add_book(_make_db(), str(link), ingest_root=str(root))


def test_path_inside_ingest_root_allowed(tmp_path):
    adder = _load_adder()
    root = tmp_path / "ingest"
    root.mkdir()
    book = root / "book.epub"
    book.write_bytes(b"epub")
    db = _make_db()
    # Should not raise the containment ValueError for a path inside the root.
    adder.add_book(db, str(book), ingest_root=str(root))
