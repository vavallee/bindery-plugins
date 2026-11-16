"""Configuration: environment variables, with an optional JSON file under them.

Kavita credentials live here and nowhere in Bindery. Every secret can also be
read from a file (`*_FILE`), for Docker and Kubernetes secrets.
"""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_PREFIX = "KAVITA_BRIDGE_"
MIN_REMOTE_KEY_LENGTH = 16
SCAN_MODES = ("all", "path")
UNMAPPED_ACTIONS = ("skip", "all", "libraries")
AUTH_MODES = ("api-key", "jwt")
REDACTED = "<redacted>"


class ConfigError(ValueError):
    """The configuration cannot be used. The message never holds a secret."""


@dataclass(frozen=True)
class PathMapping:
    """Bindery sees a folder at `bindery`, Kavita sees the same folder at `kavita`."""

    bindery: str
    kavita: str


@dataclass(frozen=True)
class Config:
    bind_host: str = "127.0.0.1"
    port: int = 8103
    api_key: str = field(default="", repr=False)
    kavita_url: str = ""
    kavita_api_key: str = field(default="", repr=False)
    auth_mode: str = "api-key"
    kavita_timeout: float = 10.0
    debounce_seconds: float = 30.0
    scan_mode: str = "all"
    path_map: tuple[PathMapping, ...] = ()
    unmapped: str = "skip"
    unmapped_libraries: tuple[int, ...] = ()
    force: bool = False
    log_level: str = "INFO"

    def secrets(self) -> list[str]:
        return [s for s in (self.api_key, self.kavita_api_key) if s]

    def redacted(self) -> dict[str, Any]:
        """A loggable view. Secrets are replaced, never truncated."""
        return {
            "bind_host": self.bind_host,
            "port": self.port,
            "api_key": REDACTED if self.api_key else "",
            "kavita_url": self.kavita_url,
            "kavita_api_key": REDACTED if self.kavita_api_key else "",
            "auth_mode": self.auth_mode,
            "kavita_timeout": self.kavita_timeout,
            "debounce_seconds": self.debounce_seconds,
            "scan_mode": self.scan_mode,
            "path_map": [{"bindery": m.bindery, "kavita": m.kavita} for m in self.path_map],
            "unmapped": self.unmapped,
            "unmapped_libraries": list(self.unmapped_libraries),
            "force": self.force,
            "log_level": self.log_level,
        }


