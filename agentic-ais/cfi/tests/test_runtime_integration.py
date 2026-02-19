from __future__ import annotations

import asyncio
import json
import tempfile
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.config import CfiConfig
from cfi_ai.runtime import CfiRuntime
from cfi_ai.types import FlightPhase, FlightSnapshot, SessionProfile, TeamDecision


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


class _StreamingUdp:
    def __init__(self, snapshots: list[FlightSnapshot]) -> None:
        self._snapshots = snapshots
        self._idx = -1

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    def latest(self) -> FlightSnapshot | None:
        if self._idx + 1 < len(self._snapshots):
            self._idx += 1
        if self._idx < 0:
            return None
        return self._snapshots[self._idx]

    def window(self, seconds: float) -> list[FlightSnapshot]:
        del seconds
        if self._idx < 0:
            return []
        return list(self._snapshots[: self._idx + 1])


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


class _FlakySpeech(_FakeSpeech):
    def __init__(self, fail_start_attempts: int) -> None:
        super().__init__()
        self._remaining_failures = fail_start_attempts
        self.start_attempts = 0

    async def start(self) -> None:
        self.start_attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("MCP unavailable")


class _FakeTeam:
    def __init__(self, speak_now: bool = True) -> None:
        self._speak_now = speak_now
        self.calls = 0

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def bootstrap_session(self, snapshots) -> SessionProfile:
        del snapshots
        return SessionProfile(
            aircraft_icao="C172",
            aircraft_category="single_engine_piston",
            confidence=0.9,
            assumptions=["Test bootstrap profile."],
            welcome_message="Welcome to CFI training.",
            raw_llm_output="{}",
        )

    async def run_review(self, review, session_profile: SessionProfile | None = None) -> TeamDecision:
        del session_profile
        self.calls += 1
        return TeamDecision(
            phase=review.phase,
            summary="review complete",
            feedback_items=["item"],
            speak_now=self._speak_now,
            speak_text="Keep refining your profile.",
            raw_master_output="{}",
        )


class _PriorityReviewTeam(_FakeTeam):
    async def run_review(self, review, session_profile: SessionProfile | None = None) -> TeamDecision:
        del session_profile
        self.calls += 1
        return TeamDecision(
            phase=review.phase,
            summary=(
                "Steep bank and high sink near the ground were observed, indicating a hazardous climb profile."
            ),
            feedback_items=["Recover to Vy and reduce bank angle immediately."],
            speak_now=False,
            speak_text="",
            raw_master_output="{}",
        )


