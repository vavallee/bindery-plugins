import logging
import ntpath
import os
import pathlib
from collections.abc import Callable
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


_WINDOWS = os.name == "nt"


def _calibre_safe_path(path: str, windows: bool | None = None) -> str:
    """Give Calibre a network share path it can open at any length.

    Calibre's ``make_long_path_useable`` prefixes ``\\\\?\\`` to any Windows path
    over 200 characters without handling UNC, so ``\\\\server\\share\\...``
    becomes ``\\\\?\\\\\\server\\...``, which Windows rejects with
    ``[Errno 22] Invalid argument``. A share path is therefore rewritten into
    the extended form ``\\\\?\\UNC\\server\\share\\...`` up front, which Calibre
    leaves alone because it already carries the prefix. Separators are
    normalised first, since the extended form turns off Windows' own
    normalisation and Bindery's push path remap can produce mixed ones.
    Anything that is not a share path is returned unchanged.
    """
    if windows is None:
        windows = _WINDOWS
    if not windows:
        return path
    normalised = ntpath.normpath(path)
    if normalised.startswith("\\\\") and not normalised.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + normalised[2:]
    return path


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


class SourceUnreadable(OSError):
    """The file at ``path`` is there but the Calibre process cannot read it.

    A permission problem or a directory where a book was expected. Kept apart
    from ``FileNotFoundError`` so a client can tell a wrong mount from a file
    it cannot open, and from a failure inside Calibre's own copy.
    """


class CopyFailed(OSError):
    """Calibre could read the source but failed to copy it into the library.

    Raised from the ``OSError`` that ``add_books`` or ``add_format`` raised,
    which stays available as ``__cause__``.
    """


class AddResult(NamedTuple):
    book_id: int
    duplicate: bool
    # None when the request carried no coverPath at all, so the handler can
    # leave the response shape exactly as 0.5.0 emitted it for old clients.
    cover_applied: bool | None
    # True when the file went onto a row that was already there (a row this
    # bridge made for the same Bindery book) instead of into a new row.
    format_added: bool = False


# Names for the rungs of the dedupe ladder in _match_existing_book. Only a
# match on RUNG_BINDERY may have a format attached to it.
RUNG_BINDERY = "bindery"
RUNG_IDENTICAL = "title_authors"


class LadderMatch(NamedTuple):
    """Which existing row the dedupe ladder matched, and on which rung.

    ``book_id`` is 0 and ``rung`` is ``""`` when nothing matched. Otherwise
    ``rung`` is :data:`RUNG_BINDERY`, one of :data:`DEDUPE_IDENTIFIERS`, or
    :data:`RUNG_IDENTICAL` for a ``find_identical_books`` match.
    """

    book_id: int
    rung: str


def add_book(
    db: Any,
    path: str,
    gui: Any | None = None,
    metadata: dict[str, Any] | None = None,
    ingest_root: str = "",
    on_added: Callable[[int], Any] | None = None,
    add_format: bool = False,
    on_updated: Callable[[int], Any] | None = None,
) -> tuple[int, bool]:
    """Add a book to the Calibre library. Returns ``(book_id, duplicate)``.

    Kept for callers that only want the 0.5.0 pair. See
    :func:`add_book_detailed` for the cover outcome as well.
    """
    result = add_book_detailed(
        db,
        path,
        gui=gui,
        metadata=metadata,
        ingest_root=ingest_root,
        on_added=on_added,
        add_format=add_format,
        on_updated=on_updated,
    )
    return result.book_id, result.duplicate


