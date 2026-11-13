#!/usr/bin/env python3
"""Run the Calibre Bridge against a real Calibre.

    python3 plugins/calibre-bridge/tests/integration/run.py [model|boot|all] \\
        --calibre-dir ~/calibre-bin/calibre

Builds the plugin zip with ``scripts/build_plugin.py``, installs it with
``calibre-customize -a`` into a throwaway Calibre configuration directory, and
then runs one or both checks:

``model``
    ``model_check.py`` inside ``calibre-debug``: a real library and a real
    ``BooksModel``, the plugin's add path, and the refresh that makes a new
    book show up in the list. This is the check that would have caught #30.

``boot``
    Starts the whole Calibre GUI with the plugin configured, waits for
    ``GET /v1/health``, posts a book, shuts Calibre down and fails on any
    traceback from the plugin. This is the check that would have caught #26.

Nothing here touches your own Calibre settings or library: every Calibre
process is started with ``CALIBRE_CONFIG_DIRECTORY`` and
``CALIBRE_CACHE_DIRECTORY`` pointed into a temporary directory, which is
removed afterwards (``--keep`` leaves it in place and prints where it is).

Standard library only. It runs on the host interpreter, not inside Calibre.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]

sys.path.insert(0, str(HERE))
import sample_books  # noqa: E402

BOOT_TITLE = "Bindery Boot Probe"
BOOT_AUTHOR = "Grace Tester"

# Run inside Calibre before the GUI starts, so it comes up without asking
# anything. calibre.gui2.main shows the welcome wizard unless
# dynamic['welcome_wizard_was_run'] is set, and takes the library from
# prefs['library_path']. Main.initialize adds the Quick Start Guide to the
# library unless gprefs['quick_start_guide_added'] is set, which would put a
# book in the library that this check did not deliver.
SEED_CALIBRE_PREFS = """
import os
from calibre.gui2 import gprefs
from calibre.utils.config import dynamic, prefs
prefs.set("library_path", os.environ["BRIDGE_BOOT_LIBRARY"])
dynamic.set("welcome_wizard_was_run", True)
gprefs["quick_start_guide_added"] = True
gprefs["show_splash_screen"] = False
"""

# Calibre allows one running GUI per user, whatever the configuration
# directory: a second "calibre" hands its arguments to the first and exits,
# and "calibre -s" stops whichever one is running. Both would land on the
# developer's own Calibre, so the boot check looks first and refuses.
PROBE_RUNNING_CALIBRE = """
from calibre.utils.lock import SingleInstance
with SingleInstance("GUI") as gui_free, SingleInstance("db") as db_free:
    raise SystemExit(0 if gui_free and db_free else 3)
"""


class CheckFailed(Exception):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def tool(calibre_dir: pathlib.Path, name: str) -> str:
    path = calibre_dir / name
    if not path.is_file():
        raise CheckFailed(f"{path} does not exist. Point --calibre-dir at a Calibre install")
    return str(path)


def find_calibre_dir(given: str | None) -> pathlib.Path:
    if given:
        return pathlib.Path(given).expanduser().resolve()
    found = shutil.which("calibre-debug")
    if not found:
        raise CheckFailed("calibre-debug is not on PATH. Pass --calibre-dir or set CALIBRE_DIR")
    return pathlib.Path(found).resolve().parent


def build_and_install(
    calibre_dir: pathlib.Path, plugin_dir: pathlib.Path, work: pathlib.Path, env: dict[str, str]
) -> pathlib.Path:
    """Build the release zip and install it the way a user would."""
    dist = work / "dist"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "build_plugin.py"),
            str(plugin_dir),
            "--output-dir",
            str(dist),
        ],
        check=True,
    )
    zips = sorted(dist.glob("*.zip"))
    if len(zips) != 1:
        raise CheckFailed(f"expected one plugin zip in {dist}, found {zips}")
    subprocess.run(
        [tool(calibre_dir, "calibre-customize"), "-a", str(zips[0])],
        check=True,
        env=env,
        timeout=120,
    )
    return zips[0]


def model_check(calibre_dir: pathlib.Path, env: dict[str, str]) -> None:
    say("== model check ==")
    done = subprocess.run(
        [tool(calibre_dir, "calibre-debug"), "-e", str(HERE / "model_check.py")],
        env=env,
        timeout=240,
    )
    if done.returncode != 0:
        raise CheckFailed(f"the model check exited with status {done.returncode}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http(method: str, url: str, key: str = "", body: dict | None = None) -> tuple[int, dict]:
    """One request to the bridge. Returns the status and the decoded JSON."""
    headers = {}
    data = None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def wait_for_health(base: str, proc: subprocess.Popen, timeout: float) -> dict:
    """Poll ``/v1/health`` until the bridge answers or Calibre gives up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise CheckFailed(
                f"Calibre exited with status {proc.returncode} before the bridge answered"
            )
        try:
            status, payload = http("GET", base + "/v1/health")
        except (OSError, ValueError):
            time.sleep(0.5)
            continue
        if status == 200:
            return payload
        raise CheckFailed(f"GET /v1/health answered {status}: {payload}")
    raise CheckFailed(f"the bridge did not answer GET /v1/health within {timeout:.0f} seconds")


