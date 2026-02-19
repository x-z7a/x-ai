from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.hazard_monitor import HazardMonitor
from cfi_ai.types import FlightPhase, FlightSnapshot, HazardProfile, PhaseState


def _phase(phase: FlightPhase = FlightPhase.APPROACH) -> PhaseState:
    return PhaseState(
        phase=phase,
        confidence=1.0,
        changed=False,
        previous_phase=None,
        changed_at_epoch=None,
    )


class TestHazardMonitor(unittest.TestCase):
    def test_low_speed_alert(self) -> None:
        monitor = HazardMonitor(urgent_cooldown_sec=8.0)
        snapshot = FlightSnapshot(
            timestamp_sec=10.0,
            on_ground=False,
            indicated_airspeed_kt=45.0,
            vertical_speed_fpm=-200.0,
        )
        alerts = monitor.evaluate(snapshot, _phase())
        alert_ids = {a.alert_id for a in alerts}
        self.assertIn("stall_or_low_speed", alert_ids)

    def test_high_bank_low_alt_alert(self) -> None:
        monitor = HazardMonitor(urgent_cooldown_sec=8.0)
        monitor.evaluate(
            FlightSnapshot(
                timestamp_sec=1.0,
                on_ground=True,
                elevation_m=100.0,
                groundspeed_m_s=0.0,
                indicated_airspeed_kt=0.0,
            ),
            _phase(),
        )

        snapshot = FlightSnapshot(
            timestamp_sec=12.0,
            on_ground=False,
            elevation_m=220.0,
            indicated_airspeed_kt=82.0,
            vertical_speed_fpm=-300.0,
            roll_deg=52.0,
        )
        alerts = monitor.evaluate(snapshot, _phase())
        alert_ids = {a.alert_id for a in alerts}
        self.assertIn("high_bank_low_alt", alert_ids)

    def test_no_alert_on_ground(self) -> None:
        monitor = HazardMonitor(urgent_cooldown_sec=8.0)
        snapshot = FlightSnapshot(
            timestamp_sec=10.0,
            on_ground=True,
            indicated_airspeed_kt=0.0,
            vertical_speed_fpm=0.0,
        )
        alerts = monitor.evaluate(snapshot, _phase())
        self.assertEqual(alerts, [])

    def test_excessive_taxi_speed_alert(self) -> None:
        monitor = HazardMonitor(urgent_cooldown_sec=8.0)
        snapshot = FlightSnapshot(
            timestamp_sec=20.0,
            on_ground=True,
            groundspeed_m_s=20.0,  # ~39 kt
            indicated_airspeed_kt=41.0,
        )
        alerts = monitor.evaluate(snapshot, _phase(FlightPhase.TAXI_OUT))
        alert_ids = {a.alert_id for a in alerts}
        self.assertIn("excessive_taxi_speed", alert_ids)

    def test_plane_specific_threshold_profile(self) -> None:
        profile = HazardProfile(
            enabled_rules=["stall_or_low_speed"],
            thresholds={
                "low_airspeed_kt": 120.0,
                "low_airspeed_min_agl_ft": 100.0,
            },
            notes=["Jet-like low-speed threshold for test."],
        )
        monitor = HazardMonitor(urgent_cooldown_sec=8.0, hazard_profile=profile)
        snapshot = FlightSnapshot(
            timestamp_sec=25.0,
            on_ground=False,
            indicated_airspeed_kt=110.0,
            elevation_m=500.0,
            vertical_speed_fpm=0.0,
        )
        alerts = monitor.evaluate(snapshot, _phase())
        alert_ids = {a.alert_id for a in alerts}
        self.assertIn("stall_or_low_speed", alert_ids)

    def test_taxi_speed_suppressed_during_takeoff_roll_transition(self) -> None:
        monitor = HazardMonitor(urgent_cooldown_sec=8.0)
        snapshot = FlightSnapshot(
            timestamp_sec=30.0,
            on_ground=True,
            groundspeed_m_s=19.0,  # ~37 kt
            indicated_airspeed_kt=44.0,
            throttle_ratio=0.9,
        )
        alerts = monitor.evaluate(snapshot, _phase(FlightPhase.TAXI_OUT))
        alert_ids = {a.alert_id for a in alerts}
        self.assertNotIn("excessive_taxi_speed", alert_ids)

    def test_taxi_speed_suppressed_until_rollout_clears(self) -> None:
        monitor = HazardMonitor(urgent_cooldown_sec=8.0)

        # High-speed rollout right after touchdown: should be ignored.
        rollout_fast = FlightSnapshot(
            timestamp_sec=40.0,
            on_ground=True,
            groundspeed_m_s=24.0,  # ~46 kt
            indicated_airspeed_kt=48.0,
            throttle_ratio=0.1,
        )
        alerts = monitor.evaluate(rollout_fast, _phase(FlightPhase.TAXI_IN))
        self.assertEqual(alerts, [])

        # Slow enough to arm taxi monitoring.
        rollout_slow = FlightSnapshot(
            timestamp_sec=41.0,
            on_ground=True,
            groundspeed_m_s=6.0,  # ~12 kt
            indicated_airspeed_kt=14.0,
            throttle_ratio=0.05,
        )
        alerts = monitor.evaluate(rollout_slow, _phase(FlightPhase.TAXI_IN))
        self.assertEqual(alerts, [])

        # After arming, fast taxi should alert.
        taxi_fast = FlightSnapshot(
            timestamp_sec=42.0,
            on_ground=True,
            groundspeed_m_s=18.0,  # ~35 kt
            indicated_airspeed_kt=37.0,
            throttle_ratio=0.35,
        )
        alerts = monitor.evaluate(taxi_fast, _phase(FlightPhase.TAXI_IN))
        alert_ids = {a.alert_id for a in alerts}
        self.assertIn("excessive_taxi_speed", alert_ids)

    def test_speech_variants_rotate(self) -> None:
        profile = HazardProfile(
            enabled_rules=["excessive_taxi_speed"],
            thresholds={
                "max_taxi_speed_kt": 20.0,
                "max_taxi_ias_kt": 25.0,
                "taxi_takeoff_roll_throttle_ratio": 0.95,
                "taxi_takeoff_roll_ias_kt": 999.0,
                "taxi_takeoff_roll_gs_kt": 999.0,
            },
            speech_variants={
                "excessive_taxi_speed": [
                    "Taxi pace high. Slow down now.",
                    "Reduce taxi speed and regain control.",
                ]
            },
        )
        monitor = HazardMonitor(urgent_cooldown_sec=8.0, hazard_profile=profile)
        snap = FlightSnapshot(
            timestamp_sec=50.0,
            on_ground=True,
            groundspeed_m_s=16.0,  # ~31 kt
            indicated_airspeed_kt=30.0,
            throttle_ratio=0.2,
        )
        phase = _phase(FlightPhase.TAXI_OUT)
        first = monitor.evaluate(snap, phase)
        second = monitor.evaluate(
            FlightSnapshot(
                timestamp_sec=51.0,
                on_ground=True,
                groundspeed_m_s=16.0,
                indicated_airspeed_kt=30.0,
                throttle_ratio=0.2,
            ),
            phase,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0].speak_text, second[0].speak_text)


if __name__ == "__main__":
    unittest.main()
