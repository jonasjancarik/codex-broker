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
                    product_thread_id=None,
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


if __name__ == "__main__":
    unittest.main()
