"""JSON response builders, error helpers, and Bearer-auth check for plugin HTTP servers."""

from __future__ import annotations

import json
from typing import Any


def json_response(status: int, payload: dict[str, Any]) -> tuple[int, bytes]:
    """Return (status, body_bytes) for a JSON response."""
    return status, json.dumps(payload).encode("utf-8")


def ok(payload: dict[str, Any]) -> tuple[int, bytes]:
    return json_response(200, payload)


def created(payload: dict[str, Any]) -> tuple[int, bytes]:
    return json_response(201, payload)


def conflict(payload: dict[str, Any]) -> tuple[int, bytes]:
    return json_response(409, payload)


def bad_request(message: str) -> tuple[int, bytes]:
    return json_response(400, {"error": message})


def unauthorized() -> tuple[int, bytes]:
    return json_response(401, {"error": "unauthorized"})


def not_found() -> tuple[int, bytes]:
    return json_response(404, {"error": "not found"})


def service_unavailable(message: str) -> tuple[int, bytes]:
    return json_response(503, {"error": message})


def internal_error(message: str) -> tuple[int, bytes]:
    return json_response(500, {"error": message})


def check_bearer(headers: Any, api_key: str) -> bool:
    """Return True if the request carries the correct Bearer token.

    If *api_key* is empty, all requests are permitted (auth disabled).
    """
    if not api_key:
        return True
    auth_header: str = headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    return auth_header[len("Bearer "):].strip() == api_key
