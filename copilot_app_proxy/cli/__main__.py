"""`copilot-proxy` command line.

Runtime dependencies are imported lazily: `install` runs before they exist.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from pathlib import Path

from .config import active_ports, local_dir, project_root, resolve_ports
from .diagnostics import run_doctor, run_status, run_vscode_config
from .keepalive import DEFAULT_LEAD_SECONDS


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be an integer between 1 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copilot-proxy",
        description="Expose Microsoft Copilot App's models as a local Messages API.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{install,start,stop,status,doctor,vscode-config,refresh,keepalive}",
    )

    install = subparsers.add_parser("install", help="Set up the project virtual environment.")
    install.add_argument("--index-url", help="Optional pip package index.")

    start = subparsers.add_parser(
        "start",
        help="Start the proxy, relaunch Copilot App, and show logs until Ctrl+C.",
    )
    start.add_argument("--mitmproxy-path", type=Path, help="Path to mitmdump.")
    start.add_argument("--copilot-path", type=Path, help="Path to copilotapp.exe.")
    start.add_argument(
        "--detach",
        action="store_true",
        help="Return after startup and keep running in the background; stop with 'stop'.",
    )
    start.add_argument(
        "--no-keepalive",
        dest="keepalive",
        action="store_false",
        help="Do not start the keep-alive that relaunches Copilot App to renew the token.",
    )
    start.add_argument(
        "--observe",
        action="store_true",
        help="Log request metadata (no tokens) to .local\\logs\\observe.jsonl.",
    )

    subparsers.add_parser("stop", help="Stop the proxy and restore Copilot App.")
    status = subparsers.add_parser("status", help="Show proxy, CA, and token status.")
    doctor = subparsers.add_parser("doctor", help="Diagnose environment and proxy setup.")
    vscode = subparsers.add_parser("vscode-config", help="Print VS Code Custom Endpoint JSON.")
    refresh = subparsers.add_parser(
        "refresh", help="Relaunch Copilot App and open its Code tab to capture a token."
    )
    keepalive = subparsers.add_parser(
        "keepalive", help="Run 'refresh' automatically shortly before the token expires."
    )
    keepalive.add_argument(
        "--lead",
        type=int,
        default=DEFAULT_LEAD_SECONDS,
        help=f"Seconds of remaining validity that trigger a refresh "
        f"(default {DEFAULT_LEAD_SECONDS}).",
    )

    for target in (start, status, doctor, vscode, refresh, keepalive):
        target.add_argument("--gateway-port", type=_port, help="Gateway port.")
    for target in (start, doctor):
        target.add_argument("--proxy-port", type=_port, help="MITM proxy port.")

    # Internal: the gateway worker launched by `start`.
    serve = subparsers.add_parser("serve")
    serve.add_argument("--port", type=_port, required=True)
    serve.add_argument("--log-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        return dispatch(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        from .ansi import RED, paint

        print(paint(f"Error: {exc}", RED, stream=sys.stderr), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def dispatch(args: argparse.Namespace) -> int:
    root = project_root()
    if args.command == "install":
        from .bootstrap import install

        return install(root, args.index_url)
    if args.command == "status":
        return run_status(root, args.gateway_port)
    if args.command == "vscode-config":
        return run_vscode_config(active_ports(root, args.gateway_port)[0])
    if args.command == "doctor":
        return run_doctor(root, *resolve_ports(root, args.gateway_port, args.proxy_port))
    if args.command == "start":
        from .launcher import start

        gateway_port, proxy_port = resolve_ports(root, args.gateway_port, args.proxy_port)
        return start(
            root,
            gateway_port,
            proxy_port,
            mitmproxy_path=args.mitmproxy_path,
            copilot_path=args.copilot_path,
            observe=args.observe,
            foreground=not args.detach,
            keepalive=args.keepalive,
        )
    if args.command in ("refresh", "keepalive"):
        from .keepalive import refresh_once, run_keepalive

        gateway_port = active_ports(root, args.gateway_port)[0]
        if args.command == "refresh":
            return 0 if refresh_once(root, gateway_port) else 1
        if args.lead < 30:
            raise ValueError("--lead must be at least 30 seconds.")
        return run_keepalive(root, gateway_port, lead=args.lead)
    if args.command == "stop":
        from .launcher import stop

        return stop(root)

    import asyncio

    from .server import serve_gateway

    return asyncio.run(
        serve_gateway(
            args.port, token_file=local_dir(root) / "token.dpapi", log_file=args.log_file
        )
    )


if __name__ == "__main__":
    sys.exit(main())
