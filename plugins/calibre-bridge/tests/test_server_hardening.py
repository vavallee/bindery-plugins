"""Server hardening: connection limits, keyless mode, key strength and throttling."""

import contextlib
import http.client
import importlib
import json
import select
import socket
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from .conftest import _PLUGIN_DIR, PLUGIN_VERSION, free_port

KEY = "k" * 32


@pytest.fixture
def server_module():
    """Import ``plugin/server.py`` with its sibling modules wired in."""
    sys.path.insert(0, str(_PLUGIN_DIR))
    try:
        for name in ("server", "status", "handlers", "adder"):
            sys.modules.pop(name, None)
        bplugin = types.ModuleType("calibre_plugins.bindery_bridge.plugin")
        mods = {n: importlib.import_module(n) for n in ("adder", "handlers", "status")}
        for name, mod in mods.items():
            setattr(bplugin, name, mod)
            sys.modules[f"calibre_plugins.bindery_bridge.plugin.{name}"] = mod
        sys.modules["calibre_plugins"] = types.ModuleType("calibre_plugins")
        sys.modules["calibre_plugins.bindery_bridge"] = types.ModuleType(
            "calibre_plugins.bindery_bridge"
        )
        sys.modules["calibre_plugins.bindery_bridge.plugin"] = bplugin
        yield importlib.import_module("server")
    finally:
        sys.path.pop(0)
        for name in ("server", "status", "handlers", "adder"):
            sys.modules.pop(name, None)
        for name in list(sys.modules):
            if name.startswith("calibre_plugins"):
                sys.modules.pop(name, None)


def _db(tmp_path):
    db = MagicMock()
    db.library_path = str(tmp_path)
    return db