def post_book(base: str, key: str, body: dict, timeout: float) -> tuple[int, dict]:
    """POST /v1/books, waiting out the 503 the bridge sends until a library is open.

    The HTTP server starts in ``genesis``, which runs before Calibre attaches
    the library to the main window, so an early request can be told to retry.
    """
    deadline = time.monotonic() + timeout
    while True:
        status, payload = http("POST", base + "/v1/books", key=key, body=body)
        if status != 503 or time.monotonic() > deadline:
            return status, payload
        time.sleep(0.5)


def tracebacks(log: str) -> list[str]:
    """Every Python traceback in ``log``, each up to its exception line."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in log.splitlines():
        if "Traceback (most recent call last)" in line:
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
            if line and not line[0].isspace():
                blocks.append("\n".join(current))
                current = None
    if current:
        blocks.append("\n".join(current))
    return blocks


def stop_calibre(calibre_dir: pathlib.Path, env: dict[str, str], proc: subprocess.Popen) -> None:
    """Ask the running Calibre to quit, the way ``calibre -s`` does for a user."""
    asked = subprocess.run(
        [tool(calibre_dir, "calibre"), "--shutdown-running-calibre"],
        env=env,
        timeout=120,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if asked.returncode != 0:
        say(f"calibre -s exited with status {asked.returncode}: {asked.stdout.strip()}")
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        raise CheckFailed("Calibre did not exit within 60 seconds of calibre -s") from None


def kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(timeout=30)


def boot_check(
    calibre_dir: pathlib.Path, env: dict[str, str], work: pathlib.Path, plugin_zip: pathlib.Path
) -> None:
    say("== boot check ==")
    probe = subprocess.run(
        [tool(calibre_dir, "calibre-debug"), "-c", PROBE_RUNNING_CALIBRE], env=env, timeout=120
    )
    if probe.returncode != 0:
        raise CheckFailed(
            "a Calibre program is already running for this user. The boot check starts and "
            "stops a Calibre of its own, and Calibre allows one per user, so close yours "
            "and run it again, or run only the model check"
        )
    library = work / "boot-library"
    library.mkdir()
    port = free_port()
    key = secrets.token_hex(16)
    base = f"http://127.0.0.1:{port}"

    # The plugin's own settings: plugin/config.py reads
    # JSONConfig("plugins/bindery_bridge"), a JSON file under the config dir.
    plugin_prefs = pathlib.Path(env["CALIBRE_CONFIG_DIRECTORY"]) / "plugins" / "bindery_bridge.json"
    plugin_prefs.parent.mkdir(parents=True, exist_ok=True)
    plugin_prefs.write_text(json.dumps({"bind_host": "127.0.0.1", "port": port, "api_key": key}))

    subprocess.run(
        [tool(calibre_dir, "calibre-debug"), "-c", SEED_CALIBRE_PREFS],
        check=True,
        env={**env, "BRIDGE_BOOT_LIBRARY": str(library)},
        timeout=120,
    )

    book = sample_books.write_epub(
        str(work / "boot-probe.epub"),
        BOOT_TITLE,
        BOOT_AUTHOR,
        "00000000-0000-4000-8000-000000000003",
    )
    body = {
        "path": book,
        "metadata": {
            "title": BOOT_TITLE,
            "authors": [BOOT_AUTHOR],
            "identifiers": {"bindery": "301"},
        },
    }

    log_path = work / "calibre.log"
    failure: CheckFailed | None = None
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            [
                tool(calibre_dir, "calibre"),
                "--with-library",
                str(library),
                "--no-update-check",
            ],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            started = time.monotonic()
            health = wait_for_health(base, proc, timeout=120)
            say(f"  ok: GET /v1/health answered after {time.monotonic() - started:.1f}s: {health}")
            expected = re.search(r"-v(\d+\.\d+\.\d+)\.zip$", plugin_zip.name)
            if expected and health.get("plugin_version") != expected.group(1):
                raise CheckFailed(
                    f"health reports plugin {health.get('plugin_version')!r}, "
                    f"the installed zip is {expected.group(1)}"
                )

            status, payload = http("POST", base + "/v1/books", key="wrong-key", body=body)
            if status != 401:
                raise CheckFailed(f"POST /v1/books with a wrong key answered {status}: {payload}")
            say("  ok: a wrong key is refused with 401")

            status, payload = post_book(base, key, body, timeout=60)
            if status != 201 or payload.get("duplicate") or not payload.get("id"):
                raise CheckFailed(f"POST /v1/books answered {status}: {payload}, expected a 201")
            say(f"  ok: POST /v1/books answered 201: {payload}")
            book_id = payload["id"]

            status, payload = post_book(base, key, body, timeout=60)
            if status != 409 or payload.get("id") != book_id:
                raise CheckFailed(
                    f"the same book posted again answered {status}: {payload}, "
                    f"expected a 409 for book {book_id}"
                )
            say("  ok: the same book posted again is a 409 duplicate")

            # The refresh the add queued runs on the GUI thread. Give it a
            # moment, so anything it prints is in the log before the shutdown.
            time.sleep(2)
            stop_calibre(calibre_dir, env, proc)
            say(f"  ok: Calibre shut down with status {proc.returncode}")
        except CheckFailed as exc:
            failure = exc
        finally:
            kill(proc)

    log = log_path.read_text(errors="replace")
    say("---- Calibre output ----")
    say(log.rstrip() or "(nothing)")
    say("---- end of Calibre output ----")
    if failure is not None:
        raise failure

    found = tracebacks(log)
    ours = [block for block in found if "bindery_bridge" in block]
    if ours:
        raise CheckFailed(
            "Calibre printed a traceback from the plugin while it ran:\n\n" + "\n\n".join(ours)
        )
    if found:
        say(f"  note: {len(found)} traceback(s) in the Calibre output, none from the plugin")
    say("  ok: no traceback from the plugin in the Calibre output")

    listed = subprocess.run(
        [
            tool(calibre_dir, "calibredb"),
            "list",
            "--with-library",
            str(library),
            "--for-machine",
            "--fields",
            "title,formats",
        ],
        check=True,
        env=env,
        timeout=120,
        stdout=subprocess.PIPE,
        text=True,
    )
    books = json.loads(listed.stdout)
    ours_in_library = [b for b in books if b.get("title") == BOOT_TITLE]
    if len(ours_in_library) != 1 or not ours_in_library[0].get("formats"):
        raise CheckFailed(f"expected the posted book once in the library, with its file: {books}")
    say("  ok: the library holds the posted book once, with its file")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("check", nargs="?", choices=("model", "boot", "all"), default="all")
    parser.add_argument(
        "--calibre-dir",
        default=os.environ.get("CALIBRE_DIR"),
        help="Directory holding calibre, calibre-debug, calibre-customize and calibredb "
        "(default: $CALIBRE_DIR, else wherever calibre-debug is on PATH)",
    )
    parser.add_argument(
        "--plugin-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "plugins" / "calibre-bridge",
        help="Plugin source tree to build and test (default: this checkout's)",
    )
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    args = parser.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(prefix="bridge-real-calibre-"))
    try:
        calibre_dir = find_calibre_dir(args.calibre_dir)
        env = dict(os.environ)
        env["CALIBRE_CONFIG_DIRECTORY"] = str(work / "config")
        env["CALIBRE_CACHE_DIRECTORY"] = str(work / "cache")
        # No display needed. Set QT_QPA_PLATFORM yourself to watch it run.
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        plugin_zip = build_and_install(calibre_dir, args.plugin_dir.resolve(), work, env)
        if args.check in ("model", "all"):
            model_check(calibre_dir, env)
        if args.check in ("boot", "all"):
            boot_check(calibre_dir, env, work, plugin_zip)
    except CheckFailed as exc:
        print(f"FAIL: {exc}", file=sys.stderr, flush=True)
        return 1
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if args.keep:
            say(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    say("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
