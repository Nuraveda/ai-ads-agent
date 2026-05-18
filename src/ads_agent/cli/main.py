from __future__ import annotations

import argparse
import asyncio

from ads_agent.cli.port_meta_to_tiktok import run_port_meta_to_tiktok


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="glitch-executor")
    sub = parser.add_subparsers(dest="command", required=True)

    port = sub.add_parser(
        "port-meta-to-tiktok",
        help="Queue a Discord approval to mirror a Meta video ad onto TikTok.",
    )
    port.add_argument("--slug", required=True, help="Store/TikTok slug, e.g. example-global.")
    port.add_argument("--meta-ad-id", default="", help="Meta ad id to mirror.")
    port.add_argument("--daily-budget", type=float, default=50.0)
    port.add_argument("--bid-price", type=float, default=10.0)
    port.add_argument("--cta", default="LEARN_MORE")
    port.add_argument(
        "--print-context-only",
        action="store_true",
        help="Resolve Meta/TikTok context and exit before posting to Discord.",
    )
    return parser


def cli(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "port-meta-to-tiktok":
        asyncio.run(run_port_meta_to_tiktok(args))
        return
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    cli()
