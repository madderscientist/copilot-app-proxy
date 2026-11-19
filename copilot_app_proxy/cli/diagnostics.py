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

# Upstream endpoint -> (local gateway path, VS Code apiType), in order of preference.
GATEWAY_APIS = {
    "/v1/messages": ("/v1/messages", "messages"),
    "/responses": ("/v1/responses", "responses"),
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORTS_WITH_NONE = ("none", *EFFORTS)
# Snapshot of /v1/models: (id, name, endpoint, context window, reasoning efforts).
# Used when the gateway cannot list models (not running or no valid token).
FALLBACK_MODELS = (
    ("claude-opus-5.5", "Claude Opus 5.5", "/v1/messages", 1_000_000, EFFORTS),
    ("claude-opus-5", "Claude Opus 5", "/v1/messages", 1_000_000, EFFORTS),
    ("claude-sonnet-5.5", "Claude Sonnet 5.5", "/v1/messages", 1_000_000, EFFORTS),
    ("claude-sonnet-5", "Claude Sonnet 5", "/v1/messages", 1_000_000, EFFORTS),
    ("claude-haiku-5.5", "Claude Haiku 5.5", "/v1/messages", 1_000_000, EFFORTS),
    ("gpt-5.6-sol", "GPT-5.6 Sol", "/responses", 1_050_000, EFFORTS_WITH_NONE),
    ("gpt-5.6-luna", "GPT-5.6 Luna", "/responses", 1_050_000, EFFORTS_WITH_NONE),
    ("gpt-6-luna", "GPT-6 Luna", "/responses", 1_050_000, EFFORTS_WITH_NONE),
    ("gpt-61-sol", "GPT-6.1 Sol", "/responses", 1_050_000, EFFORTS),
)
DEFAULT_CONTEXT_WINDOW = 1_000_000
# Not reported by /v1/models. Probed: Claude rejects max_tokens above 128000; GPT does not
# validate max_output_tokens, so the same limit is used for it.
MAX_OUTPUT_TOKENS = 128_000
MODELS_TIMEOUT_SECONDS = 15


def _get_local_json(port: int, path: str, timeout: float) -> Any:
    """GET a local gateway endpoint without going through any system proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return None


def fetch_healthz(port: int) -> dict[str, Any] | None:
    health = _get_local_json(port, "/healthz", 1.5)
    return health if isinstance(health, dict) else None


def fetch_models(port: int) -> list[Any] | None:
    """Return the upstream model list via the local gateway, or None if unavailable."""
    payload = _get_local_json(port, "/v1/models", MODELS_TIMEOUT_SECONDS)
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else None


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


def _dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _gateway_endpoint(model: Any) -> str | None:
    """Return the upstream endpoint the gateway can serve this model on, if any."""
    endpoints = _dig(model, "supported_endpoints")
    if not isinstance(_dig(model, "id"), str) or not isinstance(endpoints, list):
        return None
    return next((api for api in GATEWAY_APIS if api in endpoints), None)


def _gateway_models(upstream_models: list[Any] | None) -> list[dict[str, Any]]:
    return [model for model in upstream_models or [] if _gateway_endpoint(model)]


def _fallback_models() -> list[dict[str, Any]]:
    return [
        {
            "id": model_id,
            "name": name,
            "supported_endpoints": [endpoint],
            "capabilities": {
                "limits": {"max_context_window_tokens": context},
                "supports": {"reasoning_effort": list(efforts)},
            },
        }
        for model_id, name, endpoint, context, efforts in FALLBACK_MODELS
    ]


def _vscode_model(port: int, model: dict[str, Any]) -> dict[str, Any]:
    path, api_type = GATEWAY_APIS[_gateway_endpoint(model)]
    name = model.get("name") if isinstance(model.get("name"), str) else model["id"]
    context = _dig(model, "capabilities", "limits", "max_context_window_tokens")
    efforts = _dig(model, "capabilities", "supports", "reasoning_effort")
    entry: dict[str, Any] = {
        "id": model["id"],
        "name": f"{name} (本机代理)",
        "url": f"http://127.0.0.1:{port}{path}",
        "apiType": api_type,
        "toolCalling": True,
        # Not reported by /v1/models; base64 image input was verified on both APIs.
        "vision": True,
        "contextWindow": context if type(context) is int and context > 0 else DEFAULT_CONTEXT_WINDOW,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
    }
    if api_type == "messages":
        # The upstream only accepts thinking.type "adaptive"; budget-based thinking is rejected.
        entry.update(thinking=True, adaptiveThinking=True)
    if isinstance(efforts, list) and efforts and all(isinstance(e, str) for e in efforts):
        entry["supportsReasoningEffort"] = efforts
    return entry


def build_vscode_config(
    port: int, upstream_models: list[Any] | None = None
) -> list[dict[str, Any]]:
    models = _gateway_models(upstream_models) or _fallback_models()
    return [
        {
            "name": "本机 Copilot App 代理",
            "vendor": "customendpoint",
            # The gateway ignores caller credentials; VS Code only requires a non-empty value.
            "apiKey": "local",
            "models": [_vscode_model(port, model) for model in models],
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
    keepalive_pid = pids.get("keepalivePid")
    if keepalive_pid is not None:
        keepalive_alive = _alive(keepalive_pid)
        print(
            f"Keep-alive: {'RUNNING' if keepalive_alive else 'STOPPED'}"
            + (f" (PID {keepalive_pid})" if keepalive_alive else "")
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
            print("  -> 运行 copilot-proxy refresh，或在 Copilot App 的 Code 页发送一条消息。")
    elif pids or ca_trusted:
        print("  -> 上次运行没有完整清理：运行 copilot-proxy stop 移除临时 CA 并关闭 Copilot App。")
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

    for log_name in ("gateway.log", "mitm.log", "keepalive.log"):
        log_path = local / "logs" / log_name
        if log_path.is_file() and log_path.stat().st_size > 0:
            ok(f"Log ({log_name})", str(log_path))

    if issues == 0:
        print("\n所有检查通过。")
    else:
        print(f"\n发现 {issues} 个需要注意的项目，请按上述提示处理。")
    return 0 if issues == 0 else 1


def run_vscode_config(gateway_port: int) -> int:
    upstream_models = fetch_models(gateway_port)
    if not _gateway_models(upstream_models):
        print(
            "未能通过网关获取模型列表（网关未启动或令牌已过期），已输出内置的模型列表。",
            file=sys.stderr,
        )
    config = build_vscode_config(gateway_port, upstream_models)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0
