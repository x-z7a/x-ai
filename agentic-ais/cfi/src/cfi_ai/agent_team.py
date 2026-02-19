from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage
from autogen_agentchat.teams import SelectorGroupChat
from autogen_core.models import ChatCompletionClient

from cfi_ai.config import CfiConfig
from cfi_ai.copilot_autogen_client import CopilotAutoGenClient
from cfi_ai.memory.base import MemoryProvider
from cfi_ai.types import (
    FlightPhase,
    FlightSnapshot,
    HazardProfile,
    ReviewWindow,
    SessionProfile,
    TeamDecision,
)


PHASE_PROMPT_FILE: dict[FlightPhase, str] = {
    FlightPhase.PREFLIGHT: "phase_preflight.md",
    FlightPhase.TAXI_OUT: "phase_taxi_out.md",
    FlightPhase.TAKEOFF: "phase_takeoff.md",
    FlightPhase.INITIAL_CLIMB: "phase_initial_climb.md",
    FlightPhase.CRUISE: "phase_cruise.md",
    FlightPhase.DESCENT: "phase_descent.md",
    FlightPhase.APPROACH: "phase_approach.md",
    FlightPhase.LANDING: "phase_landing.md",
    FlightPhase.TAXI_IN: "phase_taxi_in.md",
}

STARTUP_SYSTEM_PROMPT = """
You are a simulator CFI session bootstrap assistant.
Use the startup telemetry summary to infer aircraft context for training.

Output strict JSON only:
{
  "aircraft_icao": "C172",
  "aircraft_category": "single_engine_piston",
  "confidence": 0.0,
  "assumptions": ["..."],
  "welcome_message": "...",
  "hazard_profile": {
    "enabled_rules": ["stall_or_low_speed", "..."],
    "thresholds": {
      "low_airspeed_kt": 50,
      "max_taxi_speed_kt": 30
    },
    "notes": ["..."]
  }
}

Rules:
- ICAO must be 2-4 uppercase alphanumeric characters.
- If uncertain, default to C172 and say so in assumptions.
- welcome_message must be concise and student-friendly.
- Tune hazard_profile thresholds for the inferred aircraft type.
- Only include known rule IDs:
  stall_or_low_speed, excessive_sink_low_alt, high_bank_low_alt, pull_up_now,
  excessive_taxi_speed, unstable_approach_fast_or_sink.
- Focus on primary VFR GA training context.
"""