def is_loopback(host: str) -> bool:
    """True for a bind host that only this machine can reach."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _read_secret_file(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read the {what} file: {exc.strerror}") from None


def _parse_path_map(raw: Any) -> tuple[PathMapping, ...]:
    """`/books=/data/books;/audio=/data/audio`, or a list of {bindery, kavita}."""
    if raw in (None, "", []):
        return ()
    pairs: list[PathMapping] = []
    if isinstance(raw, str):
        for item in raw.split(";"):
            item = item.strip()
            if not item:
                continue
            bindery, sep, kavita = item.partition("=")
            if not sep or not bindery.strip() or not kavita.strip():
                raise ConfigError(
                    f"path_map entry {item!r} must look like BINDERY_PATH=KAVITA_PATH"
                )
            pairs.append(PathMapping(bindery.strip(), kavita.strip()))
    elif isinstance(raw, list):
        for item in raw:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("bindery"), str)
                or not isinstance(item.get("kavita"), str)
                or not item["bindery"]
                or not item["kavita"]
            ):
                raise ConfigError("each path_map entry needs a 'bindery' and a 'kavita' string")
            pairs.append(PathMapping(item["bindery"], item["kavita"]))
    else:
        raise ConfigError("path_map must be a string or a list")
    return tuple(pairs)


def _parse_int_list(raw: Any, what: str) -> tuple[int, ...]:
    if raw in (None, "", []):
        return ()
    items = raw.split(",") if isinstance(raw, str) else raw
    try:
        ids = tuple(int(str(i).strip()) for i in items if str(i).strip() != "")
    except ValueError:
        raise ConfigError(f"{what} must be a list of whole numbers") from None
    if any(i <= 0 for i in ids):
        raise ConfigError(f"{what} must be positive library ids")
    return ids


def _number(raw: Any, what: str, minimum: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be a number") from None
    if value < minimum:
        raise ConfigError(f"{what} must be at least {minimum:g}")
    return value


def load(env: Mapping[str, str] | None = None, config_file: str | None = None) -> Config:
    """Build a Config. Environment variables win over the file."""
    env = os.environ if env is None else env
    values: dict[str, Any] = {}

    path = config_file or env.get(ENV_PREFIX + "CONFIG")
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
            loaded = json.loads(text)
        except OSError as exc:
            raise ConfigError(f"cannot read the config file: {exc.strerror}") from None
        except ValueError:
            raise ConfigError("the config file is not valid JSON") from None
        if not isinstance(loaded, dict):
            raise ConfigError("the config file must hold a JSON object")
        values.update({str(k).lower(): v for k, v in loaded.items()})

    names = (
        "bind_host",
        "port",
        "api_key",
        "kavita_url",
        "kavita_api_key",
        "auth_mode",
        "kavita_timeout",
        "debounce_seconds",
        "scan_mode",
        "path_map",
        "unmapped",
        "unmapped_libraries",
        "force",
        "log_level",
    )
    for name in names:
        upper = ENV_PREFIX + name.upper()
        if upper in env and env[upper] != "":
            values[name] = env[upper]
        if upper + "_FILE" in env and env[upper + "_FILE"] != "":
            values[name] = _read_secret_file(env[upper + "_FILE"], name.replace("_", " "))

    scan_mode = str(values.get("scan_mode", "all")).lower()
    if scan_mode not in SCAN_MODES:
        raise ConfigError(f"scan_mode must be one of: {', '.join(SCAN_MODES)}")
    unmapped = str(values.get("unmapped", "skip")).lower()
    if unmapped not in UNMAPPED_ACTIONS:
        raise ConfigError(f"unmapped must be one of: {', '.join(UNMAPPED_ACTIONS)}")
    auth_mode = str(values.get("auth_mode", "api-key")).lower()
    if auth_mode not in AUTH_MODES:
        raise ConfigError(f"auth_mode must be one of: {', '.join(AUTH_MODES)}")

    try:
        port = int(values.get("port", 8103))
    except (TypeError, ValueError):
        raise ConfigError("port must be a whole number") from None
    if not 1 <= port <= 65535:
        raise ConfigError("port must be between 1 and 65535")

    kavita_url = str(values.get("kavita_url", "")).strip().rstrip("/")
    if not kavita_url:
        raise ConfigError("kavita_url is required (KAVITA_BRIDGE_KAVITA_URL)")
    if not kavita_url.lower().startswith(("http://", "https://")):
        raise ConfigError("kavita_url must start with http:// or https://")
    if "@" in kavita_url.split("//", 1)[1].split("/", 1)[0]:
        raise ConfigError("kavita_url must not carry credentials; use kavita_api_key")

    kavita_api_key = str(values.get("kavita_api_key", "")).strip()
    if not kavita_api_key:
        raise ConfigError("kavita_api_key is required (KAVITA_BRIDGE_KAVITA_API_KEY)")

    unmapped_libraries = _parse_int_list(values.get("unmapped_libraries"), "unmapped_libraries")
    if unmapped == "libraries" and not unmapped_libraries:
        raise ConfigError("unmapped=libraries needs unmapped_libraries")

    config = Config(
        bind_host=str(values.get("bind_host", "127.0.0.1")).strip(),
        port=port,
        api_key=str(values.get("api_key", "")).strip(),
        kavita_url=kavita_url,
        kavita_api_key=kavita_api_key,
        auth_mode=auth_mode,
        kavita_timeout=_number(values.get("kavita_timeout", 10), "kavita_timeout", 1),
        debounce_seconds=_number(values.get("debounce_seconds", 30), "debounce_seconds", 0),
        scan_mode=scan_mode,
        path_map=_parse_path_map(values.get("path_map")),
        unmapped=unmapped,
        unmapped_libraries=unmapped_libraries,
        force=_truthy(values.get("force", False)),
        log_level=str(values.get("log_level", "INFO")).upper(),
    )
    check_bind(config)
    return config


def check_bind(config: Config) -> None:
    """Refuse a network bind that has no usable key.

    The same rule as the Calibre bridge (docs/protocol.md, Authentication): a
    bind that is not loopback needs an `api_key` of at least 16 characters.
    """
    if is_loopback(config.bind_host):
        return
    if not config.api_key:
        raise ConfigError(
            f"refusing to start: bind_host {config.bind_host!r} is not loopback and no api_key "
            "is set. Set KAVITA_BRIDGE_API_KEY to a key of at least "
            f"{MIN_REMOTE_KEY_LENGTH} characters, or bind to 127.0.0.1."
        )
    if len(config.api_key) < MIN_REMOTE_KEY_LENGTH:
        raise ConfigError(
            f"refusing to start: bind_host {config.bind_host!r} is not loopback and the api_key "
            f"is shorter than {MIN_REMOTE_KEY_LENGTH} characters. Use a longer key "
            "(for example `openssl rand -hex 32`)."
        )
