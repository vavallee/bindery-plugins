"""calibre-bridge 0.7.0: a second format of the same Bindery book.

When a Bindery book has an EPUB and a PDF, Bindery pushes each file on its
own with the same ``bindery`` identifier. Before 0.7.0 the second push matched
the first row on the dedupe ladder and came back 409, so the PDF never reached
Calibre (vavallee/bindery#2832).

With ``addFormat: true`` the second file joins that row, but only when the
ladder matched on the ``bindery`` rung and the row does not have the format
yet. A match on any other rung can be a book the user curated, and it never
gets a file added.
"""

from unittest.mock import MagicMock

import pytest

from .test_dedupe_ladder import FakeDB, FakeMetadata, _book
from .test_empty_rows import FormatLibrary

META = {
    "title": "Eight Stories",
    "authors": ["Isaac Asimov"],
    "description": "Stories.",
    "publisher": "Doubleday",
    "series": "Collected",
    "seriesIndex": 2,
    "identifiers": {"bindery": "42"},
}


@pytest.fixture
def adder(bridge_adder):
    bridge_adder.get_metadata = lambda stream, fmt: FakeMetadata()
    return bridge_adder


def _meta(**identifiers):
    meta = dict(META)
    meta["identifiers"] = {"bindery": "42", **identifiers}
    return meta


def _push(adder, lib, path, add_format=True, metadata=None, **kwargs):
    return adder.add_book_detailed(
        FakeDB(lib),
        path,
        metadata=metadata if metadata is not None else _meta(),
        add_format=add_format,
        **kwargs,
    )


def _epub_then(adder, lib, tmp_path):
    """Push an EPUB the normal way, as Bindery's first file would be."""
    first = _push(adder, lib, _book(tmp_path, "book.epub"), add_format=False)
    assert first.duplicate is False
    return first.book_id


# ── adder ─────────────────────────────────────────────────────────────────────


def test_a_pdf_after_an_epub_joins_the_same_row(adder, tmp_path):
    lib = FormatLibrary()
    book_id = _epub_then(adder, lib, tmp_path)
    refreshed = []

    result = _push(adder, lib, _book(tmp_path, "book.pdf"), on_added=refreshed.append)

    assert (result.book_id, result.duplicate, result.format_added) == (book_id, False, True)
    assert lib.formats(book_id) == ("EPUB", "PDF")
    assert len(lib.rows) == 1
    assert lib.add_format_kwargs == [{"replace": False, "run_hooks": False}]
    assert refreshed == [1]
    # A row that already had a file keeps its cover.
    assert result.cover_applied is None
    assert lib.set_cover_calls == []


def test_the_same_format_again_is_a_duplicate(adder, tmp_path):
    lib = FormatLibrary()
    book_id = _epub_then(adder, lib, tmp_path)

    result = _push(adder, lib, _book(tmp_path, "again.epub"))

    assert (result.book_id, result.duplicate, result.format_added) == (book_id, True, False)
    assert lib.add_format_calls == []


def test_an_isbn_match_never_gets_a_format(adder, tmp_path):
    """A row found by ISBN may be one the user built by hand."""
    lib = FormatLibrary()
    curated = lib.seed("Eight Stories", ["Isaac Asimov"], {"isbn": "9780586025"})
    lib.formats_by_book[curated] = {"EPUB"}

    result = _push(adder, lib, _book(tmp_path, "book.pdf"), metadata=_meta(isbn="9780586025"))

    assert (result.book_id, result.duplicate) == (curated, True)
    assert lib.add_format_calls == []
    assert lib.formats(curated) == ("EPUB",)


def test_a_title_and_author_match_never_gets_a_format(adder, tmp_path):
    lib = FormatLibrary()
    curated = lib.seed("Stub Title", ["Isaac Asimov"], {})
    lib.formats_by_book[curated] = {"EPUB"}
    meta = _meta()
    meta.pop("title")

    result = _push(adder, lib, _book(tmp_path, "book.pdf"), metadata=meta)

    assert (result.book_id, result.duplicate) == (curated, True)
    assert lib.add_format_calls == []


def test_without_add_format_a_second_format_is_still_a_duplicate(adder, tmp_path):
    lib = FormatLibrary()
    book_id = _epub_then(adder, lib, tmp_path)

    result = _push(adder, lib, _book(tmp_path, "book.pdf"), add_format=False)

    assert (result.book_id, result.duplicate, result.format_added) == (book_id, True, False)
    assert lib.add_format_calls == []
    assert lib.formats(book_id) == ("EPUB",)


def test_the_joined_row_gets_the_fill_only_update(adder, tmp_path):
    lib = FormatLibrary()
    book_id = lib.seed("Eight Stories", ["Isaac Asimov"], {"bindery": "42"})
    lib.formats_by_book[book_id] = {"EPUB"}
    lib.rows[book_id]["fields"] = {"publisher": "The user's own publisher"}

    result = _push(adder, lib, _book(tmp_path, "book.pdf"))

    assert result.format_added is True
    fields = lib.rows[book_id]["fields"]
    assert fields["comments"] == "Stories."
    assert fields["series"] == "Collected"
    # Fill only: what the row already had stays.
    assert fields["publisher"] == "The user's own publisher"


def test_empty_row_repair_still_works_without_add_format(adder, tmp_path):
    lib = FormatLibrary()
    ghost = lib.seed("Eight Stories", ["Isaac Asimov"], {"bindery": "42"})

    result = _push(adder, lib, _book(tmp_path), add_format=False)

    assert (result.book_id, result.duplicate, result.format_added) == (ghost, False, True)
    assert lib.formats(ghost) == ("EPUB",)
    assert lib.add_calls == []


