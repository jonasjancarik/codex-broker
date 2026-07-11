from __future__ import annotations

import sqlite3

from .util import utc_now


SCHEMA_VERSION = 3


def initialize_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("pragma user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"State database schema version {version} is newer than this broker supports ({SCHEMA_VERSION})."
        )
    connection.execute("pragma journal_mode = wal")
    connection.executescript(
        """
        begin immediate;
        create table if not exists auth_profiles (
          auth_principal_hash text not null,
          profile text not null,
          instance_id text not null,
          auth_type text,
          auth_status text not null default 'unknown',
          auth_fingerprint text,
          created_at text not null,
          updated_at text not null,
          primary key (auth_principal_hash, profile)
        );
        create table if not exists threads (
          owner_hash text not null,
          thread_id text not null,
          auth_principal_hash text not null,
          auth_profile_instance_id text not null,
          auth_binding_error text,
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
          auth_principal_hash text not null,
          auth_profile_instance_id text not null,
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
          error_code text,
          public_message text,
          admin_message text,
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
          auth_principal_hash text not null,
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
          auth_principal_hash text not null,
          profile text,
          thread_id text,
          turn_id text,
          action text not null,
          payload_json text not null,
          created_at text not null
        );
        create table if not exists pending_interactions (
          owner_hash text not null,
          interaction_id text not null,
          thread_id text not null,
          turn_id text not null,
          product_correlation_id text,
          codex_thread_id text,
          codex_turn_id text,
          kind text not null,
          method text not null,
          status text not null,
          request_json text not null,
          response_json text,
          fallback_json text not null,
          resolution_source text,
          created_at text not null,
          expires_at text,
          resolved_at text,
          updated_at text not null,
          primary key (owner_hash, interaction_id)
        );
        create index if not exists idx_pending_interactions_thread
          on pending_interactions(owner_hash, thread_id, turn_id, status, created_at);
        create index if not exists idx_events_stream
          on events(owner_hash, thread_id, turn_id, id);
        create index if not exists idx_audit_owner_cursor
          on audit_logs(owner_hash, id);
        create table if not exists audit_action_counts (
          action text primary key,
          count integer not null
        );
        """
    )
    _ensure_legacy_columns(connection)
    connection.execute(
        "create index if not exists idx_audit_owner_principal_cursor "
        "on audit_logs(owner_hash, auth_principal_hash, id)"
    )
    if version < SCHEMA_VERSION:
        _migrate_auth_profiles(connection)
        _backfill_auth_bindings(connection)
    connection.execute("delete from audit_action_counts")
    connection.execute(
        "insert into audit_action_counts(action, count) select action, count(*) from audit_logs group by action"
    )
    now = utc_now()
    connection.execute(
        """
        update app_server_processes
        set status = 'orphaned', closed_at = coalesce(closed_at, ?), last_seen_at = ?
        where status = 'running'
        """,
        (now, now),
    )
    connection.execute(f"pragma user_version = {SCHEMA_VERSION}")


def _ensure_legacy_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(
        connection,
        "threads",
        {
            "host_app": "text",
            "config_profile": "text not null default 'default'",
            "auth_principal_hash": "text",
            "auth_profile_instance_id": "text",
            "auth_binding_error": "text",
        },
    )
    _copy_column_if_present(connection, "threads", "runtime_profile", "config_profile")
    _ensure_columns(
        connection,
        "turns",
        {
            "product_correlation_id": "text",
            "config_profile": "text not null default 'default'",
            "request_fingerprint": "text",
            "bundle_digest": "text",
            "resolved_options_json": "text",
            "broker_version": "text",
            "host_app": "text",
            "error_code": "text",
            "public_message": "text",
            "admin_message": "text",
            "auth_principal_hash": "text",
            "auth_profile_instance_id": "text",
        },
    )
    _copy_column_if_present(connection, "turns", "runtime_profile", "config_profile")
    _ensure_columns(
        connection,
        "app_server_processes",
        {
            "config_profile": "text not null default 'default'",
            "auth_principal_hash": "text",
        },
    )
    _copy_column_if_present(connection, "app_server_processes", "runtime_profile", "config_profile")
    _ensure_columns(connection, "audit_logs", {"auth_principal_hash": "text"})
    _ensure_columns(
        connection,
        "events",
        {
            "product_correlation_id": "text",
            "codex_thread_id": "text",
            "codex_turn_id": "text",
        },
    )


