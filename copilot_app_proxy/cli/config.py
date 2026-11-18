"""Project paths, configured ports, and the runtime record written by `start`."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

DEFAULT_GATEWAY_PORT = 19324
DEFAULT_PROXY_PORT = 19323


def project_root() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    return source_root if (source_root / "pyproject.toml").is_file() else Path.cwd()


def local_dir(root: Path) -> Path:
    """Machine-local state: token cache, runtime record, logs, and the temporary CA."""
    return root / ".local"


def venv_python(root: Path) -> Path:
    executable = "Scripts\\python.exe" if sys.platform == "win32" else "bin/python"
    return root / ".venv" / executable


def find_mitmdump_executable(root: Path) -> Path | None:
    name = "mitmdump.exe" if sys.platform == "win32" else "mitmdump"
    for path in (venv_python(root).with_name(name), Path(sys.executable).with_name(name)):
        if path.is_file():
            return path
    found = shutil.which(name)
    return Path(found) if found else None


def resolve_ports(
    root: Path, gateway_port: int | None = None, proxy_port: int | None = None
) -> tuple[int, int]:
    """Return explicit ports, falling back to config.json and then the defaults."""
    path = root / "config.json"
    settings = {}
    if (gateway_port is None or proxy_port is None) and path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: invalid JSON.") from exc
        if not isinstance(settings, dict):
            raise ValueError(f"{path}: expected a JSON object.")

    def resolve(name: str, override: int | None, default: int) -> int:
        value = settings.get(name, default) if override is None else override
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
            raise ValueError(f"{name} must be an integer between 1 and 65535.")
        return value

    return (
        resolve("gateway_port", gateway_port, DEFAULT_GATEWAY_PORT),
        resolve("proxy_port", proxy_port, DEFAULT_PROXY_PORT),
    )


def active_ports(root: Path, gateway_port: int | None = None) -> tuple[int, int]:
    """Like resolve_ports, but prefer the ports recorded by a running session."""
    state = read_pids(local_dir(root))
    if gateway_port is None:
        gateway_port = state.get("gatewayPort")
    return resolve_ports(root, gateway_port, state.get("proxyPort"))


def read_pids(local: Path) -> dict[str, Any]:
    try:
        # Records written by the former PowerShell launcher start with a UTF-8 BOM.
        data = json.loads((local / "pids.json").read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_pids(local: Path, **state: int | str) -> None:
    temporary = local / "pids.tmp"
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(local / "pids.json")
