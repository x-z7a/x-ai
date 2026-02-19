from __future__ import annotations

from statistics import fmean

from cfi_ai.types import FlightPhase, FlightSnapshot, ReviewWindow


class ReviewWindowBuilder:
    def __init__(self) -> None:
        self._field_elevation_m: float | None = None

    def build(self, snapshots: list[FlightSnapshot], phase: FlightPhase) -> ReviewWindow:
        if not snapshots:
            raise ValueError("Cannot build review window from an empty snapshot list.")

        for s in snapshots:
            gs_kt = (s.groundspeed_m_s or 0.0) * 1.94384
            if s.on_ground and s.elevation_m is not None and gs_kt < 25:
                self._field_elevation_m = s.elevation_m

        ias_values = [s.indicated_airspeed_kt for s in snapshots if s.indicated_airspeed_kt is not None]
        vs_values = [s.vertical_speed_fpm for s in snapshots if s.vertical_speed_fpm is not None]
        roll_values = [abs(s.roll_deg) for s in snapshots if s.roll_deg is not None]
        agl_values = [
            s.agl_ft(self._field_elevation_m)
            for s in snapshots
            if s.agl_ft(self._field_elevation_m) is not None
        ]

        metrics: dict[str, float] = {
            "ias_min_kt": min(ias_values) if ias_values else 0.0,
            "ias_max_kt": max(ias_values) if ias_values else 0.0,
            "vs_mean_fpm": fmean(vs_values) if vs_values else 0.0,
            "vs_min_fpm": min(vs_values) if vs_values else 0.0,
            "vs_max_fpm": max(vs_values) if vs_values else 0.0,
            "roll_abs_max_deg": max(roll_values) if roll_values else 0.0,
            "agl_min_ft": min(agl_values) if agl_values else 0.0,
            "agl_max_ft": max(agl_values) if agl_values else 0.0,
        }

        hints: list[str] = []
        if phase in {FlightPhase.APPROACH, FlightPhase.LANDING} and metrics["ias_max_kt"] > 95:
            hints.append("Approach speed appears high for primary GA profile.")
        if phase in {FlightPhase.TAKEOFF, FlightPhase.INITIAL_CLIMB} and metrics["ias_min_kt"] < 55:
            hints.append("Low airspeed observed during takeoff/climb segment.")
        if metrics["roll_abs_max_deg"] > 35:
            hints.append("Steep bank observed; coach smoother bank discipline.")
        if metrics["agl_min_ft"] < 1000 and metrics["vs_min_fpm"] < -1000:
            hints.append("High sink near ground seen in this window.")

        return ReviewWindow(
            start_epoch=snapshots[0].timestamp_sec,
            end_epoch=snapshots[-1].timestamp_sec,
            phase=phase,
            sample_count=len(snapshots),
            metrics=metrics,
            event_hints=hints,
        )
