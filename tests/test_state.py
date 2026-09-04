from __future__ import annotations

import tempfile
import sqlite3
import os
import unittest
from pathlib import Path

from codex_broker.security import REDACTED, SecretSanitizer
from codex_broker import state_transactions
from codex_broker.state import StateStore
from test_broker import config_for


class StateStoreTests(unittest.TestCase):
    def test_state_files_and_parent_directory_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = config_for(Path(tmp_raw)).state_db_path
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o755)
            state = StateStore(path)
            try:
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
                    self.assertTrue(sidecar.exists())
                    self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
            finally:
                state.close()

    def _thread_and_turn(self, state: StateStore) -> tuple[dict[str, object], dict[str, object]]:
        profile = state.ensure_profile("principal_hash", "default")
        thread = state.create_thread(
            "owner_hash",
            thread_id="thread-security",
            auth_principal_hash="principal_hash",
            auth_profile_instance_id=profile["instance_id"],
            profile="default",
            config_profile="default",
            host_app=None,
            bundle_id=None,
            cwd=None,
        )
        turn = state.create_turn(
            "owner_hash",
            thread["thread_id"],
            auth_principal_hash="principal_hash",
            auth_profile_instance_id=profile["instance_id"],
            profile="default",
            config_profile="default",
            host_app=None,
            bundle_id=None,
            cwd=None,
            mode="reject",
            input_items=[{"type": "text", "text": "user supplied api_key=keep-this-input"}],
            idempotency_key=None,
            product_correlation_id=None,
            status="running",
            resolved_options={"authorization": "metadata-secret"},
        )
        return thread, turn

    def test_safe_mode_sanitizes_persistence_boundaries_but_preserves_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path, sanitizer=SecretSanitizer("safe"))
            try:
                thread, turn = self._thread_and_turn(state)
                state.append_event(
                    "owner_hash", thread["thread_id"], turn["turn_id"], "message",
                    {"text": "Bearer event-secret", "password": "field-secret"},
                    raw_params={"api_key": "raw-secret"},
                )
                state.append_audit(
                    "owner_hash", "security.test", {"detail": "api_key=audit-secret"},
                    auth_principal_hash="principal_hash",
                )
                state.update_turn(
                    "owner_hash", thread["thread_id"], turn["turn_id"],
                    error="Bearer error-secret", public_message="secret=public-secret",
                )

                loaded_turn = state.get_turn("owner_hash", thread["thread_id"], turn["turn_id"])
                event = state.list_events("owner_hash", thread["thread_id"])[0]
                audit = state.list_audit_logs("owner_hash")[0]
                self.assertEqual(loaded_turn["input"][0]["text"], "user supplied api_key=keep-this-input")
                self.assertNotIn("metadata-secret", str(loaded_turn["resolved_options"]))
                self.assertNotIn("event-secret", str(event["payload"]))
                self.assertEqual(event["raw_params"]["api_key"], REDACTED)
                self.assertNotIn("audit-secret", str(audit["payload"]))
                self.assertNotIn("error-secret", loaded_turn["error"])
                self.assertNotIn("public-secret", loaded_turn["public_message"])
            finally:
                state.close()

    def test_raw_mode_keeps_non_mandatory_payloads_byte_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path, sanitizer=SecretSanitizer("raw"))
            try:
                thread, turn = self._thread_and_turn(state)
                payload = {"nested": ["api_key=payload-secret"], "normal": "unchanged"}
                state.append_event(
                    "owner_hash", thread["thread_id"], turn["turn_id"], "message", payload,
                    raw_params={"api_key": "raw-secret"},
                )
                event = state.list_events("owner_hash", thread["thread_id"])[0]
                self.assertEqual(event["payload"], payload)
                self.assertEqual(event["raw_params"]["api_key"], REDACTED)
            finally:
                state.close()

    def test_historical_dirty_rows_are_sanitized_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = config_for(Path(tmp_raw)).state_db_path
            state = StateStore(path, sanitizer=SecretSanitizer("safe"))
            try:
                thread, turn = self._thread_and_turn(state)
                with state._lock, state._conn:
                    state._conn.execute(
                        "insert into events(owner_hash, thread_id, turn_id, event_type, payload_json, raw_params_json, ambiguous, created_at) values (?, ?, ?, ?, ?, ?, 0, ?)",
                        ("owner_hash", thread["thread_id"], turn["turn_id"], "legacy", '{"detail":"Bearer dirty-event"}', '{"secret":"dirty-raw"}', "2026-01-01T00:00:00Z"),
                    )
                    state._conn.execute(
                        "insert into audit_logs(owner_hash, auth_principal_hash, action, payload_json, created_at) values (?, ?, ?, ?, ?)",
                        ("owner_hash", "principal_hash", "legacy", '{"detail":"api_key=dirty-audit"}', "2026-01-01T00:00:00Z"),
                    )
                    state._conn.execute(
                        "update turns set resolved_options_json = ?, error = ? where owner_hash = ? and thread_id = ? and turn_id = ?",
                        ('{"api_key":"dirty-options"}', "Bearer dirty-error", "owner_hash", thread["thread_id"], turn["turn_id"]),
                    )

                event = state.list_events("owner_hash", thread["thread_id"])[0]
                audit = state.list_audit_logs("owner_hash")[0]
                loaded_turn = state.get_turn("owner_hash", thread["thread_id"], turn["turn_id"])
                self.assertNotIn("dirty-event", str(event["payload"]))
                self.assertEqual(event["raw_params"]["secret"], REDACTED)
                self.assertNotIn("dirty-audit", str(audit["payload"]))
                self.assertNotIn("dirty-options", str(loaded_turn["resolved_options"]))
                self.assertNotIn("dirty-error", loaded_turn["error"])
            finally:
                state.close()

    def test_low_level_finalization_is_a_defensive_sanitization_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path, sanitizer=SecretSanitizer("safe"))
            try:
                thread, turn = self._thread_and_turn(state)
                finalized = state_transactions.finalize_turn(
                    state,
                    "owner_hash",
                    str(thread["thread_id"]),
                    str(turn["turn_id"]),
                    auth_principal_hash="principal_hash",
                    status="failed",
                    error="Bearer terminal-error",
                    public_message="api_key=terminal-public",
                    admin_message="access_token=terminal-admin",
                    event_type="turn.failed",
                    event_payload={"password": "terminal-event"},
                    raw_params={"credential": "terminal-raw"},
                    audit_action="turn.failure",
                    audit_payload={"cookie": "terminal-audit"},
                )
                self.assertTrue(finalized)
                with state._lock:
                    event_row = state._conn.execute(
                        "select payload_json, raw_params_json from events where turn_id = ?",
                        (turn["turn_id"],),
                    ).fetchone()
                    audit_row = state._conn.execute(
                        "select payload_json from audit_logs where turn_id = ?",
                        (turn["turn_id"],),
                    ).fetchone()
                    turn_row = state._conn.execute(
                        "select error, public_message, admin_message from turns where turn_id = ?",
                        (turn["turn_id"],),
                    ).fetchone()
                persisted = " ".join(str(value) for value in (*event_row, *audit_row, *turn_row))
                for canary in (
                    "terminal-error",
                    "terminal-public",
                    "terminal-admin",
                    "terminal-event",
                    "terminal-raw",
                    "terminal-audit",
                ):
                    self.assertNotIn(canary, persisted)
            finally:
                state.close()

    def test_turn_lookup_by_turn_id_is_owner_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path)
            try:
                profile = state.ensure_profile("principal_hash", "default")
                thread = state.create_thread(
                    "owner-a",
                    thread_id="thread-a",
                    auth_principal_hash="principal_hash",
                    auth_profile_instance_id=profile["instance_id"],
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                )
                turn = state.create_turn(
                    "owner-a",
                    thread["thread_id"],
                    auth_principal_hash="principal_hash",
                    auth_profile_instance_id=profile["instance_id"],
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                    mode="reject",
                    input_items=[{"type": "text", "text": "hello"}],
                    idempotency_key=None,
                    product_correlation_id=None,
                    status="starting",
                )
                self.assertEqual(
                    state.find_turn_by_turn_id("owner-a", turn["turn_id"])["turn_id"],
                    turn["turn_id"],
                )
                self.assertIsNone(state.find_turn_by_turn_id("owner-b", turn["turn_id"]))
            finally:
                state.close()

    def test_schema_version_is_recorded_and_newer_databases_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = config_for(Path(tmp_raw)).state_db_path
            state = StateStore(path)
            state.close()
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("pragma user_version").fetchone()[0], 3)
                connection.execute("pragma user_version = 4")
            with self.assertRaisesRegex(RuntimeError, "incompatible"):
                StateStore(path)

    def test_previous_schema_is_rejected_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = config_for(Path(tmp_raw)).state_db_path
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as connection:
                connection.execute("pragma user_version = 2")

            with self.assertRaisesRegex(RuntimeError, "incompatible"):
                StateStore(path)

    def test_unversioned_nonempty_database_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = config_for(Path(tmp_raw)).state_db_path
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as connection:
                connection.execute("create table old_state(id integer primary key)")

            with self.assertRaisesRegex(RuntimeError, "Unversioned state database is incompatible"):
                StateStore(path)

    def test_create_turn_returns_existing_turn_for_duplicate_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path)
            try:
                profile = state.ensure_profile("principal_hash", "default")
                thread = state.create_thread(
                    "owner_hash",
                    thread_id=None,
                    auth_principal_hash="principal_hash",
                    auth_profile_instance_id=profile["instance_id"],
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                )
                first = state.create_turn(
                    "owner_hash",
                    thread["thread_id"],
                    auth_principal_hash="principal_hash",
                    auth_profile_instance_id=profile["instance_id"],
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
                    auth_principal_hash="principal_hash",
                    auth_profile_instance_id=profile["instance_id"],
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
                profile = state.ensure_profile("principal_hash", "default")
                thread = state.create_thread(
                    "owner_hash",
                    thread_id="thread_1",
                    auth_principal_hash="principal_hash",
                    auth_profile_instance_id=profile["instance_id"],
                    profile="default",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                )
                turn = state.create_turn(
                    "owner_hash",
                    thread["thread_id"],
                    auth_principal_hash="principal_hash",
                    auth_profile_instance_id=profile["instance_id"],
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
