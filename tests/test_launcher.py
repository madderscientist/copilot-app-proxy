"""Verify Python orchestration without modifying live processes or the certificate store."""

import json
import subprocess
import sys
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock, call

import pytest

from copilot_app_proxy.cli import launcher
from copilot_app_proxy.cli.__main__ import main
from copilot_app_proxy.cli.config import resolve_ports


@pytest.fixture
def simulated_platform(monkeypatch):
    monkeypatch.setattr(launcher, "_require_windows", lambda: None)
    monkeypatch.setattr(launcher.windows, "lifecycle_lock", lambda _: nullcontext())
    monkeypatch.setattr(launcher.windows, "select_code_tab", lambda timeout=60: True)
    monkeypatch.setattr(launcher, "fetch_healthz", lambda port: {"upstreamAuthenticated": True})


def test_config_validation_and_doctor_overrides(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.json"
    config.write_text('{"gateway_port": 19424, "proxy_port": 19423}', encoding="utf-8")
    monkeypatch.setattr("copilot_app_proxy.cli.__main__.project_root", lambda: tmp_path)
    doctor = Mock(return_value=0)
    monkeypatch.setattr("copilot_app_proxy.cli.__main__.run_doctor", doctor)
    assert main(["doctor", "--gateway-port", "19425"]) == 0
    doctor.assert_called_once_with(tmp_path, 19425, 19423)
    assert main(["vscode-config"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["models"][0]["url"].endswith(":19424/v1/messages")
    config.write_text('{"proxy_port": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="proxy_port"):
        resolve_ports(tmp_path)


@pytest.mark.parametrize("failure", [None, "not ready", "app launch failed"])
@pytest.mark.usefixtures("simulated_platform")
def test_managed_start_and_stop(tmp_path, monkeypatch, failure):
    exe = tmp_path / "desktop app.exe"
    exe.touch()
    kill = Mock()
    launch_app = Mock(side_effect=OSError(failure) if failure == "app launch failed" else None)
    processes = [Mock(pid=pid) for pid in (101, 102, 104)]
    for process in processes:
        process.poll.return_value = None
    workers = Mock(side_effect=processes)
    ready = Mock(side_effect=RuntimeError(failure) if failure == "not ready" else None)
    monkeypatch.setattr(launcher, "_launch_worker", workers)
    monkeypatch.setattr(launcher, "_wait_ready", ready)
    monkeypatch.setattr(launcher, "_kill_tree", kill)
    monkeypatch.setattr(launcher, "_launch_app", launch_app)
    monkeypatch.setattr(launcher, "is_port_listening", lambda _: False)
    monkeypatch.setattr(launcher, "is_pid_alive", lambda _: True)
    stop_app = Mock()
    monkeypatch.setattr(launcher, "_stop_app", stop_app)
    apps = [launcher.windows.AppProcess(103, 1, exe)]
    monkeypatch.setattr(launcher.windows, "copilot_processes", lambda: apps)
    monkeypatch.setattr(launcher.windows, "ca_thumbprint", lambda _: "A" * 40)
    trust = Mock()
    remove = Mock()
    monkeypatch.setattr(launcher.windows, "trust_ca", trust)
    monkeypatch.setattr(launcher.windows, "remove_ca", remove)
    monkeypatch.setattr(
        launcher.windows,
        "process_commands",
        lambda _: {
            101: "python -m copilot_app_proxy.cli serve --port 20024",
            102: str(launcher.ADDON_PATH),
            104: "python -m copilot_app_proxy.cli keepalive --gateway-port 20024",
        },
    )
    local = tmp_path / ".local"
    if failure:
        with pytest.raises((OSError, RuntimeError), match=failure):
            launcher.start(tmp_path, 20024, 20023, mitmproxy_path=exe, copilot_path=exe)
        if failure == "not ready":
            trust.assert_not_called()
            launch_app.assert_not_called()
            stop_app.assert_not_called()
        else:
            remove.assert_called_once_with("A" * 40)
            launch_app.assert_called_once_with(exe, 20023)
            # Closed before the proxied launch and again during rollback; never relaunched.
            assert stop_app.call_args_list == [call(apps), call(apps)]
    else:
        assert launcher.start(tmp_path, 20024, 20023, mitmproxy_path=exe, copilot_path=exe) == 0
        state = launcher.read_pids(local)
        assert (state["gatewayPort"], state["proxyPort"]) == (20024, 20023)
        assert state["keepalivePid"] == 104
        assert workers.call_args_list[2].args[0][1:] == [
            "-m", "copilot_app_proxy.cli", "keepalive", "--gateway-port", "20024"
        ]
        launch_app.assert_called_once_with(exe, 20023)
        assert launcher.stop(tmp_path) == 0
        remove.assert_called_once_with("A" * 40)
        launch_app.assert_called_once()  # stop closes Copilot App without relaunching it.
        assert stop_app.call_args_list == [call(apps), call(apps)]
    gateway_args = workers.call_args_list[0].args[0]
    assert gateway_args[1:] == [
        "-m", "copilot_app_proxy.cli", "serve", "--port", "20024",
        "--log-file", str(local / "logs" / "gateway.log"),
    ]
    assert workers.call_args_list[0].args[3].keys() == {"RELAY_INGEST_SECRET"}
    assert workers.call_args_list[0].kwargs == {"own_log": True}
    expected_kills = [call(101), call(102)] if failure else [call(104), call(101), call(102)]
    assert kill.call_args_list == expected_kills
    assert not (local / "pids.json").exists()
    assert not (local / "mitm-ca").exists()


@pytest.mark.usefixtures("simulated_platform")
def test_stop_can_retry_when_copilot_app_does_not_close(tmp_path, monkeypatch):
    local = tmp_path / ".local"
    local.mkdir()
    state_file = local / "pids.json"
    state_file.write_text(json.dumps({"gatewayPid": 101, "mitmPid": 102}), encoding="ascii")
    apps = [launcher.windows.AppProcess(103, 1, None)]
    stop_app = Mock(side_effect=[RuntimeError("did not exit in time"), None])
    monkeypatch.setattr(launcher.windows, "process_commands", lambda _: {})
    monkeypatch.setattr(launcher.windows, "copilot_processes", lambda: apps)
    monkeypatch.setattr(launcher.windows, "remove_ca", Mock())
    monkeypatch.setattr(launcher, "_stop_app", stop_app)
    with pytest.raises(RuntimeError, match="did not exit"):
        launcher.stop(tmp_path)
    assert state_file.exists()
    assert launcher.stop(tmp_path) == 0
    assert stop_app.call_count == 2
    assert not state_file.exists()


def test_stop_quickly_stops_keepalive_first_and_never_relaunches(tmp_path, monkeypatch):
    (tmp_path / ".local").mkdir()
    launcher.write_pids(tmp_path / ".local", gatewayPid=101, mitmPid=102, keepalivePid=104)
    order: list[object] = []
    monkeypatch.setattr(launcher, "_kill_tree", order.append)
    monkeypatch.setattr(launcher.windows, "copilot_processes", lambda: ["app"])
    monkeypatch.setattr(launcher, "_stop_app", lambda apps, timeout: order.append(apps))
    launch_app = Mock()
    monkeypatch.setattr(launcher, "_launch_app", launch_app)
    launcher._stop_quickly(tmp_path)
    assert order == [104, ["app"], 101, 102]
    launch_app.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop lifecycle lock")
def test_cross_process_lock_blocks_start_and_stop(tmp_path):
    code = (
        "from pathlib import Path; import sys; from copilot_app_proxy.cli.launcher import start, stop; "
        "root = Path(sys.argv[1]); "
        "start(root, 20424, 20423, mitmproxy_path=root / 'missing.exe') "
        "if sys.argv[2] == 'start' else stop(root)"
    )
    with launcher.windows.lifecycle_lock(tmp_path / ".local"):
        for operation in ("start", "stop"):
            result = subprocess.run(
                [sys.executable, "-c", code, str(tmp_path), operation],
                capture_output=True, text=True, timeout=10, check=False,
            )
            assert result.returncode != 0
            assert "Another start or stop is in progress" in result.stderr
    with launcher.windows.lifecycle_lock(tmp_path / ".local"):
        pass


def test_serve_caches_token_relative_to_project(tmp_path, monkeypatch):
    working_dir = tmp_path / "elsewhere"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr("copilot_app_proxy.cli.__main__.project_root", lambda: tmp_path)
    serve = AsyncMock(return_value=0)
    monkeypatch.setattr("copilot_app_proxy.cli.server.serve_gateway", serve)
    assert main(["serve", "--port", "20424"]) == 0
    serve.assert_awaited_once_with(
        20424, token_file=tmp_path / ".local" / "token.dpapi", log_file=None
    )


def test_cli_requires_a_command(capsys):
    assert main([]) == 2
    assert "serve" not in capsys.readouterr().out


@pytest.mark.parametrize("trusted", [[False], [True, False], [True, True]])
def test_remove_ca_uses_certutil(monkeypatch, trusted):
    run = Mock()
    monkeypatch.setattr(launcher.windows, "ca_is_trusted", Mock(side_effect=trusted))
    monkeypatch.setattr(launcher.windows.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="still trusted") if trusted[-1] else nullcontext():
        launcher.windows.remove_ca("A" * 40)
    expected = [call(["certutil.exe", "-user", "-delstore", "Root", "A" * 40], check=True)]
    assert run.call_args_list == (expected if trusted[0] else [])


def test_stop_with_retry_waits_for_keepalive_lock(tmp_path, monkeypatch):
    stop = Mock(side_effect=[RuntimeError("Another start or stop is in progress."), 0])
    monkeypatch.setattr(launcher, "stop", stop)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    assert launcher._stop_with_retry(tmp_path) == 0
    assert stop.call_count == 2
    stop.side_effect = RuntimeError("Could not stop process 1.")
    with pytest.raises(RuntimeError, match="Could not stop"):
        launcher._stop_with_retry(tmp_path)
