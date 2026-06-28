from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .util import ensure_dir, json_dumps, json_loads, random_id, utc_now


class StateStore:
    def __init__(self, path: Path) -> None:
        ensure_dir(path.parent)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
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
            self._conn.executescript(
                """
                pragma journal_mode = wal;
                create table if not exists owner_profiles (
                  owner_hash text not null,
                  profile text not null,
                  auth_type text,
                  auth_status text not null default 'unknown',
                  created_at text not null,
                  updated_at text not null,
                  primary key (owner_hash, profile)
                );
                create table if not exists threads (
                  owner_hash text not null,
                  thread_id text not null,
                  profile text not null,
                  codex_thread_id text,
                  config_profile text not null default 'default',
                  host_app text,
                  bundle_id text,
                  cwd text,
                  status text not null,
                  created_at text not null,
                  updated_at text not null,
                  primary key (owner_hash, thread_id)
                );
                create table if not exists turns (
                  owner_hash text not null,
                  thread_id text not null,
                  turn_id text not null,
                  codex_turn_id text,
                  profile text not null,
                  config_profile text not null,
                  host_app text,
                  bundle_id text,
                  cwd text,
                  mode text not null,
                  idempotency_key text,
                  product_correlation_id text,
                  status text not null,
                  input_json text not null,
                  error text,
                  created_at text not null,
                  started_at text,
                  completed_at text,
                  updated_at text not null,
                  primary key (owner_hash, thread_id, turn_id)
                );
                create unique index if not exists idx_turn_idempotency
                  on turns(owner_hash, thread_id, idempotency_key)
                  where idempotency_key is not null;
                create table if not exists events (
                  id integer primary key autoincrement,
                  owner_hash text not null,
                  thread_id text not null,
                  turn_id text,
                  product_correlation_id text,
                  codex_thread_id text,
                  codex_turn_id text,
                  event_type text not null,
                  payload_json text not null,
                  raw_method text,
                  raw_params_json text,
                  ambiguous integer not null default 0,
                  created_at text not null
                );
                create table if not exists bundle_digests (
                  bundle_id text primary key,
                  digest text not null,
                  source text not null,
                  path text not null,
                  created_at text not null,
                  updated_at text not null
                );
                create table if not exists app_server_processes (
                  id integer primary key autoincrement,
                  pool_key_hash text not null,
                  owner_hash text not null,
                  profile text not null,
                  config_profile text not null,
                  pid integer,
                  status text not null,
                  started_at text not null,
                  last_seen_at text not null,
                  closed_at text,
                  exit_code integer
                );
                create table if not exists audit_logs (
                  id integer primary key autoincrement,
                  owner_hash text not null,
                  profile text,
                  thread_id text,
                  turn_id text,
                  action text not null,
                  payload_json text not null,
                  created_at text not null
                );
                """
            )
            self._ensure_columns(
                "threads",
                {
                    "host_app": "text",
                    "config_profile": "text not null default 'default'",
                },
            )
            self._copy_column_if_present("threads", "runtime_profile", "config_profile")
            self._ensure_columns(
                "turns",
                {
                    "product_correlation_id": "text",
                    "config_profile": "text not null default 'default'",
                },
            )
            self._copy_column_if_present("turns", "runtime_profile", "config_profile")
            self._ensure_columns("turns", {"host_app": "text"})
            self._ensure_columns("app_server_processes", {"config_profile": "text not null default 'default'"})
            self._copy_column_if_present("app_server_processes", "runtime_profile", "config_profile")
            self._ensure_columns(
                "events",
                {
                    "product_correlation_id": "text",
                    "codex_thread_id": "text",
                    "codex_turn_id": "text",
                },
            )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute(f"pragma table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                self._conn.execute(f"alter table {table} add column {name} {declaration}")

    def _copy_column_if_present(self, table: str, source: str, target: str) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute(f"pragma table_info({table})").fetchall()
        }
        if source in existing and target in existing:
            self._conn.execute(f"update {table} set {target} = {source} where {source} is not null")

    def ensure_profile(self, owner_hash: str, profile: str, auth_type: str | None = None) -> None:
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                insert into owner_profiles(owner_hash, profile, auth_type, auth_status, created_at, updated_at)
                values (?, ?, ?, 'unknown', ?, ?)
                on conflict(owner_hash, profile) do update set
                  auth_type = coalesce(excluded.auth_type, owner_profiles.auth_type),
                  updated_at = excluded.updated_at
                """,
                (owner_hash, profile, auth_type, now, now),
            )

    def update_auth_status(self, owner_hash: str, profile: str, status: str, auth_type: str | None = None) -> None:
        self.ensure_profile(owner_hash, profile, auth_type)
        with self._lock, self._conn:
            self._conn.execute(
                """
                update owner_profiles
                set auth_status = ?, auth_type = coalesce(?, auth_type), updated_at = ?
                where owner_hash = ? and profile = ?
                """,
                (status, auth_type, utc_now(), owner_hash, profile),
            )

    def delete_profile(self, owner_hash: str, profile: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "delete from owner_profiles where owner_hash = ? and profile = ?",
                (owner_hash, profile),
            )

    def list_profiles(self, owner_hash: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select * from owner_profiles where owner_hash = ? order by profile asc",
                (owner_hash,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_thread(
        self,
        owner_hash: str,
        *,
        thread_id: str | None = None,
        profile: str,
        config_profile: str,
        host_app: str | None,
        bundle_id: str | None,
        cwd: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        caller_supplied_thread_id = thread_id is not None
        thread_id = thread_id or random_id("thr")
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    """
                    insert into threads(
                      owner_hash, thread_id, profile, config_profile,
                      host_app, bundle_id, cwd, status, created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (owner_hash, thread_id, profile, config_profile, host_app, bundle_id, cwd, now, now),
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
    ) -> dict[str, Any]:
        now = utc_now()
        turn_id = random_id("turn")
        with self._lock, self._conn:
            if idempotency_key:
                existing = self._conn.execute(
                    "select * from turns where owner_hash = ? and thread_id = ? and idempotency_key = ?",
                    (owner_hash, thread_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._turn_from_row(existing)
            self._conn.execute(
                """
                insert into turns(
                  owner_hash, thread_id, turn_id, profile, config_profile, bundle_id, cwd,
                  host_app, mode, idempotency_key, product_correlation_id, status, input_json, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_hash,
                    thread_id,
                    turn_id,
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
                    now,
                    now,
                ),
            )
        return self.get_turn(owner_hash, thread_id, turn_id) or {}

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
            "started_at": utc_now() if started and not row.get("started_at") else row.get("started_at"),
            "completed_at": utc_now() if completed else row.get("completed_at"),
            "updated_at": utc_now(),
        }
        with self._lock, self._conn:
            self._conn.execute(
                """
                update turns set status = ?, codex_turn_id = ?, error = ?, started_at = ?,
                  completed_at = ?, updated_at = ?
                where owner_hash = ? and thread_id = ? and turn_id = ?
                """,
                (
                    updates["status"],
                    updates["codex_turn_id"],
                    updates["error"],
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
        with self._lock, self._conn:
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
            return int(cursor.lastrowid)

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
        owner_hash: str,
        profile: str,
        config_profile: str,
        pid: int | None,
    ) -> int:
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                insert into app_server_processes(
                  pool_key_hash, owner_hash, profile, config_profile, pid, status,
                  started_at, last_seen_at
                )
                values (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (pool_key_hash, owner_hash, profile, config_profile, pid, now, now),
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
        profile: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                insert into audit_logs(owner_hash, profile, thread_id, turn_id, action, payload_json, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (owner_hash, profile, thread_id, turn_id, action, json_dumps(payload or {}), utc_now()),
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
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if owner_hash:
            where.append("owner_hash = ?")
            params.append(owner_hash)
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
            rows = self._conn.execute(
                "select action, count(*) as count from audit_logs group by action"
            ).fetchall()
        return {str(row["action"]): int(row["count"]) for row in rows}

    def _turn_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["input"] = json_loads(data.pop("input_json"), [])
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
