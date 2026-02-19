from __future__ import annotations

from typing import Any, Protocol, Sequence

from autogen_core.memory import Memory


class MemoryProvider(Protocol):
    def attach_to_agent(self, *, name: str) -> Sequence[Memory] | None: ...

    async def record_event(self, event_type: str, payload: dict[str, Any]) -> None: ...

    async def query_context(self, query: str, limit: int = 5) -> list[str]: ...


class KnowledgeProvider(Protocol):
    async def retrieve(self, query: str, k: int = 5) -> list[str]: ...
