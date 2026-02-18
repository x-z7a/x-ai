from __future__ import annotations

from cfi_ai.rules import P1_COOLDOWN_SEC, RuleEngine, normalize_ai_findings
from cfi_ai.types import FlightPhase, MonitorSample, Severity


def make_sample(
    *,
    t: float,
    on_ground: bool,
    altitude_ft_msl: float = 500.0,
    agl_ft: float = 0.0,
    groundspeed_kt: float = 0.0,
    indicated_airspeed_kt: float = 0.0,
    vertical_speed_fpm: float = 0.0,
    heading_deg: float = 90.0,
    bank_deg: float = 0.0,
    pitch_deg: float = 0.0,
    flaps_ratio: float = 0.0,
    parking_brake_ratio: float = 0.0,
    stall_warning_active: bool | None = None,
    engine_running: tuple[int, ...] | None = (1,),
) -> MonitorSample:
    return MonitorSample(
        timestamp_epoch=t,
        on_ground=on_ground,
        latitude=47.0,
        longitude=-122.0,
        altitude_ft_msl=altitude_ft_msl,
        agl_ft=agl_ft,
        groundspeed_kt=groundspeed_kt,
        indicated_airspeed_kt=indicated_airspeed_kt,
        vertical_speed_fpm=vertical_speed_fpm,
        heading_deg=heading_deg,
        bank_deg=bank_deg,
        pitch_deg=pitch_deg,
        flaps_ratio=flaps_ratio,
        parking_brake_ratio=parking_brake_ratio,
        stall_warning_active=stall_warning_active,
        engine_running=engine_running,
        raw_state={},
    )


def test_phase_progression_preflight_to_climb() -> None:
    engine = RuleEngine(engine_shutdown_hold_sec=15.0)

    engine.update(make_sample(t=0.0, on_ground=True, groundspeed_kt=0.0, indicated_airspeed_kt=0.0))
    assert engine.phase == FlightPhase.PRE_FLIGHT

    engine.update(make_sample(t=1.0, on_ground=True, groundspeed_kt=6.0, indicated_airspeed_kt=8.0))
    assert engine.phase == FlightPhase.TAXI_OUT

    engine.update(make_sample(t=2.0, on_ground=True, groundspeed_kt=35.0, indicated_airspeed_kt=45.0))
    assert engine.phase == FlightPhase.TAKEOFF_ROLL

    engine.update(
        make_sample(
            t=3.0,
            on_ground=False,
            groundspeed_kt=80.0,
            indicated_airspeed_kt=75.0,
            vertical_speed_fpm=900.0,
            agl_ft=300.0,
            altitude_ft_msl=800.0,
        )
    )
    assert engine.phase == FlightPhase.CLIMB


def test_p0_low_agl_sink_rate_trigger() -> None:
    engine = RuleEngine()

    engine.update(
        make_sample(
            t=0.0,
            on_ground=False,
            groundspeed_kt=85.0,
            indicated_airspeed_kt=80.0,
            vertical_speed_fpm=-1305.0,
            agl_ft=420.0,
            altitude_ft_msl=900.0,
        )
    )
    findings = engine.update(
        make_sample(
            t=1.0,
            on_ground=False,
            groundspeed_kt=90.0,
            indicated_airspeed_kt=78.0,
            vertical_speed_fpm=-1250.0,
            agl_ft=350.0,
            altitude_ft_msl=840.0,
        )
    )

    assert any(f.rule_id == "low_agl_sink_rate" and f.severity == Severity.P0 for f in findings)


def test_climb_out_airspeed_out_of_band_after_8_seconds() -> None:
    engine = RuleEngine()

    engine.update(
        make_sample(
            t=0.0,
            on_ground=False,
            indicated_airspeed_kt=70.0,
            vertical_speed_fpm=700.0,
            agl_ft=150.0,
            altitude_ft_msl=650.0,
        )
    )

    engine.update(
        make_sample(
            t=4.0,
            on_ground=False,
            indicated_airspeed_kt=50.0,
            vertical_speed_fpm=800.0,
            agl_ft=900.0,
            altitude_ft_msl=1400.0,
        )
    )

    findings = engine.update(
        make_sample(
            t=12.5,
            on_ground=False,
            indicated_airspeed_kt=52.0,
            vertical_speed_fpm=850.0,
            agl_ft=1800.0,
            altitude_ft_msl=2300.0,
        )
    )

    assert any(f.rule_id == "climb_out_airspeed_out_of_band" for f in findings)


