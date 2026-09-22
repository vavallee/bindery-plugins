"""Shared fixtures and HTTP helpers for the calibre-bridge test suite.

The older test modules each define a local ``handler_factory`` fixture. Those
shadow anything declared here, so this file only introduces new names and
leaves the existing modules untouched.
"""

import importlib
import json
import pathlib
import socket
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "plugin"


class StubMetadata:
    """What ``calibre.ebooks.metadata.meta.get_metadata`` hands the adder.

    A real object rather than a MagicMock, so "this attribute was never
    written" is an assertion the tests can actually make.
    """

    def __init__(self):
        self.title = "Stub Title"
        self.authors = []
        self.author_sort = ""
        self.comments = None
        self.publisher = None
        self.pubdate = None
        self.tags = []
        self.languages = []
        self.series = None
        self.series_index = 1.0
        self.rating = None
        self.cover_data = (None, None)
        self.cover = None
        self._identifiers = {}

    def get_identifiers(self):
        return dict(self._identifiers)

    def set_identifiers(self, value):
        self._identifiers = dict(value)


def _install_calibre_stubs() -> None:
    calibre = types.ModuleType("calibre")
    constants = types.ModuleType("calibre.constants")
    constants.numeric_version = (9, 7, 0)
    utils = types.ModuleType("calibre.utils")
    date = types.ModuleType("calibre.utils.date")
    ebooks = types.ModuleType("calibre.ebooks")
    metadata = types.ModuleType("calibre.ebooks.metadata")
    meta = types.ModuleType("calibre.ebooks.metadata.meta")
    meta.get_metadata = lambda f, fmt: StubMetadata()
    date.parse_date = lambda value: value
    sys.modules.update(
        {
            "calibre": calibre,
            "calibre.constants": constants,
            "calibre.utils": utils,
            "calibre.utils.date": date,
            "calibre.ebooks": ebooks,
            "calibre.ebooks.metadata": metadata,
            "calibre.ebooks.metadata.meta": meta,
        }
    )


@pytest.fixture(autouse=True)
def _baseline_calibre_stubs():
    """Reinstall the calibre stubs before every test.

    Most modules in this suite stub calibre in a fixture and tear the stubs
    back out again, which leaves whichever module runs next importing the
    plugin against a bare interpreter. Making the baseline autouse means a
    module no longer depends on the alphabetical position of its neighbours.
    """
    _install_calibre_stubs()
    yield


@pytest.fixture
def bridge_handlers():
    """Import ``plugin/handlers.py`` with calibre and the adder stubbed in."""
    _install_calibre_stubs()
    calibre_plugins = types.ModuleType("calibre_plugins")
    bbridge = types.ModuleType("calibre_plugins.bindery_bridge")
    bplugin = types.ModuleType("calibre_plugins.bindery_bridge.plugin")
    sys.path.insert(0, str(_PLUGIN_DIR))
    try:
        sys.modules.pop("adder", None)
        adder_mod = importlib.import_module("adder")
        bplugin.adder = adder_mod
        sys.modules["calibre_plugins"] = calibre_plugins
        sys.modules["calibre_plugins.bindery_bridge"] = bbridge
        sys.modules["calibre_plugins.bindery_bridge.plugin"] = bplugin
        sys.modules["calibre_plugins.bindery_bridge.plugin.adder"] = adder_mod
        sys.modules.pop("handlers", None)
        yield importlib.import_module("handlers")
    finally:
        sys.path.pop(0)
        sys.modules.pop("handlers", None)
        sys.modules.pop("adder", None)
        for name in list(sys.modules):
            if name.startswith("calibre_plugins") or name.startswith("calibre"):
                sys.modules.pop(name, None)


@pytest.fixture
def bridge_adder():
    """Import ``plugin/adder.py`` with calibre stubbed in."""
    _install_calibre_stubs()
    sys.path.insert(0, str(_PLUGIN_DIR))
    try:
        sys.modules.pop("adder", None)
        yield importlib.import_module("adder")
    finally:
        sys.path.pop(0)
        sys.modules.pop("adder", None)
        for name in list(sys.modules):
            if name.startswith("calibre"):
                sys.modules.pop(name, None)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bridge:
    """A running handler plus a tiny JSON client for it."""

    def __init__(self, handler_cls):
        self.port = free_port()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), handler_cls)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def call(self, method, path, body=None, token=None, raw_body=None, headers=None):
        """Return ``(status, payload, headers)``. Never raises on 4xx/5xx."""
        hdrs = dict(headers or {})
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=hdrs, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, _decode(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read()), dict(exc.headers)


def _decode(raw: bytes):
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


@pytest.fixture
def serve_bridge():
    """Factory fixture: ``serve_bridge(handler_cls)`` -> :class:`Bridge`."""
    running = []

    def _serve(handler_cls) -> Bridge:
        bridge = Bridge(handler_cls)
        running.append(bridge)
        return bridge

    yield _serve
    for bridge in running:
        bridge.close()
