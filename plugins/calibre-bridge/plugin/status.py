"""Last known state of the bridge server, for anything that wants to report it.

Deliberately importless: the config dialog reads this without pulling in the
HTTP stack, and the value survives a failed start so the dialog can show why
the bridge is not listening.
"""

RUNNING = "running"
DEGRADED = "degraded"
STOPPED = "stopped"

_state = {"status": STOPPED, "detail": "", "endpoint": ""}


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
        return f"Not listening for books: {state['detail']}{where}"
    if state["detail"]:
        return f"Not running: {state['detail']}"
    return "Not running"


# -- pull mode (0.8.0) ---------------------------------------------------------
#
# Kept apart from the server state above: the push server and the pull worker
# run side by side and either can fail without the other.

_pull: dict = {
    "enabled": False,
    "state": "",
    "detail": "",
    "warning": "",
    "last_run": 0.0,
    "last_error": "",
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


def pull_summary() -> str:
    """One line about pull mode for the config dialog."""
    state = pull_current()
    if not state["enabled"]:
        return "Pull from Bindery is off"
    parts = [str(state["detail"] or state["state"] or "Starting")]
    if state["delivered"]:
        n = state["delivered"]
        noun = "file" if n == 1 else "files"
        parts.append(f"{n} {noun} delivered since Calibre started")
    if state["last_error"]:
        parts.append(f"Last error: {state['last_error']}")
    if state["warning"]:
        parts.append(str(state["warning"]))
    return ". ".join(p.rstrip(".") for p in parts) + "."


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
