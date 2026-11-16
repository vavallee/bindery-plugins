"""The receiver: auth, event filtering, answers, health, and what reaches the log."""

from __future__ import annotations

import logging

import pytest
from conftest import BRIDGE_KEY, call, imported, wait_for
from fake_kavita import AUTH_KEY
from kavita_bridge.receiver import RedactFilter, token_matches


class TestBearer:
    def test_good_token_is_accepted(self, start_bridge, kavita):
        server = start_bridge()
        status, body = call(server, body=imported())
        assert status == 202
        assert body == {"status": "queued"}

    def test_missing_token_is_refused(self, start_bridge, kavita):
        server = start_bridge()
        status, body = call(server, body=imported(), token=None)
        assert status == 401
        assert body["code"] == "unauthorized"
        assert kavita.scans() == []

    @pytest.mark.parametrize(
        "token", ["wrong", BRIDGE_KEY[:-1], BRIDGE_KEY + "x", "", "Bearer " + BRIDGE_KEY]
    )
    def test_wrong_token_is_refused_and_nothing_is_scanned(self, start_bridge, kavita, token):
        server = start_bridge(debounce_seconds=0)
        status, _ = call(server, body=imported(), token=token)
        assert status == 401
        assert not wait_for(lambda: kavita.scans(), timeout=0.5)

    def test_other_scheme_is_refused(self, start_bridge):
        server = start_bridge()
        status, _ = call(
            server, body=imported(), token=None, headers={"Authorization": f"Basic {BRIDGE_KEY}"}
        )
        assert status == 401

    def test_no_key_on_loopback_accepts_unauthenticated(self, start_bridge):
        server = start_bridge(api_key="")
        status, _ = call(server, body=imported(), token=None)
        assert status == 202

    def test_comparison_is_constant_time_primitive(self, monkeypatch):
        import hmac

        calls = []
        real = hmac.compare_digest
        monkeypatch.setattr(
            "kavita_bridge.receiver.hmac.compare_digest",
            lambda a, b: calls.append((a, b)) or real(a, b),
        )
        assert token_matches("abc", "abc")
        assert not token_matches("abc", "abd")
        assert not token_matches("é", "e")  # non ASCII must not raise
        assert len(calls) == 3


class TestEvents:
    @pytest.mark.parametrize(
        "event", ["grabbed", "downloadFailed", "health", "upgrade", "bookAnnounced", "test", None]
    )
    def test_other_events_are_acknowledged_and_ignored(self, start_bridge, kavita, event):
        server = start_bridge(debounce_seconds=0)
        payload = {"title": "x"} if event is None else {"eventType": event, "title": "x"}
        status, body = call(server, body=payload)
        assert status == 200
        assert body == {"status": "ignored"}
        assert not wait_for(lambda: kavita.requests, timeout=0.4)

    def test_put_is_accepted_like_post(self, start_bridge):
        server = start_bridge()
        assert call(server, method="PUT", body=imported())[0] == 202

    def test_bad_json_and_shapes(self, start_bridge):
        server = start_bridge()
        assert call(server, raw=b"{nope")[0] == 400
        assert call(server, body=["list"])[0] == 400
        assert call(server, raw=b"")[0] == 400

    def test_content_type_is_required(self, start_bridge):
        server = start_bridge()
        status, body = call(server, body=imported(), headers={"Content-Type": "text/plain"})
        assert status == 415
        assert body["code"] == "unsupported_media_type"

    def test_oversized_body_is_refused(self, start_bridge):
        server = start_bridge()
        # Declared, not sent: the receiver must refuse before it reads a byte.
        status, _ = call(server, raw=b"{}", headers={"Content-Length": str(300 * 1024)})
        assert status == 413

    def test_browser_requests_are_refused(self, start_bridge):
        server = start_bridge()
        status, _ = call(server, body=imported(), headers={"Origin": "https://evil.example"})
        assert status == 403

    def test_unknown_path(self, start_bridge):
        server = start_bridge()
        assert call(server, path="/v1/nope", body={})[0] == 404
        assert call(server, method="GET", path="/v1/nope")[0] == 404

    def test_a_missing_or_blank_path_is_not_an_error(self, start_bridge):
        server = start_bridge()
        assert call(server, body=imported(path="  "))[0] == 202
        assert call(server, body=imported(path=123))[0] == 202


