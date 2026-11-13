"""Bindery#30: books added by the bridge never reached Calibre's own book list.

The plugin adds through ``db.new_api.add_books`` (the ``Cache``). The library
list is drawn from ``db.data`` (the legacy ``View``), whose id index only the
legacy ``LibraryDatabase.add_books`` updates (``db/legacy.py`` calls
``self.data.books_added(ids)``). 0.6.3 told the *model* ``books_added(count)``
and never told the *view*, so ``rowCount`` (``len(db.data)``) did not change,
no row appeared, and a search, which only keeps ids that are in the view's
``_map``, found nothing until Ctrl+R rebuilt the index.

The fakes below copy exactly those relationships out of Calibre 9.18, so a
refresh that only calls ``model.books_added`` leaves the book invisible here as
it does in Calibre.
"""

import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

from .test_bridge_0_6_3 import _FakeDispatcher
from .test_config import _cleanup_action, _load_init_module, _make_action_stubs
from .test_dedupe_ladder import FakeMetadata


class FakeCache:
    """``db.new_api``: knows every book, knows nothing about any view."""

    def __init__(self):
        self.titles: dict[int, str] = {}
        self._next = 1

    def all_book_ids(self):
        return frozenset(self.titles)

    def add_books(self, books, add_duplicates=True, run_hooks=True):
        ids = []
        for mi, _formats in books:
            self.titles[self._next] = mi.title
            ids.append(self._next)
            self._next += 1
        return ids, []

    def seed(self, title):
        return self.add_books([(types.SimpleNamespace(title=title), {})])[0][0]


class FakeView:
    """``db.data``: ``View``. Rows and search results come from its id index."""

    def __init__(self, cache):
        self.cache = cache
        self._map = tuple(sorted(cache.titles))
        self._map_filtered = self._map

    def __len__(self):
        return len(self._map_filtered)

    def books_added(self, ids):
        ids = tuple(ids)
        self._map = ids + self._map
        self._map_filtered = ids + self._map_filtered

    def search(self, query):
        matches = {i for i, t in self.cache.titles.items() if query in t}
        self._map_filtered = tuple(i for i in self._map if i in matches)
        return list(self._map_filtered)

    def refresh(self, field=None):
        self._map = tuple(sorted(self.cache.titles))
        self._map_filtered = self._map


class FakeLegacyDB:
    def __init__(self, cache=None):
        self.new_api = cache or FakeCache()
        self.data = FakeView(self.new_api)

    def refresh(self, field=None):
        self.data.refresh(field=field)


class FakeModel:
    """``BooksModel``. ``books_added(n)`` only signals, as in Calibre."""

    def __init__(self, db):
        self.db = db
        self.announced: list[int] = []
        self.refreshed_ids: list[list[int]] = []
        self.full_refreshes = 0
        self.consistent = True
        self._rows_seen = self.rowCount()

    def rowCount(self):
        return len(self.db.data)

    def books_added(self, num):
        # Qt's beginInsertRows/endInsertRows promise ``num`` new rows. If
        # rowCount() did not grow by that much the view is out of step.
        self.consistent = self.consistent and self.rowCount() - self._rows_seen == num
        self._rows_seen = self.rowCount()
        self.announced.append(num)

    def refresh_ids(self, ids, current_row=-1):
        self.refreshed_ids.append(list(ids))

    def refresh(self, reset=True):
        self.db.refresh(field=None)
        self._rows_seen = self.rowCount()
        self.full_refreshes += 1


class FakeLibraryView:
    def __init__(self, model):
        self._model = model

    def model(self):
        return self._model


class FakeAddBooks:
    """The Add Books action: ``refresh_gui`` is what Calibre runs after an add."""

    def __init__(self, gui):
        self.gui = gui
        self.calls: list[int] = []

    def refresh_gui(self, num, set_current_row=-1, recount=True):
        self.calls.append(num)
        self.gui.library_view.model().books_added(num)
        self.gui.tags_view.recount()


def make_gui(db=None, add_books=True):
    """A main window with a built library view."""
    db = db or FakeLegacyDB()
    gui = types.SimpleNamespace(current_db=db)
    attach_view(gui, db, add_books=add_books)
    return gui


