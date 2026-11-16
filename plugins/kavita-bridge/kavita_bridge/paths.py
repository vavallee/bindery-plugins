"""Path mapping and library lookup.

Bindery and Kavita often mount the same folder at different paths, so a path
from Bindery is first rewritten with the configured prefix map, then matched
against each Kavita library's folders on whole path segments.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .config import PathMapping
from .kavita import Library

_DRIVE = re.compile(r"^[A-Za-z]:(/|$)")


def normalize(path: str) -> str | None:
    """Forward slashes, no repeated or trailing slash. None for a path holding `..`."""
    p = re.sub(r"/+", "/", path.strip().replace("\\", "/"))
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    if not p or ".." in p.split("/"):
        return None
    return p


def _fold(path: str) -> str:
    """Windows paths compare without regard to case, everything else exactly."""
    return path.lower() if _DRIVE.match(path) else path


def _under(path: str, prefix: str) -> bool:
    a, b = _fold(path), _fold(prefix)
    if b == "/":
        return a.startswith("/")
    return a == b or a.startswith(b + "/")


def map_path(path: str, mappings: Sequence[PathMapping]) -> str | None:
    """Rewrite a Bindery path into Kavita's view of it. The longest prefix wins.

    With no mappings configured the path is used as it is. With mappings and no
    match the path is returned unchanged, because the folder may be mounted at
    the same place in both.
    """
    norm = normalize(path)
    if norm is None:
        return None
    best: tuple[int, str] | None = None
    for m in mappings:
        src, dst = normalize(m.bindery), normalize(m.kavita)
        if src is None or dst is None or not _under(norm, src):
            continue
        if best is None or len(src) > best[0]:
            rest = norm[len(src) :] if src != "/" else norm
            best = (len(src), (dst.rstrip("/") + rest) if dst != "/" else rest or "/")
    return best[1] if best else norm


def libraries_for_path(path: str, libraries: Iterable[Library]) -> list[int]:
    """Ids of the libraries whose most specific folder contains `path`."""
    hits: list[tuple[int, int]] = []
    for lib in libraries:
        for folder in lib.folders:
            f = normalize(folder)
            if f is not None and _under(path, f):
                hits.append((len(f), lib.id))
    if not hits:
        return []
    deepest = max(length for length, _ in hits)
    return sorted({lib_id for length, lib_id in hits if length == deepest})
