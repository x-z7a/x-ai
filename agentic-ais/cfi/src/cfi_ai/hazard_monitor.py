from __future__ import annotations

import random
import re
from dataclasses import replace

from cfi_ai.types import FlightPhase, FlightSnapshot, HazardAlert, HazardProfile, PhaseState


class HazardMonitor:
    def __init__(
        self,
        urgent_cooldown_sec: float,
        hazard_profile: HazardProfile | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._urgent_cooldown_sec = urgent_cooldown_sec
        self._field_elevation_m: float | None = None
        self._hazard_profile = hazard_profile or HazardProfile()
        self._rng = rng or random.Random()
        self._last_variant_idx: dict[str, int] = {}
        self._taxi_in_rollout_cleared = False

    def set_hazard_profile(self, hazard_profile: HazardProfile | None) -> None:
        if hazard_profile is None:
            return
        self._hazard_profile = hazard_profile
        self._last_variant_idx.clear()
        self._taxi_in_rollout_cleared = False

    def update_speech_variants(self, speech_variants: dict[str, list[str]] | None) -> None:
        if not speech_variants:
            return
        merged = {
            rule: list(lines)
            for rule, lines in self._hazard_profile.speech_variants.items()
        }
        for rule, lines in speech_variants.items():
            cleaned: list[str] = []
            for line in lines:
                phrase = _normalize_phrase(line)
                if phrase:
                    cleaned.append(phrase)
            if cleaned:
                merged[rule] = cleaned[:6]
        self._hazard_profile = replace(self._hazard_profile, speech_variants=merged)
        self._last_variant_idx.clear()

    def evaluate(self, snapshot: FlightSnapshot, phase_state: PhaseState) -> list[HazardAlert]:
        alerts: list[HazardAlert] = []
        now = snapshot.timestamp_sec

        gs_kt = (snapshot.groundspeed_m_s or 0.0) * 1.94384
        if not snapshot.on_ground:
            self._taxi_in_rollout_cleared = False
        if snapshot.on_ground and snapshot.elevation_m is not None and gs_kt < 25:
            self._field_elevation_m = snapshot.elevation_m

        ias = snapshot.indicated_airspeed_kt or 0.0
        vs = snapshot.vertical_speed_fpm or 0.0
        bank_abs = abs(snapshot.roll_deg or 0.0)
        agl_ft = snapshot.agl_ft(self._field_elevation_m)

        if snapshot.on_ground:
            if self._enabled("excessive_taxi_speed"):
                if not self._should_monitor_taxi_speed(snapshot, phase_state, gs_kt, ias):
                    return alerts
                max_taxi_speed = self._threshold("max_taxi_speed_kt", 30.0)
                max_taxi_ias = self._threshold("max_taxi_ias_kt", 35.0)
                if gs_kt > max_taxi_speed or ias > max_taxi_ias:
                    alerts.append(
                        HazardAlert(
                            alert_id="excessive_taxi_speed",
                            severity="warning",
                            message="Taxi speed exceeds configured limit.",
                            speak_text=self._speak_for(
                                "excessive_taxi_speed",
                                "Slow down taxi speed now and regain full directional control.",
                            ),
                            cooldown_sec=self._urgent_cooldown_sec,
                            triggered_at_epoch=now,
                        )
                    )
            return alerts

        if (
            self._enabled("stall_or_low_speed")
            and (
                snapshot.stall_warning
                or (
                    ias < self._threshold("low_airspeed_kt", 50.0)
                    and (
                        agl_ft is None
                        or agl_ft > self._threshold("low_airspeed_min_agl_ft", 100.0)
                    )
                )
            )
        ):
            alerts.append(
                HazardAlert(
                    alert_id="stall_or_low_speed",
                    severity="critical",
                    message="Low energy / stall risk detected.",
                    speak_text=self._speak_for(
                        "stall_or_low_speed",
                        "Airspeed critical. Lower the nose and add power now.",
                    ),
                    cooldown_sec=self._urgent_cooldown_sec,
                    triggered_at_epoch=now,
                )
            )

        if (
            self._enabled("excessive_sink_low_alt")
            and agl_ft is not None
            and agl_ft < self._threshold("excessive_sink_max_agl_ft", 1000.0)
            and vs < self._threshold("excessive_sink_fpm", -1500.0)
        ):
            alerts.append(
                HazardAlert(
                    alert_id="excessive_sink_low_alt",
                    severity="critical",
                    message="Excessive sink rate at low altitude.",
                    speak_text=self._speak_for(
                        "excessive_sink_low_alt",
                        "Sink rate. Reduce descent and stabilize immediately.",
                    ),
                    cooldown_sec=self._urgent_cooldown_sec,
                    triggered_at_epoch=now,
                )
            )

        if (
            self._enabled("high_bank_low_alt")
            and agl_ft is not None
            and agl_ft < self._threshold("high_bank_max_agl_ft", 1000.0)
            and bank_abs > self._threshold("high_bank_deg", 45.0)
        ):
            alerts.append(
                HazardAlert(
                    alert_id="high_bank_low_alt",
                    severity="critical",
                    message="High bank angle at low altitude.",
                    speak_text=self._speak_for(
                        "high_bank_low_alt",
                        "Bank angle. Roll wings level and stabilize the approach.",
                    ),
                    cooldown_sec=self._urgent_cooldown_sec,
                    triggered_at_epoch=now,
                )
            )

        if (
            self._enabled("pull_up_now")
            and agl_ft is not None
            and agl_ft < self._threshold("pull_up_max_agl_ft", 300.0)
            and vs < self._threshold("pull_up_fpm", -1000.0)
        ):
            alerts.append(
                HazardAlert(
                    alert_id="pull_up_now",
                    severity="critical",
                    message="Impact risk: very high descent close to ground.",
                    speak_text=self._speak_for(
                        "pull_up_now",
                        "Pull up. Arrest descent now.",
                    ),
                    cooldown_sec=self._urgent_cooldown_sec,
                    triggered_at_epoch=now,
                )
            )

        if (
            self._enabled("unstable_approach_fast_or_sink")
            and phase_state.phase in {FlightPhase.APPROACH, FlightPhase.LANDING}
            and agl_ft is not None
            and agl_ft < self._threshold("unstable_approach_max_agl_ft", 1000.0)
            and (
                ias > self._threshold("unstable_approach_max_ias_kt", 95.0)
                or vs < self._threshold("unstable_approach_min_sink_fpm", -1000.0)
            )
        ):
            alerts.append(
                HazardAlert(
                    alert_id="unstable_approach_fast_or_sink",
                    severity="critical",
                    message="Approach stability limits exceeded.",
                    speak_text=self._speak_for(
                        "unstable_approach_fast_or_sink",
                        "Unstable approach. Correct now or execute a go-around.",
                    ),
                    cooldown_sec=self._urgent_cooldown_sec,
                    triggered_at_epoch=now,
                )
            )

        return alerts

    def _enabled(self, rule_name: str) -> bool:
        return rule_name in set(self._hazard_profile.enabled_rules)

    def _threshold(self, name: str, fallback: float) -> float:
        value = self._hazard_profile.thresholds.get(name, fallback)
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _should_monitor_taxi_speed(
        self,
        snapshot: FlightSnapshot,
        phase_state: PhaseState,
        gs_kt: float,
        ias: float,
    ) -> bool:
        phase = phase_state.phase
        if phase not in {FlightPhase.TAXI_OUT, FlightPhase.TAXI_IN}:
            return False

        if phase == FlightPhase.TAXI_OUT and self._is_takeoff_ground_roll(snapshot, gs_kt, ias):
            return False

        if phase == FlightPhase.TAXI_IN:
            if not self._taxi_in_rollout_cleared:
                clear_gs = self._threshold("taxi_in_rollout_clear_gs_kt", 25.0)
                clear_ias = self._threshold("taxi_in_rollout_clear_ias_kt", 30.0)
                if gs_kt <= clear_gs and ias <= clear_ias:
                    self._taxi_in_rollout_cleared = True
                else:
                    return False

        return True

    def _is_takeoff_ground_roll(self, snapshot: FlightSnapshot, gs_kt: float, ias: float) -> bool:
        throttle = snapshot.throttle_ratio or 0.0
        thr_takeoff = self._threshold("taxi_takeoff_roll_throttle_ratio", 0.65)
        ias_takeoff = self._threshold("taxi_takeoff_roll_ias_kt", 35.0)
        gs_takeoff = self._threshold("taxi_takeoff_roll_gs_kt", 30.0)
        return throttle >= thr_takeoff and (ias >= ias_takeoff or gs_kt >= gs_takeoff)

    def _speak_for(self, alert_id: str, fallback: str) -> str:
        variants_raw = self._hazard_profile.speech_variants.get(alert_id, [])
        variants: list[str] = []
        for item in variants_raw:
            text = _normalize_phrase(item)
            if text:
                variants.append(text)
        if not variants:
            return _normalize_phrase(fallback) or fallback
        if len(variants) == 1:
            self._last_variant_idx[alert_id] = 0
            return variants[0]

        idx = self._rng.randrange(len(variants))
        prev = self._last_variant_idx.get(alert_id)
        if prev is not None and idx == prev:
            idx = (idx + 1) % len(variants)
        self._last_variant_idx[alert_id] = idx
        return variants[idx]


def _normalize_phrase(text: str) -> str:
    value = str(text).strip()
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value)
    if len(value) > 180:
        value = _truncate_text(value, max_chars=180)
    if value and value[-1] not in ".!?":
        value = f"{value}."
    return value


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[: max_chars + 1]
    boundary = head.rfind(" ")
    if boundary >= int(max_chars * 0.6):
        head = head[:boundary]
    else:
        head = text[:max_chars]
    return head.rstrip(" ,;:-")
