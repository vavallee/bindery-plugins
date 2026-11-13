"""Diagnostics a user can reach without a debugger.

Two parts. The debug log: nothing in the plugin configured logging, so INFO and
DEBUG lines went nowhere and a reporter asked to "turn on debug logging"
(bindery-plugins#30) had no switch. And the pull status line, which kept an
error on screen after pull had recovered, never showed how many deliveries
were waiting, and was read once when the settings window opened.

The debug log tests import the plugin under ``calibre_plugins.bindery_bridge``,
the name it has inside Calibre, because the handler is attached to that logger
namespace and to nothing else. A module loaded as plain ``adder`` logs outside
it, which is the point of the namespace and would make these tests vacuous.
"""

import importlib
import logging
import logging.handlers
import os
import stat
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from .test_config import (
    _cleanup,
    _cleanup_action,
    _load_config_module,
    _load_init_module,
    _make_action_stubs,
    _make_qt_stubs,
)
from .test_dedupe_ladder import FakeMetadata
from .test_gui_refresh import make_gui
from .test_pull_0_8_0 import (
    _PLUGIN_DIR,
    API_KEY,
    BinderyState,
    Harness,
    make_fake_bindery,
)

_PKG = "calibre_plugins.bindery_bridge.plugin"
_NAMESPACE = "calibre_plugins.bindery_bridge"


@pytest.fixture
def plugin_modules():
    """``load(name)`` imports a plugin module under its real dotted name."""
    saved = {n: m for n, m in sys.modules.items() if n.startswith("calibre_plugins")}
    for name in saved:
        del sys.modules[name]
    parent = None
    for name, path in (
        ("calibre_plugins", []),
        ("calibre_plugins.bindery_bridge", []),
        (_PKG, [str(_PLUGIN_DIR)]),
    ):
        module = types.ModuleType(name)
        module.__path__ = path
        sys.modules[name] = module
        if parent is not None:
            setattr(parent, name.rpartition(".")[2], module)
        parent = module

    def load(name):
        return importlib.import_module(f"{_PKG}.{name}")

    try:
        yield load
    finally:
        loaded = sys.modules.get(f"{_PKG}.debuglog")
        if loaded is not None:
            loaded.apply(False)
        for name in [n for n in sys.modules if n.startswith("calibre_plugins")]:
            del sys.modules[name]
        sys.modules.update(saved)


@pytest.fixture
def debuglog(plugin_modules):
    return plugin_modules("debuglog")


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "bindery_bridge.log"


def _read(path):
    for handler in logging.getLogger(_NAMESPACE).handlers:
        handler.flush()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _file_handlers():
    return [
        h
        for h in logging.getLogger(_NAMESPACE).handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]


# ── the debug log: on and off ────────────────────────────────────────────────


def test_the_handler_is_added_and_removed_with_the_setting(debuglog, log_file):
    namespace = logging.getLogger(_NAMESPACE)
    root = logging.getLogger()
    calibre_logger = logging.getLogger("calibre")
    before = (list(root.handlers), root.level, list(calibre_logger.handlers), calibre_logger.level)
    assert _file_handlers() == []

    assert debuglog.apply(True, path=str(log_file)) == ""

    (handler,) = _file_handlers()
    assert handler.level == logging.DEBUG
    assert (handler.maxBytes, handler.backupCount) == (1024 * 1024, 2)
    assert namespace.level == logging.DEBUG
    assert debuglog.is_active()
    # Only the plugin's namespace was touched.
    after = (list(root.handlers), root.level, list(calibre_logger.handlers), calibre_logger.level)
    assert after == before

    assert debuglog.apply(False) == ""

    assert namespace.handlers == []
    assert namespace.level == logging.NOTSET
    assert handler.stream is None, "turning the log off must close the file"
    assert not debuglog.is_active()


def test_turning_it_on_twice_keeps_one_handler(debuglog, log_file):
    debuglog.apply(True, path=str(log_file))
    debuglog.apply(True, path=str(log_file))
    assert len(_file_handlers()) == 1
    logging.getLogger(f"{_PKG}.adder").debug("one line")
    assert _read(log_file).count("one line") == 1


