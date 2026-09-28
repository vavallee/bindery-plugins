"""calibre-bridge 0.8.0: pull mode (vavallee/bindery#2833).

The plugin connects out to Bindery's ``/bridge/v1`` routes, lists pending
deliveries, downloads each one into a temp directory, adds it with the same
adder the push handler uses, and acknowledges. ``FakeBindery`` below
implements that contract, protocol 1, over real HTTP so the client's
redirect, size and TLS handling are exercised for real.
"""

import importlib
import json
import os
import pathlib
import shutil
import ssl
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from .conftest import free_port
from .test_dedupe_ladder import FakeMetadata
from .test_empty_rows import FormatLibrary

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin"
API_KEY = "pull-key"


# ── the Bindery side ─────────────────────────────────────────────────────────


class BinderyState:
    def __init__(self):
        self.transport = "pull"
        self.queue = {}  # id -> {"delivery", "file", "cover", "cover_type"}
        self.order = []
        self.acks = []
        self.nacks = []
        self.requests = []
        # Per route overrides: route -> list of (status, body, headers), used up in order.
        self.overrides = {}
        self.stream_without_length = False
        self.acked_books = set()
        self.list_calls = []
        self.on_list = None
        self.file_headers = {}
        self.lock = threading.Lock()

    def add(self, delivery_id, fmt="epub", action="add", data=b"book bytes", cover=None, **extra):
        bindery_id = extra.pop("bindery", "7")
        book_id = extra.pop("book_id", int(bindery_id))
        metadata = extra.pop("metadata", None) or {
            "title": extra.pop("title", "The Dispossessed"),
            "authors": ["Ursula K. Le Guin"],
            "identifiers": {"bindery": bindery_id},
        }
        delivery = {
            "id": delivery_id,
            "bookId": book_id,
            "format": fmt,
            "sizeBytes": len(data),
            "action": action,
            "metadata": metadata,
            "hasCover": cover is not None,
            **extra,
        }
        self.queue[delivery_id] = {"delivery": delivery, "file": data, "cover": cover}
        self.order.append(delivery_id)

    def override(self, route, status, body=None, headers=None):
        self.overrides.setdefault(route, []).append((status, body, headers or {}))

    def visible(self):
        """What Bindery lists: a book's ``add_format`` rows only once one of
        its files has been acked, as the real queue holds them back."""
        return [
            i
            for i in self.order
            if i in self.queue
            and (
                self.queue[i]["delivery"]["action"] != "add_format"
                or self.queue[i]["delivery"]["bookId"] in self.acked_books
            )
        ]

    def pending(self):
        return [i for i in self.order if i in self.queue]


