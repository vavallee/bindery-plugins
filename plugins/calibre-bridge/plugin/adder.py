import logging
import os
import pathlib
from typing import Any

from calibre.ebooks.metadata.meta import get_metadata

_log = logging.getLogger(__name__)


def add_book(
    db: Any,
    path: str,
    gui: Any | None = None,
    metadata: dict[str, Any] | None = None,
    ingest_root: str = "",
) -> tuple[int, bool]:
    """Add a book to the Calibre library.

    Returns ``(book_id, duplicate)`` where ``book_id`` is the Calibre id of
    the book in the library and ``duplicate`` indicates whether the book
    was already present.

    ``ingest_root`` optionally restricts which files may be ingested: when
    non-empty, the resolved real path of ``path`` must live inside it
    (resolving symlinks so an escaping link is rejected). When empty, no
    root restriction is applied, preserving the historical behaviour.

    Runs on the bridge's HTTP thread, so we pass ``run_hooks=False`` to
    avoid triggering Calibre hooks that touch Qt widgets from a non-GUI
    thread (which causes the handler thread to abort without a response,
    i.e. the caller sees an empty TCP reply). A GUI refresh is scheduled
    via ``QTimer.singleShot(0, ...)`` so new books appear in the library
    view without a manual Ctrl+R.
    """
    _check_ingest_path(path, ingest_root)
    fmt = os.path.splitext(path)[1][1:].upper()
    if not fmt:
        raise ValueError(f"Cannot determine book format from extension: {path!r}")
    api = db.new_api
    with open(path, "rb") as f:
        mi = get_metadata(f, os.path.splitext(path)[1][1:])
    apply_bindery_metadata(mi, metadata)
    identifiers = (
        _clean_identifiers(metadata.get("identifiers")) if isinstance(metadata, dict) else {}
    )
    bindery_id = identifiers.get("bindery")
    if bindery_id:
        existing = _book_id_for_identifier(api, "bindery", bindery_id)
        if existing:
            return existing, True

    ids, _dups = api.add_books(
        [(mi, {fmt: path})],
        add_duplicates=bool(bindery_id),
        run_hooks=False,
    )
    if ids:
        _schedule_gui_refresh(gui, len(ids))
        return int(ids[0]), False

    if bindery_id:
        existing = _book_id_for_identifier(api, "bindery", bindery_id)
        if existing:
            return existing, True
        return 0, True

    # Duplicate: ``_dups`` is a list of ``(mi, format_map)`` tuples for the
    # input metadata, NOT book ids. We look up the existing book by
    # identical-metadata match so callers still get a usable id back.
    identical = api.find_identical_books(mi) or set()
    if identical:
        return int(next(iter(identical))), True
    return 0, True


def _check_ingest_path(path: str, ingest_root: str) -> None:
    """Reject path traversal and, when configured, escapes from ``ingest_root``.

    The ``..`` check stays in place for every request so the rejection
    message is unchanged when no root is configured (backward-compat). When
    ``ingest_root`` is set we additionally resolve the real path (following
    symlinks) and require it to live inside the resolved root, which catches
    absolute paths and symlink escapes that the ``..`` check alone misses.
    """
    if ".." in pathlib.Path(path).parts:
        raise ValueError(f"Path traversal rejected: {path!r}")
    root = ingest_root.strip()
    if not root:
        return
    resolved = pathlib.Path(path).resolve()
    resolved_root = pathlib.Path(root).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Path outside ingest root: {path!r}")


def _schedule_gui_refresh(gui: Any | None, count: int) -> None:
    """Make ``count`` freshly-added books show up in the Calibre GUI (#1).

    ``add_books`` runs on the bridge's HTTP thread, so the library view never
    learns about the new rows until something pokes the model and the user is
    forced to press Ctrl+R. ``resort()`` (the previous attempt) only re-orders
    rows that are already loaded; ``books_added()`` is what actually inserts the
    new ones, and ``tags_view.recount()`` refreshes the tag-browser counts.
    Both must run on the GUI thread, hence ``QTimer.singleShot(0, ...)``.

    The import is ``qt.core`` (Qt6, Calibre 6+); the old ``PyQt5.Qt`` path
    silently failed on modern Calibre, which is why no refresh happened at all.
    """
    if gui is None:
        return
    try:
        from qt.core import QTimer
    except Exception:
        try:
            from PyQt5.Qt import QTimer  # pre-Qt6 Calibre
        except Exception as exc:
            _log.debug("no QTimer available for Calibre GUI refresh: %s", exc)
            return

    def _refresh() -> None:
        try:
            gui.library_view.model().books_added(count)
            gui.tags_view.recount()
        except Exception as exc:
            _log.debug("Calibre GUI refresh failed: %s", exc)

    QTimer.singleShot(0, _refresh)


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


def _book_id_for_identifier(api: Any, typ: str, val: str) -> int:
    typ = _clean_str(typ)
    val = _clean_str(val)
    if not typ or not val:
        return 0

    matches = set(api.search(_identifier_search_query(typ, val)) or ())
    if not matches:
        return 0
    return min(int(book_id) for book_id in matches)


def _identifier_search_query(typ: str, val: str) -> str:
    return f"identifiers:{_exact_search_literal(typ)}:{_exact_search_literal(val)}"


def _exact_search_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if value[:1] in ("=", "~", "^"):
        escaped = "\\" + escaped
    exact = f"={escaped}"
    if any(ch in value for ch in ' "\\():'):
        return f'"{exact}"'
    return exact


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
    try:
        return float(clean)
    except ValueError:
        # An unparseable optional field must not fail the whole add — mirror
        # _calibre_rating and simply ignore it.
        return None


def _parse_calibre_date(value: str) -> Any:
    from calibre.utils.date import parse_date

    return parse_date(value)
