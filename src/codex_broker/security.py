"""Pure secret sanitization used for broker events and diagnostics."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from typing import Any


REDACTED = "<redacted>"

# These expressions deliberately recognize labelled credentials, rather than
# attempting to classify arbitrary high-entropy strings as secrets.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|identity[_-]?token|"
    r"authorization|bearer|password|secret|credential|cookie)"
)
SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|identity[_-]?token|"
    r"authorization|bearer|password|secret|credential|cookie)\b\s*[:=]\s*(?:Bearer\s+)?([^\s,;]+)"
)
QUOTED_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)([\"'])(api[_-]?key|access[_-]?token|refresh[_-]?token|identity[_-]?token|"
    r"authorization|bearer|password|secret|credential|cookie)\1"
    r"\s*:\s*([\"'])(?:Bearer\s+)?[^\"']+\3"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{4,})")
OPENAI_SECRET_PATTERN = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{3,}\b")

_LABEL_AT_END_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|identity[_-]?token|"
    r"authorization|bearer|password|secret|credential|cookie)\b\s*[:=].*$"
)
_BEARER_AT_END_PATTERN = re.compile(r"(?i)\bBearer\s+.*$")
_OPENAI_AT_END_PATTERN = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]*$")
_QUOTED_KEY_AT_END_PATTERN = re.compile(
    r"(?i)(?:[\"'])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|identity[_-]?token|"
    r"authorization|bearer|password|secret|credential|cookie)(?:[\"'])\s*:\s*(?:[\"']).*$"
)
_STREAM_MARKERS = (
    "bearer ",
    "api_key=",
    "api-key=",
    "api_key:",
    "api-key:",
    "access_token=",
    "access-token=",
    "access_token:",
    "access-token:",
    "refresh_token=",
    "refresh-token=",
    "refresh_token:",
    "refresh-token:",
    "identity_token=",
    "identity-token=",
    "identity_token:",
    "identity-token:",
    "sk-",
)


def redact_text(text: str, limit: int = 4000) -> str:
    """Redact credential-shaped text for logs and other mandatory-safe sinks."""
    clipped = text if len(text) <= limit else f"{text[:limit]}..."
    redacted = QUOTED_SECRET_FIELD_PATTERN.sub(lambda match: f"{match.group(2)}={REDACTED}", clipped)
    redacted = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}={REDACTED}", redacted)
    redacted = BEARER_PATTERN.sub(f"Bearer {REDACTED}", redacted)
    return OPENAI_SECRET_PATTERN.sub(REDACTED, redacted)


def redact_value(value: Any, *, string_limit: int = 4000) -> Any:
    """Recursively redact values for mandatory-safe diagnostic output."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if SENSITIVE_KEY_PATTERN.search(str(key)) else redact_value(item, string_limit=string_limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, string_limit=string_limit) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, string_limit=string_limit) for item in value)
    if isinstance(value, str):
        return redact_text(value, string_limit)
    return value


