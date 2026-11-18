"""Foreground view of `start`: colored worker logs scroll above a live status bar."""

from __future__ import annotations

import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from . import ansi, launcher
from .ansi import BLUE, BOLD, CYAN, DIM, GREEN, MAGENTA, RED, YELLOW
from .config import local_dir, read_pids
from .diagnostics import fetch_healthz
from .keepalive import DEFAULT_LEAD_SECONDS
from .windows import is_pid_alive

CLEAR_LINE = "\r\x1b[2K"
# Erase the status bar and the blank line above it, leaving the cursor where the blank was.
ERASE_STATUS = f"{CLEAR_LINE}\x1b[1A{CLEAR_LINE}"

# A status bar block: text and the ANSI codes used to style it.
Segment = tuple[str, tuple[str, ...]]
# Token block by severity: black on green, black on yellow, white on red.
LEVEL_STYLES = {"ok": ("30", "42"), "warn": ("30", "43"), "error": ("97", "41")}
STATUS_SEPARATOR = " | "

TIMESTAMP = re.compile(rb"^\d\d:\d\d:\d\d ")
SOURCE_COLORS = {"gateway": CYAN, "mitm": MAGENTA, "keepalive": BLUE}
# "POST /v1/messages model=x stream=true -> 200 headers=1.20s total=8.40s"
ACCESS_LINE = re.compile(
    r"^(?P<method>[A-Z]+) (?P<path>\S+)(?P<info>.*?) -> (?P<status>\S+)(?P<timing>.*)$"
)
ERROR_WORDS = re.compile(r"[Ff]ail|[Ee]rror|[Ee]xception|Traceback|unexpectedly|Gave up|not running")
WARN_WORDS = re.compile(r"reused|relaunching|not found|expired|[Ww]aiting|[Ss]kipping")
GOOD_WORDS = re.compile(r"Token (updated|renewed)|Listening|^Started|\(valid,")


class LogEntry(NamedTuple):
    stamp: str
    source: str
    message: str
    stderr: bool = False


class LogTail:
    """Read new lines appended to a worker log, following truncation and rotation."""

    def __init__(self, path: Path, source: str, *, stderr: bool = False) -> None:
        self.path = path
        self.source = source
        self.stderr = stderr
        self.offset = 0
        self.partial = b""

    def read_lines(self) -> list[LogEntry]:
        try:
            if self.path.stat().st_size < self.offset:  # Cleared or rotated.
                self.offset, self.partial = 0, b""
            with self.path.open("rb") as file:
                file.seek(self.offset)
                data = file.read()
        except OSError:
            return []
        self.offset += len(data)
        *lines, self.partial = (self.partial + data).split(b"\n")
        entries = []
        for raw in lines:
            raw = raw.rstrip(b"\r")
            if not raw.strip():
                continue
            if TIMESTAMP.match(raw):
                stamp, raw = raw[:8].decode("ascii"), raw[9:]
            else:
                stamp = f"{datetime.now():%H:%M:%S}"
            message = raw.decode("utf-8", errors="replace")
            entries.append(LogEntry(stamp, self.source, message, self.stderr))
        return entries


def _status_color(status: str) -> str:
    if status.isdigit() and int(status) < 400:
        return GREEN
    return YELLOW if status.isdigit() and int(status) < 500 else RED


def _message(entry: LogEntry) -> str:
    if access := ACCESS_LINE.match(entry.message):
        status = access["status"]
        return (
            f"{ansi.wrap(access['method'], BOLD)} {access['path']}"
            f"{ansi.wrap(access['info'], DIM) if access['info'] else ''}"
            f" {ansi.wrap('->', DIM)} {ansi.wrap(status, BOLD, _status_color(status))}"
            f"{ansi.wrap(access['timing'], DIM) if access['timing'] else ''}"
        )
    if entry.stderr or ERROR_WORDS.search(entry.message):
        return ansi.wrap(entry.message, RED)
    if WARN_WORDS.search(entry.message):
        return ansi.wrap(entry.message, YELLOW)
    if GOOD_WORDS.search(entry.message):
        return ansi.wrap(entry.message, GREEN)
    return entry.message


def render(entry: LogEntry, color: bool) -> str:
    """Format ``HH:MM:SS [source] message``, colored by source, status, and severity."""
    if not color:
        return f"{entry.stamp} [{entry.source}] {entry.message}"
    source = ansi.wrap(f"[{entry.source}]", SOURCE_COLORS.get(entry.source, BOLD))
    return f"{ansi.wrap(entry.stamp, DIM)} {source} {_message(entry)}"


