"""calibre-bridge 0.6.3: error codes, the GUI refresh, and the health leak.

Three separate fixes, grouped because they ship together:

* A source file that exists but cannot be opened, and a failure inside
  Calibre's own copy into the library, both came back as ``500 internal``.
  They now have their own codes, ``path_unreadable`` and ``copy_failed``.
* The GUI refresh after an add was a ``QTimer.singleShot`` issued from the
  HTTP worker thread, which has no Qt event loop, so it never ran. The action
  now builds a ``calibre.gui2.Dispatcher`` on the GUI thread and the adder
  calls that instead.
* ``GET /v1/health`` is unauthenticated and returned the library path to
  anyone. It now returns it only to a caller presenting the bearer token.
"""

import sys
import threading
from unittest.mock import MagicMock

import pytest

from .test_config import _cleanup_action, _load_init_module, _make_action_stubs
from .test_dedupe_ladder import FakeDB, FakeMetadata, _book
from .test_empty_rows import FormatLibrary


@pytest.fixture
def adder(bridge_adder):
    bridge_adder.get_metadata = lambda stream, fmt: FakeMetadata()
    return bridge_adder


def _meta(bindery_id="42"):
    return {
        "title": "Eight Stories",
        "authors": ["Isaac Asimov"],
        "identifiers": {"bindery": bindery_id},
    }


def _unreadable(tmp_path):
    """A path that exists, has a book extension, and cannot be opened as a file.

    A directory rather than a chmod 000 file, so the test means the same thing
    when the suite runs as root.
    """
    path = tmp_path / "book.epub"
    path.mkdir()
    return str(path)


# ── error codes: adder ───────────────────────────────────────────────────────


def test_an_unreadable_source_raises_source_unreadable(adder, tmp_path):
    path = _unreadable(tmp_path)
    with pytest.raises(adder.SourceUnreadable) as info:
        adder.add_book(FakeDB(FormatLibrary()), path, metadata=_meta())
    assert path in str(info.value)
    assert not isinstance(info.value, FileNotFoundError)


def test_a_missing_source_is_still_file_not_found(adder, tmp_path):
    missing = str(tmp_path / "gone.epub")
    with pytest.raises(FileNotFoundError):
        adder.add_book(FakeDB(FormatLibrary()), missing, metadata=_meta())


def test_a_failed_copy_raises_copy_failed_and_still_rolls_back(adder, tmp_path):
    lib = FormatLibrary()
    lib.fail_copy = True
    path = _book(tmp_path)

    with pytest.raises(adder.CopyFailed) as info:
        adder.add_book(FakeDB(lib), path, metadata=_meta())

    assert path in str(info.value)
    assert isinstance(info.value.__cause__, OSError)
    assert info.value.__cause__.errno == 22
    # The 0.6.2 rollback of the empty row is kept.
    assert lib.rows == {}
    assert len(lib.removed) == 1


def test_a_failed_attach_to_an_empty_row_raises_copy_failed(adder, tmp_path):
    lib = FormatLibrary()
    ghost = lib.seed("Eight Stories", ["Isaac Asimov"], {"bindery": "42"})

    def broken_add_format(book_id, fmt, path, replace=True, run_hooks=True):
        raise PermissionError(13, "Permission denied")

    lib.add_format = broken_add_format
    path = _book(tmp_path)

    with pytest.raises(adder.CopyFailed) as info:
        adder.add_book(FakeDB(lib), path, metadata=_meta())

    assert path in str(info.value)
    # The row predates this request, so it is not ours to remove.
    assert ghost in lib.rows


# ── error codes: on the wire ─────────────────────────────────────────────────


def test_unreadable_path_is_400_path_unreadable(bridge_handlers, serve_bridge, tmp_path):
    db = MagicMock()
    db.new_api.search.return_value = set()
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))

    status, payload, _ = bridge.call("POST", "/v1/books", body={"path": _unreadable(tmp_path)})

    assert (status, payload.get("code")) == (400, "path_unreadable")
    db.new_api.add_books.assert_not_called()


