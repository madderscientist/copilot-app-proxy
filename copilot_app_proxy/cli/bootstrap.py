"""Install into the project's virtual environment without importing runtime dependencies."""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

from .config import venv_python


def install(root: Path, index_url: str | None = None) -> int:
    if sys.version_info < (3, 12):  # noqa: UP036 - Source bootstrap runs before pip checks the version.
        raise RuntimeError("Python 3.12 or later is required.")
    python = venv_python(root)
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(root / ".venv")

    args = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--progress-bar",
        "off",
        "-e",
        str(root),
    ]
    if index_url:
        args += ["--index-url", index_url]
    if subprocess.run(args, check=False).returncode:
        raise RuntimeError("Could not install the Python dependencies.")
    print("Installation complete. No certificates, proxy settings, or accounts were changed.")
    print(f"Start with: {python.with_name('copilot-proxy.exe')} start")
    return 0
