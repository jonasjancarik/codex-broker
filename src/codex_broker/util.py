from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|bearer)\b\s*[:=]\s*(?:Bearer\s+)?([^\s,;]+)"
)
QUOTED_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)([\"'])(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|bearer|password|secret|credential|cookie)\1"
    r"\s*:\s*([\"'])(?:Bearer\s+)?[^\"']+\3"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{4,})")
OPENAI_SECRET_PATTERN = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{3,}\b")
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|bearer|password|secret|credential|cookie)"
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
    clipped = text if len(text) <= limit else f"{text[:limit]}..."
    redacted = QUOTED_SECRET_FIELD_PATTERN.sub(lambda match: f"{match.group(2)}=<redacted>", clipped)
    redacted = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    redacted = BEARER_PATTERN.sub("Bearer <redacted>", redacted)
    return OPENAI_SECRET_PATTERN.sub("<redacted>", redacted)


def redact_json(value: Any, *, string_limit: int = 4000) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            redacted[key_text] = "<redacted>" if SECRET_KEY_PATTERN.search(key_text) else redact_json(item, string_limit=string_limit)
        return redacted
    if isinstance(value, list):
        return [redact_json(item, string_limit=string_limit) for item in value]
    if isinstance(value, tuple):
        return [redact_json(item, string_limit=string_limit) for item in value]
    if isinstance(value, str):
        return redact(value, string_limit)
    return value


def json_log(enabled: bool, event: str, **fields: Any) -> None:
    if not enabled:
        return
    payload = {"ts": utc_now(), "event": event}
    payload.update(redact_json(fields, string_limit=1200))
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
