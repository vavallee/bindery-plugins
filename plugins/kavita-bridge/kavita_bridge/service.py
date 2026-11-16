"""Debounce and library selection.

Bindery sends one `bookImported` event per imported book, so a bulk import is a
burst. The first event of a burst starts a timer; every event that arrives
before it fires joins the same batch, and the batch becomes one scan per
Kavita library (or a single scan-all). Events after the timer fired start the
next batch.

A fixed window from the first event, not a window that restarts on each event,
so a steady trickle cannot hold the scan back forever.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .config import Config
from .kavita import Health, KavitaError, Library
from .paths import libraries_for_path, map_path

log = logging.getLogger("kavita_bridge")


class Scanner(Protocol):
    """The part of KavitaClient the service uses. Tests fake it."""

    def libraries(self) -> list[Library]: ...
    def scan_library(self, library_id: int, force: bool = False) -> None: ...
    def scan_all(self, force: bool = False) -> None: ...
    def check(self) -> Health: ...


@dataclass
class _Batch:
    scan_all: bool = False
    library_ids: set[int] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    events: int = 0
    skipped: int = 0

    def empty(self) -> bool:
        return not (self.scan_all or self.library_ids or self.paths or self.skipped)


def printable(text: str, limit: int = 200) -> str:
    """Text from a request, safe for a log line: no control characters, bounded."""
    cleaned = "".join(c if c.isprintable() else "?" for c in text)
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


class ScanService:
    def __init__(
        self,
        config: Config,
        client: Scanner,
        timer_factory: Callable[[float, Callable[[], None]], threading.Timer] = threading.Timer,
    ) -> None:
        self._config = config
        self._client = client
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._batch = _Batch()
        self._timer: threading.Timer | None = None
        self._stopped = False

    # -- intake --------------------------------------------------------------

    def submit(self, path: str | None) -> None:
        """Record one imported book. Returns at once; Kavita is called later."""
        with self._lock:
            if self._stopped:
                return
            self._batch.events += 1
            if self._config.scan_mode == "all":
                self._batch.scan_all = True
            elif path:
                self._batch.paths.add(path)
            else:
                self._apply_unmapped(self._batch, "no path in the event")
            if self._timer is None:
                self._timer = self._timer_factory(self._config.debounce_seconds, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def _apply_unmapped(self, batch: _Batch, why: str) -> None:
        action = self._config.unmapped
        if action == "all":
            batch.scan_all = True
        elif action == "libraries":
            batch.library_ids.update(self._config.unmapped_libraries)
        else:
            batch.skipped += 1
            log.info("not scanning: %s and unmapped is skip", why)

    # -- the scan ------------------------------------------------------------

    def flush(self) -> None:
        """Turn the pending batch into scans. Runs on the timer thread."""
        with self._flush_lock:
            with self._lock:
                batch, self._batch = self._batch, _Batch()
                self._timer = None
            if batch.empty():
                return
            try:
                self._scan(batch)
            except Exception as exc:  # a timer thread must never die silently
                log.error("scan batch failed (%s)", type(exc).__name__)

    def _scan(self, batch: _Batch) -> None:
        if batch.paths:
            self._resolve(batch)
        force = self._config.force
        if batch.scan_all:
            log.info("scanning all libraries (%d import events)", batch.events)
            self._guarded(self._client.scan_all, force)
            return
        for library_id in sorted(batch.library_ids):
            log.info("scanning library %d (%d import events)", library_id, batch.events)
            self._guarded(self._client.scan_library, library_id, force)

    def _resolve(self, batch: _Batch) -> None:
        try:
            libraries = self._client.libraries()
        except KavitaError as exc:
            log.error("cannot list Kavita libraries: %s", exc)
            libraries = []
        for path in sorted(batch.paths):
            mapped = map_path(path, self._config.path_map)
            ids = libraries_for_path(mapped, libraries) if mapped else []
            if ids:
                batch.library_ids.update(ids)
            else:
                self._apply_unmapped(batch, f"no Kavita library holds {printable(path)!r}")
        batch.paths.clear()

    @staticmethod
    def _guarded(call: Callable[..., None], *args: object) -> None:
        try:
            call(*args)
        except KavitaError as exc:
            log.error("Kavita scan failed: %s", exc)

    # -- lifecycle -----------------------------------------------------------

    def stop(self) -> None:
        """Stop taking events and run whatever is pending once."""
        with self._lock:
            self._stopped = True
            timer = self._timer
        if timer is not None:
            timer.cancel()
        self.flush()