def test_the_log_gets_an_adder_debug_line_only_when_on(debuglog, plugin_modules, log_file):
    adder = plugin_modules("adder")
    gui = make_gui()
    gui.current_db.new_api.seed("Rocannon's World")

    adder.show_new_books(gui, 1)
    assert not log_file.exists(), "nothing is written while the setting is off"

    debuglog.apply(True, path=str(log_file))
    gui.current_db.new_api.seed("Planet of Exile")
    adder.show_new_books(gui, 1)

    (line,) = [ln for ln in _read(log_file).splitlines() if "Calibre GUI refresh" in ln]
    assert "1 added, 1 new rows in the view" in line
    assert " DEBUG " in line
    assert f"{_PKG}.adder" in line
    assert f"[{threading.current_thread().name}]" in line
    assert line[:4].isdigit(), "every line starts with a timestamp"

    debuglog.apply(False)
    size = log_file.stat().st_size
    gui.current_db.new_api.seed("City of Illusions")
    adder.show_new_books(gui, 1)
    assert log_file.stat().st_size == size, "nothing more is written once it is off"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no owner only mode bits")
def test_the_log_and_its_backups_are_owner_only(debuglog, log_file, monkeypatch):
    log_file.write_text("left by an earlier version\n")
    log_file.chmod(0o644)
    monkeypatch.setattr(debuglog, "MAX_BYTES", 400)

    debuglog.apply(True, path=str(log_file))
    for n in range(20):
        logging.getLogger(f"{_PKG}.puller").debug("filler line %d %s", n, "x" * 40)
    debuglog.apply(False)

    files = sorted(p.name for p in log_file.parent.iterdir())
    assert files == [log_file.name, f"{log_file.name}.1", f"{log_file.name}.2"]
    for name in files:
        mode = stat.S_IMODE((log_file.parent / name).stat().st_mode)
        assert mode == 0o600, f"{name} is {oct(mode)}"


def test_a_log_that_cannot_be_opened_does_not_raise(debuglog, tmp_path):
    missing = tmp_path / "no-such-folder" / "bindery_bridge.log"

    reason = debuglog.apply(True, path=str(missing))

    assert "no-such-folder" in reason
    assert debuglog.last_error() == reason
    assert not debuglog.is_active()
    assert logging.getLogger(_NAMESPACE).handlers == []
    # And the next successful attempt forgets the failure.
    assert debuglog.apply(False) == ""
    assert debuglog.last_error() == ""


def test_no_config_folder_is_a_reason_not_a_crash(debuglog):
    # The baseline stubs have no calibre.utils.config and no config_dir.
    assert debuglog.log_path() == ""
    assert "could not be found" in debuglog.apply(True)
    assert not debuglog.is_active()


def test_the_log_path_is_in_calibres_config_folder(debuglog, tmp_path, monkeypatch):
    config = types.ModuleType("calibre.utils.config")
    config.config_dir = str(tmp_path)
    monkeypatch.setitem(sys.modules, "calibre.utils.config", config)
    assert debuglog.log_path() == str(tmp_path / "bindery_bridge.log")


