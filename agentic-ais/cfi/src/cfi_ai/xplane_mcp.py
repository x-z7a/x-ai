from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client


DEFAULT_CFI_DATAREFS: dict[str, str] = {
    "latitude": "sim/flightmodel/position/latitude",
    "longitude": "sim/flightmodel/position/longitude",
    "elevation_m": "sim/flightmodel/position/elevation",
    "groundspeed_m_s": "sim/flightmodel/position/groundspeed",
    "indicated_airspeed_kt": "sim/cockpit2/gauges/indicators/airspeed_kts_pilot",
    "heading_true_deg": "sim/flightmodel/position/true_psi",
    "vertical_speed_fpm": "sim/flightmodel/position/vh_ind_fpm",
    "on_ground": "sim/flightmodel/failures/onground_any",
}


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
        retry = 0
        while True:
            try:
                async with self._session_lock:
                    result = await self._session.call_tool(name, arguments=args)
                return _decode_tool_result(result)
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling() > 0:
                    raise
                retry += 1
                await asyncio.sleep(_retry_delay(retry))
                continue
            except Exception as exc:  # noqa: BLE001
                if not _is_retryable_mcp_error(exc):
                    raise
                retry += 1
                await asyncio.sleep(_retry_delay(retry))
                continue

    async def dataref_info(self, name: str) -> dict[str, Any]:
        return await self.call_tool("xplm_dataref_info", {"name": name})

    async def read_dataref(
        self,
        name: str,
        *,
        mode: str = "auto",
        offset: int | None = None,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"name": name, "mode": mode}
        if offset is not None:
            arguments["offset"] = offset
        if max_items is not None:
            arguments["max"] = max_items
        return await self.call_tool("xplm_dataref_get", arguments)

    async def speak(self, message: str) -> dict[str, Any]:
        return await self.call_tool("xplm_speak_string", {"message": message})

    async def fetch_aircraft_state(self) -> dict[str, Any]:
        try:
            return await self.call_tool("fetch_aircraft_state", {})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

        async def _fetch_one(label: str, dataref_name: str) -> tuple[str, Any]:
            try:
                result = await self.read_dataref(dataref_name, mode="auto")
                return label, result.get("value")
            except Exception as exc:  # noqa: BLE001
                return label, {"error": str(exc), "dataref": dataref_name}

        tasks = [
            _fetch_one(label, dataref_name)
            for label, dataref_name in DEFAULT_CFI_DATAREFS.items()
        ]
        pairs = await asyncio.gather(*tasks)
        state = dict(pairs)

        with suppress(Exception):
            state["gps_destination"] = await self.call_tool("xplm_gps_destination", {})
        with suppress(Exception):
            state["runtime"] = await self.call_tool("xplm_get_runtime_info", {})

        return state


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


def _is_retryable_mcp_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if "invalid_params" in text or "dataref not found" in text:
        return False
    retry_markers = (
        "cancel",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection",
        "transport",
        "broken pipe",
        "eof",
    )
    return any(marker in text for marker in retry_markers)


def _retry_delay(retry: int) -> float:
    return min(0.2 * retry, 2.0)
