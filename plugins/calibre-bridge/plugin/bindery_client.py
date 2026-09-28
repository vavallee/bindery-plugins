"""A small HTTP client for Bindery's pull routes (``/bridge/v1``).

Standard library only, because Calibre bundles its own Python and a plugin
cannot install anything into it.

Rules this client keeps, and why:

* HTTPS is always verified. The context comes from
  ``ssl.create_default_context()``, which on Windows reads the system
  certificate store, plus an optional PEM bundle for a private CA. There is
  deliberately no switch to turn verification off: a self signed Bindery is
  served by pointing ``ca_file`` at its certificate.
* Plain ``http`` is allowed, since a LAN Bindery without TLS is a common and
  legitimate setup, but :func:`http_warning` reports it for any host that is
  not loopback so the config dialog can say that the key and the books cross
  the network unencrypted.
* Redirects are refused. urllib follows them by default and forwards the
  ``Authorization`` header to wherever the redirect points, which would hand
  the API key to another host.
* Downloads stream to disk in chunks and stop at ``max_download_bytes``, both
  on the advertised ``Content-Length`` and on what actually arrives.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_log = logging.getLogger(__name__)

PROTOCOL = 1
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
# An error body is only ever a small JSON object; never read more than this.
MAX_ERROR_BODY = 64 * 1024
MAX_JSON_BODY = 8 * 1024 * 1024

_ALLOWED_SCHEMES = ("http", "https")


class BinderyError(Exception):
    """Bindery answered, but not with what was asked for."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        code: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


class BinderyUnreachable(BinderyError):
    """No HTTP answer at all: DNS, refused connection, TLS failure, timeout."""


class RedirectRefused(BinderyError):
    """Bindery (or something in front of it) answered with a redirect."""


class TooLarge(BinderyError):
    """A download is bigger than ``max_download_bytes``."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an error instead of following it.

    ``HTTPRedirectHandler.redirect_request`` builds the follow up request with
    the original headers, ``Authorization`` included, so following a redirect
    to another host leaks the key. Nothing on the pull routes redirects.
    """

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise RedirectRefused(
            f"Bindery answered {code} redirecting to {newurl!r}; redirects are not "
            "followed, set the Bindery URL to the final address",
            status=code,
            code="redirect_refused",
        )


