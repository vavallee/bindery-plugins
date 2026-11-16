"""KavitaClient against the fake server: routes, both auth modes, failures."""

from __future__ import annotations

import socket

import pytest
from conftest import free_port
from fake_kavita import AUTH_KEY, FakeKavita
from kavita_bridge.kavita import KavitaAuthError, KavitaClient, KavitaError


def client(kavita: FakeKavita, mode: str = "api-key", key: str = AUTH_KEY) -> KavitaClient:
    return KavitaClient(kavita.url, key, mode, timeout=2)


class TestApiKeyMode:
    def test_key_travels_as_x_api_key_header_not_in_the_url(self, kavita):
        client(kavita).scan_all()
        req = kavita.requests[-1]
        assert (req["method"], req["path"]) == ("POST", "/api/Library/scan-all")
        assert req["headers"]["x-api-key"] == AUTH_KEY
        assert req["query"] == {"force": "false"}
        assert not [r for r in kavita.requests if r["path"].endswith("authenticate")]

    def test_scan_one_library_route(self, kavita):
        client(kavita).scan_library(7, force=True)
        req = kavita.requests[-1]
        assert (req["method"], req["path"]) == ("POST", "/api/Library/scan")
        assert req["query"] == {"libraryId": "7", "force": "true"}

    def test_401_is_not_retried(self, kavita):
        with pytest.raises(KavitaAuthError):
            client(kavita, key="wrongwrongwrong").scan_all()
        assert len(kavita.requests) == 1

    def test_libraries_are_parsed(self, kavita):
        libs = client(kavita).libraries()
        assert [(lib.id, lib.folders) for lib in libs] == [
            (1, ("/data/books",)),
            (2, ("/data/comics", "/data/manga")),
        ]

    def test_garbage_library_list_is_an_error(self, kavita):
        kavita.libraries = {"not": "a list"}  # type: ignore[assignment]
        with pytest.raises(KavitaError):
            client(kavita).libraries()

    def test_malformed_library_entries_are_skipped(self, kavita):
        kavita.libraries = [{"id": "x"}, "junk", {"id": 3, "name": None, "folders": "no"}]
        assert [lib.id for lib in client(kavita).libraries()] == [3]


class TestJwtMode:
    def test_signs_in_once_and_reuses_the_token(self, kavita):
        c = client(kavita, "jwt")
        c.scan_all()
        c.scan_all()
        auths = [r for r in kavita.requests if r["path"] == "/api/Plugin/authenticate"]
        assert len(auths) == 1
        assert auths[0]["query"]["apiKey"] == AUTH_KEY
        assert kavita.requests[-1]["headers"]["authorization"] == f"Bearer {kavita.issued[0]}"
        assert "x-api-key" not in kavita.requests[-1]["headers"]

    def test_401_triggers_one_new_sign_in_then_succeeds(self, kavita):
        c = client(kavita, "jwt")
        c.scan_all()
        kavita.revoke_tokens()  # the JWT expired (Kavita issues them for 3 days)
        c.scan_all()
        assert len(kavita.issued) == 2
        # first call, the rejected call, and the retry carrying the new token
        assert len(kavita.scans()) == 3
        assert kavita.requests[-1]["headers"]["authorization"] == f"Bearer {kavita.issued[1]}"

    def test_a_second_401_gives_up(self, kavita):
        c = client(kavita, "jwt")
        c.scan_all()
        kavita.issued.clear()
        kavita.revoke_tokens()
        # Kavita now rejects every token it issues.
        original = kavita.route

        def reject(method, path, query, headers):
            if path.lower() == "/api/plugin/authenticate":
                status, body = original(method, path, query, headers)
                kavita.valid_tokens.clear()
                return status, body
            return original(method, path, query, headers)

        kavita.route = reject  # type: ignore[method-assign]
        before = len(kavita.requests)
        with pytest.raises(KavitaAuthError):
            c.scan_all()
        # old token call, one sign in, one retry, then nothing more
        assert len(kavita.requests) - before == 3

    def test_bad_key_fails_sign_in_without_retry_storm(self, kavita):
        with pytest.raises(KavitaAuthError):
            client(kavita, "jwt", key="wrongwrongwrong").scan_all()
        assert len(kavita.requests) == 1

    def test_sign_in_without_a_token_is_an_error(self, kavita):
        original = kavita.route
        kavita.route = lambda m, p, q, h: (200, {"no": "token"})  # type: ignore[method-assign]
        with pytest.raises(KavitaError):
            client(kavita, "jwt").scan_all()
        kavita.route = original  # type: ignore[method-assign]


class TestFailures:
    def test_connection_refused(self):
        c = KavitaClient(f"http://127.0.0.1:{free_port()}", AUTH_KEY, timeout=1)
        with pytest.raises(KavitaError) as info:
            c.scan_all()
        assert info.value.status is None
        assert AUTH_KEY not in str(info.value)

    def test_timeout(self, kavita):
        kavita.hang = True
        c = KavitaClient(kavita.url, AUTH_KEY, timeout=0.5)
        with pytest.raises(KavitaError):
            c.scan_all()

    def test_server_error(self, kavita):
        kavita.fail_scans_with = 503
        with pytest.raises(KavitaError) as info:
            client(kavita).scan_all()
        assert info.value.status == 503

    def test_a_redirect_is_an_error_not_a_silent_get(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        import threading

        def serve() -> None:
            conn, _ = srv.accept()
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: http://example.invalid/\r\nContent-Length: 0\r\n\r\n"
            )
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        with pytest.raises(KavitaError) as info:
            KavitaClient(f"http://127.0.0.1:{port}", AUTH_KEY, timeout=2).scan_all()
        assert info.value.status == 302
        srv.close()

    def test_garbage_response_is_an_error(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        import threading

        def serve() -> None:
            conn, _ = srv.accept()
            conn.recv(4096)
            conn.sendall(b"not http at all")
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        with pytest.raises(KavitaError):
            KavitaClient(f"http://127.0.0.1:{port}", AUTH_KEY, timeout=2).scan_all()
        srv.close()


class TestCheck:
    def test_ok(self, kavita):
        h = client(kavita).check()
        assert (h.reachable, h.key_valid, h.admin) == (True, True, True)

    def test_bad_key(self, kavita):
        h = client(kavita, key="wrongwrongwrong").check()
        assert (h.reachable, h.key_valid, h.admin) == (True, False, False)

    def test_not_admin(self, kavita):
        kavita.admin = False
        h = client(kavita).check()
        assert (h.reachable, h.key_valid, h.admin) == (True, True, False)

    def test_down(self):
        h = KavitaClient(f"http://127.0.0.1:{free_port()}", AUTH_KEY, timeout=1).check()
        assert (h.reachable, h.key_valid, h.admin) == (False, False, False)

    def test_server_error_is_reachable_but_not_confirmed(self, kavita):
        kavita.route = lambda *a: (500, None)  # type: ignore[method-assign]
        h = client(kavita).check()
        assert (h.reachable, h.key_valid) == (True, False)