class SecretSanitizer:
    """Sanitize normalized payloads, with scoped registration of exact secrets."""

    def __init__(self, mode: str = "safe") -> None:
        if mode not in {"safe", "raw"}:
            raise ValueError("event sanitization mode must be 'safe' or 'raw'")
        self.mode = mode
        self._lock = threading.RLock()
        self._scopes: dict[str, set[str]] = {}
        self._replacement_count = 0

    @property
    def replacement_count(self) -> int:
        with self._lock:
            return self._replacement_count

    def register(self, scope: str, values: Iterable[str] | str) -> None:
        """Add non-empty exact values to a registration scope."""
        entries = self._values(values)
        with self._lock:
            self._scopes.setdefault(scope, set()).update(entries)

    def replace_scope(self, scope: str, values: Iterable[str] | str) -> None:
        """Atomically refresh all values associated with one scope."""
        entries = self._values(values)
        with self._lock:
            if entries:
                self._scopes[scope] = entries
            else:
                self._scopes.pop(scope, None)

    def remove_scope(self, scope: str) -> None:
        with self._lock:
            self._scopes.pop(scope, None)

    def sanitize(self, value: Any) -> Any:
        """Sanitize an event payload unless this deployment is explicitly raw."""
        if self.mode == "raw":
            return value
        return self.redact(value)

    def redact(self, value: Any) -> Any:
        """Always sanitize, including when rendering logs in raw event mode."""
        with self._lock:
            exact_values = self._exact_values_locked()
        return self._redact_value(value, exact_values)

    def sanitize_text(self, text: str) -> str:
        return text if self.mode == "raw" else self.redact_text(text)

    def redact_text(self, text: str) -> str:
        with self._lock:
            exact_values = self._exact_values_locked()
        return self._redact_text(text, exact_values)

    @staticmethod
    def _values(values: Iterable[str] | str) -> set[str]:
        source = (values,) if isinstance(values, str) else values
        return {value for value in source if isinstance(value, str) and value}

    def _exact_values_locked(self) -> tuple[str, ...]:
        return tuple(sorted({value for entries in self._scopes.values() for value in entries}, key=len, reverse=True))

    def _redact_text(self, text: str, exact_values: tuple[str, ...]) -> str:
        replacements = 0
        for value in exact_values:
            occurrences = text.count(value)
            if occurrences:
                text = text.replace(value, REDACTED)
                replacements += occurrences

        def replace(pattern: re.Pattern[str], replacement: Any) -> None:
            nonlocal text, replacements

            def apply(match: re.Match[str]) -> str:
                nonlocal replacements
                rendered = replacement(match) if callable(replacement) else replacement
                if rendered != match.group(0):
                    replacements += 1
                return rendered

            text = pattern.sub(apply, text)

        replace(QUOTED_SECRET_FIELD_PATTERN, lambda match: f"{match.group(2)}={REDACTED}")
        replace(SECRET_PATTERN, lambda match: f"{match.group(1)}={REDACTED}")
        replace(BEARER_PATTERN, f"Bearer {REDACTED}")
        replace(OPENAI_SECRET_PATTERN, REDACTED)
        if replacements:
            with self._lock:
                self._replacement_count += replacements
        return text

    def _redact_value(self, value: Any, exact_values: tuple[str, ...]) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            replacements = 0
            for key, item in value.items():
                if SENSITIVE_KEY_PATTERN.search(str(key)):
                    redacted[str(key)] = REDACTED
                    replacements += int(item != REDACTED)
                else:
                    redacted[str(key)] = self._redact_value(item, exact_values)
            if replacements:
                with self._lock:
                    self._replacement_count += replacements
            return redacted
        if isinstance(value, list):
            return [self._redact_value(item, exact_values) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, exact_values) for item in value)
        if isinstance(value, str):
            return self._redact_text(value, exact_values)
        return value

    def _stream_hold_start(self, text: str) -> int:
        """Return the start of a suffix that may grow into a credential."""
        with self._lock:
            values = self._exact_values_locked()
        starts = [len(text)]
        for value in values:
            max_size = min(len(value), len(text))
            for size in range(max_size, 0, -1):
                if text.endswith(value[:size]):
                    starts.append(len(text) - size)
                    break
        lowered = text.lower()
        for marker in _STREAM_MARKERS:
            max_size = min(len(marker), len(text))
            for size in range(max_size, 0, -1):
                if lowered.endswith(marker[:size]):
                    starts.append(len(text) - size)
                    break
        for pattern in (_LABEL_AT_END_PATTERN, _BEARER_AT_END_PATTERN, _OPENAI_AT_END_PATTERN, _QUOTED_KEY_AT_END_PATTERN):
            match = pattern.search(text)
            if match:
                starts.append(match.start())
        return min(starts)


class StreamingSecretSanitizer:
    """Incrementally sanitize text streams without sharing suffixes between items."""

    def __init__(self, sanitizer: SecretSanitizer) -> None:
        self.sanitizer = sanitizer
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str, str], str] = {}

    def sanitize_delta(self, turn_id: str, item_id: str, stream_type: str, text: str) -> str:
        if self.sanitizer.mode == "raw":
            return text
        key = (turn_id, item_id, stream_type)
        with self._lock:
            pending = self._pending.get(key, "") + text
            hold_start = self.sanitizer._stream_hold_start(pending)
            emitted, self._pending[key] = pending[:hold_start], pending[hold_start:]
        return self.sanitizer.sanitize_text(emitted)

    sanitize = sanitize_delta

    def flush(self, turn_id: str, item_id: str, stream_type: str) -> str:
        if self.sanitizer.mode == "raw":
            return ""
        key = (turn_id, item_id, stream_type)
        with self._lock:
            pending = self._pending.pop(key, "")
        return self.sanitizer.sanitize_text(pending)

    def flush_item(self, turn_id: str, item_id: str) -> dict[str, str]:
        return self._flush_matching(lambda key: key[0] == turn_id and key[1] == item_id)

    def flush_turn(self, turn_id: str) -> dict[tuple[str, str], str]:
        return self._flush_matching(lambda key: key[0] == turn_id, keyed=True)

    def interrupt(self, turn_id: str) -> dict[tuple[str, str], str]:
        return self.flush_turn(turn_id)

    def fail(self, turn_id: str) -> dict[tuple[str, str], str]:
        return self.flush_turn(turn_id)

    def _flush_matching(self, predicate: Any, *, keyed: bool = False) -> Any:
        if self.sanitizer.mode == "raw":
            return {}
        with self._lock:
            matched = [(key, self._pending.pop(key)) for key in list(self._pending) if predicate(key)]
        if keyed:
            return {(key[1], key[2]): self.sanitizer.sanitize_text(value) for key, value in matched}
        return {key[2]: self.sanitizer.sanitize_text(value) for key, value in matched}
