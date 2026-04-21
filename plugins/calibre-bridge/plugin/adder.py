import os

from calibre.ebooks.metadata.meta import get_metadata


def add_book(db, path: str) -> tuple[int, bool]:
    with open(path, "rb") as f:
        mi = get_metadata(f, os.path.splitext(path)[1][1:])
    fmt = os.path.splitext(path)[1][1:].upper()
    ids, dups = db.new_api.add_books(
        [(mi, {fmt: path})],
        add_duplicates=False,
        run_hooks=True,
    )
    if ids:
        return ids[0], False
    return list(dups)[0], True
