from __future__ import annotations

import json
import os
import secrets
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__


def _paths(value: str | None, default: str) -> tuple[Path, ...]:
    raw = value if value is not None else default
    return tuple(Path(part).expanduser().resolve() for part in raw.split(":") if part)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None, default: str = "") -> tuple[str, ...]:
    raw = value if value is not None else default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _config_profiles() -> dict[str, dict[str, Any]]:
    raw = os.environ.get("CODEX_BROKER_CONFIG_PROFILES_JSON")
    path = os.environ.get("CODEX_BROKER_CONFIG_PROFILES_FILE")
    if not raw and path:
        profile_path = Path(path).expanduser()
        if not profile_path.is_file():
            raise FileNotFoundError(f"Configuration profiles file does not exist or is not a file: {profile_path}")
        raw = profile_path.read_text(encoding="utf-8")
        if not raw.strip():
            raise ValueError(f"Configuration profiles file is empty: {profile_path}")
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Configuration profiles must be a JSON object keyed by profile name.")
    profiles: dict[str, dict[str, Any]] = {}
    for name, value in parsed.items():
        if not isinstance(value, dict):
            raise ValueError(f"Configuration profile {name!r} must be a JSON object.")
        profiles[str(name)] = dict(value)
    return profiles


def _auth_principal_mappings() -> dict[str, str]:
    raw = os.environ.get("CODEX_BROKER_AUTH_PRINCIPAL_MAP_JSON")
    path = os.environ.get("CODEX_BROKER_AUTH_PRINCIPAL_MAP_FILE")
    if raw and path:
        raise ValueError(
            "Set only one of CODEX_BROKER_AUTH_PRINCIPAL_MAP_JSON and CODEX_BROKER_AUTH_PRINCIPAL_MAP_FILE."
        )
    if path:
        mapping_path = Path(path).expanduser()
        if not mapping_path.is_file():
            raise FileNotFoundError(f"Auth principal mapping file does not exist or is not a file: {mapping_path}")
        raw = mapping_path.read_text(encoding="utf-8")
        if not raw.strip():
            raise ValueError(f"Auth principal mapping file is empty: {mapping_path}")
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Auth principal mappings must be a JSON object keyed by ownerId.")
    mappings: dict[str, str] = {}
    for owner_id, principal_id in parsed.items():
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("Every auth principal mapping ownerId must be a non-empty string.")
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError(f"Auth principal mapping for {owner_id!r} must be a non-empty string.")
        mappings[owner_id] = principal_id
    return mappings


