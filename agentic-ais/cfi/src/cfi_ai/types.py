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
    engine_running: bool | None = None
    engine_rpm: float | None = None
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


@dataclass(frozen=True)
class HazardProfile:
    enabled_rules: list[str] = field(
        default_factory=lambda: [
            "stall_or_low_speed",
            "excessive_sink_low_alt",
            "high_bank_low_alt",
            "pull_up_now",
            "excessive_taxi_speed",
            "unstable_approach_fast_or_sink",
        ]
    )
    thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "low_airspeed_kt": 50.0,
            "low_airspeed_min_agl_ft": 100.0,
            "excessive_sink_fpm": -1500.0,
            "excessive_sink_max_agl_ft": 1000.0,
            "high_bank_deg": 45.0,
            "high_bank_max_agl_ft": 1000.0,
            "pull_up_fpm": -1000.0,
            "pull_up_max_agl_ft": 300.0,
            "max_taxi_speed_kt": 30.0,
            "max_taxi_ias_kt": 35.0,
            "unstable_approach_max_ias_kt": 95.0,
            "unstable_approach_min_sink_fpm": -1000.0,
            "unstable_approach_max_agl_ft": 1000.0,
            "taxi_takeoff_roll_throttle_ratio": 0.65,
            "taxi_takeoff_roll_ias_kt": 35.0,
            "taxi_takeoff_roll_gs_kt": 30.0,
            "taxi_in_rollout_clear_gs_kt": 25.0,
            "taxi_in_rollout_clear_ias_kt": 30.0,
        }
    )
    speech_variants: dict[str, list[str]] = field(
        default_factory=lambda: {
            "stall_or_low_speed": [
                "Airspeed critical. Lower the nose and add power now.",
                "Watch your energy. Reduce pitch and add power immediately.",
                "Low airspeed. Recover now with pitch and power.",
            ],
            "excessive_sink_low_alt": [
                "Sink rate. Reduce descent and stabilize immediately.",
                "High sink near the ground. Add power and arrest descent.",
                "Descent is too high. Stabilize vertical speed now.",
            ],
            "high_bank_low_alt": [
                "Bank angle. Roll wings level and stabilize the approach.",
                "Steep bank low altitude. Level the wings now.",
                "Too much bank close to ground. Reduce bank and stabilize.",
            ],
            "pull_up_now": [
                "Pull up. Arrest descent now.",
                "Terrain risk. Pull up and recover immediately.",
                "Descent unsafe near ground. Pitch up now.",
            ],
            "excessive_taxi_speed": [
                "Slow down taxi speed now and regain full directional control.",
                "Taxi too fast. Reduce speed and maintain centerline.",
                "Ease off speed on taxi. Keep control and spacing.",
            ],
            "unstable_approach_fast_or_sink": [
                "Unstable approach. Correct now or execute a go-around.",
                "Approach not stabilized. Fix speed and sink or go around.",
                "Profile unstable. Stabilize immediately or go around.",
            ],
        }
    )
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SessionProfile:
    aircraft_icao: str
    aircraft_category: str
    confidence: float
    assumptions: list[str]
    welcome_message: str
    hazard_profile: HazardProfile = field(default_factory=HazardProfile)
    raw_llm_output: str = ""


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
