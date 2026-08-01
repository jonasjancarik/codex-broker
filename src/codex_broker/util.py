from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .security import (
    BEARER_PATTERN,
    OPENAI_SECRET_PATTERN,
    QUOTED_SECRET_FIELD_PATTERN,
    SECRET_PATTERN,
    SENSITIVE_KEY_PATTERN as SECRET_KEY_PATTERN,
    redact_text,
    SecretSanitizer,
    redact_value,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def random_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18).replace('-', '').replace('_', '')[:24]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def redact(text: str, limit: int = 4000) -> str:
    return redact_text(text, limit)


def redact_json(value: Any, *, string_limit: int = 4000) -> Any:
    return redact_value(value, string_limit=string_limit)


def json_log(
    enabled: bool,
    event: str,
    *,
    sanitizer: SecretSanitizer | None = None,
    **fields: Any,
) -> None:
    if not enabled:
        return
    payload = {"ts": utc_now(), "event": event}
    payload.update(
        sanitizer.redact(fields)
        if sanitizer is not None
        else redact_json(fields, string_limit=1200)
    )
    sys.stderr.write(json_dumps(payload) + "\n")
    sys.stderr.flush()


def owner_digest(owner_id: str, secret: str | None = None) -> str:
    data = owner_id.encode("utf-8")
    if secret:
        return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def env_with(base: dict[str, str], updates: dict[str, str | None]) -> dict[str, str]:
    merged = dict(base)
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def clean_process_env(extra_allowed: tuple[str, ...] = ()) -> dict[str, str]:
    blocked_terms = ("TOKEN", "SECRET", "KEY", "PASSWORD")
    allowed = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SSL_CERT_FILE", "CODEX_CA_CERTIFICATE"}
    allowed.update(extra_allowed)
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in allowed or key.startswith("FAKE_CODEX"):
            result[key] = value
        elif any(term in key.upper() for term in blocked_terms):
            continue
    return result
