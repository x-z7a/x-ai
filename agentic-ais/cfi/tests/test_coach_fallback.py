from __future__ import annotations

from cfi_ai.coach import compose_fallback_alert, compose_fallback_debrief
from cfi_ai.types import FlightPhase, RuleFinding, Severity


def test_compose_fallback_alert_prefixes_severity() -> None:
    finding = RuleFinding(
        rule_id="unstable_approach",
        severity=Severity.P1,
        phase=FlightPhase.APPROACH,
        message="Unstable approach detected below 500 feet AGL.",
        evidence={"agl_ft": 400},
        timestamp_epoch=100.0,
        cooldown_sec=8.0,
    )

    spoken = compose_fallback_alert(finding)
    assert spoken.startswith("Correction needed:")


def test_compose_fallback_debrief_populates_segments() -> None:
    payload = {
        "findings": [
            {
                "rule_id": "hard_landing",
                "severity": "P0",
                "message": "Hard landing detected.",
            },
            {
                "rule_id": "taxi_speed_high",
                "severity": "P1",
                "message": "Taxi speed too high.",
            },
        ]
    }

    debrief = compose_fallback_debrief(payload)

    assert debrief.total_findings == 2
    assert len(debrief.spoken_segments) == 3
    assert debrief.findings_by_severity["P0"] == 1
    assert debrief.findings_by_severity["P1"] == 1
