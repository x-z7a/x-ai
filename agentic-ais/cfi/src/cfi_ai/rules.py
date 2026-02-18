from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from cfi_ai.types import FlightPhase, MonitorSample, RuleFinding, Severity

P0_COOLDOWN_SEC = 3.0
P1_COOLDOWN_SEC = 8.0
P2_COOLDOWN_SEC = 12.0

STEEP_TURN_SUSTAIN_SEC = 12.0
SLOW_FLIGHT_SUSTAIN_SEC = 12.0
CLIMB_BAND_SUSTAIN_SEC = 8.0
AI_MAX_FINDINGS_PER_EVAL = 3


@dataclass
class RuleEngineState:
    phase: FlightPhase = FlightPhase.PRE_FLIGHT
    field_elevation_ft: float | None = None
    had_airborne: bool = False
    shutdown_confirmed: bool = False

    previous_on_ground: bool | None = None
    previous_vertical_speed_fpm: float | None = None

    climb_out_violation_since: float | None = None

    steep_turn_candidate_since: float | None = None
    steep_turn_reference_alt_ft: float | None = None

    slow_flight_candidate_since: float | None = None
    slow_flight_reference_alt_ft: float | None = None

    level_reference_alt_ft: float | None = None
    level_reference_heading_deg: float | None = None

    stall_event_started_at: float | None = None
    stall_recovery_seconds: list[float] = field(default_factory=list)

    shutdown_candidate_since: float | None = None