def make_fake_bindery(state):
    class FakeBindery(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, status, payload, headers=None):
            if isinstance(payload, bytes):
                # Raw bytes stand in for a non JSON page, such as the web UI
                # an older Bindery serves for an unknown path.
                body = payload
            else:
                body = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            if isinstance(payload, bytes):
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _override(self, route):
            queued = state.overrides.get(route)
            if not queued:
                return False
            status, body, headers = queued.pop(0)
            self._json(status, body, headers)
            return True

        def _record(self):
            state.requests.append((self.command, self.path, dict(self.headers)))

        def _authorized(self):
            if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                self._json(401, {"error": "unauthorized", "code": "unauthorized"})
                return False
            return True

        def do_GET(self):  # noqa: N802
            self._record()
            if not self._authorized():
                return
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            if url.path == "/bridge/v1/hello":
                if self._override("hello"):
                    return
                self._json(
                    200,
                    {
                        "binderyVersion": "1.39.0",
                        "protocol": 1,
                        "maxBatch": 20,
                        "transport": state.transport,
                    },
                )
                return
            if url.path == "/bridge/v1/deliveries":
                if self._override("deliveries"):
                    return
                if state.transport != "pull":
                    self._json(409, {"error": "not in pull mode", "code": "not_in_pull_mode"})
                    return
                query = parse_qs(url.query)
                limit = int(query.get("limit", ["20"])[0])
                # Keyset on book id, whole books only, as Bindery pages: the
                # cursor is the last book id, and a page never splits a book,
                # so it can run past ``limit`` when one book has more files.
                after = query.get("cursor", [""])[0]
                state.list_calls.append((limit, after))
                if state.on_list is not None:
                    state.on_list(state)
                visible = state.visible()
                books = {}
                for i in visible:
                    book = state.queue[i]["delivery"]["bookId"]
                    if not after or book > int(after):
                        books.setdefault(book, []).append(i)
                page, last = [], None
                for book in sorted(books):
                    if page and len(page) + len(books[book]) > limit:
                        break
                    page.extend(books[book])
                    last = book
                more = last is not None and any(b > last for b in books)
                self._json(
                    200,
                    {
                        "deliveries": [state.queue[i]["delivery"] for i in page],
                        "nextCursor": str(last) if more else "",
                        "pending": len(visible),
                    },
                )
                return
            if len(parts) == 5 and parts[:3] == ["bridge", "v1", "deliveries"]:
                delivery_id, what = parts[3], parts[4]
                if self._override(what):
                    return
                item = state.queue.get(delivery_id)
                if item is None:
                    self._json(404, {"error": "not found", "code": "not_found"})
                    return
                if what == "file":
                    self._send_bytes(item["file"], "application/octet-stream")
                    return
                if what == "cover":
                    if item["cover"] is None:
                        self._json(404, {"error": "no cover", "code": "not_found"})
                        return
                    self._send_bytes(item["cover"], "image/png")
                    return
            self._json(404, {"error": "not found", "code": "not_found"})

        def _send_bytes(self, data, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            for k, v in state.file_headers.items():
                self.send_header(k, v)
            if state.stream_without_length:
                # HTTP/1.0 close delimited body: the client only learns the
                # size by reading it.
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True
                return
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            self._record()
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) == 5 and parts[:3] == ["bridge", "v1", "deliveries"]:
                delivery_id, what = parts[3], parts[4]
                if self._override(what):
                    return
                with state.lock:
                    if what == "ack":
                        state.acks.append((delivery_id, body))
                        item = state.queue.pop(delivery_id, None)
                        if item is not None:
                            state.acked_books.add(item["delivery"]["bookId"])
                    elif what == "nack":
                        state.nacks.append((delivery_id, body))
                        state.queue.pop(delivery_id, None)
                self._json(204, None)
                return
            self._json(404, {"error": "not found", "code": "not_found"})

    return FakeBindery


# ── the plugin side ──────────────────────────────────────────────────────────


class PullLibrary(FormatLibrary):
    def __init__(self, library_id="lib-1"):
        super().__init__()
        self.library_id = library_id
        self.add_paths = []

    def add_books(self, books, add_duplicates, run_hooks):
        _mi, format_map = books[0]
        for path in format_map.values():
            self.add_paths.append(path)
            # Calibre copies the file into the library before returning.
            assert os.path.isfile(path)
        return super().add_books(books, add_duplicates, run_hooks)


class PullDB:
    def __init__(self, library, path="/srv/calibre/Library"):
        self.new_api = library
        self.library_path = path


@pytest.fixture
def pull(bridge_adder):
    bridge_adder.get_metadata = lambda stream, fmt: FakeMetadata()
    sys.path.insert(0, str(_PLUGIN_DIR))
    names = ("puller", "bindery_client", "status", "handlers")
    for name in names:
        sys.modules.pop(name, None)
    try:
        yield types.SimpleNamespace(
            adder=bridge_adder,
            client=importlib.import_module("bindery_client"),
            status=importlib.import_module("status"),
            handlers=importlib.import_module("handlers"),
            puller=importlib.import_module("puller"),
        )
    finally:
        sys.path.remove(str(_PLUGIN_DIR))
        for name in names:
            sys.modules.pop(name, None)


