from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fake_kavita import AUTH_KEY, FakeKavita
from kavita_bridge.config import Config
from kavita_bridge.kavita import KavitaClient
from kavita_bridge.receiver import Receiver
from kavita_bridge.service import ScanService

BRIDGE_KEY = "bridge-secret-key-0123456789abcdef"


@pytest.fixture
def kavita() -> Iterator[FakeKavita]:
    fake = FakeKavita().start()
    yield fake
    fake.stop()


def make_config(kavita_url: str, **overrides: Any) -> Config:
    base: dict[str, Any] = {
        "bind_host": "127.0.0.1",
        "port": 0,
        "api_key": BRIDGE_KEY,
        "kavita_url": kavita_url,
        "kavita_api_key": AUTH_KEY,
        "debounce_seconds": 0.2,
        "kavita_timeout": 2.0,
    }
    base.update(overrides)
    return Config(**base)


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def start_bridge(kavita: FakeKavita) -> Iterator[Callable[..., Receiver]]:
    """Start a receiver against the fake Kavita. `start_bridge(**config_overrides)`."""
    started: list[tuple[Receiver, ScanService]] = []

    def start(**overrides: Any) -> Receiver:
        config = make_config(kavita.url, **overrides)
        client = KavitaClient(
            config.kavita_url, config.kavita_api_key, config.auth_mode, config.kavita_timeout
        )
        service = ScanService(config, client)
        server = Receiver(config, client, service)
        threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
        ).start()
        started.append((server, service))
        return server

    yield start
    for server, service in started:
        server.shutdown()
        service.stop()
        server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def call(
    server: Receiver,
    method: str = "POST",
    path: str = "/v1/events",
    body: Any = None,
    token: str | None = BRIDGE_KEY,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    """One request to the receiver. Returns (status, parsed JSON body)."""
    import http.client
    import json

    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(text)
    except ValueError:
        return resp.status, {}


def imported(path: str | None = None, **extra: Any) -> dict[str, Any]:
    """A bookImported payload shaped like Bindery's normalizeEventPayload output."""
    payload: dict[str, Any] = {
        "eventType": "bookImported",
        "title": "Book Imported",
        "message": "Some Book (ebook)",
        "body": "Some Book (ebook)",
        "item": "Some Book",
        "format": "ebook",
        "mediaFormat": "ebook",
    }
    if path is not None:
        payload["path"] = path
    payload.update(extra)
    return payload
