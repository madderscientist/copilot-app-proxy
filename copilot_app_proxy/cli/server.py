"""Run the gateway worker process that `start` launches."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from copilot_app_proxy.gateway import MIN_INGEST_SECRET_LENGTH, create_app, listen
from copilot_app_proxy.relay_store import RelayTokenStore

LOG_MAX_BYTES = 5 * 1024 * 1024


def _logger(log_file: Path | None) -> logging.Logger:
    """Log to ``log_file`` (cleared now, rotated at 5 MB, one backup) or to stdout."""
    if log_file is None:
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
    else:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_bytes(b"")
        log_file.with_name(log_file.name + ".1").unlink(missing_ok=True)
        # Only this process may hold the file open: Windows cannot rename it otherwise.
        handler = RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
        )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger = logging.getLogger("copilot_app_proxy.gateway.access")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


async def serve_gateway(port: int, *, token_file: Path, log_file: Path | None = None) -> int:
    relay_ingest_secret = os.environ.get("RELAY_INGEST_SECRET", "")
    if len(relay_ingest_secret) < MIN_INGEST_SECRET_LENGTH:
        raise ValueError(
            f"Set RELAY_INGEST_SECRET to a random secret of at least "
            f"{MIN_INGEST_SECRET_LENGTH} characters."
        )
    log = _logger(log_file)
    relay_store = RelayTokenStore(token_file=token_file)
    if await relay_store.load_cached():
        status = relay_store.status()
        state = "valid" if status["hasToken"] else "expired"
        log.info(f"Loaded cached token ({state}, exp={status['tokenExpiresOn'] or 'unknown'})")
    app = create_app(
        relay_store=relay_store, relay_ingest_secret=relay_ingest_secret, log=log.info
    )

    try:
        async with listen(app, port):
            log.info(f"Listening on http://127.0.0.1:{port}")
            await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except OSError as exc:
        message = (
            "Loopback port is already in use."
            if exc.errno in (errno.EADDRINUSE, 10048)
            else f"Gateway failed to start: {exc}"
        )
        print(message, file=sys.stderr)
        return 1
    return 0
