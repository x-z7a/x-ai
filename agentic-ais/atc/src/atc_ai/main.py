from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace

from atc_ai.config import AtcConfig
from atc_ai.runtime import AtcRuntime


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentic ATC runtime for X-Plane MCP.")
    parser.add_argument("--task", type=str, default="", help="Single ATC task to run.")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run REPL mode for repeated ATC prompts.",
    )
    parser.add_argument(
        "--transmit",
        action="store_true",
        help="Force ATC radio transmission enabled (equivalent to ATC_AUTO_TRANSMIT=true).",
    )
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()

    config = AtcConfig.from_env()
    if args.transmit:
        config = replace(config, auto_transmit=True)
    config.validate()

    runtime = AtcRuntime(config)
    await runtime.start()
    try:
        if args.interactive:
            await _run_interactive(runtime)
            return

        task = args.task.strip() or (
            "Provide current tower guidance for the active aircraft, including "
            "airport context and concise radio phraseology."
        )
        await runtime.run_once(task)
    finally:
        await runtime.stop()


async def _run_interactive(runtime: AtcRuntime) -> None:
    print("ATC interactive mode. Continuous monitoring (deviations + handovers) is active. Type 'exit' to quit.\n")
    while True:
        try:
            task = input("atc> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        if not task:
            continue
        if task.lower() in {"exit", "quit"}:
            break

        await runtime.run_once(task)
        print()


def cli_entrypoint() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    cli_entrypoint()
