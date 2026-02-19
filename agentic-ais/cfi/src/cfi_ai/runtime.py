from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import suppress
from dataclasses import asdict, replace
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
    FlightSnapshot,
    FlightPhase,
    PhaseState,
    ReviewWindow,
    SessionProfile,
    SpeechSink,
    TeamDecision,
    UdpStateSource,
)
from cfi_ai.xplane_udp import XPlaneUdpClient

AIRBORNE_PHASES: set[FlightPhase] = {
    FlightPhase.TAKEOFF,
    FlightPhase.INITIAL_CLIMB,
    FlightPhase.CRUISE,
    FlightPhase.DESCENT,
    FlightPhase.APPROACH,
    FlightPhase.LANDING,
}

PRIORITY_REVIEW_KEYWORDS: tuple[str, ...] = (
    "high sink",
    "sink rate",
    "hard touchdown",
    "steep bank",
    "unstable",
    "hazard",
    "hazardous",
    "risk",
    "unsafe",
    "low-energy",
    "critical",
    "immediate correction",
    "immediate coaching",
    "poor control",
    "stall",
)

LOW_VALUE_COACH_MARKERS: tuple[str, ...] = (
    "no evidence",
    "no movement",
    "no issues",
    "no hazards",
    "no hazard",
    "awaiting",
    "not assessable",
    "not applicable",
    "no data",
    "stationary",
)

ACTIONABLE_COACH_KEYWORDS: tuple[str, ...] = (
    "maintain",
    "focus",
    "review",
    "practice",
    "correct",
    "reduce",
    "increase",
    "hold",
    "keep",
    "use",
    "monitor",
    "recover",
    "go-around",
    "go around",
    "let's",
)


