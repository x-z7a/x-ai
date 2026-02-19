from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.hazard_monitor import HazardMonitor
from cfi_ai.types import FlightPhase, FlightSnapshot, HazardProfile, PhaseState


def _phase() -> PhaseState:
    return PhaseState(
        phase=FlightPhase.APPROACH,
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
        alerts = monitor.evaluate(snapshot, _phase())
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


if __name__ == "__main__":
    unittest.main()
