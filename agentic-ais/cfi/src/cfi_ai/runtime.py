from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from cfi_ai.cfi_tools import CfiTools
from cfi_ai.coach import CfiCoach, compose_fallback_alert
from cfi_ai.config import CfiConfig
from cfi_ai.rules import RuleEngine, normalize_ai_findings
from cfi_ai.types import FlightDebrief, FlightEventLogEntry, FlightPhase, MonitorSample, RuleFinding
from cfi_ai.xplane_mcp import XPlaneMCPClient


REQUIRED_DATAREFS: dict[str, tuple[str, ...]] = {
    "engine_running": (
        "sim/flightmodel/engine/ENGN_running",
    ),
    "bank_deg": (
        "sim/flightmodel/position/phi",
    ),
    "pitch_deg": (
        "sim/flightmodel/position/theta",
    ),
    "flaps_ratio": (
        "sim/cockpit2/controls/flap_ratio",
    ),
    "parking_brake_ratio": (
        "sim/cockpit2/controls/parking_brake_ratio",
    ),
    "stall_warning_active": (
        "sim/cockpit2/annunciators/stall_warning",
        "sim/cockpit2/annunciators/stall_warning_ratio",
    ),
}

OPTIONAL_DATAREFS: dict[str, tuple[str, ...]] = {
    # Aircraft-specific engine count; used to trim ENGN_running array.
    "engine_count": (
        "sim/aircraft/engine/acf_num_engines",
        "sim/aircraft/prop/acf_num_engines",
    ),
    "aircraft_icao": (
        "sim/aircraft/view/acf_ICAO",
    ),
    "aircraft_name": (
        "sim/aircraft/view/acf_ui_name",
        "sim/aircraft/view/acf_descrip",
    ),
    "aircraft_tailnum": (
        "sim/aircraft/view/acf_tailnum",
    ),
}


def should_emit_alert(
    *,
    last_emitted_epoch: float | None,
    now_epoch: float,
    cooldown_sec: float,
) -> bool:
    if last_emitted_epoch is None:
        return True
    return (now_epoch - last_emitted_epoch) >= cooldown_sec


