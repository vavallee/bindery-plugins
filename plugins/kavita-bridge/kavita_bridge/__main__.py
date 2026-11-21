"""Run the receiver: `python -m kavita_bridge [--config FILE] [--check-config]`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence

from . import __version__
from .config import ConfigError, load, plain_http_warning
from .kavita import KavitaClient
from .receiver import Receiver, RedactFilter
from .service import ScanService

log = logging.getLogger("kavita_bridge")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kavita_bridge", description=__doc__)
    parser.add_argument("--config", help="path to a JSON config file; environment variables win")
    parser.add_argument("--check-config", action="store_true", help="validate and exit")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
    try:
        config = load(config_file=args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    log.setLevel(getattr(logging, config.log_level, logging.INFO))
    redact = RedactFilter(config.secrets())
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
    log.addFilter(redact)
    warning = plain_http_warning(config)
    if warning:
        log.warning("%s", warning)
    if args.check_config:
        log.info("configuration ok: %s", config.redacted())
        return 0

    client = KavitaClient(
        config.kavita_url, config.kavita_api_key, config.auth_mode, config.kavita_timeout
    )
    service = ScanService(config, client)
    try:
        server = Receiver(config, client, service)
    except OSError as exc:
        log.error("cannot listen on %s:%d: %s", config.bind_host, config.port, exc.strerror)
        return 1
    if not config.api_key:
        log.warning("no api_key set: any local process can trigger scans (loopback only)")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("kavita-bridge %s listening on %s:%d", __version__, config.bind_host, config.port)
    stop.wait()
    log.info("shutting down")
    server.shutdown()
    service.stop()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