class Harness:
    def __init__(self, ns, url, db=None, **cfg):
        self.ns = ns
        self.db = db or PullDB(PullLibrary())
        self.holder = {"db": self.db}
        self.added = []
        self.updated = []
        self.cfg = {
            "pull_enabled": True,
            "bindery_url": url,
            "api_key": API_KEY,
            "ca_file": "",
            "pull_interval_seconds": 60,
            "max_download_bytes": 1024 * 1024,
            "pull_library_id": "lib-1",
        }
        self.cfg.update(cfg)
        self.worker = ns.puller.PullWorker(
            get_db=lambda: self.holder["db"],
            on_added=self.added.append,
            on_updated=self.updated.append,
            load_config=lambda: dict(self.cfg),
            client_factory=self.client,
            adder=ns.adder,
        )

    def client(self, cfg):
        return self.ns.client.BinderyClient(
            cfg["bindery_url"],
            cfg["api_key"],
            self.ns.handlers.PLUGIN_VERSION,
            self.ns.handlers.CAPABILITIES,
            ca_file=cfg["ca_file"],
            max_download_bytes=cfg["max_download_bytes"],
        )

    @property
    def library(self):
        return self.db.new_api

    def run(self):
        return self.worker.run_once()


@pytest.fixture
def bindery(serve_bridge):
    state = BinderyState()
    bridge = serve_bridge(make_fake_bindery(state))
    state.url = f"http://127.0.0.1:{bridge.port}"
    return state


@pytest.fixture
def tempdirs(pull, monkeypatch):
    """Record every temp dir the puller makes."""
    made = []
    real = pull.puller.tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(pull.puller.tempfile, "mkdtemp", mkdtemp)
    return made


# ── happy path ───────────────────────────────────────────────────────────────


def test_add_then_add_format_lands_on_one_row(pull, bindery, tempdirs):
    bindery.add("d1", fmt="epub", cover=b"\x89PNG cover")
    bindery.add("d2", fmt="pdf", action="add_format")
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert (stats.added, stats.format_added, stats.nacked) == (1, 1, 0)
    assert len(h.library.rows) == 1
    book_id = next(iter(h.library.rows))
    assert h.library.formats(book_id) == ("EPUB", "PDF")
    assert bindery.acks == [
        (
            "d1",
            {
                "calibreId": book_id,
                "outcome": "added",
                "coverApplied": True,
                "library": "/srv/calibre/Library",
            },
        ),
        (
            "d2",
            {
                "calibreId": book_id,
                "outcome": "format_added",
                "coverApplied": None,
                "library": "/srv/calibre/Library",
            },
        ),
    ]
    assert h.added == [1]
    assert h.updated == [book_id]
    assert bindery.pending() == []
    assert tempdirs and not any(os.path.exists(d) for d in tempdirs)


def test_every_request_carries_the_contract_headers(pull, bindery):
    bindery.add("d1")
    Harness(pull, bindery.url).run()
    assert bindery.requests
    for _method, _path, headers in bindery.requests:
        assert headers["Authorization"] == f"Bearer {API_KEY}"
        assert headers["X-Bridge-Version"] == "0.8.0"
        assert headers["User-Agent"] == "calibre-bridge/0.8.0"
        caps = headers["X-Bridge-Capabilities"].split(",")
        assert caps == pull.handlers.CAPABILITIES
        assert "pull" in caps


def test_health_advertises_pull(bridge_handlers, serve_bridge):
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="k", get_db=lambda: None))
    status, payload, _ = bridge.call("GET", "/v1/health")
    assert status == 200
    assert "pull" in payload["capabilities"]
    assert payload["plugin_version"] == "0.8.0"


def test_pages_through_every_delivery(pull, bindery):
    for i in range(45):
        bindery.add(f"d{i}", bindery=str(100 + i), title=f"Book {i}")
    h = Harness(pull, bindery.url)
    stats = h.run()
    assert stats.added == 45
    assert len(h.library.rows) == 45
    assert bindery.pending() == []


# ── at least once: duplicates and lost acks ──────────────────────────────────


def test_a_book_already_in_calibre_is_acked_already(pull, bindery):
    h = Harness(pull, bindery.url)
    existing = h.library.seed("The Dispossessed", ["Ursula K. Le Guin"], {"bindery": "7"})
    h.library.formats_by_book[existing] = {"EPUB"}
    bindery.add("d1")

    stats = h.run()

    assert stats.already == 1
    assert bindery.acks[0][1]["outcome"] == "already"
    assert bindery.acks[0][1]["calibreId"] == existing
    assert len(h.library.rows) == 1


