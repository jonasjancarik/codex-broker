from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import state_schema, state_transactions
from .util import ensure_dir, json_dumps, json_loads, random_id, utc_now


class StateStore:
    def __init__(self, path: Path) -> None:
        ensure_dir(path.parent)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._events_condition = threading.Condition(self._lock)
        self._closed = False
        self._init_schema()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            return

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def ping(self) -> bool:
        with self._lock:
            self._conn.execute("select 1").fetchone()
        return True

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            state_schema.initialize_schema(self._conn)

    @staticmethod
    def _future_timestamp(seconds: float | None) -> str | None:
        if seconds is None or seconds <= 0:
            return None
        expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return expires.isoformat().replace("+00:00", "Z")

    def ensure_profile(
        self,
        auth_principal_hash: str,
        profile: str,
        auth_type: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                insert into auth_profiles(
                  auth_principal_hash, profile, instance_id, auth_type,
                  auth_status, created_at, updated_at
                )
                values (?, ?, ?, ?, 'unknown', ?, ?)
                on conflict(auth_principal_hash, profile) do update set
                  auth_type = coalesce(excluded.auth_type, auth_profiles.auth_type),
                  updated_at = excluded.updated_at
                """,
                (auth_principal_hash, profile, random_id("ap"), auth_type, now, now),
            )
        return self.get_profile(auth_principal_hash, profile) or {}

    def update_auth_status(
        self,
        auth_principal_hash: str,
        profile: str,
        status: str,
        auth_type: str | None = None,
        auth_fingerprint: str | None = None,
    ) -> None:
        self.ensure_profile(auth_principal_hash, profile, auth_type)
        with self._lock, self._conn:
            self._conn.execute(
                """
                update auth_profiles
                set auth_status = ?,
                    auth_type = coalesce(?, auth_type),
                    auth_fingerprint = coalesce(?, auth_fingerprint),
                    updated_at = ?
                where auth_principal_hash = ? and profile = ?
                """,
                (status, auth_type, auth_fingerprint, utc_now(), auth_principal_hash, profile),
            )

    def get_profile(self, auth_principal_hash: str, profile: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from auth_profiles where auth_principal_hash = ? and profile = ?",
                (auth_principal_hash, profile),
            ).fetchone()
        return dict(row) if row else None

    def delete_profile(self, auth_principal_hash: str, profile: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "delete from auth_profiles where auth_principal_hash = ? and profile = ?",
                (auth_principal_hash, profile),
            )

    def list_profiles(self, auth_principal_hash: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from auth_profiles where auth_principal_hash = ? order by profile asc",
                (auth_principal_hash,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_thread(
        self,
        owner_hash: str,
        *,
        thread_id: str | None = None,
        auth_principal_hash: str | None = None,
        auth_profile_instance_id: str | None = None,
        profile: str,
        config_profile: str,
        host_app: str | None,
        bundle_id: str | None,
        cwd: str | None,
    ) -> dict[str, Any]:
        auth_principal_hash = auth_principal_hash or owner_hash
        profile_row = self.ensure_profile(auth_principal_hash, profile)
        auth_profile_instance_id = auth_profile_instance_id or str(profile_row["instance_id"])
        if profile_row.get("instance_id") != auth_profile_instance_id:
            raise ValueError("Auth profile instance changed while creating the broker thread.")
        now = utc_now()
        caller_supplied_thread_id = thread_id is not None
        thread_id = thread_id or random_id("thr")
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    """
                    insert into threads(
                      owner_hash, thread_id, auth_principal_hash, auth_profile_instance_id,
                      profile, config_profile,
                      host_app, bundle_id, cwd, status, created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        owner_hash,
                        thread_id,
                        auth_principal_hash,
                        auth_profile_instance_id,
                        profile,
                        config_profile,
                        host_app,
                        bundle_id,
                        cwd,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                if caller_supplied_thread_id:
                    existing = self.get_thread(owner_hash, thread_id)
                    if existing:
                        return existing
                raise
        return self.get_thread(owner_hash, thread_id) or {}

    def get_thread(self, owner_hash: str, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from threads where owner_hash = ? and thread_id = ?",
                (owner_hash, thread_id),
            ).fetchone()
        return dict(row) if row else None

    def archive_thread(self, owner_hash: str, thread_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn:
            self._conn.execute(
                "update threads set status = 'archived', updated_at = ? where owner_hash = ? and thread_id = ?",
                (utc_now(), owner_hash, thread_id),
            )
        return self.get_thread(owner_hash, thread_id)

    def set_codex_thread_id(self, owner_hash: str, thread_id: str, codex_thread_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "update threads set codex_thread_id = ?, updated_at = ? where owner_hash = ? and thread_id = ?",
                (codex_thread_id, utc_now(), owner_hash, thread_id),
            )

    def find_turn_by_idempotency(self, owner_hash: str, thread_id: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from turns where owner_hash = ? and thread_id = ? and idempotency_key = ?",
                (owner_hash, thread_id, key),
            ).fetchone()
        return self._turn_from_row(row) if row else None

    def create_turn(
        self,
        owner_hash: str,
        thread_id: str,
        *,
        auth_principal_hash: str | None = None,
        auth_profile_instance_id: str | None = None,
        profile: str,
        config_profile: str,
        host_app: str | None,
        bundle_id: str | None,
        cwd: str | None,
        mode: str,
        input_items: list[dict[str, Any]],
        idempotency_key: str | None,
        product_correlation_id: str | None,
        status: str,
        request_fingerprint: str | None = None,
        bundle_digest: str | None = None,
        resolved_options: dict[str, Any] | None = None,
        broker_version: str | None = None,
    ) -> dict[str, Any]:
        thread = self.get_thread(owner_hash, thread_id)
        if not thread:
            raise ValueError("Thread not found while creating turn.")
        auth_principal_hash = auth_principal_hash or str(thread["auth_principal_hash"])
        auth_profile_instance_id = auth_profile_instance_id or str(thread["auth_profile_instance_id"])
        if (
            profile != thread["profile"]
            or auth_principal_hash != thread["auth_principal_hash"]
            or auth_profile_instance_id != thread["auth_profile_instance_id"]
        ):
            raise ValueError("Turn authentication binding must match its broker thread.")
        now = utc_now()
        turn_id = random_id("turn")
        with self._lock, self._conn:
            if idempotency_key:
                existing = self._conn.execute(
                    "select * from turns where owner_hash = ? and thread_id = ? and idempotency_key = ?",
                    (owner_hash, thread_id, idempotency_key),
                ).fetchone()
                if existing:
                    result = self._turn_from_row(existing)
                    result["_created"] = False
                    return result
            self._conn.execute(
                """
                insert into turns(
                  owner_hash, thread_id, turn_id, auth_principal_hash, auth_profile_instance_id,
                  profile, config_profile, bundle_id, cwd,
                  host_app, mode, idempotency_key, product_correlation_id, status, input_json,
                  request_fingerprint, bundle_digest, resolved_options_json, broker_version,
                  created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_hash,
                    thread_id,
                    turn_id,
                    auth_principal_hash,
                    auth_profile_instance_id,
                    profile,
                    config_profile,
                    bundle_id,
                    cwd,
                    host_app,
                    mode,
                    idempotency_key,
                    product_correlation_id,
                    status,
                    json_dumps(input_items),
                    request_fingerprint,
                    bundle_digest,
                    json_dumps(resolved_options) if resolved_options is not None else None,
                    broker_version,
                    now,
                    now,
                ),
            )
        result = self.get_turn(owner_hash, thread_id, turn_id) or {}
        result["_created"] = True
        return result

    def get_turn(self, owner_hash: str, thread_id: str, turn_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from turns where owner_hash = ? and thread_id = ? and turn_id = ?",
                (owner_hash, thread_id, turn_id),
            ).fetchone()
        return self._turn_from_row(row) if row else None

    def update_turn(
        self,
        owner_hash: str,
        thread_id: str,
        turn_id: str,
        *,
        status: str | None = None,
        codex_turn_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        public_message: str | None = None,
        admin_message: str | None = None,
        started: bool = False,
        completed: bool = False,
    ) -> dict[str, Any] | None:
        row = self.get_turn(owner_hash, thread_id, turn_id)
        if not row:
            return None
        updates = {
            "status": status if status is not None else row["status"],
            "codex_turn_id": codex_turn_id if codex_turn_id is not None else row.get("codex_turn_id"),
            "error": error,
            "error_code": error_code,
            "public_message": public_message,
            "admin_message": admin_message,
            "started_at": utc_now() if started and not row.get("started_at") else row.get("started_at"),
            "completed_at": utc_now() if completed else row.get("completed_at"),
            "updated_at": utc_now(),
        }
        with self._lock, self._conn:
            self._conn.execute(
                """
                update turns set status = ?, codex_turn_id = ?, error = ?,
                  error_code = ?, public_message = ?, admin_message = ?,
                  started_at = ?, completed_at = ?, updated_at = ?
                where owner_hash = ? and thread_id = ? and turn_id = ?
                """,
                (
                    updates["status"],
                    updates["codex_turn_id"],
                    updates["error"],
                    updates["error_code"],
                    updates["public_message"],
                    updates["admin_message"],
                    updates["started_at"],
                    updates["completed_at"],
                    updates["updated_at"],
                    owner_hash,
                    thread_id,
                    turn_id,
                ),
            )
        return self.get_turn(owner_hash, thread_id, turn_id)

    def append_event(
        self,
        owner_hash: str,
        thread_id: str,
        turn_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        *,
        product_correlation_id: str | None = None,
        codex_thread_id: str | None = None,
        codex_turn_id: str | None = None,
        raw_method: str | None = None,
        raw_params: dict[str, Any] | None = None,
        ambiguous: bool = False,
    ) -> int:
        with self._events_condition, self._conn:
            cursor = self._conn.execute(
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
                    json_dumps(payload),
                    product_correlation_id,
                    codex_thread_id,
                    codex_turn_id,
                    raw_method,
                    json_dumps(raw_params) if raw_params is not None else None,
                    1 if ambiguous else 0,
                    utc_now(),
                ),
            )
            event_id = int(cursor.lastrowid)
            self._events_condition.notify_all()
            return event_id

    def wait_for_events(self, timeout_seconds: float) -> None:
        with self._events_condition:
            self._events_condition.wait(timeout=max(timeout_seconds, 0))

    def finalize_turn(
        self,
        owner_hash: str,
        thread_id: str,
        turn_id: str,
        **finalization: Any,
    ) -> bool:
        return state_transactions.finalize_turn(self, owner_hash, thread_id, turn_id, **finalization)

    def list_events(
        self,
        owner_hash: str,
        thread_id: str,
        *,
        after: int = 0,
        turn_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [owner_hash, thread_id, after]
        where = "owner_hash = ? and thread_id = ? and id > ?"
        if turn_id:
            where += " and turn_id = ?"
            params.append(turn_id)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"select * from events where {where} order by id asc limit ?",
                params,
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def create_pending_interaction(
        self,
        owner_hash: str,
        thread_id: str,
        turn_id: str,
        *,
        kind: str,
        method: str,
        request: dict[str, Any],
        fallback_response: dict[str, Any],
        product_correlation_id: str | None,
        codex_thread_id: str | None,
        codex_turn_id: str | None,
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        now = utc_now()
        interaction_id = random_id("int")
        with self._lock, self._conn:
            self._conn.execute(
                """
                insert into pending_interactions(
                  owner_hash, interaction_id, thread_id, turn_id,
                  product_correlation_id, codex_thread_id, codex_turn_id,
                  kind, method, status, request_json, fallback_json,
                  created_at, expires_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    owner_hash,
                    interaction_id,
                    thread_id,
                    turn_id,
                    product_correlation_id,
                    codex_thread_id,
                    codex_turn_id,
                    kind,
                    method,
                    json_dumps(request),
                    json_dumps(fallback_response),
                    now,
                    self._future_timestamp(timeout_seconds),
                    now,
                ),
            )
        interaction = self.get_interaction(owner_hash, interaction_id)
        return interaction or {}

    def get_interaction(self, owner_hash: str, interaction_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from pending_interactions where owner_hash = ? and interaction_id = ?",
                (owner_hash, interaction_id),
            ).fetchone()
        return self._interaction_from_row(row) if row else None

    def list_interactions(
        self,
        owner_hash: str,
        thread_id: str,
        *,
        turn_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [owner_hash, thread_id]
        where = "owner_hash = ? and thread_id = ?"
        if turn_id:
            where += " and turn_id = ?"
            params.append(turn_id)
        if status:
            where += " and status = ?"
            params.append(status)
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(
                f"select * from pending_interactions where {where} order by created_at asc limit ?",
                params,
            ).fetchall()
        return [self._interaction_from_row(row) for row in rows]

    def complete_interaction(
        self,
        owner_hash: str,
        interaction_id: str,
        *,
        response: dict[str, Any],
        source: str,
        status: str = "resolved",
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                update pending_interactions
                set status = ?,
                    response_json = ?,
                    resolution_source = ?,
                    resolved_at = coalesce(resolved_at, ?),
                    updated_at = ?
                where owner_hash = ? and interaction_id = ? and status = 'pending'
                """,
                (status, json_dumps(response), source, now, now, owner_hash, interaction_id),
            )
            if cursor.rowcount <= 0:
                return self.get_interaction(owner_hash, interaction_id)
        return self.get_interaction(owner_hash, interaction_id)

    def recover_pending_interactions(self) -> int:
        now = utc_now()
        with self._lock, self._conn:
            rows = self._conn.execute(
                "select * from pending_interactions where status = 'pending'"
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    """
                    update pending_interactions
                    set status = 'failed',
                        response_json = fallback_json,
                        resolution_source = 'broker_restarted',
                        resolved_at = coalesce(resolved_at, ?),
                        updated_at = ?
                    where owner_hash = ? and interaction_id = ?
                    """,
                    (now, now, row["owner_hash"], row["interaction_id"]),
                )
        return len(rows)

    def recover_incomplete_turns(self, message: str) -> int:
        now = utc_now()
        with self._lock, self._conn:
            rows = self._conn.execute(
                """
                select turns.*, threads.codex_thread_id as thread_codex_thread_id
                from turns
                left join threads
                  on threads.owner_hash = turns.owner_hash and threads.thread_id = turns.thread_id
                where turns.status in ('starting', 'queued', 'running')
                order by turns.created_at asc
                """
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    """
                    update turns
                    set status = 'failed', error = ?, completed_at = ?, updated_at = ?
                    where owner_hash = ? and thread_id = ? and turn_id = ?
                    """,
                    (message, now, now, row["owner_hash"], row["thread_id"], row["turn_id"]),
                )
                self._conn.execute(
                    """
                    insert into events(owner_hash, thread_id, turn_id, event_type, payload_json,
                      product_correlation_id, codex_thread_id, codex_turn_id,
                      raw_method, raw_params_json, ambiguous, created_at)
                    values (?, ?, ?, 'turn.failed', ?, ?, ?, ?, null, null, 0, ?)
                    """,
                    (
                        row["owner_hash"],
                        row["thread_id"],
                        row["turn_id"],
                        json_dumps({"message": message, "recovered": True}),
                        row["product_correlation_id"],
                        row["thread_codex_thread_id"],
                        row["codex_turn_id"],
                        now,
                    ),
                )
        return len(rows)

    def prune_raw_events_before(self, cutoff: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                update events
                set raw_method = null, raw_params_json = null
                where created_at < ? and (raw_method is not null or raw_params_json is not null)
                """,
                (cutoff,),
            )
            return int(cursor.rowcount)

    def prune_history_before(self, cutoff: str) -> dict[str, int]:
        return state_transactions.prune_history_before(self, cutoff)

    def prune_excess_events(self, max_events_per_turn: int) -> int:
        return state_transactions.prune_excess_events(self, max_events_per_turn)

    def _rebuild_audit_counts_locked(self) -> None:
        self._conn.execute("delete from audit_action_counts")
        self._conn.execute(
            "insert into audit_action_counts(action, count) select action, count(*) from audit_logs group by action"
        )

    def record_bundle(self, bundle_id: str, digest: str, source: str, path: str) -> None:
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                insert into bundle_digests(bundle_id, digest, source, path, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(bundle_id) do update set
                  digest = excluded.digest,
                  source = excluded.source,
                  path = excluded.path,
                  updated_at = excluded.updated_at
                """,
                (bundle_id, digest, source, path, now, now),
            )

    def get_bundle_record(self, bundle_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "select * from bundle_digests where bundle_id = ?",
                (bundle_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_app_server_start(
        self,
        *,
        pool_key_hash: str,
        auth_principal_hash: str | None = None,
        owner_hash: str | None = None,
        profile: str,
        config_profile: str,
        pid: int | None,
    ) -> int:
        principal_hash = auth_principal_hash or owner_hash
        if not principal_hash:
            raise ValueError("auth_principal_hash is required.")
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                insert into app_server_processes(
                  pool_key_hash, owner_hash, auth_principal_hash, profile, config_profile, pid, status,
                  started_at, last_seen_at
                )
                values (?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (pool_key_hash, principal_hash, principal_hash, profile, config_profile, pid, now, now),
            )
            return int(cursor.lastrowid)

    def record_app_server_close(self, process_id: int, *, status: str, exit_code: int | None) -> None:
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                update app_server_processes
                set status = ?, exit_code = ?, closed_at = coalesce(closed_at, ?), last_seen_at = ?
                where id = ?
                """,
                (status, exit_code, now, now, process_id),
            )

    def list_app_server_processes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from app_server_processes order by id asc limit ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_audit(
        self,
        owner_hash: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        auth_principal_hash: str | None = None,
        profile: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> int:
        with self._lock, self._conn:
            return self._insert_audit_locked(
                owner_hash,
                action,
                payload or {},
                auth_principal_hash=auth_principal_hash or owner_hash,
                profile=profile,
                thread_id=thread_id,
                turn_id=turn_id,
                created_at=utc_now(),
            )

    def _insert_audit_locked(
        self,
        owner_hash: str,
        action: str,
        payload: dict[str, Any],
        *,
        auth_principal_hash: str | None = None,
        profile: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        created_at: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            insert into audit_logs(
              owner_hash, auth_principal_hash, profile, thread_id, turn_id,
              action, payload_json, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_hash,
                auth_principal_hash or owner_hash,
                profile,
                thread_id,
                turn_id,
                action,
                json_dumps(payload),
                created_at,
            ),
        )
        self._conn.execute(
            """
            insert into audit_action_counts(action, count) values (?, 1)
            on conflict(action) do update set count = audit_action_counts.count + 1
            """,
            (action,),
        )
        return int(cursor.lastrowid)

    def list_audit_logs(
        self,
        owner_hash: str | None = None,
        *,
        action: str | None = None,
        profile: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if owner_hash:
            where.append("owner_hash = ?")
            params.append(owner_hash)
        if after > 0:
            where.append("id > ?")
            params.append(after)
        if action:
            where.append("action = ?")
            params.append(action)
        if profile:
            where.append("profile = ?")
            params.append(profile)
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        if turn_id:
            where.append("turn_id = ?")
            params.append(turn_id)
        clause = f"where {' and '.join(where)}" if where else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(
                f"select * from audit_logs {clause} order by id asc limit ?",
                params,
            ).fetchall()
        return [self._audit_from_row(row) for row in rows]

    def count_audit_actions(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("select action, count from audit_action_counts").fetchall()
        return {str(row["action"]): int(row["count"]) for row in rows}

    def _turn_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["input"] = json_loads(data.pop("input_json"), [])
        data["resolved_options"] = json_loads(data.pop("resolved_options_json", None), None)
        return data

    def _event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json_loads(data.pop("payload_json"), {})
        raw = data.pop("raw_params_json")
        data["raw_params"] = json_loads(raw, None)
        data["ambiguous"] = bool(data["ambiguous"])
        return data

    def _audit_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json_loads(data.pop("payload_json"), {})
        return data

    def _interaction_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["request"] = json_loads(data.pop("request_json"), {})
        data["response"] = json_loads(data.pop("response_json"), None)
        data["fallback_response"] = json_loads(data.pop("fallback_json"), {})
        return data
