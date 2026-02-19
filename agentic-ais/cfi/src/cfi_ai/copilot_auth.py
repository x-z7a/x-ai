from __future__ import annotations

from typing import Any


def build_copilot_client_options(github_token: str, use_logged_in_user: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "use_logged_in_user": use_logged_in_user,
    }
    if github_token and not use_logged_in_user:
        options["github_token"] = github_token
    return options


def is_copilot_auth_error(exc: Exception | str) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc)
    lowered = text.lower()
    return (
        "authorization error" in lowered
        or "/login" in lowered
        or "not authenticated" in lowered
        or "secitemcopymatching failed" in lowered
        or "timed out" in lowered
        or "auth" in lowered and "copilot" in lowered
    )


def copilot_auth_error_message(use_logged_in_user: bool) -> str:
    if use_logged_in_user:
        return (
            "Copilot authentication/startup failed. Run `copilot login` (or `/login` in the Copilot CLI), "
            "verify `copilot --version` works, then retry."
        )
    return (
        "Copilot authentication/startup failed with GITHUB_TOKEN. Verify token access and local Copilot CLI health "
        "(`copilot --version`), or set COPILOT_USE_LOGGED_IN_USER=true and run `copilot login`."
    )