def _request(port, method, path, body=None, headers=None, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        payload = json.loads(raw) if raw else {}
        return resp.status, payload, dict(resp.getheaders())
    finally:
        conn.close()


def _bearer(key=KEY):
    return {"Authorization": f"Bearer {key}"}


def _handler_threads() -> int:
    return sum(1 for t in threading.enumerate() if "process_request_thread" in t.name)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ── M1: stalled sockets cannot pin the server ─────────────────────────────────


def test_stalled_sockets_are_capped_timed_out_and_do_not_starve_the_server(server_module, tmp_path):
    cap = 4
    srv = server_module.BridgeServer(max_connections=cap)
    port = free_port()
    srv.start(port, "127.0.0.1", "", lambda: _db(tmp_path), header_timeout=0.5)
    stalled = []
    try:
        for _ in range(cap * 3):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # never finished
            stalled.append(s)
        assert _wait_for(lambda: _handler_threads() == cap)

        # Over the cap: answered 503 at once and closed, never queued.
        status, payload, headers = _request(port, "GET", "/v1/health", timeout=2)
        assert status == 503
        assert payload["code"] == "busy"
        assert headers["Connection"] == "close"

        # Once the read timeout has fired the slots come back and a real
        # request is served from a fresh socket, and the threads have ended.
        assert _wait_for(lambda: _handler_threads() == 0, timeout=5)
        status, payload, _ = _request(port, "GET", "/v1/health", timeout=2)
        assert status == 200
        assert payload["plugin_version"]
    finally:
        for s in stalled:
            s.close()
        srv.stop()


def test_handler_threads_are_daemons(server_module, tmp_path):
    srv = server_module.BridgeServer()
    srv.start(free_port(), "127.0.0.1", "", lambda: _db(tmp_path))
    try:
        assert srv._httpd.daemon_threads is True
    finally:
        srv.stop()


def test_a_large_slow_body_still_succeeds(server_module, tmp_path):
    """The short header timeout must not cut off a body that keeps arriving."""
    srv = server_module.BridgeServer()
    port = free_port()
    srv.start(port, "127.0.0.1", KEY, lambda: _db(tmp_path), header_timeout=0.4, body_timeout=2.0)
    try:
        padding = "x" * (2 * 1024 * 1024)
        body = json.dumps({"path": str(tmp_path / "missing.epub"), "metadata": {"x": padding}})
        raw = body.encode()
        s = socket.create_connection(("127.0.0.1", port))
        head = (
            "POST /v1/books HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {KEY}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(raw)}\r\n\r\n"
        ).encode()
        s.sendall(head)
        started = time.monotonic()
        step = len(raw) // 5 + 1
        for i in range(0, len(raw), step):
            s.sendall(raw[i : i + step])
            time.sleep(0.25)  # more than the header timeout in total, under the body timeout
        assert time.monotonic() - started > 0.4
        s.settimeout(5)
        reply = b""
        while chunk := s.recv(65536):
            reply += chunk
        s.close()
        assert reply.startswith(b"HTTP/1.0 400"), reply[:80]
        assert b"path_not_found" in reply  # the whole body was read and parsed
    finally:
        srv.stop()


def test_a_stalled_body_is_cut_off(server_module, tmp_path):
    srv = server_module.BridgeServer()
    port = free_port()
    srv.start(port, "127.0.0.1", KEY, lambda: _db(tmp_path), header_timeout=5, body_timeout=0.4)
    try:
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(
            (
                "POST /v1/books HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {KEY}\r\nContent-Type: application/json\r\n"
                "Content-Length: 500\r\n\r\n{"
            ).encode()
        )
        s.settimeout(5)
        reply = b""
        while chunk := s.recv(65536):
            reply += chunk
        s.close()
        assert reply.startswith(b"HTTP/1.0 408"), reply[:80]
        assert _wait_for(lambda: _handler_threads() == 0)
    finally:
        srv.stop()


# ── M2: keyless mode, Origin, Host and Content-Type ───────────────────────────

ROUTES = [
    ("GET", "/v1/health"),
    ("GET", "/v1/paths?path=/etc"),
    ("GET", "/nowhere"),
    ("POST", "/v1/books"),
    ("POST", "/nowhere"),
    ("PATCH", "/v1/books/1"),
]


@pytest.fixture
def make_bridge(bridge_handlers, serve_bridge, tmp_path):
    def _make(api_key, **kwargs):
        db = _db(tmp_path)
        cls = bridge_handlers.make_handler(api_key=api_key, get_db=lambda: db, **kwargs)
        bridge = serve_bridge(cls)
        bridge.db = db
        return bridge

    return _make


@pytest.mark.parametrize("key", ["", KEY])
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_any_origin_header_is_refused_on_every_route(make_bridge, key, method, path):
    bridge = make_bridge(key)
    headers = {"Origin": "http://evil.example", "Content-Type": "application/json"}
    if key:
        headers.update(_bearer(key))
    body = b"{}" if method in ("POST", "PATCH") else None
    status, payload, _ = _request(bridge.port, method, path, body=body, headers=headers)
    assert status == 403
    assert payload["code"] == "forbidden"


def test_origin_null_is_refused_too(make_bridge):
    bridge = make_bridge("")
    assert _request(bridge.port, "GET", "/v1/health", headers={"Origin": "null"})[0] == 403


def test_a_cross_origin_text_plain_post_cannot_import_a_file(make_bridge, tmp_path):
    secret = tmp_path / "notes.kdbx"
    secret.write_bytes(b"not a book")
    bridge = make_bridge("")
    status, _, _ = _request(
        bridge.port,
        "POST",
        "/v1/books",
        body=json.dumps({"path": str(secret)}),
        headers={"Origin": "http://evil.example", "Content-Type": "text/plain"},
    )
    assert status == 403
    bridge.db.new_api.add_books.assert_not_called()


@pytest.mark.parametrize("path", ["/v1/health", "/v1/paths?path=/etc"])
def test_keyless_with_a_foreign_host_is_refused(make_bridge, path):
    bridge = make_bridge("")
    status, payload, _ = _request(
        bridge.port, "GET", path, headers={"Host": f"rebind.example:{bridge.port}"}
    )
    assert status == 403
    assert payload["code"] == "forbidden"


@pytest.mark.parametrize(
    "host", ["127.0.0.1:{port}", "localhost", "localhost:{port}", "[::1]:{port}", "127.5.5.5"]
)
def test_keyless_with_a_loopback_host_works(make_bridge, host):
    bridge = make_bridge("")
    status, payload, _ = _request(
        bridge.port, "GET", "/v1/health", headers={"Host": host.format(port=bridge.port)}
    )
    assert status == 200
    assert payload["library"]


def test_keyless_without_a_host_header_is_refused(make_bridge):
    bridge = make_bridge("")
    s = socket.create_connection(("127.0.0.1", bridge.port))
    s.sendall(b"GET /v1/health HTTP/1.0\r\n\r\n")
    s.settimeout(5)
    reply = b""
    while chunk := s.recv(65536):
        reply += chunk
    s.close()
    assert reply.startswith(b"HTTP/1.0 403")


def test_with_a_key_a_non_loopback_host_is_fine(make_bridge):
    bridge = make_bridge(KEY)
    status, payload, _ = _request(
        bridge.port, "GET", "/v1/health", headers={**_bearer(), "Host": "calibre.lan:8099"}
    )
    assert status == 200
    assert payload["library"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("LOCALHOST:8099", True),
        ("127.0.0.1", True),
        ("127.255.0.1:1", True),
        ("[::1]", True),
        ("[::1]:8099", True),
        ("", False),
        ("evil.example", False),
        ("localhost.evil.example", False),
        ("127.0.0.1.evil.example", False),
        ("0.0.0.0", False),
        ("128.0.0.1", False),
        ("[::2]:80", False),
        ("[::1", False),
        # Parsed strictly: userinfo, junk ports, spaces, unbracketed IPv6 and
        # a second port or host are all refused.
        ("::1", False),
        ("127.0.0.1:80@evil.com", False),
        ("localhost:x@evil.com", False),
        ("localhost:x", False),
        ("localhost:", False),
        ("localhost:0", False),
        ("localhost:65536", False),
        ("localhost:65535", True),
        ("localhost:8099:1", False),
        ("localhost.", True),
        ("localhost..", False),
        ("user@localhost", False),
        ("localhost evil.com", False),
        ("local host", False),
        ("localhost/x", False),
        ("localhost\\x", False),
        ("127.0.0.1 ", True),
        ("127.1", False),
        ("[127.0.0.1]", False),
        ("[::1]x", False),
        ("[::1]:x", False),
        ("[::1]@evil.com", False),
        ("[::1%eth0]", False),
        ("localhost:\u0663", False),
    ],
)
def test_host_is_loopback(bridge_handlers, host, expected):
    assert bridge_handlers._host_is_loopback(host) is expected