class TeamRunner(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def run_review(
        self,
        review: Any,
        session_profile: SessionProfile | None = None,
    ) -> TeamDecision: ...

    async def bootstrap_session(self, snapshots: list[Any]) -> SessionProfile: ...


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
            discovery_enabled=config.xplane_discovery_enabled,
            beacon_multicast_group=config.xplane_beacon_multicast_group,
            beacon_port=config.xplane_beacon_port,
            beacon_timeout_sec=config.xplane_beacon_timeout_sec,
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
        self._session_profile: SessionProfile | None = None
        self._hazard_events_count = 0
        self._hazard_alert_counts: dict[str, int] = {}
        self._session_snapshots: list[FlightSnapshot] = []
        self._phase_path: list[FlightPhase] = [FlightPhase.PREFLIGHT]
        self._saw_airborne_segment = False
        self._shutdown_candidate_since: float | None = None
        self._shutdown_debrief_emitted = False
        self._flight_index = 1
        self._hazard_phrase_refresh_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._start_with_retry(
            label="X-Plane UDP",
            starter=self._udp.start,
        )
        await self._start_with_retry(
            label="X-Plane MCP speech",
            starter=self._speech.start,
        )
        await self._team.start()
        await self._bootstrap_session_profile()
        self._start_hazard_phrase_refresh_loop()

    async def stop(self) -> None:
        if self._hazard_phrase_refresh_task is not None:
            self._hazard_phrase_refresh_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._hazard_phrase_refresh_task
        self._hazard_phrase_refresh_task = None
        await self._team.stop()
        await self._udp.stop()
        await self._speech.stop()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self, duration_sec: float | None = None) -> None:
        started = False
        stop_reason = "unknown"
        try:
            await self.start()
            started = True
            start_epoch = time.time()
            next_review_epoch = start_epoch + self._config.review_tick_sec

            while not self._stop_event.is_set():
                now = time.time()
                if duration_sec is not None and duration_sec > 0:
                    if now - start_epoch >= duration_sec:
                        stop_reason = "duration_elapsed"
                        break

                snapshot = self._udp.latest()
                if snapshot is not None and snapshot.timestamp_sec > self._last_snapshot_ts:
                    self._last_snapshot_ts = snapshot.timestamp_sec
                    await self._process_snapshot(snapshot)

                if now >= next_review_epoch:
                    await self._run_nonurgent_review(now)
                    next_review_epoch = now + self._config.review_tick_sec

                await asyncio.sleep(0.05)

            if self._stop_event.is_set():
                stop_reason = "stop_requested"
            if stop_reason == "unknown":
                stop_reason = "loop_exit"
        finally:
            if started:
                with suppress(Exception):
                    await self._run_shutdown_debrief(stop_reason)
                await self.stop()

    async def _process_snapshot(self, snapshot: Any) -> None:
        await self._maybe_start_new_flight_cycle(snapshot)

        self._session_snapshots.append(snapshot)
        if len(self._session_snapshots) > 100_000:
            self._session_snapshots = self._session_snapshots[-100_000:]

        self._phase_state = self._phase_tracker.update(snapshot)
        if (not snapshot.on_ground) or self._phase_state.phase in AIRBORNE_PHASES:
            self._saw_airborne_segment = True

        if self._phase_state.changed:
            if self._phase_path[-1] != self._phase_state.phase:
                self._phase_path.append(self._phase_state.phase)
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
            self._hazard_events_count += 1
            self._hazard_alert_counts[alert.alert_id] = self._hazard_alert_counts.get(alert.alert_id, 0) + 1
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

        await self._maybe_trigger_shutdown_debrief(snapshot)

    async def _run_nonurgent_review(self, now_epoch: float) -> None:
        snapshots = self._udp.window(self._config.review_window_sec)
        if not snapshots:
            return

        review = self._review_builder.build(snapshots, self._phase_state.phase)
        decision = await self._team.run_review(review, session_profile=self._session_profile)

        self._runtime_log.write(
            {
                "ts": now_epoch,
                "event": "team_decision",
                "flight_index": self._flight_index,
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
        if self._shutdown_debrief_emitted:
            return

        priority_review = _is_priority_review(decision)
        coach_text = _select_coach_text(decision)
        if not decision.speak_now and not priority_review:
            return
        if not coach_text:
            self._runtime_log.write(
                {
                    "ts": now_epoch,
                    "event": "nonurgent_speech_skipped",
                    "phase": self._phase_state.phase.value,
                    "reason": "empty_coach_text",
                }
            )
            return
        if _is_low_value_coach_text(coach_text) and not priority_review:
            self._runtime_log.write(
                {
                    "ts": now_epoch,
                    "event": "nonurgent_speech_skipped",
                    "phase": self._phase_state.phase.value,
                    "reason": "low_value_text",
                    "text": coach_text,
                }
            )
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

        spoke = await self._speech.speak_nonurgent(coach_text)
        channel = "nonurgent"
        if not spoke and priority_review:
            key = f"priority_review_{self._phase_state.phase.value}"
            spoke = await self._speech.speak_urgent(coach_text, key)
            channel = "priority_review_fallback"

        self._runtime_log.write(
            {
                "ts": time.time(),
                "event": "nonurgent_speech",
                "phase": self._phase_state.phase.value,
                "spoken": spoke,
                "text": coach_text,
                "channel": channel,
                "priority_review": priority_review,
            }
        )
        if spoke:
            print(f"[COACH] {coach_text}")
            self._telemetry.emit("nonurgent_speech_spoken", 1.0, {"phase": self._phase_state.phase.value})

    async def _start_with_retry(self, *, label: str, starter: Any) -> None:
        attempt = 0
        max_attempts = self._config.xplane_start_max_retries
        while True:
            attempt += 1
            try:
                await starter()
                if attempt > 1:
                    print(f"[RETRY] {label} connected on attempt {attempt}.")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._runtime_log.write(
                    {
                        "ts": time.time(),
                        "event": "startup_retry",
                        "component": label,
                        "attempt": attempt,
                        "error": str(exc),
                    }
                )
                print(
                    f"[RETRY] {label} unavailable: {exc}. "
                    f"Retrying in {self._config.xplane_retry_sec:.1f}s."
                )
                if max_attempts > 0 and attempt >= max_attempts:
                    raise RuntimeError(
                        f"{label} failed to start after {attempt} attempts."
                    ) from exc
                await asyncio.sleep(self._config.xplane_retry_sec)

    async def _bootstrap_session_profile(self) -> None:
        start = time.time()
        snapshots = self._udp.window(self._config.startup_bootstrap_wait_sec)
        while (
            len(snapshots) < 3
            and time.time() - start < self._config.startup_bootstrap_wait_sec
        ):
            await asyncio.sleep(0.2)
            snapshots = self._udp.window(self._config.startup_bootstrap_wait_sec)

        try:
            self._session_profile = await self._team.bootstrap_session(snapshots)
        except Exception as exc:  # noqa: BLE001
            self._session_profile = SessionProfile(
                aircraft_icao="C172",
                aircraft_category="single_engine_piston",
                confidence=0.0,
                assumptions=[f"Bootstrap fallback due to startup error: {type(exc).__name__}"],
                welcome_message=(
                    "Welcome aboard. We'll use a Cessna 172 baseline profile and start coaching each phase."
                ),
                raw_llm_output=str(exc),
            )
        await self._memory.record_event(
            "session_profile",
            asdict(self._session_profile),
        )
        self._runtime_log.write(
            {
                "ts": time.time(),
                "event": "session_profile_initialized",
                "profile": asdict(self._session_profile),
                "snapshot_count": len(snapshots),
            }
        )
        self._hazard_monitor.set_hazard_profile(self._session_profile.hazard_profile)
        print(
            "[BOOTSTRAP] "
            f"aircraft={self._session_profile.aircraft_icao} "
            f"confidence={self._session_profile.confidence:.2f}"
        )
        print(
            "[BOOTSTRAP PROFILE] "
            + json.dumps(_profile_console_payload(self._session_profile), ensure_ascii=True)
        )

        if self._nonurgent_speak_enabled and self._session_profile.welcome_message.strip():
            spoke = await self._speech.speak_nonurgent(
                self._session_profile.welcome_message.strip()
            )
            self._runtime_log.write(
                {
                    "ts": time.time(),
                    "event": "startup_welcome",
                    "spoken": spoke,
                    "text": self._session_profile.welcome_message.strip(),
                }
            )
            if spoke:
                print(f"[WELCOME] {self._session_profile.welcome_message.strip()}")

    def _start_hazard_phrase_refresh_loop(self) -> None:
        if not self._config.hazard_phrase_runtime_enabled:
            return
        refresher = getattr(self._team, "refresh_hazard_phrase_variants", None)
        if not callable(refresher):
            return
        if self._hazard_phrase_refresh_task is not None and not self._hazard_phrase_refresh_task.done():
            return
        self._hazard_phrase_refresh_task = asyncio.create_task(self._hazard_phrase_refresh_loop())

    async def _hazard_phrase_refresh_loop(self) -> None:
        refresh_sec = max(0.1, self._config.hazard_phrase_refresh_sec)
        while True:
            try:
                await asyncio.sleep(refresh_sec)
            except asyncio.CancelledError:
                raise

            if self._session_profile is None:
                continue
            refresher = getattr(self._team, "refresh_hazard_phrase_variants", None)
            if not callable(refresher):
                continue

            try:
                variants = await refresher(
                    session_profile=self._session_profile,
                    recent_alert_counts=dict(self._hazard_alert_counts),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._runtime_log.write(
                    {
                        "ts": time.time(),
                        "event": "hazard_phrase_refresh_failed",
                        "error": str(exc),
                    }
                )
                continue

            if not variants:
                continue

            self._hazard_monitor.update_speech_variants(variants)
            self._session_profile = replace(
                self._session_profile,
                hazard_profile=replace(
                    self._session_profile.hazard_profile,
                    speech_variants=_merge_speech_variants(
                        self._session_profile.hazard_profile.speech_variants,
                        variants,
                    ),
                ),
            )
            self._runtime_log.write(
                {
                    "ts": time.time(),
                    "event": "hazard_phrase_refresh_applied",
                    "rule_count": len(variants),
                    "rules": sorted(variants.keys()),
                }
            )

    async def _run_shutdown_debrief(self, reason: str) -> None:
        if self._shutdown_debrief_emitted:
            return

        snapshots = list(self._session_snapshots)
        if not snapshots:
            window_sec = max(60.0, self._config.review_window_sec)
            snapshots = self._udp.window(window_sec)
        if not snapshots:
            self._runtime_log.write(
                {
                    "ts": time.time(),
                    "event": "shutdown_debrief_skipped",
                    "reason": reason,
                    "details": "No snapshots available.",
                }
            )
            return

        review = self._build_shutdown_review(snapshots)
        try:
            decision = await self._team.run_review(review, session_profile=self._session_profile)
        except Exception as exc:  # noqa: BLE001
            self._runtime_log.write(
                {
                    "ts": time.time(),
                    "event": "shutdown_debrief_failed",
                    "reason": reason,
                    "error": str(exc),
                }
            )
            return

        self._runtime_log.write(
            {
                "ts": time.time(),
                "event": "shutdown_debrief",
                "flight_index": self._flight_index,
                "reason": reason,
                "phase": self._phase_state.phase.value,
                "review_window": asdict(review),
                "decision": asdict(decision),
                "hazard_events_count": self._hazard_events_count,
            }
        )
        await self._memory.record_event(
            "shutdown_debrief",
            {
                "reason": reason,
                "phase": self._phase_state.phase.value,
                "review_window": asdict(review),
                "decision": asdict(decision),
                "hazard_events_count": self._hazard_events_count,
            },
        )
        self._shutdown_debrief_emitted = True

        print(f"[SHUTDOWN REVIEW] {decision.summary}")

        shutdown_text = _select_coach_text(decision)
        if self._nonurgent_speak_enabled and shutdown_text:
            if _is_low_value_coach_text(shutdown_text):
                self._runtime_log.write(
                    {
                        "ts": time.time(),
                        "event": "shutdown_debrief_speech_suppressed",
                        "reason": "low_value_text",
                        "text": shutdown_text,
                    }
                )
            elif self._speech.recent_urgent(self._config.nonurgent_suppress_after_urgent_sec):
                self._runtime_log.write(
                    {
                        "ts": time.time(),
                        "event": "shutdown_debrief_speech_suppressed",
                        "reason": "recent_urgent",
                    }
                )
            else:
                spoke = await self._speech.speak_nonurgent(shutdown_text)
                self._runtime_log.write(
                    {
                        "ts": time.time(),
                        "event": "shutdown_debrief_speech",
                        "spoken": spoke,
                        "text": shutdown_text,
                    }
                )

    async def _maybe_trigger_shutdown_debrief(self, snapshot: FlightSnapshot) -> None:
        if self._shutdown_debrief_emitted:
            return
        if not self._saw_airborne_segment:
            return

        if not self._is_shutdown_candidate(snapshot):
            self._shutdown_candidate_since = None
            return

        if self._shutdown_candidate_since is None:
            self._shutdown_candidate_since = snapshot.timestamp_sec
            return

        dwell_sec = snapshot.timestamp_sec - self._shutdown_candidate_since
        if dwell_sec < self._config.shutdown_detect_dwell_sec:
            return

        self._runtime_log.write(
            {
                "ts": time.time(),
                "event": "engine_shutdown_detected",
                "flight_index": self._flight_index,
                "phase": self._phase_state.phase.value,
                "dwell_sec": dwell_sec,
            }
        )
        print("[SHUTDOWN] Engine shutdown detected, running full-flight debrief.")
        await self._run_shutdown_debrief("engine_shutdown_detected")

    def _is_shutdown_candidate(self, snapshot: FlightSnapshot) -> bool:
        if not snapshot.on_ground:
            return False
        if self._phase_state.phase not in {FlightPhase.TAXI_IN, FlightPhase.PREFLIGHT}:
            return False

        gs_kt = (snapshot.groundspeed_m_s or 0.0) * 1.94384
        ias = snapshot.indicated_airspeed_kt or 0.0
        throttle = snapshot.throttle_ratio or 0.0
        park = snapshot.parking_brake_ratio or 0.0

        if gs_kt > 2.0 or ias > 8.0 or throttle > 0.12:
            return False

        if snapshot.engine_running is not None:
            return not snapshot.engine_running
        if snapshot.engine_rpm is not None:
            return snapshot.engine_rpm <= 200.0

        return park >= 0.5

    async def _maybe_start_new_flight_cycle(self, snapshot: FlightSnapshot) -> None:
        if not self._shutdown_debrief_emitted:
            return
        if not self._is_new_flight_activity(snapshot):
            return

        self._flight_index += 1
        self._reset_flight_cycle_state()
        self._runtime_log.write(
            {
                "ts": time.time(),
                "event": "flight_cycle_started",
                "flight_index": self._flight_index,
                "reason": "post_shutdown_activity",
            }
        )
        await self._memory.record_event(
            "flight_cycle_started",
            {
                "flight_index": self._flight_index,
                "reason": "post_shutdown_activity",
            },
        )
        print(f"[FLIGHT] New flight cycle started: #{self._flight_index}")

    def _is_new_flight_activity(self, snapshot: FlightSnapshot) -> bool:
        gs_kt = (snapshot.groundspeed_m_s or 0.0) * 1.94384
        ias = snapshot.indicated_airspeed_kt or 0.0
        throttle = snapshot.throttle_ratio or 0.0
        parking_brake = snapshot.parking_brake_ratio or 0.0

        engine_on = False
        if snapshot.engine_running is not None:
            engine_on = snapshot.engine_running
        elif snapshot.engine_rpm is not None:
            engine_on = snapshot.engine_rpm > 500.0

        if not snapshot.on_ground:
            return True

        if not engine_on:
            return False
        if throttle > 0.22:
            return True
        if gs_kt > 3.0:
            return True
        if ias > 10.0:
            return True
        if parking_brake < 0.2 and gs_kt > 1.5:
            return True
        return False

    def _reset_flight_cycle_state(self) -> None:
        self._phase_tracker = FlightPhaseTracker()
        self._review_builder = ReviewWindowBuilder()
        self._phase_state = PhaseState(
            phase=FlightPhase.PREFLIGHT,
            confidence=0.0,
            changed=False,
            previous_phase=None,
            changed_at_epoch=None,
        )
        self._hazard_events_count = 0
        self._hazard_alert_counts = {}
        self._session_snapshots = []
        self._phase_path = [FlightPhase.PREFLIGHT]
        self._saw_airborne_segment = False
        self._shutdown_candidate_since = None
        self._shutdown_debrief_emitted = False

    def _build_shutdown_review(self, snapshots: list[FlightSnapshot]) -> ReviewWindow:
        sampled = _downsample_snapshots(snapshots, max_samples=1800)
        review = self._review_builder.build(sampled, self._phase_state.phase)

        duration_sec = max(0.0, snapshots[-1].timestamp_sec - snapshots[0].timestamp_sec)
        phase_path = " -> ".join(phase.value for phase in self._phase_path)
        top_alerts = ", ".join(
            f"{alert_id} x{count}"
            for alert_id, count in sorted(
                self._hazard_alert_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:4]
        )
        if not top_alerts:
            top_alerts = "none"

        hints = list(review.event_hints)
        hints.extend(
            [
                (
                    f"Full-flight debrief across {duration_sec:.0f} seconds and "
                    f"{len(snapshots)} snapshots."
                ),
                f"Phase path: {phase_path}.",
                f"Urgent alerts triggered: {self._hazard_events_count}.",
                f"Top urgent alerts: {top_alerts}.",
            ]
        )

        return ReviewWindow(
            start_epoch=snapshots[0].timestamp_sec,
            end_epoch=snapshots[-1].timestamp_sec,
            phase=self._phase_state.phase,
            sample_count=len(sampled),
            metrics=review.metrics,
            event_hints=hints[:12],
        )


def _downsample_snapshots(snapshots: list[FlightSnapshot], max_samples: int) -> list[FlightSnapshot]:
    if max_samples <= 0 or len(snapshots) <= max_samples:
        return snapshots

    step = max(1, len(snapshots) // max_samples)
    sampled = snapshots[::step]
    if len(sampled) >= max_samples:
        sampled = sampled[: max_samples - 1]
    if sampled[-1].timestamp_sec != snapshots[-1].timestamp_sec:
        sampled.append(snapshots[-1])
    return sampled


def _is_priority_review(decision: TeamDecision) -> bool:
    corpus_parts = [decision.summary, decision.speak_text]
    corpus_parts.extend(decision.feedback_items)
    corpus = " ".join(part.strip().lower() for part in corpus_parts if part and part.strip())
    if not corpus:
        return False
    return any(keyword in corpus for keyword in PRIORITY_REVIEW_KEYWORDS)


def _select_coach_text(decision: TeamDecision) -> str:
    if decision.speak_text.strip():
        return _normalize_speech_text(decision.speak_text)
    for item in decision.feedback_items:
        if item.strip():
            return _normalize_speech_text(item)
    if decision.summary.strip():
        return _normalize_speech_text(decision.summary)
    return ""


def _normalize_speech_text(text: str, max_chars: int = 160) -> str:
    cleaned = " ".join(text.split()).strip()
    cleaned = re.sub(r"^[A-Za-z ]{1,32} review:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(however|but|and)\s*[:,\-]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = _humanize_coach_text(cleaned)
    if not cleaned:
        return ""
    cleaned = _truncate_text_boundary(cleaned, max_chars=max_chars)
    if cleaned and cleaned[-1] not in ".!?":
        cleaned = f"{cleaned}."
    return cleaned


def _humanize_coach_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"\([^)]{1,40}\)", "", cleaned)
    cleaned = re.sub(r"\bthis indicates\b", "That means", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bit indicates\b", "That means", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bwas observed\b", "was noted", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bwere observed\b", "were noted", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bwas detected\b", "was noted", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bwere detected\b", "were noted", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bimmediate coaching is needed on\b", "Let's focus on", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\brecommend\b", "Let's", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bemphasize\b", "Focus on", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
    if not cleaned:
        return ""

    lower = cleaned.lower()
    if not re.search(r"\b(you|we|let's)\b", lower):
        if any(token in lower for token in ACTIONABLE_COACH_KEYWORDS):
            cleaned = f"Let's {cleaned[0].lower() + cleaned[1:]}" if len(cleaned) > 1 else cleaned
    return cleaned


def _is_low_value_coach_text(text: str) -> bool:
    lower = text.lower().strip()
    if not lower:
        return True
    has_low_marker = any(marker in lower for marker in LOW_VALUE_COACH_MARKERS)
    has_no_detect_pattern = bool(
        re.search(r"\bno\b.{0,35}\b(detected|observed|noted|issues?)\b", lower)
    )
    has_action = any(keyword in lower for keyword in ACTIONABLE_COACH_KEYWORDS)
    return (has_low_marker or has_no_detect_pattern) and not has_action


def _truncate_text_boundary(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[: max_chars + 1]
    boundary = head.rfind(" ")
    if boundary >= int(max_chars * 0.6):
        head = head[:boundary]
    else:
        head = text[:max_chars]
    return head.rstrip(" ,;:-")


def _merge_speech_variants(
    current: dict[str, list[str]],
    updates: dict[str, list[str]],
) -> dict[str, list[str]]:
    merged = {
        rule: list(lines)
        for rule, lines in current.items()
    }
    for rule, lines in updates.items():
        merged[rule] = list(lines)
    return merged


def _profile_console_payload(profile: SessionProfile) -> dict[str, Any]:
    variant_counts = {
        rule: len(lines)
        for rule, lines in profile.hazard_profile.speech_variants.items()
    }
    return {
        "aircraft_icao": profile.aircraft_icao,
        "aircraft_category": profile.aircraft_category,
        "confidence": round(profile.confidence, 3),
        "assumptions": profile.assumptions[:5],
        "hazard_enabled_rules": list(profile.hazard_profile.enabled_rules),
        "hazard_thresholds": dict(profile.hazard_profile.thresholds),
        "hazard_speech_variant_counts": variant_counts,
    }
