import os
import pathlib
from typing import Any

from calibre.ebooks.metadata.meta import get_metadata


def add_book(
    db: Any,
    path: str,
    gui: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Add a book to the Calibre library.

    Returns ``(book_id, duplicate)`` where ``book_id`` is the Calibre id of
    the book in the library and ``duplicate`` indicates whether the book
    was already present.

    Runs on the bridge's HTTP thread, so we pass ``run_hooks=False`` to
    avoid triggering Calibre hooks that touch Qt widgets from a non-GUI
    thread (which causes the handler thread to abort without a response,
    i.e. the caller sees an empty TCP reply). A GUI refresh is scheduled
    via ``QTimer.singleShot(0, ...)`` so new books appear in the library
    view without a manual Ctrl+R.
    """
    if ".." in pathlib.Path(path).parts:
        raise ValueError(f"Path traversal rejected: {path!r}")
    with open(path, "rb") as f:
        mi = get_metadata(f, os.path.splitext(path)[1][1:])
    apply_bindery_metadata(mi, metadata)
    fmt = os.path.splitext(path)[1][1:].upper()
    ids, _dups = db.new_api.add_books(
        [(mi, {fmt: path})],
        add_duplicates=False,
        run_hooks=False,
    )
    if ids:
        if gui is not None:
            try:
                from PyQt5.Qt import QTimer

                QTimer.singleShot(0, gui.library_view.model().resort)
            except Exception:
                pass
        return int(ids[0]), False

    # Duplicate: ``_dups`` is a list of ``(mi, format_map)`` tuples for the
    # input metadata, NOT book ids. We look up the existing book by
    # identical-metadata match so callers still get a usable id back.
    existing = db.new_api.find_identical_books(mi) or set()
    if existing:
        return int(next(iter(existing))), True
    return 0, True


def apply_bindery_metadata(mi: Any, metadata: dict[str, Any] | None) -> None:
    """Apply Bindery's optional metadata envelope to a Calibre Metadata object."""
    if not metadata:
        return
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")

    if title := _clean_str(metadata.get("title")):
        mi.title = title
    if authors := _clean_str_list(metadata.get("authors")):
        mi.authors = authors
    if author_sort := _clean_str(metadata.get("authorSort")):
        mi.author_sort = author_sort
    if description := _clean_str(metadata.get("description")):
        mi.comments = description
    if publisher := _clean_str(metadata.get("publisher")):
        mi.publisher = publisher
    if published := _clean_str(metadata.get("publishedDate")):
        mi.pubdate = _parse_calibre_date(published)
    if genres := _clean_str_list(metadata.get("genres")):
        mi.tags = genres
    if language := _clean_str(metadata.get("language")):
        mi.languages = [language]
    if series := _clean_str(metadata.get("series")):
        mi.series = series
    if (series_index := _calibre_series_index(metadata.get("seriesIndex"))) is not None:
        mi.series_index = series_index
    if (rating := _calibre_rating(metadata.get("rating"))) is not None:
        mi.rating = rating
    if identifiers := _clean_identifiers(metadata.get("identifiers")):
        mi.set_identifiers(identifiers)


def _clean_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        clean = _clean_str(item)
        key = clean.casefold()
        if clean and key not in seen:
            out.append(clean)
            seen.add(key)
    return out


def _clean_identifiers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        clean_key = _clean_str(key)
        clean_value = _clean_str(raw)
        if clean_key and clean_value:
            out[clean_key] = clean_value
    return out


def _calibre_rating(value: Any) -> int | None:
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if rating <= 0:
        return 0
    return max(0, min(10, int((rating * 2) + 0.5)))


def _calibre_series_index(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    clean = _clean_str(value)
    if not clean:
        return None
    return float(clean)


def _parse_calibre_date(value: str) -> Any:
    from calibre.utils.date import parse_date

    return parse_date(value)