def test_a_lost_ack_is_re_added_as_already_with_one_row(pull, bindery, tempdirs):
    bindery.add("d1")
    bindery.override("ack", 500, {"error": "database locked", "code": "internal"})
    h = Harness(pull, bindery.url)

    first = h.run()
    assert (first.added, first.ack_failed) == (1, 1)
    assert bindery.pending() == ["d1"]

    second = h.run()
    assert second.already == 1
    assert bindery.acks == [
        (
            "d1",
            {
                "calibreId": 1,
                "outcome": "already",
                "coverApplied": None,
                "library": "/srv/calibre/Library",
            },
        )
    ]
    assert len(h.library.rows) == 1
    assert not any(os.path.exists(d) for d in tempdirs)


# ── size cap ─────────────────────────────────────────────────────────────────


def test_content_length_over_the_cap_is_refused_before_reading(pull, bindery, tempdirs):
    bindery.add("d1", data=b"x" * 5000)
    h = Harness(pull, bindery.url, max_download_bytes=1000)

    stats = h.run()

    assert stats.nacked == 1
    assert bindery.nacks[0][1]["code"] == "body_too_large"
    assert bindery.nacks[0][1]["retryable"] is False
    # Refused on the header, not after streaming 1000 bytes of it.
    assert "is 5000 bytes, over the 1000 byte limit" in bindery.nacks[0][1]["error"]
    assert h.library.rows == {}
    assert not any(os.path.exists(d) for d in tempdirs)


def test_a_stream_without_length_is_cut_off_at_the_cap(pull, bindery, tempdirs):
    bindery.add("d1", data=b"x" * 5000)
    bindery.stream_without_length = True
    h = Harness(pull, bindery.url, max_download_bytes=1000)

    stats = h.run()

    assert stats.nacked == 1
    assert bindery.nacks[0][1]["code"] == "body_too_large"
    assert "while downloading" in bindery.nacks[0][1]["error"]
    assert h.library.rows == {}
    assert not any(os.path.exists(d) for d in tempdirs)


def test_a_stream_under_the_cap_still_works(pull, bindery):
    bindery.add("d1", data=b"x" * 500)
    bindery.stream_without_length = True
    h = Harness(pull, bindery.url, max_download_bytes=1000)
    assert h.run().added == 1


# ── redirects ────────────────────────────────────────────────────────────────


def test_a_redirect_is_refused_and_the_key_never_leaves(pull, bindery, serve_bridge):
    elsewhere = BinderyState()
    other = serve_bridge(make_fake_bindery(elsewhere))
    bindery.add("d1")
    bindery.override(
        "file", 302, {}, {"Location": f"http://127.0.0.1:{other.port}/bridge/v1/deliveries/d1/file"}
    )
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert elsewhere.requests == []
    assert h.library.rows == {}
    assert bindery.acks == []
    assert "redirect" in stats.error.lower()


def test_a_redirect_on_hello_sets_a_status(pull, bindery):
    bindery.override("hello", 301, {}, {"Location": "https://example.invalid/bridge/v1/hello"})
    stats = Harness(pull, bindery.url).run()
    assert "redirect" in stats.status.lower()
    assert stats.listed == 0


# ── Bindery refusing ─────────────────────────────────────────────────────────


def test_a_wrong_key_backs_off_an_hour(pull, bindery):
    bindery.add("d1")
    h = Harness(pull, bindery.url, api_key="wrong")

    stats = h.run()

    assert "API key" in stats.status
    assert h.worker.next_delay(stats) == 3600
    assert h.library.rows == {}


def test_not_in_pull_mode_reports_the_setting(pull, bindery):
    bindery.add("d1")
    bindery.override("deliveries", 409, {"error": "push", "code": "not_in_pull_mode"})
    stats = Harness(pull, bindery.url).run()
    assert stats.status == "Bindery is set to push; switch Settings, Calibre, Transport to Pull"
    assert stats.next_delay == 300


def test_a_bindery_without_pull_routes_says_so(pull, bindery):
    """An older Bindery, or a URL missing its URL base, serves the web UI page
    with a 200 for /bridge/v1/hello rather than a 404."""
    bindery.add("d1")
    bindery.override("hello", 200, b"<!doctype html><html><body>Bindery</body></html>")
    stats = Harness(pull, bindery.url).run()
    assert stats.status == "This Bindery has no pull routes; update Bindery or check the URL"
    assert stats.next_delay == 15 * 60
    assert [p for _m, p, _h in bindery.requests] == ["/bridge/v1/hello"]


