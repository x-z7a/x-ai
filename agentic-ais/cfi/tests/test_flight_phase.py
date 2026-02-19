from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.flight_phase import FlightPhaseTracker
from cfi_ai.types import FlightPhase, FlightSnapshot


def _snap(
    ts: float,
    *,
    on_ground: bool,
    elevation_m: float,
    gs_m_s: float,
    ias: float,
    vs: float,
    throttle: float = 0.4,
    brake: float = 0.0,
) -> FlightSnapshot:
    return FlightSnapshot(
        timestamp_sec=ts,
        on_ground=on_ground,
        elevation_m=elevation_m,
        groundspeed_m_s=gs_m_s,
        indicated_airspeed_kt=ias,
        vertical_speed_fpm=vs,
        throttle_ratio=throttle,
        parking_brake_ratio=brake,
    )


class TestFlightPhaseTracker(unittest.TestCase):
    def test_full_phase_sequence(self) -> None:
        tracker = FlightPhaseTracker()

        phase = tracker.update(
            _snap(0.0, on_ground=True, elevation_m=100.0, gs_m_s=0.0, ias=0.0, vs=0.0, throttle=0.0, brake=1.0)
        ).phase
        self.assertEqual(phase, FlightPhase.PREFLIGHT)

        for ts in (1.0, 2.0, 4.2):
            state = tracker.update(
                _snap(ts, on_ground=True, elevation_m=100.0, gs_m_s=4.0, ias=10.0, vs=0.0, throttle=0.25)
            )
        self.assertEqual(state.phase, FlightPhase.TAXI_OUT)

        for ts in (5.0, 6.2, 7.4):
            state = tracker.update(
                _snap(ts, on_ground=True, elevation_m=100.0, gs_m_s=22.0, ias=55.0, vs=100.0, throttle=0.8)
            )
        self.assertEqual(state.phase, FlightPhase.TAKEOFF)

        for ts in (8.0, 9.2, 11.5):
            state = tracker.update(
                _snap(ts, on_ground=False, elevation_m=220.0, gs_m_s=45.0, ias=80.0, vs=900.0, throttle=0.85)
            )
        self.assertEqual(state.phase, FlightPhase.INITIAL_CLIMB)

        for ts in (12.0, 14.0, 16.0, 18.0, 20.5):
            state = tracker.update(
                _snap(ts, on_ground=False, elevation_m=1250.0, gs_m_s=65.0, ias=105.0, vs=50.0, throttle=0.65)
            )
        self.assertEqual(state.phase, FlightPhase.CRUISE)

        for ts in (21.0, 22.5, 24.0, 25.5, 27.2):
            state = tracker.update(
                _snap(ts, on_ground=False, elevation_m=1050.0, gs_m_s=58.0, ias=98.0, vs=-700.0, throttle=0.3)
            )
        self.assertEqual(state.phase, FlightPhase.DESCENT)

        for ts in (28.0, 29.5, 31.2, 32.6):
            state = tracker.update(
                _snap(ts, on_ground=False, elevation_m=650.0, gs_m_s=50.0, ias=85.0, vs=-650.0, throttle=0.25)
            )
        self.assertEqual(state.phase, FlightPhase.APPROACH)

        for ts in (33.0, 34.2):
            state = tracker.update(
                _snap(ts, on_ground=False, elevation_m=120.0, gs_m_s=35.0, ias=60.0, vs=-500.0, throttle=0.2)
            )
        self.assertEqual(state.phase, FlightPhase.LANDING)

        for ts in (35.0, 36.5, 38.3):
            state = tracker.update(
                _snap(ts, on_ground=True, elevation_m=100.0, gs_m_s=6.0, ias=18.0, vs=0.0, throttle=0.1)
            )
        self.assertEqual(state.phase, FlightPhase.TAXI_IN)

    def test_antiflap_with_short_candidate(self) -> None:
        tracker = FlightPhaseTracker()
        tracker.update(
            _snap(0.0, on_ground=False, elevation_m=150.0, gs_m_s=40.0, ias=75.0, vs=900.0)
        )
        tracker.update(
            _snap(1.0, on_ground=False, elevation_m=180.0, gs_m_s=42.0, ias=78.0, vs=850.0)
        )
        tracker.update(
            _snap(4.5, on_ground=False, elevation_m=220.0, gs_m_s=44.0, ias=80.0, vs=800.0)
        )
        self.assertEqual(tracker.phase, FlightPhase.INITIAL_CLIMB)

        tracker.update(
            _snap(5.0, on_ground=False, elevation_m=1250.0, gs_m_s=60.0, ias=100.0, vs=0.0)
        )
        tracker.update(
            _snap(6.0, on_ground=False, elevation_m=1248.0, gs_m_s=60.0, ias=100.0, vs=0.0)
        )
        self.assertEqual(tracker.phase, FlightPhase.INITIAL_CLIMB)


if __name__ == "__main__":
    unittest.main()
