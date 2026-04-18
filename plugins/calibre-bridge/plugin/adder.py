import os

from calibre.ebooks.metadata.meta import get_metadata


def add_book(db, path: str, gui=None) -> tuple[int, bool]:
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
    with open(path, 'rb') as f:
        mi = get_metadata(f, os.path.splitext(path)[1][1:])
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
