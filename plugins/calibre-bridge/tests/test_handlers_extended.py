"""Edge-case tests for make_handler — paths not covered by test_handlers.py."""

import json
import socket
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _shutdown(httpd):
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def handler_factory(monkeypatch):
    import importlib
    import pathlib

    calibre = types.ModuleType("calibre")
    constants = types.ModuleType("calibre.constants")
    constants.numeric_version = (9, 7, 0)
    ebooks = types.ModuleType("calibre.ebooks")
    metadata = types.ModuleType("calibre.ebooks.metadata")
    meta = types.ModuleType("calibre.ebooks.metadata.meta")
    meta.get_metadata = lambda f, fmt: MagicMock()
    sys.modules.update(
        {
            "calibre": calibre,
            "calibre.constants": constants,
            "calibre.ebooks": ebooks,
            "calibre.ebooks.metadata": metadata,
            "calibre.ebooks.metadata.meta": meta,
        }
    )

    calibre_plugins = types.ModuleType("calibre_plugins")
    bbridge = types.ModuleType("calibre_plugins.bindery_bridge")
    bplugin = types.ModuleType("calibre_plugins.bindery_bridge.plugin")
    plugin_dir = pathlib.Path(__file__).resolve().parent.parent / "plugin"
    sys.path.insert(0, str(plugin_dir))
    try:
        adder_mod = importlib.import_module("adder")
        bplugin.adder = adder_mod
        sys.modules["calibre_plugins"] = calibre_plugins
        sys.modules["calibre_plugins.bindery_bridge"] = bbridge
        sys.modules["calibre_plugins.bindery_bridge.plugin"] = bplugin
        sys.modules["calibre_plugins.bindery_bridge.plugin.adder"] = adder_mod

        for mod_name in list(sys.modules):
            if mod_name == "handlers":
                del sys.modules[mod_name]
        handlers = importlib.import_module("handlers")
        yield handlers
    finally:
        sys.path.pop(0)
        for name in list(sys.modules):
            if name.startswith("calibre_plugins") or name.startswith("calibre"):
                sys.modules.pop(name, None)


def _serve(handler_cls):
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


# ── GET endpoints ─────────────────────────────────────────────────────────────


def test_get_unknown_path_returns_404(handler_factory):
    db = MagicMock()
    handler_cls = handler_factory.make_handler(api_key="k", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/not/a/real/path", timeout=5)
        assert exc_info.value.code == 404
        payload = json.loads(exc_info.value.read())
        assert payload["error"] == "not found"
    finally:
        _shutdown(httpd)


def test_health_with_db_none(handler_factory):
    handler_cls = handler_factory.make_handler(api_key="k", get_db=lambda: None)
    httpd, port = _serve(handler_cls)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=5) as r:
            payload = json.loads(r.read())
        assert payload["library"] == ""
    finally:
        _shutdown(httpd)


def test_health_library_path_exception(handler_factory):
    db = MagicMock()
    type(db).library_path = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    handler_cls = handler_factory.make_handler(api_key="k", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=5) as r:
            payload = json.loads(r.read())
        assert payload["library"] == ""
    finally:
        _shutdown(httpd)


# ── POST /v1/books edge cases ──────────────────────────────────────────────────


def test_post_unknown_path_returns_404(handler_factory):
    db = MagicMock()
    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/other",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 404
    finally:
        _shutdown(httpd)


def test_post_books_db_none_returns_503(handler_factory):
    handler_cls = handler_factory.make_handler(api_key="k", get_db=lambda: None)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=json.dumps({"path": "/tmp/x.epub"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer k"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 503
    finally:
        _shutdown(httpd)


def test_post_books_invalid_json_returns_400(handler_factory):
    db = MagicMock()
    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=b"not json",
            headers={"Content-Type": "application/json", "Content-Length": "8"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 400
        payload = json.loads(exc_info.value.read())
        assert payload["error"] == "invalid json"
    finally:
        _shutdown(httpd)


def test_post_books_missing_path_returns_400(handler_factory):
    db = MagicMock()
    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 400
        payload = json.loads(exc_info.value.read())
        assert payload["error"] == "path required"
    finally:
        _shutdown(httpd)


def test_post_books_non_string_path_returns_400(handler_factory):
    db = MagicMock()
    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=json.dumps({"path": 42}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 400
    finally:
        _shutdown(httpd)


def test_post_books_file_not_found_returns_400(handler_factory, tmp_path):
    db = MagicMock()
    db.new_api.add_books.side_effect = FileNotFoundError("/no/such/file.epub")
    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=json.dumps({"path": "/no/such/file.epub"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 400
    finally:
        _shutdown(httpd)


def test_post_books_duplicate_returns_409(handler_factory, tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub")

    db = MagicMock()
    db.library_path = str(tmp_path)
    db.new_api.add_books.return_value = ([], {7: MagicMock()})

    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=json.dumps({"path": str(book)}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 409
        payload = json.loads(exc_info.value.read())
        assert payload == {"id": 7, "duplicate": True}
    finally:
        _shutdown(httpd)


def test_post_books_no_api_key_allows_any_request(handler_factory, tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"stub")

    db = MagicMock()
    db.new_api.add_books.return_value = ([99], {})

    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=json.dumps({"path": str(book)}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 201
    finally:
        _shutdown(httpd)


def test_post_books_empty_body_returns_400(handler_factory):
    db = MagicMock()
    handler_cls = handler_factory.make_handler(api_key="", get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/books",
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 400
    finally:
        _shutdown(httpd)
