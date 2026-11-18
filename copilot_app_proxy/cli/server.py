"""Run the gateway worker that `start` launches in the background."""

from __future__ import annotations

import asyncio
import errno
import os
import sys
from pathlib import Path

from copilot_app_proxy.gateway import MIN_INGEST_SECRET_LENGTH, create_app, listen
from copilot_app_proxy.relay_store import RelayTokenStore


async def serve_gateway(port: int, *, token_file: Path) -> int:
    relay_ingest_secret = os.environ.get("RELAY_INGEST_SECRET", "")
    if len(relay_ingest_secret) < MIN_INGEST_SECRET_LENGTH:
        raise ValueError(
            f"Set RELAY_INGEST_SECRET to a random secret of at least "
            f"{MIN_INGEST_SECRET_LENGTH} characters."
        )
    relay_store = RelayTokenStore(token_file=token_file)
    if await relay_store.load_cached():
        status = relay_store.status()
        print(
            f"Loaded DPAPI-protected cached token "
            f"(valid={status['hasToken']}, exp={status['tokenExpiresOn'] or 'unknown'}).",
            flush=True,
        )
    app = create_app(relay_store=relay_store, relay_ingest_secret=relay_ingest_secret)

    try:
        async with listen(app, port):
            print(f"Messages endpoint: http://127.0.0.1:{port}/v1/messages", flush=True)
            print(
                "Local programs need no API key. Copilot App requests refresh the upstream token.",
                flush=True,
            )
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
