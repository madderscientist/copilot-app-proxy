"""Read-only checks behind `status`, `doctor`, and `vscode-config`."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import windows
from .config import active_ports, find_mitmdump_executable, local_dir, read_pids

DEFAULT_MODEL_ID = "claude-opus-5.5"


def fetch_healthz(port: int) -> dict[str, Any] | None:
    """Query the local gateway's /healthz without going through any system proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return None


def is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def check_ca_trusted(local: Path) -> tuple[bool, str | None]:
    """Return whether the recorded temporary CA is in the current user's Root store."""
    try:
        thumb = (local / "ca-thumbprint.txt").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return False, None
    if not thumb or sys.platform != "win32":
        return False, thumb or None
    try:
        return windows.ca_is_trusted(thumb), thumb
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False, thumb


def build_vscode_config(port: int) -> list[dict[str, Any]]:
    return [
        {
            "name": "本机 Copilot App 代理",
            "vendor": "customendpoint",
            # The gateway ignores caller credentials; VS Code only requires a non-empty value.
            "apiKey": "local",
            "models": [
                {
                    "id": DEFAULT_MODEL_ID,
                    "name": f"{DEFAULT_MODEL_ID} (本机代理)",
                    "url": f"http://127.0.0.1:{port}/v1/messages",
                    "apiType": "messages",
                    "toolCalling": True,
                    "vision": False,
                    "contextWindow": 1000000,
                    "maxOutputTokens": 32768,
                }
            ],
        }
    ]


def _alive(pid: Any) -> bool:
    return type(pid) is int and windows.is_pid_alive(pid)


def run_status(root: Path, gateway_port: int | None = None) -> int:
    local = local_dir(root)
    pids = read_pids(local)
    gateway_port, proxy_port = active_ports(root, gateway_port)
    gateway_pid, mitm_pid = pids.get("gatewayPid"), pids.get("mitmPid")
    gateway_alive, mitm_alive = _alive(gateway_pid), _alive(mitm_pid)
    health = fetch_healthz(gateway_port)
    ca_trusted, thumb = check_ca_trusted(local)
    proxy_running = mitm_alive or is_port_listening(proxy_port)

    print("=== Copilot App Proxy Status ===")
    print(
        f"Gateway (127.0.0.1:{gateway_port}): {'RUNNING' if health else 'STOPPED'}"
        + (f" (PID {gateway_pid})" if gateway_alive else "")
    )
    print(
        f"MITM Proxy (127.0.0.1:{proxy_port}): {'RUNNING' if proxy_running else 'STOPPED'}"
        + (f" (PID {mitm_pid})" if mitm_alive else "")
    )
    print(
        f"Temp CA Trusted: {'YES' if ca_trusted else 'NO'}"
        + (f" ({thumb[:12]}...)" if thumb else "")
    )
    if health:
        authenticated = health.get("upstreamAuthenticated", False)
        expires = health.get("tokenExpiresOn") or "unknown"
        ttl = health.get("expiresInSeconds")
        remaining = f" ({ttl}s remaining)" if isinstance(ttl, int) else ""
        state = "READY" if authenticated else "MISSING/EXPIRED"
        print(f"Upstream Token: {state} (exp={expires}{remaining})")
        if not authenticated:
            print("  -> 在 Copilot App 的 Code 页发送一条消息即可自动捕获令牌。")
    else:
        print("  -> 运行 copilot-proxy start 启动代理。")
    return 0 if health else 1


def run_doctor(root: Path, gateway_port: int, proxy_port: int) -> int:
    local = local_dir(root)
    issues = 0

    def ok(label: str, detail: str) -> None:
        print(f"[OK]   {label}: {detail}")

    def warn(label: str, detail: str) -> None:
        nonlocal issues
        issues += 1
        print(f"[WARN] {label}: {detail}")

    ok("Python", f"{sys.version.split()[0]} ({sys.executable})")

    mitmdump = find_mitmdump_executable(root)
    if mitmdump:
        ok("mitmdump", str(mitmdump))
    else:
        warn("mitmdump", "未找到 mitmdump，请先运行 .\\install.ps1")

    copilot_exe = windows.find_copilot_executable()
    if copilot_exe:
        ok("Copilot App", str(copilot_exe))
    else:
        warn("Copilot App", "未在标准目录找到 copilotapp.exe，启动时请传入 --copilot-path")

    if gateway_port == proxy_port:
        warn("Ports", f"网关端口与代理端口不能相同（均为 {gateway_port}）")
    else:
        if fetch_healthz(gateway_port):
            ok("Gateway Port", f"127.0.0.1:{gateway_port} 正在运行 (healthy)")
        elif is_port_listening(gateway_port):
            warn("Gateway Port", f"127.0.0.1:{gateway_port} 已被其他进程占用")
        else:
            ok("Gateway Port", f"127.0.0.1:{gateway_port} 空闲可用")

        mitm_pid = read_pids(local).get("mitmPid")
        if not is_port_listening(proxy_port):
            ok("Proxy Port", f"127.0.0.1:{proxy_port} 空闲可用")
        elif _alive(mitm_pid):
            ok("Proxy Port", f"127.0.0.1:{proxy_port} 正在运行 (PID {mitm_pid})")
        else:
            warn("Proxy Port", f"127.0.0.1:{proxy_port} 已被其他进程占用")

    for log_name in ("gateway.log", "mitm.log"):
        log_path = local / "logs" / log_name
        if log_path.is_file() and log_path.stat().st_size > 0:
            ok(f"Log ({log_name})", str(log_path))

    if issues == 0:
        print("\n所有检查通过。")
    else:
        print(f"\n发现 {issues} 个需要注意的项目，请按上述提示处理。")
    return 0 if issues == 0 else 1


def run_vscode_config(gateway_port: int) -> int:
    print(json.dumps(build_vscode_config(gateway_port), ensure_ascii=False, indent=2))
    return 0
