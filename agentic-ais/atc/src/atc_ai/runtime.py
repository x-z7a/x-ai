from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from dataclasses import dataclass
from contextlib import suppress
from typing import Any

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.ui import Console
from autogen_core.models import ChatCompletionClient

from atc_ai.atc_tools import AtcTools
from atc_ai.copilot_auth import copilot_auth_error_message, is_copilot_auth_error
from atc_ai.copilot_autogen_client import CopilotAutoGenClient
from atc_ai.config import AtcConfig
from atc_ai.copilot_experts import CopilotExperts
from atc_ai.xplane_mcp import XPlaneMCPClient


CONTROLLER_SYSTEM_PROMPT = """
You are the tower ATC coordinator for an X-Plane simulator.
You are acting as ATC and answering pilot requests directly.

Execution policy:
1. First call fetch_aircraft_state to gather live sim context.
2. Ask the experts before issuing instructions:
   - ask_airport_expert for airport/chart/nav/weather/ATIS context
   - ask_flow_expert for runway/sequence strategy
   - ask_phraseology_expert for final concise transmission wording
3. Produce a final ATC response with:
   - Airport context summary
   - Clearance/Instruction block
   - Final radio phraseology
4. For pilot-facing requests/conversation turns, call transmit_radio exactly once
   with the final phraseology.
   - Use confirm=true so the transmission is spoken now.
   - Do this after phraseology is finalized and before your final text response.
5. If the user explicitly asks for text-only/no transmission, skip transmit_radio.

Keep wording concise and operational. Do not fabricate tool outputs.
Your final answer must include the exact ATC radio transmission for the pilot.
"""

MONITOR_POLL_SEC = 2.0
ALERT_COOLDOWN_SEC = 12.0
OPS_TX_COOLDOWN_SEC = 20.0
FREQ_MISMATCH_REMINDER_COOLDOWN_SEC = 30.0


@dataclass
class ClearanceTargets:
    issued_at_epoch: float
    phraseology: str
    squawk_code: int | None = None
    heading_deg: float | None = None
    altitude_ft: float | None = None
    speed_kt: float | None = None
    expected_freq_mhz: float | None = None
    expected_contact: str | None = None


@dataclass
class MonitorOpsState:
    field_elevation_ft: float | None = None
    was_on_ground: bool | None = None
    departure_handover_issued: bool = False
    tower_handover_issued: bool = False
    ground_handover_issued: bool = False
    last_ops_tx_epoch: float = 0.0
    last_freq_reminder_epoch: float = 0.0