def test_copy_failure_is_500_copy_failed_naming_the_path(bridge_handlers, serve_bridge, tmp_path):
    db = MagicMock()
    db.new_api.search.return_value = set()
    db.new_api.find_identical_books.return_value = set()
    db.new_api.add_books.side_effect = OSError(22, "Invalid argument")
    bridge = serve_bridge(bridge_handlers.make_handler(api_key="", get_db=lambda: db))
    path = _book(tmp_path)

    status, payload, _ = bridge.call(
        "POST", "/v1/books", body={"path": path, "metadata": {"identifiers": {"bindery": "7"}}}
    )

    assert (status, payload.get("code")) == (500, "copy_failed")
    assert path in payload["error"]


def test_the_new_codes_are_constants(bridge_handlers):
    assert bridge_handlers.CODE_PATH_UNREADABLE == "path_unreadable"
    assert bridge_handlers.CODE_COPY_FAILED == "copy_failed"


# ── GUI refresh: plumbing from the action to the adder ───────────────────────


def _add_ok_db():
    db = MagicMock()
    db.new_api.search.return_value = set()
    db.new_api.add_books.return_value = ([42], {})
    return db


def test_handler_passes_on_added_to_the_adder(bridge_handlers, serve_bridge, tmp_path):
    seen = []
    bridge = serve_bridge(
        bridge_handlers.make_handler(
            api_key="", get_db=_add_ok_db, get_gui=MagicMock, on_added=seen.append
        )
    )

    status, _payload, _ = bridge.call("POST", "/v1/books", body={"path": _book(tmp_path)})

    assert status == 201
    assert seen == [1]


def test_bridge_server_forwards_on_added(bridge_handlers, tmp_path):
    import importlib
    import pathlib

    from .conftest import Bridge, free_port

    status_mod = importlib.import_module("status")
    bplugin = sys.modules["calibre_plugins.bindery_bridge.plugin"]
    bplugin.status = status_mod
    sys.modules["calibre_plugins.bindery_bridge.plugin.status"] = status_mod
    sys.modules["calibre_plugins.bindery_bridge.plugin.handlers"] = bridge_handlers
    sys.modules.pop("server", None)
    server_mod = importlib.import_module("server")
    assert pathlib.Path(server_mod.__file__).name == "server.py"

    seen = []
    srv = server_mod.BridgeServer()
    port = free_port()
    srv.start(port=port, bind_host="127.0.0.1", api_key="", get_db=_add_ok_db, on_added=seen.append)
    try:
        client = Bridge.__new__(Bridge)
        client.port = port
        status, _payload, _ = client.call("POST", "/v1/books", body={"path": _book(tmp_path)})
    finally:
        srv.stop()
        sys.modules.pop("server", None)
        sys.modules.pop("status", None)
    assert status == 201
    assert seen == [1]


class _FakeDispatcher:
    """Records construction, like calibre.gui2.Dispatcher wraps a callable."""

    made = []

    def __init__(self, fn):
        self.fn = fn
        self.constructed_on = threading.current_thread()
        _FakeDispatcher.made.append(self)

    def __call__(self, *args):
        self.fn(*args)


def _genesis(stubs, mod, mock_server_cls, mock_cfg, with_dispatcher):
    import types

    if with_dispatcher:
        stubs["calibre.gui2"].Dispatcher = _FakeDispatcher
        sys.modules["calibre.gui2"] = stubs["calibre.gui2"]
    plugin_pkg = sys.modules["calibre_plugins.bindery_bridge.plugin"]
    for name, attr, value in (
        ("config", "load_config", mock_cfg),
        ("server", "BridgeServer", mock_server_cls),
        ("status", None, None),
    ):
        sub = types.ModuleType(f"calibre_plugins.bindery_bridge.plugin.{name}")
        if attr:
            setattr(sub, attr, value)
        setattr(plugin_pkg, name, sub)
        sys.modules[f"calibre_plugins.bindery_bridge.plugin.{name}"] = sub
    action = mod.BinderyBridgeAction.__new__(mod.BinderyBridgeAction)
    action.gui = MagicMock()
    action.qaction = MagicMock()
    action.genesis()
    return action


