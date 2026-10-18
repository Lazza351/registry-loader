"""Command line service. --once waits one stability window then drains that snapshot."""

import argparse
import logging
import os
import signal
import threading

from .config import Config
from .upload import Uploader
from .watcher import Watcher

log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Nexus directory uploader")
    parser.add_argument(
        "--once", action="store_true", help="Two scans separated by stability window"
    )
    parser.add_argument("--check-config", action="store_true", help="Validate only; do not upload")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Third-party logs can include server-supplied reasons, URLs and credentials.
    for name in ("twine", "urllib3", "requests"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        config = Config.from_env(os.environ)
    except (ValueError, OSError):
        log.error("Invalid configuration; check README (values suppressed)")
        return 2
    if args.check_config:
        log.info("Configuration valid")
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        with Watcher(config, Uploader(config)) as watcher:
            if args.once:
                watcher.scan(stop=stop)
                if not stop.wait(config.stable):
                    watcher.scan(stop=stop)
            else:
                while not stop.is_set():
                    watcher.scan(stop=stop)
                    stop.wait(config.poll)
    except (RuntimeError, OSError):
        log.error("Fatal local queue/lock failure; inspect permissions and processing state")
        return 1
    return 0