class CfiAgentTeam:
    def __init__(
        self,
        *,
        config: CfiConfig,
        memory_provider: MemoryProvider,
        team_chat_log_path: str,
    ) -> None:
        self._config = config
        self._memory_provider = memory_provider
        self._team_chat_log_path = Path(team_chat_log_path)

        self._model_client: ChatCompletionClient | None = None
        self._agents: dict[str, AssistantAgent] = {}
        self._phase_to_agent_name: dict[FlightPhase, str] = {}
        self._master_name = "master_cfi"
        self._team: SelectorGroupChat | None = None

        self._active_expert_name = ""

    async def start(self) -> None:
        self._model_client = CopilotAutoGenClient(
            model=self._config.autogen_model,
            github_token=self._config.github_token,
            use_logged_in_user=self._config.copilot_use_logged_in_user,
            use_custom_provider=self._config.copilot_use_custom_provider,
            provider_base_url=self._config.autogen_base_url,
            provider_bearer_token=self._config.autogen_api_key,
        )
        self._build_agents_and_team()

    async def stop(self) -> None:
        if self._model_client is not None:
            await self._model_client.close()
        self._model_client = None
        self._team = None
        self._agents.clear()
        self._phase_to_agent_name.clear()

    async def run_review(
        self,
        review: ReviewWindow,
        session_profile: SessionProfile | None = None,
    ) -> TeamDecision:
        if self._team is None:
            raise RuntimeError("CfiAgentTeam is not started.")

        phase_agent = self._phase_to_agent_name[review.phase]
        self._active_expert_name = phase_agent

        task = self._build_task(review, session_profile=session_profile)
        result = await self._team.run(task=task)
        await self._team.reset()

        self._write_team_log(review, result.messages)

        master_content = self._extract_master_output(result.messages)
        return self.parse_decision(master_content, review.phase)

    async def bootstrap_session(self, snapshots: list[FlightSnapshot]) -> SessionProfile:
        if self._model_client is None:
            raise RuntimeError("CfiAgentTeam is not started.")

        bootstrap_agent = AssistantAgent(
            name="startup_bootstrapper",
            model_client=self._model_client,
            description="Build initial aircraft profile and welcome message.",
            system_message=STARTUP_SYSTEM_PROMPT.strip(),
        )

        summary_payload = {
            "startup_summary": _summarize_startup_snapshots(snapshots),
            "defaults": {
                "aircraft_icao": "C172",
                "aircraft_category": "single_engine_piston",
            },
        }
        task = json.dumps(summary_payload, ensure_ascii=True)

        try:
            result = await bootstrap_agent.run(task=task)
            raw = self._extract_last_output_text(result.messages)
            return self.parse_startup_profile(raw)
        except Exception as exc:  # noqa: BLE001
            default_profile = _default_hazard_profile_for_aircraft(
                aircraft_icao="C172",
                aircraft_category="single_engine_piston",
            )
            return SessionProfile(
                aircraft_icao="C172",
                aircraft_category="single_engine_piston",
                confidence=0.0,
                assumptions=[f"Bootstrap fallback due to model error: {type(exc).__name__}"],
                welcome_message=(
                    "Welcome aboard. We'll fly this session as a Cessna 172 profile and coach each phase."
                ),
                hazard_profile=default_profile,
                raw_llm_output=str(exc),
            )

    @staticmethod
    def choose_candidates(
        messages: Sequence[BaseAgentEvent | BaseChatMessage],
        active_expert_name: str,
        master_name: str,
    ) -> list[str]:
        if not messages:
            return [active_expert_name]

        # Ignore user/task message when determining speaker turn.
        agent_msgs = [
            msg
            for msg in messages
            if getattr(msg, "source", "") not in {"user", "system", ""}
        ]
        if not agent_msgs:
            return [active_expert_name]

        last_source = getattr(agent_msgs[-1], "source", "")
        if last_source == active_expert_name:
            return [master_name]
        return [master_name]

    @staticmethod
    def parse_decision(raw_text: str, phase: FlightPhase) -> TeamDecision:
        parsed = _extract_json_object(raw_text)
        if not isinstance(parsed, dict):
            summary = _coerce_summary_from_text(raw_text)
            feedback = _coerce_feedback_items_from_text(raw_text)
            inferred_speak_now, inferred_speak_text = _infer_nonurgent_speech(
                summary,
                feedback,
            )
            return TeamDecision(
                phase=phase,
                summary=summary,
                feedback_items=feedback,
                speak_now=inferred_speak_now,
                speak_text=inferred_speak_text,
                raw_master_output=raw_text,
            )

        summary = str(parsed.get("summary", "")).strip() or "No summary provided."

        feedback_items_raw = parsed.get("feedback_items", [])
        feedback_items: list[str] = []
        if isinstance(feedback_items_raw, list):
            for item in feedback_items_raw:
                text = str(item).strip()
                if text:
                    feedback_items.append(text)
        if not feedback_items:
            feedback_items = ["No specific feedback items."]

        speak_now_raw = parsed.get("speak_now")
        speak_now: bool | None
        if isinstance(speak_now_raw, bool):
            speak_now = speak_now_raw
        elif isinstance(speak_now_raw, (int, float)):
            speak_now = bool(speak_now_raw)
        else:
            speak_now = None

        speak_text = str(parsed.get("speak_text", "")).strip()

        if speak_now is None:
            if speak_text:
                speak_now = True
            else:
                speak_now, speak_text = _infer_nonurgent_speech(summary, feedback_items)
        elif speak_now and not speak_text:
            inferred_speak_now, inferred_speak_text = _infer_nonurgent_speech(
                summary,
                feedback_items,
            )
            if inferred_speak_now and inferred_speak_text:
                speak_text = inferred_speak_text
            else:
                speak_now = False

        if not speak_text:
            speak_now = False

        return TeamDecision(
            phase=phase,
            summary=summary,
            feedback_items=feedback_items[:3],
            speak_now=speak_now,
            speak_text=speak_text,
            raw_master_output=raw_text,
        )

    @staticmethod
    def parse_startup_profile(raw_text: str) -> SessionProfile:
        parsed = _extract_json_object(raw_text)
        if not isinstance(parsed, dict):
            default_profile = _default_hazard_profile_for_aircraft(
                aircraft_icao="C172",
                aircraft_category="single_engine_piston",
            )
            return SessionProfile(
                aircraft_icao="C172",
                aircraft_category="single_engine_piston",
                confidence=0.0,
                assumptions=["No valid bootstrap JSON; defaulted to C172 profile."],
                welcome_message=(
                    "Welcome aboard. We'll start with a Cessna 172 training profile for this lesson."
                ),
                hazard_profile=default_profile,
                raw_llm_output=raw_text,
            )

        aircraft_icao = str(parsed.get("aircraft_icao", "C172")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,4}", aircraft_icao):
            aircraft_icao = "C172"

        aircraft_category = (
            str(parsed.get("aircraft_category", "single_engine_piston")).strip().lower()
            or "single_engine_piston"
        )

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))

        assumptions_raw = parsed.get("assumptions", [])
        assumptions: list[str] = []
        if isinstance(assumptions_raw, list):
            for item in assumptions_raw:
                text = str(item).strip()
                if text:
                    assumptions.append(text)
        if not assumptions:
            assumptions = ["Default assumptions applied for training profile."]

        welcome_message = str(parsed.get("welcome_message", "")).strip()
        if not welcome_message:
            welcome_message = (
                f"Welcome aboard. We'll use a {aircraft_icao} training profile and coach each flight phase."
            )

        hazard_profile = _parse_hazard_profile(
            raw_hazard=parsed.get("hazard_profile"),
            aircraft_icao=aircraft_icao,
            aircraft_category=aircraft_category,
        )

        return SessionProfile(
            aircraft_icao=aircraft_icao,
            aircraft_category=aircraft_category,
            confidence=confidence,
            assumptions=assumptions[:5],
            welcome_message=welcome_message,
            hazard_profile=hazard_profile,
            raw_llm_output=raw_text,
        )

    def _build_agents_and_team(self) -> None:
        if self._model_client is None:
            raise RuntimeError("Model client is not initialized.")

        prompt_root = Path(__file__).resolve().parent / "prompts"
        master_prompt = (prompt_root / "master_cfi.md").read_text(encoding="utf-8").strip()

        self._agents.clear()
        self._phase_to_agent_name.clear()

        for phase, prompt_file in PHASE_PROMPT_FILE.items():
            phase_prompt = (prompt_root / prompt_file).read_text(encoding="utf-8").strip()
            name = f"{phase.value}_expert"
            memory = self._memory_provider.attach_to_agent(name=name)

            agent = AssistantAgent(
                name=name,
                model_client=self._model_client,
                description=f"CFI expert for {phase.value} stage.",
                system_message=phase_prompt,
                memory=memory,
            )
            self._agents[name] = agent
            self._phase_to_agent_name[phase] = name

        master_memory = self._memory_provider.attach_to_agent(name=self._master_name)
        master = AssistantAgent(
            name=self._master_name,
            model_client=self._model_client,
            description="Master CFI coordinator and final decision maker.",
            system_message=master_prompt,
            memory=master_memory,
        )
        self._agents[self._master_name] = master

        participants = [self._agents[name] for name in sorted(self._agents.keys())]
        termination = MaxMessageTermination(2)

        self._team = SelectorGroupChat(
            participants,
            model_client=self._model_client,
            termination_condition=termination,
            max_turns=2,
            allow_repeated_speaker=False,
            candidate_func=lambda messages: self.choose_candidates(
                messages,
                active_expert_name=self._active_expert_name,
                master_name=self._master_name,
            ),
        )

    def _build_task(
        self,
        review: ReviewWindow,
        session_profile: SessionProfile | None,
    ) -> str:
        payload = {
            "review_window": asdict(review),
            "session_profile": asdict(session_profile) if session_profile is not None else None,
            "instructions": {
                "turn_policy": [
                    "Phase expert provides stage-specific analysis.",
                    "Master CFI provides final JSON decision.",
                ],
                "output": "Master must return strict JSON only.",
                "aircraft_constraint": (
                    "Use session_profile.aircraft_icao and aircraft_category to tailor guidance."
                ),
            },
        }
        return json.dumps(payload, ensure_ascii=True)

    def _extract_master_output(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str:
        for message in reversed(messages):
            if getattr(message, "source", "") != self._master_name:
                continue
            content = getattr(message, "content", "")
            if isinstance(content, str):
                return content
            return str(content)

        # Fallback to the final message if master was not found.
        if messages:
            content = getattr(messages[-1], "content", "")
            if isinstance(content, str):
                return content
            return str(content)
        return ""

    def _extract_last_output_text(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> str:
        if not messages:
            return ""
        content = getattr(messages[-1], "content", "")
        if isinstance(content, str):
            return content
        return str(content)

    def _write_team_log(
        self,
        review: ReviewWindow,
        messages: Sequence[BaseAgentEvent | BaseChatMessage],
    ) -> None:
        self._team_chat_log_path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with self._team_chat_log_path.open("a", encoding="utf-8") as f:
            for msg in messages:
                record = {
                    "ts": now,
                    "phase": review.phase.value,
                    "window_start": review.start_epoch,
                    "window_end": review.end_epoch,
                    "source": getattr(msg, "source", "unknown"),
                    "content": _to_text(getattr(msg, "content", "")),
                }
                f.write(json.dumps(record, ensure_ascii=True) + "\n")


def _to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=True)
    except TypeError:
        return str(content)


def _json_candidates(raw_text: str) -> list[str]:
    out: list[str] = []
    stripped = raw_text.strip()
    if stripped:
        out.append(stripped)

    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw_text, flags=re.IGNORECASE)
    out.extend(fenced)

    first_open = raw_text.find("{")
    last_close = raw_text.rfind("}")
    if first_open != -1 and last_close != -1 and last_close > first_open:
        out.append(raw_text[first_open : last_close + 1])

    return out


