from __future__ import annotations

import tempfile
import sqlite3
import unittest
from pathlib import Path

from codex_broker.state import StateStore
from test_broker import config_for


class StateStoreTests(unittest.TestCase):
    def test_schema_version_is_recorded_and_newer_databases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = config_for(Path(tmp_raw)).state_db_path
            state = StateStore(path)
            state.close()
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 3)
                connection.execute("pragma user_version = 4")
            with self.assertRaisesRegex(RuntimeError, "newer than this broker supports"):
                StateStore(path)

    def test_v2_owner_auth_state_migrates_to_default_principal_and_flags_mixed_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = config_for(Path(tmp_raw)).state_db_path
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    create table owner_profiles (
                      owner_hash text not null,
                      profile text not null,
                      auth_type text,
                      auth_status text not null,
                      auth_fingerprint text,
                      created_at text not null,
                      updated_at text not null,
                      primary key (owner_hash, profile)
                    );
                    create table threads (
                      owner_hash text not null,
                      thread_id text not null,
                      profile text not null,
                      codex_thread_id text,
                      status text not null,
                      created_at text not null,
                      updated_at text not null,
                      primary key (owner_hash, thread_id)
                    );
                    create table turns (
                      owner_hash text not null,
                      thread_id text not null,
                      turn_id text not null,
                      profile text not null,
                      idempotency_key text,
                      status text not null,
                      input_json text not null,
                      created_at text not null,
                      updated_at text not null,
                      primary key (owner_hash, thread_id, turn_id)
                    );
                    insert into owner_profiles values (
                      'owner_hash', 'work', 'api-key', 'authenticated', 'sha256:old',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                    );
                    insert into threads values (
                      'owner_hash', 'legacy-thread', 'work', 'codex-thread', 'active',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                    );
                    insert into turns values (
                      'owner_hash', 'legacy-thread', 'legacy-turn', 'other', null, 'completed', '[]',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                    );
                    pragma user_version = 2;
                    """
                )

            state = StateStore(path)
            try:
                profile = state.get_profile("owner_hash", "work")
                thread = state.get_thread("owner_hash", "legacy-thread")
                turn = state.get_turn("owner_hash", "legacy-thread", "legacy-turn")
                assert profile is not None and thread is not None and turn is not None
                self.assertEqual(profile["auth_principal_hash"], "owner_hash")
                self.assertEqual(thread["auth_principal_hash"], "owner_hash")
                self.assertEqual(turn["auth_principal_hash"], "owner_hash")
                self.assertEqual(thread["auth_binding_error"], "legacy_mixed_auth_profiles")
                self.assertEqual(thread["auth_profile_instance_id"], profile["instance_id"])
                self.assertNotEqual(turn["auth_profile_instance_id"], profile["instance_id"])
                self.assertEqual(state._conn.execute("pragma user_version").fetchone()[0], 3)
                self.assertIsNone(
                    state._conn.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'owner_profiles'"
                    ).fetchone()
                )
            finally:
                state.close()

    def test_create_turn_returns_existing_turn_for_duplicate_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path)
            try:
                thread = state.create_thread(
                    "owner_hash",
                    thread_id=None,
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                )
                first = state.create_turn(
                    "owner_hash",
                    thread["thread_id"],
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                    mode="reject",
                    input_items=[{"type": "text", "text": "original"}],
                    idempotency_key="host-turn-1",
                    product_correlation_id="correlation-1",
                    status="starting",
                )
                duplicate = state.create_turn(
                    "owner_hash",
                    thread["thread_id"],
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                    mode="queue",
                    input_items=[{"type": "text", "text": "retry"}],
                    idempotency_key="host-turn-1",
                    product_correlation_id="correlation-2",
                    status="queued",
                )

                self.assertEqual(duplicate["turn_id"], first["turn_id"])
                self.assertEqual(duplicate["mode"], "reject")
                self.assertEqual(duplicate["input"], [{"type": "text", "text": "original"}])
                self.assertEqual(duplicate["product_correlation_id"], "correlation-1")
            finally:
                state.close()

    def test_pending_interaction_lifecycle_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path)
            try:
                thread = state.create_thread(
                    "owner_hash",
                    thread_id="thread_1",
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                )
                turn = state.create_turn(
                    "owner_hash",
                    thread["thread_id"],
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                    mode="reject",
                    input_items=[{"type": "text", "text": "approval"}],
                    idempotency_key=None,
                    product_correlation_id="correlation-1",
                    status="running",
                )
                pending = state.create_pending_interaction(
                    "owner_hash",
                    "thread_1",
                    turn["turn_id"],
                    kind="approval",
                    method="item/commandExecution/requestApproval",
                    request={"command": "printf test"},
                    fallback_response={"decision": "decline"},
                    product_correlation_id="correlation-1",
                    codex_thread_id="codex_thread_1",
                    codex_turn_id="codex_turn_1",
                    timeout_seconds=30,
                )

                listed = state.list_interactions("owner_hash", "thread_1", status="pending")
                self.assertEqual(listed[0]["interaction_id"], pending["interaction_id"])
                resolved = state.complete_interaction(
                    "owner_hash",
                    pending["interaction_id"],
                    response={"decision": "accept"},
                    source="host",
                )
                self.assertEqual(resolved["status"], "resolved")
                self.assertEqual(resolved["response"], {"decision": "accept"})
                self.assertEqual(resolved["resolution_source"], "host")

                orphan = state.create_pending_interaction(
                    "owner_hash",
                    "thread_1",
                    turn["turn_id"],
                    kind="mcpElicitation",
                    method="mcpServer/elicitation/request",
                    request={"serverName": "host"},
                    fallback_response={"action": "decline", "content": None, "_meta": None},
                    product_correlation_id=None,
                    codex_thread_id=None,
                    codex_turn_id=None,
                    timeout_seconds=30,
                )
                self.assertEqual(state.recover_pending_interactions(), 1)
                recovered = state.get_interaction("owner_hash", orphan["interaction_id"])
                self.assertEqual(recovered["status"], "failed")
                self.assertEqual(recovered["response"], {"action": "decline", "content": None, "_meta": None})
                self.assertEqual(recovered["resolution_source"], "broker_restarted")
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
