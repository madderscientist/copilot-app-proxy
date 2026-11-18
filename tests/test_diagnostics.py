"""Tests for read-only CLI diagnostics (status, doctor, vscode-config)."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from copilot_app_proxy.cli.diagnostics import (
    FALLBACK_MODELS,
    build_vscode_config,
    run_doctor,
    run_status,
)
from copilot_app_proxy.cli.windows import is_pid_alive


def test_build_vscode_config_falls_back_to_builtin_models():
    cfg = build_vscode_config(20000)
    assert len(cfg) == 1
    assert cfg[0]["apiKey"] == "local"
    models = cfg[0]["models"]
    assert [m["id"] for m in models] == [entry[0] for entry in FALLBACK_MODELS]
    assert all(m["supportsReasoningEffort"] for m in models)
    claude = [m for m in models if m["apiType"] == "messages"]
    gpt = [m for m in models if m["apiType"] == "responses"]
    assert len(claude) + len(gpt) == len(models) and claude and gpt
    assert {m["url"] for m in claude} == {"http://127.0.0.1:20000/v1/messages"}
    assert {m["url"] for m in gpt} == {"http://127.0.0.1:20000/v1/responses"}
    assert all(m["adaptiveThinking"] for m in claude)


def test_build_vscode_config_maps_upstream_models_by_endpoint():
    upstream = [
        {
            "id": "claude-x",
            "name": "Claude X",
            "supported_endpoints": ["/v1/messages"],
            "capabilities": {"limits": {"max_context_window_tokens": 200_000}},
        },
        {
            "id": "gpt-x",
            "name": "GPT X",
            "supported_endpoints": ["/responses"],
            "capabilities": {"supports": {"reasoning_effort": ["none", "low"]}},
        },
        {"id": "other-x", "name": "Other X", "supported_endpoints": ["/chat/completions"]},
    ]
    claude, gpt = build_vscode_config(20000, upstream)[0]["models"]
    assert claude["name"] == "Claude X (本机代理)"
    assert claude["apiType"] == "messages"
    assert claude["contextWindow"] == 200_000
    assert claude["adaptiveThinking"] is True
    assert "supportsReasoningEffort" not in claude
    assert gpt["url"] == "http://127.0.0.1:20000/v1/responses"
    assert gpt["apiType"] == "responses"
    assert gpt["supportsReasoningEffort"] == ["none", "low"]
    assert "thinking" not in gpt and "adaptiveThinking" not in gpt


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process API")
def test_is_pid_alive_checks_current_and_invalid_pids():
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(-1) is False
    assert is_pid_alive(0) is False


@pytest.mark.parametrize(("running", "exit_code"), [(False, 1), (True, 0)])
def test_run_status_reports_gateway_state(tmp_path, capsys, running, exit_code):
    local_dir = tmp_path / ".local"
    local_dir.mkdir(parents=True)
    (tmp_path / "config.json").write_text('{"gateway_port": 19998}', encoding="utf-8")
    if running:
        (local_dir / "pids.json").write_text(
            '{"gatewayPort": 19999, "proxyPort": 19323}', encoding="utf-8"
        )
    health = {"upstreamAuthenticated": True} if running else None
    with patch("copilot_app_proxy.cli.diagnostics.fetch_healthz", return_value=health):
        assert run_status(tmp_path) == exit_code
    out = capsys.readouterr().out
    state = "RUNNING" if running else "STOPPED"
    port = 19999 if running else 19998
    assert f"Gateway (127.0.0.1:{port}): {state}" in out
    assert ("Upstream Token: READY" in out) is running


def test_run_doctor_flags_same_ports(tmp_path, capsys):
    assert run_doctor(tmp_path, 19324, 19324) == 1
    assert "不能相同" in capsys.readouterr().out
