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


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw.strip())


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())


@dataclass(frozen=True)
class CfiConfig:
    xplane_udp_host: str
    xplane_udp_port: int
    xplane_udp_local_port: int
    xplane_rref_hz: int
    xplane_retry_sec: float
    xplane_start_max_retries: int
    startup_bootstrap_wait_sec: float

    xplane_mcp_sse_url: str
    enable_mcp_commands: bool

    github_token: str
    copilot_use_logged_in_user: bool
    copilot_use_custom_provider: bool
    copilot_model: str
    copilot_base_url: str
    copilot_bearer_token: str

    autogen_model: str
    autogen_model_fallbacks: Tuple[str, ...]
    autogen_base_url: str
    autogen_api_key: str

    review_window_sec: float
    review_tick_sec: float
    urgent_cooldown_sec: float
    nonurgent_cooldown_sec: float
    nonurgent_suppress_after_urgent_sec: float
    shutdown_detect_dwell_sec: float
    hazard_phrase_refresh_sec: float
    hazard_phrase_runtime_enabled: bool

    memory_backend: str
    telemetry_enabled: bool

    team_chat_log_path: str
    runtime_events_log_path: str
    telemetry_log_path: str

    @staticmethod
    def from_env() -> "CfiConfig":
        load_dotenv()
        cfi_root = Path(__file__).resolve().parents[2]
        load_dotenv(cfi_root / ".env")

        github_token = os.getenv("GITHUB_TOKEN", "").strip()
        autogen_api_key = os.getenv("AUTOGEN_API_KEY", "").strip() or github_token
        copilot_bearer_token = os.getenv("COPILOT_BEARER_TOKEN", "").strip() or github_token

        autogen_fallbacks_raw = os.getenv(
            "AUTOGEN_MODEL_FALLBACKS",
            "openai/gpt-4.1-mini,openai/gpt-4o-mini",
        ).strip()
        autogen_fallbacks = tuple(
            model.strip()
            for model in autogen_fallbacks_raw.split(",")
            if model.strip()
        )

        team_chat_log_path = (
            os.getenv("CFI_TEAM_CHAT_LOG_PATH", "").strip()
            or str(cfi_root / "team.chat.log.jsonl")
        )
        runtime_events_log_path = (
            os.getenv("CFI_RUNTIME_EVENTS_LOG_PATH", "").strip()
            or str(cfi_root / "runtime.events.log.jsonl")
        )
        telemetry_log_path = (
            os.getenv("CFI_TELEMETRY_LOG_PATH", "").strip()
            or str(cfi_root / "telemetry.log.jsonl")
        )

        return CfiConfig(
            xplane_udp_host=os.getenv("XPLANE_UDP_HOST", "127.0.0.1").strip(),
            xplane_udp_port=_int_env("XPLANE_UDP_PORT", 49000),
            xplane_udp_local_port=_int_env("XPLANE_UDP_LOCAL_PORT", 49001),
            xplane_rref_hz=_int_env("XPLANE_RREF_HZ", 10),
            xplane_retry_sec=_float_env("XPLANE_RETRY_SEC", 3.0),
            xplane_start_max_retries=_int_env("XPLANE_START_MAX_RETRIES", 0),
            startup_bootstrap_wait_sec=_float_env("CFI_STARTUP_BOOTSTRAP_WAIT_SEC", 8.0),
            xplane_mcp_sse_url=os.getenv("XPLANE_MCP_SSE_URL", "http://127.0.0.1:8765/sse").strip(),
            enable_mcp_commands=_bool_env("CFI_ENABLE_MCP_COMMANDS", default=False),
            github_token=github_token,
            copilot_use_logged_in_user=_bool_env("COPILOT_USE_LOGGED_IN_USER", default=not bool(github_token)),
            copilot_use_custom_provider=_bool_env("COPILOT_USE_CUSTOM_PROVIDER", default=False),
            copilot_model=os.getenv("COPILOT_MODEL", "gpt-4o-mini").strip(),
            copilot_base_url=os.getenv("COPILOT_BASE_URL", "https://models.github.ai/inference").strip(),
            copilot_bearer_token=copilot_bearer_token,
            autogen_model=os.getenv("AUTOGEN_MODEL", "openai/gpt-4.1-mini").strip(),
            autogen_model_fallbacks=autogen_fallbacks,
            autogen_base_url=os.getenv("AUTOGEN_BASE_URL", "https://models.github.ai/inference").strip(),
            autogen_api_key=autogen_api_key,
            review_window_sec=_float_env("CFI_REVIEW_WINDOW_SEC", 30.0),
            review_tick_sec=_float_env("CFI_REVIEW_TICK_SEC", 10.0),
            urgent_cooldown_sec=_float_env("CFI_URGENT_COOLDOWN_SEC", 8.0),
            nonurgent_cooldown_sec=_float_env("CFI_NONURGENT_COOLDOWN_SEC", 45.0),
            nonurgent_suppress_after_urgent_sec=_float_env("CFI_NONURGENT_SUPPRESS_AFTER_URGENT_SEC", 12.0),
            shutdown_detect_dwell_sec=_float_env("CFI_SHUTDOWN_DETECT_DWELL_SEC", 8.0),
            hazard_phrase_refresh_sec=_float_env("CFI_HAZARD_PHRASE_REFRESH_SEC", 90.0),
            hazard_phrase_runtime_enabled=_bool_env("CFI_HAZARD_PHRASE_RUNTIME_ENABLED", default=True),
            memory_backend=os.getenv("CFI_MEMORY_BACKEND", "none").strip().lower(),
            telemetry_enabled=_bool_env("CFI_TELEMETRY_ENABLED", default=False),
            team_chat_log_path=team_chat_log_path,
            runtime_events_log_path=runtime_events_log_path,
            telemetry_log_path=telemetry_log_path,
        )

    def validate(self) -> None:
        if not self.xplane_udp_host:
            raise ValueError("XPLANE_UDP_HOST is required.")
        if self.xplane_udp_port <= 0:
            raise ValueError("XPLANE_UDP_PORT must be > 0.")
        if self.xplane_udp_local_port <= 0:
            raise ValueError("XPLANE_UDP_LOCAL_PORT must be > 0.")
        if self.xplane_rref_hz <= 0:
            raise ValueError("XPLANE_RREF_HZ must be > 0.")
        if self.xplane_retry_sec <= 0:
            raise ValueError("XPLANE_RETRY_SEC must be > 0.")
        if self.xplane_start_max_retries < 0:
            raise ValueError("XPLANE_START_MAX_RETRIES must be >= 0.")
        if self.startup_bootstrap_wait_sec < 0:
            raise ValueError("CFI_STARTUP_BOOTSTRAP_WAIT_SEC must be >= 0.")
        if not self.xplane_mcp_sse_url:
            raise ValueError("XPLANE_MCP_SSE_URL is required.")
        if not self.autogen_model:
            raise ValueError("AUTOGEN_MODEL is required.")
        if self.review_window_sec <= 0 or self.review_tick_sec <= 0:
            raise ValueError("CFI_REVIEW_WINDOW_SEC and CFI_REVIEW_TICK_SEC must be > 0.")
        if self.shutdown_detect_dwell_sec <= 0:
            raise ValueError("CFI_SHUTDOWN_DETECT_DWELL_SEC must be > 0.")
        if self.hazard_phrase_refresh_sec <= 0:
            raise ValueError("CFI_HAZARD_PHRASE_REFRESH_SEC must be > 0.")
        if self.memory_backend not in {"none", "list"}:
            raise ValueError("CFI_MEMORY_BACKEND must be one of: none, list")
        if self.copilot_use_custom_provider:
            if not self.copilot_base_url:
                raise ValueError("COPILOT_BASE_URL is required when COPILOT_USE_CUSTOM_PROVIDER=true.")
            if not self.copilot_bearer_token:
                raise ValueError("COPILOT_BEARER_TOKEN (or GITHUB_TOKEN fallback) is required.")
            if not self.autogen_api_key:
                raise ValueError("AUTOGEN_API_KEY (or GITHUB_TOKEN fallback) is required.")
