"""Tests for CLI operational tools (status, doctor, key, vscode-config)."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from copilot_app_proxy.cli_tools import (
    build_vscode_config,
    ensure_gateway_key,
    is_pid_alive,
    run_doctor,
    run_status,
)


def test_build_vscode_config_uses_configured_port_and_models():
    cfg = build_vscode_config(port=20000, models=["claude-opus-5.5", "gpt-5"])
    assert len(cfg) == 1
    assert cfg[0]["apiKey"] == "${input:gatewayCredential}"
    models = cfg[0]["models"]
    assert [m["id"] for m in models] == ["claude-opus-5.5", "gpt-5"]
    assert all(m["url"] == "http://127.0.0.1:20000/v1/messages" for m in models)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_ensure_gateway_key_creates_and_reuses_dpapi_key(tmp_path):
    local_dir = tmp_path / ".local"
    key1, created1 = ensure_gateway_key(local_dir)
    assert created1 is True
    assert len(key1) == 64

    key2, created2 = ensure_gateway_key(local_dir)
    assert created2 is False
    assert key2 == key1


def test_is_pid_alive_checks_current_and_invalid_pids():
    import os

    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(-1) is False
    assert is_pid_alive(0) is False


def test_run_status_reports_stopped_when_not_running(tmp_path, capsys):
    with patch("copilot_app_proxy.cli_tools.fetch_healthz", return_value=None):
        code = run_status(port=19999, base_dir=tmp_path)
    assert code == 1
    out = capsys.readouterr().out
    assert "STOPPED" in out


def test_run_status_reports_ready_when_healthy(tmp_path, capsys):
    local_dir = tmp_path / ".local"
    local_dir.mkdir(parents=True)
    (local_dir / "pids.json").write_text(
        json.dumps({"gatewayPort": 19324, "proxyPort": 19323}), encoding="utf-8"
    )
    health = {
        "status": "ok",
        "upstreamAuthenticated": True,
        "tokenExpiresOn": "2030-01-01T00:00:00Z",
        "expiresInSeconds": 3600,
    }
    with (
        patch("copilot_app_proxy.cli_tools.fetch_healthz", return_value=health),
        patch("copilot_app_proxy.cli_tools.check_ca_trusted", return_value=(True, "AABBCC")),
    ):
        code = run_status(base_dir=tmp_path)
    assert code == 0
    out = capsys.readouterr().out
    assert "RUNNING" in out
    assert "READY" in out


def test_run_doctor_flags_same_ports(tmp_path, capsys):
    code = run_doctor(gateway_port=19324, proxy_port=19324, base_dir=tmp_path)
    assert code == 1
    out = capsys.readouterr().out
    assert "不能相同" in out
