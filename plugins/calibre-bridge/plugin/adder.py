import os

from calibre.ebooks.metadata.meta import get_metadata


def add_book(db, path: str, gui=None) -> tuple[int, bool]:
    with open(path, 'rb') as f:
        mi = get_metadata(f, os.path.splitext(path)[1][1:])
    fmt = os.path.splitext(path)[1][1:].upper()
    # run_hooks=False: hooks may try to update Qt widgets from this background
    # HTTP thread, which causes a fatal Qt error. We handle the GUI refresh
    # ourselves via GObject.idle_add so Calibre sees the new books immediately.
    ids, dups = db.new_api.add_books(
        [(mi, {fmt: path})],
        add_duplicates=False,
        run_hooks=False,
    )
    added = bool(ids)
    if gui is not None and added:
        try:
            from PyQt5.Qt import QTimer
            QTimer.singleShot(0, gui.library_view.model().resort)
        except Exception:
            pass
    if added:
        return ids[0], False
    return list(dups)[0], True
