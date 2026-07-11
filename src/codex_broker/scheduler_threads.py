from __future__ import annotations

from typing import Any, Protocol

from .identity import AuthScope
from .scheduler_errors import ConflictError, NotFoundError


class ThreadScheduler(Protocol):
    auth: Any
    state: Any
    bundles: Any

    def _request_config_profile(self, body: dict[str, Any], fallback: Any = "default") -> str: ...

    def _config_profile_config(self, config_profile: str) -> dict[str, Any]: ...

    def _validate_config_profile_bundle(self, profile_config: dict[str, Any], bundle_id: str | None) -> None: ...

    def _validate_config_profile_cwd(self, cwd: Any, profile_config: dict[str, Any]) -> None: ...

    def _public_thread(self, thread: dict[str, Any]) -> dict[str, Any]: ...

    def _gate(self, owner_hash: str, thread_id: str) -> Any: ...


def optional_selector(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def create_thread(scheduler: ThreadScheduler, owner_id: str, body: dict[str, Any]) -> dict[str, Any]:
    scope = scheduler.auth.resolve_scope(owner_id, optional_selector(body, "authPrincipalId"))
    profile = scheduler.auth.profile_key(optional_selector(body, "profile") or "default")
    if "productThreadId" in body:
        raise ValueError("productThreadId has been removed; pass threadId instead.")
    requested_thread_id = _optional_text(body.get("threadId"))
    if requested_thread_id:
        with scheduler._gate(scope.owner_hash, requested_thread_id).binding_lock:
            return _create_thread_locked(
                scheduler,
                scope,
                profile,
                body,
                requested_thread_id,
            )
    return _create_thread_locked(scheduler, scope, profile, body, None)


def _create_thread_locked(
    scheduler: ThreadScheduler,
    scope: AuthScope,
    profile: str,
    body: dict[str, Any],
    requested_thread_id: str | None,
) -> dict[str, Any]:
    config_profile = scheduler._request_config_profile(body)
    config_profile_config = scheduler._config_profile_config(config_profile)
    host_app = _optional_text(body.get("hostApp"))
    if requested_thread_id:
        existing = scheduler.state.get_thread(scope.owner_hash, requested_thread_id)
        if existing:
            validate_thread_auth_binding(scheduler, scope, existing, profile)
            return scheduler._public_thread(existing)
    bundle_id = str(body["bundleId"]) if body.get("bundleId") else None
    scheduler._validate_config_profile_bundle(config_profile_config, bundle_id)
    bundle = scheduler.bundles.resolve(bundle_id) if bundle_id else None
    cwd = scheduler.bundles.validate_cwd(body.get("cwd"), bundle)
    scheduler._validate_config_profile_cwd(cwd, config_profile_config)
    with scheduler.auth.profile_guard(scope.auth_principal_hash, profile):
        scheduler.auth.profile_home(scope.auth_principal_hash, profile)
        profile_row = scheduler.state.get_profile(scope.auth_principal_hash, profile)
        if not profile_row:
            raise ConflictError("Auth profile disappeared while creating the broker thread.")
        thread = scheduler.state.create_thread(
            scope.owner_hash,
            thread_id=requested_thread_id,
            auth_principal_hash=scope.auth_principal_hash,
            auth_profile_instance_id=str(profile_row["instance_id"]),
            profile=profile,
            config_profile=config_profile,
            host_app=host_app,
            bundle_id=str(bundle_id) if bundle_id else None,
            cwd=str(cwd) if cwd else None,
        )
        validate_thread_auth_binding(scheduler, scope, thread, profile)
    return scheduler._public_thread(thread)


def get_thread(scheduler: ThreadScheduler, owner_id: str, thread_id: str) -> dict[str, Any]:
    owner_hash = scheduler.auth.hash_owner(owner_id)
    thread = scheduler.state.get_thread(owner_hash, thread_id)
    if not thread:
        raise NotFoundError("Thread not found.")
    return scheduler._public_thread(thread)


def archive_thread(scheduler: ThreadScheduler, owner_id: str, thread_id: str) -> dict[str, Any]:
    owner_hash = scheduler.auth.hash_owner(owner_id)
    thread = scheduler.state.archive_thread(owner_hash, thread_id)
    if not thread:
        raise NotFoundError("Thread not found.")
    return scheduler._public_thread(thread)


def validate_turn_auth_binding(
    scheduler: ThreadScheduler,
    owner_id: str,
    thread: dict[str, Any],
    body: dict[str, Any],
) -> AuthScope:
    scope = scheduler.auth.resolve_scope(owner_id, optional_selector(body, "authPrincipalId"))
    requested_profile = (
        scheduler.auth.profile_key(optional_selector(body, "profile"))
        if body.get("profile") is not None
        else str(thread["profile"])
    )
    validate_thread_auth_binding(scheduler, scope, thread, requested_profile)
    return scope


def validate_thread_auth_binding(
    scheduler: ThreadScheduler,
    scope: AuthScope,
    thread: dict[str, Any],
    requested_profile: str,
) -> None:
    thread_id = str(thread["thread_id"])
    if thread.get("auth_principal_hash") != scope.auth_principal_hash:
        raise ConflictError(
            f"Broker thread {thread_id!r} is bound to a different auth principal. Start a new broker thread."
        )
    if thread.get("profile") != requested_profile:
        raise ConflictError(
            f"Broker thread {thread_id!r} is bound to auth profile {thread['profile']!r}. "
            f"Start a new broker thread to use auth profile {requested_profile!r}."
        )
    profile_row = scheduler.state.get_profile(scope.auth_principal_hash, requested_profile)
    if not profile_row or profile_row.get("instance_id") != thread.get("auth_profile_instance_id"):
        raise ConflictError(
            f"The Codex account in auth profile {requested_profile!r} was removed or replaced. "
            "Start a new broker thread."
        )


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def public_thread(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "threadId": thread["thread_id"],
        "codexThreadId": thread.get("codex_thread_id"),
        "authPrincipalHash": thread["auth_principal_hash"],
        "profile": thread["profile"],
        "configProfile": thread["config_profile"],
        "hostApp": thread.get("host_app"),
        "bundleId": thread.get("bundle_id"),
        "cwd": thread.get("cwd"),
        "status": thread["status"],
        "createdAt": thread["created_at"],
        "updatedAt": thread["updated_at"],
    }


def public_turn(turn: dict[str, Any]) -> dict[str, Any]:
    return {
        "threadId": turn["thread_id"],
        "turnId": turn["turn_id"],
        "codexTurnId": turn.get("codex_turn_id"),
        "authPrincipalHash": turn["auth_principal_hash"],
        "profile": turn["profile"],
        "configProfile": turn["config_profile"],
        "hostApp": turn.get("host_app"),
        "bundleId": turn.get("bundle_id"),
        "cwd": turn.get("cwd"),
        "mode": turn["mode"],
        "productCorrelationId": turn.get("product_correlation_id"),
        "status": turn["status"],
        "error": turn.get("error"),
        "errorCode": turn.get("error_code"),
        "publicMessage": turn.get("public_message"),
        "adminMessage": turn.get("admin_message"),
        "createdAt": turn["created_at"],
        "startedAt": turn.get("started_at"),
        "completedAt": turn.get("completed_at"),
        "updatedAt": turn["updated_at"],
        "execution": {
            "requestFingerprint": turn.get("request_fingerprint"),
            "bundleDigest": turn.get("bundle_digest"),
            "resolvedOptions": turn.get("resolved_options"),
            "brokerVersion": turn.get("broker_version"),
        },
    }