class CfiRuntime:
    def __init__(
        self,
        config: CfiConfig,
        *,
        mcp_client: XPlaneMCPClient | None = None,
        coach: CfiCoach | None = None,
    ) -> None:
        self._config = config
        self._mcp = mcp_client or XPlaneMCPClient(config.xplane_mcp_sse_url)
        self._coach = coach or CfiCoach(config)

        self._tools: CfiTools | None = None
        self._rules = RuleEngine(engine_shutdown_hold_sec=config.engine_shutdown_hold_sec)

        self._monitor_task: asyncio.Task[None] | None = None
        self._alert_task: asyncio.Task[None] | None = None
        self._debrief_task: asyncio.Task[None] | None = None

        self._stop_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._finished_event = asyncio.Event()

        self._alerts_queue: asyncio.Queue[RuleFinding] = asyncio.Queue()
        self._last_alert_epoch_by_rule: dict[str, float] = {}

        self._event_log: list[FlightEventLogEntry] = []
        self._findings: list[RuleFinding] = []

        self._available_datarefs: dict[str, str] = {}
        self._missing_datarefs: dict[str, tuple[str, ...]] = {}

        self._last_sample: MonitorSample | None = None
        self._recent_samples: list[MonitorSample] = []
        self._agl_reference_ft: float | None = None
        self._last_phase: FlightPhase | None = None
        self._last_ai_eval_epoch = 0.0

        self._debrief: FlightDebrief | None = None
        self._started = False
        self._plane_memory_db = _load_plane_memory_db(config.plane_memory_path)
        self._plane_memory_signature: str | None = None

    @property
    def debrief(self) -> FlightDebrief | None:
        return self._debrief

    async def start(self) -> None:
        if self._started:
            return

        await self._mcp.connect()
        self._tools = CfiTools(self._mcp, speak_enabled=self._config.speak_enabled)
        await self._coach.start()

        await self._probe_datarefs()
        await self._maybe_announce_limited_monitoring()

        self._stop_event.clear()
        self._shutdown_event.clear()
        self._finished_event.clear()

        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._alert_task = asyncio.create_task(self._alert_dispatcher_loop())
        self._debrief_task = asyncio.create_task(self._debrief_loop())

        self._started = True
        self._record_event(
            category="runtime",
            phase=FlightPhase.PRE_FLIGHT,
            summary="CFI monitor started.",
            details={
                "poll_sec": self._config.monitor_poll_sec,
                "speak_enabled": self._config.speak_enabled,
                "ai_eval_interval_sec": self._config.ai_rule_eval_interval_sec,
                "plane_memory_path": self._config.plane_memory_path,
            },
        )

    async def run_until_shutdown(self) -> None:
        if not self._started:
            raise RuntimeError("CFI runtime has not been started.")

        timeout_sec = self._config.max_flight_hours * 3600.0
        try:
            await asyncio.wait_for(self._finished_event.wait(), timeout=timeout_sec)
        except TimeoutError:
            print("[CFI] Max flight runtime exceeded. Stopping monitor.")
            self._stop_event.set()
            self._finished_event.set()

    async def stop(self) -> None:
        self._stop_event.set()

        for task in (self._monitor_task, self._alert_task, self._debrief_task):
            if task is None:
                continue
            with suppress(Exception):
                await task

        self._monitor_task = None
        self._alert_task = None
        self._debrief_task = None

        await self._coach.stop()
        await self._mcp.close()
        self._tools = None
        self._started = False

    async def _probe_datarefs(self) -> None:
        if self._tools is None:
            return

        self._available_datarefs.clear()
        self._missing_datarefs.clear()

        for key, candidates in REQUIRED_DATAREFS.items():
            selected = await self._select_available_dataref(candidates)
            if selected is not None:
                self._available_datarefs[key] = selected
            else:
                self._missing_datarefs[key] = candidates

        for key, candidates in OPTIONAL_DATAREFS.items():
            selected = await self._select_available_dataref(candidates)
            if selected is not None:
                self._available_datarefs[key] = selected

    async def _select_available_dataref(self, candidates: tuple[str, ...]) -> str | None:
        if self._tools is None:
            return None

        for name in candidates:
            try:
                info = await self._tools.dataref_info(name)
            except Exception:
                continue
            if info:
                return name
        return None

    async def _maybe_announce_limited_monitoring(self) -> None:
        if not self._missing_datarefs:
            return

        missing_list = ", ".join(sorted(self._missing_datarefs.keys()))
        message = (
            "Limited monitoring: unavailable datarefs for "
            f"{missing_list}. Dependent checks are disabled."
        )

        print(f"[CFI] {message}")
        self._record_event(
            category="capability",
            phase=self._rules.phase,
            summary=message,
            details={"missing_datarefs": self._missing_datarefs},
        )

        if self._tools is not None:
            with suppress(Exception):
                await self._tools.speak(message)

    async def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                sample = await self._collect_sample()
                self._last_sample = sample
                self._recent_samples.append(sample)
                if len(self._recent_samples) > 10:
                    self._recent_samples.pop(0)
                await self._maybe_refresh_aircraft_memory(sample)

                findings = self._rules.update(sample)
                ai_findings = await self._maybe_collect_ai_findings(
                    sample=sample,
                    deterministic_findings=findings,
                )
                if ai_findings:
                    findings.extend(ai_findings)
                if self._rules.phase != self._last_phase:
                    self._last_phase = self._rules.phase
                    self._record_event(
                        category="phase",
                        phase=self._rules.phase,
                        summary=f"Phase changed to {self._rules.phase.value}.",
                    )
                    print(f"[CFI] Phase: {self._rules.phase.value}")

                for finding in findings:
                    self._findings.append(finding)
                    await self._alerts_queue.put(finding)

                if self._rules.shutdown_confirmed:
                    self._shutdown_event.set()
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"[CFI] Monitor loop error: {exc}")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._config.monitor_poll_sec)
            except TimeoutError:
                continue

    async def _alert_dispatcher_loop(self) -> None:
        while not self._stop_event.is_set() or not self._alerts_queue.empty():
            try:
                finding = await asyncio.wait_for(self._alerts_queue.get(), timeout=0.5)
            except TimeoutError:
                if self._shutdown_event.is_set() and self._alerts_queue.empty():
                    await asyncio.sleep(0)
                continue

            now = finding.timestamp_epoch
            last = self._last_alert_epoch_by_rule.get(finding.rule_id)
            if not should_emit_alert(
                last_emitted_epoch=last,
                now_epoch=now,
                cooldown_sec=finding.cooldown_sec,
            ):
                continue

            self._last_alert_epoch_by_rule[finding.rule_id] = now
            spoken = await self._safe_compose_alert(finding)

            self._record_event(
                category="alert",
                phase=finding.phase,
                summary=spoken,
                details={
                    "rule_id": finding.rule_id,
                    "severity": finding.severity.value,
                },
            )

            print(f"[CFI][{finding.severity.value}] {spoken}")
            if self._tools is not None:
                with suppress(Exception):
                    await self._tools.speak(spoken)

    async def _debrief_loop(self) -> None:
        while not self._stop_event.is_set() and not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=0.5)
            except TimeoutError:
                continue

        if self._stop_event.is_set() or not self._shutdown_event.is_set():
            return

        self._debrief = await self._build_debrief()
        print("[CFI] Shutdown confirmed. Delivering verbal debrief.")

        if self._tools is not None:
            for segment in self._debrief.spoken_segments:
                with suppress(Exception):
                    await self._tools.speak(segment)

        self._record_event(
            category="debrief",
            phase=FlightPhase.SHUTDOWN,
            summary="Debrief completed.",
            details={"segments": self._debrief.spoken_segments},
        )

        self._finished_event.set()
        self._stop_event.set()

    async def _safe_compose_alert(self, finding: RuleFinding) -> str:
        try:
            return await self._coach.compose_alert(finding, self._last_sample)
        except Exception:
            return compose_fallback_alert(finding)

    async def _build_debrief(self) -> FlightDebrief:
        payload = {
            "findings": [_finding_to_payload(item) for item in self._findings],
            "event_log": [
                {
                    "timestamp_epoch": entry.timestamp_epoch,
                    "category": entry.category,
                    "phase": entry.phase.value,
                    "summary": entry.summary,
                    "details": entry.details,
                }
                for entry in self._event_log
            ],
            "stall_recovery_seconds": self._rules.stall_recovery_seconds,
        }
        return await self._coach.compose_debrief(payload)

    async def _maybe_refresh_aircraft_memory(self, sample: MonitorSample) -> None:
        aircraft = sample.raw_state.get("aircraft")
        if not isinstance(aircraft, dict):
            return

        icao = _safe_str(aircraft.get("icao"))
        name = _safe_str(aircraft.get("name"))
        tailnum = _safe_str(aircraft.get("tailnum"))
        engine_count = _to_int(aircraft.get("engine_count"))
        if engine_count is not None and engine_count > 0:
            engine_type = "single_engine" if engine_count == 1 else "multi_engine"
        else:
            engine_type = "unknown"

        defaults = _string_list(self._plane_memory_db.get("defaults"))
        profile_key, profile_data = _resolve_plane_profile(
            self._plane_memory_db,
            aircraft_icao=icao,
            aircraft_name=name,
        )

        aircraft_context = {
            "icao": icao,
            "name": name,
            "tailnum": tailnum,
            "engine_count": engine_count,
            "engine_type": engine_type,
        }

        signature_payload = {
            "aircraft_context": aircraft_context,
            "defaults": defaults,
            "profile_key": profile_key,
            "profile_data": profile_data or {},
        }
        signature = json.dumps(signature_payload, ensure_ascii=True, sort_keys=True, default=str)
        if signature == self._plane_memory_signature:
            return

        await self._coach.set_aircraft_memory(
            aircraft_context=aircraft_context,
            defaults=defaults,
            profile_key=profile_key,
            profile_data=profile_data,
        )
        self._plane_memory_signature = signature

        summary = "Updated aircraft memory context."
        details = {
            "icao": icao,
            "name": name,
            "engine_count": engine_count,
            "profile_key": profile_key,
        }
        self._record_event(
            category="aircraft_memory",
            phase=self._rules.phase,
            summary=summary,
            details=details,
        )

    async def _maybe_collect_ai_findings(
        self,
        *,
        sample: MonitorSample,
        deterministic_findings: list[RuleFinding],
    ) -> list[RuleFinding]:
        if self._rules.phase == FlightPhase.SHUTDOWN:
            return []

        if (
            self._rules.phase == FlightPhase.PRE_FLIGHT
            and not self._rules.had_airborne
            and (sample.groundspeed_kt or 0.0) < 2.0
        ):
            return []

        now = sample.timestamp_epoch
        if (now - self._last_ai_eval_epoch) < self._config.ai_rule_eval_interval_sec:
            return []
        self._last_ai_eval_epoch = now

        payload = {
            "phase": self._rules.phase.value,
            "current_sample": _sample_summary(sample),
            "recent_samples": [_sample_summary(item) for item in self._recent_samples[-5:]],
            "deterministic_findings": [_finding_to_payload(item) for item in deterministic_findings[-5:]],
            "recent_event_summaries": [entry.summary for entry in self._event_log[-6:]],
            "instruction": (
                "Identify additional safety/checkride issues not already present in deterministic_findings. "
                "Do not repeat deterministic findings."
            ),
        }

        raw_ai_findings = await self._coach.evaluate_additional_findings(payload)
        ai_findings = normalize_ai_findings(
            raw_ai_findings,
            phase=self._rules.phase,
            timestamp_epoch=sample.timestamp_epoch,
        )

        if ai_findings:
            self._record_event(
                category="ai_evaluator",
                phase=self._rules.phase,
                summary=f"AI evaluator produced {len(ai_findings)} additional findings.",
                details={
                    "rule_ids": [item.rule_id for item in ai_findings],
                    "severities": [item.severity.value for item in ai_findings],
                },
            )

        return ai_findings

    async def _collect_sample(self) -> MonitorSample:
        if self._tools is None:
            raise RuntimeError("Tools are not initialized.")

        state = await self._tools.fetch_aircraft_state()

        extras: dict[str, Any] = {}
        for key, dataref in self._available_datarefs.items():
            try:
                extras[key] = await self._tools.dataref_get(dataref)
            except Exception:
                extras[key] = None

        on_ground = bool(state.get("on_ground", False))
        altitude_ft_msl = _to_float(state.get("elevation_m"))
        altitude_ft_msl = altitude_ft_msl * 3.28084 if altitude_ft_msl is not None else None

        groundspeed_m_s = _to_float(state.get("groundspeed_m_s"))
        groundspeed_kt = groundspeed_m_s * 1.94384 if groundspeed_m_s is not None else None

        if on_ground and altitude_ft_msl is not None and (groundspeed_kt or 0.0) < 40.0:
            self._agl_reference_ft = altitude_ft_msl

        agl_ft: float | None = None
        if altitude_ft_msl is not None and self._agl_reference_ft is not None:
            agl_ft = altitude_ft_msl - self._agl_reference_ft

        heading_deg = _to_float(state.get("magnetic_heading_deg"))
        if heading_deg is None:
            heading_deg = _to_float(state.get("heading_true_deg"))

        engine_count = _extract_engine_count(extras.get("engine_count"))
        engine_running = _extract_engine_running(
            extras.get("engine_running"),
            engine_count=engine_count,
        )
        stall_warning_active = _extract_stall_warning(
            extras.get("stall_warning_active"),
            self._available_datarefs.get("stall_warning_active", ""),
        )
        aircraft_icao = _extract_text_value(extras.get("aircraft_icao"))
        aircraft_name = _extract_text_value(extras.get("aircraft_name"))
        aircraft_tailnum = _extract_text_value(extras.get("aircraft_tailnum"))

        aircraft_context = {
            "icao": aircraft_icao,
            "name": aircraft_name,
            "tailnum": aircraft_tailnum,
            "engine_count": engine_count,
        }

        sample = MonitorSample(
            timestamp_epoch=time.time(),
            on_ground=on_ground,
            latitude=_to_float(state.get("latitude")),
            longitude=_to_float(state.get("longitude")),
            altitude_ft_msl=altitude_ft_msl,
            agl_ft=agl_ft,
            groundspeed_kt=groundspeed_kt,
            indicated_airspeed_kt=_to_float(state.get("indicated_airspeed_kt")),
            vertical_speed_fpm=_to_float(state.get("vertical_speed_fpm")),
            heading_deg=heading_deg,
            bank_deg=_extract_scalar(extras.get("bank_deg")),
            pitch_deg=_extract_scalar(extras.get("pitch_deg")),
            flaps_ratio=_extract_scalar(extras.get("flaps_ratio")),
            parking_brake_ratio=_extract_scalar(extras.get("parking_brake_ratio")),
            stall_warning_active=stall_warning_active,
            engine_running=engine_running,
            engine_count=engine_count,
            aircraft_icao=aircraft_icao,
            aircraft_name=aircraft_name,
            aircraft_tailnum=aircraft_tailnum,
            raw_state={"state": state, "extras": extras, "aircraft": aircraft_context},
        )

        return sample

    def _record_event(
        self,
        *,
        category: str,
        phase: FlightPhase,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._event_log.append(
            FlightEventLogEntry(
                timestamp_epoch=time.time(),
                category=category,
                phase=phase,
                summary=summary,
                details=details or {},
            )
        )



def _extract_scalar(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    return _to_float(payload.get("value"))


def _extract_engine_running(
    payload: Any,
    *,
    engine_count: int | None = None,
) -> tuple[int, ...] | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("value")
    if isinstance(value, list):
        out: list[int] = []
        for item in value:
            parsed = _to_int(item)
            if parsed is not None:
                out.append(parsed)
        if out:
            if engine_count is not None and engine_count > 0:
                return tuple(out[:engine_count])
            return tuple(out)
        return None

    parsed = _to_int(value)
    if parsed is None:
        return None
    return (parsed,)


def _extract_stall_warning(payload: Any, selected_dataref: str) -> bool | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("value")

    numeric = _to_float(value)
    if numeric is None:
        return None

    if "ratio" in selected_dataref:
        return numeric > 0.1
    return numeric > 0.0


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _extract_engine_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    count = _to_int(payload.get("value"))
    if count is None or count <= 0:
        return None
    return count


def _extract_text_value(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("value")
    kind = _safe_str(payload.get("kind")) or ""

    if isinstance(value, str):
        if kind == "bytes":
            return _extract_bytes_text(value)
        text = value.strip().strip("\x00")
        return text or None

    if isinstance(value, list):
        chars: list[str] = []
        for item in value:
            parsed = _to_int(item)
            if parsed is None:
                continue
            if parsed == 0:
                break
            if 32 <= parsed <= 126:
                chars.append(chr(parsed))
        text = "".join(chars).strip()
        return text or None

    return None


def _extract_bytes_text(hex_value: str) -> str | None:
    normalized = "".join(ch for ch in hex_value.strip() if ch in "0123456789abcdefABCDEF")
    if len(normalized) < 2:
        return None
    if len(normalized) % 2 != 0:
        normalized = normalized[:-1]
    if not normalized:
        return None
    try:
        decoded = bytes.fromhex(normalized)
    except ValueError:
        return None
    text = decoded.decode("utf-8", errors="ignore").split("\x00", 1)[0].strip()
    return text or None


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = _safe_str(item)
        if text:
            output.append(text)
    return output


def _load_plane_memory_db(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute():
        root = Path(__file__).resolve().parents[2]
        path = root / path

    if not path.exists():
        return {"defaults": [], "profiles": {}}

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"defaults": [], "profiles": {}}

    if not isinstance(loaded, dict):
        return {"defaults": [], "profiles": {}}

    defaults = _string_list(loaded.get("defaults"))
    profiles = loaded.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}

    normalized_profiles: dict[str, dict[str, Any]] = {}
    for key, value in profiles.items():
        key_text = _safe_str(key)
        if key_text is None or not isinstance(value, dict):
            continue
        normalized_profiles[key_text] = value

    return {
        "defaults": defaults,
        "profiles": normalized_profiles,
    }


def _resolve_plane_profile(
    db: dict[str, Any],
    *,
    aircraft_icao: str | None,
    aircraft_name: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    profiles = db.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        return None, None

    icao_upper = aircraft_icao.upper() if aircraft_icao else ""
    name_lower = aircraft_name.lower() if aircraft_name else ""

    for key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue

        key_upper = key.upper()
        if icao_upper and key_upper == icao_upper:
            return key, profile

        icao_aliases = profile.get("icao")
        if isinstance(icao_aliases, str):
            icao_aliases = [icao_aliases]
        if isinstance(icao_aliases, list):
            for alias in icao_aliases:
                alias_text = _safe_str(alias)
                if alias_text and alias_text.upper() == icao_upper:
                    return key, profile

    for key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if name_lower and key.lower() in name_lower:
            return key, profile

        name_contains = profile.get("name_contains")
        if isinstance(name_contains, str):
            name_contains = [name_contains]
        if isinstance(name_contains, list):
            for token in name_contains:
                token_text = _safe_str(token)
                if token_text and token_text.lower() in name_lower:
                    return key, profile

    return None, None


def _sample_summary(sample: MonitorSample) -> dict[str, Any]:
    return {
        "timestamp_epoch": sample.timestamp_epoch,
        "on_ground": sample.on_ground,
        "agl_ft": sample.agl_ft,
        "altitude_ft_msl": sample.altitude_ft_msl,
        "groundspeed_kt": sample.groundspeed_kt,
        "indicated_airspeed_kt": sample.indicated_airspeed_kt,
        "vertical_speed_fpm": sample.vertical_speed_fpm,
        "heading_deg": sample.heading_deg,
        "bank_deg": sample.bank_deg,
        "pitch_deg": sample.pitch_deg,
        "flaps_ratio": sample.flaps_ratio,
        "stall_warning_active": sample.stall_warning_active,
        "engine_running": list(sample.engine_running) if sample.engine_running is not None else None,
        "engine_count": sample.engine_count,
        "aircraft_icao": sample.aircraft_icao,
        "aircraft_name": sample.aircraft_name,
        "aircraft_tailnum": sample.aircraft_tailnum,
    }


def _finding_to_payload(finding: RuleFinding) -> dict[str, Any]:
    return {
        "rule_id": finding.rule_id,
        "severity": finding.severity.value,
        "phase": finding.phase.value,
        "message": finding.message,
        "evidence": finding.evidence,
        "timestamp_epoch": finding.timestamp_epoch,
        "cooldown_sec": finding.cooldown_sec,
    }
