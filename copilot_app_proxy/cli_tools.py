"""Operational helpers for status, diagnostics (doctor), key management, and VS Code config."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from copilot_app_proxy.dpapi import DpapiError, load_protected_text, save_protected_text

DEFAULT_GATEWAY_PORT = 19324
DEFAULT_PROXY_PORT = 19323
DEFAULT_MODEL_ID = "claude-opus-5.5"


def repo_local_dir(base_dir: Path | None = None) -> Path:
    return (base_dir or Path.cwd()) / ".local"


def is_pid_alive(pid: int) -> bool:
    """Check if a process ID is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if a TCP socket is accepting connections on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def find_copilot_executable() -> Path | None:
    """Locate copilotapp.exe from standard Windows installation paths."""
    candidates: list[Path] = []
    for env_var, rel in (
        ("ProgramFiles(x86)", Path("Microsoft/Copilot/Application/copilotapp.exe")),
        ("ProgramFiles", Path("Microsoft/Copilot/Application/copilotapp.exe")),
        ("LocalAppData", Path("Microsoft/Copilot/Application/copilotapp.exe")),
        ("LocalAppData", Path("Programs/Microsoft Copilot/copilotapp.exe")),
    ):
        root = os.environ.get(env_var)
        if root:
            candidates.append(Path(root) / rel)
    for path in candidates:
        if path.is_file():
            return path
    return None