def validate_base_url(url: str) -> str:
    """Return ``url`` without a trailing slash, or raise ValueError.

    Only ``http`` and ``https`` with a host are accepted, which is also what
    keeps ``urllib`` away from ``file:`` and other schemes (bandit B310).
    """
    cleaned = (url or "").strip()
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(f"Bindery URL must start with http:// or https://, got {url!r}")
    if not parsed.hostname:
        raise ValueError(f"Bindery URL has no host: {url!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"Bindery URL must not carry a query or fragment: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError("Bindery URL must not carry a user name or password")
    return cleaned.rstrip("/")


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_warning(url: str) -> str:
    """A warning for the status line when ``url`` is plain http off this machine."""
    try:
        parsed = urllib.parse.urlsplit(validate_base_url(url))
    except ValueError:
        return ""
    if parsed.scheme.lower() == "http" and not is_loopback_host(parsed.hostname or ""):
        return "Bindery URL is plain http: the API key and every book cross the network unencrypted"
    return ""


def build_ssl_context(ca_file: str = "") -> ssl.SSLContext:
    """The system trust store, plus ``ca_file`` when one is configured.

    ``create_default_context(cafile=...)`` would load only that file, so the
    extra bundle is added to the default one instead of replacing it.
    """
    context = ssl.create_default_context()
    if ca_file and ca_file.strip():
        context.load_verify_locations(cafile=ca_file.strip())
    return context


def _retry_after(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class BinderyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        version: str,
        capabilities: list[str] | tuple[str, ...],
        ca_file: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self._api_key = api_key
        self.timeout = timeout
        self.max_download_bytes = int(max_download_bytes)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Bridge-Version": version,
            "X-Bridge-Capabilities": ",".join(capabilities),
            "User-Agent": f"calibre-bridge/{version}",
        }
        handlers: list[Any] = [_NoRedirect()]
        if urllib.parse.urlsplit(self.base_url).scheme.lower() == "https":
            handlers.append(urllib.request.HTTPSHandler(context=build_ssl_context(ca_file)))
        self._opener = urllib.request.build_opener(*handlers)

    # -- routes --------------------------------------------------------------

    def hello(self) -> dict[str, Any]:
        return self._get_json("/bridge/v1/hello")

    def deliveries(self, limit: int, cursor: str = "") -> dict[str, Any]:
        query: dict[str, str] = {"limit": str(int(limit))}
        if cursor:
            query["cursor"] = cursor
        return self._get_json("/bridge/v1/deliveries?" + urllib.parse.urlencode(query))

    def download_file(self, delivery_id: str, dest: str) -> int:
        """Stream the delivery's book to ``dest``. Returns the byte count."""
        path = f"/bridge/v1/deliveries/{_quote_id(delivery_id)}/file"
        size, _ctype = self._download(path, dest, self.max_download_bytes)
        return size

    def download_cover(self, delivery_id: str, dest: str, limit: int) -> str | None:
        """Stream the cover to ``dest``. Returns its Content-Type, None on 404."""
        path = f"/bridge/v1/deliveries/{_quote_id(delivery_id)}/cover"
        try:
            _size, ctype = self._download(path, dest, min(limit, self.max_download_bytes))
        except BinderyError as exc:
            if exc.status == 404 and not isinstance(exc, BinderyUnreachable):
                return None
            raise
        return ctype

    def ack(self, delivery_id: str, body: dict[str, Any]) -> None:
        self._post_json(f"/bridge/v1/deliveries/{_quote_id(delivery_id)}/ack", body)

    def nack(self, delivery_id: str, body: dict[str, Any]) -> None:
        self._post_json(f"/bridge/v1/deliveries/{_quote_id(delivery_id)}/nack", body)

    # -- plumbing ------------------------------------------------------------

    def _request(
        self, path: str, data: bytes | None = None, method: str = "GET", accept: str = ""
    ) -> Any:
        url = self.base_url + path
        # Checked again here, not only in __init__, so no code path can hand
        # urllib a scheme other than http or https.
        if urllib.parse.urlsplit(url).scheme.lower() not in _ALLOWED_SCHEMES:
            raise ValueError(f"refusing to fetch {url!r}")
        headers = dict(self._headers)
        if accept:
            headers["Accept"] = accept
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
        try:
            # Scheme validated just above, so bandit B310 does not apply.
            return self._opener.open(req, timeout=self.timeout)  # nosec B310
        except RedirectRefused:
            raise
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, RedirectRefused):
                raise reason from exc
            raise BinderyUnreachable(f"cannot reach Bindery at {self.base_url}: {reason}") from exc
        except (OSError, ValueError) as exc:
            raise BinderyUnreachable(f"cannot reach Bindery at {self.base_url}: {exc}") from exc

    def _http_error(self, exc: urllib.error.HTTPError) -> BinderyError:
        message = f"HTTP {exc.code}"
        code = ""
        try:
            raw = exc.read(MAX_ERROR_BODY)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if isinstance(payload, dict):
                code = str(payload.get("code") or "")
                if payload.get("error"):
                    message = f"HTTP {exc.code}: {payload['error']}"
        # An unparseable error body keeps the status line as the message.
        except Exception:  # nosec B110
            pass
        finally:
            exc.close()
        return BinderyError(
            message, status=exc.code, code=code, retry_after=_retry_after(exc.headers)
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        with self._request(path, accept="application/json") as resp:
            raw = resp.read(MAX_JSON_BODY + 1)
        if len(raw) > MAX_JSON_BODY:
            raise BinderyError(f"response from {path} is too large", status=200)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise BinderyError(f"response from {path} is not JSON", status=200) from exc
        if not isinstance(payload, dict):
            raise BinderyError(f"response from {path} is not a JSON object", status=200)
        return payload

    def _post_json(self, path: str, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        with self._request(path, data=data, method="POST", accept="application/json") as resp:
            resp.read(MAX_ERROR_BODY)

    def _download(self, path: str, dest: str, limit: int) -> tuple[int, str]:
        with self._request(path) as resp:
            declared = resp.headers.get("Content-Length")
            expected: int | None = None
            if declared is not None:
                try:
                    expected = int(declared)
                except ValueError:
                    expected = None
            if expected is not None and expected > limit:
                raise TooLarge(
                    f"{path} is {expected} bytes, over the {limit} byte limit",
                    code="body_too_large",
                )
            written = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise TooLarge(
                            f"{path} passed the {limit} byte limit while downloading",
                            code="body_too_large",
                        )
                    out.write(chunk)
            if expected is not None and written != expected:
                raise BinderyUnreachable(
                    f"{path} ended after {written} of {expected} bytes", code="truncated"
                )
            ctype = str(resp.headers.get("Content-Type") or "")
        return written, ctype


def _quote_id(delivery_id: str) -> str:
    """A delivery id goes into the path, so nothing in it may add a segment."""
    return urllib.parse.quote(str(delivery_id), safe="")
