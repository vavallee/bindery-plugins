"""The optional debug log file.

Nothing else in the plugin configures logging, so without this only bare
WARNING lines reach stderr (through ``logging.lastResort``) and every INFO and
DEBUG line is dropped. A user asked to "turn on debug logging" had no switch
to turn. :func:`apply` is that switch: it puts a rotating file handler on the
plugin's own logger namespace and takes it off again.

Rules this module keeps:

* Only :data:`LOGGER_NAME` is touched. The root logger and Calibre's own
  loggers keep their handlers and their levels.
* :func:`apply` never raises. A log that cannot be opened must not stop
  ``genesis`` or a settings save; the reason is kept for the settings window.
* The API key never reaches the file. Nothing in the plugin logs it today, and
  the formatter still removes the configured key, anything after ``Bearer``
  and the value of an ``Authorization`` or API key field from every line, so
  an error message that echoes a request cannot leak it either.
* The file is created readable by its owner only, where the platform has
  such a thing.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import re
import threading
from collections.abc import Iterable
from typing import Any

# Every module of the plugin logs under this name inside Calibre, where the
# package is ``calibre_plugins.bindery_bridge`` (the import name file next to
# the plugin's ``__init__.py`` fixes it).
LOGGER_NAME = "calibre_plugins.bindery_bridge"
FILE_NAME = "bindery_bridge.log"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 2
REDACTED = "[redacted]"
# A shorter key is not a secret worth the name, and blanking every "a" in the
# file for a one letter key would leave nothing to read.
MIN_SECRET_LENGTH = 4

_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
# Eight or more token characters, so the plain words of a message such as
# "missing Bearer token from 10.0.0.5" are left alone.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
# The value of an Authorization or API key field, with or without "Bearer".
_FIELD_RE = re.compile(
    r"(?i)\b((?:authorization|x-api-key|api[_-]?key)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)"
    r"([^\s\"',;&]+)"
)

_lock = threading.Lock()
_handlers: list[logging.Handler] = []
_error = ""


def log_dir() -> str:
    """Calibre's configuration directory, or "" when it cannot be found.

    ``calibre.utils.config.config_dir`` is what Calibre's own "Open calibre
    configuration folder" button opens. The import is inside the function so
    this module loads without Calibre.
    """
    try:
        from calibre.utils.config import config_dir
    except Exception:
        try:
            from calibre.constants import config_dir
        except Exception:
            return ""
    return str(config_dir or "")


def log_path() -> str:
    folder = log_dir()
    return os.path.join(folder, FILE_NAME) if folder else ""


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with every credential replaced by :data:`REDACTED`."""
    for secret in secrets:
        if len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    text = _FIELD_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)


class _RedactingFormatter(logging.Formatter):
    """Formats a record, traceback included, then removes credentials."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__(_FORMAT)
        unique = {str(s) for s in secrets if s}
        # Longest first, so a key that contains another is replaced whole.
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating log that only its owner can read.

    ``_open`` is also what a rollover calls for the fresh file, and a backup
    is the old file renamed, so every file in the set gets the same mode.
    Windows ignores the mode bits; the user profile's own ACL covers it there.
    """

    def _open(self) -> Any:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        # The mode above only applies to a file this call created.
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)


class _KeepStderrWarnings(logging.Handler):
    """Keep warnings on stderr while the file handler is attached.

    Python prints WARNING and above to stderr only when no handler at all is
    found for a record. Attaching the file handler would end that, and a user
    watching ``calibre-debug -g`` would stop seeing the plugin's warnings the
    moment they turned the debug log on. When something else already handles
    the record (a handler on the root logger, say) this does nothing.
    """

    def emit(self, record: logging.LogRecord) -> None:
        node: logging.Logger | None = logging.getLogger(LOGGER_NAME)
        while node is not None:
            if any(handler not in _handlers for handler in node.handlers):
                return
            node = node.parent if node.propagate else None
        last = logging.lastResort
        if last is not None and record.levelno >= last.level:
            last.handle(record)


def _detach() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(_handlers):
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    if _handlers:
        # Back to inheriting the level, which is how the logger started.
        logger.setLevel(logging.NOTSET)
    _handlers.clear()


def _attach(path: str, secrets: Iterable[str]) -> None:
    handler = _PrivateRotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_RedactingFormatter(secrets))
    logger = logging.getLogger(LOGGER_NAME)
    for added in (handler, _KeepStderrWarnings(logging.WARNING)):
        logger.addHandler(added)
        _handlers.append(added)
    # The handler level is not enough: a logger drops DEBUG before any
    # handler sees it unless its own level lets it through.
    logger.setLevel(logging.DEBUG)


def apply(enabled: bool, secrets: Iterable[str] = (), path: str = "") -> str:
    """Turn the debug log on or off. Returns "" or why it could not be opened.

    Safe to call again with the same or different settings: the old handler
    is removed and its file closed first, so a changed API key takes effect
    in the redaction and the log is never written twice. Never raises.
    """
    global _error
    with _lock:
        try:
            _detach()
            _error = ""
            if enabled:
                target = path or log_path()
                if not target:
                    raise OSError("Calibre's configuration folder could not be found")
                _attach(target, list(secrets))
        except Exception as exc:
            with contextlib.suppress(Exception):
                _detach()
            _error = str(exc) or type(exc).__name__
        return _error


def is_active() -> bool:
    return bool(_handlers)


def last_error() -> str:
    """Why the last :func:`apply` could not open the log, or ""."""
    return _error
