"""Contract tests for calibre-bridge 0.6.0.

Covers the four additions to the wire protocol: machine readable error codes,
``GET /v1/paths``, ``metadata.coverPath`` on ``POST /v1/books``, and
``PATCH /v1/books/{id}``. Plus the timing safe bearer comparison and the 503
library swap window.
"""

import os
from unittest.mock import MagicMock

import pytest

EXPECTED_CAPABILITIES = {
    "book_metadata",
    "cover",
    "path_probe",
    "metadata_update",
    "error_codes",
}


class FakeMetadata:
    """Stand in for calibre's ``Metadata`` with the fields the bridge touches."""

    def __init__(self, **kwargs):
        self.title = kwargs.get("title", "Unknown")
        self.authors = kwargs.get("authors", ["Unknown"])
        self.author_sort = kwargs.get("author_sort", "")
        self.comments = kwargs.get("comments")
        self.publisher = kwargs.get("publisher")
        self.pubdate = kwargs.get("pubdate")
        self.tags = kwargs.get("tags", [])
        self.languages = kwargs.get("languages", [])
        self.series = kwargs.get("series")
        self.series_index = kwargs.get("series_index", 1.0)
        self.rating = kwargs.get("rating")
        self._identifiers = dict(kwargs.get("identifiers", {}))
        self.cover_data = (None, None)
        self.cover = None

    def get_identifiers(self):
        return dict(self._identifiers)

    def set_identifiers(self, value):
        self._identifiers = dict(value)


class FakeNewAPI:
    def __init__(self, books=None):
        self.books = dict(books or {})
        self.set_metadata_calls = []
        self.set_cover_calls = []

    def all_book_ids(self):
        return set(self.books)

    def get_metadata(self, book_id, **kwargs):
        return self.books[book_id]

    def set_metadata(self, book_id, mi, **kwargs):
        self.set_metadata_calls.append((book_id, mi))
        self.books[book_id] = mi

    def set_cover(self, book_id_data_map):
        self.set_cover_calls.append(book_id_data_map)


class FakeDB:
    def __init__(self, books=None):
        self.new_api = FakeNewAPI(books)
        self.library_path = "/tmp/library"


def _epub(tmp_path, name="book.epub"):
    path = tmp_path / name
    path.write_bytes(b"stub epub bytes")
    return str(path)


# ── health capabilities ───────────────────────────────────────────────────────


def test_health_advertises_0_6_capabilities(bridge_handlers, serve_bridge):
    db = MagicMock()
    db.library_path = "/tmp/library"
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="k", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", "/v1/health")
    assert status == 200
    assert EXPECTED_CAPABILITIES.issubset(set(payload["capabilities"]))
    assert payload["plugin_version"] == "0.6.0"


# ── error codes ───────────────────────────────────────────────────────────────


def test_bearer_check_is_timing_safe(bridge_handlers):
    """The token comparison must not short circuit on the first wrong byte."""
    import inspect

    source = inspect.getsource(bridge_handlers)
    assert "compare_digest" in source, "bearer check still uses a plain != comparison"
    assert "!= api_key" not in source
    # and it still has to work
    assert bridge_handlers._tokens_match("abc", "abc") is True
    assert bridge_handlers._tokens_match("abc", "abd") is False
    assert bridge_handlers._tokens_match("k\u00e9y", "k\u00e9y") is True


def test_unauthorized_carries_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": "/x.epub"}, token="nope")
    assert status == 401
    assert payload["error"] == "unauthorized"
    assert payload["code"] == "unauthorized"


def test_not_found_carries_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", "/nope")
    assert status == 404
    assert payload["error"] == "not found"
    assert payload["code"] == "not_found"


def test_db_unavailable_carries_code_and_retry_after(bridge_handlers, serve_bridge):
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: None))
    status, payload, headers = bridge.call("POST", "/v1/books", body={"path": "/x.epub"})
    assert status == 503
    assert payload["error"] == "library not ready"
    assert payload["code"] == "db_unavailable"
    assert int(headers["Retry-After"]) >= 1


def test_invalid_json_carries_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("POST", "/v1/books", raw_body=b"{not json")
    assert status == 400
    assert payload["error"] == "invalid json"
    assert payload["code"] == "invalid_json"


