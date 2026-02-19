from __future__ import annotations

from cfi_ai.types import FlightPhase, FlightSnapshot, HazardAlert, HazardProfile, PhaseState


class HazardMonitor:
    def __init__(
        self,
        urgent_cooldown_sec: float,
        hazard_profile: HazardProfile | None = None,
    ) -> None:
        self._urgent_cooldown_sec = urgent_cooldown_sec
        self._field_elevation_m: float | None = None
        self._hazard_profile = hazard_profile or HazardProfile()

    def set_hazard_profile(self, hazard_profile: HazardProfile | None) -> None:
        if hazard_profile is None:
            return
        self._hazard_profile = hazard_profile

    def evaluate(self, snapshot: FlightSnapshot, phase_state: PhaseState) -> list[HazardAlert]:
        alerts: list[HazardAlert] = []
        now = snapshot.timestamp_sec

        gs_kt = (snapshot.groundspeed_m_s or 0.0) * 1.94384
        if snapshot.on_ground and snapshot.elevation_m is not None and gs_kt < 25:
            self._field_elevation_m = snapshot.elevation_m

        ias = snapshot.indicated_airspeed_kt or 0.0
        vs = snapshot.vertical_speed_fpm or 0.0
        bank_abs = abs(snapshot.roll_deg or 0.0)
        agl_ft = snapshot.agl_ft(self._field_elevation_m)

        if snapshot.on_ground:
            if self._enabled("excessive_taxi_speed"):
                max_taxi_speed = self._threshold("max_taxi_speed_kt", 30.0)
                max_taxi_ias = self._threshold("max_taxi_ias_kt", 35.0)
                if gs_kt > max_taxi_speed or ias > max_taxi_ias:
                    alerts.append(
                        HazardAlert(
                            alert_id="excessive_taxi_speed",
                            severity="warning",
                            message="Taxi speed exceeds configured limit.",
                            speak_text="Slow down taxi speed now and regain full directional control.",
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
                    speak_text="Airspeed critical. Lower the nose and add power now.",
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
                    speak_text="Sink rate. Reduce descent and stabilize immediately.",
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
                    speak_text="Bank angle. Roll wings level and stabilize the approach.",
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
                    speak_text="Pull up. Arrest descent now.",
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
                    speak_text="Unstable approach. Correct now or execute a go-around.",
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
