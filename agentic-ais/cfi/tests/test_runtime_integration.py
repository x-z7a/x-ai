from __future__ import annotations

import asyncio
import tempfile
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.config import CfiConfig
from cfi_ai.runtime import CfiRuntime
from cfi_ai.types import FlightPhase, FlightSnapshot, TeamDecision


class _FakeUdp:
    def __init__(self, snapshots: list[FlightSnapshot]) -> None:
        self._snapshots = snapshots

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    def latest(self) -> FlightSnapshot | None:
        return self._snapshots[-1] if self._snapshots else None

    def window(self, seconds: float) -> list[FlightSnapshot]:
        del seconds
        return list(self._snapshots)


class _FakeSpeech:
    def __init__(self) -> None:
        self.urgent_calls: list[tuple[str, str]] = []
        self.nonurgent_calls: list[str] = []
        self._last_urgent_at = 0.0

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def speak_urgent(self, text: str, key: str) -> bool:
        self.urgent_calls.append((key, text))
        self._last_urgent_at = time.time()
        return True

    async def speak_nonurgent(self, text: str) -> bool:
        self.nonurgent_calls.append(text)
        return True

    def recent_urgent(self, within_sec: float) -> bool:
        return (time.time() - self._last_urgent_at) <= within_sec


class _FakeTeam:
    def __init__(self, speak_now: bool = True) -> None:
        self._speak_now = speak_now
        self.calls = 0

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def run_review(self, review) -> TeamDecision:
        self.calls += 1
        return TeamDecision(
            phase=review.phase,
            summary="review complete",
            feedback_items=["item"],
            speak_now=self._speak_now,
            speak_text="Keep refining your profile.",
            raw_master_output="{}",
        )


def _config(tmpdir: str) -> CfiConfig:
    return CfiConfig(
        xplane_udp_host="127.0.0.1",
        xplane_udp_port=49000,
        xplane_udp_local_port=49001,
        xplane_rref_hz=10,
        xplane_mcp_sse_url="http://127.0.0.1:8765/sse",
        enable_mcp_commands=False,
        github_token="",
        copilot_use_logged_in_user=True,
        copilot_use_custom_provider=False,
        copilot_model="gpt-4o-mini",
        copilot_base_url="https://models.github.ai/inference",
        copilot_bearer_token="",
        autogen_model="openai/gpt-4.1-mini",
        autogen_model_fallbacks=("openai/gpt-4o-mini",),
        autogen_base_url="https://models.github.ai/inference",
        autogen_api_key="",
        review_window_sec=0.5,
        review_tick_sec=0.1,
        urgent_cooldown_sec=8.0,
        nonurgent_cooldown_sec=0.1,
        nonurgent_suppress_after_urgent_sec=5.0,
        memory_backend="none",
        telemetry_enabled=False,
        team_chat_log_path=str(Path(tmpdir) / "team.chat.log.jsonl"),
        runtime_events_log_path=str(Path(tmpdir) / "runtime.events.log.jsonl"),
        telemetry_log_path=str(Path(tmpdir) / "telemetry.log.jsonl"),
    )


class TestRuntimeIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_urgent_suppresses_nonurgent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(tmpdir)
            snapshot = FlightSnapshot(
                timestamp_sec=time.time(),
                on_ground=False,
                indicated_airspeed_kt=45.0,
                vertical_speed_fpm=-200.0,
            )
            udp = _FakeUdp([snapshot])
            speech = _FakeSpeech()
            team = _FakeTeam(speak_now=True)

            runtime = CfiRuntime(
                cfg,
                udp_source=udp,
                speech_sink=speech,
                team_runner=team,
            )
            await runtime.run(duration_sec=0.25)

            self.assertGreaterEqual(len(speech.urgent_calls), 1)
            self.assertEqual(len(speech.nonurgent_calls), 0)
            self.assertGreaterEqual(team.calls, 1)

    async def test_nonurgent_spoken_without_recent_urgent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(tmpdir)
            snapshot = FlightSnapshot(
                timestamp_sec=time.time(),
                on_ground=True,
                elevation_m=100.0,
                groundspeed_m_s=0.0,
                indicated_airspeed_kt=0.0,
                vertical_speed_fpm=0.0,
            )
            udp = _FakeUdp([snapshot])
            speech = _FakeSpeech()
            team = _FakeTeam(speak_now=True)

            runtime = CfiRuntime(
                cfg,
                udp_source=udp,
                speech_sink=speech,
                team_runner=team,
            )
            await runtime.run(duration_sec=0.25)

            self.assertEqual(len(speech.urgent_calls), 0)
            self.assertGreaterEqual(len(speech.nonurgent_calls), 1)
            self.assertGreaterEqual(team.calls, 1)


if __name__ == "__main__":
    unittest.main()