def test_a_404_on_hello_says_no_pull_routes(pull, bindery):
    bindery.override("hello", 404, {"error": "not found", "code": "not_found"})
    stats = Harness(pull, bindery.url).run()
    assert stats.status == "This Bindery has no pull routes; update Bindery or check the URL"


def test_hello_saying_push_stops_before_listing(pull, bindery):
    bindery.transport = "push"
    bindery.add("d1")
    stats = Harness(pull, bindery.url).run()
    assert stats.status == "Bindery is set to push; switch Settings, Calibre, Transport to Pull"
    assert [p for _m, p, _h in bindery.requests] == ["/bridge/v1/hello"]


def test_retry_after_is_honoured(pull, bindery):
    bindery.override(
        "deliveries", 429, {"error": "slow down", "code": "rate_limited"}, {"Retry-After": "42"}
    )
    h = Harness(pull, bindery.url)
    stats = h.run()
    assert h.worker.next_delay(stats) == 42


def test_unreachable_doubles_up_to_fifteen_minutes_and_resets(pull, bindery):
    h = Harness(pull, f"http://127.0.0.1:{free_port()}")
    delays = []
    for _ in range(6):
        stats = h.run()
        assert stats.unreachable
        delays.append(h.worker.next_delay(stats))
    assert delays == [120, 240, 480, 900, 900, 900]
    h.cfg["bindery_url"] = bindery.url
    assert h.worker.next_delay(h.run()) == 60


# ── library pause ────────────────────────────────────────────────────────────


def test_a_different_library_open_pauses_without_contacting_bindery(pull, bindery):
    bindery.add("d1")
    h = Harness(pull, bindery.url, db=PullDB(PullLibrary("lib-other")))
    stats = h.run()
    assert stats.paused
    assert "different library" in stats.status
    assert bindery.requests == []
    assert bindery.pending() == ["d1"]


def test_a_library_switch_mid_batch_pauses_the_rest(pull, bindery):
    bindery.add("d1", bindery="1", title="One")
    bindery.add("d2", bindery="2", title="Two")
    bindery.add("d3", bindery="3", title="Three")
    h = Harness(pull, bindery.url)
    other = PullDB(PullLibrary("lib-other"))

    def switch(count):
        h.holder["db"] = other

    h.worker._on_added = switch

    stats = h.run()

    assert stats.paused
    assert stats.added == 1
    assert [a[0] for a in bindery.acks] == ["d1"]
    assert bindery.pending() == ["d2", "d3"]
    assert other.new_api.rows == {}


# ── what Bindery may and may not choose ──────────────────────────────────────


def test_a_bad_format_is_nacked_non_retryable_without_downloading(pull, bindery, tempdirs):
    bindery.add("d1", fmt="../../evil")
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert bindery.nacks == [
        ("d1", {"code": "bad_format", "error": "unusable format '../../evil'", "retryable": False})
    ]
    assert stats.nacked == 1
    assert not any("/file" in p for _m, p, _h in bindery.requests)
    assert tempdirs == []


def test_an_adder_bad_format_is_nacked_non_retryable(pull, bindery, monkeypatch, tempdirs):
    bindery.add("d1")

    def refuse(*args, **kwargs):
        raise pull.adder.BadFormat("not a book")

    monkeypatch.setattr(pull.adder, "add_book_detailed", refuse)
    Harness(pull, bindery.url).run()
    assert bindery.nacks == [
        ("d1", {"code": "bad_format", "error": "not a book", "retryable": False})
    ]
    assert not any(os.path.exists(d) for d in tempdirs)


@pytest.mark.parametrize(
    ("exc_name", "code", "retryable"),
    [
        ("PathForbidden", "path_forbidden", False),
        ("CopyFailed", "copy_failed", True),
        ("SourceUnreadable", "path_unreadable", True),
        (None, "internal", True),
    ],
)
def test_adder_errors_map_to_nack_codes(pull, bindery, monkeypatch, exc_name, code, retryable):
    bindery.add("d1")
    exc_cls = getattr(pull.adder, exc_name) if exc_name else RuntimeError

    def fail(*args, **kwargs):
        raise exc_cls("boom")

    monkeypatch.setattr(pull.adder, "add_book_detailed", fail)
    Harness(pull, bindery.url).run()
    assert bindery.nacks == [("d1", {"code": code, "error": "boom", "retryable": retryable})]


