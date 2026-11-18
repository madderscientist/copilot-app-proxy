"""Start and stop the managed desktop proxy. Protocol handling lives in the gateway."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from copilot_app_proxy import __file__ as core_package_file

from . import ansi, windows
from .config import find_mitmdump_executable, local_dir, read_pids, venv_python, write_pids
from .diagnostics import fetch_healthz, is_port_listening
from .windows import find_copilot_executable, is_pid_alive

ADDON_PATH = Path(core_package_file).resolve().with_name("relay_addon.py")
CLI_MODULE = "copilot_app_proxy.cli"
GATEWAY_MARKER = f"-m {CLI_MODULE} serve --port "
KEEPALIVE_MARKER = f"-m {CLI_MODULE} keepalive "


def _field(label: str, value: str, color: str) -> None:
    print(f"{ansi.paint(f'{label}:'.ljust(9), ansi.BOLD)}{ansi.paint(value, color)}", flush=True)


def _warn(message: str) -> None:
    print(ansi.paint(message, ansi.YELLOW, stream=sys.stderr), file=sys.stderr, flush=True)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Managed desktop start and stop currently require Windows.")


def _kill_tree(pid: int) -> None:
    result = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, timeout=10
    )
    if result.returncode and is_pid_alive(pid):
        raise RuntimeError(f"Could not stop process {pid}.")


def _stop_workers(state: dict[str, Any]) -> None:
    if not state or (state.keys() == {"copilotPath"} and isinstance(state["copilotPath"], str)):
        return
    expected: dict[int, str] = {}
    for key, marker in (("gatewayPid", GATEWAY_MARKER), ("mitmPid", str(ADDON_PATH).casefold())):
        pid = state.get(key)
        if type(pid) is not int or pid <= 0:
            raise ValueError(f"Invalid {key} in pids.json.")
        expected[pid] = marker
    # Optional; stopped first so it cannot relaunch Copilot App while the proxy goes away.
    if type(pid := state.get("keepalivePid")) is int and pid > 0:
        expected = {pid: KEEPALIVE_MARKER, **expected}
    # A recorded PID may now belong to a different process.
    commands = windows.process_commands(list(expected))
    for pid, marker in expected.items():
        if (command := commands.get(pid)) is None:
            continue
        if marker in command.casefold():
            _kill_tree(pid)
        else:
            print(f"Skipping PID {pid}: no longer matches the proxy process.", file=sys.stderr)


def _app_path(apps: list[windows.AppProcess]) -> Path | None:
    pids = {app.pid for app in apps}
    return (
        next((app.path for app in apps if app.parent_pid not in pids and app.path), None)
        or find_copilot_executable()
    )


def _stop_app(apps: list[windows.AppProcess], timeout: float = 8) -> None:
    pids = {app.pid for app in apps}
    for app in apps:
        if app.parent_pid not in pids:
            _kill_tree(app.pid)
    deadline = time.monotonic() + timeout
    while any(is_pid_alive(pid) for pid in pids):
        if time.monotonic() >= deadline:
            raise RuntimeError("Existing Copilot processes did not exit in time.")
        time.sleep(0.1)


def _launch_app(path: Path, proxy_port: int | None = None) -> None:
    env = os.environ.copy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "RELAY_INGEST_SECRET"):
        env.pop(name, None)
    args = [str(path)]
    if proxy_port is not None:
        proxy = f"http://127.0.0.1:{proxy_port}"
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, NO_PROXY="localhost,127.0.0.1,::1")
        args += [f"--proxy-server={proxy}", "--disable-quic"]
    subprocess.Popen(
        args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _cleanup(local: Path, *, restore: bool, keep_records: bool = False) -> None:
    thumb_file = local / "ca-thumbprint.txt"
    if thumb_file.exists():
        windows.remove_ca(thumb_file.read_text(encoding="ascii").strip())
    ca_dir = local / "mitm-ca"
    if ca_dir.is_dir():
        shutil.rmtree(ca_dir)

    if keep_records:
        print("Run 'copilot-proxy stop' to restore the previous session.", file=sys.stderr)
        return
    if restore:
        apps = windows.copilot_processes()
        recorded = read_pids(local).get("copilotPath")
        app = Path(recorded) if isinstance(recorded, str) else None
        if app is None or not app.is_file():
            app = _app_path(apps)
        if app is None or not app.is_file():
            raise FileNotFoundError("Cannot restore Copilot App: copilotapp.exe not found.")
        # Keep the executable path for retry even after the process has exited.
        write_pids(local, copilotPath=str(app.resolve()))
        _stop_app(apps)
        _launch_app(app)
        print(ansi.paint("Copilot App restored without the proxy.", ansi.GREEN))
    thumb_file.unlink(missing_ok=True)
    (local / "pids.json").unlink(missing_ok=True)


def _read_runtime_state(local: Path) -> dict[str, Any]:
    state = read_pids(local)
    if not state and (record := local / "pids.json").exists():
        raise ValueError(f"Cannot read process records from {record}.")
    return state


def _launch_worker(
    args: list[str], root: Path, name: str, extra_env: dict[str, str], *, own_log: bool = False
) -> subprocess.Popen[bytes]:
    """Start a worker writing to .local/logs/<name>.log and <name>.err.log.

    With ``own_log`` the worker writes and rotates <name>.log itself, so both of its
    standard streams go to <name>.err.log.
    """
    logs = local_dir(root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(extra_env)
    err_path = logs / f"{name}.err.log"
    out_path = err_path if own_log else logs / f"{name}.log"
    for path in {out_path, err_path}:
        path.write_bytes(b"")
    # Use files instead of pipes so workers keep logging after the launcher exits.
    with out_path.open("ab") as out, err_path.open("ab") as err:
        return subprocess.Popen(
            args, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def _wait_ready(
    workers: dict[str, subprocess.Popen[bytes]], gateway_port: int, proxy_port: int, cert: Path
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for name, process in workers.items():
            code = process.poll()
            if code is not None:
                raise RuntimeError(
                    f"{name} exited with code {code}. See .local\\logs\\{name}.err.log."
                )
        if cert.is_file() and is_port_listening(proxy_port) and fetch_healthz(gateway_port):
            return
        time.sleep(0.25)
    raise RuntimeError("Gateway, proxy or temporary CA not ready. See .local\\logs.")


def start(
    root: Path,
    gateway_port: int,
    proxy_port: int,
    *,
    mitmproxy_path: Path | None = None,
    copilot_path: Path | None = None,
    observe: bool = False,
    foreground: bool = False,
    keepalive: bool = True,
) -> int:
    """Start the proxy. With ``foreground`` stay attached until Ctrl+C, then run `stop`."""
    _require_windows()
    if gateway_port == proxy_port:
        raise ValueError("Gateway and proxy ports must be different.")
    with windows.lifecycle_lock(local_dir(root)):
        _start_locked(root, gateway_port, proxy_port, mitmproxy_path, copilot_path, observe)
    _field("Gateway", f"http://127.0.0.1:{gateway_port}/v1/messages", ansi.CYAN)
    _field("Proxy", f"http://127.0.0.1:{proxy_port}", ansi.CYAN)
    # The first Code tab visit after launch makes Copilot App fetch models with its token.
    if windows.select_code_tab():
        _report_token(gateway_port)
    else:
        _warn("Open Copilot App's Code tab to capture a token.")
    # Started only now, so it does not mistake the token still being captured for a missing one.
    if keepalive:
        try:
            _start_keepalive(root, gateway_port)
        except (OSError, RuntimeError) as exc:
            _warn(f"Keep-alive not started: {exc}")
    if not foreground:
        print(ansi.paint("Running in the background. Run 'copilot-proxy stop' to stop.", ansi.DIM))
        return 0
    from .foreground import run_foreground

    return run_foreground(root, gateway_port)


def _cli_command(root: Path, *args: str) -> list[str]:
    python = venv_python(root)
    return [str(python) if python.is_file() else sys.executable, "-m", CLI_MODULE, *args]


def _start_keepalive(root: Path, gateway_port: int) -> None:
    local = local_dir(root)
    args = _cli_command(root, "keepalive", "--gateway-port", str(gateway_port))
    with windows.lifecycle_lock(local):
        state = read_pids(local)
        if not is_pid_alive(state.get("gatewayPid", 0)):
            raise RuntimeError("Proxy stopped during startup.")
        worker = _launch_worker(args, root, "keepalive", {})
        write_pids(local, **state, keepalivePid=worker.pid)


def _report_token(gateway_port: int, timeout: float = 20) -> None:
    # Wait briefly so keep-alive does not mistake a token still in flight for a missing one.
    deadline = time.monotonic() + timeout
    while True:
        health = fetch_healthz(gateway_port) or {}
        if health.get("upstreamAuthenticated"):
            _field("Token", f"ready (exp={health.get('tokenExpiresOn') or 'unknown'})", ansi.GREEN)
            return
        if time.monotonic() >= deadline:
            _field("Token", "not captured yet; open Copilot App's Code tab.", ansi.YELLOW)
            return
        time.sleep(1)


def _start_locked(
    root: Path,
    gateway_port: int,
    proxy_port: int,
    mitmproxy_path: Path | None,
    copilot_path: Path | None,
    observe: bool,
) -> None:
    mitm = mitmproxy_path or find_mitmdump_executable(root)
    if mitm is None or not mitm.is_file():
        raise FileNotFoundError(
            "mitmdump.exe not found. Run .\\install.ps1 or pass --mitmproxy-path."
        )
    apps = windows.copilot_processes()
    app = copilot_path or _app_path(apps)
    if app is None or not app.is_file():
        raise FileNotFoundError("copilotapp.exe not found. Open Copilot or pass --copilot-path.")

    app = app.resolve()
    local = local_dir(root)
    state = _read_runtime_state(local)
    thumb_file = local / "ca-thumbprint.txt"
    previous_session = bool(state)
    _stop_workers(state)
    for port in (gateway_port, proxy_port):
        if is_port_listening(port):
            raise RuntimeError(f"Port {port} is already in use.")
    # Per-run secret so only this session's mitmproxy addon can push tokens to the gateway.
    secret = secrets.token_hex(32)

    ca_dir = local / "mitm-ca"
    ca_dir.mkdir(parents=True, exist_ok=True)
    cert = ca_dir / "mitmproxy-ca-cert.cer"
    workers: dict[str, subprocess.Popen[bytes]] = {}
    gateway_args = _cli_command(
        root, "serve", "--port", str(gateway_port),
        "--log-file", str(local / "logs" / "gateway.log"),
    )
    mitm_env = {
        "RELAY_INGEST_URL": f"http://127.0.0.1:{gateway_port}/_internal/token",
        "RELAY_INGEST_SECRET": secret,
    }
    if observe:
        mitm_env["RELAY_OBSERVE_LOG"] = str(local / "logs" / "observe.jsonl")
    restart_needed = False
    try:
        workers["gateway"] = _launch_worker(
            gateway_args, root, "gateway", {"RELAY_INGEST_SECRET": secret}, own_log=True
        )
        workers["mitm"] = _launch_worker(
            [
                str(mitm.resolve()),
                "--listen-host", "127.0.0.1",
                "--listen-port", str(proxy_port),
                "--set", f"confdir={ca_dir}",
                "--set", "flow_detail=0",
                "--set", "termlog_verbosity=error",
                "--scripts", str(ADDON_PATH),
            ],
            root,
            "mitm",
            mitm_env,
        )
        write_pids(
            local,
            gatewayPid=workers["gateway"].pid,
            mitmPid=workers["mitm"].pid,
            gatewayPort=gateway_port,
            proxyPort=proxy_port,
            copilotPath=str(app),
        )
        _wait_ready(workers, gateway_port, proxy_port, cert)
        thumb = windows.ca_thumbprint(cert)
        thumb_file.write_text(thumb, encoding="ascii")
        windows.trust_ca(cert, thumb)
        restart_needed = bool(apps)
        _stop_app(apps)
        _launch_app(app, proxy_port)
    except BaseException:
        try:
            for worker in workers.values():
                if worker.poll() is None:
                    _kill_tree(worker.pid)
            _cleanup(
                local, restore=restart_needed, keep_records=previous_session and not restart_needed
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(
                f"Startup cleanup failed: {exc}. Run 'copilot-proxy stop' to retry.",
                file=sys.stderr,
            )
        raise


def _restore_quickly(root: Path) -> None:
    """Best-effort restore within the ~5 s Windows allows when the console is closed.

    Copilot App is relaunched without the proxy first so it keeps working; the trusted CA
    and process records stay for a later `stop` to finish, since CA removal needs a prompt.
    """
    state = read_pids(local_dir(root))
    try:
        app = Path(state["copilotPath"])
        _stop_app(windows.copilot_processes(), timeout=2)
        _launch_app(app)
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError):
        pass
    for key in ("keepalivePid", "gatewayPid", "mitmPid"):
        if type(pid := state.get(key)) is int and pid > 0:
            try:
                _kill_tree(pid)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass


def _stop_with_retry(root: Path, timeout: float = 30) -> int:
    # A keep-alive relaunch may hold the lifecycle lock for a few seconds.
    deadline = time.monotonic() + timeout
    while True:
        try:
            return stop(root)
        except RuntimeError as exc:
            if "in progress" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(1)


def stop(root: Path) -> int:
    _require_windows()
    local = local_dir(root)
    with windows.lifecycle_lock(local):
        state = _read_runtime_state(local)
        _stop_workers(state)
        _cleanup(local, restore=bool(state))
    print(ansi.paint("Proxy stopped and temporary CA removed.", ansi.GREEN))
    return 0
