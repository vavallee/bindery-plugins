"""The dedupe fallback ladder, and the Calibre search grammar it rides on.

0.5.0 passed ``add_duplicates=bool(bindery_id)`` to ``add_books``, and Bindery
always sets a ``bindery`` identifier, so Calibre's own duplicate check was
always bypassed. The only dedupe left was an exact ``bindery`` identifier
search, which can never match a library Bindery did not fill. The first
"Push all to Calibre" against a library populated by CWA, by calibredb or by
hand therefore cloned the whole library and reported it as success.

The fake library below models the two real Calibre heuristics, quoting the
source so the difference between them stays visible:

``add_duplicates=False`` goes through ``Cache.create_book_entry``::

    if not add_duplicates and self._has_book(mi):
        return

and ``Cache.has_book`` is title only::

    "Return True iff the database contains an entry with the same title
     as the passed in Metadata object. The comparison is case-insensitive."

``Cache.find_identical_books`` is a different, much stricter heuristic::

    "Finds books that have a superset of the authors in mi and the same
     title (title is fuzzy matched)."

That difference is why ``find_identical_books`` is safe as the last rung of
the ladder while ``add_duplicates=False`` is not: it will not collapse three
books called "The Complete Poems" by three different poets.

Source: calibre ``src/calibre/db/cache.py`` (GPL-3.0), read at master on
2026-09-22.
"""

import re

import pytest


def _fuzzy_title(title):
    """Close enough to calibre.db.utils.fuzzy_title for these tests."""
    return re.sub(r"[^0-9a-z]", "", (title or "").lower())


class FakeLibrary:
    """A Calibre library that implements the two real duplicate heuristics."""

    _QUERY_RE = re.compile(r'^identifiers:(?:"(?P<quoted>.*)"|(?P<bare>.*))$')

    def __init__(self):
        self.rows = {}  # book_id -> {"title", "authors", "identifiers"}
        self._next_id = 1
        self.search_calls = []
        self.find_identical_books_calls = []
        self.add_calls = []

    # -- library construction -------------------------------------------------

    def seed(self, title, authors, identifiers=None):
        book_id = self._next_id
        self._next_id += 1
        self.rows[book_id] = {
            "title": title,
            "authors": list(authors),
            "identifiers": dict(identifiers or {}),
        }
        return book_id

    # -- Calibre new_api surface ---------------------------------------------

    def search(self, query, *args, **kwargs):
        self.search_calls.append(query)
        match = self._QUERY_RE.match(query)
        if not match:
            return set()
        term = match.group("quoted") if match.group("quoted") is not None else match.group("bare")
        key, _sep, value = term.partition(":")
        if not key.startswith("=") or not value.startswith("="):
            raise AssertionError(f"not an exact identifier query: {query!r}")
        key = key[1:].strip().lower()
        value = value[1:].strip().lower()
        return {
            book_id
            for book_id, row in self.rows.items()
            if row["identifiers"].get(key, "").lower() == value
        }

    def find_identical_books(self, mi):
        self.find_identical_books_calls.append(mi)
        wanted_authors = {a.lower() for a in (mi.authors or [])}
        if not wanted_authors:
            return set()
        wanted_title = _fuzzy_title(mi.title)
        return {
            book_id
            for book_id, row in self.rows.items()
            if {a.lower() for a in row["authors"]}.issuperset(wanted_authors)
            and _fuzzy_title(row["title"]) == wanted_title
        }

    def add_books(self, books, add_duplicates, run_hooks):
        mi, format_map = books[0]
        self.add_calls.append({"add_duplicates": add_duplicates, "title": mi.title})
        if not add_duplicates and any(
            row["title"].lower() == (mi.title or "").lower() for row in self.rows.values()
        ):
            return [], [(mi, format_map)]
        book_id = self.seed(mi.title, mi.authors or [], mi.identifiers_set)
        return [book_id], []


class FakeDB:
    def __init__(self, library):
        self.new_api = library


