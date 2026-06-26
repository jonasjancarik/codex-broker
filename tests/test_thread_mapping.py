from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_broker.http_api import BrokerServices
from test_broker import config_for, wait_turn


class ProductThreadMappingTests(unittest.TestCase):
    def test_product_thread_id_is_owner_scoped_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                body = {
                    "productThreadId": "chat-123",
                    "hostApp": "chat-app",
                    "cwd": str(services.config.allowed_workspace_roots[0]),
                }
                first = services.scheduler.create_thread("owner-a", body)
                repeat = services.scheduler.create_thread("owner-a", body)
                other_owner = services.scheduler.create_thread("owner-b", body)

                self.assertEqual(first["threadId"], repeat["threadId"])
                self.assertEqual(first["productThreadId"], "chat-123")
                self.assertEqual(repeat["productThreadId"], "chat-123")
                self.assertNotEqual(first["threadId"], other_owner["threadId"])
                self.assertEqual(other_owner["productThreadId"], "chat-123")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_product_thread_id_mapping_keeps_codex_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {
                        "productThreadId": "document-job-456",
                        "cwd": str(services.config.allowed_workspace_roots[0]),
                    },
                )
                turn = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "map this thread"}]},
                )
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"], "completed")

                mapped = services.scheduler.create_thread(
                    "owner-a",
                    {
                        "productThreadId": "document-job-456",
                        "cwd": str(services.config.allowed_workspace_roots[0]),
                    },
                )
                self.assertEqual(mapped["threadId"], thread["threadId"])
                self.assertEqual(mapped["productThreadId"], "document-job-456")
                self.assertTrue(str(mapped["codexThreadId"]).startswith("thr_fake_"))
            finally:
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