def test_the_server_file_name_is_never_used(pull, bindery):
    bindery.add("d1", fileName="../../../outside.epub")
    bindery.file_headers = {"Content-Disposition": 'attachment; filename="../../evil.sh"'}
    h = Harness(pull, bindery.url)

    h.run()

    assert len(h.library.add_paths) == 1
    assert os.path.basename(h.library.add_paths[0]) == "book.epub"
    assert os.path.basename(os.path.dirname(h.library.add_paths[0])).startswith("bindery-pull-")


def test_a_cover_path_from_bindery_is_dropped(pull, bindery, tmp_path):
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"not for Bindery")
    bindery.add(
        "d1",
        metadata={
            "title": "The Dispossessed",
            "identifiers": {"bindery": "7"},
            "coverPath": str(secret),
        },
    )
    h = Harness(pull, bindery.url)
    h.run()
    assert bindery.acks[0][1]["coverApplied"] is None


def test_kepub_is_filed_as_kepub(pull, bindery):
    bindery.add("d1", fmt="kepub.epub")
    h = Harness(pull, bindery.url)
    h.run()
    assert os.path.basename(h.library.add_paths[0]) == "book.kepub"


def test_temp_dir_is_removed_when_the_add_raises(pull, bindery, monkeypatch, tempdirs):
    bindery.add("d1", cover=b"img")

    def fail(*args, **kwargs):
        raise RuntimeError("calibre fell over")

    monkeypatch.setattr(pull.adder, "add_book_detailed", fail)
    Harness(pull, bindery.url).run()
    assert len(tempdirs) == 1
    assert not os.path.exists(tempdirs[0])


def test_disabled_or_unconfigured_does_nothing(pull, bindery):
    bindery.add("d1")
    assert Harness(pull, bindery.url, pull_enabled=False).run().status == "Pull from Bindery is off"
    assert Harness(pull, "", pull_enabled=True).run().status == "Pull from Bindery is off"
    assert bindery.requests == []


# ── client rules ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://bindery", "bindery.lan", "http://", "https://u:p@bindery"],
)
def test_only_http_and_https_urls_are_accepted(pull, url):
    with pytest.raises(ValueError):
        pull.client.validate_base_url(url)


def test_plain_http_off_this_machine_is_warned_about(pull):
    assert pull.client.http_warning("http://127.0.0.1:8787") == ""
    assert pull.client.http_warning("http://localhost:8787/bindery") == ""
    assert pull.client.http_warning("https://bindery.lan") == ""
    assert "unencrypted" in pull.client.http_warning("http://192.168.1.20:8787")


def test_worker_thread_runs_and_stops(pull, bindery):
    bindery.add("d1")
    h = Harness(pull, bindery.url)
    ran = threading.Event()
    real = h.worker.run_once

    def run_once():
        stats = real()
        ran.set()
        return stats

    h.worker.run_once = run_once
    h.worker._wait = lambda seconds: None
    h.worker.start()
    try:
        assert ran.wait(5)
    finally:
        h.worker.stop()
    assert bindery.acks


# ── TLS ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def self_signed(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed, cannot make a throwaway certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    result = subprocess.run(  # noqa: S603
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        capture_output=True,
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        pytest.skip(f"openssl could not make a certificate: {result.stderr.decode()[:200]}")
    return str(cert), str(key)


@pytest.fixture
def tls_bindery(self_signed):
    cert, key = self_signed
    state = BinderyState()
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_fake_bindery(state))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.url = f"https://127.0.0.1:{port}"
    state.cert = cert
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_https_with_a_self_signed_cert_needs_the_ca_file(pull, tls_bindery):
    tls_bindery.add("d1")

    refused = Harness(pull, tls_bindery.url).run()
    assert refused.unreachable
    assert "CERTIFICATE_VERIFY_FAILED" in refused.error
    assert tls_bindery.pending() == ["d1"]

    h = Harness(pull, tls_bindery.url, ca_file=tls_bindery.cert)
    stats = h.run()
    assert stats.added == 1
    assert tls_bindery.acks[0][1]["outcome"] == "added"


