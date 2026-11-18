"""Tests for Windows DPAPI encryption helpers."""

from __future__ import annotations

import subprocess
import sys

import pytest

from copilot_app_proxy.dpapi import (
    DpapiError,
    load_protected_text,
    protect_text,
    save_protected_text,
    unprotect_text,
)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_roundtrip_and_file_persistence(tmp_path):
    secret = "synthetic-dpapi-secret-value-12345"
    encrypted = protect_text(secret)
    assert secret not in encrypted
    assert unprotect_text(encrypted) == secret

    dpapi_file = tmp_path / "nested" / "secret.dpapi"
    save_protected_text(dpapi_file, secret)
    raw = dpapi_file.read_text(encoding="utf-8")
    assert secret not in raw
    assert load_protected_text(dpapi_file) == secret


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_compatible_with_powershell_protected_data(tmp_path):
    """Ensure Python DPAPI files can be decrypted by PowerShell ProtectedData and vice versa."""
    secret = "cross-compat-gateway-key-0123456789abcdef"
    dpapi_file = tmp_path / "gateway-key.dpapi"
    save_protected_text(dpapi_file, secret)

    ps_cmd = (
        "Add-Type -AssemblyName System.Security; "
        f"$enc = (Get-Content -Raw -LiteralPath '{dpapi_file}').Trim(); "
        "$bytes = [Security.Cryptography.ProtectedData]::Unprotect("
        "[Convert]::FromBase64String($enc), $null, "
        "[Security.Cryptography.DataProtectionScope]::CurrentUser); "
        "[Console]::Out.Write([Text.Encoding]::UTF8.GetString($bytes))"
    )
    res = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout == secret


def test_dpapi_rejects_empty_or_invalid_payload():
    with pytest.raises(DpapiError):
        protect_text("   ")
    with pytest.raises(DpapiError):
        unprotect_text("   ")
    with pytest.raises(DpapiError):
        unprotect_text("not-valid-base64!!!")