def attach_view(gui, db, add_books=True):
    gui.library_view = FakeLibraryView(FakeModel(db))
    gui.tags_view = MagicMock()
    gui.iactions = {"Add Books": FakeAddBooks(gui)} if add_books else {}
    return gui.library_view.model()


def visible(gui):
    db = gui.library_view.model().db
    return [db.new_api.titles[i] for i in db.data._map_filtered]


def find(gui, query):
    db = gui.library_view.model().db
    return [db.new_api.titles[i] for i in db.data.search(query)]


@pytest.fixture
def adder(bridge_adder):
    bridge_adder.get_metadata = lambda stream, fmt: FakeMetadata()
    return bridge_adder


@pytest.fixture
def plugin():
    """The action module with ``adder`` reachable the way Calibre reaches it."""
    stubs = _make_action_stubs()
    mod, server_cls, server_inst, cfg = _load_init_module(stubs)
    try:
        yield types.SimpleNamespace(
            mod=mod, stubs=stubs, server_cls=server_cls, server_inst=server_inst, cfg=cfg
        )
    finally:
        for name in ("config", "server", "status"):
            sys.modules.pop(f"calibre_plugins.bindery_bridge.plugin.{name}", None)
        sys.modules.pop("calibre_plugins.bindery_bridge.plugin.adder", None)
        _cleanup_action(stubs)


def _wire_adder(adder):
    pkg = sys.modules["calibre_plugins.bindery_bridge.plugin"]
    pkg.adder = adder
    sys.modules["calibre_plugins.bindery_bridge.plugin.adder"] = adder


def _genesis(plugin, adder, gui):
    """Run ``genesis`` against ``gui`` with a Dispatcher, as Calibre does."""
    _wire_adder(adder)
    plugin.stubs["calibre.gui2"].Dispatcher = _FakeDispatcher
    sys.modules["calibre.gui2"] = plugin.stubs["calibre.gui2"]
    pkg = sys.modules["calibre_plugins.bindery_bridge.plugin"]
    for name, attr, value in (
        ("config", "load_config", plugin.cfg),
        ("server", "BridgeServer", plugin.server_cls),
        ("status", None, None),
    ):
        sub = types.ModuleType(f"calibre_plugins.bindery_bridge.plugin.{name}")
        if attr:
            setattr(sub, attr, value)
        setattr(pkg, name, sub)
        sys.modules[f"calibre_plugins.bindery_bridge.plugin.{name}"] = sub
    action = plugin.mod.BinderyBridgeAction.__new__(plugin.mod.BinderyBridgeAction)
    action.gui = gui
    action.qaction = MagicMock()
    action.genesis()
    return action


def _push(adder, db, tmp_path, on_added, on_updated=None, name="book.epub"):
    path = tmp_path / name
    path.write_bytes(b"stub epub bytes")
    return adder.add_book_detailed(db, str(path), on_added=on_added, on_updated=on_updated)


# ── the cause ────────────────────────────────────────────────────────────────


def test_announcing_rows_without_registering_them_shows_nothing(adder):
    """The 0.6.3 refresh, reduced to what it did: signal, never register.

    Pins the Calibre behaviour this whole fix rests on, in the fakes: a book
    the Cache added is not in the view, not in the list and not searchable, and
    ``model.books_added(1)`` does not change that.
    """
    gui = make_gui()
    db = gui.library_view.model().db
    db.new_api.add_books([(types.SimpleNamespace(title="Zebra Notes"), {})])

    gui.library_view.model().books_added(1)

    assert visible(gui) == []
    assert find(gui, "Zebra") == []
    assert gui.library_view.model().consistent is False


def test_a_pushed_book_shows_up_in_the_list_and_in_search(adder, plugin, tmp_path):
    gui = make_gui()
    action = _genesis(plugin, adder, gui)
    db = gui.current_db

    result = _push(adder, db, tmp_path, action._on_added)

    assert result.duplicate is False
    assert visible(gui) == ["Stub Title"]
    assert find(gui, "Stub") == ["Stub Title"]
    model = gui.library_view.model()
    assert model.announced == [1]
    assert model.consistent
    # The call Calibre itself makes after an add, not a private substitute.
    assert gui.iactions["Add Books"].calls == [1]
    gui.tags_view.recount.assert_called_once_with()


