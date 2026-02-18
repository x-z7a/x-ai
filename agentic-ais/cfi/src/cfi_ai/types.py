from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FlightPhase(str, Enum):
    PRE_FLIGHT = "preflight"
    TAXI_OUT = "taxi_out"
    TAKEOFF_ROLL = "takeoff_roll"
    CLIMB = "climb"
    CRUISE = "cruise"
    MANEUVER = "maneuver"
    DESCENT = "descent"
    APPROACH = "approach"
    LANDING_ROLLOUT = "landing_rollout"
    TAXI_IN = "taxi_in"
    SHUTDOWN = "shutdown"


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


@dataclass(frozen=True)
class MonitorSample:
    timestamp_epoch: float
    on_ground: bool
    latitude: float | None
    longitude: float | None
    altitude_ft_msl: float | None
    agl_ft: float | None
    groundspeed_kt: float | None
    indicated_airspeed_kt: float | None
    vertical_speed_fpm: float | None
    heading_deg: float | None
    bank_deg: float | None
    pitch_deg: float | None
    flaps_ratio: float | None
    parking_brake_ratio: float | None
    stall_warning_active: bool | None
    engine_running: tuple[int, ...] | None
    engine_count: int | None = None
    aircraft_icao: str | None = None
    aircraft_name: str | None = None
    aircraft_tailnum: str | None = None
    raw_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleFinding:
    rule_id: str
    severity: Severity
    phase: FlightPhase
    message: str
    evidence: dict[str, Any]
    timestamp_epoch: float
    cooldown_sec: float


@dataclass(frozen=True)
class FlightEventLogEntry:
    timestamp_epoch: float
    category: str
    phase: FlightPhase
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FlightDebrief:
    total_findings: int
    findings_by_severity: dict[str, int]
    key_events: list[str]
    strengths: list[str]
    improvement_items: list[str]
    spoken_segments: list[str]
