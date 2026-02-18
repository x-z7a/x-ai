from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace

from cfi_ai.config import CfiConfig
from cfi_ai.runtime import CfiRuntime


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autonomous CFI monitor for full-flight coaching and debrief."
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=None,
        help="Override monitor poll interval in seconds.",
    )
    parser.add_argument(
        "--no-speak",
        action="store_true",
        help="Disable xplm_speak_string output (logs only).",
    )
    parser.add_argument(
        "--max-flight-hours",
        type=float,
        default=None,
        help="Maximum runtime before auto-stop.",
    )
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()

    config = CfiConfig.from_env()
    if args.poll_sec is not None:
        config = replace(config, monitor_poll_sec=args.poll_sec)
    if args.no_speak:
        config = replace(config, speak_enabled=False)
    if args.max_flight_hours is not None:
        config = replace(config, max_flight_hours=args.max_flight_hours)

    config.validate()

    runtime = CfiRuntime(config)
    await runtime.start()
    try:
        await runtime.run_until_shutdown()
    finally:
        await runtime.stop()


def cli_entrypoint() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    cli_entrypoint()