def _owner_hash_secret(data_dir: Path) -> str:
    explicit = os.environ.get("CODEX_BROKER_OWNER_HASH_KEY")
    explicit_file = os.environ.get("CODEX_BROKER_OWNER_HASH_KEY_FILE")
    if explicit and explicit_file:
        raise ValueError("Set only one of CODEX_BROKER_OWNER_HASH_KEY and CODEX_BROKER_OWNER_HASH_KEY_FILE.")
    if explicit:
        return explicit
    if explicit_file:
        path = Path(explicit_file).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Owner hash key file does not exist or is not a file: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"Owner hash key file is empty: {path}")
        return value
    path = data_dir / "state" / "owner-hash.key"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"Persistent owner hash key file is empty: {path}")
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    value = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    data_dir: Path
    internal_key: str | None
    allow_unauthenticated: bool
    owner_hash_secret: str | None
    allowed_workspace_roots: tuple[Path, ...]
    allowed_bundle_roots: tuple[Path, ...]
    max_active_turns: int
    pool_idle_ttl_seconds: int
    codex_command: tuple[str, ...]
    allowed_tool_commands: tuple[str, ...]
    allowed_hosted_tool_url_prefixes: tuple[str, ...]
    credential_store: str
    request_timeout_seconds: float
    host_response_timeout_seconds: float
    turn_timeout_seconds: float
    enable_inline_bundles: bool
    inline_bundle_max_bytes: int
    debug_raw_events: bool
    raw_event_retention_seconds: int
    json_logs: bool
    shutdown_mode: str
    shutdown_drain_timeout_seconds: float
    codex_passthrough_env: tuple[str, ...] = ()
    config_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    auth_principal_mappings: dict[str, str] = field(default_factory=dict)
    client_name: str = "codex_broker"
    client_title: str = "Codex Broker"
    client_version: str = __version__
    max_queued_turns: int = 1000
    hosted_tool_max_response_bytes: int = 1_048_576
    history_retention_seconds: int = 90 * 24 * 60 * 60
    max_events_per_turn: int = 10_000

    def __post_init__(self) -> None:
        if not self.codex_command:
            raise ValueError("Codex command must not be empty.")
        if self.max_active_turns < 0:
            raise ValueError("CODEX_BROKER_MAX_ACTIVE_TURNS must be zero or greater.")
        if self.max_queued_turns <= 0:
            raise ValueError("CODEX_BROKER_MAX_QUEUED_TURNS must be greater than zero.")
        if self.inline_bundle_max_bytes <= 0:
            raise ValueError("CODEX_BROKER_INLINE_BUNDLE_MAX_BYTES must be greater than zero.")
        if self.hosted_tool_max_response_bytes <= 0:
            raise ValueError("CODEX_BROKER_HOSTED_TOOL_MAX_RESPONSE_BYTES must be greater than zero.")
        if self.history_retention_seconds < 0:
            raise ValueError("CODEX_BROKER_HISTORY_RETENTION_SECONDS must be zero or greater.")
        if self.max_events_per_turn <= 0:
            raise ValueError("CODEX_BROKER_MAX_EVENTS_PER_TURN must be greater than zero.")
        for name, value in (
            ("CODEX_BROKER_REQUEST_TIMEOUT_SECONDS", self.request_timeout_seconds),
            ("CODEX_BROKER_HOST_RESPONSE_TIMEOUT_SECONDS", self.host_response_timeout_seconds),
            ("CODEX_BROKER_TURN_TIMEOUT_SECONDS", self.turn_timeout_seconds),
            ("CODEX_BROKER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS", self.shutdown_drain_timeout_seconds),
        ):
            if value < 0:
                raise ValueError(f"{name} must be zero or greater.")

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "state" / "broker.sqlite"

    @property
    def auth_root(self) -> Path:
        return self.data_dir / "auth" / "principals"

    @property
    def inline_bundle_root(self) -> Path:
        return self.data_dir / "bundles" / "inline"

    @property
    def overlay_root(self) -> Path:
        return self.data_dir / "workspaces" / "overlays"

    @classmethod
    def from_env(cls) -> "BrokerConfig":
        data_dir = Path(os.environ.get("CODEX_BROKER_DATA_DIR", ".data")).expanduser().resolve()
        key = os.environ.get("CODEX_BROKER_INTERNAL_KEY")
        key_file = os.environ.get("CODEX_BROKER_INTERNAL_KEY_FILE")
        if not key and key_file:
            path = Path(key_file).expanduser()
            if path.exists():
                key = path.read_text(encoding="utf-8").strip()
        codex_bin = os.environ.get("CODEX_BIN", "codex")
        return cls(
            host=os.environ.get("CODEX_BROKER_HOST", "127.0.0.1"),
            port=_int_env("CODEX_BROKER_PORT", 3400),
            data_dir=data_dir,
            internal_key=key or None,
            allow_unauthenticated=_bool_env("CODEX_BROKER_ALLOW_UNAUTHENTICATED", False),
            owner_hash_secret=_owner_hash_secret(data_dir),
            allowed_workspace_roots=_paths(os.environ.get("CODEX_BROKER_ALLOWED_WORKSPACE_ROOTS"), str(Path.cwd())),
            allowed_bundle_roots=_paths(os.environ.get("CODEX_BROKER_ALLOWED_BUNDLE_ROOTS"), str(Path.cwd())),
            max_active_turns=_int_env("CODEX_BROKER_MAX_ACTIVE_TURNS", 0),
            pool_idle_ttl_seconds=_int_env("CODEX_BROKER_POOL_IDLE_TTL_SECONDS", 900),
            codex_command=tuple(shlex.split(codex_bin)),
            allowed_tool_commands=tuple(
                item.strip()
                for item in os.environ.get("CODEX_BROKER_ALLOWED_TOOL_COMMANDS", "").split(",")
                if item.strip()
            ),
            allowed_hosted_tool_url_prefixes=_csv(
                os.environ.get("CODEX_BROKER_ALLOWED_HOSTED_TOOL_URL_PREFIXES"),
                "http://127.0.0.1,http://localhost,http://host.docker.internal",
            ),
            codex_passthrough_env=_csv(os.environ.get("CODEX_BROKER_PASSTHROUGH_ENV")),
            credential_store=os.environ.get("CODEX_CREDENTIAL_STORE", "file"),
            request_timeout_seconds=float(os.environ.get("CODEX_BROKER_REQUEST_TIMEOUT_SECONDS", "60")),
            host_response_timeout_seconds=float(os.environ.get("CODEX_BROKER_HOST_RESPONSE_TIMEOUT_SECONDS", "30")),
            turn_timeout_seconds=float(os.environ.get("CODEX_BROKER_TURN_TIMEOUT_SECONDS", "0")),
            enable_inline_bundles=_bool_env("CODEX_BROKER_ENABLE_INLINE_BUNDLES", False),
            inline_bundle_max_bytes=_int_env("CODEX_BROKER_INLINE_BUNDLE_MAX_BYTES", 262_144),
            debug_raw_events=_bool_env("CODEX_BROKER_DEBUG_RAW_EVENTS", False),
            raw_event_retention_seconds=_int_env("CODEX_BROKER_RAW_EVENT_RETENTION_SECONDS", 7 * 24 * 60 * 60),
            json_logs=_bool_env("CODEX_BROKER_JSON_LOGS", True),
            shutdown_mode=os.environ.get("CODEX_BROKER_SHUTDOWN_MODE", "interrupt"),
            shutdown_drain_timeout_seconds=float(os.environ.get("CODEX_BROKER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS", "30")),
            config_profiles=_config_profiles(),
            auth_principal_mappings=_auth_principal_mappings(),
            max_queued_turns=_int_env("CODEX_BROKER_MAX_QUEUED_TURNS", 1000),
            hosted_tool_max_response_bytes=_int_env(
                "CODEX_BROKER_HOSTED_TOOL_MAX_RESPONSE_BYTES",
                1_048_576,
            ),
            history_retention_seconds=_int_env("CODEX_BROKER_HISTORY_RETENTION_SECONDS", 90 * 24 * 60 * 60),
            max_events_per_turn=_int_env("CODEX_BROKER_MAX_EVENTS_PER_TURN", 10_000),
        )
