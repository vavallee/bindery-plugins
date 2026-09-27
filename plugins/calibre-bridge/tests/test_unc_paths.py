"""Share paths over 200 characters failed to add on Windows.

Calibre's make_long_path_useable prefixes a bare ``\\\\?\\`` to any long
Windows path, which turns ``\\\\server\\share\\...`` into the invalid
``\\\\?\\\\\\server\\...`` and fails the add with ``[Errno 22]``. Reproduced
against Calibre 9.14 with Bindery's push path remap output; the extended
``\\\\?\\UNC\\`` form added the same file.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

from .test_adder import _load_adder, _stub_calibre  # noqa: F401  (autouse fixture)

LONG_REMAPPED = (
    "\\\\192.168.1.4\\MEDIA\\BOOKS/Isaac Asimov/"
    + "Eight Stories From Rest of the Robots " * 6
    + "/book.pdf"
)


@pytest.mark.parametrize(
    "given, want",
    [
        # Bindery's remap output: UNC root, forward slashes after it.
        (
            "\\\\nas\\MEDIA\\BOOKS/Author/Title/book.epub",
            "\\\\?\\UNC\\nas\\MEDIA\\BOOKS\\Author\\Title\\book.epub",
        ),
        ("//nas/MEDIA/BOOKS/book.epub", "\\\\?\\UNC\\nas\\MEDIA\\BOOKS\\book.epub"),
        # Already extended: left alone, never double prefixed.
        ("\\\\?\\UNC\\nas\\MEDIA\\book.epub", "\\\\?\\UNC\\nas\\MEDIA\\book.epub"),
        ("\\\\?\\C:\\books\\book.epub", "\\\\?\\C:\\books\\book.epub"),
        # Drive letter paths: Calibre's own prefix already works for these.
        ("C:\\books\\book.epub", "C:\\books\\book.epub"),
        ("Z:/BOOKS/book.epub", "Z:/BOOKS/book.epub"),
    ],
)
def test_share_paths_get_the_extended_unc_form(given, want):
    adder = _load_adder()
    assert adder._calibre_safe_path(given, windows=True) == want


def test_non_windows_paths_are_untouched():
    adder = _load_adder()
    for p in ("/books/Author/book.epub", "\\\\nas\\share\\book.epub"):
        assert adder._calibre_safe_path(p, windows=False) == p


def test_add_hands_calibre_the_extended_form(monkeypatch, tmp_path):
    adder = _load_adder()
    opened = []
    monkeypatch.setattr(adder, "_WINDOWS", True)
    monkeypatch.setattr(
        "builtins.open",
        lambda p, mode="r", *a, **k: opened.append(p) or open_stub(),
    )

    def open_stub():
        f = MagicMock()
        f.__enter__ = lambda self: self
        f.__exit__ = lambda self, *a: False
        return f

    api = MagicMock()
    api.add_books.return_value = ([7], [])
    db = MagicMock()
    db.new_api = api

    result = adder.add_book_detailed(db, LONG_REMAPPED)

    assert result.book_id == 7
    (books,), kwargs = api.add_books.call_args
    format_map = books[0][1]
    sent = format_map["PDF"]
    assert sent.startswith("\\\\?\\UNC\\192.168.1.4\\MEDIA\\BOOKS\\Isaac Asimov\\"), sent
    assert "/" not in sent
    assert opened and opened[0] == sent
