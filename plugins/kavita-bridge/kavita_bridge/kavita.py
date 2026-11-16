"""Everything the bridge knows about Kavita, behind one small class.

Routes, verbs and auth are documented in plugins/kavita-bridge/README.md with
the Kavita source file and line each was read from. Tests replace this class
or point it at a fake HTTP server.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import __version__

log = logging.getLogger("kavita_bridge")

PLUGIN_NAME = "bindery-kavita-bridge"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class KavitaError(Exception):
    """A Kavita call failed. `status` is the HTTP status, or None for no response.

    The message is safe to log: it never holds a URL, a key or a token.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class KavitaAuthError(KavitaError):
    """Kavita refused the credentials (401), or the account may not do this (403)."""


@dataclass(frozen=True)
class Library:
    id: int
    name: str
    folders: tuple[str, ...]


@dataclass(frozen=True)
class Health:
    reachable: bool
    key_valid: bool
    admin: bool


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect on a POST would silently become a GET and lose the scan."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class KavitaClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        auth_mode: str = "api-key",
        timeout: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._mode = auth_mode
        self._timeout = timeout
        self._token: str | None = None
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- transport -----------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(  # noqa: S310 - scheme is checked in config.load
            url,
            method=method,
            data=b"" if method == "POST" else None,
            headers={"User-Agent": f"bindery-kavita-bridge/{__version__}", **(headers or {})},
        )
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:  # nosec B310
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # str(exc) is "HTTP Error 401: Unauthorized": no URL, no key.
            exc.close()
            if exc.code in (401, 403):
                raise KavitaAuthError(f"Kavita answered HTTP {exc.code}", exc.code) from None
            raise KavitaError(f"Kavita answered HTTP {exc.code}", exc.code) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            # URLError text can name the host but never the query. Keep only the class.
            raise KavitaError(f"cannot reach Kavita ({type(exc).__name__})") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise KavitaError("Kavita response too large")
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _authenticate(self) -> str:
        """Exchange the auth key for a JWT. The key travels as a query parameter
        because that is where Kavita's action reads it; it is never logged."""
        body = self._send(
            "POST",
            "/api/Plugin/authenticate",
            query={"apiKey": self._key, "pluginName": PLUGIN_NAME},
        )
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise KavitaError("Kavita sign in returned no token")
        self._token = token
        return token

    def _auth_headers(self) -> dict[str, str]:
        if self._mode == "jwt":
            return {"Authorization": f"Bearer {self._token or self._authenticate()}"}
        return {"x-api-key": self._key}

    def _call(self, method: str, path: str, query: dict[str, str] | None = None) -> Any:
        """One authenticated call. In jwt mode a 401 gets exactly one new sign in."""
        headers = self._auth_headers()
        try:
            return self._send(method, path, query, headers)
        except KavitaAuthError as exc:
            if self._mode != "jwt" or exc.status != 401:
                raise
        log.info("Kavita rejected the token, signing in again")
        self._token = None
        return self._send(method, path, query, self._auth_headers())

    # -- operations ----------------------------------------------------------

    def libraries(self) -> list[Library]:
        body = self._call("GET", "/api/Library/libraries")
        if not isinstance(body, list):
            raise KavitaError("Kavita returned an unexpected library list")
        found: list[Library] = []
        for item in body:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            folders = item.get("folders")
            found.append(
                Library(
                    id=item["id"],
                    name=str(item.get("name") or ""),
                    folders=tuple(f for f in folders if isinstance(f, str))
                    if isinstance(folders, list)
                    else (),
                )
            )
        return found

    def scan_library(self, library_id: int, force: bool = False) -> None:
        self._call(
            "POST",
            "/api/Library/scan",
            {"libraryId": str(library_id), "force": "true" if force else "false"},
        )

    def scan_all(self, force: bool = False) -> None:
        self._call("POST", "/api/Library/scan-all", {"force": "true" if force else "false"})

    def check(self) -> Health:
        """Is Kavita up, is the key accepted, and may it scan? Never raises."""
        try:
            # Admin only and read only. With no path it returns the server's root
            # directories (drive names), which this code discards. A 200 proves an
            # admin key. /api/Library/libraries would not: any signed in user may
            # call it, so it cannot show scan rights.
            self._call("GET", "/api/Library/list")
        except KavitaAuthError as exc:
            return Health(True, exc.status == 403, False)
        except KavitaError as exc:
            return Health(exc.status is not None, False, False)
        return Health(True, True, True)