def fetch_healthz(port: int) -> dict[str, Any] | None:
    """Query local gateway /healthz without using any system proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return None


def read_pids(local_dir: Path) -> dict[str, Any]:
    pids_file = local_dir / "pids.json"
    if not pids_file.is_file():
        return {}
    try:
        data = json.loads(pids_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def check_ca_trusted(local_dir: Path) -> tuple[bool, str | None]:
    """Check if the recorded temporary CA thumbprint is trusted in Cert:\\CurrentUser\\Root."""
    thumb_file = local_dir / "ca-thumbprint.txt"
    if not thumb_file.is_file():
        return False, None
    thumb = thumb_file.read_text(encoding="ascii", errors="ignore").strip()
    if not thumb or sys.platform != "win32":
        return False, thumb or None
    res = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Test-Path -LiteralPath 'Cert:\\CurrentUser\\Root\\{thumb}'",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return res.returncode == 0 and res.stdout.strip().lower() == "true", thumb


def ensure_gateway_key(local_dir: Path) -> tuple[str, bool]:
    """Load existing DPAPI gateway key or generate a new 64-char hex key. Returns (key, created)."""
    key_file = local_dir / "gateway-key.dpapi"
    if key_file.is_file():
        return load_protected_text(key_file).strip(), False
    new_key = secrets.token_hex(32)
    save_protected_text(key_file, new_key)
    return new_key, True


def copy_to_clipboard(text: str) -> None:
    """Copy text to Windows clipboard without echoing it."""
    if sys.platform != "win32":
        raise RuntimeError("Clipboard copy is only supported on Windows.")
    subprocess.run(
        ["clip.exe"],
        input=text,
        text=True,
        check=True,
    )


def build_vscode_config(port: int = DEFAULT_GATEWAY_PORT, models: list[str] | None = None) -> list[dict[str, Any]]:
    """Build VS Code Custom Endpoint JSON configuration."""
    model_ids = models or [DEFAULT_MODEL_ID]
    return [
        {
            "name": "本机 Copilot App 代理",
            "vendor": "customendpoint",
            "apiKey": "${input:gatewayCredential}",
            "models": [
                {
                    "id": model_id,
                    "name": f"{model_id} (本机代理)",
                    "url": f"http://127.0.0.1:{port}/v1/messages",
                    "apiType": "messages",
                    "toolCalling": True,
                    "vision": False,
                    "contextWindow": 1000000,
                    "maxOutputTokens": 32768,
                }
                for model_id in model_ids
            ],
        }
    ]


def run_status(port: int | None = None, base_dir: Path | None = None) -> int:
    """Print human-friendly proxy and token status."""
    local_dir = repo_local_dir(base_dir)
    pids = read_pids(local_dir)
    gw_port = port or int(pids.get("gatewayPort") or DEFAULT_GATEWAY_PORT)
    proxy_port = int(pids.get("proxyPort") or DEFAULT_PROXY_PORT)

    gw_pid = pids.get("gatewayPid")
    mitm_pid = pids.get("mitmPid")
    gw_alive = isinstance(gw_pid, int) and is_pid_alive(gw_pid)
    mitm_alive = isinstance(mitm_pid, int) and is_pid_alive(mitm_pid)
    health = fetch_healthz(gw_port)
    ca_ok, thumb = check_ca_trusted(local_dir)

    print("=== Copilot App Proxy Status ===")
    print(f"Gateway (127.0.0.1:{gw_port}): {'RUNNING' if health else 'STOPPED'}"
          + (f" (PID {gw_pid})" if gw_alive else ""))
    print(f"MITM Proxy (127.0.0.1:{proxy_port}): {'RUNNING' if (mitm_alive or is_port_listening(proxy_port)) else 'STOPPED'}"
          + (f" (PID {mitm_pid})" if mitm_alive else ""))
    print(f"Temp CA Trusted: {'YES' if ca_ok else 'NO'}" + (f" ({thumb[:12]}...)" if thumb else ""))
    if health:
        authed = health.get("upstreamAuthenticated", False)
        exp = health.get("tokenExpiresOn") or "unknown"
        ttl = health.get("expiresInSeconds")
        ttl_str = f" ({ttl}s remaining)" if isinstance(ttl, int) else ""
        print(f"Upstream Token: {'READY' if authed else 'MISSING/EXPIRED'} (exp={exp}{ttl_str})")
        if not authed:
            print("  -> 在 Copilot App 的 Code 页发送一条消息即可自动捕获令牌。")
    else:
        print("  -> 运行 .\\start-transparent-proxy.ps1 -CopyKey 启动代理。")
    return 0 if health else 1


def run_doctor(
    gateway_port: int = DEFAULT_GATEWAY_PORT,
    proxy_port: int = DEFAULT_PROXY_PORT,
    base_dir: Path | None = None,
) -> int:
    """Run diagnostic checks and print actionable remediation steps."""
    root = base_dir or Path.cwd()
    local_dir = repo_local_dir(root)
    issues = 0

    def ok(label: str, detail: str) -> None:
        print(f"[OK]   {label}: {detail}")

    def warn(label: str, detail: str) -> None:
        nonlocal issues
        issues += 1
        print(f"[WARN] {label}: {detail}")

    ok("Python", f"{sys.version.split()[0]} ({sys.executable})")

    mitmdump = root / ".venv" / "Scripts" / "mitmdump.exe"
    if mitmdump.is_file():
        ok("mitmdump", str(mitmdump))
    else:
        warn("mitmdump", "未找到 .venv\\Scripts\\mitmdump.exe，请先运行 .\\install.ps1")

    copilot_exe = find_copilot_executable()
    if copilot_exe:
        ok("Copilot App", str(copilot_exe))
    else:
        warn("Copilot App", "未在标准目录找到 copilotapp.exe，启动时请传入 -CopilotPath")

    if gateway_port == proxy_port:
        warn("Ports", f"GatewayPort ({gateway_port}) 与 ProxyPort ({proxy_port}) 不能相同")
    else:
        gw_health = fetch_healthz(gateway_port)
        if gw_health:
            ok("Gateway Port", f"127.0.0.1:{gateway_port} 正在运行 (healthy)")
        elif is_port_listening(gateway_port):
            warn("Gateway Port", f"127.0.0.1:{gateway_port} 已被其他进程占用")
        else:
            ok("Gateway Port", f"127.0.0.1:{gateway_port} 空闲可用")

        pids = read_pids(local_dir)
        mitm_alive = isinstance(pids.get("mitmPid"), int) and is_pid_alive(pids["mitmPid"])
        if is_port_listening(proxy_port):
            if mitm_alive:
                ok("Proxy Port", f"127.0.0.1:{proxy_port} 正在运行 (PID {pids['mitmPid']})")
            else:
                warn("Proxy Port", f"127.0.0.1:{proxy_port} 已被其他进程占用")
        else:
            ok("Proxy Port", f"127.0.0.1:{proxy_port} 空闲可用")

    key_file = local_dir / "gateway-key.dpapi"
    if key_file.is_file():
        try:
            load_protected_text(key_file)
            ok("Gateway Key", "已存在且 DPAPI 解密正常")
        except DpapiError:
            warn("Gateway Key", ".local\\gateway-key.dpapi 无法解密，请删除后重新启动")
    else:
        ok("Gateway Key", "尚未生成（首次启动时自动生成）")

    logs_dir = local_dir / "logs"
    for log_name in ("gateway.log", "mitm.log"):
        log_path = logs_dir / log_name
        if log_path.is_file() and log_path.stat().st_size > 0:
            ok(f"Log ({log_name})", str(log_path))

    if issues == 0:
        print("\n所有检查通过。")
    else:
        print(f"\n发现 {issues} 个需要注意的项目，请按上述提示处理。")
    return 0 if issues == 0 else 1


def run_key(copy: bool = True, base_dir: Path | None = None) -> int:
    """Ensure gateway key exists and copy it to clipboard."""
    local_dir = repo_local_dir(base_dir)
    key, created = ensure_gateway_key(local_dir)
    if copy:
        copy_to_clipboard(key)
        action = "已生成并复制" if created else "已复制"
        print(f"{action}本机网关密钥到剪贴板。")
    else:
        action = "已生成" if created else "已就绪"
        print(f"本机网关密钥{action}（使用 --copy 复制到剪贴板）。")
    return 0


def run_vscode_config(port: int = DEFAULT_GATEWAY_PORT) -> int:
    """Print VS Code Custom Endpoint configuration JSON."""
    config = build_vscode_config(port=port)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0