def test_empty_row_repair_fills_metadata_and_sets_the_cover(adder, tmp_path):
    lib = FormatLibrary()
    ghost = lib.seed("Eight Stories", ["Isaac Asimov"], {"bindery": "42"})
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8 jpeg bytes")
    meta = _meta()
    meta["coverPath"] = str(cover)

    result = _push(adder, lib, _book(tmp_path), add_format=False, metadata=meta)

    assert result.cover_applied is True
    assert lib.set_cover_calls == [{ghost: b"\xff\xd8 jpeg bytes"}]
    fields = lib.rows[ghost]["fields"]
    assert fields["comments"] == "Stories."
    assert fields["publisher"] == "Doubleday"


def test_a_failed_add_format_is_copy_failed(adder, tmp_path):
    lib = FormatLibrary()
    book_id = _epub_then(adder, lib, tmp_path)

    def broken(book_id, fmt, path, replace=True, run_hooks=True):
        raise OSError(22, "Invalid argument")

    lib.add_format = broken
    path = _book(tmp_path, "book.pdf")

    with pytest.raises(adder.CopyFailed) as info:
        _push(adder, lib, path)

    assert path in str(info.value)
    # The row predates this request and still has its EPUB.
    assert book_id in lib.rows
    assert lib.removed == []


def test_a_race_that_lost_to_another_push_is_a_duplicate(adder, tmp_path):
    """Calibre returns False from add_format when replace=False meets the format."""
    lib = FormatLibrary()
    book_id = _epub_then(adder, lib, tmp_path)
    lib.formats = lambda _id: ("EPUB",)  # stale read: the PDF lands in between
    lib.formats_by_book[book_id].add("PDF")

    result = _push(adder, lib, _book(tmp_path, "book.pdf"))

    assert (result.book_id, result.duplicate) == (book_id, True)


def test_the_ladder_reports_its_rung(adder):
    lib = FormatLibrary()
    by_bindery = lib.seed("A", ["X"], {"bindery": "1"})
    by_isbn = lib.seed("B", ["Y"], {"isbn": "978"})
    by_title = lib.seed("Stub Title", ["Z"], {})

    def match(identifiers, authors=("Z",)):
        mi = FakeMetadata()
        mi.authors = list(authors)
        return adder._match_existing_book(lib, identifiers, mi)

    assert match({"bindery": "1"}) == (by_bindery, "bindery")
    assert match({"bindery": "9", "isbn": "978"}) == (by_isbn, "isbn")
    assert match({"bindery": "9"}) == (by_title, "title_authors")
    assert match({"bindery": "9"}, authors=("Nobody",)) == (0, "")
    # The old int API is unchanged for anything that still calls it.
    mi = FakeMetadata()
    assert adder._existing_book_id(lib, {"isbn": "978"}, mi) == by_isbn


# ── on the wire ───────────────────────────────────────────────────────────────


def _wire_db():
    db = MagicMock()
    db.new_api.search.return_value = {7}
    db.new_api.formats.return_value = ("EPUB",)
    db.new_api.add_format.return_value = True
    db.new_api.get_metadata.return_value = MagicMock()
    # A new row, for the requests that never match.
    db.new_api.add_books.return_value = ([9], [])
    return db


def _post(bridge_handlers, serve_bridge, db, body):
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    return bridge.call("POST", "/v1/books", body=body)


def test_add_format_on_the_wire_is_201_format_added(bridge_handlers, serve_bridge, tmp_path):
    db = _wire_db()
    status, payload, _ = _post(
        bridge_handlers,
        serve_bridge,
        db,
        {
            "path": _book(tmp_path, "book.pdf"),
            "addFormat": True,
            "metadata": {"identifiers": {"bindery": "42"}},
        },
    )
    assert status == 201
    assert payload == {"id": 7, "duplicate": False, "format_added": True}
    db.new_api.add_format.assert_called_once()


def test_without_add_format_the_wire_shape_is_unchanged(bridge_handlers, serve_bridge, tmp_path):
    db = _wire_db()
    status, payload, _ = _post(
        bridge_handlers,
        serve_bridge,
        db,
        {"path": _book(tmp_path, "book.pdf"), "metadata": {"identifiers": {"bindery": "42"}}},
    )
    assert status == 409
    assert payload == {"id": 7, "duplicate": True}
    db.new_api.add_format.assert_not_called()


def test_add_format_must_be_a_boolean(bridge_handlers, serve_bridge, tmp_path):
    db = _wire_db()
    status, payload, _ = _post(
        bridge_handlers,
        serve_bridge,
        db,
        {"path": _book(tmp_path, "book.pdf"), "addFormat": "yes"},
    )
    assert (status, payload.get("code")) == (400, "invalid_metadata")
    db.new_api.add_format.assert_not_called()


def test_add_format_failure_is_500_copy_failed(bridge_handlers, serve_bridge, tmp_path):
    db = _wire_db()
    db.new_api.add_format.side_effect = PermissionError(13, "Permission denied")
    path = _book(tmp_path, "book.pdf")
    status, payload, _ = _post(
        bridge_handlers,
        serve_bridge,
        db,
        {"path": path, "addFormat": True, "metadata": {"identifiers": {"bindery": "42"}}},
    )
    assert (status, payload.get("code")) == (500, "copy_failed")
    assert path in payload["error"]


def test_health_advertises_add_format(bridge_handlers, serve_bridge):
    db = MagicMock()
    db.library_path = "/tmp/library"
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="k", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", "/v1/health")
    assert status == 200
    assert "add_format" in payload["capabilities"]
    assert payload["plugin_version"] == "0.7.0"