def add_book_detailed(
    db: Any,
    path: str,
    gui: Any | None = None,
    metadata: dict[str, Any] | None = None,
    ingest_root: str = "",
    on_added: Callable[[int], Any] | None = None,
    add_format: bool = False,
    on_updated: Callable[[int], Any] | None = None,
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
    i.e. the caller sees an empty TCP reply). For the same reason the GUI
    refresh that makes new books appear without a manual Ctrl+R is handed to
    ``on_added``, a callable that marshals onto the GUI thread (the action
    passes a ``calibre.gui2.Dispatcher``). See :func:`_schedule_gui_refresh`.

    A source file that exists but cannot be opened raises
    :class:`SourceUnreadable`; a failure while Calibre copies it into the
    library raises :class:`CopyFailed`. A missing file still raises
    ``FileNotFoundError``.

    ``on_updated`` is the counterpart of ``on_added`` for a push that changed
    a row already in the library rather than creating one: it is called with
    that row's id, so the GUI can redraw the row instead of inserting a new
    one. When it is None those pushes fall back to ``on_added``, as in 0.6.3.

    ``add_format`` lets a second file of the same Bindery book (a PDF after
    an EPUB) join the row the first one made instead of coming back as a
    duplicate. See :func:`_attach_to_bindery_row` for exactly when that
    happens. A formatless row left by a failed add is filled in whether or not
    ``add_format`` is set, as it has been since 0.6.2.
    """
    requested_path = path
    _check_ingest_path(path, ingest_root)
    fmt = os.path.splitext(path)[1][1:].upper()
    if not fmt:
        raise BadFormat(f"Cannot determine book format from extension: {path!r}")
    api = db.new_api
    path = _calibre_safe_path(path)
    try:
        source = open(path, "rb")  # noqa: SIM115 (closed by the with below)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SourceUnreadable(f"Cannot read {requested_path!r}: {exc.strerror or exc}") from exc
    with source as f:
        mi = get_metadata(f, os.path.splitext(path)[1][1:])
    apply_bindery_metadata(mi, metadata)
    identifiers = (
        _clean_identifiers(metadata.get("identifiers")) if isinstance(metadata, dict) else {}
    )
    bindery_id = identifiers.get("bindery")
    if bindery_id:
        match = _match_existing_book(api, identifiers, mi)
        if match.book_id:
            if match.rung == RUNG_BINDERY:
                attached = _attach_to_bindery_row(
                    api,
                    match.book_id,
                    bindery_id,
                    fmt,
                    path,
                    requested_path,
                    metadata,
                    ingest_root,
                    add_format,
                )
                if attached is not None:
                    _schedule_gui_row_refresh(gui, attached.book_id, on_added, on_updated)
                    return attached
            return AddResult(match.book_id, True, None)

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
    try:
        ids, _dups = api.add_books(
            [(mi, {fmt: path})],
            add_duplicates=bool(bindery_id),
            run_hooks=False,
        )
    except Exception as exc:
        # add_books writes the row before it copies the file, so a copy that
        # fails (a path Calibre cannot open, for one) leaves a book with no
        # format behind. Remove it so the library does not fill with empty
        # records and the next push is not taken for a duplicate.
        if bindery_id:
            _remove_empty_bindery_row(api, bindery_id)
        if isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError):
            raise _copy_failed(requested_path, exc) from exc
        raise
    if ids:
        _schedule_gui_refresh(gui, len(ids), on_added)
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


def _attach_to_bindery_row(
    api: Any,
    book_id: int,
    bindery_id: str,
    fmt: str,
    path: str,
    requested_path: str,
    metadata: dict[str, Any] | None,
    ingest_root: str,
    add_format: bool,
) -> AddResult | None:
    """Put this file on a row the ladder matched on the ``bindery`` rung.

    Returns None when the push is a plain duplicate and the caller should
    answer 409. The rule, which the caller has already narrowed to a
    ``bindery`` identifier match:

    * The row has no format at all. It is a row this bridge created for the
      same Bindery book and an add that failed after the row was written left
      it empty (0.6.2). Reporting it as a duplicate would leave it empty for
      good, so the file goes on whether or not ``add_format`` was sent.
    * The row has formats, ``add_format`` was sent, and none of them is this
      one. A second file of the same Bindery book, so it joins the row.
    * Anything else is a duplicate, including the same format a second time.

    A match on any other rung never reaches here. An ISBN or a title match can
    be a book the user curated by hand, and a file is never added to that.

    After the file goes on, the row gets the fill only metadata update that
    ``PATCH /v1/books/{id}`` applies, so it picks up anything Bindery knows
    that the row is missing and loses nothing the user set. A row that was
    empty also gets ``coverPath``, since the add that made it never finished;
    a row that already had a file keeps its cover.
    """
    formats = _row_formats(api, book_id)
    if formats is None:
        return None
    if formats:
        if not add_format or fmt in formats:
            return None
    elif not _is_empty_bindery_row(api, book_id, bindery_id):
        return None
    try:
        added = api.add_format(book_id, fmt, path, replace=False, run_hooks=False)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _copy_failed(requested_path, exc) from exc
    if added is False:
        # Calibre returns False when replace=False meets a format that is
        # already there, which means another push got in first.
        return None
    if formats:
        _log.info("added %s to book %d, which already had %s", fmt, book_id, ",".join(formats))
    else:
        _log.info("attached %s to book %d, which an earlier add left empty", fmt, book_id)
    if isinstance(metadata, dict):
        _fill_existing_row(api, book_id, metadata)
    cover_applied = None if formats else _set_row_cover(api, book_id, metadata, ingest_root)
    return AddResult(book_id, False, cover_applied, True)


def _row_formats(api: Any, book_id: int) -> set[str] | None:
    """The formats a row carries, upper case, or None if they cannot be read."""
    try:
        return {str(f).upper() for f in (api.formats(book_id) or ())}
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("could not read the formats of book %d: %s", book_id, exc)
        return None


def _fill_existing_row(api: Any, book_id: int, metadata: dict[str, Any]) -> list[str]:
    """Apply the fill only metadata rule to ``book_id``. Never fails the add.

    The file is already in the library by the time this runs, so a metadata
    problem is logged rather than turned into an error for a book that did
    arrive.
    """
    try:
        mi = api.get_metadata(book_id)
        applied = _fill_empty_fields(mi, metadata)
        if applied:
            api.set_metadata(book_id, mi)
            _log.info("filled book %d: %s", book_id, ",".join(applied))
        return applied
    except Exception as exc:
        _log.warning("could not fill the metadata of book %d: %s", book_id, exc)
        return []


def _set_row_cover(
    api: Any, book_id: int, metadata: dict[str, Any] | None, ingest_root: str
) -> bool | None:
    """Set ``metadata.coverPath`` on an existing row, with the add's own checks."""
    cover = _load_cover(metadata, ingest_root)
    if cover.data is None:
        return cover.applied
    try:
        api.set_cover({book_id: cover.data[1]})
    except Exception as exc:
        _log.warning("could not set the cover of book %d: %s", book_id, exc)
        return False
    return True


