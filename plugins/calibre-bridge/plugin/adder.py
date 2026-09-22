import logging
import os
import pathlib
from typing import Any, NamedTuple

from calibre.ebooks.metadata.meta import get_metadata

_log = logging.getLogger(__name__)

# Identifiers, in descending order of confidence, that the dedupe ladder falls
# back to when Bindery's own ``bindery`` identifier finds nothing. These are
# the keys Bindery sends today (internal/calibre/identifiers.go); adding one is
# a one line change here. ``openlibrary``, ``openlibrary_edition`` and ``dnb``
# are deliberately left out for now: they identify a work or a national library
# record rather than the specific copy, so a match is weaker evidence.
DEDUPE_IDENTIFIERS = ("isbn", "asin", "google", "hardcover")

# Upper bound on a cover we will read off disk. Bindery caps its own covers at
# 10 MiB; this is the same guard on the receiving side, since coverPath arrives
# from the network like any other field.
MAX_COVER_BYTES = 16 * 1024 * 1024

# Calibre writes these placeholders when it has nothing better (see
# Cache.create_book_entry: ``mi.title = mi.title or _('Unknown')``), so a
# metadata update must treat them as empty rather than as a user's own value.
_PLACEHOLDERS = frozenset({"", "unknown"})


class PathForbidden(ValueError):
    """The requested path is outside what this bridge is allowed to read.

    Subclasses ``ValueError`` so callers written against 0.5.0 keep working.
    """


class BadFormat(ValueError):
    """The path carries no usable Calibre book format.

    Subclasses ``ValueError`` so callers written against 0.5.0 keep working.
    """


class BookNotFound(LookupError):
    """No book with the requested id exists in the active library."""


class AddResult(NamedTuple):
    book_id: int
    duplicate: bool
    # None when the request carried no coverPath at all, so the handler can
    # leave the response shape exactly as 0.5.0 emitted it for old clients.
    cover_applied: bool | None


def add_book(
    db: Any,
    path: str,
    gui: Any | None = None,
    metadata: dict[str, Any] | None = None,
    ingest_root: str = "",
) -> tuple[int, bool]:
    """Add a book to the Calibre library. Returns ``(book_id, duplicate)``.

    Kept for callers that only want the 0.5.0 pair. See
    :func:`add_book_detailed` for the cover outcome as well.
    """
    result = add_book_detailed(db, path, gui=gui, metadata=metadata, ingest_root=ingest_root)
    return result.book_id, result.duplicate


