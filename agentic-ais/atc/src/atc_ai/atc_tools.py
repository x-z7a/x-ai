from __future__ import annotations

import json
import time

from atc_ai.copilot_experts import CopilotExperts
from atc_ai.xplane_mcp import XPlaneMCPClient


class AtcTools:
    def __init__(
        self,
        mcp_client: XPlaneMCPClient,
        experts: CopilotExperts,
        auto_transmit: bool,
    ) -> None:
        self._mcp = mcp_client
        self._experts = experts
        self._auto_transmit = auto_transmit
        self._last_transmitted_message = ""
        self._last_transmitted_at_epoch = 0.0

    async def fetch_aircraft_state(self) -> str:
        """Read key aircraft/nav/radio state from X-Plane via MCP."""
        state = await self._mcp.fetch_aircraft_state()
        return json.dumps(state, indent=2, sort_keys=True)

    async def ask_phraseology_expert(self, question: str, context_json: str) -> str:
        """Ask Copilot phraseology expert for a concise ATC transmission."""
        return await self._experts.ask_phraseology(question, context_json)

    async def ask_flow_expert(self, question: str, context_json: str) -> str:
        """Ask Copilot flow expert for sequencing/runway strategy."""
        return await self._experts.ask_flow(question, context_json)

    async def ask_airport_expert(self, question: str, context_json: str) -> str:
        """Ask Copilot airport expert for local airport/chart/nav/weather/ATIS context."""
        return await self._experts.ask_airport(question, context_json)

    async def transmit_radio(self, message: str, confirm: bool = False) -> str:
        """Speak ATC instruction in simulator via `xplm_speak_string`."""
        if not (self._auto_transmit or confirm):
            return (
                "Transmission skipped. Set confirm=true for this call or "
                "ATC_AUTO_TRANSMIT=true in env."
            )

        result = await self._mcp.speak(message)
        ok = result.get("success", False)
        if ok:
            self._last_transmitted_message = message
            self._last_transmitted_at_epoch = time.time()
            return f"Transmitted via X-Plane: {message}"
        return f"X-Plane transmission attempted but result was unexpected: {result}"

    def last_transmitted(self) -> tuple[str, float]:
        return self._last_transmitted_message, self._last_transmitted_at_epoch