def _copy_failed(path: str, exc: OSError) -> CopyFailed:
    return CopyFailed(f"Calibre could not copy {path!r} into the library: {exc}")


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


def _is_empty_bindery_row(api: Any, book_id: int, bindery_id: str) -> bool:
    """True when ``book_id`` carries this ``bindery`` identifier and no format.

    Only a row matched on Bindery's own identifier qualifies. An empty row a
    user made by hand, a wishlist entry for example, is never filled in.
    """
    try:
        if api.formats(book_id):
            return False
        ids = api.field_for("identifiers", book_id) or {}
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("could not inspect book %d: %s", book_id, exc)
        return False
    return str(ids.get("bindery", "")).strip().lower() == bindery_id.strip().lower()


def _remove_empty_bindery_row(api: Any, bindery_id: str) -> None:
    """Remove the formatless row a failed ``add_books`` left for ``bindery_id``."""
    try:
        orphan = _book_id_for_identifier(api, "bindery", bindery_id)
        if orphan and _is_empty_bindery_row(api, orphan, bindery_id):
            api.remove_books((orphan,))
            _log.warning("removed book %d, left without a file by a failed add", orphan)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("could not remove the empty row a failed add left: %s", exc)


def _existing_book_id(api: Any, identifiers: dict[str, str], mi: Any) -> int:
    """The id half of :func:`_match_existing_book`, 0 when nothing matched."""
    return _match_existing_book(api, identifiers, mi).book_id


def _match_existing_book(api: Any, identifiers: dict[str, str], mi: Any) -> LadderMatch:
    """Find a book already in the library that this push would duplicate.

    The ladder, strongest evidence first:

    1. Bindery's own ``bindery`` identifier. Only ever matches a row Bindery
       put there itself.
    2. The other identifiers Bindery sends. This is the rung that matters for
       a library populated by CWA, by calibredb, by hand or by plugin 0.4.0,
       where no ``bindery`` identifier exists anywhere.
    3. ``find_identical_books``, for a library with no identifiers at all.

    Returns the matched id and the rung that matched it, or ``(0, "")`` when
    nothing matches, which means the caller goes on to add.
    """
    bindery_id = identifiers.get("bindery")
    if bindery_id:
        existing = _book_id_for_identifier(api, "bindery", bindery_id)
        if existing:
            return LadderMatch(existing, RUNG_BINDERY)

    for typ in DEDUPE_IDENTIFIERS:
        value = identifiers.get(typ)
        if not value:
            continue
        existing = _book_id_for_identifier(api, typ, value)
        if existing:
            _log.info("dedupe: matched existing book %d on %s identifier", existing, typ)
            return LadderMatch(existing, typ)

    identical = _safe_find_identical_books(api, mi)
    if identical:
        existing = min(int(book_id) for book_id in identical)
        _log.info("dedupe: matched existing book %d on title and authors", existing)
        return LadderMatch(existing, RUNG_IDENTICAL)
    return LadderMatch(0, "")


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
    cover = _load_cover(metadata, ingest_root)
    if cover.data is None:
        return cover.applied
    mi.cover_data = cover.data
    # Calibre's set_metadata falls back to reading mi.cover from disk when
    # cover_data is unset, so setting both covers either code path.
    mi.cover = cover.path
    return True


