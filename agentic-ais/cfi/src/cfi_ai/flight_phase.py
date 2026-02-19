from __future__ import annotations

from dataclasses import dataclass

from cfi_ai.types import FlightPhase, FlightSnapshot, PhaseState


DEFAULT_MIN_DWELL_SEC: dict[FlightPhase, float] = {
    FlightPhase.PREFLIGHT: 3.0,
    FlightPhase.TAXI_OUT: 3.0,
    FlightPhase.TAKEOFF: 2.0,
    FlightPhase.INITIAL_CLIMB: 3.0,
    FlightPhase.CRUISE: 5.0,
    FlightPhase.DESCENT: 4.0,
    FlightPhase.APPROACH: 3.0,
    FlightPhase.LANDING: 1.0,
    FlightPhase.TAXI_IN: 3.0,
}


@dataclass
class _TrackerState:
    phase: FlightPhase = FlightPhase.PREFLIGHT
    phase_started_at: float = 0.0
    candidate: FlightPhase = FlightPhase.PREFLIGHT
    candidate_since: float = 0.0
    field_elevation_m: float | None = None
    was_airborne: bool = False


class FlightPhaseTracker:
    def __init__(self, min_dwell_sec: dict[FlightPhase, float] | None = None) -> None:
        self._min_dwell_sec = min_dwell_sec or DEFAULT_MIN_DWELL_SEC
        self._s = _TrackerState()

    @property
    def phase(self) -> FlightPhase:
        return self._s.phase

    def update(self, snapshot: FlightSnapshot) -> PhaseState:
        if self._s.phase_started_at <= 0:
            self._s.phase_started_at = snapshot.timestamp_sec
            self._s.candidate_since = snapshot.timestamp_sec

        ias = snapshot.indicated_airspeed_kt or 0.0
        gs_kt = (snapshot.groundspeed_m_s or 0.0) * 1.94384

        if snapshot.on_ground and snapshot.elevation_m is not None and gs_kt < 25:
            self._s.field_elevation_m = snapshot.elevation_m

        if not snapshot.on_ground and ias >= 60:
            self._s.was_airborne = True

        candidate = self._determine_candidate(snapshot)
        if candidate != self._s.candidate:
            self._s.candidate = candidate
            self._s.candidate_since = snapshot.timestamp_sec

        dwell_required = self._min_dwell_sec.get(candidate, 3.0)
        dwell = max(0.0, snapshot.timestamp_sec - self._s.candidate_since)

        changed = False
        previous = self._s.phase
        if candidate != self._s.phase and dwell >= dwell_required:
            self._s.phase = candidate
            self._s.phase_started_at = snapshot.timestamp_sec
            changed = True

        if candidate == self._s.phase:
            confidence = 1.0
        else:
            confidence = min(0.99, dwell / max(0.1, dwell_required))

        return PhaseState(
            phase=self._s.phase,
            confidence=confidence,
            changed=changed,
            previous_phase=previous if changed else None,
            changed_at_epoch=snapshot.timestamp_sec if changed else None,
        )

    def _determine_candidate(self, snapshot: FlightSnapshot) -> FlightPhase:
        ias = snapshot.indicated_airspeed_kt or 0.0
        gs_kt = (snapshot.groundspeed_m_s or 0.0) * 1.94384
        vs = snapshot.vertical_speed_fpm or 0.0
        throttle = snapshot.throttle_ratio or 0.0
        park = snapshot.parking_brake_ratio or 0.0
        agl_ft = snapshot.agl_ft(self._s.field_elevation_m)

        if snapshot.on_ground:
            if self._s.was_airborne:
                if gs_kt > 40 or ias > 45:
                    return FlightPhase.LANDING
                return FlightPhase.TAXI_IN

            if gs_kt < 2 and ias < 5 and throttle < 0.2 and park > 0.3:
                return FlightPhase.PREFLIGHT
            if gs_kt >= 35 and ias >= 40:
                return FlightPhase.TAKEOFF
            return FlightPhase.TAXI_OUT

        # Airborne
        if agl_ft is None:
            # Fallback if no elevation baseline captured yet.
            if vs > 300:
                return FlightPhase.INITIAL_CLIMB
            if vs < -300:
                return FlightPhase.DESCENT
            return FlightPhase.CRUISE

        if agl_ft < 250 and vs < -150 and ias > 55:
            return FlightPhase.LANDING
        if agl_ft <= 2500 and vs < -400:
            return FlightPhase.APPROACH
        if vs > 300 and agl_ft < 3000:
            return FlightPhase.INITIAL_CLIMB
        if vs < -300 and agl_ft > 2500:
            return FlightPhase.DESCENT
        if abs(vs) < 400 and agl_ft >= 3000:
            return FlightPhase.CRUISE

        if self._s.phase in {FlightPhase.TAKEOFF, FlightPhase.INITIAL_CLIMB}:
            return FlightPhase.INITIAL_CLIMB
        if self._s.phase in {FlightPhase.APPROACH, FlightPhase.LANDING}:
            return FlightPhase.APPROACH
        return FlightPhase.CRUISE
