from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol


class FlightPhase(str, Enum):
    PREFLIGHT = "preflight"
    TAXI_OUT = "taxi_out"
    TAKEOFF = "takeoff"
    INITIAL_CLIMB = "initial_climb"
    CRUISE = "cruise"
    DESCENT = "descent"
    APPROACH = "approach"
    LANDING = "landing"
    TAXI_IN = "taxi_in"


@dataclass(frozen=True)
class FlightSnapshot:
    timestamp_sec: float
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    elevation_m: float | None = None
    groundspeed_m_s: float | None = None
    indicated_airspeed_kt: float | None = None
    heading_true_deg: float | None = None
    magnetic_heading_deg: float | None = None
    vertical_speed_fpm: float | None = None
    roll_deg: float | None = None
    pitch_deg: float | None = None
    throttle_ratio: float | None = None
    flap_ratio: float | None = None
    parking_brake_ratio: float | None = None
    com1_hz: int | None = None
    on_ground: bool = False
    stall_warning: bool = False

    def agl_ft(self, field_elevation_m: float | None) -> float | None:
        if self.elevation_m is None or field_elevation_m is None:
            return None
        return (self.elevation_m - field_elevation_m) * 3.28084

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PhaseState:
    phase: FlightPhase
    confidence: float
    changed: bool
    previous_phase: FlightPhase | None
    changed_at_epoch: float | None = None


@dataclass(frozen=True)
class HazardAlert:
    alert_id: str
    severity: str
    message: str
    speak_text: str
    cooldown_sec: float
    triggered_at_epoch: float


@dataclass(frozen=True)
class ReviewWindow:
    start_epoch: float
    end_epoch: float
    phase: FlightPhase
    sample_count: int
    metrics: dict[str, float]
    event_hints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TeamDecision:
    phase: FlightPhase
    summary: str
    feedback_items: list[str]
    speak_now: bool
    speak_text: str
    raw_master_output: str = ""


class UdpStateSource(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def latest(self) -> FlightSnapshot | None: ...

    def window(self, seconds: float) -> list[FlightSnapshot]: ...


class SpeechSink(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def speak_urgent(self, text: str, key: str) -> bool: ...

    async def speak_nonurgent(self, text: str) -> bool: ...

    def recent_urgent(self, within_sec: float) -> bool: ...
