"""Tests for UI-automation token renewal."""

from __future__ import annotations

import json
from contextlib import nullcontext

import pytest

from copilot_app_proxy.cli import keepalive


@pytest.fixture
def running(tmp_path, monkeypatch):
    exe = tmp_path / "copilotapp.exe"
    exe.write_bytes(b"")
    local = tmp_path / ".local"
    local.mkdir()
    (local / "pids.json").write_text(
        json.dumps({"gatewayPid": 1, "mitmPid": 2, "proxyPort": 20023, "copilotPath": str(exe)}),
        encoding="ascii",
    )
    calls: list[tuple] = []
    monkeypatch.setattr(keepalive, "_require_windows", lambda: None)
    monkeypatch.setattr(keepalive.windows, "lifecycle_lock", lambda _: nullcontext())
    monkeypatch.setattr(keepalive, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(keepalive.windows, "copilot_processes", lambda: ["app"])
    monkeypatch.setattr(keepalive, "_stop_app", lambda apps: calls.append(("stop", apps)))
    monkeypatch.setattr(keepalive, "_launch_app", lambda *a: calls.append(("launch", *a)))
    monkeypatch.setattr(keepalive.windows, "select_code_tab", lambda timeout: True)
    monkeypatch.setattr(keepalive, "TOKEN_WAIT_SECONDS", 0.05)
    return tmp_path, exe, calls


def test_refresh_relaunches_through_proxy_and_detects_new_token(running, monkeypatch):
    root, exe, calls = running
    expiries = iter(["old", "old", "new"])
    monkeypatch.setattr(keepalive, "fetch_healthz", lambda port: {"tokenExpiresOn": next(expiries)})
    monkeypatch.setattr(keepalive.time, "sleep", lambda s: None)
    assert keepalive.refresh_once(root, 20024) is True
    assert calls == [("stop", ["app"]), ("launch", exe, 20023)]


def test_refresh_reports_reused_token(running, monkeypatch):
    root, _, _ = running
    monkeypatch.setattr(keepalive, "fetch_healthz", lambda port: {"tokenExpiresOn": "same"})
    assert keepalive.refresh_once(root, 20024) is False


def test_refresh_requires_running_proxy(running, monkeypatch):
    root, _, calls = running
    monkeypatch.setattr(keepalive, "is_pid_alive", lambda pid: False)
    with pytest.raises(RuntimeError, match="not running"):
        keepalive.refresh_once(root, 20024)
    assert calls == []


def test_keepalive_waits_then_gives_up_after_bounded_attempts(monkeypatch, tmp_path):
    health = iter([{"upstreamAuthenticated": True, "expiresInSeconds": 1000}])
    monkeypatch.setattr(keepalive, "_require_windows", lambda: None)
    monkeypatch.setattr(
        keepalive,
        "fetch_healthz",
        lambda port: next(health, {"upstreamAuthenticated": True, "expiresInSeconds": 100}),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(keepalive.time, "sleep", sleeps.append)
    attempts: list[int] = []
    monkeypatch.setattr(keepalive, "refresh_once", lambda root, port: attempts.append(port))
    assert keepalive.run_keepalive(tmp_path, 20024, lead=240, max_attempts=3) == 1
    assert len(attempts) == 3
    assert sleeps[0] == 60


def test_keepalive_exits_once_gateway_is_gone(monkeypatch, tmp_path):
    health = iter([{"upstreamAuthenticated": True, "expiresInSeconds": 1000}, None, None, None])
    monkeypatch.setattr(keepalive, "_require_windows", lambda: None)
    monkeypatch.setattr(keepalive, "fetch_healthz", lambda port: next(health))
    monkeypatch.setattr(keepalive.time, "sleep", lambda s: None)
    refresh = []
    monkeypatch.setattr(keepalive, "refresh_once", lambda root, port: refresh.append(port))
    assert keepalive.run_keepalive(tmp_path, 20024, lead=240) == 0
    assert refresh == []