class AtcRuntime:
    def __init__(self, config: AtcConfig) -> None:
        self._config = config
        self._mcp = XPlaneMCPClient(config.xplane_mcp_sse_url)
        self._experts = CopilotExperts(
            github_token=config.github_token,
            use_logged_in_user=config.copilot_use_logged_in_user,
            model=config.copilot_model,
            use_custom_provider=config.copilot_use_custom_provider,
            provider_base_url=config.copilot_base_url,
            provider_bearer_token=config.copilot_bearer_token,
        )
        self._tools: AtcTools | None = None
        self._agent: AssistantAgent | None = None
        self._model_client: ChatCompletionClient | None = None
        self._active_autogen_model: str | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._monitor_stop_event = asyncio.Event()
        self._active_clearance: ClearanceTargets | None = None
        self._last_alert_epoch: dict[str, float] = {}
        self._last_processed_transmission_epoch = 0.0
        self._ops = MonitorOpsState()

    async def start(self) -> None:
        await self._mcp.connect()
        await self._experts.start()
        self._tools = AtcTools(
            mcp_client=self._mcp,
            experts=self._experts,
            auto_transmit=self._config.auto_transmit,
        )
        await self._set_autogen_model(self._config.autogen_model)
        self._start_monitor()

    async def stop(self) -> None:
        await self._stop_monitor()
        await self._experts.stop()
        await self._mcp.close()
        if self._model_client is not None:
            with suppress(Exception):
                await self._model_client.close()
        self._model_client = None
        self._tools = None
        self._agent = None

    def _start_monitor(self) -> None:
        self._monitor_stop_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _stop_monitor(self) -> None:
        self._monitor_stop_event.set()
        if self._monitor_task is not None:
            with suppress(Exception):
                await self._monitor_task
        self._monitor_task = None

    async def run_once(self, task: str) -> None:
        if self._agent is None:
            raise RuntimeError("ATC runtime has not been started.")
        try:
            await Console(self._agent.run_stream(task=task))
            return
        except Exception as exc:  # noqa: BLE001
            if is_copilot_auth_error(exc):
                print(
                    "Copilot authentication error detected. See message below for details."
                )
                print(
                    type(exc).__name__
                    + ": "
                    + str(exc)
                    + "\n"
                    + copilot_auth_error_message(
                        self._config.copilot_use_logged_in_user
                    )
                )
                raise RuntimeError(
                    copilot_auth_error_message(self._config.copilot_use_logged_in_user)
                ) from exc
            if not _is_model_access_error(exc):
                raise
            if not await self._run_with_fallback_models(task):
                raise

    async def _run_with_fallback_models(self, task: str) -> bool:
        current_model = self._active_autogen_model
        for candidate_model in self._config.autogen_model_fallbacks:
            if candidate_model == current_model:
                continue

            await self._set_autogen_model(candidate_model)
            if self._agent is None:
                raise RuntimeError("ATC runtime has not been started.")

            try:
                await Console(self._agent.run_stream(task=task))
                return True
            except Exception as exc:  # noqa: BLE001
                if _is_model_access_error(exc):
                    continue
                raise

        return False

    async def _set_autogen_model(self, model_name: str) -> None:
        if self._model_client is not None:
            with suppress(Exception):
                await self._model_client.close()

        self._model_client = CopilotAutoGenClient(
            model=model_name,
            github_token=self._config.github_token,
            use_logged_in_user=self._config.copilot_use_logged_in_user,
            use_custom_provider=self._config.copilot_use_custom_provider,
            provider_base_url=self._config.autogen_base_url,
            provider_bearer_token=self._config.autogen_api_key,
        )
        self._active_autogen_model = model_name
        self._agent = self._build_agent()

    def _build_agent(self) -> AssistantAgent:
        if self._tools is None or self._model_client is None:
            raise RuntimeError("ATC runtime dependencies are not initialized.")
        kwargs: dict[str, Any] = {
            "name": "tower_coordinator",
            "model_client": self._model_client,
            "system_message": CONTROLLER_SYSTEM_PROMPT.strip(),
            "tools": [
                self._tools.fetch_aircraft_state,
                self._tools.ask_airport_expert,
                self._tools.ask_flow_expert,
                self._tools.ask_phraseology_expert,
                self._tools.transmit_radio,
            ],
        }

        # Keep compatibility across AutoGen minor versions while enabling
        # post-tool final synthesis when supported.
        params = inspect.signature(AssistantAgent.__init__).parameters
        if "reflect_on_tool_use" in params:
            kwargs["reflect_on_tool_use"] = True
        if "max_tool_iterations" in params:
            kwargs["max_tool_iterations"] = 4

        return AssistantAgent(**kwargs)

    async def _monitor_loop(self) -> None:
        while not self._monitor_stop_event.is_set():
            try:
                await self._monitor_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[MONITOR] State monitor error: {exc}")
            try:
                await asyncio.wait_for(
                    self._monitor_stop_event.wait(),
                    timeout=MONITOR_POLL_SEC,
                )
            except TimeoutError:
                continue

    async def _monitor_once(self) -> None:
        if self._tools is None:
            return
        phraseology, issued_at = self._tools.last_transmitted()
        if phraseology and issued_at > self._last_processed_transmission_epoch:
            parsed = _parse_clearance_targets(phraseology, issued_at)
            self._apply_clearance_update(parsed)
            self._last_processed_transmission_epoch = issued_at

        state = await self._mcp.fetch_aircraft_state()
        await self._maybe_manage_handover(state)

        if self._active_clearance is None:
            return
        alerts = _evaluate_deviations(state, self._active_clearance)
        if not alerts:
            return

        now = time.time()
        for alert in alerts:
            last = self._last_alert_epoch.get(alert, 0.0)
            if now - last < ALERT_COOLDOWN_SEC:
                continue
            self._last_alert_epoch[alert] = now
            print(f"[MONITOR] Pilot deviation: {alert}")
            if alert.startswith("frequency mismatch"):
                await self._maybe_transmit_frequency_reminder()

    def _apply_clearance_update(self, incoming: ClearanceTargets) -> None:
        if self._active_clearance is None:
            self._active_clearance = incoming
            return
        self._active_clearance = _merge_clearance_targets(
            self._active_clearance,
            incoming,
        )

    async def _maybe_manage_handover(self, state: dict[str, Any]) -> None:
        on_ground = bool(state.get("on_ground", False))
        ias = _to_float(state.get("indicated_airspeed_kt")) or 0.0
        groundspeed_m_s = _to_float(state.get("groundspeed_m_s")) or 0.0
        groundspeed_kt = groundspeed_m_s * 1.94384
        elevation_m = _to_float(state.get("elevation_m"))
        elevation_ft = elevation_m * 3.28084 if elevation_m is not None else None
        vertical_speed_fpm = _to_float(state.get("vertical_speed_fpm")) or 0.0

        if on_ground and elevation_ft is not None and groundspeed_kt < 40:
            self._ops.field_elevation_ft = elevation_ft

        agl_ft: float | None = None
        if elevation_ft is not None and self._ops.field_elevation_ft is not None:
            agl_ft = elevation_ft - self._ops.field_elevation_ft

        was_on_ground = self._ops.was_on_ground
        self._ops.was_on_ground = on_ground

        takeoff_transition = was_on_ground is True and not on_ground and ias >= 60
        landing_transition = was_on_ground is False and on_ground and groundspeed_kt >= 15

        if (
            takeoff_transition
            and not self._ops.departure_handover_issued
            and self._can_send_ops_transmission()
        ):
            await self._issue_handover("departure", state)
            self._ops.departure_handover_issued = True
            self._ops.tower_handover_issued = False
            self._ops.ground_handover_issued = False
            return

        if (
            not on_ground
            and agl_ft is not None
            and agl_ft < 3500
            and self._ops.departure_handover_issued
            and not self._ops.tower_handover_issued
            and vertical_speed_fpm < -500
            and self._can_send_ops_transmission()
        ):
            await self._issue_handover("tower", state)
            self._ops.tower_handover_issued = True
            return

        if (
            landing_transition
            and not self._ops.ground_handover_issued
            and self._can_send_ops_transmission()
        ):
            await self._issue_handover("ground", state)
            self._ops.ground_handover_issued = True
            self._ops.departure_handover_issued = False
            self._ops.tower_handover_issued = False

    def _can_send_ops_transmission(self) -> bool:
        return (time.time() - self._ops.last_ops_tx_epoch) >= OPS_TX_COOLDOWN_SEC

    async def _issue_handover(self, target: str, state: dict[str, Any]) -> None:
        if self._tools is None:
            return
        context = {
            "phase_handover_target": target,
            "current_state": {
                "on_ground": state.get("on_ground"),
                "com1_hz": state.get("com1_hz"),
                "indicated_airspeed_kt": state.get("indicated_airspeed_kt"),
                "groundspeed_m_s": state.get("groundspeed_m_s"),
                "vertical_speed_fpm": state.get("vertical_speed_fpm"),
                "elevation_m": state.get("elevation_m"),
                "latitude": state.get("latitude"),
                "longitude": state.get("longitude"),
            },
            "current_clearance": (
                self._active_clearance.phraseology if self._active_clearance is not None else ""
            ),
        }
        question = (
            "Provide one short ATC handover transmission for the target controller. "
            f"Target: {target}. "
            "Include frequency only if known from context. If unknown, do not invent one."
        )
        expert_reply = await self._tools.ask_airport_expert(
            question=question,
            context_json=json.dumps(context, ensure_ascii=True),
        )
        phraseology = _coerce_single_transmission(expert_reply, target)
        tx_result = await self._tools.transmit_radio(message=phraseology, confirm=True)
        self._ops.last_ops_tx_epoch = time.time()
        print(f"[MONITOR] Auto handover ({target}): {phraseology}")
        print(f"[MONITOR] {tx_result}")
        self._apply_clearance_update(
            _parse_clearance_targets(phraseology, self._ops.last_ops_tx_epoch)
        )

    async def _maybe_transmit_frequency_reminder(self) -> None:
        if self._tools is None or self._active_clearance is None:
            return
        expected_freq = self._active_clearance.expected_freq_mhz
        if expected_freq is None:
            return
        now = time.time()
        if (
            now - self._ops.last_freq_reminder_epoch
            < FREQ_MISMATCH_REMINDER_COOLDOWN_SEC
        ):
            return
        contact = self._active_clearance.expected_contact or "assigned controller"
        reminder = (
            f"Verify frequency, contact {contact} on "
            f"{_format_frequency_mhz(expected_freq)}."
        )
        tx_result = await self._tools.transmit_radio(message=reminder, confirm=True)
        self._ops.last_freq_reminder_epoch = now
        self._ops.last_ops_tx_epoch = now
        print(f"[MONITOR] Frequency reminder: {reminder}")
        print(f"[MONITOR] {tx_result}")