class _Cover(NamedTuple):
    # None: no coverPath sent. False: sent but unusable. True: ``data`` is set.
    applied: bool | None
    data: tuple[str, bytes] | None = None
    path: str = ""


def _load_cover(metadata: dict[str, Any] | None, ingest_root: str) -> _Cover:
    """Read ``metadata.coverPath`` off disk with every check the add applies."""
    if not isinstance(metadata, dict):
        return _Cover(None)
    cover_path = _clean_str(metadata.get("coverPath"))
    if not cover_path:
        return _Cover(None)
    try:
        _check_ingest_path(cover_path, ingest_root)
    except ValueError as exc:
        _log.warning("cover rejected: %s", exc)
        return _Cover(False)
    cover_path = _calibre_safe_path(cover_path)
    try:
        size = os.path.getsize(cover_path)
        if size > MAX_COVER_BYTES:
            _log.warning(
                "cover rejected: %d bytes exceeds the %d byte limit: %r",
                size,
                MAX_COVER_BYTES,
                cover_path,
            )
            return _Cover(False)
        with open(cover_path, "rb") as f:
            data = f.read()
    except OSError as exc:
        _log.warning("cover could not be read, adding the book without it: %s", exc)
        return _Cover(False)
    if not data:
        _log.warning("cover file is empty, adding the book without it: %r", cover_path)
        return _Cover(False)
    return _Cover(True, (_cover_format(cover_path), data), cover_path)


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
    exists = os.path.exists(_calibre_safe_path(path))
    return {
        "path": path,
        "exists": exists,
        "readable": bool(exists and os.access(_calibre_safe_path(path), os.R_OK)),
        "isDir": bool(exists and os.path.isdir(_calibre_safe_path(path))),
    }


def _schedule_gui_row_refresh(
    gui: Any | None,
    book_id: int,
    on_added: Callable[[int], Any] | None = None,
    on_updated: Callable[[int], Any] | None = None,
) -> None:
    """Redraw a row that was already in the library, on the GUI thread.

    ``on_updated`` is a Dispatcher the action built on the GUI thread, like
    ``on_added``, and it calls ``BooksModel.refresh_ids([book_id])``.
    ``books_added`` is wrong for an existing row: Calibre implements it as a
    ``beginInsertRows`` at row 0, which tells the view a row appeared that the
    model does not have. Without ``on_updated`` (a caller from before 0.7.0)
    this falls back to the ``on_added`` refresh the 0.6.2 repair used.
    """
    if on_updated is None:
        _schedule_gui_refresh(gui, 1, on_added)
        return
    try:
        on_updated(book_id)
    except Exception as exc:
        _log.debug("Calibre GUI row refresh dispatch failed: %s", exc)


def _schedule_gui_refresh(
    gui: Any | None, count: int, on_added: Callable[[int], Any] | None = None
) -> None:
    """Make ``count`` freshly-added books show up in the Calibre GUI (#1).

    ``add_books`` runs on the bridge's HTTP thread, so the library view never
    learns about the new rows until something pokes the model and the user is
    forced to press Ctrl+R. ``books_added()`` is what inserts the new rows and
    ``tags_view.recount()`` refreshes the tag browser counts. Both must run on
    the GUI thread.

    ``on_added`` is how they get there. The action builds it on the GUI thread
    as a ``calibre.gui2.Dispatcher``, which queues the call onto the thread it
    was created on, so calling it from here is safe and the refresh actually
    runs. That is the path every 0.6.3 install takes.

    The ``gui`` fallback is kept for callers that pass no ``on_added``. It uses
    ``QTimer.singleShot(0, ...)``, which queues onto the calling thread's event
    loop. The HTTP worker thread has none, so on real Calibre (checked on 9.14)
    the timer never fires and the view is not refreshed. Do not rely on it.
    """
    if on_added is not None:
        try:
            on_added(count)
        except Exception as exc:
            _log.debug("Calibre GUI refresh dispatch failed: %s", exc)
        return
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
