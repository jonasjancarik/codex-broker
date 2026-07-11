from __future__ import annotations

import sqlite3

from .util import utc_now


SCHEMA_VERSION = 3


def initialize_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("pragma user_version").fetchone()[0])
    if version not in {0, SCHEMA_VERSION}:
        raise RuntimeError(
            f"State database schema version {version} is incompatible with this broker "
            f"(expected {SCHEMA_VERSION}); start with an empty data directory."
        )
    if version == 0:
        existing_tables = connection.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
        ).fetchall()
        if existing_tables:
            raise RuntimeError(
                "Unversioned state database is incompatible with this broker; "
                "start with an empty data directory."
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
          request_fingerprint text,
          bundle_digest text,
          resolved_options_json text,
          broker_version text,
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
        create index if not exists idx_audit_owner_principal_cursor
          on audit_logs(owner_hash, auth_principal_hash, id);
        create table if not exists audit_action_counts (
          action text primary key,
          count integer not null
        );
        """
    )
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