def _is_model_access_error(exc: Any) -> bool:
    text = str(exc)
    return ("no_access" in text and "No access to model" in text) or (
        "Error code: 403" in text and "No access to model" in text
    )


def _evaluate_deviations(state: dict[str, Any], clearance: ClearanceTargets) -> list[str]:
    alerts: list[str] = []
    age_sec = max(0.0, time.time() - clearance.issued_at_epoch)

    on_ground = bool(state.get("on_ground", False))
    ias = _to_float(state.get("indicated_airspeed_kt"))
    heading = _to_float(
        state.get("magnetic_heading_deg", state.get("heading_true_deg"))
    )
    altitude_m = _to_float(state.get("elevation_m"))
    altitude_ft = altitude_m * 3.28084 if altitude_m is not None else None
    transponder = _to_int(state.get("transponder_code"))
    com1_mhz = _com1_hz_to_mhz(state.get("com1_hz"))

    if clearance.squawk_code is not None and transponder is not None:
        if transponder != clearance.squawk_code:
            alerts.append(
                f"squawk mismatch (assigned {clearance.squawk_code}, actual {transponder})"
            )

    if (
        clearance.heading_deg is not None
        and heading is not None
        and not on_ground
        and ias is not None
        and ias >= 60
        and age_sec >= 20
    ):
        diff = _heading_delta_deg(heading, clearance.heading_deg)
        if diff > 35:
            alerts.append(
                f"heading deviation ({int(round(heading)):03d} vs assigned {int(round(clearance.heading_deg)):03d})"
            )

    if (
        clearance.altitude_ft is not None
        and altitude_ft is not None
        and not on_ground
        and age_sec >= 120
    ):
        delta_ft = abs(altitude_ft - clearance.altitude_ft)
        if delta_ft > 1500:
            alerts.append(
                "altitude deviation "
                f"({int(round(altitude_ft))} ft vs assigned {int(round(clearance.altitude_ft))} ft)"
            )

    if (
        clearance.speed_kt is not None
        and ias is not None
        and not on_ground
        and age_sec >= 60
    ):
        if abs(ias - clearance.speed_kt) > 40:
            alerts.append(
                f"speed deviation ({int(round(ias))} kt vs assigned {int(round(clearance.speed_kt))} kt)"
            )

    if (
        clearance.expected_freq_mhz is not None
        and com1_mhz is not None
        and age_sec >= 8
    ):
        if abs(com1_mhz - clearance.expected_freq_mhz) > 0.015:
            expected = _format_frequency_mhz(clearance.expected_freq_mhz)
            actual = _format_frequency_mhz(com1_mhz)
            alerts.append(
                f"frequency mismatch (expected {expected}, actual {actual})"
            )

    return alerts