class FakeMetadata:
    """Minimal Metadata with the attributes the adder writes."""

    def __init__(self):
        self.title = "Stub Title"
        self.authors = []
        self.identifiers_set = {}
        self.cover_data = (None, None)
        self.cover = None
        self.rating = None
        self.series_index = 1.0

    def set_identifiers(self, value):
        self.identifiers_set = dict(value)


@pytest.fixture
def adder(bridge_adder):
    """The real adder, with calibre's format sniffing replaced by a stub."""
    bridge_adder.get_metadata = lambda stream, fmt: FakeMetadata()
    return bridge_adder


def _book(tmp_path, name="book.epub"):
    path = tmp_path / name
    path.write_bytes(b"stub epub bytes")
    return str(path)


def _push(adder, db, path, bindery_id, **identifiers):
    ids = {"bindery": bindery_id}
    ids.update(identifiers)
    return adder.add_book(
        db,
        path,
        metadata={
            "title": "The Left Hand of Darkness",
            "authors": ["Ursula K. Le Guin"],
            "identifiers": ids,
        },
    )


# ── the duplicate explosion ───────────────────────────────────────────────────


def test_isbn_match_adopts_an_existing_row(adder, tmp_path):
    """A CWA populated library has no bindery ids. ISBN must still match."""
    lib = FakeLibrary()
    existing = lib.seed(
        "The Left Hand of Darkness",
        ["Ursula K. Le Guin"],
        {"isbn": "9780441478125"},
    )
    book_id, duplicate = _push(adder, FakeDB(lib), _book(tmp_path), "42", isbn="9780441478125")
    assert (book_id, duplicate) == (existing, True)
    assert lib.add_calls == []


def test_asin_match_adopts_an_existing_row(adder, tmp_path):
    lib = FakeLibrary()
    existing = lib.seed("The Left Hand of Darkness", ["Ursula K. Le Guin"], {"asin": "B003WUYQOQ"})
    book_id, duplicate = _push(adder, FakeDB(lib), _book(tmp_path), "42", asin="B003WUYQOQ")
    assert (book_id, duplicate) == (existing, True)


def test_google_match_adopts_an_existing_row(adder, tmp_path):
    lib = FakeLibrary()
    existing = lib.seed("The Left Hand of Darkness", ["Ursula K. Le Guin"], {"google": "gBk2"})
    book_id, duplicate = _push(adder, FakeDB(lib), _book(tmp_path), "42", google="gBk2")
    assert (book_id, duplicate) == (existing, True)


def test_hardcover_match_adopts_an_existing_row(adder, tmp_path):
    lib = FakeLibrary()
    existing = lib.seed("The Left Hand of Darkness", ["Ursula K. Le Guin"], {"hardcover": "991"})
    book_id, duplicate = _push(adder, FakeDB(lib), _book(tmp_path), "42", hardcover="991")
    assert (book_id, duplicate) == (existing, True)


def test_find_identical_books_is_the_last_resort(adder, tmp_path):
    """A hand built library has no identifiers at all, only title and author."""
    lib = FakeLibrary()
    existing = lib.seed("The Left Hand of Darkness", ["Ursula K. Le Guin"])
    book_id, duplicate = _push(adder, FakeDB(lib), _book(tmp_path), "42", isbn="9780441478125")
    assert (book_id, duplicate) == (existing, True)
    assert lib.find_identical_books_calls
    assert lib.add_calls == []


def test_bindery_identifier_still_wins_first(adder, tmp_path):
    lib = FakeLibrary()
    existing = lib.seed("Whatever", ["Someone Else"], {"bindery": "42"})
    book_id, duplicate = _push(adder, FakeDB(lib), _book(tmp_path), "42", isbn="9780441478125")
    assert (book_id, duplicate) == (existing, True)
    assert lib.search_calls == ["identifiers:=bindery:=42"]
    assert lib.find_identical_books_calls == []


def test_ladder_order_and_only_identifiers_that_were_sent(adder, tmp_path):
    lib = FakeLibrary()
    _push(adder, FakeDB(lib), _book(tmp_path), "42", isbn="X", hardcover="Y")
    assert lib.search_calls == [
        "identifiers:=bindery:=42",
        "identifiers:=isbn:=X",
        "identifiers:=hardcover:=Y",
    ]


