"""Windows process queries and current-user certificate operations."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import ssl
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppProcess:
    pid: int
    parent_pid: int
    path: Path | None


@contextmanager
def lifecycle_lock(local: Path) -> Generator[None, None, None]:
    import msvcrt

    local.mkdir(parents=True, exist_ok=True)
    with (local / "lifecycle.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise RuntimeError("Another start or stop is in progress. Try again later.") from exc
        # Windows releases the byte lock on close, even if the launcher exits unexpectedly.
        yield


def is_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    if pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == 259  # STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def find_copilot_executable() -> Path | None:
    """Locate copilotapp.exe in the standard installation directories."""
    for env_var, relative in (
        ("ProgramFiles(x86)", "Microsoft/Copilot/Application/copilotapp.exe"),
        ("ProgramFiles", "Microsoft/Copilot/Application/copilotapp.exe"),
        ("LocalAppData", "Microsoft/Copilot/Application/copilotapp.exe"),
        ("LocalAppData", "Programs/Microsoft Copilot/copilotapp.exe"),
    ):
        base = os.environ.get(env_var)
        if base and (path := Path(base) / relative).is_file():
            return path
    return None


def _powershell(command: str) -> str:
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
            "$ErrorActionPreference = 'Stop'; " + command,
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Windows system query failed.")
    return result.stdout.strip()


def copilot_processes() -> list[AppProcess]:
    # Do not restart applications in another Windows logon session.
    output = _powershell(
        "$session = [Diagnostics.Process]::GetCurrentProcess().SessionId; "
        "ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process "
        "-Filter \"Name = 'copilotapp.exe' AND SessionId = $session\" | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath)"
    )
    return [
        AppProcess(
            row["ProcessId"],
            row["ParentProcessId"],
            Path(row["ExecutablePath"]) if row["ExecutablePath"] else None,
        )
        for row in json.loads(output)
    ]


def process_commands(pids: list[int]) -> dict[int, str]:
    if not pids:
        return {}
    query = " OR ".join(f"ProcessId = {pid:d}" for pid in pids)
    output = _powershell(
        f"ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process -Filter '{query}' "
        "| Select-Object ProcessId,CommandLine)"
    )
    return {row["ProcessId"]: row["CommandLine"] or "" for row in json.loads(output)}


def _ca_path(thumbprint: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9]{40}", thumbprint):
        raise ValueError("Invalid temporary CA thumbprint.")
    return rf"Cert:\CurrentUser\Root\{thumbprint}"


def ca_is_trusted(thumbprint: str) -> bool:
    return _powershell(f"Test-Path -LiteralPath '{_ca_path(thumbprint)}'") == "True"


def ca_thumbprint(cert: Path) -> str:
    data = cert.read_bytes()
    if data.lstrip().startswith(b"-----BEGIN CERTIFICATE-----"):
        data = ssl.PEM_cert_to_DER_cert(data.decode("ascii"))
    # SHA-1 is the Windows certificate store's identifier, not a signature check.
    return hashlib.sha1(data, usedforsecurity=False).hexdigest().upper()


def trust_ca(cert: Path, thumbprint: str) -> None:
    if not ca_is_trusted(thumbprint):
        subprocess.run(["certutil.exe", "-user", "-addstore", "Root", str(cert)], check=True)
        if not ca_is_trusted(thumbprint):
            raise RuntimeError("Temporary CA was not trusted in the current user's store.")


_SELECT_CODE_TAB = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$ae = [Windows.Automation.AutomationElement]
$tabCondition = New-Object Windows.Automation.AndCondition(
    (New-Object Windows.Automation.PropertyCondition($ae::ControlTypeProperty,
        [Windows.Automation.ControlType]::TabItem)),
    (New-Object Windows.Automation.PropertyCondition($ae::NameProperty, 'Code')))
$session = [Diagnostics.Process]::GetCurrentProcess().SessionId
$deadline = (Get-Date).AddSeconds(__TIMEOUT__)
while ((Get-Date) -lt $deadline) {
    foreach ($p in Get-Process copilotapp -ErrorAction SilentlyContinue) {
        if ($p.SessionId -ne $session -or $p.MainWindowHandle -eq [IntPtr]::Zero) { continue }
        try {
            $tab = $ae::FromHandle($p.MainWindowHandle).FindFirst(
                [Windows.Automation.TreeScope]::Descendants, $tabCondition)
            if ($tab) {
                $tab.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
                'selected'; exit 0
            }
        } catch { }
    }
    Start-Sleep -Milliseconds 500
}
exit 1
"""


def select_code_tab(timeout: int = 60) -> bool:
    """Select Copilot App's Code tab through UI Automation once its window is ready."""
    script = _SELECT_CODE_TAB.replace("__TIMEOUT__", str(int(timeout)))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 30,
    )
    return result.returncode == 0 and "selected" in result.stdout


def remove_ca(thumbprint: str) -> None:
    # PowerShell's certificate provider refuses user-root deletions when UI is
    # not allowed; certutil shows the Windows confirmation prompt instead.
    if ca_is_trusted(thumbprint):
        subprocess.run(["certutil.exe", "-user", "-delstore", "Root", thumbprint], check=True)
        if ca_is_trusted(thumbprint):
            raise RuntimeError("Temporary CA is still trusted in the current user's store.")
