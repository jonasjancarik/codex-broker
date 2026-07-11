from __future__ import annotations

from typing import Any, Protocol

from .util import json_dumps, utc_now


class StateConnection(Protocol):
    _conn: Any
    _lock: Any
    _events_condition: Any

    def _insert_audit_locked(self, owner_hash: str, action: str, payload: dict[str, Any], **kwargs: Any) -> int: ...

    def _rebuild_audit_counts_locked(self) -> None: ...


def finalize_turn(
    state: StateConnection,
    owner_hash: str,
    thread_id: str,
    turn_id: str,
    *,
    auth_principal_hash: str,
    status: str,
    error: str | None,
    error_code: str | None = None,
    public_message: str | None = None,
    admin_message: str | None = None,
    event_type: str,
    event_payload: dict[str, Any],
    product_correlation_id: str | None = None,
    codex_thread_id: str | None = None,
    codex_turn_id: str | None = None,
    raw_method: str | None = None,
    raw_params: dict[str, Any] | None = None,
    ambiguous: bool = False,
    audit_action: str | None = None,
    audit_payload: dict[str, Any] | None = None,
) -> bool:
    now = utc_now()
    with state._events_condition, state._conn:
        cursor = state._conn.execute(
            """
            update turns set status = ?, error = ?, error_code = ?, public_message = ?,
              admin_message = ?, completed_at = ?, updated_at = ?
            where owner_hash = ? and thread_id = ? and turn_id = ?
              and status in ('starting', 'queued', 'running')
            """,
            (status, error, error_code, public_message, admin_message, now, now, owner_hash, thread_id, turn_id),
        )
        if cursor.rowcount <= 0:
            return False
        state._conn.execute(
            """
            insert into events(owner_hash, thread_id, turn_id, event_type, payload_json,
              product_correlation_id, codex_thread_id, codex_turn_id,
              raw_method, raw_params_json, ambiguous, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_hash,
                thread_id,
                turn_id,
                event_type,
                json_dumps(event_payload),
                product_correlation_id,
                codex_thread_id,
                codex_turn_id,
                raw_method,
                json_dumps(raw_params) if raw_params is not None else None,
                1 if ambiguous else 0,
                now,
            ),
        )
        if audit_action:
            state._insert_audit_locked(
                owner_hash,
                audit_action,
                audit_payload or {},
                auth_principal_hash=auth_principal_hash,
                thread_id=thread_id,
                turn_id=turn_id,
                created_at=now,
            )
        state._events_condition.notify_all()
        return True


def prune_history_before(state: StateConnection, cutoff: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with state._lock, state._conn:
        for table, timestamp, extra in (
            ("events", "created_at", ""),
            ("audit_logs", "created_at", ""),
            ("pending_interactions", "updated_at", "and status != 'pending'"),
            ("app_server_processes", "closed_at", "and status != 'running'"),
            ("turns", "completed_at", "and status not in ('starting', 'queued', 'running')"),
        ):
            cursor = state._conn.execute(
                f"delete from {table} where {timestamp} is not null and {timestamp} < ? {extra}",
                (cutoff,),
            )
            counts[table] = int(cursor.rowcount)
        cursor = state._conn.execute(
            """
            delete from threads
            where status = 'archived' and updated_at < ?
              and not exists (
                select 1 from turns
                where turns.owner_hash = threads.owner_hash and turns.thread_id = threads.thread_id
              )
            """,
            (cutoff,),
        )
        counts["threads"] = int(cursor.rowcount)
        state._rebuild_audit_counts_locked()
    return counts


def prune_excess_events(state: StateConnection, max_events_per_turn: int) -> int:
    with state._lock, state._conn:
        cursor = state._conn.execute(
            """
            delete from events where id in (
              select id from (
                select id, row_number() over (
                  partition by owner_hash, thread_id, coalesce(turn_id, '') order by id desc
                ) as position
                from events
              ) where position > ?
            )
            """,
            (max_events_per_turn,),
        )
        return int(cursor.rowcount)