def test_missing_path_carries_invalid_metadata_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("POST", "/v1/books", body={})
    assert status == 400
    assert payload["error"] == "path required"
    assert payload["code"] == "invalid_metadata"


def test_non_object_metadata_carries_invalid_metadata_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call(
        "POST", "/v1/books", body={"path": "/x.epub", "metadata": "nope"}
    )
    assert status == 400
    assert payload["error"] == "metadata must be an object"
    assert payload["code"] == "invalid_metadata"


def test_missing_file_carries_path_not_found_code(bridge_handlers, serve_bridge, tmp_path):
    """The bug this closes: a wrong container mount used to look like bad metadata."""
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    missing = str(tmp_path / "gone.epub")
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": missing})
    assert status == 400
    assert payload["code"] == "path_not_found"
    assert "No such file" in payload["error"]


def test_traversal_carries_path_forbidden_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": "../etc/passwd"})
    assert status == 400
    assert payload["code"] == "path_forbidden"
    assert "traversal" in payload["error"]


def test_outside_ingest_root_carries_path_forbidden_code(bridge_handlers, serve_bridge, tmp_path):
    root = tmp_path / "ingest"
    root.mkdir()
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"x")
    db = MagicMock()
    bridge = serve_bridge(
        bridge_handlers.make_handler(api_key="", get_db=lambda: db, ingest_root=str(root))
    )
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": str(outside)})
    assert status == 400
    assert payload["code"] == "path_forbidden"
    assert "outside ingest root" in payload["error"]


def test_extensionless_path_carries_bad_format_code(bridge_handlers, serve_bridge, tmp_path):
    folder = tmp_path / "Audiobook Folder"
    folder.mkdir()
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": str(folder)})
    assert status == 400
    assert payload["code"] == "bad_format"
    assert "Cannot determine book format" in payload["error"]


def test_oversized_body_carries_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(
        bridge_handlers.make_handler(api_key="", get_db=lambda: db, max_body_bytes=10)
    )
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": "/" + "x" * 200})
    assert status == 413
    assert payload["error"] == "payload too large"
    assert payload["code"] == "body_too_large"


