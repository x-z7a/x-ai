from __future__ import annotations

from cfi_ai.types import FlightSnapshot, HazardAlert, PhaseState


class HazardMonitor:
    def __init__(self, urgent_cooldown_sec: float) -> None:
        self._urgent_cooldown_sec = urgent_cooldown_sec
        self._field_elevation_m: float | None = None

    def evaluate(self, snapshot: FlightSnapshot, _phase: PhaseState) -> list[HazardAlert]:
        alerts: list[HazardAlert] = []
        now = snapshot.timestamp_sec

        gs_kt = (snapshot.groundspeed_m_s or 0.0) * 1.94384
        if snapshot.on_ground and snapshot.elevation_m is not None and gs_kt < 25:
            self._field_elevation_m = snapshot.elevation_m

        if snapshot.on_ground:
            return alerts

        ias = snapshot.indicated_airspeed_kt or 0.0
        vs = snapshot.vertical_speed_fpm or 0.0
        bank_abs = abs(snapshot.roll_deg or 0.0)
        agl_ft = snapshot.agl_ft(self._field_elevation_m)

        if snapshot.stall_warning or (ias < 50 and (agl_ft is None or agl_ft > 100)):
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

        if agl_ft is not None and agl_ft < 1000 and vs < -1500:
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

        if agl_ft is not None and agl_ft < 1000 and bank_abs > 45:
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

        if agl_ft is not None and agl_ft < 300 and vs < -1000:
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

        return alerts