class RuleEngine:
    def __init__(self, engine_shutdown_hold_sec: float = 15.0) -> None:
        self._state = RuleEngineState()
        self._engine_shutdown_hold_sec = engine_shutdown_hold_sec

    @property
    def phase(self) -> FlightPhase:
        return self._state.phase

    @property
    def had_airborne(self) -> bool:
        return self._state.had_airborne

    @property
    def shutdown_confirmed(self) -> bool:
        return self._state.shutdown_confirmed

    @property
    def stall_recovery_seconds(self) -> list[float]:
        return list(self._state.stall_recovery_seconds)

    def update(self, sample: MonitorSample) -> list[RuleFinding]:
        self._update_field_elevation(sample)
        self._update_maneuver_trackers(sample)

        phase = self._detect_phase(sample)
        self._state.phase = phase

        findings = self._evaluate_rules(sample)

        self._update_shutdown_state(sample)
        if self._state.shutdown_confirmed:
            self._state.phase = FlightPhase.SHUTDOWN

        self._state.previous_on_ground = sample.on_ground
        self._state.previous_vertical_speed_fpm = sample.vertical_speed_fpm

        return findings

    def _update_field_elevation(self, sample: MonitorSample) -> None:
        if sample.altitude_ft_msl is None:
            return
        groundspeed = sample.groundspeed_kt or 0.0
        if sample.on_ground and groundspeed < 40.0:
            self._state.field_elevation_ft = sample.altitude_ft_msl

    def _update_maneuver_trackers(self, sample: MonitorSample) -> None:
        timestamp = sample.timestamp_epoch
        bank_deg = abs(sample.bank_deg or 0.0)
        agl_ft = sample.agl_ft
        ias = sample.indicated_airspeed_kt
        flaps = sample.flaps_ratio
        vs = abs(sample.vertical_speed_fpm or 0.0)

        steep_turn_candidate = (
            (not sample.on_ground)
            and agl_ft is not None
            and agl_ft > 1500.0
            and 35.0 <= bank_deg <= 60.0
        )
        if steep_turn_candidate:
            if self._state.steep_turn_candidate_since is None:
                self._state.steep_turn_candidate_since = timestamp
                self._state.steep_turn_reference_alt_ft = sample.altitude_ft_msl
        else:
            self._state.steep_turn_candidate_since = None
            self._state.steep_turn_reference_alt_ft = None

        slow_flight_candidate = (
            (not sample.on_ground)
            and ias is not None
            and 40.0 <= ias <= 70.0
            and flaps is not None
            and flaps > 0.2
            and vs < 300.0
        )
        if slow_flight_candidate:
            if self._state.slow_flight_candidate_since is None:
                self._state.slow_flight_candidate_since = timestamp
                self._state.slow_flight_reference_alt_ft = sample.altitude_ft_msl
        else:
            self._state.slow_flight_candidate_since = None
            self._state.slow_flight_reference_alt_ft = None

    def _detect_phase(self, sample: MonitorSample) -> FlightPhase:
        if self._state.shutdown_confirmed:
            return FlightPhase.SHUTDOWN

        groundspeed = sample.groundspeed_kt or 0.0
        ias = sample.indicated_airspeed_kt or 0.0
        vs = sample.vertical_speed_fpm or 0.0

        steep_turn_active = self._is_steep_turn_active(sample.timestamp_epoch)
        slow_flight_active = self._is_slow_flight_active(sample.timestamp_epoch)

        if self._state.had_airborne:
            if sample.on_ground:
                if groundspeed > 15.0:
                    return FlightPhase.LANDING_ROLLOUT
                return FlightPhase.TAXI_IN

            if steep_turn_active or slow_flight_active:
                return FlightPhase.MANEUVER

            if sample.agl_ft is not None and sample.agl_ft < 800.0 and vs < -200.0:
                return FlightPhase.APPROACH
            if vs > 300.0:
                return FlightPhase.CLIMB
            if vs < -300.0:
                return FlightPhase.DESCENT
            return FlightPhase.CRUISE

        if not sample.on_ground:
            self._state.had_airborne = True
            return FlightPhase.CLIMB

        if ias >= 40.0 or groundspeed >= 30.0:
            return FlightPhase.TAKEOFF_ROLL
        if groundspeed >= 2.0:
            return FlightPhase.TAXI_OUT
        return FlightPhase.PRE_FLIGHT

    def _is_steep_turn_active(self, timestamp: float) -> bool:
        start = self._state.steep_turn_candidate_since
        return start is not None and (timestamp - start) >= STEEP_TURN_SUSTAIN_SEC

    def _is_slow_flight_active(self, timestamp: float) -> bool:
        start = self._state.slow_flight_candidate_since
        return start is not None and (timestamp - start) >= SLOW_FLIGHT_SUSTAIN_SEC

    def _evaluate_rules(self, sample: MonitorSample) -> list[RuleFinding]:
        findings: list[RuleFinding] = []
        phase = self._state.phase
        t = sample.timestamp_epoch

        ias = sample.indicated_airspeed_kt
        agl_ft = sample.agl_ft
        vs = sample.vertical_speed_fpm
        bank = abs(sample.bank_deg or 0.0)
        groundspeed = sample.groundspeed_kt
        altitude_ft = sample.altitude_ft_msl
        heading = sample.heading_deg

        if sample.stall_warning_active:
            if self._state.stall_event_started_at is None:
                self._state.stall_event_started_at = t
            findings.append(
                _finding(
                    rule_id="stall_warning",
                    severity=Severity.P0,
                    phase=phase,
                    message="Stall warning active. Reduce angle of attack and recover immediately.",
                    timestamp=t,
                    cooldown_sec=P0_COOLDOWN_SEC,
                    evidence={"stall_warning_active": True},
                )
            )
        elif self._state.stall_event_started_at is not None:
            self._state.stall_recovery_seconds.append(max(0.0, t - self._state.stall_event_started_at))
            self._state.stall_event_started_at = None

        if (
            agl_ft is not None
            and vs is not None
            and (not sample.on_ground)
            and agl_ft < 500.0
            and vs < -1200.0
        ):
            findings.append(
                _finding(
                    rule_id="low_agl_sink_rate",
                    severity=Severity.P0,
                    phase=phase,
                    message="Dangerous sink rate close to the ground.",
                    timestamp=t,
                    cooldown_sec=P0_COOLDOWN_SEC,
                    evidence={"agl_ft": agl_ft, "vertical_speed_fpm": vs},
                )
            )

        if (
            agl_ft is not None
            and (not sample.on_ground)
            and agl_ft < 1000.0
            and bank > 45.0
        ):
            findings.append(
                _finding(
                    rule_id="low_agl_excessive_bank",
                    severity=Severity.P0,
                    phase=phase,
                    message="Excessive bank angle at low altitude.",
                    timestamp=t,
                    cooldown_sec=P0_COOLDOWN_SEC,
                    evidence={"agl_ft": agl_ft, "bank_deg": bank},
                )
            )

        touchdown = self._state.previous_on_ground is False and sample.on_ground
        prev_vs = self._state.previous_vertical_speed_fpm
        if touchdown and prev_vs is not None and prev_vs < -700.0:
            findings.append(
                _finding(
                    rule_id="hard_landing",
                    severity=Severity.P0,
                    phase=phase,
                    message="Hard landing detected. Manage flare and descent rate earlier.",
                    timestamp=t,
                    cooldown_sec=P0_COOLDOWN_SEC,
                    evidence={"touchdown_vertical_speed_fpm": prev_vs},
                )
            )

        if phase in {FlightPhase.TAXI_OUT, FlightPhase.TAXI_IN} and groundspeed is not None and groundspeed > 20.0:
            findings.append(
                _finding(
                    rule_id="taxi_speed_high",
                    severity=Severity.P1,
                    phase=phase,
                    message="Taxi speed too high for checkride standards.",
                    timestamp=t,
                    cooldown_sec=P1_COOLDOWN_SEC,
                    evidence={"groundspeed_kt": groundspeed},
                )
            )

        climb_out_violation = (
            phase == FlightPhase.CLIMB
            and agl_ft is not None
            and agl_ft < 2000.0
            and ias is not None
            and (ias < 60.0 or ias > 90.0)
        )
        if climb_out_violation:
            if self._state.climb_out_violation_since is None:
                self._state.climb_out_violation_since = t
            elif (t - self._state.climb_out_violation_since) >= CLIMB_BAND_SUSTAIN_SEC:
                findings.append(
                    _finding(
                        rule_id="climb_out_airspeed_out_of_band",
                        severity=Severity.P1,
                        phase=phase,
                        message="Climb-out airspeed outside 60 to 90 knots.",
                        timestamp=t,
                        cooldown_sec=P1_COOLDOWN_SEC,
                        evidence={"indicated_airspeed_kt": ias, "agl_ft": agl_ft},
                    )
                )
        else:
            self._state.climb_out_violation_since = None

        unstable_approach = (
            phase == FlightPhase.APPROACH
            and agl_ft is not None
            and agl_ft < 500.0
            and (
                (ias is not None and (ias < 55.0 or ias > 95.0))
                or (vs is not None and vs < -1000.0)
            )
        )
        if unstable_approach:
            findings.append(
                _finding(
                    rule_id="unstable_approach",
                    severity=Severity.P1,
                    phase=phase,
                    message="Unstable approach detected below 500 feet AGL.",
                    timestamp=t,
                    cooldown_sec=P1_COOLDOWN_SEC,
                    evidence={
                        "agl_ft": agl_ft,
                        "indicated_airspeed_kt": ias,
                        "vertical_speed_fpm": vs,
                    },
                )
            )

        is_level_segment = (
            (not sample.on_ground)
            and vs is not None
            and abs(vs) < 300.0
            and phase in {FlightPhase.CRUISE, FlightPhase.MANEUVER}
        )
        if is_level_segment:
            if self._state.level_reference_alt_ft is None:
                self._state.level_reference_alt_ft = altitude_ft
                self._state.level_reference_heading_deg = heading
            else:
                if altitude_ft is not None and self._state.level_reference_alt_ft is not None:
                    if abs(altitude_ft - self._state.level_reference_alt_ft) > 150.0:
                        findings.append(
                            _finding(
                                rule_id="level_altitude_wander",
                                severity=Severity.P2,
                                phase=phase,
                                message="Altitude wander exceeds 150 feet in a level segment.",
                                timestamp=t,
                                cooldown_sec=P2_COOLDOWN_SEC,
                                evidence={
                                    "altitude_ft_msl": altitude_ft,
                                    "reference_altitude_ft_msl": self._state.level_reference_alt_ft,
                                },
                            )
                        )
                if heading is not None and self._state.level_reference_heading_deg is not None:
                    if _heading_delta_deg(heading, self._state.level_reference_heading_deg) > 20.0:
                        findings.append(
                            _finding(
                                rule_id="level_heading_wander",
                                severity=Severity.P2,
                                phase=phase,
                                message="Heading wander exceeds 20 degrees in a level segment.",
                                timestamp=t,
                                cooldown_sec=P2_COOLDOWN_SEC,
                                evidence={
                                    "heading_deg": heading,
                                    "reference_heading_deg": self._state.level_reference_heading_deg,
                                },
                            )
                        )
        else:
            self._state.level_reference_alt_ft = None
            self._state.level_reference_heading_deg = None

        if self._is_steep_turn_active(t):
            reference_alt = self._state.steep_turn_reference_alt_ft
            bank_error = abs(bank - 45.0)
            altitude_drift = (
                abs((altitude_ft or 0.0) - reference_alt)
                if (altitude_ft is not None and reference_alt is not None)
                else None
            )
            if bank_error > 10.0 or (altitude_drift is not None and altitude_drift > 100.0):
                findings.append(
                    _finding(
                        rule_id="steep_turn_quality",
                        severity=Severity.P2,
                        phase=phase,
                        message="Steep-turn quality outside target (bank/altitude control).",
                        timestamp=t,
                        cooldown_sec=P2_COOLDOWN_SEC,
                        evidence={
                            "bank_deg": bank,
                            "bank_error_deg": bank_error,
                            "altitude_drift_ft": altitude_drift,
                        },
                    )
                )

        if self._is_slow_flight_active(t):
            reference_alt = self._state.slow_flight_reference_alt_ft
            altitude_drift = (
                abs((altitude_ft or 0.0) - reference_alt)
                if (altitude_ft is not None and reference_alt is not None)
                else None
            )
            if altitude_drift is not None and altitude_drift > 100.0:
                findings.append(
                    _finding(
                        rule_id="slow_flight_quality",
                        severity=Severity.P2,
                        phase=phase,
                        message="Slow-flight altitude control exceeds 100 feet drift.",
                        timestamp=t,
                        cooldown_sec=P2_COOLDOWN_SEC,
                        evidence={"altitude_drift_ft": altitude_drift},
                    )
                )

        return findings

    def _update_shutdown_state(self, sample: MonitorSample) -> None:
        if self._state.shutdown_confirmed:
            return

        stationary = (sample.groundspeed_kt or 0.0) < 2.0
        engines_off = sample.engine_running is not None and all(v == 0 for v in sample.engine_running)
        conditions_met = (
            self._state.had_airborne
            and sample.on_ground
            and stationary
            and engines_off
        )

        if not conditions_met:
            self._state.shutdown_candidate_since = None
            return

        if self._state.shutdown_candidate_since is None:
            self._state.shutdown_candidate_since = sample.timestamp_epoch
            return

        elapsed = sample.timestamp_epoch - self._state.shutdown_candidate_since
        if elapsed >= self._engine_shutdown_hold_sec:
            self._state.shutdown_confirmed = True


