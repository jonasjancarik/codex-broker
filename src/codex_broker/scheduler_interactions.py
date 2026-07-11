from __future__ import annotations

from typing import Any

from .interactions import InteractionResponseError, validate_host_response
from .scheduler_errors import ConflictError, NotFoundError


def list_interactions(
    scheduler: Any,
    owner_id: str,
    thread_id: str,
    *,
    turn_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    owner_hash = scheduler.auth.hash_owner(owner_id)
    if not scheduler.state.get_thread(owner_hash, thread_id):
        raise NotFoundError("Thread not found.")
    if turn_id and not scheduler.state.get_turn(owner_hash, thread_id, turn_id):
        raise NotFoundError("Turn not found.")
    interactions = scheduler.state.list_interactions(owner_hash, thread_id, turn_id=turn_id, status=status, limit=limit)
    return {"interactions": [public_interaction(interaction) for interaction in interactions]}


def get_interaction(scheduler: Any, owner_id: str, thread_id: str, turn_id: str, interaction_id: str) -> dict[str, Any]:
    owner_hash = scheduler.auth.hash_owner(owner_id)
    interaction = get_thread_turn_interaction(scheduler, owner_hash, thread_id, turn_id, interaction_id)
    return public_interaction(interaction)


def resolve_interaction(
    scheduler: Any,
    owner_id: str,
    thread_id: str,
    turn_id: str,
    interaction_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    owner_hash = scheduler.auth.hash_owner(owner_id)
    interaction = get_thread_turn_interaction(scheduler, owner_hash, thread_id, turn_id, interaction_id)
    if interaction["status"] != "pending":
        raise ConflictError("interaction_already_resolved")
    try:
        response = validate_host_response(str(interaction["method"]), body)
    except InteractionResponseError as exc:
        raise ValueError(str(exc)) from exc
    active = scheduler._active_context(owner_hash, thread_id)
    if not active or active.turn_id != turn_id or not active.client:
        raise ConflictError("interaction_not_active")
    resolved = active.client.resolve_pending_interaction(interaction_id, response, source="host")
    if not resolved:
        raise ConflictError("interaction_not_active")
    scheduler.state.append_audit(
        owner_hash,
        "interaction.resolved_by_host",
        {"interactionId": interaction_id, "method": interaction["method"], "response": response},
        auth_principal_hash=active.auth_principal_hash,
        thread_id=thread_id,
        turn_id=turn_id,
    )
    return public_interaction(resolved)


def get_thread_turn_interaction(
    scheduler: Any,
    owner_hash: str,
    thread_id: str,
    turn_id: str,
    interaction_id: str,
) -> dict[str, Any]:
    if not scheduler.state.get_thread(owner_hash, thread_id):
        raise NotFoundError("Thread not found.")
    if not scheduler.state.get_turn(owner_hash, thread_id, turn_id):
        raise NotFoundError("Turn not found.")
    interaction = scheduler.state.get_interaction(owner_hash, interaction_id)
    if not interaction or interaction["thread_id"] != thread_id or interaction["turn_id"] != turn_id:
        raise NotFoundError("Interaction not found.")
    return interaction


def public_interaction(interaction: dict[str, Any]) -> dict[str, Any]:
    return {
        "interactionId": interaction["interaction_id"],
        "threadId": interaction["thread_id"],
        "turnId": interaction["turn_id"],
        "productCorrelationId": interaction.get("product_correlation_id"),
        "codexThreadId": interaction.get("codex_thread_id"),
        "codexTurnId": interaction.get("codex_turn_id"),
        "kind": interaction["kind"],
        "method": interaction["method"],
        "status": interaction["status"],
        "request": interaction["request"],
        "response": interaction.get("response"),
        "fallbackResponse": interaction["fallback_response"],
        "resolutionSource": interaction.get("resolution_source"),
        "createdAt": interaction["created_at"],
        "expiresAt": interaction.get("expires_at"),
        "resolvedAt": interaction.get("resolved_at"),
        "updatedAt": interaction["updated_at"],
    }