@pytest.mark.parametrize("key", ["", KEY])
def test_post_books_requires_a_json_content_type(make_bridge, key):
    bridge = make_bridge(key)
    base = _bearer(key) if key else {}
    body = json.dumps({"path": "/nowhere/x.epub"})
    for content_type in ("text/plain", "application/x-www-form-urlencoded", None):
        headers = dict(base)
        if content_type:
            headers["Content-Type"] = content_type
        status, payload, _ = _request(bridge.port, "POST", "/v1/books", body=body, headers=headers)
        assert status == 415, content_type
        assert payload["code"] == "unsupported_media_type"
    # JSON, with a parameter or in another case, gets past the check.
    for content_type in ("application/json", "Application/JSON; charset=utf-8"):
        headers = {**base, "Content-Type": content_type}
        status, payload, _ = _request(bridge.port, "POST", "/v1/books", body=body, headers=headers)
        assert status == 400
        assert payload["code"] == "path_not_found"


def test_content_type_is_checked_after_auth(make_bridge):
    bridge = make_bridge(KEY)
    status, _, _ = _request(
        bridge.port,
        "POST",
        "/v1/books",
        body="{}",
        headers={**_bearer("w" * 32), "Content-Type": "text/plain"},
    )
    assert status == 401


# ── L4: short keys and the failure throttle ───────────────────────────────────


