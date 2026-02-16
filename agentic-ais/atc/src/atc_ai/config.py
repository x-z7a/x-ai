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


@dataclass(frozen=True)
class AtcConfig:
    xplane_mcp_sse_url: str
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
    auto_transmit: bool

    @staticmethod
    def from_env() -> "AtcConfig":
        load_dotenv()
        atc_root = Path(__file__).resolve().parents[2]
        load_dotenv(atc_root / ".env")

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

        return AtcConfig(
            xplane_mcp_sse_url=os.getenv("XPLANE_MCP_SSE_URL", "http://127.0.0.1:8765/sse").strip(),
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
            auto_transmit=_bool_env("ATC_AUTO_TRANSMIT", default=False),
        )

    def validate(self) -> None:
        if not self.github_token and not self.copilot_use_logged_in_user:
            raise ValueError(
                "Set GITHUB_TOKEN or enable COPILOT_USE_LOGGED_IN_USER=true (and run copilot login)."
            )
        if self.copilot_use_custom_provider:
            if not self.copilot_base_url:
                raise ValueError("COPILOT_BASE_URL is required when COPILOT_USE_CUSTOM_PROVIDER=true.")
            if not self.copilot_bearer_token:
                raise ValueError(
                    "COPILOT_BEARER_TOKEN (or GITHUB_TOKEN fallback) is required when COPILOT_USE_CUSTOM_PROVIDER=true."
                )
            if not self.autogen_api_key:
                raise ValueError(
                    "AUTOGEN_API_KEY (or GITHUB_TOKEN fallback) is required when COPILOT_USE_CUSTOM_PROVIDER=true."
                )
        if not self.xplane_mcp_sse_url:
            raise ValueError("XPLANE_MCP_SSE_URL is required.")
        if not self.autogen_model:
            raise ValueError("AUTOGEN_MODEL is required.")
        if self.copilot_use_custom_provider and not self.autogen_base_url:
            raise ValueError("AUTOGEN_BASE_URL is required.")
