"""Startup refusal rules, parsing, and the command line entry point."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import free_port
from kavita_bridge import __main__ as entry
from kavita_bridge.config import Config, ConfigError, PathMapping, is_loopback, load

ENV = {
    "KAVITA_BRIDGE_KAVITA_URL": "http://kavita:5000/",
    "KAVITA_BRIDGE_KAVITA_API_KEY": "kavitakeykavitakey",
}
GOOD_KEY = "abcd" * 8


def env(**extra: str) -> dict[str, str]:
    return {**ENV, **extra}


class TestStartupRefusal:
    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "bridge.lan", "::", ""])  # noqa: S104
    def test_network_bind_without_a_key_is_refused(self, host):
        with pytest.raises(ConfigError, match="not loopback and no api_key"):
            load(env(KAVITA_BRIDGE_BIND_HOST=host or "0.0.0.0"))  # noqa: S104

    @pytest.mark.parametrize("key", ["a", "x" * 15])
    def test_network_bind_with_a_short_key_is_refused(self, key):
        with pytest.raises(ConfigError, match="shorter than 16 characters"):
            load(env(KAVITA_BRIDGE_BIND_HOST="0.0.0.0", KAVITA_BRIDGE_API_KEY=key))  # noqa: S104

    def test_sixteen_characters_is_enough(self):
        cfg = load(env(KAVITA_BRIDGE_BIND_HOST="0.0.0.0", KAVITA_BRIDGE_API_KEY="x" * 16))  # noqa: S104
        assert cfg.bind_host == "0.0.0.0"  # noqa: S104

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.9.9.9"])
    def test_loopback_may_run_without_a_key_or_with_a_short_one(self, host):
        assert load(env(KAVITA_BRIDGE_BIND_HOST=host)).api_key == ""
        assert load(env(KAVITA_BRIDGE_BIND_HOST=host, KAVITA_BRIDGE_API_KEY="short")).api_key

    def test_default_bind_is_loopback(self):
        assert load(env()).bind_host == "127.0.0.1"

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST"])
    def test_is_loopback_true(self, host):
        assert is_loopback(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.1", "example.com", "::"])  # noqa: S104
    def test_is_loopback_false(self, host):
        assert not is_loopback(host)


class TestParsing:
    def test_kavita_settings_are_required(self):
        with pytest.raises(ConfigError, match="kavita_url"):
            load({})
        with pytest.raises(ConfigError, match="kavita_api_key"):
            load({"KAVITA_BRIDGE_KAVITA_URL": "http://k"})

    def test_defaults(self):
        cfg = load(env())
        assert cfg.kavita_url == "http://kavita:5000"
        assert cfg.debounce_seconds == 30
        assert cfg.scan_mode == "all"
        assert cfg.unmapped == "skip"
        assert cfg.auth_mode == "api-key"
        assert cfg.kavita_timeout == 10

    def test_path_map_env_form(self):
        cfg = load(env(KAVITA_BRIDGE_PATH_MAP="/media=/data; /audio=/data/audio;"))
        assert cfg.path_map == (
            PathMapping("/media", "/data"),
            PathMapping("/audio", "/data/audio"),
        )

    @pytest.mark.parametrize("bad", ["nonsense", "=/x", "/x="])
    def test_path_map_rejects_bad_pairs(self, bad):
        with pytest.raises(ConfigError, match="path_map"):
            load(env(KAVITA_BRIDGE_PATH_MAP=bad))

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("SCAN_MODE", "sometimes"),
            ("UNMAPPED", "maybe"),
            ("AUTH_MODE", "basic"),
            ("PORT", "http"),
            ("PORT", "70000"),
            ("DEBOUNCE_SECONDS", "-1"),
            ("KAVITA_TIMEOUT", "0"),
            ("KAVITA_TIMEOUT", "soon"),
            ("UNMAPPED_LIBRARIES", "a,b"),
            ("UNMAPPED_LIBRARIES", "0"),
            ("KAVITA_URL", "ftp://k"),
            ("KAVITA_URL", "http://user:pw@k"),
        ],
    )
    def test_bad_values_are_refused(self, name, value):
        with pytest.raises(ConfigError):
            load(env(**{"KAVITA_BRIDGE_" + name: value}))

    def test_unmapped_libraries_needs_ids(self):
        with pytest.raises(ConfigError, match="unmapped_libraries"):
            load(env(KAVITA_BRIDGE_UNMAPPED="libraries"))
        cfg = load(env(KAVITA_BRIDGE_UNMAPPED="libraries", KAVITA_BRIDGE_UNMAPPED_LIBRARIES="3, 4"))
        assert cfg.unmapped_libraries == (3, 4)

    def test_file_and_env_precedence(self, tmp_path: Path):
        f = tmp_path / "c.json"
        f.write_text(
            json.dumps(
                {
                    "kavita_url": "http://from-file",
                    "kavita_api_key": "FileKey1234567890",
                    "debounce_seconds": 5,
                    "path_map": [{"bindery": "/a", "kavita": "/b"}],
                    "force": True,
                }
            )
        )
        cfg = load({"KAVITA_BRIDGE_DEBOUNCE_SECONDS": "9"}, config_file=str(f))
        assert (cfg.kavita_url, cfg.debounce_seconds, cfg.force) == ("http://from-file", 9, True)
        assert cfg.path_map == (PathMapping("/a", "/b"),)

    def test_secret_from_file(self, tmp_path: Path):
        f = tmp_path / "key"
        f.write_text("FromSecretFile123456\n")
        cfg = load(env(KAVITA_BRIDGE_KAVITA_API_KEY_FILE=str(f)))
        assert cfg.kavita_api_key == "FromSecretFile123456"

    @pytest.mark.parametrize("content", ["{broken", "[1]"])
    def test_bad_config_file(self, tmp_path: Path, content):
        f = tmp_path / "c.json"
        f.write_text(content)
        with pytest.raises(ConfigError):
            load({}, config_file=str(f))

    def test_missing_files(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="config file"):
            load({}, config_file=str(tmp_path / "nope.json"))
        with pytest.raises(ConfigError, match="file"):
            load(env(KAVITA_BRIDGE_API_KEY_FILE=str(tmp_path / "nope")))

    def test_path_map_list_validation(self):
        from kavita_bridge.config import _parse_path_map

        with pytest.raises(ConfigError):
            _parse_path_map([{"bindery": "/a"}])
        with pytest.raises(ConfigError):
            _parse_path_map(5)


class TestSecretsInConfigViews:
    def test_repr_and_redacted_hide_both_keys(self):
        cfg = load(env(KAVITA_BRIDGE_API_KEY=GOOD_KEY))
        shown = repr(cfg) + str(cfg.redacted())
        assert GOOD_KEY not in shown
        assert "kavitakeykavitakey" not in shown
        assert cfg.redacted()["api_key"] == "<redacted>"
        assert Config().redacted()["api_key"] == ""

    def test_config_errors_never_quote_a_key(self):
        with pytest.raises(ConfigError) as info:
            load(env(KAVITA_BRIDGE_BIND_HOST="0.0.0.0", KAVITA_BRIDGE_API_KEY="shortsecret"))  # noqa: S104
        assert "shortsecret" not in str(info.value)


class TestEntryPoint:
    def test_check_config_ok_and_redacted(self, monkeypatch, caplog):
        for k, v in env(KAVITA_BRIDGE_API_KEY=GOOD_KEY).items():
            monkeypatch.setenv(k, v)
        caplog.set_level(logging.INFO, logger="kavita_bridge")
        assert entry.main(["--check-config"]) == 0
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "configuration ok" in text
        assert GOOD_KEY not in text
        assert "kavitakeykavitakey" not in text

    def test_main_refuses_a_bad_bind(self, monkeypatch, caplog):
        for k, v in env(KAVITA_BRIDGE_BIND_HOST="0.0.0.0").items():  # noqa: S104
            monkeypatch.setenv(k, v)

        def started(*args, **kwargs):
            # Without the refusal main() would serve until a signal arrives and
            # hang the suite, so make "got as far as listening" an immediate failure.
            raise AssertionError("the bridge started instead of refusing")

        monkeypatch.setattr(entry, "Receiver", started)
        assert entry.main([]) == 2
        assert "refusing to start" in caplog.text

    def test_a_real_process_refuses_and_exits_nonzero(self):
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "kavita_bridge"],
            cwd=root,
            env={**ENV, "KAVITA_BRIDGE_BIND_HOST": "0.0.0.0", "PATH": ""},  # noqa: S104
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # A refusal exits at once. If the rule is gone the process would
            # serve for ever, so kill it and fail instead of hanging the suite.
            _, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("the process did not refuse to start")
        assert proc.returncode == 2
        assert "refusing to start" in err

    def test_a_real_process_serves_health_and_stops_on_sigterm(self):
        import http.client
        import os
        import signal

        root = Path(__file__).resolve().parents[1]
        port = free_port()
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "kavita_bridge"],
            cwd=root,
            env={
                **ENV,
                "KAVITA_BRIDGE_PORT": str(port),
                "KAVITA_BRIDGE_API_KEY": GOOD_KEY,
                "PATH": os.environ.get("PATH", ""),
            },
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(100):
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    conn.request("GET", "/v1/health")
                    assert conn.getresponse().status == 200
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("the process never listened")
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=15) == 0
            err = proc.stderr.read() if proc.stderr else ""
            assert GOOD_KEY not in err
            assert "kavitakeykavitakey" not in err
        finally:
            if proc.poll() is None:
                proc.kill()
            if proc.stderr:
                proc.stderr.close()

    def test_port_in_use(self, monkeypatch, caplog):
        import socket

        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            for k, v in env(KAVITA_BRIDGE_PORT=str(taken.getsockname()[1])).items():
                monkeypatch.setenv(k, v)
            assert entry.main([]) == 1
        assert "cannot listen" in caplog.text
