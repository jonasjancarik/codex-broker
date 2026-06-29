from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .util import json_dumps


CODEX_AUTH_REQUIRES_ADMIN = "codex_auth_requires_admin"
CODEX_AUTH_REQUIRES_ADMIN_PUBLIC_MESSAGE = (
    "Codex authentication needs administrator attention. Please wait until an "
    "administrator refreshes the shared Codex session."
)
SESSION_NOT_RESUMABLE = "session_not_resumable"
SESSION_NOT_RESUMABLE_PUBLIC_MESSAGE = (
    "The previous Codex session could not be resumed. Start a new session from the current workspace state."
)


@dataclass(frozen=True)
class RuntimeErrorInfo:
    code: str
    public_message: str
    admin_message: str

    def public_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.public_message,
            "publicMessage": self.public_message,
            "adminMessage": self.admin_message,
        }


def render_app_server_error(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        parts = [str(error.get("message") or "").strip()]
        details = str(error.get("additionalDetails") or "").strip()
        if details:
            parts.append(details)
        rendered = "\n\n".join(part for part in parts if part)
        return rendered or json_dumps(error)
    return str(error)


def classify_runtime_error(message: str) -> RuntimeErrorInfo:
    normalized = " ".join(message.lower().split())
    if "no rollout found for thread id" in normalized:
        return RuntimeErrorInfo(
            code=SESSION_NOT_RESUMABLE,
            public_message=SESSION_NOT_RESUMABLE_PUBLIC_MESSAGE,
            admin_message=message,
        )
    if (
        "access token could not be refreshed" in normalized
        and "refresh token was already used" in normalized
    ):
        return RuntimeErrorInfo(
            code=CODEX_AUTH_REQUIRES_ADMIN,
            public_message=CODEX_AUTH_REQUIRES_ADMIN_PUBLIC_MESSAGE,
            admin_message=message,
        )
    return RuntimeErrorInfo(code="codex_runtime_error", public_message=message, admin_message=message)


def classify_app_server_error(error: Any) -> RuntimeErrorInfo | None:
    message = render_app_server_error(error)
    return classify_runtime_error(message) if message else None