def _cleanup_genesis(stubs):
    for name in ("config", "server", "status"):
        sys.modules.pop(f"calibre_plugins.bindery_bridge.plugin.{name}", None)
    _cleanup_action(stubs)


def test_genesis_builds_a_dispatcher_and_hands_it_to_the_server():
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    _FakeDispatcher.made.clear()
    try:
        action = _genesis(stubs, mod, mock_server_cls, mock_cfg, with_dispatcher=True)

        # 0.7.0 builds a second one, on_updated; see test_bridge_0_7_0.
        assert len(_FakeDispatcher.made) == 2
        dispatcher = _FakeDispatcher.made[0]
        assert dispatcher.constructed_on is threading.main_thread()
        assert action._on_added is dispatcher
        assert mock_server_inst.start.call_args.kwargs["on_added"] is dispatcher

        # Calling it (as the worker thread would) runs the refresh body.
        dispatcher(3)
        action.gui.library_view.model().books_added.assert_called_once_with(3)
        action.gui.tags_view.recount.assert_called_once_with()
    finally:
        _cleanup_genesis(stubs)


def test_genesis_without_calibre_gui2_leaves_the_fallback():
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    try:
        action = _genesis(stubs, mod, mock_server_cls, mock_cfg, with_dispatcher=False)
        assert action._on_added is None
        assert mock_server_inst.start.call_args.kwargs["on_added"] is None
    finally:
        _cleanup_genesis(stubs)


def test_refresh_gui_survives_a_broken_gui():
    stubs = _make_action_stubs()
    mod, mock_server_cls, mock_server_inst, mock_cfg = _load_init_module(stubs)
    try:
        action = mod.BinderyBridgeAction.__new__(mod.BinderyBridgeAction)
        action.gui = MagicMock()
        action.gui.library_view.model.side_effect = RuntimeError("closing")
        action._refresh_gui(1)
    finally:
        _cleanup_action(stubs)


# ── health: the library path needs the token ─────────────────────────────────


def _health_bridge(bridge_handlers, serve_bridge, api_key):
    db = MagicMock()
    db.library_path = "/srv/calibre/Library"
    return serve_bridge(bridge_handlers.make_handler(api_key=api_key, get_db=lambda: db))


def test_health_hides_the_library_without_a_token(bridge_handlers, serve_bridge):
    bridge = _health_bridge(bridge_handlers, serve_bridge, "secret")
    status, payload, _ = bridge.call("GET", "/v1/health")
    assert status == 200
    assert payload["library"] == ""
    assert payload["plugin_version"] == "0.8.0"
    assert "error_codes" in payload["capabilities"]


def test_health_hides_the_library_from_a_wrong_token(bridge_handlers, serve_bridge):
    bridge = _health_bridge(bridge_handlers, serve_bridge, "secret")
    status, payload, _ = bridge.call("GET", "/v1/health", token="wrong")
    assert status == 200
    assert payload["library"] == ""


def test_health_shows_the_library_to_the_right_token(bridge_handlers, serve_bridge):
    bridge = _health_bridge(bridge_handlers, serve_bridge, "secret")
    status, payload, _ = bridge.call("GET", "/v1/health", token="secret")
    assert status == 200
    assert payload["library"] == "/srv/calibre/Library"


def test_health_shows_the_library_when_no_key_is_configured(bridge_handlers, serve_bridge):
    """No api_key is only possible on a loopback bind, where everything is open."""
    bridge = _health_bridge(bridge_handlers, serve_bridge, "")
    status, payload, _ = bridge.call("GET", "/v1/health")
    assert status == 200
    assert payload["library"] == "/srv/calibre/Library"
