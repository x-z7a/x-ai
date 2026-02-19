from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from cfi_ai.agent_team import CfiAgentTeam
from cfi_ai.config import CfiConfig
from cfi_ai.flight_phase import FlightPhaseTracker
from cfi_ai.hazard_monitor import HazardMonitor
from cfi_ai.mcp_client import McpSpeechSink, XPlaneMCPClient
from cfi_ai.memory.base import MemoryProvider
from cfi_ai.memory.providers import create_memory_provider
from cfi_ai.review_window import ReviewWindowBuilder
from cfi_ai.types import (
    FlightPhase,
    PhaseState,
    SpeechSink,
    TeamDecision,
    UdpStateSource,
)
from cfi_ai.xplane_udp import XPlaneUdpClient


class TeamRunner(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def run_review(self, review: Any) -> TeamDecision: ...


class JsonlLogger:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=True) + "\n")


class TelemetryCollector:
    def __init__(self, *, enabled: bool, logger: JsonlLogger) -> None:
        self._enabled = enabled
        self._logger = logger

    def emit(self, metric: str, value: float, attrs: dict[str, Any] | None = None) -> None:
        if not self._enabled:
            return
        self._logger.write(
            {
                "ts": time.time(),
                "metric": metric,
                "value": value,
                "attrs": attrs or {},
            }
        )


