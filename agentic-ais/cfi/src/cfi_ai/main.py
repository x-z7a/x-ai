from __future__ import annotations

import argparse
import asyncio

from cfi_ai.config import CfiConfig
from cfi_ai.runtime import CfiRuntime


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentic CFI runtime for X-Plane UDP + MCP.")
    parser.add_argument(
        "--duration-sec",
        type=int,
        default=0,
        help="Optional bounded run duration in seconds (0 means run continuously).",
    )
    parser.add_argument(
        "--no-nonurgent-speak",
        action="store_true",
        help="Disable non-urgent team speech while keeping urgent safety speech enabled.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip MCP speech calls and run logic-only mode.",
    )
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()

    config = CfiConfig.from_env()
    config.validate()

    runtime = CfiRuntime(
        config,
        nonurgent_speak_enabled=not args.no_nonurgent_speak,
        dry_run=args.dry_run,
    )
    await runtime.run(duration_sec=float(args.duration_sec) if args.duration_sec > 0 else None)


def cli_entrypoint() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    cli_entrypoint()