def test_a_new_book_lands_above_the_existing_ones(adder, plugin, tmp_path):
    db = FakeLegacyDB()
    db.new_api.seed("Old One")
    db.data = FakeView(db.new_api)
    gui = make_gui(db)
    action = _genesis(plugin, adder, gui)

    _push(adder, db, tmp_path, action._on_added)

    assert visible(gui) == ["Stub Title", "Old One"]
    assert gui.library_view.model().consistent


# ── genesis before the window exists ─────────────────────────────────────────


def test_genesis_before_the_library_view_exists(adder, plugin, tmp_path):
    """Calibre runs every ``genesis`` before it builds the library view.

    ``Main.initialize`` calls ``do_genesis`` on each action, then
    ``init_library_view_mixin``. The Dispatcher is made in the first step and
    the view appears in the second, so nothing about the view may be captured
    when the plugin starts.
    """
    gui = types.SimpleNamespace()  # no library_view, tags_view, iactions yet
    action = _genesis(plugin, adder, gui)
    assert not hasattr(gui, "library_view")

    db = FakeLegacyDB()
    gui.current_db = db
    attach_view(gui, db)
    _push(adder, db, tmp_path, action._on_added)

    assert visible(gui) == ["Stub Title"]
    assert find(gui, "Stub") == ["Stub Title"]
    assert gui.library_view.model().consistent


def test_a_book_added_before_the_view_exists_shows_on_the_next_refresh(adder, plugin, tmp_path):
    """A delivery that lands during startup is not lost.

    The refresh finds no view and gives up quietly; the book is in the
    database, so the next refresh registers it together with the next book.
    """
    gui = types.SimpleNamespace()
    action = _genesis(plugin, adder, gui)
    db = FakeLegacyDB()
    gui.current_db = db

    _push(adder, db, tmp_path, action._on_added, name="early.epub")  # no view: must not raise
    attach_view(gui, db)
    assert visible(gui) == []

    _push(adder, db, tmp_path, action._on_added, name="late.epub")

    assert visible(gui) == ["Stub Title", "Stub Title"]
    assert gui.library_view.model().announced == [2]
    assert gui.library_view.model().consistent


def test_a_library_view_that_was_replaced_after_genesis_gets_the_refresh(adder, plugin, tmp_path):
    db = FakeLegacyDB()
    gui = make_gui(db)
    action = _genesis(plugin, adder, gui)
    old_model = gui.library_view.model()
    # Calibre rebuilds the view (a layout change, a library switch).
    new_model = attach_view(gui, db)

    _push(adder, db, tmp_path, action._on_added)

    assert visible(gui) == ["Stub Title"]
    assert new_model.announced == [1]
    assert old_model.announced == []


def test_the_refresh_follows_the_library_that_is_open_now(adder, plugin, tmp_path):
    """The book went into library A, then the user opened library B.

    Registration compares each library with its own database, so B's list is
    left alone and no id from A is inserted into it.
    """
    lib_a, lib_b = FakeLegacyDB(), FakeLegacyDB()
    lib_b.new_api.seed("Only In B")
    lib_b.data = FakeView(lib_b.new_api)
    gui = make_gui(lib_a)
    action = _genesis(plugin, adder, gui)

    _push(adder, lib_a, tmp_path, lambda count: None)  # the add ran against A
    gui.current_db = lib_b
    attach_view(gui, lib_b)
    action._refresh_gui(1)  # the queued refresh runs after the switch

    assert visible(gui) == ["Only In B"]
    assert gui.library_view.model().announced == []


# ── idempotence ──────────────────────────────────────────────────────────────


def test_two_refreshes_for_two_adds_show_each_book_once(adder, plugin, tmp_path):
    gui = make_gui()
    action = _genesis(plugin, adder, gui)
    db = gui.current_db
    # Two adds finish before the GUI thread runs either queued refresh.
    _push(adder, db, tmp_path, lambda count: None, name="a.epub")
    _push(adder, db, tmp_path, lambda count: None, name="b.epub")

    action._refresh_gui(1)
    action._refresh_gui(1)

    assert visible(gui) == ["Stub Title", "Stub Title"]
    assert gui.library_view.model().announced == [2]
    assert gui.library_view.model().consistent


