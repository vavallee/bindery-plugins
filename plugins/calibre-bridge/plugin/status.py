"""Last known state of the bridge server, for anything that wants to report it.

Deliberately imports nothing from the plugin or from Calibre: the config dialog
reads this without pulling in the HTTP stack, and the value survives a failed
start so the dialog can show why the bridge is not listening.
"""

import time

RUNNING = "running"
DEGRADED = "degraded"
STOPPED = "stopped"

# Longest piece of error text a status line shows. An error can quote what a
# server sent, and the settings window is not the place for a page of it.
MAX_ERROR_CHARS = 300

_state = {"status": STOPPED, "detail": "", "endpoint": ""}


def clip(text: object, limit: int = MAX_ERROR_CHARS) -> str:
    """``text`` on one line, cut to ``limit`` characters with a mark when cut."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def set_running(endpoint: str) -> None:
    _state.update({"status": RUNNING, "detail": "", "endpoint": endpoint})


def set_degraded(detail: str, endpoint: str = "") -> None:
    _state.update({"status": DEGRADED, "detail": detail, "endpoint": endpoint})


def set_stopped(detail: str = "") -> None:
    _state.update({"status": STOPPED, "detail": detail, "endpoint": ""})


def current() -> dict:
    return dict(_state)


def summary() -> str:
    """One line for the config dialog. Safe to show before any start attempt."""
    state = current()
    if state["status"] == RUNNING:
        return f"Listening on {state['endpoint']}"
    if state["status"] == DEGRADED:
        endpoint = state["endpoint"]
        where = f" A health only server is answering on {endpoint}." if endpoint else ""
        return f"Not listening for books: {clip(state['detail'])}{where}"
    if state["detail"]:
        return f"Not running: {clip(state['detail'])}"
    return "Not running"


# -- pull mode (0.8.0) ---------------------------------------------------------
#
# Kept apart from the server state above: the push server and the pull worker
# run side by side and either can fail without the other.

_pull: dict = {
    "enabled": False,
    "detail": "",
    "warning": "",
    # The error of the newest pass that had one, and when. A pass that reaches
    # Bindery and finishes without an error clears both.
    "last_error": "",
    "last_error_at": 0.0,
    # When a pass last reached Bindery and finished without an error.
    "last_success": 0.0,
    "delivered": 0,
}

# Called after the config dialog saves, so a change made from Preferences,
# Plugins takes effect without a restart. The action registers it in genesis.
_config_listeners: list = []


def set_pull(**fields: object) -> None:
    _pull.update(fields)


def add_pull_delivered(count: int) -> None:
    _pull["delivered"] = int(_pull.get("delivered") or 0) + int(count)


def pull_current() -> dict:
    return dict(_pull)


def _clock_text(when: float, now: float) -> str:
    """``when`` as local HH:MM, with the date when it was not today."""
    then = time.localtime(when)
    today = time.localtime(now)
    if (then.tm_year, then.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M", then)
    return time.strftime("%Y-%m-%d %H:%M", then)


def _sentence(text: str) -> str:
    # A part that was cut already ends in its own mark.
    return text if text.endswith("…") else text.rstrip(".") + "."


def pull_summary(now: float | None = None) -> str:
    """One line about pull mode for the config dialog."""
    state = pull_current()
    if not state["enabled"]:
        return "Pull from Bindery is off"
    current = time.time() if now is None else now
    parts = [clip(state["detail"] or "Starting")]
    if state["delivered"]:
        n = state["delivered"]
        noun = "file" if n == 1 else "files"
        parts.append(f"{n} {noun} delivered since Calibre started")
    if state["last_error"]:
        at = state.get("last_error_at") or 0.0
        when = f" ({_clock_text(at, current)})" if at else ""
        parts.append(f"Last error{when}: {clip(state['last_error'])}")
    if state.get("last_success"):
        parts.append(f"Last successful check {_clock_text(state['last_success'], current)}")
    if state["warning"]:
        parts.append(str(state["warning"]))
    return " ".join(_sentence(p) for p in parts)


def on_config_saved(listener: object) -> None:
    if listener not in _config_listeners:
        _config_listeners.append(listener)


def config_saved() -> None:
    for listener in list(_config_listeners):
        try:
            listener()  # type: ignore[operator]
        # A listener must not break the dialog that saved.
        except Exception:  # nosec B112
            continue