def test_warnings_still_reach_stderr_while_the_log_is_on(debuglog, log_file, monkeypatch):
    """Attaching any handler turns off Python's last resort stderr output.

    Without the mirror, turning the debug log on would take the plugin's
    warnings away from someone watching ``calibre-debug -g``.
    """
    seen = []

    class Recorder(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    monkeypatch.setattr(logging, "lastResort", Recorder(logging.WARNING))
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    debuglog.apply(True, path=str(log_file))
    logger = logging.getLogger(f"{_PKG}.adder")

    logger.warning("a refresh step failed")
    logger.debug("a detail")
    assert seen == ["a refresh step failed"]

    # Something else handles the record: the mirror stays out of the way.
    other = Recorder()
    monkeypatch.setattr(logging.getLogger(), "handlers", [other])
    seen.clear()
    logger.warning("handled elsewhere")
    assert seen == ["handled elsewhere"], "exactly once, by the root handler"


# ── the debug log: no credentials ────────────────────────────────────────────


def test_redact_leaves_ordinary_words_alone(debuglog):
    redact = debuglog.redact
    line = "auth failure: missing Bearer token from 10.0.0.5"
    assert redact(line, ["0123456789abcdef"]) == line
    assert redact("Authorization: Bearer abc", []) == "Authorization: Bearer [redacted]"
    assert redact("sent 'X-Api-Key': 'abc'", []) == "sent 'X-Api-Key': '[redacted]'"
    assert redact("GET /x?api_key=abc&limit=2", []) == "GET /x?api_key=[redacted]&limit=2"
    assert redact("header Bearer 0123456789abcdef seen", []) == "header Bearer [redacted] seen"
    assert redact("key is hunter22 here", ["hunter22"]) == "key is [redacted] here"
    # A key too short to be a secret is not blanked out of every word.
    assert redact("a book was added", ["a"]) == "a book was added"


def test_a_pull_delivery_never_writes_the_key(debuglog, plugin_modules, log_file, serve_bridge):
    adder = plugin_modules("adder")
    adder.get_metadata = lambda stream, fmt: FakeMetadata()
    ns = types.SimpleNamespace(
        adder=adder,
        client=plugin_modules("bindery_client"),
        status=plugin_modules("status"),
        handlers=plugin_modules("handlers"),
        puller=plugin_modules("puller"),
    )
    state = BinderyState()
    url = f"http://127.0.0.1:{serve_bridge(make_fake_bindery(state)).port}"
    state.add("d1", fmt="epub")
    debuglog.apply(True, secrets=[API_KEY], path=str(log_file))
    harness = Harness(ns, url)

    assert harness.run().added == 1
    # A server that echoes the request back in its error, key included.
    state.override("hello", 500, {"error": f"upstream said: Authorization: Bearer {API_KEY}"})
    assert harness.run().unreachable
    logging.getLogger(f"{_PKG}.puller").debug(
        "headers were %r", harness.client(harness.cfg)._headers
    )

    text = _read(log_file)
    assert "pull: delivery d1 (add epub) acknowledged as added" in text
    assert "pull hello failed" in text
    assert "headers were" in text
    assert "[redacted]" in text
    assert API_KEY not in text
    # The request really carried the key, so its absence above means something.
    assert all(h["Authorization"] == f"Bearer {API_KEY}" for _m, _p, h in state.requests)


def test_a_push_request_never_writes_the_key(debuglog, plugin_modules, log_file, serve_bridge):
    plugin_modules("adder")
    handlers = plugin_modules("handlers")
    key = "push-secret-key-0001"
    debuglog.apply(True, secrets=[key], path=str(log_file))
    bridge = serve_bridge(handlers.make_handler(api_key=key, get_db=lambda: object()))

    assert bridge.call("GET", "/v1/paths?path=/books/x.epub", token=key)[0] == 200
    assert bridge.call("GET", "/v1/paths?path=/books/x.epub", token="a-wrong-token-9999")[0] == 401

    text = _read(log_file)
    assert "GET /v1/paths?path=/books/x.epub" in text
    assert "auth failure: invalid token" in text
    assert key not in text
    assert "a-wrong-token-9999" not in text


# ── GUI refresh failures are warnings ────────────────────────────────────────


def test_a_failed_refresh_step_is_a_warning_that_names_the_step(bridge_adder, caplog):
    """These were debug lines, so a user with a list that never updated saw
    nothing at all, with or without ``calibre-debug -g``."""
    gui = make_gui()
    book_id = gui.current_db.new_api.seed("The Word for World Is Forest")
    model = gui.library_view.model()
    model.refresh_ids = MagicMock(side_effect=RuntimeError("no such row"))
    gui.current_db.data.books_added([book_id])

    with caplog.at_level(logging.DEBUG, logger="adder"):
        bridge_adder.show_new_books(gui, 1, book_id)
        bridge_adder.show_new_books(types.SimpleNamespace(), 1)

    by_level = {}
    for record in caplog.records:
        by_level.setdefault(record.levelname, []).append(record.getMessage())
    repaint, no_view = by_level["WARNING"]
    assert repaint == (
        f"Calibre GUI refresh: repainting book {book_id} failed, doing a full refresh: no such row"
    )
    assert no_view.startswith("Calibre GUI refresh: no library view to refresh: ")
    # The normal summary stays out of the way.
    assert by_level["DEBUG"] == ["Calibre GUI refresh: 1 added, 0 new rows in the view"]


def test_every_refresh_fallback_logs_a_warning(bridge_adder, caplog):
    gui = make_gui(add_books=False)
    gui.current_db.new_api.seed("Malafrena")
    model = gui.library_view.model()
    model.books_added = MagicMock(side_effect=RuntimeError("announce"))
    model.refresh = MagicMock(side_effect=RuntimeError("full"))

    with caplog.at_level(logging.WARNING, logger="adder"):
        bridge_adder.show_new_books(gui, 1)
        del gui.current_db.data._map
        bridge_adder.redraw_book(gui, 1)
        bridge_adder._schedule_gui_refresh(None, 1, MagicMock(side_effect=RuntimeError("thread")))

    steps = [r.getMessage().split(": ", 2)[1] for r in caplog.records]
    assert all(r.levelno == logging.WARNING for r in caplog.records)
    assert all(r.getMessage().startswith("Calibre GUI refresh: ") for r in caplog.records)
    assert steps == [
        "Add Books action unavailable, announcing rows directly",
        "announcing the new rows failed, doing a full refresh",
        "the full refresh failed, press Ctrl+R",
        "registering rows with the view failed, doing a full refresh",
        "the full refresh failed, press Ctrl+R",
        "handing the refresh to the GUI thread failed",
    ]


# ── the pull status line ─────────────────────────────────────────────────────


@pytest.fixture
def pull_ns(plugin_modules):
    adder = plugin_modules("adder")
    adder.get_metadata = lambda stream, fmt: FakeMetadata()
    return types.SimpleNamespace(
        adder=adder,
        client=plugin_modules("bindery_client"),
        status=plugin_modules("status"),
        handlers=plugin_modules("handlers"),
        puller=plugin_modules("puller"),
    )


@pytest.fixture
def fake_bindery(serve_bridge):
    state = BinderyState()
    state.url = f"http://127.0.0.1:{serve_bridge(make_fake_bindery(state)).port}"
    return state


def _hhmm(when):
    return time.strftime("%H:%M", time.localtime(when))


def test_a_recovered_pull_drops_the_stale_error(pull_ns, fake_bindery):
    """It used to stay until Calibre restarted: "Connected to Bindery; nothing
    waiting. Last error: hello: cannot reach Bindery"."""
    harness = Harness(pull_ns, fake_bindery.url)
    fake_bindery.override("hello", 503, {"error": "starting up"})
    harness.run()
    assert "Last error" in pull_ns.status.pull_summary()

    harness.run()

    summary = pull_ns.status.pull_summary()
    assert summary.startswith("Connected to Bindery; nothing waiting.")
    assert "Last error" not in summary


def test_the_last_error_carries_its_time_and_clears_after_a_good_pass(pull_ns, fake_bindery):
    harness = Harness(pull_ns, fake_bindery.url)
    clock = iter([1_700_000_000.0, 1_700_000_300.0])
    harness.worker._clock = lambda: next(clock)

    fake_bindery.override("hello", 503, {"error": "starting up"})
    assert harness.run().unreachable
    failed = pull_ns.status.pull_summary(now=1_700_000_010.0)
    assert failed == (
        "Cannot reach Bindery; retrying with backoff. "
        f"Last error ({_hhmm(1_700_000_000.0)}): hello: HTTP 503: starting up."
    )

    stats = harness.run()
    assert stats.completed and not stats.error
    assert pull_ns.status.pull_summary(now=1_700_000_310.0) == (
        f"Connected to Bindery; nothing waiting. Last successful check {_hhmm(1_700_000_300.0)}."
    )
    assert pull_ns.status.pull_current()["last_error"] == ""


def test_an_error_inside_a_pass_clears_on_the_next_clean_one(pull_ns, fake_bindery):
    fake_bindery.add("d1")
    fake_bindery.override("file", 500, {"error": "disk"})
    harness = Harness(pull_ns, fake_bindery.url)

    first = harness.run()
    assert first.error == "download d1: HTTP 500: disk"
    assert not first.completed
    assert "Last error (" in pull_ns.status.pull_summary()
    assert "Last successful check" not in pull_ns.status.pull_summary()

    assert harness.run().added == 1
    assert "Last error" not in pull_ns.status.pull_summary()


def test_the_last_error_stays_while_bindery_has_not_answered_again(pull_ns, fake_bindery):
    harness = Harness(pull_ns, fake_bindery.url)
    fake_bindery.override("hello", 503, {"error": "starting up"})
    harness.run()

    # No library open: the pass never reaches Bindery, so it proves nothing.
    harness.holder["db"] = None
    stats = harness.run()
    assert not stats.completed
    summary = pull_ns.status.pull_summary()
    assert summary.startswith("Waiting for a Calibre library to open. Last error (")
    assert "hello: HTTP 503: starting up" in summary


def test_the_pull_status_shows_how_many_are_waiting(pull_ns, fake_bindery):
    fake_bindery.add("d1", bindery="7")
    fake_bindery.add("d2", bindery="8", title="The Lathe of Heaven")
    fake_bindery.override("file", 500, {"error": "disk"})
    harness = Harness(pull_ns, fake_bindery.url)

    stats = harness.run()
    assert stats.status == "Connected to Bindery; 1 of 2 delivered in the last pass, 1 waiting"
    # Two were due, one arrived, and the listing after that ack agrees.
    assert (stats.listed, stats.pending, stats.waiting) == (2, 1, 1)
    assert pull_ns.status.pull_summary().startswith(
        "Connected to Bindery; 1 of 2 delivered in the last pass, 1 waiting. "
    )

    stats = harness.run()
    assert stats.waiting == 0
    assert stats.status == "Connected to Bindery; 1 of 1 delivered in the last pass"

    assert harness.run().status == "Connected to Bindery; nothing waiting"


def test_waiting_is_shown_when_nothing_was_listed(pull_ns, fake_bindery):
    fake_bindery.override("deliveries", 200, {"deliveries": [], "nextCursor": "", "pending": 3})
    stats = Harness(pull_ns, fake_bindery.url).run()
    assert stats.status == "Connected to Bindery; 3 waiting"


def test_a_refused_delivery_is_not_counted_as_waiting(pull_ns, fake_bindery):
    """Nothing is listed again after a nack, so this is the count Bindery gave
    at the listing less the one delivery the pass settled."""
    fake_bindery.add("d1", fmt="../evil")
    stats = Harness(pull_ns, fake_bindery.url).run()
    assert (stats.nacked, stats.pending, stats.waiting) == (1, 1, 0)
    assert stats.status == "Connected to Bindery; 0 of 1 delivered in the last pass"


def test_the_pull_state_has_no_unused_fields(plugin_modules):
    status = plugin_modules("status")
    assert sorted(status.pull_current()) == [
        "delivered",
        "detail",
        "enabled",
        "last_error",
        "last_error_at",
        "last_success",
        "warning",
    ]


def test_times_from_another_day_carry_the_date(plugin_modules):
    status = plugin_modules("status")
    now = time.mktime((2026, 11, 12, 9, 30, 0, 0, 0, -1))
    yesterday = time.mktime((2026, 11, 11, 23, 5, 0, 0, 0, -1))
    status.set_pull(
        enabled=True,
        detail="Cannot reach Bindery",
        last_error="hello: timed out",
        last_error_at=now - 60,
        last_success=yesterday,
    )
    assert status.pull_summary(now=now) == (
        "Cannot reach Bindery. Last error (09:29): hello: timed out. "
        "Last successful check 2026-11-11 23:05."
    )


def test_a_long_error_is_cut_at_about_300_characters(plugin_modules):
    status = plugin_modules("status")
    status.set_pull(enabled=True, detail="Cannot reach Bindery", last_error="x" * 5000)
    summary = status.pull_summary()
    assert len(summary) < 400
    assert summary.endswith("xxx…"), "the cut is marked"
    assert status.clip("a\nb   c") == "a b c"
    assert len(status.clip("y" * 301)) == 300

    status.set_stopped("z" * 5000)
    assert len(status.summary()) < 400


# ── the settings window ──────────────────────────────────────────────────────


class FakeLabel:
    def __init__(self, *args):
        self._text = args[0] if args and isinstance(args[0], str) else ""
        self.format = None
        self.format_when_text_was_set = "never set"
        self.flags = None
        self.word_wrap = False

    def setText(self, text):
        self.format_when_text_was_set = self.format
        self._text = text

    def text(self):
        return self._text

    def setTextFormat(self, fmt):
        self.format = fmt

    def setWordWrap(self, on):
        self.word_wrap = on

    def setTextInteractionFlags(self, flags):
        self.flags = flags


class FakeTimer:
    def __init__(self, parent=None):
        self.interval = None
        self.running = False
        self.slots = []
        self.timeout = types.SimpleNamespace(connect=self.slots.append)

    def setInterval(self, ms):
        self.interval = ms

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def fire(self):
        for slot in self.slots:
            slot()


@pytest.fixture
def settings(plugin_modules, tmp_path):
    """config.py with recording labels and a timer, next to the real status
    and debug log modules."""
    stubs = _make_qt_stubs()
    stubs["qt.core"].QLabel = FakeLabel
    stubs["qt.core"].QTimer = FakeTimer
    stubs["calibre.utils.config"].config_dir = str(tmp_path)
    config = _load_config_module(stubs)
    try:
        yield types.SimpleNamespace(
            config=config,
            stubs=stubs,
            prefs=stubs["_mock_prefs_storage"],
            status=plugin_modules("status"),
            debuglog=plugin_modules("debuglog"),
            log_file=tmp_path / "bindery_bridge.log",
        )
    finally:
        _cleanup(stubs)


MARKUP = 'bad host <b>bold</b> <img src="http://evil.example/x.png"> end'


def test_server_text_in_a_status_line_is_shown_literally(settings):
    settings.status.set_degraded(MARKUP)
    settings.status.set_pull(enabled=True, detail="Cannot reach Bindery", last_error=MARKUP)

    widget = settings.config.ConfigWidget()

    plain = settings.config.Qt.TextFormat.PlainText
    for label in (widget.status_label, widget.pull_status_label, widget.debug_log_path_label):
        assert label.format is plain
        # Set before the first text, so Qt never guesses rich text from it.
        assert label.format_when_text_was_set is plain
        assert label.word_wrap is True
    assert widget.status_label.text() == f"Not listening for books: {MARKUP}"
    assert widget.pull_status_label.text() == f"Cannot reach Bindery. Last error: {MARKUP}."


def test_the_status_lines_refresh_while_the_window_is_open(settings):
    settings.status.set_pull(enabled=True, detail="Cannot reach Bindery; retrying with backoff")
    widget = settings.config.ConfigWidget()
    timer = widget._refresh_timer
    assert timer.interval == 3000
    assert not timer.running, "nothing ticks before the window is shown"
    assert widget.pull_status_label.text() == "Cannot reach Bindery; retrying with backoff."

    widget.showEvent(None)
    assert timer.running

    settings.status.set_running("0.0.0.0:8099")
    settings.status.set_pull(detail="Connected to Bindery; nothing waiting")
    timer.fire()
    assert widget.status_label.text() == "Listening on 0.0.0.0:8099"
    assert widget.pull_status_label.text() == "Connected to Bindery; nothing waiting."

    widget.hideEvent(None)
    assert not timer.running, "a closed window must not keep polling"


def test_showing_the_window_reads_the_status_again(settings):
    widget = settings.config.ConfigWidget()
    settings.status.set_running("127.0.0.1:8099")
    widget.showEvent(None)
    assert widget.status_label.text() == "Listening on 127.0.0.1:8099"


def test_the_debug_log_is_off_by_default_and_follows_the_checkbox(settings):
    config = settings.config
    assert config.load_config()["debug_log"] is False
    widget = config.ConfigWidget()
    widget.debug_log_input.setChecked.assert_called_with(False)
    assert widget.debug_log_path_label.text() == str(settings.log_file)
    assert widget.debug_log_path_label.flags is (
        config.Qt.TextInteractionFlag.TextSelectableByMouse
    )

    def commit(checked):
        form = MagicMock()
        form.port_input.value.return_value = 8099
        form.api_key_input.text.return_value = "settings-key-123"
        form.debug_log_input.isChecked.return_value = checked
        config.ConfigWidget.commit(form)

    commit(True)
    assert settings.prefs["debug_log"] is True
    assert settings.debuglog.is_active()
    logging.getLogger(f"{_PKG}.server").info("key settings-key-123 in a message")
    text = _read(settings.log_file)
    assert "in a message" in text
    assert "settings-key-123" not in text, "the saved key is the one that is redacted"

    commit(False)
    assert settings.prefs["debug_log"] is False
    assert not settings.debuglog.is_active()


def test_a_log_that_cannot_be_opened_shows_in_the_status_line(settings):
    config = settings.config
    settings.stubs["calibre.utils.config"].config_dir = str(settings.log_file / "not-a-folder")
    settings.prefs.update({"debug_log": True, "api_key": "k"})
    settings.status.set_running("0.0.0.0:8099")

    reason = config.apply_debug_log()

    assert reason
    assert config._status_summary() == (
        f"Listening on 0.0.0.0:8099. The debug log could not be opened: {reason}"
    )
    # Fixed, and the note goes away.
    settings.stubs["calibre.utils.config"].config_dir = str(settings.log_file.parent)
    assert config.apply_debug_log() == ""
    assert config._status_summary() == "Listening on 0.0.0.0:8099"


def test_saving_never_fails_because_of_the_debug_log(settings, monkeypatch):
    config = settings.config
    monkeypatch.setattr(
        settings.debuglog, "apply", MagicMock(side_effect=RuntimeError("logging broke"))
    )
    form = MagicMock()
    form.port_input.value.return_value = 8099

    config.ConfigWidget.commit(form)

    assert config.apply_debug_log() == "logging broke"
    assert settings.prefs["port"] == 8099


def test_the_open_folder_button_needs_calibres_helper(settings, monkeypatch):
    config = settings.config
    button = config.QPushButton.return_value

    widget = config.ConfigWidget()
    assert widget._open_local_file is None
    assert "Open log folder" not in [c.args[0] for c in config.QPushButton.call_args_list]

    opened = []
    gui2 = types.ModuleType("calibre.gui2")
    gui2.open_local_file = opened.append
    monkeypatch.setitem(sys.modules, "calibre.gui2", gui2)
    widget = config.ConfigWidget()
    assert "Open log folder" in [c.args[0] for c in config.QPushButton.call_args_list]
    button.clicked.connect.assert_called_with(widget._open_log_folder)
    widget._open_log_folder()
    assert opened == [str(settings.log_file.parent)]

    # A file manager that will not start is ignored.
    widget._open_local_file = MagicMock(side_effect=OSError("no file manager"))
    widget._open_log_folder()


# ── startup ──────────────────────────────────────────────────────────────────


def _genesis_with(apply_debug_log):
    stubs = _make_action_stubs()
    mod, server_cls, server_inst, cfg = _load_init_module(stubs)
    pkg = sys.modules[_PKG]
    order = []
    server_inst.start.side_effect = lambda **kwargs: order.append("server")
    subs = {
        "config": {
            "load_config": cfg,
            "apply_debug_log": lambda: order.append("debug log") or apply_debug_log(),
        },
        "server": {"BridgeServer": server_cls},
        "status": {},
    }
    for name, attrs in subs.items():
        sub = types.ModuleType(f"{_PKG}.{name}")
        for attr, value in attrs.items():
            setattr(sub, attr, value)
        setattr(pkg, name, sub)
        sys.modules[f"{_PKG}.{name}"] = sub
    action = mod.BinderyBridgeAction.__new__(mod.BinderyBridgeAction)
    action.gui = MagicMock()
    action.qaction = MagicMock()
    try:
        action.genesis()
    finally:
        for name in subs:
            sys.modules.pop(f"{_PKG}.{name}", None)
        _cleanup_action(stubs)
    return action, server_inst, order


def test_genesis_opens_the_debug_log_before_anything_else():
    _action, server_inst, order = _genesis_with(lambda: "")
    assert order == ["debug log", "server"]


def test_genesis_survives_a_debug_log_that_raises(caplog):
    def boom():
        raise RuntimeError("logging broke")

    with caplog.at_level(logging.WARNING):
        action, server_inst, _order = _genesis_with(boom)

    server_inst.start.assert_called_once()
    assert action._server is server_inst
    assert "calibre-bridge debug log could not be opened: logging broke" in caplog.text


def test_genesis_reports_a_debug_log_that_could_not_be_opened(caplog):
    with caplog.at_level(logging.WARNING):
        _action, server_inst, _order = _genesis_with(lambda: "Permission denied")
    server_inst.start.assert_called_once()
    assert "calibre-bridge debug log could not be opened: Permission denied" in caplog.text
