"""Renew the relayed token by having Copilot App fetch it again through UI automation.

Copilot App requests `/api/v1/llmapi/models` only when the Code tab is first opened after
launch, so a renewal relaunches the app behind the running proxy and selects that tab. The
app decides whether to reuse its cached token or get a new one; nothing here reads its
credentials.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import windows
from .config import local_dir, read_pids
from .diagnostics import fetch_healthz
from .launcher import _launch_app, _require_windows, _stop_app
from .windows import is_pid_alive

DEFAULT_LEAD_SECONDS = 240
TAB_TIMEOUT_SECONDS = 60
TOKEN_WAIT_SECONDS = 45
# Consecutive failed health checks, this far apart, mean the gateway has stopped.
GATEWAY_GONE_CHECKS = 3
GATEWAY_RECHECK_SECONDS = 10


def _log(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


def _token_expiry(gateway_port: int) -> str | None:
    health = fetch_healthz(gateway_port)
    return health.get("tokenExpiresOn") if health else None


def refresh_once(root: Path, gateway_port: int) -> bool:
    """Relaunch Copilot App through the proxy; return True if the gateway got a newer token."""
    _require_windows()
    local = local_dir(root)
    with windows.lifecycle_lock(local):
        state = read_pids(local)
        proxy_port = state.get("proxyPort")
        mitm_pid = state.get("mitmPid")
        app = Path(state["copilotPath"]) if isinstance(state.get("copilotPath"), str) else None
        if type(proxy_port) is not int or type(mitm_pid) is not int or not is_pid_alive(mitm_pid):
            raise RuntimeError("Proxy is not running. Run 'copilot-proxy start' first.")
        if fetch_healthz(gateway_port) is None:
            raise RuntimeError(f"Gateway is not responding on port {gateway_port}.")
        if app is None or not app.is_file():
            raise FileNotFoundError("copilotapp.exe recorded by 'start' was not found.")
        before = _token_expiry(gateway_port)
        _stop_app(windows.copilot_processes())
        _launch_app(app, proxy_port)

    if not windows.select_code_tab(TAB_TIMEOUT_SECONDS):
        _log("Copilot App's Code tab was not found; open it manually to capture a token.")
        return False
    deadline = time.monotonic() + TOKEN_WAIT_SECONDS
    while time.monotonic() < deadline:
        after = _token_expiry(gateway_port)
        if after and after != before:
            _log(f"Token renewed: exp={after}.")
            return True
        time.sleep(1)
    _log(f"Copilot App reused its cached token (exp={before}).")
    return False


def run_keepalive(
    root: Path,
    gateway_port: int,
    *,
    lead: int,
    retry_interval: int = 60,
    max_attempts: int = 5,
) -> int:
    """Watch token expiry and relaunch Copilot App shortly before it runs out."""
    _require_windows()
    _log(f"Started: relaunches Copilot App when the token has under {lead}s left.")
    failures = 0
    unreachable = 0
    while True:
        health = fetch_healthz(gateway_port)
        if health is None:
            # Exit with the gateway so an orphaned keep-alive never relaunches Copilot App.
            unreachable += 1
            if unreachable >= GATEWAY_GONE_CHECKS:
                _log(f"Gateway on port {gateway_port} is not running; keep-alive exits.")
                return 0
            time.sleep(GATEWAY_RECHECK_SECONDS)
            continue
        unreachable = 0
        ttl = health.get("expiresInSeconds")
        usable = bool(health.get("upstreamAuthenticated"))
        if usable and isinstance(ttl, int) and ttl > lead:
            failures = 0
            time.sleep(min(60, ttl - lead))
            continue
        if failures >= max_attempts:
            _log(f"Gave up after {failures} attempts without a new token.")
            return 1
        failures += 1
        _log(f"Token has {ttl}s left; relaunching Copilot App (attempt {failures}).")
        try:
            renewed = refresh_once(root, gateway_port)
        except (OSError, RuntimeError) as exc:
            _log(f"Renewal failed: {exc}")
            renewed = False
        if renewed:
            failures = 0
        else:
            time.sleep(retry_interval)
