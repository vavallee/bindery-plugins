"""An identifier must not be able to change what the dedupe search means.

Calibre's search lexer splits on every Unicode whitespace character, while the
plugin only quoted a term around a plain space. A tab, newline or no break
space in an identifier therefore turned one term into several, and a search
that matched an unrelated row could lead to a file being attached to it. Two
layers: such identifiers are not searched for, and a row a search returned is
read back before anything is attached to it.
"""

import logging

import pytest

from .test_dedupe_ladder import FakeDB, FakeMetadata, _book
from .test_empty_rows import FormatLibrary


@pytest.fixture
def adder(bridge_adder):
    bridge_adder.get_metadata = lambda stream, fmt: FakeMetadata()
    return bridge_adder


class HostileLibrary(FormatLibrary):
    """A library whose search answers with an unrelated row for any query."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def search(self, query, *args, **kwargs):
        self.search_calls.append(query)
        return {self.answer}


BAD_CHARS = ["\t", "\n", "\r", "\x0b", "\x0c", "\x00", "\x1b", "\x7f", " ", " ", "​"]


@pytest.mark.parametrize("ch", BAD_CHARS)
def test_value_with_whitespace_or_control_char_is_not_searched(adder, ch):
    assert adder._identifier_search_query("bindery", f"0{ch}or{ch}id:=57") == ""
    lib = FormatLibrary()
    assert adder._book_id_for_identifier(lib, "bindery", f"0{ch}or{ch}id:=57") == 0
    assert lib.search_calls == []


@pytest.mark.parametrize("ch", [*BAD_CHARS, " ", ":", '"', "\\"])
def test_type_with_whitespace_control_colon_quote_or_backslash_is_not_searched(adder, ch):
    assert adder._identifier_search_query(f"is{ch}bn", "42") == ""


@pytest.mark.parametrize("val", ['a"b', "a\\b", "a\\"])
def test_value_with_quote_or_backslash_is_not_searched(adder, val):
    assert adder._identifier_search_query("bindery", val) == ""


def test_rejection_is_logged_without_the_value(adder, caplog):
    with caplog.at_level(logging.WARNING):
        adder._identifier_search_query("bindery", "secret\tvalue")
    assert caplog.records
    assert "secret" not in caplog.text


def test_plain_and_spaced_values_still_build_a_query(adder):
    assert adder._identifier_search_query("bindery", "42") == "identifiers:=bindery:=42"
    assert adder._identifier_search_query("isbn", "978-0 441") == 'identifiers:"=isbn:=978-0 441"'


def _hostile_push(adder, lib, tmp_path, bindery_id, add_format):
    return adder.add_book_detailed(
        FakeDB(lib),
        _book(tmp_path),
        metadata={
            "title": "Eight Stories",
            "authors": ["Isaac Asimov"],
            "identifiers": {"bindery": bindery_id},
        },
        add_format=add_format,
    )


def test_unrelated_row_from_a_search_never_gets_a_format(adder, tmp_path):
    lib = HostileLibrary(answer=1)
    victim = lib.seed("Someone Else's Book", ["Other Author"], {"isbn": "1"})
    assert victim == 1
    lib.formats_by_book[victim] = {"PDF"}

    result = _hostile_push(adder, lib, tmp_path, "0\tor\tid:=1", add_format=True)

    assert lib.add_format_calls == []
    assert lib.formats(victim) == ("PDF",)
    # Nothing was searched for, so the push is a new book, not the victim's.
    assert lib.search_calls == []
    assert result.book_id != victim


def test_unrelated_row_matched_by_a_plain_value_never_gets_a_format(adder, tmp_path):
    """The read back also covers a search that is simply wrong."""
    lib = HostileLibrary(answer=1)
    victim = lib.seed("Someone Else's Book", ["Other Author"], {"bindery": "99"})
    lib.formats_by_book[victim] = {"PDF"}

    result = _hostile_push(adder, lib, tmp_path, "42", add_format=True)

    assert lib.add_format_calls == []
    assert lib.formats(victim) == ("PDF",)
    # The hit never carried the identifier, so this is a new book.
    assert result.duplicate is False
    assert result.book_id != victim


def test_legitimate_row_still_gets_the_added_format(adder, tmp_path):
    lib = FormatLibrary()
    book = lib.seed("Eight Stories", ["Isaac Asimov"], {"bindery": "42"})
    lib.formats_by_book[book] = {"PDF"}

    result = _hostile_push(adder, lib, tmp_path, "42", add_format=True)

    assert result.book_id == book and result.duplicate is False
    assert lib.add_format_calls == [(book, "EPUB", lib.add_format_calls[0][2])]


def test_readback_is_case_insensitive_like_the_search(adder):
    lib = FormatLibrary()
    book = lib.seed("T", ["A"], {"Bindery": "AbC"})
    assert adder._row_has_identifier(lib, book, "bindery", "abc")
    assert not adder._row_has_identifier(lib, book, "bindery", "abd")
    assert not adder._row_has_identifier(lib, book, "isbn", "abc")


def test_readback_failure_refuses(adder):
    class Broken:
        def field_for(self, name, book_id):
            raise RuntimeError("db gone")

    assert adder._row_has_identifier(Broken(), 1, "bindery", "42") is False


# A leading dot makes Calibre's matcher compare dotted components, which is
# broader than an exact match, and three characters are the tokenizer's
# docstring separators.


@pytest.mark.parametrize("val", [".9780441013593", "..9780441013593", "."])
def test_value_with_a_leading_dot_is_not_searched(adder, val):
    assert adder._identifier_search_query("isbn", val) == ""
    lib = FormatLibrary()
    assert adder._book_id_for_identifier(lib, "isbn", val) == 0
    assert lib.search_calls == []


def test_type_with_a_leading_dot_is_not_searched(adder):
    assert adder._identifier_search_query(".isbn", "42") == ""
    assert adder._identifier_search_query("isbn", "4.2") == "identifiers:=isbn:=4.2"


@pytest.mark.parametrize("sep", ["\u25a1", "\u0f00", "\u0606"])
def test_docstring_separators_are_not_searched(adder, sep):
    assert adder._identifier_search_query("isbn", f"{sep}42{sep}") == ""
    assert adder._identifier_search_query("isbn", f"4{sep}2") == ""
    assert adder._identifier_search_query(f"is{sep}bn", "42") == ""


class ParseException(Exception):
    """Stands in for calibre.utils.search_query_parser.ParseException."""


@pytest.mark.parametrize("exc", [ValueError("bad query"), ParseException("bad query")])
def test_a_refused_search_is_no_match_and_is_logged_without_the_value(adder, caplog, exc):
    class Raising(FormatLibrary):
        def search(self, query, *args, **kwargs):
            raise exc

    with caplog.at_level(logging.WARNING):
        assert adder._book_id_for_identifier(Raising(), "isbn", "secret-value") == 0
    assert caplog.records
    assert "secret-value" not in caplog.text


def test_a_library_fault_during_the_search_still_propagates(adder):
    """Reading it as "no such book" would add a duplicate."""

    class Broken(FormatLibrary):
        def search(self, query, *args, **kwargs):
            raise RuntimeError("database locked")

    with pytest.raises(RuntimeError):
        adder._book_id_for_identifier(Broken(), "isbn", "42")


def test_a_push_survives_a_search_the_parser_refuses(adder, tmp_path):
    class Raising(FormatLibrary):
        def search(self, query, *args, **kwargs):
            raise ValueError("bad query")

    result = _hostile_push(adder, Raising(), tmp_path, "42", add_format=False)

    assert result.duplicate is False and result.book_id


def _unrelated_row_library(identifiers):
    lib = HostileLibrary(answer=1)
    assert lib.seed("Someone Else's Book", ["Other Author"], identifiers) == 1
    return lib


def test_a_search_hit_without_the_identifier_is_not_a_book_id(adder):
    lib = _unrelated_row_library({"isbn": "111"})
    assert adder._book_id_for_identifier(lib, "isbn", "222") == 0
    assert lib.search_calls


def test_only_verified_candidates_are_considered(adder):
    class Many(FormatLibrary):
        def search(self, query, *args, **kwargs):
            return {1, 2, 3}

    lib = Many()
    lib.seed("A", ["x"], {"isbn": "other"})
    lib.seed("B", ["x"], {"isbn": "wanted"})
    lib.seed("C", ["x"], {"ISBN": "WANTED"})
    assert adder._book_id_for_identifier(lib, "isbn", "wanted") == 2


def test_isbn_rung_ignores_an_unrelated_search_hit(adder):
    lib = _unrelated_row_library({"isbn": "111"})
    match = adder._match_existing_book(lib, {"isbn": "222"}, FakeMetadata())
    assert (match.book_id, match.rung) == (0, "")


def test_match_existing_book_ignores_an_unrelated_bindery_hit(adder):
    lib = _unrelated_row_library({"bindery": "99"})
    match = adder._match_existing_book(lib, {"bindery": "42"}, FakeMetadata())
    assert (match.book_id, match.rung) == (0, "")


def test_push_with_an_unrelated_isbn_hit_is_added_not_a_duplicate(adder, tmp_path):
    lib = _unrelated_row_library({"isbn": "111"})
    result = adder.add_book_detailed(
        FakeDB(lib),
        _book(tmp_path),
        metadata={
            "title": "Eight Stories",
            "authors": ["Isaac Asimov"],
            "identifiers": {"isbn": "222"},
        },
    )
    assert result.duplicate is False
    assert result.book_id != 1


def test_post_add_lookup_ignores_an_unrelated_hit(adder, tmp_path):
    class Refusing(HostileLibrary):
        def add_books(self, books, add_duplicates, run_hooks):
            return [], [books[0]]

    lib = Refusing(answer=1)
    lib.seed("Someone Else's Book", ["Other Author"], {"bindery": "99"})

    result = _hostile_push(adder, lib, tmp_path, "42", add_format=False)

    assert (result.book_id, result.duplicate) == (0, True)
