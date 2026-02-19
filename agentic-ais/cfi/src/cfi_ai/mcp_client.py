from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client

from cfi_ai.types import SpeechSink


class XPlaneMCPClient:
    def __init__(self, sse_url: str) -> None:
        self._sse_url = sse_url
        self._sse_cm: Any | None = None
        self._session_cm: Any | None = None
        self._session: ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._sse_cm = sse_client(self._sse_url)
        read_stream, write_stream = await self._sse_cm.__aenter__()
        self._session_cm = ClientSession(read_stream, write_stream)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

    async def close(self) -> None:
        if self._session_cm is not None:
            with suppress(Exception):
                await self._session_cm.__aexit__(None, None, None)
        if self._sse_cm is not None:
            with suppress(Exception):
                await self._sse_cm.__aexit__(None, None, None)
        self._session = None
        self._session_cm = None
        self._sse_cm = None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("MCP session is not connected.")

        args = arguments or {}
        async with self._session_lock:
            result = await self._session.call_tool(name, arguments=args)
        return _decode_tool_result(result)

    async def speak(self, message: str) -> dict[str, Any]:
        return await self.call_tool("xplm_speak_string", {"message": message})

    async def command_execute(
        self,
        name: str,
        *,
        action: str = "once",
        create_if_missing: bool = False,
        description: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "name": name,
            "action": action,
            "create_if_missing": create_if_missing,
        }
        if description:
            args["description"] = description
        return await self.call_tool("xplm_command_execute", args)


class McpSpeechSink(SpeechSink):
    def __init__(
        self,
        *,
        mcp_client: XPlaneMCPClient,
        urgent_cooldown_sec: float,
        nonurgent_cooldown_sec: float,
        dry_run: bool = False,
    ) -> None:
        self._mcp = mcp_client
        self._urgent_cooldown_sec = urgent_cooldown_sec
        self._nonurgent_cooldown_sec = nonurgent_cooldown_sec
        self._dry_run = dry_run

        self._last_urgent_by_key: dict[str, float] = {}
        self._last_nonurgent_epoch = 0.0
        self._last_urgent_epoch = 0.0

    async def start(self) -> None:
        await self._mcp.connect()

    async def stop(self) -> None:
        await self._mcp.close()

    async def speak_urgent(self, text: str, key: str) -> bool:
        now = time.time()
        last = self._last_urgent_by_key.get(key, 0.0)
        if now - last < self._urgent_cooldown_sec:
            return False

        if self._dry_run:
            self._last_urgent_by_key[key] = now
            self._last_urgent_epoch = now
            return True

        result = await self._mcp.speak(text)
        ok = bool(result.get("success", False))
        if ok:
            self._last_urgent_by_key[key] = now
            self._last_urgent_epoch = now
        return ok

    async def speak_nonurgent(self, text: str) -> bool:
        now = time.time()
        if now - self._last_nonurgent_epoch < self._nonurgent_cooldown_sec:
            return False

        if self._dry_run:
            self._last_nonurgent_epoch = now
            return True

        result = await self._mcp.speak(text)
        ok = bool(result.get("success", False))
        if ok:
            self._last_nonurgent_epoch = now
        return ok

    def recent_urgent(self, within_sec: float) -> bool:
        return (time.time() - self._last_urgent_epoch) <= max(0.0, within_sec)


def _decode_tool_result(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")

    if not content:
        return {}

    for item in content:
        text = _extract_text(item)
        if not text:
            continue
        parsed = _try_parse_json(text)
        if parsed is not None:
            return parsed
        return {"text": text}

    return {"content": [str(c) for c in content]}


def _extract_text(item: Any) -> str | None:
    if isinstance(item, dict):
        text = item.get("text")
        if isinstance(text, str):
            return text
        return None

    text_attr = getattr(item, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    return None


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
        return {"value": value}
    except json.JSONDecodeError:
        return None