def _parse_clearance_targets(phraseology: str, issued_at_epoch: float) -> ClearanceTargets:
    text = phraseology.lower()
    targets = ClearanceTargets(
        issued_at_epoch=issued_at_epoch,
        phraseology=phraseology,
    )

    squawk_match = re.search(r"\bsquawk\s+(\d{4})\b", text)
    if squawk_match:
        targets.squawk_code = int(squawk_match.group(1))

    heading_match = re.search(
        r"\b(?:fly\s+)?(?:turn\s+(?:left|right)\s+)?heading\s+(\d{1,3})\b",
        text,
    )
    if heading_match:
        heading = int(heading_match.group(1))
        targets.heading_deg = float(heading % 360)

    fl_match = re.search(r"\bflight\s+level\s+(\d{2,3})\b", text)
    if fl_match:
        targets.altitude_ft = float(int(fl_match.group(1)) * 100)
    else:
        altitude_match = re.search(
            r"\b(?:climb and maintain|descend and maintain|maintain)\s+(\d{3,5})\b",
            text,
        )
        if altitude_match:
            value = int(altitude_match.group(1))
            if value >= 1000:
                targets.altitude_ft = float(value)

    speed_match = re.search(
        r"\b(?:maintain|reduce speed to)\s+(\d{2,3})\s*(?:knots|kts|kt)\b",
        text,
    )
    if speed_match:
        targets.speed_kt = float(int(speed_match.group(1)))

    targets.expected_freq_mhz = _extract_frequency_mhz(text)
    targets.expected_contact = _extract_contact_target(text)

    return targets


