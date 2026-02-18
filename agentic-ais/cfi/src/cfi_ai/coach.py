from __future__ import annotations

import json
import re
from contextlib import suppress
from typing import Any, Literal

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.memory import ListMemory, Memory, MemoryContent, MemoryMimeType
from autogen_core.models import ChatCompletionClient

from cfi_ai.config import CfiConfig
from cfi_ai.copilot_auth import is_copilot_auth_error
from cfi_ai.copilot_autogen_client import CopilotAutoGenClient
from cfi_ai.types import FlightDebrief, MonitorSample, RuleFinding


ALERT_CHIEF_SOURCE = "alert_chief_cfi"
EVALUATOR_CHIEF_SOURCE = "evaluator_chief_cfi"
DEBRIEF_CHIEF_SOURCE = "debrief_chief_cfi"

AIRFRAME_EXPERT_SYSTEM = (
    "You are a CFI expert in airframe, systems, and configuration management. "
    "Call out risks related to engine state, configuration, and aircraft-specific handling. "
    "Be concise and operational."
)

MANEUVER_EXPERT_SYSTEM = (
    "You are a CFI expert in attitude, energy, and maneuver quality. "
    "Focus on control precision, trend stability, and recovery quality."
)

ACS_EXPERT_SYSTEM = (
    "You are a CFI expert in checkride standards and ACS-style tolerances. "
    "Identify likely standard deviations and checkride-impacting errors."
)

SAFETY_EXPERT_SYSTEM = (
    "You are a safety-first CFI. "
    "Prioritize immediate hazards, unstable trajectories, and go-around/recovery timing."
)

ALERT_CHIEF_SYSTEM = (
    "You are the chief CFI synthesizer. "
    "Read expert outputs and produce exactly one plain sentence for pilot coaching. "
    "No markdown, no JSON, no prefix labels."
)

EVALUATOR_CHIEF_SYSTEM = (
    "You are the chief CFI synthesizer for rule evaluation. "
    "Output strict JSON only with key `findings`. "
    "Each finding object must contain rule_id, severity(P0|P1|P2), message, evidence(object), cooldown_sec(number). "
    "Return at most 3 findings and do not repeat deterministic findings."
)

DEBRIEF_CHIEF_SYSTEM = (
    "You are the chief CFI synthesizer for post-flight debrief. "
    "Output strict JSON only with keys: key_events (array), strengths (array), "
    "improvement_items (array), spoken_segments (array of exactly 3 strings)."
)


