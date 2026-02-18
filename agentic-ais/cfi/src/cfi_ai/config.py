from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class CfiConfig:
    xplane_mcp_sse_url: str
    github_token: str
    copilot_use_logged_in_user: bool
    copilot_use_custom_provider: bool
    autogen_model: str
    autogen_model_fallbacks: Tuple[str, ...]
    autogen_base_url: str
    autogen_api_key: str
    monitor_poll_sec: float
    speak_enabled: bool
    alert_policy: str
    engine_shutdown_hold_sec: float
    ai_rule_eval_interval_sec: float
    plane_memory_path: str
    max_flight_hours: float

    @staticmethod
    def from_env() -> "CfiConfig":
        load_dotenv()
        cfi_root = Path(__file__).resolve().parents[2]
        load_dotenv(cfi_root / ".env")

        github_token = os.getenv("GITHUB_TOKEN", "").strip()
        autogen_api_key = os.getenv("AUTOGEN_API_KEY", "").strip() or github_token
        autogen_fallbacks_raw = os.getenv(
            "AUTOGEN_MODEL_FALLBACKS",
            "openai/gpt-4.1-mini,openai/gpt-4o-mini",
        ).strip()
        autogen_fallbacks = tuple(
            model.strip()
            for model in autogen_fallbacks_raw.split(",")
            if model.strip()
        )

        return CfiConfig(
            xplane_mcp_sse_url=os.getenv("XPLANE_MCP_SSE_URL", "http://127.0.0.1:8765/sse").strip(),
            github_token=github_token,
            copilot_use_logged_in_user=_bool_env("COPILOT_USE_LOGGED_IN_USER", default=not bool(github_token)),
            copilot_use_custom_provider=_bool_env("COPILOT_USE_CUSTOM_PROVIDER", default=False),
            autogen_model=os.getenv("AUTOGEN_MODEL", "openai/gpt-4.1-mini").strip(),
            autogen_model_fallbacks=autogen_fallbacks,
            autogen_base_url=os.getenv("AUTOGEN_BASE_URL", "https://models.github.ai/inference").strip(),
            autogen_api_key=autogen_api_key,
            monitor_poll_sec=_float_env("CFI_MONITOR_POLL_SEC", 1.0),
            speak_enabled=_bool_env("CFI_SPEAK_ENABLED", default=True),
            alert_policy=os.getenv("CFI_ALERT_POLICY", "all").strip().lower() or "all",
            engine_shutdown_hold_sec=_float_env("CFI_ENGINE_SHUTDOWN_HOLD_SEC", 15.0),
            ai_rule_eval_interval_sec=_float_env("CFI_AI_RULE_EVAL_INTERVAL_SEC", 4.0),
            plane_memory_path=os.getenv("CFI_PLANE_MEMORY_PATH", "plane_memory.json").strip() or "plane_memory.json",
            max_flight_hours=_float_env("CFI_MAX_FLIGHT_HOURS", 6.0),
        )

    def validate(self) -> None:
        if not self.github_token and not self.copilot_use_logged_in_user:
            raise ValueError(
                "Set GITHUB_TOKEN or enable COPILOT_USE_LOGGED_IN_USER=true (and run copilot login)."
            )
        if not self.xplane_mcp_sse_url:
            raise ValueError("XPLANE_MCP_SSE_URL is required.")
        if not self.autogen_model:
            raise ValueError("AUTOGEN_MODEL is required.")
        if self.copilot_use_custom_provider:
            if not self.autogen_base_url:
                raise ValueError("AUTOGEN_BASE_URL is required when COPILOT_USE_CUSTOM_PROVIDER=true.")
            if not self.autogen_api_key:
                raise ValueError(
                    "AUTOGEN_API_KEY (or GITHUB_TOKEN fallback) is required when COPILOT_USE_CUSTOM_PROVIDER=true."
                )
        if self.monitor_poll_sec <= 0:
            raise ValueError("CFI_MONITOR_POLL_SEC must be > 0.")
        if self.engine_shutdown_hold_sec <= 0:
            raise ValueError("CFI_ENGINE_SHUTDOWN_HOLD_SEC must be > 0.")
        if self.ai_rule_eval_interval_sec <= 0:
            raise ValueError("CFI_AI_RULE_EVAL_INTERVAL_SEC must be > 0.")
        if not self.plane_memory_path:
            raise ValueError("CFI_PLANE_MEMORY_PATH must not be empty.")
        if self.max_flight_hours <= 0:
            raise ValueError("CFI_MAX_FLIGHT_HOURS must be > 0.")
        if self.alert_policy != "all":
            raise ValueError("CFI_ALERT_POLICY currently supports only 'all'.")
