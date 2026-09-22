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