def test_a_row_another_refresh_registered_is_still_repainted(adder, plugin, tmp_path):
    """Cache.add_books is not atomic: delivery A's refresh can register B's row.

    B's id is then in the view when B's own refresh runs, so the comparison
    finds nothing missing. The row was registered while B was still adding its
    format, so it has to be repainted or its Formats column stays stale.
    """
    gui = make_gui()
    action = _genesis(plugin, adder, gui)
    db = gui.current_db
    calls: list[tuple[int, ...]] = []
    _push(adder, db, tmp_path, lambda *args: calls.append(args), name="a.epub")
    _push(adder, db, tmp_path, lambda *args: calls.append(args), name="b.epub")
    id_a, id_b = sorted(db.new_api.all_book_ids())
    assert calls == [(1, id_a), (1, id_b)]
    model = gui.library_view.model()

    action._refresh_gui(*calls[0])  # A's refresh registers both ids
    assert model.announced == [2]
    assert model.refreshed_ids == []

    action._refresh_gui(*calls[1])  # B's finds nothing missing

    assert model.announced == [2]
    assert model.refreshed_ids == [[id_b]]
    assert visible(gui) == ["Stub Title", "Stub Title"]
    assert model.consistent


def test_the_dispatcher_carries_the_book_id_to_the_refresh(adder, plugin, tmp_path):
    gui = make_gui()
    action = _genesis(plugin, adder, gui)
    db = gui.current_db

    result = _push(adder, db, tmp_path, action._on_added)

    assert result.book_id == 1
    assert gui.library_view.model().announced == [1]
    # The row was new, so registering it is the repaint: no second one.
    assert gui.library_view.model().refreshed_ids == []


def test_a_refresh_that_cannot_repaint_the_row_does_a_full_refresh(adder, plugin):
    db = FakeLegacyDB()
    book_id = db.new_api.seed("Existing")
    db.data = FakeView(db.new_api)
    gui = make_gui(db)
    action = _genesis(plugin, adder, gui)
    model = gui.library_view.model()
    model.refresh_ids = MagicMock(side_effect=RuntimeError("model gone"))

    action._refresh_gui(1, book_id)

    assert model.full_refreshes == 1


def test_a_book_the_view_already_has_is_not_shown_twice(adder, plugin):
    """If a future Calibre registers Cache adds itself, this must go quiet."""
    gui = make_gui()
    action = _genesis(plugin, adder, gui)
    db = gui.current_db
    book_id = db.new_api.seed("Self Registered")
    db.data.books_added([book_id])
    gui.library_view.model().books_added(1)
    before = list(gui.library_view.model().announced)

    action._refresh_gui(1)

    assert visible(gui) == ["Self Registered"]
    assert gui.library_view.model().announced == before


# ── the update path ──────────────────────────────────────────────────────────


def test_a_row_already_in_the_list_is_redrawn_not_inserted(adder, plugin):
    db = FakeLegacyDB()
    book_id = db.new_api.seed("Existing")
    db.data = FakeView(db.new_api)
    gui = make_gui(db)
    action = _genesis(plugin, adder, gui)

    action._refresh_gui_row(book_id)

    model = gui.library_view.model()
    assert model.refreshed_ids == [[book_id]]
    assert model.announced == []
    assert visible(gui) == ["Existing"]
    gui.tags_view.recount.assert_called_once_with()


def test_a_repaired_row_the_view_never_had_is_shown(adder, plugin):
    """An empty row left by a failed add, filled in by a later push.

    The Cache created it, so the view never heard of it and ``refresh_ids``
    would repaint nothing.
    """
    db = FakeLegacyDB()
    gui = make_gui(db)
    action = _genesis(plugin, adder, gui)
    book_id = db.new_api.seed("Repaired")

    action._refresh_gui_row(book_id)

    assert visible(gui) == ["Repaired"]
    assert gui.library_view.model().announced == [1]
    assert gui.library_view.model().refreshed_ids == []
    assert gui.library_view.model().consistent


def test_the_updated_dispatcher_reaches_the_final_view(adder, plugin):
    db = FakeLegacyDB()
    book_id = db.new_api.seed("Existing")
    db.data = FakeView(db.new_api)
    gui = types.SimpleNamespace()
    action = _genesis(plugin, adder, gui)
    attach_view(gui, db)

    action._on_updated(book_id)

    assert gui.library_view.model().refreshed_ids == [[book_id]]


# ── both transports go through the same refresh ──────────────────────────────