def test_short_key_on_a_network_bind_is_refused(server_module, tmp_path):
    srv = server_module.BridgeServer()
    with pytest.raises(ValueError, match="16 characters"):
        srv.start(free_port(), "0.0.0.0", "short", lambda: _db(tmp_path))
    assert srv._httpd is None


def test_a_key_of_exactly_the_minimum_length_is_accepted_on_a_network_bind(server_module, tmp_path):
    srv = server_module.BridgeServer()
    srv.start(free_port(), "0.0.0.0", "k" * 16, lambda: _db(tmp_path))
    srv.stop()


def test_short_key_on_loopback_is_allowed(server_module, tmp_path):
    srv = server_module.BridgeServer()
    port = free_port()
    srv.start(port, "127.0.0.1", "short", lambda: _db(tmp_path))
    try:
        assert _request(port, "GET", "/v1/health", headers=_bearer("short"))[0] == 200
    finally:
        srv.stop()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_throttle_trips_after_five_failures_and_resets_after_the_window(make_bridge):
    clock = _Clock()
    bridge = make_bridge(KEY, clock=clock)
    wrong = _bearer("w" * 32)
    for _ in range(5):
        status, payload, _ = _request(bridge.port, "POST", "/v1/books", body="{}", headers=wrong)
        assert status == 401
    status, payload, headers = _request(bridge.port, "POST", "/v1/books", body="{}", headers=wrong)
    assert status == 429
    assert payload["code"] == "rate_limited"
    assert int(headers["Retry-After"]) == 60

    # Blocked means blocked: a correct key from the same address waits too.
    clock.now += 30
    status, _, headers = _request(bridge.port, "GET", "/v1/paths?path=/x", headers=_bearer())
    assert status == 429
    assert int(headers["Retry-After"]) == 30

    clock.now += 31
    status, _, _ = _request(bridge.port, "GET", "/v1/paths?path=/x", headers=_bearer())
    assert status != 429 and status != 401
    # And the counter starts afresh.
    for _ in range(4):
        assert _request(bridge.port, "GET", "/v1/paths?path=/x", headers=wrong)[0] == 401


def test_a_wrong_token_on_health_counts_but_a_missing_one_does_not(make_bridge):
    bridge = make_bridge(KEY, clock=_Clock())
    for _ in range(20):
        assert _request(bridge.port, "GET", "/v1/health")[0] == 200
    assert _request(bridge.port, "GET", "/v1/paths?path=/x", headers=_bearer())[0] != 429
    for _ in range(5):
        _request(bridge.port, "GET", "/v1/health", headers=_bearer("w" * 32))
    status, payload, _ = _request(bridge.port, "GET", "/v1/paths?path=/x", headers=_bearer())
    assert status == 429


def test_a_correct_key_is_not_blocked_by_another_clients_failures(make_bridge):
    bridge = make_bridge(KEY, clock=_Clock())
    wrong = _bearer("w" * 32)
    # The attacker is a different local address than the good client.
    for _ in range(6):
        conn = http.client.HTTPConnection(
            "127.0.0.1", bridge.port, timeout=5, source_address=("127.0.0.2", 0)
        )
        try:
            conn.request("GET", "/v1/paths?path=/x", headers=wrong)
        except OSError:
            pytest.skip("cannot bind a second loopback address here")
        resp = conn.getresponse()
        resp.read()
        conn.close()
    assert resp.status == 429
    status, _, _ = _request(bridge.port, "GET", "/v1/paths?path=/x", headers=_bearer())
    assert status not in (401, 429)


