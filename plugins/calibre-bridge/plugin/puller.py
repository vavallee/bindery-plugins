"""Pull mode: the plugin fetches pending books from Bindery.

In push mode Bindery calls the plugin with a file path, so Calibre has to see
Bindery's files (a shared drive, a path remap) and Bindery has to reach
Calibre (an open inbound port, a fixed address). In pull mode the plugin
connects out instead: it lists the deliveries Bindery has queued for it,
downloads each file into a private temp directory, adds it through the same
:func:`adder.add_book_detailed` the push handler uses, and acknowledges.

Delivery is at least once. A book that was added but whose ack never reached
Bindery is listed again on the next pass; the ``bindery`` identifier dedupe
then answers "duplicate", and the retry is acknowledged as ``already``
without a second row.

:meth:`PullWorker.run_once` is one complete pass and is what the tests
drive. :meth:`PullWorker.start` runs it on a daemon thread on a schedule.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

try:
    from calibre_plugins.bindery_bridge.plugin import bindery_client, status
except ImportError:  # the test suite and calibre-debug load the plugin files directly
    import bindery_client  # type: ignore[no-redef]
    import status  # type: ignore[no-redef]

_log = logging.getLogger(__name__)

FIRST_RUN_DELAY = 10.0
MAX_BACKOFF = 15 * 60.0
AUTH_BACKOFF = 60 * 60.0
# Bindery answered but is set to push, or has no pull routes yet. Nothing the
# plugin does will change that, so check back now and then, not every minute.
MODE_BACKOFF = 5 * 60.0
MAX_RETRY_AFTER = 60 * 60.0
DEFAULT_BATCH = 20
# A pass stops after this many pages even if Bindery keeps handing out
# cursors, so a server bug cannot keep the worker busy forever.
MAX_PAGES_PER_PASS = 50
COVER_LIMIT = 16 * 1024 * 1024
# Bindery caps nack error text at 2000 characters.
MAX_NACK_ERROR = 2000

# A format is only ever used as a file extension on a name the plugin makes
# up. Never a server supplied file name.
_FORMAT_RE = re.compile(r"^[a-z0-9]{1,10}$")
# Kobo's kepub is sent as ``kepub.epub``; Calibre files it as KEPUB.
_FORMAT_ALIASES = {"kepub.epub": "kepub"}
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_COVER_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

OUTCOME_ADDED = "added"
OUTCOME_ALREADY = "already"
OUTCOME_FORMAT_ADDED = "format_added"


@dataclass
class PullStats:
    """What one pass did. ``next_delay`` is how long to wait before the next."""

    listed: int = 0
    added: int = 0
    already: int = 0
    format_added: int = 0
    nacked: int = 0
    ack_failed: int = 0
    skipped: int = 0
    # Ack or nack answered 409 not_pending or 404: Bindery had already
    # settled the delivery, so there is nothing left to do.
    settled: int = 0
    # Acks of ``add`` deliveries, which is what can make Bindery list a
    # book's other formats as ``add_format``.
    acked_adds: int = 0
    relisted: bool = False
    pending: int | None = None
    status: str = ""
    error: str = ""
    # None: the normal interval. Anything else overrides it.
    next_delay: float | None = None
    unreachable: bool = False
    paused: bool = False
    acks: list[dict[str, Any]] = field(default_factory=list)


class _Stop(Exception):
    """Raised inside a pass to end it early without an error."""


def delivery_extension(fmt: Any) -> str:
    """Validate a delivery ``format`` and return the extension to write.

    Raises ValueError for anything that is not a short lower case token, so
    a crafted ``../../x`` or ``epub/../../evil`` never reaches a file name.
    """
    if not isinstance(fmt, str):
        raise ValueError(f"format must be a string, got {fmt!r}")
    cleaned = fmt.strip().lower()
    cleaned = _FORMAT_ALIASES.get(cleaned, cleaned)
    if not _FORMAT_RE.match(cleaned):
        raise ValueError(f"unusable format {fmt!r}")
    return cleaned


def library_id(db: Any) -> str:
    """The open library's id, or "" when it cannot be read.

    ``Cache.library_id`` on the new API, and ``LibraryDatabase.library_id`` on
    the legacy wrapper Calibre hands a plugin as ``gui.current_db``. Both were
    checked on Calibre 9.14.
    """
    for owner in (getattr(db, "new_api", None), db):
        if owner is None:
            continue
        try:
            value = owner.library_id
        # Fall through to the next spelling.
        except Exception:  # nosec B112
            continue
        if isinstance(value, str) and value:
            return value
    return ""


def _library_path(db: Any) -> str:
    try:
        return str(db.library_path)
    except Exception:
        return ""


def _default_adder() -> Any:
    try:
        from calibre_plugins.bindery_bridge.plugin import adder
    except ImportError:
        import adder  # type: ignore[no-redef]
    return adder


class PullWorker:
    def __init__(
        self,
        get_db: Callable[[], Any],
        on_added: Callable[[int], Any] | None,
        on_updated: Callable[[int], Any] | None,
        load_config: Callable[[], dict[str, Any]],
        client_factory: Callable[[dict[str, Any]], Any],
        adder: Any = None,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._get_db = get_db
        self._on_added = on_added
        self._on_updated = on_updated
        self._load_config = load_config
        self._client_factory = client_factory
        self._adder = adder
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._wait = wait
        self._thread: threading.Thread | None = None
        self._failures = 0
        self.last_stats: PullStats | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bindery-bridge-pull", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def wake(self) -> None:
        """Run a pass now instead of at the next scheduled time."""
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _sleep(self, seconds: float) -> None:
        if self._wait is not None:
            self._wait(seconds)
            return
        self._wake.wait(timeout=max(0.0, seconds))
        self._wake.clear()

    def _loop(self) -> None:
        delay = FIRST_RUN_DELAY
        while not self._stop.is_set():
            self._sleep(delay)
            if self._stop.is_set():
                break
            try:
                stats = self.run_once()
            except Exception as exc:  # pragma: no cover - run_once catches its own
                _log.exception("pull pass failed")
                stats = PullStats(error=str(exc), unreachable=True)
            delay = self.next_delay(stats)

    def next_delay(self, stats: PullStats) -> float:
        """Seconds until the next pass, with backoff while Bindery is down."""
        interval = self._interval()
        if stats.unreachable:
            self._failures += 1
            backoff = interval * (2 ** min(self._failures, 16))
            return float(min(backoff, MAX_BACKOFF))
        self._failures = 0
        if stats.next_delay is not None:
            return float(stats.next_delay)
        return interval

    def _interval(self) -> float:
        try:
            value = float(self._load_config().get("pull_interval_seconds", 60))
        except Exception:
            value = 60.0
        return max(5.0, value)

    # -- one pass ------------------------------------------------------------

    def run_once(self) -> PullStats:
        stats = PullStats()
        with contextlib.suppress(_Stop):
            self._run(stats)
        self.last_stats = stats
        if stats.status:
            status.set_pull(detail=stats.status)
        if stats.error:
            status.set_pull(last_error=stats.error)
        return stats

    def _run(self, stats: PullStats) -> None:
        cfg = self._load_config()
        if not cfg.get("pull_enabled") or not str(cfg.get("bindery_url") or "").strip():
            status.set_pull(enabled=False)
            stats.status = "Pull from Bindery is off"
            return
        status.set_pull(
            enabled=True,
            warning=bindery_client.http_warning(str(cfg.get("bindery_url") or "")),
        )
        db = self._get_db()
        if db is None:
            stats.status = "Waiting for a Calibre library to open"
            return
        expected_library = str(cfg.get("pull_library_id") or "")
        if not self._library_ok(db, expected_library, stats):
            return
        status.set_pull(last_run=self._clock())
        try:
            client = self._client_factory(cfg)
        except (ValueError, OSError) as exc:
            stats.status = "Pull is not configured correctly"
            stats.error = str(exc)
            stats.next_delay = MODE_BACKOFF
            return
        try:
            hello = client.hello()
        except bindery_client.BinderyError as exc:
            self._fail(stats, exc, "hello")
            return
        if hello.get("transport") == "push":
            stats.status = "Bindery is set to push; switch Settings, Calibre, Transport to Pull"
            stats.next_delay = MODE_BACKOFF
            return
        try:
            batch = int(hello.get("maxBatch") or DEFAULT_BATCH)
        except (TypeError, ValueError):
            batch = DEFAULT_BATCH
        batch = max(1, min(batch, DEFAULT_BATCH))
        self._drain(client, db, expected_library, batch, stats)
        if not stats.status:
            stats.status = self._done_status(stats)

    def _library_ok(self, db: Any, expected: str, stats: PullStats) -> bool:
        if expected and library_id(db) != expected:
            stats.status = (
                "Paused: a different library is open. Pull delivers only to the "
                "library it was turned on in; turn it off and on again to move it"
            )
            stats.paused = True
            return False
        return True

    def _still_same_library(self, db: Any, expected: str, stats: PullStats) -> bool:
        current = self._get_db()
        if current is not db or (expected and library_id(current) != expected):
            stats.status = (
                "Paused: the Calibre library changed during a pull; the rest waits "
                "until its library is open again"
            )
            stats.paused = True
            return False
        return True

    def _drain(self, client: Any, db: Any, expected: str, batch: int, stats: PullStats) -> None:
        """List and deliver, then list once more if that can unlock formats.

        Bindery holds a book's second format back until the first file of
        that book is acknowledged, and then lists it as ``add_format``. One
        extra listing from the start, after a sweep that acked at least one
        ``add``, puts a two format book into Calibre in one pass instead of
        two. Never more than one: a Bindery that keeps producing new rows is
        left for the next pass.
        """
        seen: set[str] = set()
        if not self._sweep(client, db, expected, batch, stats, seen):
            return
        if stats.acked_adds:
            stats.relisted = True
            self._sweep(client, db, expected, batch, stats, seen)

    def _sweep(
        self,
        client: Any,
        db: Any,
        expected: str,
        batch: int,
        stats: PullStats,
        seen: set[str],
    ) -> bool:
        """One walk through every page. False when the pass has to end.

        Bindery pages by whole book, so a page can hold more than ``batch``
        rows when one book has more files than that; nothing here assumes
        otherwise.
        """
        cursor = ""
        for _page in range(MAX_PAGES_PER_PASS):
            try:
                page = client.deliveries(limit=batch, cursor=cursor)
            except bindery_client.BinderyError as exc:
                self._fail(stats, exc, "deliveries")
                return False
            deliveries = page.get("deliveries") or []
            if not isinstance(deliveries, list):
                stats.error = "Bindery sent a delivery list that is not a list"
                return False
            pending = page.get("pending")
            if isinstance(pending, int):
                stats.pending = pending
            for delivery in deliveries:
                if self._stop.is_set():
                    raise _Stop()
                if not self._still_same_library(db, expected, stats):
                    raise _Stop()
                if not isinstance(delivery, dict):
                    stats.skipped += 1
                    continue
                delivery_id = str(delivery.get("id") or "")
                if delivery_id in seen:
                    continue
                seen.add(delivery_id)
                stats.listed += 1
                try:
                    self._deliver(client, db, delivery, stats)
                except bindery_client.BinderyUnreachable as exc:
                    self._fail(stats, exc, f"delivery {delivery_id}")
                    return False
            next_cursor = page.get("nextCursor")
            if not deliveries or not next_cursor or not isinstance(next_cursor, str):
                return True
            cursor = str(next_cursor)
        return True

    def _fail(self, stats: PullStats, exc: bindery_client.BinderyError, what: str) -> None:
        stats.error = f"{what}: {exc}"
        _log.warning("pull %s failed: %s", what, exc)
        if isinstance(exc, bindery_client.BinderyUnreachable) or exc.status >= 500:
            stats.status = "Cannot reach Bindery; retrying with backoff"
            stats.unreachable = True
        elif isinstance(exc, bindery_client.RedirectRefused):
            stats.status = (
                "Bindery redirected the request; set the Bindery URL to the final address"
            )
            stats.next_delay = MODE_BACKOFF
        elif exc.status == 401:
            stats.status = (
                "Bindery rejected the API key; set the same key here and in Bindery "
                "Settings, Calibre"
            )
            stats.next_delay = AUTH_BACKOFF
        elif exc.status == 409 and exc.code == "not_in_pull_mode":
            stats.status = "Bindery is set to push; switch Settings, Calibre, Transport to Pull"
            stats.next_delay = MODE_BACKOFF
        elif exc.status == 404 or exc.code == "not_json":
            # A Bindery without the pull routes serves its web UI page for
            # any unknown path, so this is usually a 200 that is not JSON.
            stats.status = "This Bindery has no pull routes; update Bindery or check the URL"
            stats.next_delay = MAX_BACKOFF
        elif exc.status == 429:
            stats.status = "Bindery asked the plugin to slow down"
            retry = exc.retry_after if exc.retry_after is not None else self._interval()
            stats.next_delay = max(1.0, min(float(retry), MAX_RETRY_AFTER))
        else:
            stats.status = f"Bindery refused the request ({exc})"
            stats.next_delay = MODE_BACKOFF

    @staticmethod
    def _done_status(stats: PullStats) -> str:
        delivered = stats.added + stats.format_added + stats.already
        if stats.listed == 0:
            return "Connected to Bindery; nothing waiting"
        return f"Connected to Bindery; {delivered} of {stats.listed} delivered in the last pass"

    # -- one delivery --------------------------------------------------------

    def _deliver(self, client: Any, db: Any, delivery: dict[str, Any], stats: PullStats) -> None:
        delivery_id = str(delivery.get("id") or "")
        if not _DELIVERY_ID_RE.match(delivery_id) or delivery_id in (".", ".."):
            _log.warning("pull: skipping a delivery with an unusable id %r", delivery_id)
            stats.skipped += 1
            return
        try:
            ext = delivery_extension(delivery.get("format"))
        except ValueError as exc:
            self._nack(client, delivery_id, "bad_format", str(exc), False, stats)
            return
        action = delivery.get("action") or "add"
        if action not in ("add", "add_format"):
            self._nack(
                client, delivery_id, "invalid_metadata", f"unknown action {action!r}", False, stats
            )
            return
        metadata = delivery.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            self._nack(
                client, delivery_id, "invalid_metadata", "metadata must be an object", False, stats
            )
            return
        # coverPath is a local file path in push mode. Here only the plugin
        # may set it, to the cover it downloaded itself; a path from Bindery
        # would make the adder read any file this process can open.
        metadata = dict(metadata)
        metadata.pop("coverPath", None)

        adder = self._adder if self._adder is not None else _default_adder()
        tmpdir = tempfile.mkdtemp(prefix="bindery-pull-")
        try:
            book_path = os.path.join(tmpdir, f"book.{ext}")
            try:
                client.download_file(delivery_id, book_path)
            except bindery_client.TooLarge as exc:
                self._nack(client, delivery_id, "body_too_large", str(exc), False, stats)
                return
            except bindery_client.BinderyUnreachable:
                raise
            except bindery_client.BinderyError as exc:
                if exc.status == 403 and exc.code == "path_forbidden":
                    # Bindery will not serve this file (outside its library
                    # roots, or not a regular file). Retrying cannot help.
                    self._nack(client, delivery_id, "path_forbidden", str(exc), False, stats)
                    return
                _log.warning("pull: download of delivery %s failed: %s", delivery_id, exc)
                stats.error = f"download {delivery_id}: {exc}"
                stats.skipped += 1
                return
            cover_wanted = bool(delivery.get("hasCover"))
            if cover_wanted:
                cover_path = self._download_cover(client, delivery_id, tmpdir)
                if cover_path:
                    metadata["coverPath"] = cover_path
            try:
                result = adder.add_book_detailed(
                    db,
                    book_path,
                    metadata=metadata,
                    ingest_root="",
                    on_added=self._on_added,
                    on_updated=self._on_updated,
                    add_format=(action == "add_format"),
                )
            except Exception as exc:
                code, retryable = _error_code(adder, exc)
                _log.warning("pull: adding delivery %s failed (%s): %s", delivery_id, code, exc)
                self._nack(client, delivery_id, code, str(exc), retryable, stats)
                return
            if result.format_added:
                outcome = OUTCOME_FORMAT_ADDED
                stats.format_added += 1
            elif result.duplicate:
                outcome = OUTCOME_ALREADY
                stats.already += 1
            else:
                outcome = OUTCOME_ADDED
                stats.added += 1
            cover_applied = result.cover_applied
            if cover_wanted and cover_applied is None and outcome == OUTCOME_ADDED:
                # Bindery said there was a cover and none could be fetched.
                cover_applied = False
            body = {
                "calibreId": int(result.book_id or 0),
                "outcome": outcome,
                "coverApplied": cover_applied,
                "library": _library_path(db),
            }
            try:
                client.ack(delivery_id, body)
            except bindery_client.BinderyError as exc:
                if _already_settled(exc):
                    # Delivered elsewhere, failed, skipped or gone on
                    # Bindery's side. Nothing to retry and nothing wrong here.
                    _log.debug("pull: ack of delivery %s not needed: %s", delivery_id, exc)
                    stats.settled += 1
                    return
                # The book is in Calibre; Bindery will list it again and the
                # dedupe turns the retry into an ``already`` ack.
                _log.warning("pull: ack of delivery %s failed: %s", delivery_id, exc)
                stats.ack_failed += 1
                stats.error = f"ack {delivery_id}: {exc}"
                return
            stats.acks.append({"id": delivery_id, **body})
            if action == "add":
                stats.acked_adds += 1
            status.add_pull_delivered(1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _download_cover(self, client: Any, delivery_id: str, tmpdir: str) -> str:
        """Fetch the cover next to the book. "" when there is none or it failed.

        A cover never fails the delivery: the book is added without it, as a
        push with an unreadable ``coverPath`` is.
        """
        partial = os.path.join(tmpdir, "cover.part")
        try:
            ctype = client.download_cover(delivery_id, partial, COVER_LIMIT)
        except bindery_client.BinderyUnreachable:
            raise
        except bindery_client.BinderyError as exc:
            _log.warning("pull: cover of delivery %s not fetched: %s", delivery_id, exc)
            return ""
        if ctype is None:
            return ""
        ext = _COVER_TYPES.get(ctype.split(";")[0].strip().lower(), "jpg")
        final = os.path.join(tmpdir, f"cover.{ext}")
        shutil.move(partial, final)
        return final

    def _nack(
        self,
        client: Any,
        delivery_id: str,
        code: str,
        message: str,
        retryable: bool,
        stats: PullStats,
    ) -> None:
        stats.nacked += 1
        body = {"code": code, "error": message[:MAX_NACK_ERROR], "retryable": retryable}
        try:
            client.nack(delivery_id, body)
        except bindery_client.BinderyUnreachable:
            raise
        except bindery_client.BinderyError as exc:
            if _already_settled(exc):
                _log.debug("pull: nack of delivery %s not needed: %s", delivery_id, exc)
                stats.settled += 1
                return
            _log.warning("pull: nack of delivery %s failed: %s", delivery_id, exc)
            stats.error = f"nack {delivery_id}: {exc}"


def _already_settled(exc: bindery_client.BinderyError) -> bool:
    """Bindery no longer has this delivery pending: done, move on.

    409 ``not_pending`` on an ack means the row was delivered to another
    Calibre id, or already failed or was skipped; on a nack, any row that is
    not pending. 404 is an id Bindery does not know. None of these is a
    reason to retry or back off.
    """
    if isinstance(exc, bindery_client.BinderyUnreachable):
        return False
    return bool((exc.status == 409 and exc.code == "not_pending") or exc.status == 404)


def _error_code(adder: Any, exc: Exception) -> tuple[str, bool]:
    """Map an adder exception to the push API's error code and retryability."""
    if isinstance(exc, adder.BadFormat):
        return "bad_format", False
    if isinstance(exc, adder.PathForbidden):
        return "path_forbidden", False
    if isinstance(exc, adder.CopyFailed):
        return "copy_failed", True
    if isinstance(exc, adder.SourceUnreadable):
        return "path_unreadable", True
    return "internal", True