def test_pull_and_push_share_one_adder_entry_point(adder):
    """``add_book`` is a wrapper over ``add_book_detailed``, which the puller calls."""
    import inspect

    assert "add_book_detailed(" in inspect.getsource(adder.add_book)
    from pathlib import Path

    puller_src = (Path(__file__).resolve().parent.parent / "plugin" / "puller.py").read_text()
    assert "adder.add_book_detailed(" in puller_src
    assert "on_added=self._on_added" in puller_src
    assert "on_updated=self._on_updated" in puller_src


def test_the_pull_worker_gets_the_same_dispatchers_as_the_server(adder, plugin):
    gui = types.SimpleNamespace()
    action = _genesis(plugin, adder, gui)

    assert plugin.server_inst.start.call_args.kwargs["on_added"] is action._on_added
    assert plugin.server_inst.start.call_args.kwargs["on_updated"] is action._on_updated


# ── fallbacks: a refresh problem never breaks delivery ───────────────────────


def test_without_the_add_books_action_the_model_is_told_directly(adder, plugin, tmp_path):
    gui = make_gui(add_books=False)
    action = _genesis(plugin, adder, gui)

    _push(adder, gui.current_db, tmp_path, action._on_added)

    assert visible(gui) == ["Stub Title"]
    assert gui.library_view.model().announced == [1]
    assert gui.library_view.model().consistent
    gui.tags_view.recount.assert_called_once_with()


def test_a_failing_add_books_refresh_falls_back_to_a_full_refresh(adder, plugin, tmp_path):
    gui = make_gui()
    gui.iactions["Add Books"].refresh_gui = MagicMock(side_effect=RuntimeError("boom"))
    action = _genesis(plugin, adder, gui)

    _push(adder, gui.current_db, tmp_path, action._on_added)

    assert visible(gui) == ["Stub Title"]
    assert gui.library_view.model().full_refreshes == 1


def test_a_view_without_an_id_index_falls_back_to_a_full_refresh(adder, plugin, tmp_path):
    gui = make_gui()
    del gui.current_db.data._map  # a Calibre that renamed it
    gui.current_db.data._map_filtered = ()
    action = _genesis(plugin, adder, gui)
    book_id = gui.current_db.new_api.seed("Shown By Full Refresh")

    action._refresh_gui(1)

    assert gui.library_view.model().full_refreshes == 1
    assert visible(gui) == ["Shown By Full Refresh"]
    assert book_id in gui.current_db.data._map_filtered


def test_everything_failing_never_raises_and_never_fails_the_add(adder, plugin, tmp_path):
    gui = make_gui()
    gui.library_view.model().db.data.books_added = MagicMock(side_effect=RuntimeError("x"))
    gui.library_view.model().refresh = MagicMock(side_effect=RuntimeError("y"))
    action = _genesis(plugin, adder, gui)

    result = _push(adder, gui.current_db, tmp_path, action._on_added)

    assert result.duplicate is False
    assert result.book_id == 1
    action._refresh_gui_row(1)


def test_a_dispatch_that_raises_does_not_fail_the_add(adder, tmp_path):
    db = FakeLegacyDB()

    def broken(count):
        raise RuntimeError("queue closed")

    result = _push(adder, db, tmp_path, broken)

    assert result.duplicate is False


def test_the_timer_fallback_registers_rows_too(adder, tmp_path, monkeypatch):
    """No Dispatcher (calibre.gui2 missing): the old timer path shares the fix."""
    timers = []
    qt = types.ModuleType("qt")
    qt_core = types.ModuleType("qt.core")
    qt_core.QTimer = types.SimpleNamespace(singleShot=lambda ms, fn: timers.append(fn))
    monkeypatch.setitem(sys.modules, "qt", qt)
    monkeypatch.setitem(sys.modules, "qt.core", qt_core)
    gui = make_gui()
    path = tmp_path / "book.epub"
    path.write_bytes(b"x")

    adder.add_book(gui.current_db, str(path), gui=gui)
    assert len(timers) == 1
    timers[0]()

    assert visible(gui) == ["Stub Title"]
    assert gui.library_view.model().consistent


def test_the_dispatcher_is_built_on_the_gui_thread(adder, plugin):
    _FakeDispatcher.made.clear()
    _genesis(plugin, adder, types.SimpleNamespace())

    assert _FakeDispatcher.made
    assert all(d.constructed_on is threading.main_thread() for d in _FakeDispatcher.made)