def add_book_detailed(
    db: Any,
    path: str,
    gui: Any | None = None,
    metadata: dict[str, Any] | None = None,
    ingest_root: str = "",
) -> AddResult:
    """Add a book to the Calibre library.

    Returns ``(book_id, duplicate, cover_applied)`` where ``book_id`` is the
    Calibre id of the book in the library, ``duplicate`` indicates whether the
    book was already present, and ``cover_applied`` reports whether
    ``metadata.coverPath`` made it onto the row (``None`` when none was sent).

    ``ingest_root`` optionally restricts which files may be ingested: when
    non-empty, the resolved real path of ``path`` must live inside it
    (resolving symlinks so an escaping link is rejected). When empty, no
    root restriction is applied, preserving the historical behaviour. The
    same restriction is applied to ``metadata.coverPath``.

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
        raise BadFormat(f"Cannot determine book format from extension: {path!r}")
    api = db.new_api
    with open(path, "rb") as f:
        mi = get_metadata(f, os.path.splitext(path)[1][1:])
    apply_bindery_metadata(mi, metadata)
    identifiers = (
        _clean_identifiers(metadata.get("identifiers")) if isinstance(metadata, dict) else {}
    )
    bindery_id = identifiers.get("bindery")
    if bindery_id:
        existing = _existing_book_id(api, identifiers, mi)
        if existing:
            return AddResult(existing, True, None)

    cover_applied = _apply_cover(mi, metadata, ingest_root)

    # ``add_duplicates=True`` whenever Bindery supplied its own identifier.
    # The reason is that Calibre's own check behind ``add_duplicates=False``
    # is title only: Cache.create_book_entry does
    # ``if not add_duplicates and self._has_book(mi): return``, and has_book is
    # documented as "Return True iff the database contains an entry with the
    # same title as the passed in Metadata object. The comparison is
    # case-insensitive." Three different poets' "The Complete Poems" would
    # therefore collapse into one row. The ladder in _existing_book_id above
    # is what replaces it: an exact identifier match first, then
    # find_identical_books, which requires a superset of the authors as well
    # as a fuzzy title match and so does not have that failure mode.
    ids, _dups = api.add_books(
        [(mi, {fmt: path})],
        add_duplicates=bool(bindery_id),
        run_hooks=False,
    )
    if ids:
        _schedule_gui_refresh(gui, len(ids))
        return AddResult(int(ids[0]), False, cover_applied)

    if bindery_id:
        existing = _book_id_for_identifier(api, "bindery", bindery_id)
        if existing:
            return AddResult(existing, True, None)
        return AddResult(0, True, None)

    # Duplicate: ``_dups`` is a list of ``(mi, format_map)`` tuples for the
    # input metadata, NOT book ids. We look up the existing book by
    # identical-metadata match so callers still get a usable id back.
    identical = _safe_find_identical_books(api, mi)
    if identical:
        return AddResult(int(next(iter(identical))), True, None)
    return AddResult(0, True, None)


def update_book(
    db: Any,
    book_id: int,
    metadata: dict[str, Any] | None,
) -> list[str]:
    """Apply Bindery metadata to a book already in the library.

    Returns the list of Bindery field names that were actually written.

    **The rule is fill only.** A field is written when the Calibre row has
    nothing in it; a field the row already carries is left alone, and nothing
    is ever cleared. Identifiers are merged key by key: a key Calibre does not
    have is added, a key it does have keeps its existing value.

    The reason is that the bridge cannot tell a value it wrote itself from one
    the user typed in Calibre. Overwriting would silently undo hand edits on
    every push, and a push happens without the user asking. Filling blanks
    closes the gap this endpoint exists for (a bulk push that landed without
    series, description, publisher, pubdate or rating) without ever taking
    something away.

    ``metadata.coverPath`` is ignored here. Replacing artwork on an existing
    row is a bigger decision than filling an empty text field, and Calibre's
    own set_metadata treats covers specially ("Covers are always changed if a
    new cover is provided"), so it does not fit the fill only rule.
    """
    api = db.new_api
    if not _book_exists(api, book_id):
        raise BookNotFound(f"No book with id {book_id} in the Calibre library")
    if metadata is None:
        return []
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")

    mi = api.get_metadata(book_id)
    applied = _fill_empty_fields(mi, metadata)
    if applied:
        api.set_metadata(book_id, mi)
        _log.info("update_book id=%d applied=%s", book_id, ",".join(applied))
    else:
        _log.info("update_book id=%d applied nothing: no empty fields to fill", book_id)
    return applied


def _book_exists(api: Any, book_id: int) -> bool:
    try:
        return book_id in set(api.all_book_ids())
    except Exception as exc:  # pragma: no cover - defensive
        # Cannot tell, so do not invent a 404. set_metadata will fail loudly.
        _log.debug("could not enumerate book ids: %s", exc)
        return True


def _fill_empty_fields(mi: Any, metadata: dict[str, Any]) -> list[str]:
    applied: list[str] = []

    title = _clean_str(metadata.get("title"))
    if title and _is_placeholder(getattr(mi, "title", None)):
        mi.title = title
        applied.append("title")

    authors = _clean_str_list(metadata.get("authors"))
    current_authors = [a for a in (getattr(mi, "authors", None) or []) if not _is_placeholder(a)]
    if authors and not current_authors:
        mi.authors = authors
        applied.append("authors")

    author_sort = _clean_str(metadata.get("authorSort"))
    if author_sort and _is_empty(getattr(mi, "author_sort", None)):
        mi.author_sort = author_sort
        applied.append("authorSort")

    description = _clean_str(metadata.get("description"))
    if description and _is_empty(getattr(mi, "comments", None)):
        mi.comments = description
        applied.append("description")

    publisher = _clean_str(metadata.get("publisher"))
    if publisher and _is_empty(getattr(mi, "publisher", None)):
        mi.publisher = publisher
        applied.append("publisher")

    published = _clean_str(metadata.get("publishedDate"))
    if published and _is_empty(getattr(mi, "pubdate", None)):
        mi.pubdate = _parse_calibre_date(published)
        applied.append("publishedDate")

    genres = _clean_str_list(metadata.get("genres"))
    if genres and _is_empty(getattr(mi, "tags", None)):
        mi.tags = genres
        applied.append("genres")

    language = _clean_str(metadata.get("language"))
    if language and _is_empty(getattr(mi, "languages", None)):
        mi.languages = [language]
        applied.append("language")

    series = _clean_str(metadata.get("series"))
    if series and _is_empty(getattr(mi, "series", None)):
        mi.series = series
        applied.append("series")
        # Calibre defaults series_index to 1.0 and never leaves it empty, so
        # there is no "is it blank" test for it. Write it only when we
        # supplied the series it belongs to.
        index = _calibre_series_index(metadata.get("seriesIndex"))
        if index is not None:
            mi.series_index = index
            applied.append("seriesIndex")

    rating = _calibre_rating(metadata.get("rating"))
    if rating and not getattr(mi, "rating", None):
        mi.rating = rating
        applied.append("rating")

    identifiers = _clean_identifiers(metadata.get("identifiers"))
    if identifiers:
        current = dict(mi.get_identifiers() or {})
        merged = dict(current)
        for key, value in identifiers.items():
            merged.setdefault(key, value)
        if merged != current:
            mi.set_identifiers(merged)
            applied.append("identifiers")

    return applied


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple | set | dict):
        return not value
    return False


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return _is_empty(value)
    return value.strip().lower() in _PLACEHOLDERS


def _existing_book_id(api: Any, identifiers: dict[str, str], mi: Any) -> int:
    """Find a book already in the library that this push would duplicate.

    The ladder, strongest evidence first:

    1. Bindery's own ``bindery`` identifier. Only ever matches a row Bindery
       put there itself.
    2. The other identifiers Bindery sends. This is the rung that matters for
       a library populated by CWA, by calibredb, by hand or by plugin 0.4.0,
       where no ``bindery`` identifier exists anywhere.
    3. ``find_identical_books``, for a library with no identifiers at all.

    Returns 0 when nothing matches, which means the caller goes on to add.
    """
    bindery_id = identifiers.get("bindery")
    if bindery_id:
        existing = _book_id_for_identifier(api, "bindery", bindery_id)
        if existing:
            return existing

    for typ in DEDUPE_IDENTIFIERS:
        value = identifiers.get(typ)
        if not value:
            continue
        existing = _book_id_for_identifier(api, typ, value)
        if existing:
            _log.info("dedupe: matched existing book %d on %s identifier", existing, typ)
            return existing

    identical = _safe_find_identical_books(api, mi)
    if identical:
        existing = min(int(book_id) for book_id in identical)
        _log.info("dedupe: matched existing book %d on title and authors", existing)
        return existing
    return 0


def _safe_find_identical_books(api: Any, mi: Any) -> set:
    try:
        return set(api.find_identical_books(mi) or ())
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("find_identical_books failed: %s", exc)
        return set()


def _apply_cover(mi: Any, metadata: dict[str, Any] | None, ingest_root: str) -> bool | None:
    """Read ``metadata.coverPath`` onto ``mi``. Never fails the add.

    Returns True when the cover was applied, False when a coverPath was sent
    but could not be used, and None when none was sent.

    A cover is worth less than the book it belongs to, so a missing or
    unreadable cover, or one outside the ingest root, is logged and reported
    in the response rather than costing the user the book. The ingest root and
    traversal checks are still enforced: ``coverPath`` arrives from the network
    and is no more trustworthy than any other field, and the bytes end up
    readable through Calibre's content server.
    """
    if not isinstance(metadata, dict):
        return None
    cover_path = _clean_str(metadata.get("coverPath"))
    if not cover_path:
        return None
    try:
        _check_ingest_path(cover_path, ingest_root)
    except ValueError as exc:
        _log.warning("cover rejected: %s", exc)
        return False
    try:
        size = os.path.getsize(cover_path)
        if size > MAX_COVER_BYTES:
            _log.warning(
                "cover rejected: %d bytes exceeds the %d byte limit: %r",
                size,
                MAX_COVER_BYTES,
                cover_path,
            )
            return False
        with open(cover_path, "rb") as f:
            data = f.read()
    except OSError as exc:
        _log.warning("cover could not be read, adding the book without it: %s", exc)
        return False
    if not data:
        _log.warning("cover file is empty, adding the book without it: %r", cover_path)
        return False
    mi.cover_data = (_cover_format(cover_path), data)
    # Calibre's set_metadata falls back to reading mi.cover from disk when
    # cover_data is unset, so setting both covers either code path.
    mi.cover = cover_path
    return True


def _cover_format(path: str) -> str:
    ext = os.path.splitext(path)[1][1:].lower()
    if ext in ("jpg", "jpeg"):
        return "jpeg"
    return ext or "jpeg"


def _check_ingest_path(path: str, ingest_root: str) -> None:
    """Reject path traversal and, when configured, escapes from ``ingest_root``.

    The ``..`` check stays in place for every request so the rejection
    message is unchanged when no root is configured (backward-compat). When
    ``ingest_root`` is set we additionally resolve the real path (following
    symlinks) and require it to live inside the resolved root, which catches
    absolute paths and symlink escapes that the ``..`` check alone misses.
    """
    if ".." in pathlib.Path(path).parts:
        raise PathForbidden(f"Path traversal rejected: {path!r}")
    root = ingest_root.strip()
    if not root:
        return
    resolved = pathlib.Path(path).resolve()
    resolved_root = pathlib.Path(root).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise PathForbidden(f"Path outside ingest root: {path!r}")


def probe_path(path: str, ingest_root: str = "") -> dict[str, Any]:
    """Report whether the Calibre process can see ``path``.

    Answers the one question the operator cannot otherwise ask before a push:
    can this container actually reach the path Bindery is about to send. Never
    opens the file, so it cannot be used to read anything, and it applies the
    same ingest root restriction as an add so it cannot be used to map the
    filesystem outside it either.
    """
    _check_ingest_path(path, ingest_root)
    exists = os.path.exists(path)
    return {
        "path": path,
        "exists": exists,
        "readable": bool(exists and os.access(path, os.R_OK)),
        "isDir": bool(exists and os.path.isdir(path)),
    }


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
    """Apply Bindery's optional metadata envelope to a Calibre Metadata object.

    Used when creating a row, where Bindery's metadata is better than whatever
    the file happens to embed, so it overwrites. The fill only rule in
    :func:`update_book` applies to existing rows instead.
    """
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
    query = _identifier_search_query(typ, val)
    if not query:
        return 0

    matches = set(api.search(query) or ())
    if not matches:
        return 0
    return min(int(book_id) for book_id in matches)


def _identifier_search_query(typ: str, val: str) -> str:
    """Build an exact ``identifiers`` search for calibre's search grammar.

    Returns ``""`` when the value cannot be expressed as a search literal, so
    the caller skips the lookup rather than running a query that quietly means
    something else.

    Three rules, all read out of calibre's own source at master on 2026-09-22:

    * The whole ``key:value`` term is one search token, and the quote, when one
      is needed, goes around all of it. The lexer in
      ``src/calibre/utils/search_query_parser.py`` matches a bare word with
      ``[^"()\\s]+``, which stops at a quote, so quoting only the value half
      splits the query into two ANDed terms and it matches nothing. The manual
      says the same thing: ``tag:"=science fiction"``, not ``tag:="science
      fiction"``.
    * ``src/calibre/db/search.py::_matchkind`` strips exactly one leading
      sigil from each half, so ``=`` marks an exact match and everything after
      it is taken literally. A value that itself starts with ``=``, ``~``,
      ``^`` or ``\\`` needs no escaping, and adding a backslash (which 0.5.0
      did) leaves the backslash in the compared literal.
    * ``KeyPairSearch`` tests ``valq in {'true', 'false'}`` after that strip,
      so ``=true`` is not an exact match for the string "true": it silently
      becomes "has this identifier key at all" and would match an unrelated
      book. Refuse those values.

    A double quote in the value is unexpressible: the lexer keeps the
    backslash of a ``\\"`` escape in the literal it hands to the matcher, so
    there is no spelling that compares equal. Refuse those too.
    """
    if '"' in typ or '"' in val:
        return ""
    if val.strip().lower() in ("true", "false"):
        return ""
    term = f"={typ}:={val}"
    if any(ch in term for ch in " ()"):
        return f'identifiers:"{term}"'
    return f"identifiers:{term}"


def _calibre_rating(value: Any) -> int | None:
    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None
    if rating <= 0:
        return 0
    return max(0, min(10, int((rating * 2) + 0.5)))


def _calibre_series_index(value: Any) -> float | None:
    if isinstance(value, int | float):
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