def test_shutdown_detection_resets_if_engine_restarts() -> None:
    engine = RuleEngine(engine_shutdown_hold_sec=15.0)

    engine.update(
        make_sample(
            t=0.0,
            on_ground=False,
            groundspeed_kt=100.0,
            indicated_airspeed_kt=95.0,
            vertical_speed_fpm=500.0,
            agl_ft=1000.0,
            altitude_ft_msl=1500.0,
            engine_running=(1,),
        )
    )

    engine.update(
        make_sample(
            t=10.0,
            on_ground=True,
            groundspeed_kt=0.5,
            engine_running=(0,),
            agl_ft=0.0,
            altitude_ft_msl=500.0,
        )
    )
    assert not engine.shutdown_confirmed

    engine.update(
        make_sample(
            t=20.0,
            on_ground=True,
            groundspeed_kt=0.5,
            engine_running=(1,),
            agl_ft=0.0,
            altitude_ft_msl=500.0,
        )
    )
    assert not engine.shutdown_confirmed

    engine.update(
        make_sample(
            t=30.0,
            on_ground=True,
            groundspeed_kt=0.5,
            engine_running=(0,),
            agl_ft=0.0,
            altitude_ft_msl=500.0,
        )
    )
    engine.update(
        make_sample(
            t=44.0,
            on_ground=True,
            groundspeed_kt=0.5,
            engine_running=(0,),
            agl_ft=0.0,
            altitude_ft_msl=500.0,
        )
    )
    assert not engine.shutdown_confirmed

    engine.update(
        make_sample(
            t=46.0,
            on_ground=True,
            groundspeed_kt=0.5,
            engine_running=(0,),
            agl_ft=0.0,
            altitude_ft_msl=500.0,
        )
    )
    assert engine.shutdown_confirmed
    assert engine.phase == FlightPhase.SHUTDOWN


def test_steep_turn_quality_trigger() -> None:
    engine = RuleEngine()

    engine.update(
        make_sample(
            t=0.0,
            on_ground=False,
            bank_deg=40.0,
            agl_ft=2000.0,
            altitude_ft_msl=3000.0,
            indicated_airspeed_kt=95.0,
            vertical_speed_fpm=50.0,
        )
    )
    findings = engine.update(
        make_sample(
            t=13.0,
            on_ground=False,
            bank_deg=60.0,
            agl_ft=2050.0,
            altitude_ft_msl=3170.0,
            indicated_airspeed_kt=98.0,
            vertical_speed_fpm=100.0,
        )
    )

    assert any(f.rule_id == "steep_turn_quality" and f.severity == Severity.P2 for f in findings)


def test_normalize_ai_findings_generates_prefixed_findings() -> None:
    raw = [
        {
            "rule_id": "Energy Management",
            "severity": "p1",
            "message": "Approach energy trend is unstable.",
            "evidence": {"ias_trend": "high"},
        },
        {
            "rule_id": "Energy Management",
            "severity": "P1",
            "message": "Approach energy trend is unstable.",
            "evidence": {"ias_trend": "high"},
        },
        {
            "rule_id": "bad_severity",
            "severity": "warn",
            "message": "Ignored",
        },
    ]

    findings = normalize_ai_findings(
        raw,
        phase=FlightPhase.APPROACH,
        timestamp_epoch=50.0,
    )

    assert len(findings) == 1
    assert findings[0].rule_id == "ai_energy_management"
    assert findings[0].severity == Severity.P1
    assert findings[0].cooldown_sec == P1_COOLDOWN_SEC
