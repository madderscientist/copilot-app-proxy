"""ANSI colors for interactive consoles; plain text when redirected or NO_COLOR is set."""

from __future__ import annotations

import os
import sys
from typing import TextIO

RESET = "\x1b[0m"
BOLD = "1"
DIM = "2"
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
CYAN = "36"

_enabled: dict[int, bool] = {}


def enabled(stream: TextIO) -> bool:
    """Return whether ``stream`` is a console that renders ANSI escape sequences."""
    key = id(stream)
    if key not in _enabled:
        _enabled[key] = _detect(stream)
    return _enabled[key]


def _detect(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") or not stream.isatty():
        return False
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-12 if stream is sys.stderr else -11)
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    # ENABLE_VIRTUAL_TERMINAL_PROCESSING; on by default in Windows Terminal, not in conhost.
    return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))


def wrap(text: str, *codes: str) -> str:
    """Style ``text`` unconditionally; callers decide whether color is enabled."""
    return f"\x1b[{';'.join(codes)}m{text}{RESET}" if codes else text


def paint(text: str, *codes: str, stream: TextIO | None = None) -> str:
    """Style ``text`` if ``stream`` (default stdout) supports color."""
    return wrap(text, *codes) if enabled(stream or sys.stdout) else text
