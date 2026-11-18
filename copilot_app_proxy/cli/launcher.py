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

from . import windows
from .config import find_mitmdump_executable, local_dir, read_pids, venv_python, write_pids
from .diagnostics import fetch_healthz, is_port_listening
from .windows import find_copilot_executable, is_pid_alive

ADDON_PATH = Path(core_package_file).resolve().with_name("relay_addon.py")
CLI_MODULE = "copilot_app_proxy.cli"
GATEWAY_MARKERS = (
    f"-m {CLI_MODULE} serve --port ",
    # Workers started by earlier versions of this project.
    "-m cli serve --relay --port ",
    "-m copilot_app_proxy ",
)


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
    expected: dict[int, tuple[str, ...]] = {}
    for key, markers in (
        ("gatewayPid", GATEWAY_MARKERS),
        ("mitmPid", (str(ADDON_PATH).casefold(),)),
    ):
        pid = state.get(key)
        if type(pid) is not int or pid <= 0:
            raise ValueError(f"Invalid {key} in pids.json.")
        expected[pid] = markers
    # A recorded PID may now belong to a different process.
    for pid, command in windows.process_commands(list(expected)).items():
        if any(marker in command.casefold() for marker in expected[pid]):
            _kill_tree(pid)
        else:
            print(f"Skipping PID {pid}: no longer matches the proxy process.", file=sys.stderr)


def _app_path(apps: list[windows.AppProcess]) -> Path | None:
    pids = {app.pid for app in apps}
    return (
        next((app.path for app in apps if app.parent_pid not in pids and app.path), None)
        or find_copilot_executable()
    )


def _stop_app(apps: list[windows.AppProcess]) -> None:
    pids = {app.pid for app in apps}
    for app in apps:
        if app.parent_pid not in pids:
            _kill_tree(app.pid)
    deadline = time.monotonic() + 8
    while any(is_pid_alive(pid) for pid in pids):
        if time.monotonic() >= deadline:
            raise RuntimeError("Existing Copilot processes did not exit in time.")
        time.sleep(0.25)


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
        print("Copilot App restored without the proxy.")
    thumb_file.unlink(missing_ok=True)
    (local / "pids.json").unlink(missing_ok=True)


def _read_runtime_state(local: Path) -> dict[str, Any]:
    state = read_pids(local)
    if not state and (record := local / "pids.json").exists():
        raise ValueError(f"Cannot read process records from {record}.")
    return state


def _launch_worker(
    args: list[str], root: Path, name: str, extra_env: dict[str, str]
) -> subprocess.Popen[bytes]:
    logs = local_dir(root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(extra_env)
    # Use files instead of pipes so workers keep logging after the launcher exits.
    with (logs / f"{name}.log").open("wb") as out, (logs / f"{name}.err.log").open("wb") as err:
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
) -> int:
    _require_windows()
    if gateway_port == proxy_port:
        raise ValueError("Gateway and proxy ports must be different.")
    with windows.lifecycle_lock(local_dir(root)):
        return _start_locked(root, gateway_port, proxy_port, mitmproxy_path, copilot_path)


def _start_locked(
    root: Path,
    gateway_port: int,
    proxy_port: int,
    mitmproxy_path: Path | None,
    copilot_path: Path | None,
) -> int:
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
    previous_session = bool(state) or thumb_file.exists()
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
    python = venv_python(root)
    gateway_args = [
        str(python) if python.is_file() else sys.executable,
        "-m", CLI_MODULE, "serve", "--port", str(gateway_port),
    ]
    restart_needed = False
    try:
        workers["gateway"] = _launch_worker(
            gateway_args, root, "gateway", {"RELAY_INGEST_SECRET": secret}
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
            {
                "RELAY_INGEST_URL": f"http://127.0.0.1:{gateway_port}/_internal/token",
                "RELAY_INGEST_SECRET": secret,
            },
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

    print(f"Gateway: http://127.0.0.1:{gateway_port}/v1/messages")
    print(f"Proxy:   http://127.0.0.1:{proxy_port}")
    print("Send a message in Copilot App's Code tab, then run 'copilot-proxy status'.")
    return 0


def stop(root: Path) -> int:
    _require_windows()
    local = local_dir(root)
    with windows.lifecycle_lock(local):
        state = _read_runtime_state(local)
        _stop_workers(state)
        _cleanup(local, restore=bool(state) or (local / "ca-thumbprint.txt").is_file())
    print("Proxy stopped and temporary CA removed.")
    return 0