def _finding(
    *,
    rule_id: str,
    severity: Severity,
    phase: FlightPhase,
    message: str,
    timestamp: float,
    cooldown_sec: float,
    evidence: dict[str, object],
) -> RuleFinding:
    return RuleFinding(
        rule_id=rule_id,
        severity=severity,
        phase=phase,
        message=message,
        evidence=evidence,
        timestamp_epoch=timestamp,
        cooldown_sec=cooldown_sec,
    )


def _heading_delta_deg(a: float, b: float) -> float:
    diff = abs((a - b) % 360.0)
    return min(diff, 360.0 - diff)


def normalize_ai_findings(
    raw_findings: list[dict[str, Any]],
    *,
    phase: FlightPhase,
    timestamp_epoch: float,
) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    seen: set[tuple[str, str]] = set()

    for item in raw_findings[:AI_MAX_FINDINGS_PER_EVAL]:
        if not isinstance(item, dict):
            continue

        severity = _parse_severity(item.get("severity"))
        if severity is None:
            continue

        message = str(item.get("message", "")).strip()
        if not message:
            continue

        base_rule_id = _normalize_rule_id(str(item.get("rule_id", "")).strip())
        if not base_rule_id:
            base_rule_id = f"suggested_{severity.value.lower()}"

        dedupe_key = (base_rule_id, message.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        evidence = item.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}

        cooldown = _to_float(item.get("cooldown_sec"))
        if cooldown is None or cooldown <= 0.0:
            cooldown = _default_cooldown_for_severity(severity)

        findings.append(
            _finding(
                rule_id=f"ai_{base_rule_id}",
                severity=severity,
                phase=phase,
                message=message[:220],
                timestamp=timestamp_epoch,
                cooldown_sec=cooldown,
                evidence=evidence,
            )
        )

    return findings


def _parse_severity(value: Any) -> Severity | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    try:
        return Severity(candidate)
    except ValueError:
        return None


def _default_cooldown_for_severity(severity: Severity) -> float:
    if severity == Severity.P0:
        return P0_COOLDOWN_SEC
    if severity == Severity.P1:
        return P1_COOLDOWN_SEC
    return P2_COOLDOWN_SEC


def _normalize_rule_id(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    return cleaned


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