def test_same_title_different_author_is_not_a_duplicate(adder, tmp_path):
    """The 0.5.0 rationale, preserved.

    Calibre's ``has_book`` check behind ``add_duplicates=False`` is title only,
    so three different poets' "The Complete Poems" collapse into one row. The
    ladder must not reintroduce that: ``find_identical_books`` requires an
    author superset, so it correctly declines to match here.
    """
    lib = FakeLibrary()
    lib.seed("The Complete Poems", ["Emily Bronte"])
    db = FakeDB(lib)
    book_id, duplicate = adder.add_book(
        db,
        _book(tmp_path),
        metadata={
            "title": "The Complete Poems",
            "authors": ["Anne Sexton"],
            "identifiers": {"bindery": "852", "isbn": "9781504034364"},
        },
    )
    assert duplicate is False
    assert book_id > 0
    assert lib.add_calls == [{"add_duplicates": True, "title": "The Complete Poems"}]


def test_no_bindery_identifier_keeps_the_0_5_0_path(adder, tmp_path):
    """Without a bindery id we never bypassed Calibre, so nothing changes."""
    lib = FakeLibrary()
    db = FakeDB(lib)
    adder.add_book(db, _book(tmp_path), metadata={"title": "A Book"})
    assert lib.search_calls == []
    assert lib.find_identical_books_calls == []
    assert lib.add_calls == [{"add_duplicates": False, "title": "A Book"}]


def test_true_and_false_identifier_values_are_not_searched(adder, tmp_path):
    """``identifiers:=isbn:=true`` matches every book that has an isbn at all.

    calibre ``src/calibre/db/search.py`` KeyPairSearch checks
    ``if valq in {'true', 'false'}`` after the ``=`` prefix has been stripped,
    so the exact match is silently turned into a key existence test. Searching
    for such a value would adopt an unrelated book.
    """
    lib = FakeLibrary()
    lib.seed("Something", ["Someone"], {"isbn": "9780441478125"})
    _push(adder, FakeDB(lib), _book(tmp_path), "42", isbn="true")
    assert lib.search_calls == ["identifiers:=bindery:=42"]


# ── the search grammar itself ─────────────────────────────────────────────────


def test_identifier_query_for_a_plain_value(adder):
    assert adder._identifier_search_query("bindery", "42") == "identifiers:=bindery:=42"


def test_identifier_query_quotes_the_whole_term(adder):
    """A value with a space must be quoted as one term, not half a term.

    calibre's lexer (``src/calibre/utils/search_query_parser.py``) matches a
    bare word with ``[^"()\\s]+``, which stops dead at a quote. The 0.5.0 form
    ``identifiers:=bindery:"=series:42 copy"`` therefore lexed as two tokens
    and parsed as two ANDed terms: an identifiers contains match on
    ``=bindery:`` and a free text search for ``=series:42 copy``. It matched
    nothing, so every such push created a duplicate. The quote belongs around
    the whole ``key:value`` term.
    """
    assert (
        adder._identifier_search_query("bindery", "series:42 copy")
        == 'identifiers:"=bindery:=series:42 copy"'
    )


def test_identifier_query_quotes_parentheses(adder):
    assert adder._identifier_search_query("bindery", "42(a)") == 'identifiers:"=bindery:=42(a)"'


def test_identifier_query_does_not_escape_a_leading_sigil(adder):
    """``_matchkind`` strips exactly one leading sigil, so ``==x`` means ``=x``.

    0.5.0 emitted ``=\\=x``, which ``_matchkind`` read as EQUALS with the
    literal ``\\=x``, keeping the backslash in the compared value.
    """
    assert adder._identifier_search_query("bindery", "=x") == "identifiers:=bindery:==x"


def test_identifier_query_is_skipped_for_unexpressible_values(adder):
    """A double quote cannot survive calibre's lexer, which keeps the backslash."""
    assert adder._identifier_search_query("bindery", 'a"b') == ""


def test_book_id_for_identifier_skips_an_unexpressible_value(adder):
    lib = FakeLibrary()
    assert adder._book_id_for_identifier(lib, "bindery", 'a"b') == 0
    assert lib.search_calls == []
