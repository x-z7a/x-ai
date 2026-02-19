from __future__ import annotations

import json
from typing import Any, Sequence

from autogen_core.memory import ListMemory, Memory, MemoryContent, MemoryMimeType

from cfi_ai.memory.base import MemoryProvider


class NullMemoryProvider(MemoryProvider):
    def attach_to_agent(self, *, name: str) -> Sequence[Memory] | None:
        return None

    async def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        del event_type, payload

    async def query_context(self, query: str, limit: int = 5) -> list[str]:
        del query, limit
        return []


class ListBackedMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._memory = ListMemory(name="cfi_event_memory")

    def attach_to_agent(self, *, name: str) -> Sequence[Memory] | None:
        del name
        return [self._memory]

    async def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event_doc = {
            "event_type": event_type,
            "payload": payload,
        }
        await self._memory.add(
            MemoryContent(
                content=json.dumps(event_doc, ensure_ascii=True),
                mime_type=MemoryMimeType.TEXT,
            )
        )

    async def query_context(self, query: str, limit: int = 5) -> list[str]:
        result = await self._memory.query(query)
        out: list[str] = []
        for item in result.results[: max(0, limit)]:
            out.append(str(item.content))
        return out


def create_memory_provider(name: str) -> MemoryProvider:
    normalized = name.strip().lower()
    if normalized == "list":
        return ListBackedMemoryProvider()
    return NullMemoryProvider()