class CfiCoach:
    def __init__(self, config: CfiConfig) -> None:
        self._config = config
        self._model_client: ChatCompletionClient | None = None
        self._active_model: str | None = None

        self._memory: Memory = ListMemory(name="cfi_aircraft_memory")
        self._memory_signature: str | None = None

        self._alert_team: RoundRobinGroupChat | None = None
        self._evaluator_team: RoundRobinGroupChat | None = None
        self._debrief_team: RoundRobinGroupChat | None = None

    async def start(self) -> None:
        await self._set_model(self._config.autogen_model)

    async def stop(self) -> None:
        if self._model_client is not None:
            with suppress(Exception):
                await self._model_client.close()
        self._model_client = None
        self._active_model = None

        self._alert_team = None
        self._evaluator_team = None
        self._debrief_team = None

        with suppress(Exception):
            await self._memory.close()

    async def compose_alert(self, finding: RuleFinding, sample: MonitorSample | None) -> str:
        payload = {
            "finding": {
                "rule_id": finding.rule_id,
                "severity": finding.severity.value,
                "phase": finding.phase.value,
                "message": finding.message,
                "evidence": finding.evidence,
            },
            "sample": {
                "aircraft_icao": sample.aircraft_icao if sample is not None else None,
                "aircraft_name": sample.aircraft_name if sample is not None else None,
                "engine_count": sample.engine_count if sample is not None else None,
                "agl_ft": sample.agl_ft if sample is not None else None,
                "indicated_airspeed_kt": sample.indicated_airspeed_kt if sample is not None else None,
                "vertical_speed_fpm": sample.vertical_speed_fpm if sample is not None else None,
                "bank_deg": sample.bank_deg if sample is not None else None,
            },
            "task": "Produce one short, direct spoken coaching sentence for the pilot.",
        }

        task = (
            "CFI alert synthesis request.\n"
            "Use expert roles first, then chief must produce final answer.\n"
            "Chief output rules: exactly one sentence, plain text only.\n\n"
            "Payload JSON:\n"
            f"{json.dumps(payload, ensure_ascii=True)}"
        )

        try:
            response = await self._run_team_request(
                kind="alert",
                task=task,
                chief_source=ALERT_CHIEF_SOURCE,
            )
            return _normalize_spoken_line(response) or compose_fallback_alert(finding)
        except Exception:
            return compose_fallback_alert(finding)

    async def compose_debrief(self, payload: dict[str, Any]) -> FlightDebrief:
        task = (
            "CFI post-flight debrief synthesis request.\n"
            "Use expert roles then chief must output strict JSON only.\n"
            "Required JSON keys: key_events, strengths, improvement_items, spoken_segments (3 strings).\n\n"
            "Payload JSON:\n"
            f"{json.dumps(payload, ensure_ascii=True)}"
        )

        try:
            response = await self._run_team_request(
                kind="debrief",
                task=task,
                chief_source=DEBRIEF_CHIEF_SOURCE,
            )
            parsed = _extract_json_dict(response)
            if parsed is None:
                return compose_fallback_debrief(payload)
            return _debrief_from_model(payload, parsed)
        except Exception:
            return compose_fallback_debrief(payload)

    async def evaluate_additional_findings(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        team_payload = {
            **payload,
            "task": (
                "Return only additional findings not already listed in deterministic_findings. "
                "Output strict JSON only."
            ),
        }
        task = (
            "CFI hybrid evaluation request.\n"
            "Use expert roles then chief must output strict JSON with key `findings`.\n"
            "At most 3 findings.\n\n"
            "Payload JSON:\n"
            f"{json.dumps(team_payload, ensure_ascii=True)}"
        )

        try:
            response = await self._run_team_request(
                kind="evaluator",
                task=task,
                chief_source=EVALUATOR_CHIEF_SOURCE,
            )
            parsed = _extract_json_dict(response)
            if parsed is None:
                return []
            findings = parsed.get("findings")
            return _list_of_dicts(findings)[:3]
        except Exception:
            return []

    async def set_aircraft_memory(
        self,
        *,
        aircraft_context: dict[str, Any],
        defaults: list[str],
        profile_key: str | None,
        profile_data: dict[str, Any] | None,
    ) -> None:
        payload = {
            "aircraft_context": aircraft_context,
            "defaults": defaults,
            "profile_key": profile_key,
            "profile_data": profile_data or {},
        }
        signature = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
        if signature == self._memory_signature:
            return

        await self._memory.clear()

        if defaults:
            await self._memory.add(
                MemoryContent(
                    content={"type": "default_guidance", "notes": defaults},
                    mime_type=MemoryMimeType.JSON,
                    metadata={"scope": "default"},
                )
            )

        await self._memory.add(
            MemoryContent(
                content=aircraft_context,
                mime_type=MemoryMimeType.JSON,
                metadata={"scope": "aircraft_context"},
            )
        )

        if profile_data:
            await self._memory.add(
                MemoryContent(
                    content={
                        "profile_key": profile_key,
                        "profile": profile_data,
                    },
                    mime_type=MemoryMimeType.JSON,
                    metadata={"scope": "aircraft_profile"},
                )
            )

        self._memory_signature = signature

    async def _run_team_request(
        self,
        *,
        kind: Literal["alert", "evaluator", "debrief"],
        task: str,
        chief_source: str,
    ) -> str:
        errors: list[str] = []

        for model_name in self._candidate_models():
            if self._active_model != model_name:
                await self._set_model(model_name)

            team = self._team_for_kind(kind)
            if team is None:
                continue

            try:
                await team.reset()
                result = await team.run(task=task)
                return _extract_team_text(result, preferred_source=chief_source)
            except Exception as exc:  # noqa: BLE001
                if is_copilot_auth_error(exc):
                    raise
                errors.append(f"{model_name}: {exc}")
                if _is_model_access_error(exc):
                    continue
                raise

        raise RuntimeError("All model candidates failed: " + " | ".join(errors))

    def _team_for_kind(self, kind: Literal["alert", "evaluator", "debrief"]) -> RoundRobinGroupChat | None:
        if kind == "alert":
            return self._alert_team
        if kind == "evaluator":
            return self._evaluator_team
        return self._debrief_team

    async def _set_model(self, model_name: str) -> None:
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
        self._active_model = model_name
        self._build_teams()

    def _build_teams(self) -> None:
        if self._model_client is None:
            self._alert_team = None
            self._evaluator_team = None
            self._debrief_team = None
            return

        self._alert_team = RoundRobinGroupChat(
            participants=[
                self._expert_agent("alert_airframe_cfi", AIRFRAME_EXPERT_SYSTEM),
                self._expert_agent("alert_maneuver_cfi", MANEUVER_EXPERT_SYSTEM),
                self._expert_agent("alert_acs_cfi", ACS_EXPERT_SYSTEM),
                self._expert_agent("alert_safety_cfi", SAFETY_EXPERT_SYSTEM),
                self._chief_agent(ALERT_CHIEF_SOURCE, ALERT_CHIEF_SYSTEM),
            ],
            name="cfi_alert_team",
            description="Multi-expert CFI alert synthesis team.",
            max_turns=5,
        )

        self._evaluator_team = RoundRobinGroupChat(
            participants=[
                self._expert_agent("eval_airframe_cfi", AIRFRAME_EXPERT_SYSTEM),
                self._expert_agent("eval_maneuver_cfi", MANEUVER_EXPERT_SYSTEM),
                self._expert_agent("eval_acs_cfi", ACS_EXPERT_SYSTEM),
                self._expert_agent("eval_safety_cfi", SAFETY_EXPERT_SYSTEM),
                self._chief_agent(EVALUATOR_CHIEF_SOURCE, EVALUATOR_CHIEF_SYSTEM),
            ],
            name="cfi_evaluator_team",
            description="Multi-expert CFI additional finding evaluator team.",
            max_turns=5,
        )

        self._debrief_team = RoundRobinGroupChat(
            participants=[
                self._expert_agent("debrief_airframe_cfi", AIRFRAME_EXPERT_SYSTEM),
                self._expert_agent("debrief_maneuver_cfi", MANEUVER_EXPERT_SYSTEM),
                self._expert_agent("debrief_acs_cfi", ACS_EXPERT_SYSTEM),
                self._expert_agent("debrief_safety_cfi", SAFETY_EXPERT_SYSTEM),
                self._chief_agent(DEBRIEF_CHIEF_SOURCE, DEBRIEF_CHIEF_SYSTEM),
            ],
            name="cfi_debrief_team",
            description="Multi-expert CFI debrief synthesis team.",
            max_turns=5,
        )

    def _expert_agent(self, name: str, system_message: str) -> AssistantAgent:
        if self._model_client is None:
            raise RuntimeError("Model client not initialized.")
        return AssistantAgent(
            name=name,
            model_client=self._model_client,
            system_message=system_message,
            description="CFI domain expert",
            memory=[self._memory],
            reflect_on_tool_use=False,
            max_tool_iterations=1,
        )

    def _chief_agent(self, name: str, system_message: str) -> AssistantAgent:
        if self._model_client is None:
            raise RuntimeError("Model client not initialized.")
        return AssistantAgent(
            name=name,
            model_client=self._model_client,
            system_message=system_message,
            description="Chief CFI synthesizer",
            memory=[self._memory],
            reflect_on_tool_use=False,
            max_tool_iterations=1,
        )

    def _candidate_models(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []

        for model in (self._config.autogen_model, *self._config.autogen_model_fallbacks):
            if model and model not in seen:
                seen.add(model)
                ordered.append(model)

        return tuple(ordered)


def compose_fallback_alert(finding: RuleFinding) -> str:
    prefix = {
        "P0": "Immediate action",
        "P1": "Correction needed",
        "P2": "Coaching note",
    }.get(finding.severity.value, "CFI")
    return f"{prefix}: {finding.message}"


def compose_fallback_debrief(payload: dict[str, Any]) -> FlightDebrief:
    findings = payload.get("findings", [])
    findings_by_severity = _count_by_severity(findings)
    total_findings = sum(findings_by_severity.values())

    key_events: list[str] = []
    for item in findings[:3]:
        if isinstance(item, dict):
            message = str(item.get("message", "")).strip()
            if message:
                key_events.append(message)

    if not key_events:
        key_events.append("No major events were captured in the debrief timeline.")

    strengths: list[str] = []
    if findings_by_severity.get("P0", 0) == 0:
        strengths.append("No immediate-danger events were recorded.")
    if findings_by_severity.get("P1", 0) <= 2:
        strengths.append("Major deviations stayed relatively limited.")
    if findings_by_severity.get("P2", 0) <= 4:
        strengths.append("Basic control consistency was generally acceptable.")
    if not strengths:
        strengths.append("Flight completed with data sufficient for targeted retraining.")

    improvement_items: list[str] = []
    if findings:
        for item in findings[:3]:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message", "")).strip()
            if message:
                improvement_items.append(message)
    if not improvement_items:
        improvement_items.append("Focus on stabilized profiles and smooth energy management.")

    spoken_segments = [
        (
            "Flight complete. "
            f"Total findings: {total_findings}. "
            f"Danger: {findings_by_severity.get('P0', 0)}, "
            f"major: {findings_by_severity.get('P1', 0)}, "
            f"coaching: {findings_by_severity.get('P2', 0)}."
        ),
        "Top issues: " + "; ".join(key_events[:3]),
        "Strengths: " + "; ".join(strengths[:2]) + ". Next focus: " + "; ".join(improvement_items[:2]),
    ]

    return FlightDebrief(
        total_findings=total_findings,
        findings_by_severity=findings_by_severity,
        key_events=key_events,
        strengths=strengths,
        improvement_items=improvement_items,
        spoken_segments=[_normalize_spoken_line(x) for x in spoken_segments],
    )


def _debrief_from_model(payload: dict[str, Any], parsed: dict[str, Any]) -> FlightDebrief:
    findings = payload.get("findings", [])
    findings_by_severity = _count_by_severity(findings)
    total_findings = sum(findings_by_severity.values())

    key_events = _list_of_strings(parsed.get("key_events")) or [
        "No major events were captured in the debrief timeline."
    ]
    strengths = _list_of_strings(parsed.get("strengths")) or [
        "Flight completed with enough data to guide focused practice."
    ]
    improvement_items = _list_of_strings(parsed.get("improvement_items")) or [
        "Focus on stabilized profiles and smooth energy management."
    ]

    spoken_segments = _list_of_strings(parsed.get("spoken_segments"))
    if len(spoken_segments) < 3:
        return compose_fallback_debrief(payload)

    spoken_segments = [_normalize_spoken_line(x) for x in spoken_segments[:3]]

    return FlightDebrief(
        total_findings=total_findings,
        findings_by_severity=findings_by_severity,
        key_events=key_events,
        strengths=strengths,
        improvement_items=improvement_items,
        spoken_segments=spoken_segments,
    )


def _extract_team_text(result: TaskResult, *, preferred_source: str) -> str:
    for message in reversed(result.messages):
        source = getattr(message, "source", "")
        content = getattr(message, "content", None)
        if source == preferred_source and isinstance(content, str) and content.strip():
            return content.strip()

    for message in reversed(result.messages):
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()

    return ""


def _count_by_severity(findings: list[Any]) -> dict[str, int]:
    counts = {"P0": 0, "P1": 0, "P2": 0}
    for item in findings:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).upper()
        if severity in counts:
            counts[severity] += 1
    return counts


def _extract_json_dict(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None

    with suppress(json.JSONDecodeError):
        data = json.loads(text)
        if isinstance(data, dict):
            return data

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    with suppress(json.JSONDecodeError):
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data

    return None


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                output.append(cleaned)
    return output


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            output.append(item)
    return output


def _normalize_spoken_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().strip('"')
        if cleaned:
            return cleaned[:240]
    return ""


def _is_model_access_error(exc: Exception) -> bool:
    text = str(exc)
    return ("no_access" in text and "No access to model" in text) or (
        "Error code: 403" in text and "No access to model" in text
    )