def _extract_json_object(raw_text: str) -> Any:
    for candidate in _json_candidates(raw_text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _summarize_startup_snapshots(snapshots: list[FlightSnapshot]) -> dict[str, Any]:
    if not snapshots:
        return {
            "sample_count": 0,
            "hint": "No telemetry snapshots captured yet.",
        }

    ias = [s.indicated_airspeed_kt for s in snapshots if s.indicated_airspeed_kt is not None]
    gs = [s.groundspeed_m_s for s in snapshots if s.groundspeed_m_s is not None]
    vs = [s.vertical_speed_fpm for s in snapshots if s.vertical_speed_fpm is not None]
    on_ground_count = sum(1 for s in snapshots if s.on_ground)

    return {
        "sample_count": len(snapshots),
        "on_ground_fraction": float(on_ground_count) / float(len(snapshots)),
        "ias_min_kt": min(ias) if ias else 0.0,
        "ias_max_kt": max(ias) if ias else 0.0,
        "gs_max_m_s": max(gs) if gs else 0.0,
        "vs_min_fpm": min(vs) if vs else 0.0,
        "vs_max_fpm": max(vs) if vs else 0.0,
    }


def _default_hazard_profile_for_aircraft(
    *,
    aircraft_icao: str,
    aircraft_category: str,
) -> HazardProfile:
    profile = HazardProfile()
    thresholds = dict(profile.thresholds)
    notes: list[str] = []

    cat = aircraft_category.strip().lower()
    code = aircraft_icao.strip().upper()

    if cat == "turboprop":
        thresholds["low_airspeed_kt"] = 85.0
        thresholds["max_taxi_speed_kt"] = 25.0
        thresholds["unstable_approach_max_ias_kt"] = 130.0
        notes.append("Adjusted baseline thresholds for turboprop profile.")
    elif cat == "jet":
        thresholds["low_airspeed_kt"] = 130.0
        thresholds["max_taxi_speed_kt"] = 25.0
        thresholds["unstable_approach_max_ias_kt"] = 170.0
        notes.append("Adjusted baseline thresholds for jet profile.")
    elif code.startswith("B7") or code.startswith("A3"):
        thresholds["low_airspeed_kt"] = 130.0
        thresholds["unstable_approach_max_ias_kt"] = 170.0
        notes.append("Large transport ICAO pattern detected; raised low-speed/approach thresholds.")
    else:
        notes.append("Using GA baseline thresholds (C172-like) unless overridden.")

    return HazardProfile(
        enabled_rules=list(profile.enabled_rules),
        thresholds=thresholds,
        notes=notes,
    )


def _parse_hazard_profile(
    *,
    raw_hazard: Any,
    aircraft_icao: str,
    aircraft_category: str,
) -> HazardProfile:
    default_profile = _default_hazard_profile_for_aircraft(
        aircraft_icao=aircraft_icao,
        aircraft_category=aircraft_category,
    )

    if not isinstance(raw_hazard, dict):
        return default_profile

    allowed_rules = set(HazardProfile().enabled_rules)
    enabled_rules_raw = raw_hazard.get("enabled_rules", [])
    enabled_rules: list[str] = []
    if isinstance(enabled_rules_raw, list):
        for item in enabled_rules_raw:
            rule = str(item).strip()
            if rule in allowed_rules:
                enabled_rules.append(rule)
    if not enabled_rules:
        enabled_rules = list(default_profile.enabled_rules)

    thresholds = dict(default_profile.thresholds)
    thresholds_raw = raw_hazard.get("thresholds", {})
    if isinstance(thresholds_raw, dict):
        for key, value in thresholds_raw.items():
            if key not in thresholds:
                continue
            try:
                thresholds[key] = float(value)
            except (TypeError, ValueError):
                continue

    notes = list(default_profile.notes)
    notes_raw = raw_hazard.get("notes", [])
    if isinstance(notes_raw, list):
        parsed_notes: list[str] = []
        for item in notes_raw:
            text = str(item).strip()
            if text:
                parsed_notes.append(text)
        if parsed_notes:
            notes = parsed_notes[:8]

    return HazardProfile(
        enabled_rules=enabled_rules,
        thresholds=thresholds,
        notes=notes,
    )


def _coerce_summary_from_text(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return "No structured decision."
    first_line = text.splitlines()[0].strip()
    if first_line:
        return _truncate_text(first_line, max_chars=320)
    return "No structured decision."


def _coerce_feedback_items_from_text(raw_text: str) -> list[str]:
    text = " ".join(line.strip() for line in raw_text.splitlines() if line.strip())
    if not text:
        return ["Unable to parse master output."]

    segments = [segment.strip() for segment in re.split(r"[.;]", text) if segment.strip()]
    if not segments:
        return ["Unable to parse master output."]

    out: list[str] = []
    for seg in segments[:3]:
        out.append(_truncate_text(seg, max_chars=240))
    return out


def _infer_nonurgent_speech(summary: str, feedback_items: list[str]) -> tuple[bool, str]:
    candidates: list[str] = []
    if summary.strip():
        candidates.append(summary.strip())
    for item in feedback_items:
        text = item.strip()
        if text:
            candidates.append(text)

    combined = " ".join(candidates).strip()
    if not combined:
        return False, ""

    lowered = combined.lower()
    has_risk = _contains_keyword(lowered, _RISK_KEYWORDS)
    has_action = _contains_keyword(lowered, _ACTION_KEYWORDS)
    has_contrast = any(token in lowered for token in ("however", " but ", "although", "yet "))

    if not (has_risk or has_action or has_contrast):
        return False, ""

    if _contains_keyword(lowered, _POSITIVE_ONLY_KEYWORDS) and not (has_risk or has_action):
        return False, ""

    speak_text = _choose_spoken_text(candidates)
    if not speak_text:
        return False, ""
    return True, speak_text


def _choose_spoken_text(candidates: list[str]) -> str:
    segments: list[str] = []
    for text in candidates:
        parts = [part.strip() for part in re.split(r"[.;]", text) if part.strip()]
        segments.extend(parts)

    for segment in segments:
        cleaned = _clean_spoken_segment(segment)
        if cleaned and _contains_keyword(cleaned.lower(), _RISK_KEYWORDS):
            return cleaned

    for segment in segments:
        cleaned = _clean_spoken_segment(segment)
        if cleaned and _contains_keyword(cleaned.lower(), _ACTION_KEYWORDS):
            return cleaned

    for segment in segments:
        cleaned = _clean_spoken_segment(segment)
        if cleaned:
            return cleaned

    return ""


def _clean_spoken_segment(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return ""

    cleaned = re.sub(r"^[A-Za-z ]{1,32} review:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(however|but|and)\s*[:,\-]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    clauses = [part.strip() for part in cleaned.split(",") if part.strip()]
    if len(clauses) > 2:
        cleaned = ", ".join(clauses[:2])
    cleaned = _truncate_text(cleaned, max_chars=160)
    if not cleaned:
        return ""
    if cleaned[-1] not in ".!?":
        cleaned = f"{cleaned}."
    return cleaned


def _truncate_text(text: str, max_chars: int) -> str:
    value = " ".join(text.split()).strip()
    if not value:
        return ""
    if max_chars <= 0 or len(value) <= max_chars:
        return value

    head = value[: max_chars + 1]
    boundary = head.rfind(" ")
    if boundary >= int(max_chars * 0.6):
        head = head[:boundary]
    else:
        head = value[:max_chars]
    head = head.rstrip(" ,;:-")
    if not head:
        head = value[:max_chars].rstrip(" ,;:-")
    return f"{head}..."


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


_RISK_KEYWORDS: tuple[str, ...] = (
    "stall",
    "sink rate",
    "high sink",
    "hard touchdown",
    "unstable",
    "excessive",
    "pull up",
    "bank",
    "risk",
    "unsafe",
    "low altitude",
    "too fast",
    "too slow",
    "overspeed",
    "underspeed",
    "warning",
)

_ACTION_KEYWORDS: tuple[str, ...] = (
    "maintain",
    "reduce",
    "increase",
    "correct",
    "add power",
    "pitch",
    "flare",
    "aim",
    "hold",
    "keep",
    "use",
    "adjust",
    "monitor",
)

_POSITIVE_ONLY_KEYWORDS: tuple[str, ...] = (
    "within normal",
    "stable",
    "well above",
    "on profile",
    "no issue",
    "no significant",
)