# ── Bindery PR 2850 additions ────────────────────────────────────────────────


def test_a_forbidden_file_is_nacked_path_forbidden_non_retryable(pull, bindery, tempdirs):
    bindery.add("d1")
    bindery.override(
        "file", 403, {"error": "file is outside the library", "code": "path_forbidden"}
    )
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert [(i, b["code"], b["retryable"]) for i, b in bindery.nacks] == [
        ("d1", "path_forbidden", False)
    ]
    assert h.library.rows == {}
    assert not stats.unreachable
    assert not any(os.path.exists(d) for d in tempdirs)


def test_a_not_pending_answer_to_that_nack_is_harmless(pull, bindery):
    bindery.add("d1")
    bindery.override("file", 403, {"error": "not a regular file", "code": "path_forbidden"})
    bindery.override("nack", 409, {"error": "no longer pending", "code": "not_pending"})
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert (stats.nacked, stats.settled) == (1, 1)
    assert stats.error == ""
    assert h.worker.next_delay(stats) == 60


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (409, {"error": "delivered to another id", "code": "not_pending"}),
        (404, {"error": "no delivery with that id", "code": "not_found"}),
    ],
)
def test_an_ack_bindery_already_settled_is_done(pull, bindery, status, body):
    bindery.add("d1", bindery="1", title="One")
    bindery.add("d2", bindery="2", title="Two")
    bindery.override("ack", status, body)
    h = Harness(pull, bindery.url)

    stats = h.run()

    # Not a failure: no error, no backoff, and the pass carried on to d2.
    assert (stats.added, stats.settled, stats.ack_failed) == (2, 1, 0)
    assert stats.error == ""
    assert not stats.unreachable
    assert h.worker.next_delay(stats) == 60
    assert [a[0] for a in bindery.acks] == ["d2"]


def test_an_ack_that_fails_otherwise_is_still_a_failed_ack(pull, bindery):
    bindery.add("d1")
    bindery.override("ack", 409, {"error": "something else", "code": "conflict"})
    stats = Harness(pull, bindery.url).run()
    assert (stats.ack_failed, stats.settled) == (1, 0)
    assert stats.error


def test_a_page_may_hold_more_rows_than_the_limit(pull, bindery):
    """A book with more files than ``limit`` comes in one page, never split."""
    bindery.override(
        "hello",
        200,
        {"binderyVersion": "x", "protocol": 1, "maxBatch": 2, "transport": "pull"},
    )
    bindery.add("e", fmt="epub")
    bindery.add("p", fmt="pdf", action="add_format")
    bindery.add("m", fmt="mobi", action="add_format")
    bindery.add("a", fmt="azw3", action="add_format")
    bindery.add("other", bindery="8", title="Another book")
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert all(limit == 2 for limit, _cursor in bindery.list_calls)
    book = h.library.search('identifiers:"=bindery:=7"').pop()
    assert h.library.formats(book) == ("AZW3", "EPUB", "MOBI", "PDF")
    assert (stats.added, stats.format_added) == (2, 3)
    assert bindery.pending() == []


def test_a_two_format_book_lands_in_one_pass(pull, bindery):
    bindery.add("d1", fmt="epub")
    bindery.add("d2", fmt="pdf", action="add_format")
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert stats.relisted
    assert (stats.added, stats.format_added) == (1, 1)
    # The first listing withheld the PDF; one listing from the start found it.
    assert [cursor for _limit, cursor in bindery.list_calls] == ["", ""]
    assert bindery.pending() == []


def test_the_re_list_happens_at_most_once(pull, bindery):
    """A Bindery that grows a new book on every listing cannot loop a pass."""
    counter = iter(range(100, 200))

    def grow(state):
        n = next(counter)
        state.add(f"new{n}", bindery=str(n), title=f"Book {n}")

    bindery.on_list = grow
    h = Harness(pull, bindery.url)

    stats = h.run()

    assert stats.relisted
    assert [cursor for _limit, cursor in bindery.list_calls] == ["", ""]
    assert stats.added == 2


