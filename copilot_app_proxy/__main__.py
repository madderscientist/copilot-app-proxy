"""CLI entry point for copilot-app-proxy."""

from __future__ import annotations

import argparse
import asyncio
import sys

from copilot_app_proxy.cli_tools import (
    DEFAULT_GATEWAY_PORT,
    DEFAULT_PROXY_PORT,
    run_doctor,
    run_key,
    run_status,
    run_vscode_config,
)
from copilot_app_proxy.gateway import serve_gateway


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copilot-proxy",
        description="Local Messages API gateway and diagnostics for Microsoft Copilot App.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # `serve` subcommand (and top-level flags for backwards compatibility)
    serve_parser = subparsers.add_parser("serve", help="Run the local Messages API gateway.")
    for target in (parser, serve_parser):
        target.add_argument(
            "--relay",
            action="store_true",
            help="Run gateway in Copilot App token relay mode.",
        )
        target.add_argument(
            "--port",
            type=int,
            default=None,
            help="Loopback port to listen on (overrides PORT env var, default 19324).",
        )

    status_parser = subparsers.add_parser("status", help="Show proxy, CA, and token status.")
    status_parser.add_argument("--port", type=int, default=None, help="Gateway port to query.")

    doctor_parser = subparsers.add_parser("doctor", help="Diagnose environment and proxy setup.")
    doctor_parser.add_argument(
        "--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT, help="Gateway port."
    )
    doctor_parser.add_argument(
        "--proxy-port", type=int, default=DEFAULT_PROXY_PORT, help="MITM proxy port."
    )

    key_parser = subparsers.add_parser(
        "key", help="Ensure local gateway key exists and copy it to clipboard."
    )
    key_parser.add_argument(
        "--no-copy", action="store_true", help="Do not copy the key to clipboard."
    )

    vscode_parser = subparsers.add_parser(
        "vscode-config", help="Print VS Code Custom Endpoint JSON configuration."
    )
    vscode_parser.add_argument(
        "--port", type=int, default=DEFAULT_GATEWAY_PORT, help="Gateway port."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return run_status(port=args.port)
    if args.command == "doctor":
        return run_doctor(gateway_port=args.gateway_port, proxy_port=args.proxy_port)
    if args.command == "key":
        return run_key(copy=not args.no_copy)
    if args.command == "vscode-config":
        return run_vscode_config(port=args.port)

    mode = "relay" if args.relay else "passthrough"
    try:
        return asyncio.run(serve_gateway(mode=mode, port=args.port))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