def test_throttle_unit_isolates_addresses_and_bounds_memory(bridge_handlers):
    clock = _Clock()
    throttle = bridge_handlers._FailureThrottle(limit=3, window=10, clock=clock, max_entries=8)
    assert [throttle.record_failure("a") for _ in range(3)] == [False, False, True]
    assert throttle.retry_after("a") == 10
    assert throttle.retry_after("b") == 0
    for i in range(100):
        throttle.record_failure(f"10.0.0.{i}")
    assert len(throttle._entries) <= 8
    clock.now += 11
    assert throttle.retry_after("a") == 0


def test_the_key_is_never_logged_on_failure(make_bridge, caplog):
    bridge = make_bridge(KEY, clock=_Clock())
    caplog.set_level("DEBUG")
    for _ in range(6):
        _request(
            bridge.port, "GET", "/v1/paths?path=/x", headers=_bearer("secret-guess-" + "z" * 20)
        )
    assert "secret-guess" not in caplog.text
    assert KEY not in caplog.text


# ── small hardenings ──────────────────────────────────────────────────────────


def test_server_header_names_the_plugin_and_nothing_else(make_bridge, server_module, tmp_path):
    bridge = make_bridge("")
    for path in ("/v1/health", "/nowhere"):
        assert _request(bridge.port, "GET", path)[2]["Server"] == f"BinderyBridge/{PLUGIN_VERSION}"
    srv = server_module.BridgeServer()
    port = free_port()
    srv.start_degraded(port, "127.0.0.1", "why")
    try:
        assert _request(port, "GET", "/v1/health")[2]["Server"] == f"BinderyBridge/{PLUGIN_VERSION}"
    finally:
        srv.stop()


def test_deeply_nested_json_gets_a_400_not_a_dropped_connection(make_bridge):
    bridge = make_bridge(KEY)
    for body in (b"[" * 200000, b'{"a":' * 100000):
        status, payload, _ = _request(
            bridge.port,
            "POST",
            "/v1/books",
            body=body,
            headers={**_bearer(), "Content-Type": "application/json"},
        )
        assert status == 400
        assert payload["code"] == "invalid_json"
    status, payload, _ = _request(
        bridge.port,
        "PATCH",
        "/v1/books/1",
        body=b"[" * 200000,
        headers={**_bearer(), "Content-Type": "application/json"},
    )
    assert status == 400


# ── slow clients cannot hold the connection slots ─────────────────────────────


def _read_reply(sock, limit=5.0):
    sock.settimeout(limit)
    reply = b""
    try:
        while chunk := sock.recv(65536):
            reply += chunk
    except (TimeoutError, OSError):
        pass
    return reply


def test_header_trickle_cannot_hold_every_slot(server_module, tmp_path):
    """One byte at a time resets a per recv timeout for ever; the total deadline cuts it."""
    cap = 6
    timeout = 1.0
    srv = server_module.BridgeServer(max_connections=cap)
    port = free_port()
    srv.start(port, "127.0.0.1", "", lambda: _db(tmp_path), header_timeout=timeout)
    crawlers = [socket.create_connection(("127.0.0.1", port)) for _ in range(cap)]
    try:
        assert _wait_for(lambda: _handler_threads() == cap)
        started = time.monotonic()
        request = b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Slow: " + b"a" * 4000
        status = None
        sent = 0
        while time.monotonic() - started < timeout + 3:
            for c in crawlers:
                with contextlib.suppress(OSError):
                    c.sendall(request[sent : sent + 1])
            sent += 1
            time.sleep(0.2)
            if time.monotonic() - started > timeout + 0.5:
                status = _request(port, "GET", "/v1/health", timeout=2)[0]
                if status == 200:
                    break
        # Served while the crawlers are still sending, not after they leave.
        assert status == 200
        assert time.monotonic() - started < timeout + 2.5
        assert sent < len(request)
    finally:
        for c in crawlers:
            c.close()
        srv.stop()


