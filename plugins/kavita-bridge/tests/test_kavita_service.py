"""Debounce, path mapping and what is left to do when nothing matches."""

from __future__ import annotations

import threading

from conftest import make_config, wait_for
from fake_kavita import FakeKavita
from kavita_bridge.config import PathMapping
from kavita_bridge.kavita import KavitaClient
from kavita_bridge.service import ScanService


class ManualTimer:
    """Stands in for threading.Timer so a test decides when the window ends."""

    created: list[ManualTimer] = []

    def __init__(self, interval: float, function) -> None:
        self.interval, self.function = interval, function
        self.daemon = False
        self.cancelled = False
        ManualTimer.created.append(self)

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.function()


def build(kavita: FakeKavita, manual: bool = True, **overrides):
    ManualTimer.created = []
    config = make_config(kavita.url, **overrides)
    client = KavitaClient(config.kavita_url, config.kavita_api_key, config.auth_mode, 2.0)
    kwargs = {"timer_factory": ManualTimer} if manual else {}
    return ScanService(config, client, **kwargs)


class TestDebounce:
    def test_a_burst_is_one_scan(self, kavita):
        service = build(kavita)
        for _ in range(25):
            service.submit(None)
        assert len(ManualTimer.created) == 1  # one window for the whole burst
        assert kavita.scans() == []  # nothing yet: still inside the window
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan-all", None)]

    def test_the_next_burst_gets_its_own_scan(self, kavita):
        service = build(kavita)
        service.submit(None)
        ManualTimer.created[0].fire()
        service.submit(None)
        service.submit(None)
        assert len(ManualTimer.created) == 2
        ManualTimer.created[1].fire()
        assert len(kavita.scans()) == 2

    def test_the_window_comes_from_config(self, kavita):
        service = build(kavita, debounce_seconds=45)
        service.submit(None)
        assert ManualTimer.created[0].interval == 45

    def test_real_timer_collapses_a_burst(self, kavita):
        service = build(kavita, manual=False, debounce_seconds=0.3)
        for _ in range(10):
            service.submit(None)
        assert wait_for(lambda: kavita.scans())
        threading.Event().wait(0.5)  # a late duplicate would show up by now
        assert kavita.scans() == [("/api/library/scan-all", None)]

    def test_events_during_a_slow_flush_are_not_lost(self, kavita):
        service = build(kavita)
        service.submit(None)
        first = ManualTimer.created[0]
        service.submit(None)  # same window
        first.fire()
        service.submit(None)  # after the flush: a new window
        assert len(ManualTimer.created) == 2

    def test_stop_flushes_what_is_pending(self, kavita):
        service = build(kavita)
        service.submit(None)
        service.stop()
        assert len(kavita.scans()) == 1
        assert ManualTimer.created[0].cancelled
        service.submit(None)  # after stop, nothing is accepted
        assert len(ManualTimer.created) == 1


class TestLibrarySelection:
    PATH_MODE = {"scan_mode": "path"}

    def test_path_picks_the_library_that_holds_it(self, kavita):
        service = build(kavita, **self.PATH_MODE)
        service.submit("/data/books/Author/Title/book.epub")
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan", "1")]

    def test_second_folder_of_a_library(self, kavita):
        service = build(kavita, **self.PATH_MODE)
        service.submit("/data/manga/Series/vol1.cbz")
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan", "2")]

    def test_a_burst_across_libraries_is_one_scan_each(self, kavita):
        service = build(kavita, **self.PATH_MODE)
        for p in ("/data/books/a.epub", "/data/books/b.epub", "/data/comics/c.cbz"):
            service.submit(p)
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan", "1"), ("/api/library/scan", "2")]
        assert len([r for r in kavita.requests if r["path"].endswith("/libraries")]) == 1

    def test_prefix_map_translates_bindery_paths(self, kavita):
        service = build(
            kavita,
            **self.PATH_MODE,
            path_map=(PathMapping("/media/library", "/data"),),
        )
        service.submit("/media/library/comics/Series/c.cbz")
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan", "2")]

    def test_the_map_works_on_whole_segments(self, kavita):
        service = build(kavita, **self.PATH_MODE, path_map=(PathMapping("/media/lib", "/data"),))
        service.submit("/media/library/books/a.epub")  # /media/lib is not a parent of this
        ManualTimer.created[0].fire()
        assert kavita.scans() == []

    def test_a_folder_name_prefix_is_not_a_match(self, kavita):
        service = build(kavita, **self.PATH_MODE)
        service.submit("/data/books-archive/a.epub")  # /data/books is a prefix, not a parent
        ManualTimer.created[0].fire()
        assert kavita.scans() == []

    def test_parent_traversal_is_never_mapped(self, kavita):
        service = build(kavita, **self.PATH_MODE)
        service.submit("/data/books/../comics/c.cbz")
        ManualTimer.created[0].fire()
        assert kavita.scans() == []

    def test_all_mode_ignores_the_path(self, kavita):
        service = build(kavita, scan_mode="all")
        service.submit("/data/books/a.epub")
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan-all", None)]
        assert not [r for r in kavita.requests if r["path"].endswith("/libraries")]


class TestUnmapped:
    def test_default_is_to_do_nothing(self, kavita):
        service = build(kavita, scan_mode="path")
        service.submit("/elsewhere/a.epub")
        service.submit(None)
        ManualTimer.created[0].fire()
        assert kavita.scans() == []

    def test_all_falls_back_to_scan_all_once(self, kavita):
        service = build(kavita, scan_mode="path", unmapped="all")
        service.submit("/elsewhere/a.epub")
        service.submit(None)
        service.submit("/data/books/b.epub")
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan-all", None)]

    def test_libraries_falls_back_to_the_listed_ids(self, kavita):
        service = build(kavita, scan_mode="path", unmapped="libraries", unmapped_libraries=(2, 1))
        service.submit(None)  # an ebook event carries no path
        ManualTimer.created[0].fire()
        assert kavita.scans() == [("/api/library/scan", "1"), ("/api/library/scan", "2")]

    def test_listing_failure_falls_back_to_the_policy(self, kavita):
        service = build(kavita, scan_mode="path", unmapped="all", kavita_api_key="wrongwrongwrong")
        service.submit("/data/books/a.epub")
        ManualTimer.created[0].fire()  # must not raise
        # Listing failed, so the policy applied: scan-all was attempted (and refused,
        # because the key is wrong, which must not raise either).
        assert kavita.scans() == [("/api/library/scan-all", None)]

    def test_force_is_passed_through(self, kavita):
        service = build(kavita, force=True)
        service.submit(None)
        ManualTimer.created[0].fire()
        assert kavita.requests[-1]["query"]["force"] == "true"
