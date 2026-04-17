import json
import socket
import sys
import threading
import types
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def handler_factory(monkeypatch):
    calibre = types.ModuleType('calibre')
    constants = types.ModuleType('calibre.constants')
    constants.numeric_version = (9, 7, 0)
    ebooks = types.ModuleType('calibre.ebooks')
    metadata = types.ModuleType('calibre.ebooks.metadata')
    meta = types.ModuleType('calibre.ebooks.metadata.meta')
    meta.get_metadata = lambda f, fmt: MagicMock()
    sys.modules.update({
        'calibre': calibre,
        'calibre.constants': constants,
        'calibre.ebooks': ebooks,
        'calibre.ebooks.metadata': metadata,
        'calibre.ebooks.metadata.meta': meta,
    })

    calibre_plugins = types.ModuleType('calibre_plugins')
    bbridge = types.ModuleType('calibre_plugins.bindery_bridge')
    bplugin = types.ModuleType('calibre_plugins.bindery_bridge.plugin')
    import importlib
    import pathlib
    plugin_dir = pathlib.Path(__file__).resolve().parent.parent / 'plugin'
    sys.path.insert(0, str(plugin_dir))
    try:
        adder_mod = importlib.import_module('adder')
        bplugin.adder = adder_mod
        sys.modules['calibre_plugins'] = calibre_plugins
        sys.modules['calibre_plugins.bindery_bridge'] = bbridge
        sys.modules['calibre_plugins.bindery_bridge.plugin'] = bplugin
        sys.modules['calibre_plugins.bindery_bridge.plugin.adder'] = adder_mod

        if 'handlers' in sys.modules:
            del sys.modules['handlers']
        handlers = importlib.import_module('handlers')
        yield handlers
    finally:
        sys.path.pop(0)
        for name in list(sys.modules):
            if name.startswith('calibre_plugins') or name.startswith('calibre'):
                sys.modules.pop(name, None)


def _serve(handler_cls):
    port = _free_port()
    httpd = ThreadingHTTPServer(('127.0.0.1', port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def test_health_endpoint(handler_factory):
    db = MagicMock()
    db.library_path = '/tmp/library'
    handler_cls = handler_factory.make_handler(api_key='secret', get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/v1/health' % port, timeout=5) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode('utf-8'))
        assert payload['plugin_version']
        assert payload['calibre_version']
        assert payload['library'] == '/tmp/library'
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_books_requires_auth(handler_factory):
    db = MagicMock()
    handler_cls = handler_factory.make_handler(api_key='secret', get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:%d/v1/books' % port,
            data=json.dumps({'path': '/tmp/x.epub'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raised = False
        except urllib.error.HTTPError as exc:
            raised = True
            assert exc.code == 401
        assert raised
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_books_happy_path(handler_factory, tmp_path, monkeypatch):
    book = tmp_path / 'book.epub'
    book.write_bytes(b'stub')

    db = MagicMock()
    db.library_path = str(tmp_path)
    db.new_api.add_books.return_value = ([123], {})

    handler_cls = handler_factory.make_handler(api_key='secret', get_db=lambda: db)
    httpd, port = _serve(handler_cls)
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:%d/v1/books' % port,
            data=json.dumps({'path': str(book)}).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer secret',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 201
            payload = json.loads(resp.read().decode('utf-8'))
        assert payload == {'id': 123, 'duplicate': False}
    finally:
        httpd.shutdown()
        httpd.server_close()