def test_body_trickle_is_cut_off_at_the_deadline(server_module, tmp_path):
    """A buffered 64 KiB read used to wait for the whole chunk, one byte per recv."""
    body_timeout = 1.0
    srv = server_module.BridgeServer()
    port = free_port()
    srv.start(
        port, "127.0.0.1", KEY, lambda: _db(tmp_path), header_timeout=5, body_timeout=body_timeout
    )
    try:
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(
            (
                "POST /v1/books HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Authorization: Bearer {KEY}\r\nContent-Type: application/json\r\n"
                "Content-Length: 65536\r\n\r\n"
            ).encode()
        )
        started = time.monotonic()
        reply = b""
        while time.monotonic() - started < 10:
            s.sendall(b" ")
            readable, _, _ = select.select([s], [], [], 0.2)
            if readable:
                reply = s.recv(65536)
                break
        elapsed = time.monotonic() - started
        s.close()
        assert reply.startswith(b"HTTP/1.0 408"), reply[:60]
        # Hard total: body_timeout plus the rate allowance for 64 KiB, with slack.
        assert body_timeout <= elapsed < body_timeout + 1.0
    finally:
        srv.stop()


def test_a_slow_request_on_one_connection_does_not_outlive_its_handler(server_module, tmp_path):
    """Nothing is left running per connection once the deadline has fired."""
    srv = server_module.BridgeServer()
    port = free_port()
    srv.start(port, "127.0.0.1", "", lambda: _db(tmp_path), header_timeout=0.5)
    try:
        before = threading.active_count()
        for _ in range(5):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"GET /v1/")
            assert _read_reply(s, 3) == b""
            s.close()
        assert _wait_for(lambda: threading.active_count() <= before)
        status, _, _ = _request(port, "GET", "/v1/health")
        assert status == 200
    finally:
        srv.stop()


def _raw_exchange(port, data):
    s = socket.create_connection(("127.0.0.1", port))
    try:
        s.sendall(data)
        return _read_reply(s)
    finally:
        s.close()