class CfiRuntime:
    def __init__(
        self,
        config: CfiConfig,
        *,
        nonurgent_speak_enabled: bool = True,
        dry_run: bool = False,
        udp_source: UdpStateSource | None = None,
        speech_sink: SpeechSink | None = None,
        team_runner: TeamRunner | None = None,
        memory_provider: MemoryProvider | None = None,
    ) -> None:
        self._config = config
        self._nonurgent_speak_enabled = nonurgent_speak_enabled
        self._dry_run = dry_run

        self._memory = memory_provider or create_memory_provider(config.memory_backend)
        self._udp = udp_source or XPlaneUdpClient(
            xplane_host=config.xplane_udp_host,
            xplane_port=config.xplane_udp_port,
            local_port=config.xplane_udp_local_port,
            rref_hz=config.xplane_rref_hz,
            buffer_retention_sec=max(120.0, config.review_window_sec * 4),
        )

        if speech_sink is None:
            mcp = XPlaneMCPClient(config.xplane_mcp_sse_url)
            speech_sink = McpSpeechSink(
                mcp_client=mcp,
                urgent_cooldown_sec=config.urgent_cooldown_sec,
                nonurgent_cooldown_sec=config.nonurgent_cooldown_sec,
                dry_run=dry_run,
            )
        self._speech = speech_sink

        self._team = team_runner or CfiAgentTeam(
            config=config,
            memory_provider=self._memory,
            team_chat_log_path=config.team_chat_log_path,
        )

        self._phase_tracker = FlightPhaseTracker()
        self._hazard_monitor = HazardMonitor(config.urgent_cooldown_sec)
        self._review_builder = ReviewWindowBuilder()

        self._runtime_log = JsonlLogger(config.runtime_events_log_path)
        telemetry_logger = JsonlLogger(config.telemetry_log_path)
        self._telemetry = TelemetryCollector(
            enabled=config.telemetry_enabled,
            logger=telemetry_logger,
        )

        self._stop_event = asyncio.Event()
        self._last_snapshot_ts = 0.0
        self._phase_state = PhaseState(
            phase=FlightPhase.PREFLIGHT,
            confidence=0.0,
            changed=False,
            previous_phase=None,
            changed_at_epoch=None,
        )

    async def start(self) -> None:
        await self._speech.start()
        await self._udp.start()
        await self._team.start()

    async def stop(self) -> None:
        await self._team.stop()
        await self._udp.stop()
        await self._speech.stop()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self, duration_sec: float | None = None) -> None:
        await self.start()
        start_epoch = time.time()
        next_review_epoch = start_epoch + self._config.review_tick_sec

        try:
            while not self._stop_event.is_set():
                now = time.time()
                if duration_sec is not None and duration_sec > 0:
                    if now - start_epoch >= duration_sec:
                        break

                snapshot = self._udp.latest()
                if snapshot is not None and snapshot.timestamp_sec > self._last_snapshot_ts:
                    self._last_snapshot_ts = snapshot.timestamp_sec
                    await self._process_snapshot(snapshot)

                if now >= next_review_epoch:
                    await self._run_nonurgent_review(now)
                    next_review_epoch = now + self._config.review_tick_sec

                await asyncio.sleep(0.05)
        finally:
            await self.stop()

    async def _process_snapshot(self, snapshot: Any) -> None:
        self._phase_state = self._phase_tracker.update(snapshot)

        if self._phase_state.changed:
            msg = (
                f"[PHASE] {self._phase_state.previous_phase.value if self._phase_state.previous_phase else 'none'}"
                f" -> {self._phase_state.phase.value}"
            )
            print(msg)
            self._runtime_log.write(
                {
                    "ts": time.time(),
                    "event": "phase_change",
                    "phase": self._phase_state.phase.value,
                    "previous_phase": (
                        self._phase_state.previous_phase.value
                        if self._phase_state.previous_phase
                        else None
                    ),
                    "confidence": self._phase_state.confidence,
                }
            )
            await self._memory.record_event(
                "phase_change",
                {
                    "phase": self._phase_state.phase.value,
                    "previous_phase": (
                        self._phase_state.previous_phase.value
                        if self._phase_state.previous_phase
                        else None
                    ),
                    "confidence": self._phase_state.confidence,
                },
            )

        alerts = self._hazard_monitor.evaluate(snapshot, self._phase_state)
        for alert in alerts:
            did_speak = await self._speech.speak_urgent(alert.speak_text, alert.alert_id)
            self._runtime_log.write(
                {
                    "ts": time.time(),
                    "event": "hazard_alert",
                    "phase": self._phase_state.phase.value,
                    "alert": asdict(alert),
                    "spoken": did_speak,
                }
            )
            await self._memory.record_event(
                "hazard_alert",
                {
                    "phase": self._phase_state.phase.value,
                    "alert": asdict(alert),
                    "spoken": did_speak,
                },
            )
            if did_speak:
                print(f"[URGENT] {alert.speak_text}")
                self._telemetry.emit("urgent_alert_spoken", 1.0, {"alert_id": alert.alert_id})

    async def _run_nonurgent_review(self, now_epoch: float) -> None:
        snapshots = self._udp.window(self._config.review_window_sec)
        if not snapshots:
            return

        review = self._review_builder.build(snapshots, self._phase_state.phase)
        decision = await self._team.run_review(review)

        self._runtime_log.write(
            {
                "ts": now_epoch,
                "event": "team_decision",
                "phase": self._phase_state.phase.value,
                "review_window": asdict(review),
                "decision": asdict(decision),
            }
        )

        await self._memory.record_event(
            "team_decision",
            {
                "phase": self._phase_state.phase.value,
                "review_window": asdict(review),
                "decision": asdict(decision),
            },
        )

        print(f"[REVIEW] {decision.summary}")

        if not self._nonurgent_speak_enabled:
            return
        if not decision.speak_now or not decision.speak_text.strip():
            return
        if self._speech.recent_urgent(self._config.nonurgent_suppress_after_urgent_sec):
            self._runtime_log.write(
                {
                    "ts": now_epoch,
                    "event": "nonurgent_speech_suppressed",
                    "reason": "recent_urgent",
                    "phase": self._phase_state.phase.value,
                }
            )
            return

        spoke = await self._speech.speak_nonurgent(decision.speak_text.strip())
        self._runtime_log.write(
            {
                "ts": time.time(),
                "event": "nonurgent_speech",
                "phase": self._phase_state.phase.value,
                "spoken": spoke,
                "text": decision.speak_text.strip(),
            }
        )
        if spoke:
            print(f"[COACH] {decision.speak_text.strip()}")
            self._telemetry.emit("nonurgent_speech_spoken", 1.0, {"phase": self._phase_state.phase.value})
