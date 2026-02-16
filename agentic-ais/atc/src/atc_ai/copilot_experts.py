from __future__ import annotations

from contextlib import suppress

from copilot import CopilotClient
from copilot.session import CopilotSession

from atc_ai.copilot_auth import (
    build_copilot_client_options,
    copilot_auth_error_message,
    is_copilot_auth_error,
)


PHRASEOLOGY_SYSTEM = (
    "You are an FAA/ICAO ATC phraseology expert for simulator operations. "
    "Output concise, unambiguous phraseology with callsign, runway, altitude, "
    "heading, speed, and readback-critical items when relevant. "
    "Radio frequency needs space, so '123.45' should be 'one two three point four five'."
)

FLOW_SYSTEM = (
    "You are an ATC traffic flow manager. Prioritize sequencing, runway efficiency, "
    "conflict minimization, and smooth arrival/departure throughput."
)

AIRPORT_SYSTEM = (
    "You are an airport operations expert for simulator ATC. "
    "Given airport/context data, provide concise operational guidance on "
    "runway usage, nav procedures, chart/transition considerations, weather impact, "
    "and ATIS-style info assumptions/unknowns."
)


class CopilotExperts:
    def __init__(
        self,
        github_token: str,
        use_logged_in_user: bool,
        model: str,
        use_custom_provider: bool,
        provider_base_url: str,
        provider_bearer_token: str,
    ) -> None:
        self._client = CopilotClient(
            build_copilot_client_options(github_token, use_logged_in_user)
        )
        self._use_logged_in_user = use_logged_in_user
        self._model = _normalize_model_name(model)
        self._use_custom_provider = use_custom_provider
        self._provider_base_url = provider_base_url
        self._provider_bearer_token = provider_bearer_token
        self._sessions: dict[str, CopilotSession] = {}

    async def start(self) -> None:
        try:
            await self._client.start()
            self._sessions["phraseology"] = await self._create_session(
                PHRASEOLOGY_SYSTEM
            )
            self._sessions["flow"] = await self._create_session(FLOW_SYSTEM)
            self._sessions["airport"] = await self._create_session(AIRPORT_SYSTEM)
        except Exception as exc:  # noqa: BLE001
            if is_copilot_auth_error(exc):
                raise RuntimeError(
                    copilot_auth_error_message(self._use_logged_in_user)
                ) from exc
            raise

    async def stop(self) -> None:
        for session in self._sessions.values():
            with suppress(BaseException):
                await session.destroy()
        self._sessions.clear()
        with suppress(BaseException):
            await self._client.stop()

    async def ask_phraseology(self, prompt: str, context: str) -> str:
        return await self._ask("phraseology", prompt, context)

    async def ask_flow(self, prompt: str, context: str) -> str:
        return await self._ask("flow", prompt, context)

    async def ask_airport(self, prompt: str, context: str) -> str:
        return await self._ask("airport", prompt, context)

    async def _create_session(self, system_prompt: str) -> CopilotSession:
        session_config = {
            "model": self._model,
            "streaming": False,
            "system_message": {"mode": "append", "content": system_prompt},
        }
        if self._use_custom_provider:
            session_config["provider"] = {
                "type": "openai",
                "base_url": self._provider_base_url,
                "bearer_token": self._provider_bearer_token,
            }
        return await self._client.create_session(session_config)

    async def _ask(self, role: str, prompt: str, context: str) -> str:
        session = self._sessions.get(role)
        if session is None:
            raise RuntimeError("CopilotExperts is not started.")

        event = await session.send_and_wait(
            {
                "prompt": (
                    "Context:\n"
                    f"{context}\n\n"
                    "Task:\n"
                    f"{prompt}\n\n"
                    "Respond with operationally useful guidance only."
                )
            },
            timeout=90,
        )

        if event is not None and getattr(event, "data", None) is not None:
            content = getattr(event.data, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()

        messages = await session.get_messages()
        for message in reversed(messages):
            if message.type.value == "assistant.message":
                content = getattr(message.data, "content", None)
                if isinstance(content, str) and content.strip():
                    return content.strip()

        return "No expert response."


def _normalize_model_name(model: str) -> str:
    if model.startswith("openai/"):
        return model.split("/", 1)[1]
    return model
