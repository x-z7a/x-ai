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
from cfi_ai.types import FlightPhase, ReviewWindow, TeamDecision


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

    async def run_review(self, review: ReviewWindow) -> TeamDecision:
        if self._team is None:
            raise RuntimeError("CfiAgentTeam is not started.")

        phase_agent = self._phase_to_agent_name[review.phase]
        self._active_expert_name = phase_agent

        task = self._build_task(review)
        result = await self._team.run(task=task)
        await self._team.reset()

        self._write_team_log(review, result.messages)

        master_content = self._extract_master_output(result.messages)
        return self.parse_decision(master_content, review.phase)

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
            return TeamDecision(
                phase=phase,
                summary="No structured decision.",
                feedback_items=["Unable to parse master output; no spoken coaching emitted."],
                speak_now=False,
                speak_text="",
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

        speak_now = bool(parsed.get("speak_now", False))
        speak_text = str(parsed.get("speak_text", "")).strip()
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

    def _build_task(self, review: ReviewWindow) -> str:
        payload = {
            "review_window": asdict(review),
            "instructions": {
                "turn_policy": [
                    "Phase expert provides stage-specific analysis.",
                    "Master CFI provides final JSON decision.",
                ],
                "output": "Master must return strict JSON only.",
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
