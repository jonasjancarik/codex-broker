from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any


_DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_DEFAULT_MODEL_ALIASES = {"gpt-5.6": "gpt-5.6-sol"}


class OpenAICompatAuthError(ValueError):
    pass


@dataclass(frozen=True)
class OpenAICompatBinding:
    owner_id: str
    auth_principal_id: str | None = None
    profile: str = "default"
    config_profile: str = "default"
    host_app: str = "openai-compatible-api"
    bundle_id: str | None = None
    cwd: str | None = None
    model_aliases: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_aliases", {**_DEFAULT_MODEL_ALIASES, **self.model_aliases})

    @classmethod
    def from_json(cls, value: Any) -> "OpenAICompatBinding":
        if not isinstance(value, dict):
            raise ValueError("Every OpenAI compatibility binding must be a JSON object.")
        allowed = {
            "ownerId",
            "authPrincipalId",
            "profile",
            "configProfile",
            "hostApp",
            "bundleId",
            "cwd",
            "modelAliases",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unknown OpenAI compatibility binding field: {sorted(unknown)[0]}")
        owner_id = _required_text(value, "ownerId")
        aliases = value.get("modelAliases") or {}
        if not isinstance(aliases, dict):
            raise ValueError("OpenAI compatibility modelAliases must be a JSON object.")
        normalized_aliases: dict[str, str] = {}
        for alias, model in aliases.items():
            if not isinstance(alias, str) or not alias.strip() or not isinstance(model, str) or not model.strip():
                raise ValueError("OpenAI compatibility model aliases and targets must be non-empty strings.")
            normalized_aliases[alias.strip()] = model.strip()
        return cls(
            owner_id=owner_id,
            auth_principal_id=_optional_text(value, "authPrincipalId"),
            profile=_optional_text(value, "profile") or "default",
            config_profile=_optional_text(value, "configProfile") or "default",
            host_app=_optional_text(value, "hostApp") or "openai-compatible-api",
            bundle_id=_optional_text(value, "bundleId"),
            cwd=_optional_text(value, "cwd"),
            model_aliases=normalized_aliases,
        )


class OpenAICompatAuth:
    def __init__(self, bindings: dict[str, Any] | None = None) -> None:
        self._bindings: list[tuple[str, OpenAICompatBinding]] = []
        for digest_key, value in (bindings or {}).items():
            if not isinstance(digest_key, str):
                raise ValueError("OpenAI compatibility binding digests must be strings.")
            match = _DIGEST_RE.fullmatch(digest_key)
            if not match:
                raise ValueError("OpenAI compatibility binding keys must use sha256:<64 lowercase hex characters>.")
            self._bindings.append((match.group(1), OpenAICompatBinding.from_json(value)))

    @property
    def configured(self) -> bool:
        return bool(self._bindings)

    def resolve_authorization(self, authorization: str | None) -> OpenAICompatBinding:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise OpenAICompatAuthError("Invalid API key.")
        key = authorization[len(prefix) :]
        if not key or key != key.strip():
            raise OpenAICompatAuthError("Invalid API key.")
        presented = hashlib.sha256(key.encode("utf-8")).hexdigest()
        found: OpenAICompatBinding | None = None
        for expected, binding in self._bindings:
            if hmac.compare_digest(presented, expected):
                found = binding
        if found is None:
            raise OpenAICompatAuthError("Invalid API key.")
        return found


def compatibility_key_digest(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("Compatibility key must be a non-empty string.")
    return f"sha256:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _required_text(value: dict[str, Any], key: str) -> str:
    text = _optional_text(value, key)
    if text is None:
        raise ValueError(f"OpenAI compatibility binding {key} must be a non-empty string.")
    return text


def _optional_text(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"OpenAI compatibility binding {key} must be a non-empty string.")
    return item.strip()
