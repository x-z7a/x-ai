from __future__ import annotations

from typing import Any

from cfi_ai.xplane_mcp import XPlaneMCPClient


class CfiTools:
    def __init__(self, mcp_client: XPlaneMCPClient, speak_enabled: bool) -> None:
        self._mcp = mcp_client
        self._speak_enabled = speak_enabled

    async def fetch_aircraft_state(self) -> dict[str, Any]:
        return await self._mcp.fetch_aircraft_state()

    async def dataref_info(self, name: str) -> dict[str, Any]:
        return await self._mcp.dataref_info(name)

    async def dataref_get(
        self,
        name: str,
        *,
        mode: str = "auto",
        offset: int | None = None,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        return await self._mcp.read_dataref(
            name=name,
            mode=mode,
            offset=offset,
            max_items=max_items,
        )

    async def speak(self, message: str, *, force: bool = False) -> dict[str, Any]:
        if not (self._speak_enabled or force):
            return {
                "success": False,
                "skipped": True,
                "reason": "CFI_SPEAK_ENABLED=false",
                "message": message,
            }
        return await self._mcp.speak(message)
