from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_broker.state import StateStore
from test_broker import config_for


class StateStoreTests(unittest.TestCase):
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
