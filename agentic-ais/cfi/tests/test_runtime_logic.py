from __future__ import annotations

from cfi_ai.runtime import (
    _extract_bytes_text,
    _extract_engine_count,
    _extract_engine_running,
    _extract_text_value,
    _resolve_plane_profile,
    _extract_stall_warning,
    should_emit_alert,
)


def test_should_emit_alert_respects_cooldown() -> None:
    assert should_emit_alert(last_emitted_epoch=None, now_epoch=10.0, cooldown_sec=3.0)
    assert not should_emit_alert(last_emitted_epoch=10.0, now_epoch=11.0, cooldown_sec=3.0)
    assert should_emit_alert(last_emitted_epoch=10.0, now_epoch=13.0, cooldown_sec=3.0)


def test_extract_engine_running_array_and_scalar() -> None:
    assert _extract_engine_running({"value": [1, 0, 1]}) == (1, 0, 1)
    assert _extract_engine_running({"value": 0}) == (0,)
    assert _extract_engine_running({"value": "bad"}) is None


def test_extract_engine_running_trimmed_to_engine_count() -> None:
    assert _extract_engine_running({"value": [1, 0, 0, 0]}, engine_count=1) == (1,)
    assert _extract_engine_running({"value": [1, 1, 0, 0]}, engine_count=2) == (1, 1)


def test_extract_engine_count_scalar() -> None:
    assert _extract_engine_count({"value": 1}) == 1
    assert _extract_engine_count({"value": 0}) is None
    assert _extract_engine_count({"value": "x"}) is None


def test_extract_text_value_from_hex_bytes() -> None:
    assert _extract_bytes_text("433137320000") == "C172"
    assert _extract_text_value({"kind": "bytes", "value": "433137320000"}) == "C172"


def test_resolve_plane_profile_by_icao_and_name() -> None:
    db = {
        "defaults": [],
        "profiles": {
            "C172": {"notes": ["Use C172 profile"]},
            "PA28": {"icao": ["P28A"], "name_contains": ["arrow"]},
        },
    }
    key, profile = _resolve_plane_profile(db, aircraft_icao="C172", aircraft_name="Cessna 172SP")
    assert key == "C172"
    assert profile and profile["notes"][0] == "Use C172 profile"

    key2, profile2 = _resolve_plane_profile(db, aircraft_icao="P28A", aircraft_name="Piper Arrow")
    assert key2 == "PA28"
    assert profile2 and profile2["icao"][0] == "P28A"


def test_extract_stall_warning_uses_ratio_threshold() -> None:
    assert _extract_stall_warning({"value": 0.15}, "sim/cockpit2/annunciators/stall_warning_ratio") is True
    assert _extract_stall_warning({"value": 0.05}, "sim/cockpit2/annunciators/stall_warning_ratio") is False
    assert _extract_stall_warning({"value": 1}, "sim/cockpit2/annunciators/stall_warning") is True