def _merge_clearance_targets(
    current: ClearanceTargets,
    incoming: ClearanceTargets,
) -> ClearanceTargets:
    return ClearanceTargets(
        issued_at_epoch=max(current.issued_at_epoch, incoming.issued_at_epoch),
        phraseology=incoming.phraseology or current.phraseology,
        squawk_code=(
            incoming.squawk_code
            if incoming.squawk_code is not None
            else current.squawk_code
        ),
        heading_deg=(
            incoming.heading_deg
            if incoming.heading_deg is not None
            else current.heading_deg
        ),
        altitude_ft=(
            incoming.altitude_ft
            if incoming.altitude_ft is not None
            else current.altitude_ft
        ),
        speed_kt=(
            incoming.speed_kt if incoming.speed_kt is not None else current.speed_kt
        ),
        expected_freq_mhz=(
            incoming.expected_freq_mhz
            if incoming.expected_freq_mhz is not None
            else current.expected_freq_mhz
        ),
        expected_contact=(
            incoming.expected_contact
            if incoming.expected_contact is not None
            else current.expected_contact
        ),
    )


def _extract_frequency_mhz(text: str) -> float | None:
    # Common ATC format: 118.8 / 118.80 / 118.800
    match = re.search(r"\b(1[1-3][0-9]\.\d{1,3})\b", text)
    if match:
        return float(match.group(1))
    return None


def _extract_contact_target(text: str) -> str | None:
    for target in ("departure", "tower", "ground", "approach", "center"):
        if target in text:
            return target
    return None


def _com1_hz_to_mhz(value: Any) -> float | None:
    freq = _to_float(value)
    if freq is None:
        return None
    if freq >= 1000:
        return freq / 1000.0
    return freq


def _format_frequency_mhz(freq_mhz: float) -> str:
    return f"{freq_mhz:.3f}".rstrip("0").rstrip(".")


def _coerce_single_transmission(raw: str, target: str) -> str:
    for line in raw.splitlines():
        cleaned = line.strip().strip('"')
        if cleaned:
            # Use first concise line from expert output.
            return cleaned[:220]
    return f"Contact {target}."


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


def _heading_delta_deg(a: float, b: float) -> float:
    diff = abs((a - b) % 360.0)
    return min(diff, 360.0 - diff)
