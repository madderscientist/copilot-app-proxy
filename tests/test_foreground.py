"""Tests for the foreground log view and status line."""

from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

from copilot_app_proxy.cli import foreground, launcher


def test_log_tail_normalizes_lines_and_follows_rotation(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_bytes(b"12:00:00 POST /v1/messages -> 200\r\nno stamp\npartial")
    tail = foreground.LogTail(log, "gateway")
    entries = tail.read_lines()
    assert entries[0] == ("12:00:00", "gateway", "POST /v1/messages -> 200", False)
    assert entries[1].message == "no stamp"
    assert len(entries) == 2
    log.write_bytes(b"12:05:00 after rotation\n")
    assert tail.read_lines() == [("12:05:00", "gateway", "after rotation", False)]


def test_render_colors_by_source_status_and_severity():
    wrap = foreground.ansi.wrap
    access = foreground.LogEntry(
        "12:00:00", "gateway", "POST /v1/messages model=m -> 503 total=1.00s"
    )
    assert foreground.render(access, color=False) == (
        "12:00:00 [gateway] POST /v1/messages model=m -> 503 total=1.00s"
    )
    assert foreground.render(access, color=True) == (
        f"{wrap('12:00:00', '2')} {wrap('[gateway]', '36')} "
        f"{wrap('POST', '1')} /v1/messages{wrap(' model=m', '2')} {wrap('->', '2')} "
        f"{wrap('503', '1', '31')}{wrap(' total=1.00s', '2')}"
    )
    truncated = foreground.LogEntry("12:00:00", "gateway", "POST /v1/messages -> 200 truncated")
    assert foreground.render(truncated, color=True).endswith(wrap("200 truncated", "1", "31"))
    renewed = foreground.LogEntry("12:00:01", "keepalive", "Token renewed: exp=x.")
    assert foreground.render(renewed, color=True).endswith(wrap("Token renewed: exp=x.", "32"))
    stderr = foreground.LogEntry("12:00:02", "mitm", "anything", stderr=True)
    assert foreground.render(stderr, color=True).endswith(wrap("anything", "31"))
    reused = foreground.LogEntry("12:00:03", "keepalive", "Copilot App reused its cached token")
    assert foreground.render(reused, color=True).endswith(
        wrap("Copilot App reused its cached token", "33")
    )


def test_screen_keeps_status_below_logs(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(foreground.ansi, "enabled", lambda s: True)
    monkeypatch.setattr(foreground, "_get_title", lambda: "old")
    titles: list[str] = []
    monkeypatch.setattr(foreground, "_set_title", titles.append)
    monkeypatch.setattr(foreground.shutil, "get_terminal_size", lambda: Mock(columns=40))
    monkeypatch.setattr(foreground, "render", lambda entry, color: entry.message)
    monkeypatch.setattr(foreground, "format_status", lambda segments, width: segments[0][0])
    screen = foreground.Screen(stream)
    screen.log(foreground.LogEntry("", "", "first"))
    screen.set_status([("bar1", ())], "title")
    screen.log(foreground.LogEntry("", "", "line"))
    screen.set_status([("bar2", ())], "title2")
    screen.close()
    erase = foreground.ERASE_STATUS
    assert stream.getvalue() == (
        "first\n"  # No status yet: nothing to erase.
        "\nbar1"  # Blank line, then the status bar.
        f"{erase}line\n\nbar1"
        f"{erase}\nbar2"
        f"{erase}"
    )
    assert titles == ["title", "title2", "old"]

    plain = io.StringIO()
    monkeypatch.setattr(foreground.ansi, "enabled", lambda s: False)
    screen = foreground.Screen(plain)
    screen.set_status([("bar", ())], "title")
    screen.log(foreground.LogEntry("", "", "line"))
    assert plain.getvalue() == "line\n"


def test_format_status_colors_blocks_and_drops_what_does_not_fit():
    wrap = foreground.ansi.wrap
    ok = foreground.LEVEL_STYLES["ok"]
    segments = [("Token", ok), ("plain block", ()), ("keep-alive STOPPED", ("33",)), ("Ctrl", ("2",))]
    sep = wrap(" | ", "2")
    assert foreground.format_status(segments, 80) == (
        f"{wrap(' Token ', *ok)} plain block{sep}{wrap('keep-alive STOPPED', '33')}"
        f"{sep}{wrap('Ctrl', '2')}"
    )
    # " Token " (7) + " plain block" (12) fits in 20; the next segment does not.
    assert foreground.format_status(segments, 20) == f"{wrap(' Token ', *ok)} plain block"
    assert foreground.format_status([("Token too long", ok)], 10) == wrap(" Token ...", *ok)


def test_describe_status():
    health = {
        "upstreamAuthenticated": True,
        "tokenExpiresOn": "2026-11-18T07:45:58Z",
        "expiresInSeconds": 3660,
    }
    styles = foreground.LEVEL_STYLES
    segments, title = foreground.describe(health, keepalive=True)
    token, codes = segments[0]
    assert token.startswith("Token 61 min left (expires ") and ", renews ~" in token
    assert codes == styles["ok"]
    assert segments[1:] == [("keep-alive on", ()), ("Ctrl+C to stop", ("2",))]
    assert title == "copilot-proxy - token 61 min"

    segments, _ = foreground.describe(health, keepalive=False)
    assert segments[0][1] == styles["ok"]  # Token block reflects the token only.
    assert segments[1] == ("keep-alive STOPPED", ("33",))
    assert foreground.describe({**health, "expiresInSeconds": 200}, True)[0][0][1] == styles["warn"]

    segments, _ = foreground.describe({**health, "upstreamAuthenticated": False}, keepalive=None)
    assert segments[0] == ("No valid token: run 'copilot-proxy refresh'", styles["error"])
    assert all("keep-alive" not in text for text, _ in segments)
    segments, title = foreground.describe(None, keepalive=True)
    assert segments[0] == ("Gateway not responding", styles["error"])
    assert title == "copilot-proxy - gateway down"


@pytest.mark.parametrize("ending", ["ctrl-c", "worker exit"])
def test_run_foreground_echoes_logs_and_stops(tmp_path, monkeypatch, capsys, ending):
    logs = tmp_path / ".local" / "logs"
    logs.mkdir(parents=True)
    (logs / "gateway.log").write_bytes(b"12:00:00 POST /v1/messages -> 200\n")
    (logs / "keepalive.log").write_bytes(b"12:00:01 Started\n")
    launcher.write_pids(tmp_path / ".local", gatewayPid=101, mitmPid=102, keepalivePid=104)
    monkeypatch.setattr(foreground, "_install_close_handler", lambda root: None)
    monkeypatch.setattr(foreground, "fetch_healthz", lambda port: None)
    stop = Mock(return_value=0)
    monkeypatch.setattr(launcher, "stop", stop)
    if ending == "ctrl-c":
        monkeypatch.setattr(foreground, "is_pid_alive", lambda pid: True)
        monkeypatch.setattr(foreground.time, "sleep", Mock(side_effect=KeyboardInterrupt))
    else:
        monkeypatch.setattr(foreground, "is_pid_alive", lambda pid: pid != 102)

    code = foreground.run_foreground(tmp_path, 20024)

    assert code == (0 if ending == "ctrl-c" else 1)
    stop.assert_called_once_with(tmp_path)
    out = capsys.readouterr().out
    assert "12:00:00 [gateway] POST /v1/messages -> 200\n" in out
    assert "12:00:01 [keepalive] Started\n" in out
    if ending == "worker exit":
        assert "mitm exited unexpectedly" in out