def test_no_re_list_without_an_acked_add(pull, bindery):
    bindery.add("d1", fmt="../evil")
    stats = Harness(pull, bindery.url).run()
    assert not stats.relisted
    assert len(bindery.list_calls) == 1


def test_the_limit_sent_is_at_most_twenty(pull, bindery):
    bindery.override(
        "hello",
        200,
        {"binderyVersion": "x", "protocol": 1, "maxBatch": 50, "transport": "pull"},
    )
    bindery.add("d1")
    Harness(pull, bindery.url).run()
    assert {limit for limit, _cursor in bindery.list_calls} == {20}


def test_a_long_nack_error_is_cut_to_bindery_s_cap(pull, bindery, monkeypatch):
    bindery.add("d1")

    def fail(*args, **kwargs):
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(pull.adder, "add_book_detailed", fail)
    Harness(pull, bindery.url).run()
    assert len(bindery.nacks[0][1]["error"]) == 2000


# ── the action: start, wake, stop ────────────────────────────────────────────


def _action_with_puller(cfg):
    from unittest.mock import MagicMock

    from .test_config import _load_init_module, _make_action, _make_action_stubs

    stubs = _make_action_stubs()
    mod, server_cls, _server, _cfg = _load_init_module(stubs)
    plugin_pkg = sys.modules["calibre_plugins.bindery_bridge.plugin"]
    puller_mod = types.ModuleType("calibre_plugins.bindery_bridge.plugin.puller")
    puller_mod.PullWorker = MagicMock(name="PullWorker")
    puller_mod.library_id = lambda db: db.new_api.library_id
    config_mod = types.ModuleType("calibre_plugins.bindery_bridge.plugin.config")
    saved = {}
    config_mod.save_value = saved.__setitem__
    for name, sub in (("puller", puller_mod), ("config", config_mod)):
        setattr(plugin_pkg, name, sub)
        sys.modules[f"calibre_plugins.bindery_bridge.plugin.{name}"] = sub
    action = _make_action(mod, server_cls, MagicMock(return_value=cfg))
    action._get_db = lambda: PullDB(PullLibrary("lib-now"))
    action._puller = None
    return stubs, action, puller_mod.PullWorker, saved


def _cleanup_puller(stubs):
    from .test_config import _cleanup_action

    for name in ("puller", "config"):
        sys.modules.pop(f"calibre_plugins.bindery_bridge.plugin.{name}", None)
    _cleanup_action(stubs)


def test_the_action_starts_pull_and_records_the_library():
    stubs, action, worker_cls, saved = _action_with_puller(
        {"pull_enabled": True, "bindery_url": "https://b", "pull_library_id": ""}
    )
    try:
        action._start_puller()
        worker_cls.return_value.start.assert_called_once_with()
        assert saved == {"pull_library_id": "lib-now"}

        action.library_changed(object())
        worker_cls.return_value.wake.assert_called_once_with()

        assert action.shutting_down() is True
        worker_cls.return_value.stop.assert_called_once_with()
        assert action._puller is None
    finally:
        _cleanup_puller(stubs)


def test_the_action_leaves_pull_off_by_default():
    stubs, action, worker_cls, saved = _action_with_puller({"pull_enabled": False})
    try:
        action._start_puller()
        worker_cls.assert_not_called()
        action.library_changed(object())
        assert saved == {}
    finally:
        _cleanup_puller(stubs)


def test_status_summary_reports_pull(pull):
    pull.status.set_pull(enabled=False, detail="", last_error="", warning="", delivered=0)
    assert pull.status.pull_summary() == "Pull from Bindery is off"
    pull.status.set_pull(enabled=True, detail="Connected to Bindery; nothing waiting")
    pull.status.add_pull_delivered(3)
    pull.status.set_pull(last_error="ack d1: HTTP 500")
    assert pull.status.pull_summary() == (
        "Connected to Bindery; nothing waiting. 3 files delivered since Calibre started. "
        "Last error: ack d1: HTTP 500."
    )


def test_status_summary_counts_files_not_books(pull):
    """A two format book is two deliveries, so the count is files."""
    pull.status.set_pull(
        enabled=True, detail="Connected to Bindery", last_error="", warning="", delivered=0
    )
    pull.status.add_pull_delivered(1)
    assert "1 file delivered since Calibre started" in pull.status.pull_summary()
