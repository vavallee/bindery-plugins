"""A failed add left an empty row that every later push took for a duplicate.

Calibre's ``add_books`` writes the book row before it copies the file. When
the copy failed (the long share path bug fixed in 0.6.1), the row stayed with
no format, and the next push matched it on the ``bindery`` identifier and
reported "already in Calibre", so the book never got its file. Seen on a real
library: 15 formatless rows after one Push all.
"""

import pytest

from .conftest import StubMetadata
from .test_dedupe_ladder import FakeDB, FakeLibrary, FakeMetadata, _book


@pytest.fixture
def adder(bridge_adder):
    """The real adder, with calibre's format sniffing replaced by a stub."""
    bridge_adder.get_metadata = lambda stream, fmt: FakeMetadata()
    return bridge_adder


class FormatLibrary(FakeLibrary):
    def __init__(self):
        super().__init__()
        self.formats_by_book = {}
        self.add_format_calls = []
        self.add_format_kwargs = []
        self.set_metadata_calls = []
        self.set_cover_calls = []
        self.removed = []
        self.fail_copy = False

    def add_books(self, books, add_duplicates, run_hooks):
        mi, format_map = books[0]
        book_ids, dups = super().add_books(books, add_duplicates, run_hooks)
        if self.fail_copy:
            # The row is already written when the copy fails, as in Calibre.
            raise OSError(22, "Invalid argument")
        for book_id in book_ids:
            self.formats_by_book[book_id] = set(format_map)
        return book_ids, dups

    def formats(self, book_id):
        return tuple(sorted(self.formats_by_book.get(book_id, ())))

    def field_for(self, name, book_id):
        assert name == "identifiers"
        return dict(self.rows[book_id]["identifiers"])

    def add_format(self, book_id, fmt, path, replace=True, run_hooks=True):
        """Calibre's Cache.add_format: False when replace=False meets an existing format."""
        self.add_format_calls.append((book_id, fmt, path))
        self.add_format_kwargs.append({"replace": replace, "run_hooks": run_hooks})
        have = self.formats_by_book.setdefault(book_id, set())
        if fmt in have and not replace:
            return False
        have.add(fmt)
        return True

    def get_metadata(self, book_id, **kwargs):
        row = self.rows[book_id]
        mi = StubMetadata()
        mi.title = row["title"]
        mi.authors = list(row["authors"])
        mi.set_identifiers(row["identifiers"])
        for key, value in row.get("fields", {}).items():
            setattr(mi, key, value)
        return mi

    def set_metadata(self, book_id, mi, **kwargs):
        self.set_metadata_calls.append((book_id, mi))
        row = self.rows[book_id]
        row["title"] = mi.title
        row["authors"] = list(mi.authors)
        row["identifiers"] = mi.get_identifiers()
        row["fields"] = {
            "comments": mi.comments,
            "publisher": mi.publisher,
            "series": mi.series,
            "series_index": mi.series_index,
            "tags": list(mi.tags),
        }

    def set_cover(self, book_id_data_map):
        self.set_cover_calls.append(dict(book_id_data_map))

    def remove_books(self, book_ids):
        for book_id in book_ids:
            self.removed.append(book_id)
            self.rows.pop(book_id, None)
            self.formats_by_book.pop(book_id, None)


def _push(adder, lib, path, bindery_id="42", **extra):
    ids = {"bindery": bindery_id, **extra}
    return adder.add_book(
        FakeDB(lib),
        path,
        metadata={"title": "Eight Stories", "authors": ["Isaac Asimov"], "identifiers": ids},
    )


def test_empty_row_from_an_earlier_failure_gets_the_file(adder, tmp_path):
    lib = FormatLibrary()
    ghost = lib.seed("Eight Stories", ["Isaac Asimov"], {"bindery": "42"})

    book_id, duplicate = _push(adder, lib, _book(tmp_path))

    assert (book_id, duplicate) == (ghost, False)
    assert lib.formats(ghost) == ("EPUB",)
    assert lib.add_calls == []


def test_a_row_with_a_file_is_still_a_duplicate(adder, tmp_path):
    lib = FormatLibrary()
    existing = lib.seed("Eight Stories", ["Isaac Asimov"], {"bindery": "42"})
    lib.formats_by_book[existing] = {"PDF"}

    assert _push(adder, lib, _book(tmp_path)) == (existing, True)
    assert lib.add_format_calls == []


def test_an_empty_row_matched_on_isbn_is_not_filled(adder, tmp_path):
    """A formatless row the user made (a wishlist entry) is left alone."""
    lib = FormatLibrary()
    wishlist = lib.seed("Eight Stories", ["Isaac Asimov"], {"isbn": "9780586025"})

    assert _push(adder, lib, _book(tmp_path), isbn="9780586025") == (wishlist, True)
    assert lib.add_format_calls == []


def test_a_failed_copy_removes_the_row_it_left(adder, tmp_path):
    lib = FormatLibrary()
    lib.fail_copy = True

    with pytest.raises(OSError):
        _push(adder, lib, _book(tmp_path))

    assert lib.rows == {}
    assert len(lib.removed) == 1


def test_a_failed_copy_never_removes_someone_elses_row(adder, tmp_path):
    lib = FormatLibrary()
    other = lib.seed("Other Book", ["Someone"], {"bindery": "7"})
    lib.fail_copy = True

    with pytest.raises(OSError):
        _push(adder, lib, _book(tmp_path))

    assert other in lib.rows
    assert other not in lib.removed
