"""Tests for CLI operational tools (status, doctor, key, vscode-config)."""

from __future__ import annotations

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


@pytest.mark.parametrize(("running", "exit_code"), [(False, 1), (True, 0)])
def test_run_status_reports_gateway_state(tmp_path, capsys, running, exit_code):
    local_dir = tmp_path / ".local"
    local_dir.mkdir(parents=True)
    (local_dir / "pids.json").write_text('{"gatewayPort": 19999}', encoding="utf-8")
    health = {"upstreamAuthenticated": True} if running else None
    with patch("copilot_app_proxy.cli_tools.fetch_healthz", return_value=health):
        assert run_status(base_dir=tmp_path) == exit_code
    out = capsys.readouterr().out
    state = "RUNNING" if running else "STOPPED"
    assert f"Gateway (127.0.0.1:19999): {state}" in out
    assert ("Upstream Token: READY" in out) is running


def test_run_doctor_flags_same_ports(tmp_path, capsys):
    code = run_doctor(gateway_port=19324, proxy_port=19324, base_dir=tmp_path)
    assert code == 1
    out = capsys.readouterr().out
    assert "不能相同" in out