def test_malformed_content_length_carries_code(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call(
        "POST", "/v1/books", raw_body=b"{}", headers={"Content-Length": "abc"}
    )
    assert status == 400
    assert payload["error"] == "invalid Content-Length"
    assert payload["code"] == "invalid_json"


def test_internal_error_carries_code(bridge_handlers, serve_bridge, tmp_path):
    db = MagicMock()
    db.new_api.add_books.side_effect = RuntimeError("disk on fire")
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": _epub(tmp_path)})
    assert status == 500
    assert payload["code"] == "internal"
    assert "disk on fire" in payload["error"]


# ── GET /v1/paths ─────────────────────────────────────────────────────────────


def test_paths_probe_requires_auth(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", "/v1/paths?path=/tmp")
    assert status == 401
    assert payload["code"] == "unauthorized"


def test_paths_probe_reports_an_existing_file(bridge_handlers, serve_bridge, tmp_path):
    book = _epub(tmp_path)
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", f"/v1/paths?path={book}", token="secret")
    assert status == 200
    assert payload == {"path": book, "exists": True, "readable": True, "isDir": False}


def test_paths_probe_reports_a_directory(bridge_handlers, serve_bridge, tmp_path):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", f"/v1/paths?path={tmp_path}", token="secret")
    assert status == 200
    assert payload["exists"] is True
    assert payload["isDir"] is True


def test_paths_probe_reports_a_missing_path(bridge_handlers, serve_bridge, tmp_path):
    missing = str(tmp_path / "nope.epub")
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", f"/v1/paths?path={missing}", token="secret")
    assert status == 200
    assert payload == {"path": missing, "exists": False, "readable": False, "isDir": False}


def test_paths_probe_reports_unreadable(bridge_handlers, serve_bridge, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses the read permission bit")
    book = tmp_path / "locked.epub"
    book.write_bytes(b"x")
    book.chmod(0o000)
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    try:
        status, payload, _ = bridge.call("GET", f"/v1/paths?path={book}", token="secret")
    finally:
        book.chmod(0o600)
    assert status == 200
    assert payload["exists"] is True
    assert payload["readable"] is False


def test_paths_probe_applies_the_ingest_root(bridge_handlers, serve_bridge, tmp_path):
    root = tmp_path / "ingest"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("shh")
    db = MagicMock()
    bridge = serve_bridge(
        bridge_handlers.make_handler(api_key="secret", get_db=lambda: db, ingest_root=str(root))
    )
    status, payload, _ = bridge.call("GET", f"/v1/paths?path={outside}", token="secret")
    assert status == 400
    assert payload["code"] == "path_forbidden"


def test_paths_probe_rejects_traversal(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", "/v1/paths?path=../etc/passwd", token="secret")
    assert status == 400
    assert payload["code"] == "path_forbidden"


def test_paths_probe_requires_a_path_parameter(bridge_handlers, serve_bridge):
    db = MagicMock()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("GET", "/v1/paths", token="secret")
    assert status == 400
    assert payload["code"] == "invalid_metadata"


def test_paths_probe_answers_while_the_library_is_swapping(bridge_handlers, serve_bridge, tmp_path):
    """The probe exists to diagnose mounts, so it must not need a database."""
    book = _epub(tmp_path)
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: None))
    status, payload, _ = bridge.call("GET", f"/v1/paths?path={book}", token="secret")
    assert status == 200
    assert payload["exists"] is True


def test_paths_probe_never_reads_file_contents(
    bridge_handlers, serve_bridge, tmp_path, monkeypatch
):
    book = _epub(tmp_path)
    db = MagicMock()
    handlers_mod = bridge_handlers
    opened = []
    real_open = open

    def _tracking_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _tracking_open)
    bridge = serve_bridge(handlers_mod.make_handler(api_key="secret", get_db=lambda: db))
    status, _payload, _ = bridge.call("GET", f"/v1/paths?path={book}", token="secret")
    assert status == 200
    assert book not in opened


# ── covers ────────────────────────────────────────────────────────────────────


def test_post_applies_cover_path(bridge_handlers, serve_bridge, tmp_path):
    book = _epub(tmp_path)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff-jpeg-bytes")
    db = MagicMock()
    db.new_api.add_books.return_value = ([77], {})
    db.new_api.search.return_value = set()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call(
        "POST", "/v1/books", body={"path": book, "metadata": {"coverPath": str(cover)}}
    )
    assert status == 201
    assert payload["id"] == 77
    assert payload["cover_applied"] is True
    mi = db.new_api.add_books.call_args.args[0][0][0]
    assert mi.cover_data == ("jpeg", b"\xff\xd8\xff-jpeg-bytes")
    assert mi.cover == str(cover)


def test_missing_cover_does_not_fail_the_add(bridge_handlers, serve_bridge, tmp_path):
    """A cover is worth less than the book: report it, do not lose the book."""
    book = _epub(tmp_path)
    db = MagicMock()
    db.new_api.add_books.return_value = ([78], {})
    db.new_api.search.return_value = set()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call(
        "POST",
        "/v1/books",
        body={"path": book, "metadata": {"coverPath": str(tmp_path / "gone.jpg")}},
    )
    assert status == 201
    assert payload["id"] == 78
    assert payload["cover_applied"] is False


def test_cover_outside_ingest_root_is_refused_but_book_is_added(
    bridge_handlers, serve_bridge, tmp_path
):
    root = tmp_path / "ingest"
    root.mkdir()
    book = _epub(root)
    outside = tmp_path / "shadow"
    outside.write_bytes(b"root:x:")
    db = MagicMock()
    db.new_api.add_books.return_value = ([79], {})
    db.new_api.search.return_value = set()
    bridge = serve_bridge(
        bridge_handlers.make_handler(api_key="", get_db=lambda: db, ingest_root=str(root))
    )
    status, payload, _ = bridge.call(
        "POST", "/v1/books", body={"path": book, "metadata": {"coverPath": str(outside)}}
    )
    assert status == 201
    assert payload["cover_applied"] is False
    mi = db.new_api.add_books.call_args.args[0][0][0]
    assert mi.cover_data == (None, None)


def test_no_cover_path_omits_the_cover_field(bridge_handlers, serve_bridge, tmp_path):
    """Old clients send no coverPath and must see the 0.5.0 response shape."""
    book = _epub(tmp_path)
    db = MagicMock()
    db.new_api.add_books.return_value = ([80], {})
    db.new_api.search.return_value = set()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": book})
    assert status == 201
    assert payload == {"id": 80, "duplicate": False}


# ── PATCH /v1/books/{id} ──────────────────────────────────────────────────────


def test_patch_requires_auth(bridge_handlers, serve_bridge):
    db = FakeDB({1: FakeMetadata()})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="secret", get_db=lambda: db))
    status, payload, _ = bridge.call("PATCH", "/v1/books/1", body={"title": "Dune"})
    assert status == 401
    assert payload["code"] == "unauthorized"