class TestKavitaFailuresNeverReachBindery:
    def test_kavita_down_answers_202_and_keeps_serving(self, start_bridge, kavita):
        kavita.stop()
        server = start_bridge(debounce_seconds=0)
        assert call(server, body=imported())[0] == 202
        assert call(server, body=imported())[0] == 202
        status, body = call(server, method="GET", path="/v1/health")
        assert status == 200
        assert body["kavita"]["reachable"] is False

    def test_kavita_500_on_scan_is_not_a_5xx_for_bindery(self, start_bridge, kavita):
        kavita.fail_scans_with = 500
        server = start_bridge(debounce_seconds=0)
        assert call(server, body=imported())[0] == 202
        assert wait_for(lambda: kavita.scans())
        assert call(server, body=imported())[0] == 202

    def test_a_hung_kavita_does_not_hold_the_response(self, start_bridge, kavita):
        kavita.hang = True
        server = start_bridge(debounce_seconds=0, kavita_timeout=1)
        import time

        started = time.monotonic()
        assert call(server, body=imported())[0] == 202
        assert time.monotonic() - started < 1.0


class TestHealth:
    def test_authenticated_health_reports_kavita(self, start_bridge):
        server = start_bridge()
        status, body = call(server, method="GET", path="/v1/health")
        assert status == 200
        assert body["status"] == "ok"
        assert body["kavita"] == {"reachable": True, "keyValid": True, "canScan": True}

    def test_unauthenticated_health_says_nothing_about_kavita(self, start_bridge, kavita):
        server = start_bridge()
        status, body = call(server, method="GET", path="/v1/health", token=None)
        assert status == 200
        assert set(body) == {"status", "version"}
        assert kavita.requests == []  # no outbound call on behalf of a stranger

    def test_health_names_no_secret_and_no_address(self, start_bridge, kavita):
        server = start_bridge()
        _, body = call(server, method="GET", path="/v1/health")
        text = str(body)
        for needle in (AUTH_KEY, BRIDGE_KEY, kavita.url, "127.0.0.1"):
            assert needle not in text

    def test_wrong_kavita_key_is_reported_not_leaked(self, start_bridge):
        server = start_bridge(kavita_api_key="wrongwrongwrongwrong")
        _, body = call(server, method="GET", path="/v1/health")
        assert body["kavita"] == {"reachable": True, "keyValid": False, "canScan": False}

    def test_non_admin_key_is_reported(self, start_bridge, kavita):
        kavita.admin = False
        server = start_bridge()
        _, body = call(server, method="GET", path="/v1/health")
        assert body["kavita"] == {"reachable": True, "keyValid": True, "canScan": False}

    def test_kavita_check_is_cached(self, start_bridge, kavita):
        server = start_bridge()
        for _ in range(5):
            call(server, method="GET", path="/v1/health")
        assert len([r for r in kavita.requests if r["path"] == "/api/Library/list"]) == 1


class TestSecretsStayOutOfLogs:
    def test_no_secret_in_any_log_line(self, start_bridge, kavita, caplog):
        caplog.set_level(logging.DEBUG, logger="kavita_bridge")
        server = start_bridge(debounce_seconds=0, auth_mode="jwt")
        # A wrong bearer, a good event, a Kavita refusal and a re-sign in.
        call(server, body=imported(), token="not-the-key")
        call(server, body=imported(path="/data/books/a.epub"))
        assert wait_for(lambda: kavita.scans())
        kavita.revoke_tokens()
        call(server, body=imported())
        wait_for(lambda: len(kavita.issued) >= 2)
        kavita.valid_key = "rotated-key"
        kavita.revoke_tokens()
        call(server, body=imported())
        wait_for(lambda: len(kavita.scans()) >= 4)
        call(server, method="GET", path="/v1/health")

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert caplog.records
        for secret in (AUTH_KEY, BRIDGE_KEY, "secretjwt", "not-the-key"):
            assert secret not in logged

    def test_filter_replaces_secrets_that_slip_into_a_message(self):
        record = logging.LogRecord("x", logging.ERROR, "f", 1, "oops %s", ("hunter2hunter2",), None)
        assert RedactFilter(["hunter2hunter2"]).filter(record)
        assert "hunter2" not in record.getMessage()

    def test_log_injection_is_neutralised(self, start_bridge, caplog):
        caplog.set_level(logging.INFO, logger="kavita_bridge")
        server = start_bridge()
        call(server, body=imported(path="/x\nFAKE LOG LINE\r\x1b[31m"))
        assert all("\n" not in r.getMessage() for r in caplog.records)
