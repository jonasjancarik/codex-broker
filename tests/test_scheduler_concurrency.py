from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from codex_broker.http_api import BrokerServices
from test_broker import config_for, wait_interaction, wait_turn


class SchedulerConcurrencyTests(unittest.TestCase):
    def test_pending_interaction_does_not_block_other_app_server_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw), turn_delay=0.01), host_response_timeout_seconds=10)
            os.environ["FAKE_CODEX_REQUEST_APPROVAL"] = "1"
            services = BrokerServices.build(config)
            try:
                first_thread = services.scheduler.create_thread("owner-a", {"cwd": str(config.allowed_workspace_roots[0])})
                second_thread = services.scheduler.create_thread("owner-a", {"cwd": str(config.allowed_workspace_roots[0])})
                first = services.scheduler.start_turn(
                    "owner-a",
                    first_thread["threadId"],
                    {"input": [{"type": "text", "text": "first"}]},
                )
                owner_hash = services.auth.hash_owner("owner-a")
                first_pending = wait_interaction(services, owner_hash, first_thread["threadId"], timeout=1)

                second = services.scheduler.start_turn(
                    "owner-a",
                    second_thread["threadId"],
                    {"input": [{"type": "text", "text": "second"}]},
                )
                second_pending = wait_interaction(services, owner_hash, second_thread["threadId"], timeout=1)

                for thread, turn, pending in (
                    (first_thread, first, first_pending),
                    (second_thread, second, second_pending),
                ):
                    services.scheduler.resolve_interaction(
                        "owner-a",
                        thread["threadId"],
                        turn["turnId"],
                        pending["interaction_id"],
                        {"decision": "accept"},
                    )
                    self.assertEqual(
                        wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"],
                        "completed",
                    )
            finally:
                os.environ.pop("FAKE_CODEX_REQUEST_APPROVAL", None)
                services.scheduler.shutdown("interrupt", 1)
                services.pool.close_all()
                services.state.close()

    def test_drain_timeout_does_not_start_another_queued_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=1))
            try:
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                first = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "first"}]},
                )
                second = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "second"}], "mode": "queue"},
                )
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    if services.scheduler.get_turn("owner-a", thread["threadId"], first["turnId"])["status"] == "running":
                        break
                    time.sleep(0.01)

                services.scheduler.shutdown("drain", timeout_seconds=0.05)

                self.assertEqual(
                    services.scheduler.get_turn("owner-a", thread["threadId"], second["turnId"])["status"],
                    "interrupted",
                )
                self.assertIsNone(
                    services.scheduler.get_turn("owner-a", thread["threadId"], second["turnId"])["startedAt"]
                )
            finally:
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