def _config(tmpdir: str) -> CfiConfig:
    return CfiConfig(
        xplane_udp_host="127.0.0.1",
        xplane_udp_port=49000,
        xplane_udp_local_port=49001,
        xplane_rref_hz=10,
        xplane_retry_sec=0.1,
        xplane_start_max_retries=3,
        startup_bootstrap_wait_sec=0.1,
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
        shutdown_detect_dwell_sec=0.2,
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
            self.assertNotIn("Keep refining your profile.", speech.nonurgent_calls)
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

    async def test_priority_review_spoken_even_when_speak_now_false(self) -> None:
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
            team = _PriorityReviewTeam(speak_now=False)

            runtime = CfiRuntime(
                cfg,
                udp_source=udp,
                speech_sink=speech,
                team_runner=team,
            )
            await runtime.run(duration_sec=0.25)

            self.assertGreaterEqual(len(speech.nonurgent_calls), 1)

    async def test_startup_retries_speech(self) -> None:
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
            speech = _FlakySpeech(fail_start_attempts=2)
            team = _FakeTeam(speak_now=False)

            runtime = CfiRuntime(
                cfg,
                udp_source=udp,
                speech_sink=speech,
                team_runner=team,
            )
            await runtime.run(duration_sec=0.25)

            self.assertGreaterEqual(speech.start_attempts, 3)

    async def test_shutdown_debrief_logged(self) -> None:
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
            team = _FakeTeam(speak_now=False)

            runtime = CfiRuntime(
                cfg,
                udp_source=udp,
                speech_sink=speech,
                team_runner=team,
            )
            await runtime.run(duration_sec=0.2)

            log_path = Path(cfg.runtime_events_log_path)
            self.assertTrue(log_path.exists())
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            event_names = {evt.get("event") for evt in events}
            self.assertIn("shutdown_debrief", event_names)

    async def test_engine_shutdown_auto_debrief_full_flight(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(tmpdir)
            cfg = CfiConfig(
                **{
                    **cfg.__dict__,
                    "shutdown_detect_dwell_sec": 0.1,
                    "review_tick_sec": 0.5,
                    "review_window_sec": 0.5,
                }
            )
            base = time.time()
            snapshots = [
                FlightSnapshot(
                    timestamp_sec=base + 1.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.4,
                    parking_brake_ratio=0.0,
                    engine_running=True,
                    engine_rpm=900.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 2.0,
                    on_ground=False,
                    groundspeed_m_s=35.0,
                    indicated_airspeed_kt=80.0,
                    throttle_ratio=1.0,
                    vertical_speed_fpm=700.0,
                    engine_running=True,
                    engine_rpm=2500.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 3.0,
                    on_ground=False,
                    groundspeed_m_s=45.0,
                    indicated_airspeed_kt=95.0,
                    throttle_ratio=0.6,
                    vertical_speed_fpm=0.0,
                    engine_running=True,
                    engine_rpm=2300.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 4.0,
                    on_ground=True,
                    groundspeed_m_s=4.5,
                    indicated_airspeed_kt=9.0,
                    throttle_ratio=0.2,
                    parking_brake_ratio=0.1,
                    engine_running=True,
                    engine_rpm=1000.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 5.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.05,
                    parking_brake_ratio=1.0,
                    engine_running=False,
                    engine_rpm=0.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 6.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.05,
                    parking_brake_ratio=1.0,
                    engine_running=False,
                    engine_rpm=0.0,
                ),
            ]

            udp = _StreamingUdp(snapshots)
            speech = _FakeSpeech()
            team = _FakeTeam(speak_now=False)
            runtime = CfiRuntime(
                cfg,
                udp_source=udp,
                speech_sink=speech,
                team_runner=team,
            )
            await runtime.run(duration_sec=0.6)

            events = [
                json.loads(line)
                for line in Path(cfg.runtime_events_log_path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            names = [evt.get("event") for evt in events]
            self.assertIn("engine_shutdown_detected", names)

            debriefs = [evt for evt in events if evt.get("event") == "shutdown_debrief"]
            self.assertEqual(len(debriefs), 1)
            self.assertEqual(debriefs[0].get("reason"), "engine_shutdown_detected")
            self.assertEqual(debriefs[0]["review_window"]["sample_count"], len(snapshots))

    async def test_multiple_flights_in_one_daemon_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _config(tmpdir)
            cfg = CfiConfig(
                **{
                    **cfg.__dict__,
                    "shutdown_detect_dwell_sec": 0.1,
                    "review_tick_sec": 0.5,
                    "review_window_sec": 0.5,
                }
            )
            base = time.time()
            snapshots = [
                # Flight 1
                FlightSnapshot(
                    timestamp_sec=base + 1.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.4,
                    parking_brake_ratio=0.0,
                    engine_running=True,
                    engine_rpm=900.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 2.0,
                    on_ground=False,
                    groundspeed_m_s=35.0,
                    indicated_airspeed_kt=80.0,
                    throttle_ratio=1.0,
                    vertical_speed_fpm=600.0,
                    engine_running=True,
                    engine_rpm=2400.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 3.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.05,
                    parking_brake_ratio=1.0,
                    engine_running=False,
                    engine_rpm=0.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 4.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.05,
                    parking_brake_ratio=1.0,
                    engine_running=False,
                    engine_rpm=0.0,
                ),
                # Flight 2 start activity after shutdown
                FlightSnapshot(
                    timestamp_sec=base + 5.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.35,
                    parking_brake_ratio=0.0,
                    engine_running=True,
                    engine_rpm=1000.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 6.0,
                    on_ground=False,
                    groundspeed_m_s=30.0,
                    indicated_airspeed_kt=75.0,
                    throttle_ratio=0.9,
                    vertical_speed_fpm=500.0,
                    engine_running=True,
                    engine_rpm=2300.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 7.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.05,
                    parking_brake_ratio=1.0,
                    engine_running=False,
                    engine_rpm=0.0,
                ),
                FlightSnapshot(
                    timestamp_sec=base + 8.0,
                    on_ground=True,
                    groundspeed_m_s=0.0,
                    indicated_airspeed_kt=0.0,
                    throttle_ratio=0.05,
                    parking_brake_ratio=1.0,
                    engine_running=False,
                    engine_rpm=0.0,
                ),
            ]

            udp = _StreamingUdp(snapshots)
            speech = _FakeSpeech()
            team = _FakeTeam(speak_now=False)
            runtime = CfiRuntime(
                cfg,
                udp_source=udp,
                speech_sink=speech,
                team_runner=team,
            )
            await runtime.run(duration_sec=0.9)

            events = [
                json.loads(line)
                for line in Path(cfg.runtime_events_log_path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            shutdown_detected = [evt for evt in events if evt.get("event") == "engine_shutdown_detected"]
            self.assertEqual(len(shutdown_detected), 2)

            debriefs = [evt for evt in events if evt.get("event") == "shutdown_debrief"]
            self.assertEqual(len(debriefs), 2)
            self.assertEqual({evt.get("flight_index") for evt in debriefs}, {1, 2})

            cycle_starts = [evt for evt in events if evt.get("event") == "flight_cycle_started"]
            self.assertEqual(len(cycle_starts), 1)
            self.assertEqual(cycle_starts[0].get("flight_index"), 2)


if __name__ == "__main__":
    unittest.main()