class Screen:
    """Print log lines; on an interactive console keep a status bar below a blank line."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.live = ansi.enabled(self.stream)
        self.status = ""  # Styled bar, drawn below a blank line once set.
        self._title = _get_title() if self.live else None

    def _erase(self) -> str:
        return ERASE_STATUS if self.status else ""

    def _draw(self) -> str:
        return f"\n{self.status}" if self.status else ""

    def log(self, entry: LogEntry) -> None:
        line = render(entry, self.live)
        if self.live:
            self.stream.write(f"{self._erase()}{line}\n{self._draw()}")
        else:
            self.stream.write(f"{line}\n")
        self.stream.flush()

    def set_status(self, segments: list[Segment], title: str) -> None:
        if not self.live:
            return
        erase = self._erase()
        # One column short of the edge so the bar never wraps.
        self.status = format_status(segments, shutil.get_terminal_size().columns - 1)
        self.stream.write(f"{erase}{self._draw()}")
        self.stream.flush()
        _set_title(title)

    def close(self) -> None:
        if self.live:
            self.stream.write(self._erase())
            self.stream.flush()
            if self._title is not None:
                _set_title(self._title)
        self.status = ""


def _get_title() -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    return buffer.value if ctypes.windll.kernel32.GetConsoleTitleW(buffer, 1024) else None


def _set_title(title: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)


def _local_time(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso).astimezone() if iso else None
    except ValueError:
        return None


def format_status(segments: list[Segment], width: int) -> str:
    """Join segments: the first as a padded colored block, the rest after `` | ``.

    Segments that do not fit in ``width`` are dropped; an oversized first block is cut.
    """
    (first, first_codes), *rest = segments
    block = f" {first} "
    if len(block) > width:
        block = block[: max(width - 3, 0)] + "..."
    parts, used = [ansi.wrap(block, *first_codes)], len(block)
    for index, (text, codes) in enumerate(rest):
        joiner = " " if index == 0 else STATUS_SEPARATOR
        if used + len(joiner) + len(text) > width:
            break
        separator = joiner if index == 0 else ansi.wrap(joiner, DIM)
        parts.append(separator + ansi.wrap(text, *codes))
        used += len(joiner) + len(text)
    return "".join(parts)


def describe(
    health: dict[str, Any] | None, keepalive: bool | None
) -> tuple[list[Segment], str]:
    """Return status bar segments and the window title.

    The first segment is the token state on a green, yellow, or red background; the rest
    are plain except a stopped keep-alive. ``keepalive`` None means it was not started.
    """
    stop_hint: Segment = ("Ctrl+C to stop", (DIM,))
    if health is None:
        return [
            ("Gateway not responding", LEVEL_STYLES["error"]),
            stop_hint,
        ], "copilot-proxy - gateway down"
    expires = _local_time(health.get("tokenExpiresOn"))
    ttl = health.get("expiresInSeconds")
    if health.get("upstreamAuthenticated") and isinstance(ttl, int) and expires:
        token = f"Token {ttl // 60} min left (expires {expires:%H:%M}"
        if keepalive:
            token += f", renews ~{expires - timedelta(seconds=DEFAULT_LEAD_SECONDS):%H:%M}"
        level = "warn" if ttl <= DEFAULT_LEAD_SECONDS else "ok"
        segments: list[Segment] = [(token + ")", LEVEL_STYLES[level])]
        title = f"copilot-proxy - token {ttl // 60} min"
    else:
        segments = [("No valid token: run 'copilot-proxy refresh'", LEVEL_STYLES["error"])]
        title = "copilot-proxy - no token"
    if keepalive is not None:
        segments.append(("keep-alive on", ()) if keepalive else ("keep-alive STOPPED", (YELLOW,)))
    segments.append(stop_hint)
    return segments, title


def _install_close_handler(root: Path) -> Any:
    """Stop keep-alive, Copilot App, and the proxy if the console window is closed."""
    import ctypes
    from ctypes import wintypes

    close_events = {2, 5, 6}  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event: int) -> bool:
        if event not in close_events:
            return False  # Ctrl+C / Ctrl+Break fall through to Python's KeyboardInterrupt.
        launcher._stop_quickly(root)
        return True

    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    return handler  # The caller must keep this reference alive.


def run_foreground(root: Path, gateway_port: int) -> int:
    """Show worker logs and status until Ctrl+C or a worker exits, then run `stop`."""
    local = local_dir(root)
    state = read_pids(local)
    names = ["gateway", "mitm"] + (["keepalive"] if "keepalivePid" in state else [])
    tails = [
        LogTail(local / "logs" / f"{name}{suffix}.log", name, stderr=bool(suffix))
        for name in names
        for suffix in ("", ".err")
    ]
    close_handler = _install_close_handler(root)  # noqa: F841 - must stay referenced
    screen = Screen()
    exit_code = 0
    tick = 0
    try:
        while True:
            for tail in tails:
                for entry in tail.read_lines():
                    screen.log(entry)
            exited = [name for name in ("gateway", "mitm") if not is_pid_alive(state[f"{name}Pid"])]
            if exited:
                message = f"{', '.join(exited)} exited unexpectedly; stopping."
                screen.log(LogEntry(f"{datetime.now():%H:%M:%S}", "start", message, True))
                exit_code = 1
                break
            if tick % 2 == 0:
                keepalive = (
                    is_pid_alive(state["keepalivePid"]) if "keepalivePid" in state else None
                )
                screen.set_status(*describe(fetch_healthz(gateway_port), keepalive))
            tick += 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    screen.close()
    print(ansi.paint("Stopping...", YELLOW), flush=True)
    launcher._stop_with_retry(root)
    for tail in tails:
        for entry in tail.read_lines():
            screen.log(entry)
    return exit_code
