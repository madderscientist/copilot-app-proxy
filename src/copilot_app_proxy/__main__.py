"""CLI entry point for copilot-app-proxy."""

from __future__ import annotations

import argparse
import asyncio
import sys

from copilot_app_proxy.gateway import serve_gateway


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="copilot-proxy",
        description="Local Messages API gateway for Microsoft Copilot App.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--relay",
        action="store_true",
        help="Run gateway in Copilot App token relay mode.",
    )
    mode_group.add_argument(
        "--login",
        action="store_true",
        help="Run gateway with interactive Microsoft Entra browser sign-in.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Loopback port to listen on (overrides PORT env var, default 19324).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "relay" if args.relay else "login" if args.login else "passthrough"
    try:
        return asyncio.run(serve_gateway(mode=mode, port=args.port))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
