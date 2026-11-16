from __future__ import annotations

import pytest
from kavita_bridge.config import PathMapping
from kavita_bridge.kavita import Library
from kavita_bridge.paths import libraries_for_path, map_path, normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/a//b/", "/a/b"),
        ("C:\\Books\\x", "C:/Books/x"),
        ("/", "/"),
        ("  /a/b  ", "/a/b"),
        ("/a/../b", None),
        ("", None),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_longest_prefix_wins():
    maps = (PathMapping("/media", "/a"), PathMapping("/media/books", "/b"))
    assert map_path("/media/books/x.epub", maps) == "/b/x.epub"
    assert map_path("/media/other/x.epub", maps) == "/a/other/x.epub"


def test_no_match_keeps_the_path():
    assert map_path("/same/place/x", (PathMapping("/other", "/y"),)) == "/same/place/x"
    assert map_path("/same/place/x", ()) == "/same/place/x"


def test_root_mappings():
    assert map_path("/a/b", (PathMapping("/", "/data"),)) == "/data/a/b"
    assert map_path("/data/a", (PathMapping("/data", "/"),)) == "/a"


def test_windows_paths_ignore_case_and_separators():
    maps = (PathMapping("/books", "D:\\Library\\Books"),)
    assert map_path("/books/Author/x.epub", maps) == "D:/Library/Books/Author/x.epub"
    libs = [Library(1, "B", ("d:\\library\\books",))]
    assert libraries_for_path("D:/Library/Books/Author/x.epub", libs) == [1]


def test_posix_paths_are_case_sensitive():
    assert libraries_for_path("/Data/Books/x", [Library(1, "B", ("/data/books",))]) == []


def test_the_most_specific_folder_wins_and_ties_return_both():
    libs = [
        Library(1, "All", ("/data",)),
        Library(2, "Books", ("/data/books",)),
        Library(3, "Dup", ("/data/books",)),
    ]
    assert libraries_for_path("/data/books/x.epub", libs) == [2, 3]
    assert libraries_for_path("/data/other/x.epub", libs) == [1]
