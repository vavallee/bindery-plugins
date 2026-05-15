import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_calibre(monkeypatch):
    calibre = types.ModuleType("calibre")
    utils = types.ModuleType("calibre.utils")
    date = types.ModuleType("calibre.utils.date")
    ebooks = types.ModuleType("calibre.ebooks")
    metadata = types.ModuleType("calibre.ebooks.metadata")
    meta = types.ModuleType("calibre.ebooks.metadata.meta")

    def get_metadata(stream, fmt):
        mi = MagicMock(name="Metadata")
        mi.title = "Stub Title"
        mi.rating = 8
        return mi

    meta.get_metadata = get_metadata
    date.parse_date = lambda value: f"parsed:{value}"
    sys.modules["calibre"] = calibre
    sys.modules["calibre.utils"] = utils
    sys.modules["calibre.utils.date"] = date
    sys.modules["calibre.ebooks"] = ebooks
    sys.modules["calibre.ebooks.metadata"] = metadata
    sys.modules["calibre.ebooks.metadata.meta"] = meta
    yield
    for name in [
        "calibre.ebooks.metadata.meta",
        "calibre.ebooks.metadata",
        "calibre.ebooks",
        "calibre.utils.date",
        "calibre.utils",
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


class _CollisionNewAPI:
    """Fake the Calibre behavior from the live Complete Poems collision.

    If add_duplicates is False, later books with the same title are rejected
    as duplicates of the first title match, even when authors differ.
    """

    def __init__(self):
        self._next_id = 1
        self._first_id_by_title = {}
        self._identifiers_by_book = {}
        self.add_calls = []
        self.find_identical_books_calls = []

    def add_books(self, books, add_duplicates, run_hooks):
        mi, format_map = books[0]
        identifiers = _metadata_identifiers(mi)
        self.add_calls.append(
            {
                "title": mi.title,
                "authors": list(mi.authors),
                "identifiers": identifiers,
                "add_duplicates": add_duplicates,
                "run_hooks": run_hooks,
                "format_map": format_map,
            }
        )
        if not add_duplicates and mi.title in self._first_id_by_title:
            return [], [(mi, format_map)]

        book_id = self._next_id
        self._next_id += 1
        self._first_id_by_title.setdefault(mi.title, book_id)
        self._identifiers_by_book[book_id] = identifiers
        return [book_id], []

    def find_identical_books(self, mi):
        self.find_identical_books_calls.append(mi)
        book_id = self._first_id_by_title.get(mi.title)
        return set() if book_id is None else {book_id}

    def all_book_ids(self):
        return frozenset(self._identifiers_by_book)

    def all_field_for(self, field, book_ids, default_value=None):
        assert field == "identifiers"
        return {
            book_id: self._identifiers_by_book.get(book_id, default_value or {})
            for book_id in book_ids
        }


class _CollisionDB:
    def __init__(self):
        self.new_api = _CollisionNewAPI()


def _metadata_identifiers(mi):
    if not mi.set_identifiers.called:
        return {}
    return dict(mi.set_identifiers.call_args.args[0])


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
    """Calibre's add_books returns dups as a list of (mi, format_map) tuples
    — the ORIGINAL inputs, not book ids. The adder must look up the existing
    library id via find_identical_books. v0.3.0 returned the raw tuple, which
    crashed the handler with TypeError on int() coercion and produced an
    empty TCP reply to the caller."""
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    mi = MagicMock(name="Metadata")
    db.new_api.add_books.return_value = ([], [(mi, {"EPUB": str(book)})])
    db.new_api.find_identical_books.return_value = {7}

    book_id, duplicate = adder.add_book(db, str(book))

    assert book_id == 7
    assert duplicate is True


def test_add_book_duplicate_no_identical_match(tmp_path):
    """Fallback: if find_identical_books returns empty, return id=0 rather
    than raising — caller still sees a valid 409 response."""
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    db.new_api.add_books.return_value = ([], [(MagicMock(), {"EPUB": str(book)})])
    db.new_api.find_identical_books.return_value = set()

    book_id, duplicate = adder.add_book(db, str(book))

    assert book_id == 0
    assert duplicate is True


def test_add_book_applies_bindery_metadata(tmp_path):
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    db.new_api.add_books.return_value = ([42], {})
    metadata = {
        "title": "Dune",
        "authors": ["Frank Herbert", "Frank Herbert", " "],
        "authorSort": "Herbert, Frank",
        "description": "Desert planet.",
        "publisher": "Ace",
        "publishedDate": "1965-08-01",
        "genres": ["Science Fiction", "Classics", "science fiction"],
        "language": "eng",
        "series": "Dune Chronicles",
        "seriesIndex": "1.5",
        "rating": 4.6,
        "identifiers": {
            "asin": "B000FC1BN8",
            "bindery": "42",
            "empty": " ",
        },
    }

    book_id, duplicate = adder.add_book(db, str(book), metadata=metadata)

    assert book_id == 42
    assert duplicate is False
    mi = db.new_api.add_books.call_args.args[0][0][0]
    assert mi.title == "Dune"
    assert mi.authors == ["Frank Herbert"]
    assert mi.author_sort == "Herbert, Frank"
    assert mi.comments == "Desert planet."
    assert mi.publisher == "Ace"
    assert mi.pubdate == "parsed:1965-08-01"
    assert mi.tags == ["Science Fiction", "Classics"]
    assert mi.languages == ["eng"]
    assert mi.series == "Dune Chronicles"
    assert mi.series_index == 1.5
    assert mi.rating == 9
    mi.set_identifiers.assert_called_once_with({"asin": "B000FC1BN8", "bindery": "42"})


def test_add_book_duplicate_with_metadata_does_not_update_existing_book(tmp_path):
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    db.new_api.add_books.return_value = ([], [(MagicMock(), {"EPUB": str(book)})])
    db.new_api.find_identical_books.return_value = {7}

    book_id, duplicate = adder.add_book(db, str(book), metadata={"title": "Dune"})

    assert book_id == 7
    assert duplicate is True
    assert not db.new_api.set_metadata.called


def test_add_book_applies_zero_rating(tmp_path):
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    db.new_api.add_books.return_value = ([42], {})

    book_id, duplicate = adder.add_book(db, str(book), metadata={"rating": 0})

    assert book_id == 42
    assert duplicate is False
    mi = db.new_api.add_books.call_args.args[0][0][0]
    assert mi.rating == 0


def test_bindery_identifier_prevents_same_title_author_collision(tmp_path):
    """Regression for the live Complete Poems collision.

    Calibre's fuzzy duplicate detector can reject same-title books even when
    Bindery supplies distinct author metadata. Bindery-origin plugin sync must
    use the bindery identifier as the exact idempotency key instead.
    """
    adder = _load_adder()
    db = _CollisionDB()
    books = [
        ("715", "Emily Brontë", "9780141966762", "mobi"),
        ("852", "Anne Sexton", "9781504034364", "azw3"),
        ("921", "William Blake", "9780140422153", "epub"),
    ]

    first_pass_ids = []
    for bindery_id, author, isbn, ext in books:
        path = tmp_path / f"{bindery_id}.{ext}"
        path.write_bytes(b"stub book bytes")
        book_id, duplicate = adder.add_book(
            db,
            str(path),
            metadata={
                "title": "The Complete Poems",
                "authors": [author],
                "identifiers": {
                    "bindery": bindery_id,
                    "isbn": isbn,
                },
            },
        )

        assert duplicate is False
        first_pass_ids.append(book_id)

    assert len(set(first_pass_ids)) == 3
    assert db.new_api.find_identical_books_calls == []
    assert [call["add_duplicates"] for call in db.new_api.add_calls] == [True, True, True]
    assert [call["authors"] for call in db.new_api.add_calls] == [
        ["Emily Brontë"],
        ["Anne Sexton"],
        ["William Blake"],
    ]
    identifiers_by_book = db.new_api.all_field_for("identifiers", first_pass_ids, default_value={})
    assert identifiers_by_book[first_pass_ids[0]]["bindery"] == "715"
    assert identifiers_by_book[first_pass_ids[1]]["bindery"] == "852"
    assert identifiers_by_book[first_pass_ids[2]]["bindery"] == "921"

    for (bindery_id, author, isbn, ext), expected_id in zip(books, first_pass_ids, strict=True):
        path = tmp_path / f"{bindery_id}.{ext}"
        book_id, duplicate = adder.add_book(
            db,
            str(path),
            metadata={
                "title": "The Complete Poems",
                "authors": [author],
                "identifiers": {
                    "bindery": bindery_id,
                    "isbn": isbn,
                },
            },
        )

        assert book_id == expected_id
        assert duplicate is True

    assert len(db.new_api.add_calls) == 3


def test_bindery_identifier_lookup_failure_does_not_add_book(tmp_path):
    adder = _load_adder()
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub epub bytes")

    db = MagicMock()
    db.new_api.all_book_ids.return_value = {1}
    db.new_api.all_field_for.side_effect = RuntimeError("identifier lookup unavailable")

    with pytest.raises(RuntimeError, match="identifier lookup unavailable"):
        adder.add_book(db, str(book), metadata={"identifiers": {"bindery": "42"}})

    db.new_api.add_books.assert_not_called()