def _migrate_auth_profiles(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, "owner_profiles"):
        _ensure_columns(connection, "owner_profiles", {"auth_fingerprint": "text"})
        connection.execute(
            """
            insert or ignore into auth_profiles(
              auth_principal_hash, profile, instance_id, auth_type, auth_status,
              auth_fingerprint, created_at, updated_at
            )
            select owner_hash, profile, 'ap_' || lower(hex(randomblob(16))), auth_type,
              auth_status, auth_fingerprint, created_at, updated_at
            from owner_profiles
            """
        )
        connection.execute("drop table owner_profiles")


def _backfill_auth_bindings(connection: sqlite3.Connection) -> None:
    connection.execute(
        "update threads set auth_principal_hash = owner_hash where auth_principal_hash is null or auth_principal_hash = ''"
    )
    connection.execute(
        """
        insert or ignore into auth_profiles(
          auth_principal_hash, profile, instance_id, auth_status, created_at, updated_at
        )
        select auth_principal_hash, profile, 'ap_' || lower(hex(randomblob(16))),
          'unknown', min(created_at), max(updated_at)
        from threads
        group by auth_principal_hash, profile
        """
    )
    connection.execute(
        """
        update threads
        set auth_profile_instance_id = (
          select instance_id from auth_profiles
          where auth_profiles.auth_principal_hash = threads.auth_principal_hash
            and auth_profiles.profile = threads.profile
        )
        where auth_profile_instance_id is null or auth_profile_instance_id = ''
        """
    )
    connection.execute(
        """
        update turns
        set auth_principal_hash = coalesce(
          (select threads.auth_principal_hash from threads
           where threads.owner_hash = turns.owner_hash and threads.thread_id = turns.thread_id),
          owner_hash
        )
        where auth_principal_hash is null or auth_principal_hash = ''
        """
    )
    connection.execute(
        """
        insert or ignore into auth_profiles(
          auth_principal_hash, profile, instance_id, auth_status, created_at, updated_at
        )
        select auth_principal_hash, profile, 'ap_' || lower(hex(randomblob(16))),
          'unknown', min(created_at), max(updated_at)
        from turns
        group by auth_principal_hash, profile
        """
    )
    connection.execute(
        """
        update turns
        set auth_profile_instance_id = (
          select instance_id from auth_profiles
          where auth_profiles.auth_principal_hash = turns.auth_principal_hash
            and auth_profiles.profile = turns.profile
        )
        where auth_profile_instance_id is null or auth_profile_instance_id = ''
        """
    )
    connection.execute(
        """
        update threads
        set auth_binding_error = 'legacy_mixed_auth_profiles'
        where auth_binding_error is null and exists (
          select 1 from turns
          where turns.owner_hash = threads.owner_hash
            and turns.thread_id = threads.thread_id
            and (
              turns.profile != threads.profile
              or turns.auth_principal_hash != threads.auth_principal_hash
              or turns.auth_profile_instance_id != threads.auth_profile_instance_id
            )
        )
        """
    )
    connection.execute(
        """
        update app_server_processes
        set auth_principal_hash = owner_hash
        where auth_principal_hash is null or auth_principal_hash = ''
        """
    )
    connection.execute(
        """
        update audit_logs
        set auth_principal_hash = owner_hash
        where auth_principal_hash is null or auth_principal_hash = ''
        """
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table,),
    ).fetchone() is not None


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row["name"]) for row in connection.execute(f"pragma table_info({table})").fetchall()}
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"alter table {table} add column {name} {declaration}")


def _copy_column_if_present(
    connection: sqlite3.Connection,
    table: str,
    source: str,
    target: str,
) -> None:
    existing = {str(row["name"]) for row in connection.execute(f"pragma table_info({table})").fetchall()}
    if source in existing and target in existing:
        connection.execute(f"update {table} set {target} = {source} where {source} is not null")