def test_patch_missing_book_returns_not_found(bridge_handlers, serve_bridge):
    db = FakeDB({1: FakeMetadata()})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("PATCH", "/v1/books/999", body={"title": "Dune"})
    assert status == 404
    assert payload["code"] == "not_found"


def test_patch_non_numeric_id_returns_not_found(bridge_handlers, serve_bridge):
    db = FakeDB({1: FakeMetadata()})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("PATCH", "/v1/books/abc", body={"title": "Dune"})
    assert status == 404
    assert payload["code"] == "not_found"


def test_patch_fills_empty_fields(bridge_handlers, serve_bridge):
    mi = FakeMetadata(title="Dune", authors=["Frank Herbert"])
    db = FakeDB({1: mi})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call(
        "PATCH",
        "/v1/books/1",
        body={
            "series": "Dune Chronicles",
            "seriesIndex": "1",
            "description": "Desert planet.",
            "publisher": "Ace",
            "rating": 4.5,
        },
    )
    assert status == 200
    assert payload["id"] == 1
    assert payload["updated"] is True
    assert mi.series == "Dune Chronicles"
    assert mi.comments == "Desert planet."
    assert mi.publisher == "Ace"
    assert mi.rating == 9
    assert db.new_api.set_metadata_calls


def test_patch_does_not_overwrite_hand_edits(bridge_handlers, serve_bridge):
    """A row the user edited in Calibre wins. We only fill blanks."""
    mi = FakeMetadata(
        title="Dune (annotated)",
        authors=["Herbert, Frank"],
        comments="My own notes.",
        publisher="My Press",
        tags=["favourites"],
    )
    db = FakeDB({1: mi})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call(
        "PATCH",
        "/v1/books/1",
        body={
            "title": "Dune",
            "authors": ["Frank Herbert"],
            "description": "Desert planet.",
            "publisher": "Ace",
            "genres": ["Science Fiction"],
        },
    )
    assert status == 200
    assert mi.title == "Dune (annotated)"
    assert mi.authors == ["Herbert, Frank"]
    assert mi.comments == "My own notes."
    assert mi.publisher == "My Press"
    assert mi.tags == ["favourites"]
    assert payload["fields"] == []


def test_patch_treats_calibre_placeholders_as_empty(bridge_handlers, serve_bridge):
    mi = FakeMetadata(title="Unknown", authors=["Unknown"])
    db = FakeDB({1: mi})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, _payload, _ = bridge.call(
        "PATCH", "/v1/books/1", body={"title": "Dune", "authors": ["Frank Herbert"]}
    )
    assert status == 200
    assert mi.title == "Dune"
    assert mi.authors == ["Frank Herbert"]


def test_patch_merges_identifiers_without_replacing_existing_keys(bridge_handlers, serve_bridge):
    mi = FakeMetadata(title="Dune", identifiers={"isbn": "OLD-ISBN"})
    db = FakeDB({1: mi})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, _payload, _ = bridge.call(
        "PATCH",
        "/v1/books/1",
        body={"identifiers": {"isbn": "9780441172719", "bindery": "42"}},
    )
    assert status == 200
    assert mi.get_identifiers() == {"isbn": "OLD-ISBN", "bindery": "42"}


def test_patch_ignores_cover_path(bridge_handlers, serve_bridge, tmp_path):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")
    mi = FakeMetadata(title="Dune")
    db = FakeDB({1: mi})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, _payload, _ = bridge.call("PATCH", "/v1/books/1", body={"coverPath": str(cover)})
    assert status == 200
    assert db.new_api.set_cover_calls == []
    assert mi.cover_data == (None, None)


