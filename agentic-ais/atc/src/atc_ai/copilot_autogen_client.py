from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from typing import Any, AsyncGenerator, Literal, Mapping, Optional, Sequence
from uuid import uuid4

from autogen_core import CancellationToken, FunctionCall
from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    LLMMessage,
    ModelCapabilities,
    ModelFamily,
    ModelInfo,
    RequestUsage,
)
from autogen_core.models._types import (
    AssistantMessage,
    FunctionExecutionResultMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import Tool, ToolSchema
from pydantic import BaseModel

from copilot import CopilotClient

from atc_ai.copilot_auth import (
    build_copilot_client_options,
    copilot_auth_error_message,
    is_copilot_auth_error,
)


class CopilotAutoGenClient(ChatCompletionClient):
    """AutoGen ChatCompletionClient wrapper backed by GitHub Copilot SDK sessions."""

    def __init__(
        self,
        *,
        model: str,
        github_token: str,
        use_logged_in_user: bool,
        use_custom_provider: bool,
        provider_base_url: str,
        provider_bearer_token: str,
        context_window_tokens: int = 128000,
    ) -> None:
        self._model = _normalize_model_name(model)
        self._use_custom_provider = use_custom_provider
        self._provider_base_url = provider_base_url
        self._provider_bearer_token = provider_bearer_token
        self._context_window_tokens = context_window_tokens

        self._client = CopilotClient(build_copilot_client_options(github_token, use_logged_in_user))
        self._use_logged_in_user = use_logged_in_user
        self._started = False
        self._actual_usage = RequestUsage(prompt_tokens=0, completion_tokens=0)
        self._total_usage = RequestUsage(prompt_tokens=0, completion_tokens=0)

        self._model_info: ModelInfo = {
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "family": ModelFamily.GPT_41,
        }

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: Optional[bool | type[BaseModel]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[CancellationToken] = None,
    ) -> CreateResult:
        del cancellation_token
        await self._ensure_started()

        tool_schemas = [self._to_tool_schema(t) for t in tools]
        prompt = self._build_prompt(
            messages=messages,
            tools=tool_schemas,
            tool_choice=tool_choice,
            json_output=json_output,
        )

        session_config: dict[str, Any] = {
            "model": self._model,
            "streaming": False,
        }
        if self._use_custom_provider:
            session_config["provider"] = {
                "type": "openai",
                "base_url": self._provider_base_url,
                "bearer_token": self._provider_bearer_token,
            }
        session = await self._client.create_session(session_config)

        try:
            timeout = float(extra_create_args.get("timeout", 120))
            event = await session.send_and_wait({"prompt": prompt}, timeout=timeout)
            raw = self._extract_event_content(event)
            if not raw:
                raw = await self._last_assistant_message(session)
        finally:
            await session.destroy()

        usage = self._estimate_usage(prompt, raw)
        self._accumulate_usage(usage)

        parse = self._parse_response(raw, tool_schemas)
        if (
            parse is None
            and tool_schemas
            and isinstance(tool_choice, str)
            and tool_choice == "required"
        ):
            first_tool = tool_schemas[0].get("name", "")
            if isinstance(first_tool, str) and first_tool:
                parse = {
                    "kind": "tool_calls",
                    "tool_calls": [
                        FunctionCall(
                            id=f"copilot-{uuid4().hex}",
                            name=first_tool,
                            arguments="{}",
                        )
                    ],
                    "thought": "Tool call required by policy.",
                }

        if parse is None:
            return CreateResult(
                finish_reason="stop",
                content=raw.strip() or "No response.",
                usage=usage,
                cached=False,
            )

        if parse["kind"] == "tool_calls":
            return CreateResult(
                finish_reason="function_calls",
                content=parse["tool_calls"],
                usage=usage,
                cached=False,
                thought=parse.get("thought"),
            )

        return CreateResult(
            finish_reason="stop",
            content=parse.get("content", raw).strip(),
            usage=usage,
            cached=False,
            thought=parse.get("thought"),
        )

    def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: Optional[bool | type[BaseModel]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[CancellationToken] = None,
    ) -> AsyncGenerator[str | CreateResult, None]:
        async def _generator() -> AsyncGenerator[str | CreateResult, None]:
            result = await self.create(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args,
                cancellation_token=cancellation_token,
            )
            yield result

        return _generator()

    async def close(self) -> None:
        if self._started:
            await self._client.stop()
            self._started = False

    def actual_usage(self) -> RequestUsage:
        return self._actual_usage

    def total_usage(self) -> RequestUsage:
        return self._total_usage

    def count_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        prompt = self._build_prompt(messages=messages, tools=[self._to_tool_schema(t) for t in tools], tool_choice="auto")
        return max(1, math.ceil(len(prompt) / 4))

    def remaining_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return max(0, self._context_window_tokens - self.count_tokens(messages, tools=tools))

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            vision=self._model_info["vision"],
            function_calling=self._model_info["function_calling"],
            json_output=self._model_info["json_output"],
        )

    @property
    def model_info(self) -> ModelInfo:
        return self._model_info

    async def _ensure_started(self) -> None:
        if not self._started:
            try:
                await self._client.start()
                self._started = True
            except Exception as exc:  # noqa: BLE001
                if is_copilot_auth_error(exc):
                    raise RuntimeError(copilot_auth_error_message(self._use_logged_in_user)) from exc
                raise

    @staticmethod
    def _to_tool_schema(tool: Tool | ToolSchema) -> ToolSchema:
        if hasattr(tool, "schema"):
            return tool.schema  # type: ignore[return-value]
        return tool

    @staticmethod
    def _extract_event_content(event: Any) -> str:
        if event is None:
            return ""
        data = getattr(event, "data", None)
        if data is None:
            return ""
        content = getattr(data, "content", None)
        if isinstance(content, str):
            return content
        return ""

    async def _last_assistant_message(self, session: Any) -> str:
        messages = await session.get_messages()
        for message in reversed(messages):
            event_type = getattr(getattr(message, "type", None), "value", "")
            if event_type == "assistant.message":
                content = getattr(getattr(message, "data", None), "content", None)
                if isinstance(content, str):
                    return content
        return ""

    def _build_prompt(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSchema],
        tool_choice: Tool | Literal["auto", "required", "none"],
        json_output: Optional[bool | type[BaseModel]],
    ) -> str:
        transcript = self._render_messages(messages)
        tools_block = self._render_tools(tools)
        tool_choice_text = self._render_tool_choice(tool_choice)

        output_contract = (
            "Respond in strict JSON only (no markdown). "
            "If you want to call a tool, return:\n"
            '{"type":"tool_call","name":"<tool_name>","arguments":{...},"thought":"optional short reason"}\n'
            "If you need multiple tool calls, return:\n"
            '{"type":"tool_calls","calls":[{"name":"<tool_name>","arguments":{...}}]}\n'
            "If no tool is needed, return:\n"
            '{"type":"final","content":"...","thought":"optional"}'
        )

        json_mode = self._render_json_output_requirement(json_output)
        return (
            "You are a model backend for an AutoGen agent.\n\n"
            f"{tool_choice_text}\n\n"
            f"{json_mode}\n\n"
            f"{output_contract}\n\n"
            "Available tools:\n"
            f"{tools_block}\n\n"
            "Conversation transcript:\n"
            f"{transcript}\n"
        )

    @staticmethod
    def _render_tool_choice(tool_choice: Tool | Literal["auto", "required", "none"]) -> str:
        if isinstance(tool_choice, str):
            if tool_choice == "required":
                return "Tool call is required for this turn."
            if tool_choice == "none":
                return "Do not call tools on this turn."
            return "Call tools only when needed."
        name = getattr(tool_choice, "name", None)
        if isinstance(name, str) and name:
            return f"You must call this tool: {name}"
        return "Call tools only when needed."

    @staticmethod
    def _render_json_output_requirement(json_output: Optional[bool | type[BaseModel]]) -> str:
        if json_output is None:
            return "Return concise output."
        if isinstance(json_output, bool):
            if json_output:
                return "Final output content must be valid JSON in the `content` field."
            return "Final output content can be plain text."
        schema = json.dumps(json_output.model_json_schema(), indent=2)
        return (
            "When returning type=final, `content` must be a JSON string matching this schema:\n"
            f"{schema}"
        )

    @staticmethod
    def _render_tools(tools: Sequence[ToolSchema]) -> str:
        if not tools:
            return "(none)"
        lines: list[str] = []
        for tool in tools:
            lines.append(
                json.dumps(
                    {
                        "name": tool.get("name"),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                    ensure_ascii=True,
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _render_messages(messages: Sequence[LLMMessage]) -> str:
        out: list[str] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                out.append(f"[SYSTEM] {message.content}")
            elif isinstance(message, UserMessage):
                out.append(f"[USER:{message.source}] {CopilotAutoGenClient._stringify_content(message.content)}")
            elif isinstance(message, AssistantMessage):
                if isinstance(message.content, str):
                    out.append(f"[ASSISTANT:{message.source}] {message.content}")
                else:
                    calls = [asdict(call) for call in message.content]
                    out.append(f"[ASSISTANT_TOOL_CALLS:{message.source}] {json.dumps(calls)}")
            elif isinstance(message, FunctionExecutionResultMessage):
                results = [
                    {
                        "name": result.name,
                        "call_id": result.call_id,
                        "is_error": result.is_error,
                        "content": result.content,
                    }
                    for result in message.content
                ]
                out.append(f"[TOOL_RESULTS] {json.dumps(results)}")
            else:
                out.append(f"[MESSAGE] {str(message)}")
        return "\n".join(out)

    @staticmethod
    def _stringify_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        return str(content)

    def _parse_response(self, raw_text: str, tools: Sequence[ToolSchema]) -> dict[str, Any] | None:
        parsed = self._extract_json_object(raw_text)
        if not isinstance(parsed, dict):
            return None

        tool_names = {t.get("name") for t in tools}
        kind = parsed.get("type")
        if kind == "tool_call":
            name = str(parsed.get("name", "")).strip()
            if name and (not tool_names or name in tool_names):
                args = parsed.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                return {
                    "kind": "tool_calls",
                    "tool_calls": [
                        FunctionCall(
                            id=f"copilot-{uuid4().hex}",
                            name=name,
                            arguments=json.dumps(args, ensure_ascii=True),
                        )
                    ],
                    "thought": _string_or_none(parsed.get("thought")),
                }

        if kind == "tool_calls" and isinstance(parsed.get("calls"), list):
            calls: list[FunctionCall] = []
            for call in parsed["calls"]:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name", "")).strip()
                if not name or (tool_names and name not in tool_names):
                    continue
                args = call.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                calls.append(
                    FunctionCall(
                        id=f"copilot-{uuid4().hex}",
                        name=name,
                        arguments=json.dumps(args, ensure_ascii=True),
                    )
                )
            if calls:
                return {
                    "kind": "tool_calls",
                    "tool_calls": calls,
                    "thought": _string_or_none(parsed.get("thought")),
                }

        if kind == "final":
            return {
                "kind": "final",
                "content": str(parsed.get("content", "")),
                "thought": _string_or_none(parsed.get("thought")),
            }

        return None

    @staticmethod
    def _extract_json_object(raw_text: str) -> Any:
        raw_text = raw_text.strip()
        if not raw_text:
            return None

        for candidate in _json_candidates(raw_text):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _estimate_usage(prompt: str, completion: str) -> RequestUsage:
        return RequestUsage(
            prompt_tokens=max(1, math.ceil(len(prompt) / 4)),
            completion_tokens=max(1, math.ceil(len(completion) / 4)),
        )

    def _accumulate_usage(self, usage: RequestUsage) -> None:
        self._actual_usage = RequestUsage(
            prompt_tokens=self._actual_usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self._actual_usage.completion_tokens + usage.completion_tokens,
        )
        self._total_usage = RequestUsage(
            prompt_tokens=self._total_usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self._total_usage.completion_tokens + usage.completion_tokens,
        )


def _json_candidates(raw_text: str) -> list[str]:
    candidates: list[str] = [raw_text]
    fence_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw_text, flags=re.IGNORECASE)
    candidates.extend(fence_matches)

    first_open = raw_text.find("{")
    last_close = raw_text.rfind("}")
    if first_open != -1 and last_close != -1 and last_close > first_open:
        candidates.append(raw_text[first_open : last_close + 1])

    first_open_list = raw_text.find("[")
    last_close_list = raw_text.rfind("]")
    if first_open_list != -1 and last_close_list != -1 and last_close_list > first_open_list:
        candidates.append(raw_text[first_open_list : last_close_list + 1])
    return candidates


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _normalize_model_name(model: str) -> str:
    if model.startswith("openai/"):
        return model.split("/", 1)[1]
    return model