def test_oversized_headers_are_refused_with_431(make_bridge, bridge_handlers):
    bridge = make_bridge("")
    big = bridge_handlers.MAX_HEAD_BYTES
    # Many ordinary lines past the byte cap.
    lines = b"".join(b"X-%d: %s\r\n" % (i, b"v" * 200) for i in range(big // 100))
    reply = _raw_exchange(
        bridge.port, b"GET /v1/health HTTP/1.0\r\nHost: 127.0.0.1\r\n" + lines + b"\r\n"
    )
    assert reply.startswith(b"HTTP/1.0 431"), reply[:40]
    # A request line past the cap.
    reply = _raw_exchange(
        bridge.port, b"GET /" + b"a" * (big + 10) + b" HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
    )
    assert reply.startswith(b"HTTP/1.0 431"), reply[:40]
    # More header lines than the count cap, each small.
    many = b"".join(b"X-%d: v\r\n" % i for i in range(bridge_handlers.MAX_HEADERS + 5))
    reply = _raw_exchange(
        bridge.port, b"GET /v1/health HTTP/1.0\r\nHost: 127.0.0.1\r\n" + many + b"\r\n"
    )
    assert reply.startswith(b"HTTP/1.0 431"), reply[:40]
    # The server is fine afterwards, and a normal request still works.
    assert _request(bridge.port, "GET", "/v1/health")[0] == 200


def test_a_body_larger_than_the_header_cap_is_not_counted_against_it(make_bridge, bridge_handlers):
    bridge = make_bridge(KEY)
    body = json.dumps(
        {"path": "/nowhere/x.epub", "pad": "x" * (bridge_handlers.MAX_HEAD_BYTES * 2)}
    )
    status, payload, _ = _request(
        bridge.port,
        "POST",
        "/v1/books",
        body=body,
        headers={**_bearer(), "Content-Type": "application/json"},
    )
    assert status == 400
    assert payload["code"] == "path_not_found"


@pytest.mark.parametrize("key", ["", KEY])
def test_a_second_host_header_is_refused(make_bridge, key):
    bridge = make_bridge(key)
    extra = f"Authorization: Bearer {key}\r\n" if key else ""
    reply = _raw_exchange(
        bridge.port,
        f"GET /v1/health HTTP/1.0\r\nHost: 127.0.0.1\r\nHost: evil.example\r\n{extra}\r\n".encode(),
    )
    assert reply.startswith(b"HTTP/1.0 400"), reply[:40]


@pytest.mark.parametrize("host", ["127.0.0.1:80@evil.com", "localhost:x@evil.com", "a b"])
def test_keyless_with_a_malformed_host_is_refused(make_bridge, host):
    bridge = make_bridge("")
    reply = _raw_exchange(bridge.port, f"GET /v1/health HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
    assert reply.startswith(b"HTTP/1.0 403"), reply[:40]


@pytest.mark.parametrize("key", ["", KEY])
def test_patch_requires_a_json_content_type(make_bridge, key):
    bridge = make_bridge(key)
    base = _bearer(key) if key else {}
    for content_type in ("text/plain", "application/x-www-form-urlencoded", None):
        headers = dict(base)
        if content_type:
            headers["Content-Type"] = content_type
        status, payload, _ = _request(
            bridge.port, "PATCH", "/v1/books/1", body="{}", headers=headers
        )
        assert status == 415, content_type
        assert payload["code"] == "unsupported_media_type"
    status, _, _ = _request(
        bridge.port,
        "PATCH",
        "/v1/books/1",
        body="[]",
        headers={**base, "Content-Type": "application/json"},
    )
    assert status == 400


# ── throttle table limits ─────────────────────────────────────────────────────


def _trip(throttle, addr):
    for _ in range(3):
        throttle.record_failure(addr)
    assert throttle.retry_after(addr) > 0


def test_throttle_keys_an_ipv6_peer_by_its_64(bridge_handlers):
    clock = _Clock()
    throttle = bridge_handlers._FailureThrottle(limit=3, window=10, clock=clock, max_entries=8)
    for i in range(3):
        throttle.record_failure(f"2001:db8:1:2::{i:x}")
    assert throttle.retry_after("2001:db8:1:2:ffff:ffff:ffff:ffff") > 0
    assert throttle.retry_after("2001:db8:1:3::1") == 0
    assert len(throttle._entries) == 1
    # An IPv4 mapped address is the IPv4 address, and a zone id is ignored.
    throttle.record_failure("::ffff:10.0.0.9")
    assert throttle.retry_after("10.0.0.9") == 0
    _trip(throttle, "10.0.0.9")
    assert throttle.retry_after("::ffff:10.0.0.9") > 0
    assert throttle.retry_after("fe80::1%eth0") == throttle.retry_after("fe80::2")


def test_a_flood_of_other_addresses_does_not_release_a_block(bridge_handlers):
    clock = _Clock()
    throttle = bridge_handlers._FailureThrottle(limit=3, window=10, clock=clock, max_entries=8)
    _trip(throttle, "10.9.9.9")
    for i in range(200):
        clock.now += 0.01
        throttle.record_failure(f"10.0.0.{i}")
    assert throttle.retry_after("10.9.9.9") > 0
    assert len(throttle._entries) <= 8


def test_when_every_entry_is_blocked_a_new_address_is_not_tracked(bridge_handlers):
    clock = _Clock()
    throttle = bridge_handlers._FailureThrottle(limit=3, window=10, clock=clock, max_entries=4)
    for i in range(4):
        _trip(throttle, f"10.0.0.{i}")
    assert throttle.record_failure("10.1.1.1") is False
    assert throttle.retry_after("10.1.1.1") == 0
    assert len(throttle._entries) == 4
    assert all(throttle.retry_after(f"10.0.0.{i}") > 0 for i in range(4))
    # Once the blocks have expired the table makes room again.
    clock.now += 11
    throttle.record_failure("10.1.1.1")
    assert "10.1.1.1" in throttle._entries