def test_patch_rejects_a_non_object_body(bridge_handlers, serve_bridge):
    db = FakeDB({1: FakeMetadata()})
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    status, payload, _ = bridge.call("PATCH", "/v1/books/1", body=["nope"])
    assert status == 400
    assert payload["code"] == "invalid_metadata"


def test_patch_during_library_swap_returns_503(bridge_handlers, serve_bridge):
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: None))
    status, payload, headers = bridge.call("PATCH", "/v1/books/1", body={"title": "Dune"})
    assert status == 503
    assert payload["code"] == "db_unavailable"
    assert int(headers["Retry-After"]) >= 1


# ── 503 library swap window ───────────────────────────────────────────────────


def test_post_succeeds_after_the_library_swap_window_closes(
    bridge_handlers, serve_bridge, tmp_path
):
    """503 then 201 on the same connection, which is what a backoff sees."""
    book = _epub(tmp_path)
    db = MagicMock()
    db.new_api.add_books.return_value = ([5], {})
    db.new_api.search.return_value = set()
    state = {"swapping": True}

    def get_db():
        if state["swapping"]:
            state["swapping"] = False
            return None
        return db

    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=get_db))
    first_status, first_payload, first_headers = bridge.call(
        "POST", "/v1/books", body={"path": book}
    )
    assert first_status == 503
    assert first_payload["code"] == "db_unavailable"
    assert "Retry-After" in first_headers

    second_status, second_payload, _ = bridge.call("POST", "/v1/books", body={"path": book})
    assert second_status == 201
    assert second_payload["id"] == 5


def test_health_still_answers_during_the_swap_window(bridge_handlers, serve_bridge):
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: None))
    status, payload, _ = bridge.call("GET", "/v1/health")
    assert status == 200
    assert payload["library"] == ""
    assert EXPECTED_CAPABILITIES.issubset(set(payload["capabilities"]))


# ── adder unit level: the fill only rule and the cover guards ────────────────


def test_update_book_fills_every_supported_field(bridge_adder):
    mi = FakeMetadata()
    db = FakeDB({1: mi})
    applied = bridge_adder.update_book(
        db,
        1,
        {
            "title": "Dune",
            "authors": ["Frank Herbert"],
            "authorSort": "Herbert, Frank",
            "description": "Desert planet.",
            "publisher": "Ace",
            "publishedDate": "1965-08-01",
            "genres": ["Science Fiction"],
            "language": "eng",
            "series": "Dune Chronicles",
            "seriesIndex": "1.5",
            "rating": 4.6,
            "identifiers": {"isbn": "9780441172719"},
        },
    )
    assert applied == [
        "title",
        "authors",
        "authorSort",
        "description",
        "publisher",
        "publishedDate",
        "genres",
        "language",
        "series",
        "seriesIndex",
        "rating",
        "identifiers",
    ]
    assert mi.author_sort == "Herbert, Frank"
    assert mi.tags == ["Science Fiction"]
    assert mi.languages == ["eng"]
    assert mi.series_index == 1.5
    assert mi.rating == 9


def test_update_book_with_no_metadata_does_nothing(bridge_adder):
    db = FakeDB({1: FakeMetadata()})
    assert bridge_adder.update_book(db, 1, None) == []
    assert db.new_api.set_metadata_calls == []


def test_update_book_rejects_a_non_object(bridge_adder):
    db = FakeDB({1: FakeMetadata()})
    with pytest.raises(ValueError, match="metadata must be an object"):
        bridge_adder.update_book(db, 1, ["nope"])


def test_update_book_raises_for_a_missing_id(bridge_adder):
    db = FakeDB({1: FakeMetadata()})
    with pytest.raises(bridge_adder.BookNotFound):
        bridge_adder.update_book(db, 99, {"title": "Dune"})


def test_update_book_assumes_the_book_exists_when_it_cannot_enumerate(bridge_adder):
    """A db that cannot list ids must not be reported to the client as a 404."""

    class NoIdsAPI(FakeNewAPI):
        def all_book_ids(self):
            raise RuntimeError("not supported")

    db = FakeDB()
    db.new_api = NoIdsAPI({1: FakeMetadata()})
    assert bridge_adder.update_book(db, 1, {"title": "Dune"}) == ["title"]


def test_update_book_leaves_series_index_alone_without_a_series(bridge_adder):
    mi = FakeMetadata(series="Existing", series_index=3.0)
    db = FakeDB({1: mi})
    assert bridge_adder.update_book(db, 1, {"series": "New", "seriesIndex": "9"}) == []
    assert mi.series == "Existing"
    assert mi.series_index == 3.0


def test_update_book_does_not_overwrite_a_rating(bridge_adder):
    mi = FakeMetadata(rating=6)
    db = FakeDB({1: mi})
    assert bridge_adder.update_book(db, 1, {"rating": 5}) == []
    assert mi.rating == 6


def test_update_book_ignores_a_zero_rating(bridge_adder):
    mi = FakeMetadata()
    db = FakeDB({1: mi})
    assert bridge_adder.update_book(db, 1, {"rating": 0}) == []


def test_cover_over_the_size_limit_is_refused(bridge_adder, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_adder, "MAX_COVER_BYTES", 8)
    cover = tmp_path / "huge.jpg"
    cover.write_bytes(b"x" * 64)
    mi = FakeMetadata()
    assert bridge_adder._apply_cover(mi, {"coverPath": str(cover)}, "") is False
    assert mi.cover_data == (None, None)


def test_empty_cover_file_is_refused(bridge_adder, tmp_path):
    cover = tmp_path / "empty.jpg"
    cover.write_bytes(b"")
    mi = FakeMetadata()
    assert bridge_adder._apply_cover(mi, {"coverPath": str(cover)}, "") is False


def test_cover_format_from_the_extension(bridge_adder):
    assert bridge_adder._cover_format("/a/b.jpg") == "jpeg"
    assert bridge_adder._cover_format("/a/b.JPEG") == "jpeg"
    assert bridge_adder._cover_format("/a/b.png") == "png"
    assert bridge_adder._cover_format("/a/b") == "jpeg"


def test_apply_cover_ignores_a_non_dict_and_a_missing_key(bridge_adder):
    mi = FakeMetadata()
    assert bridge_adder._apply_cover(mi, None, "") is None
    assert bridge_adder._apply_cover(mi, {"title": "Dune"}, "") is None


def test_add_book_recovers_the_id_when_calibre_reports_a_duplicate(bridge_adder, tmp_path):
    """add_books returned nothing although the ladder found nothing either."""
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub")

    class RacyAPI:
        def __init__(self):
            self.searches = 0

        def search(self, query):
            self.searches += 1
            # Empty on the ladder's pass, populated by the time we look again.
            return {5} if self.searches > 1 else set()

        def find_identical_books(self, mi):
            return set()

        def add_books(self, books, add_duplicates, run_hooks):
            return [], [books[0]]

    db = FakeDB()
    db.new_api = RacyAPI()
    result = bridge_adder.add_book_detailed(
        db, str(book), metadata={"identifiers": {"bindery": "42"}}
    )
    assert (result.book_id, result.duplicate) == (5, True)


def test_add_book_returns_zero_when_the_duplicate_cannot_be_identified(bridge_adder, tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub")

    class BlindAPI:
        def search(self, query):
            return set()

        def find_identical_books(self, mi):
            return set()

        def add_books(self, books, add_duplicates, run_hooks):
            return [], [books[0]]

    db = FakeDB()
    db.new_api = BlindAPI()
    result = bridge_adder.add_book_detailed(
        db, str(book), metadata={"identifiers": {"bindery": "42"}}
    )
    assert (result.book_id, result.duplicate) == (0, True)


def test_find_identical_books_failure_is_not_fatal(bridge_adder, tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub")

    class BrokenAPI:
        def search(self, query):
            return set()

        def find_identical_books(self, mi):
            raise RuntimeError("index corrupt")

        def add_books(self, books, add_duplicates, run_hooks):
            return [7], []

    db = FakeDB()
    db.new_api = BrokenAPI()
    result = bridge_adder.add_book_detailed(
        db, str(book), metadata={"identifiers": {"bindery": "42"}}
    )
    assert (result.book_id, result.duplicate) == (7, False)
