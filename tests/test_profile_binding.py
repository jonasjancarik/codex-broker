from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codex_broker.scheduler import ConflictError
from codex_broker.services import BrokerServices
from test_broker import config_for, wait_turn


class ThreadAuthBindingTests(unittest.TestCase):
    def test_repeated_create_and_turn_profile_are_consistency_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"threadId": "chat-1", "profile": "work/team"},
                )
                same = services.scheduler.create_thread(
                    "owner-a",
                    {"threadId": "chat-1", "profile": "work_team"},
                )
                self.assertEqual(same["threadId"], thread["threadId"])
                with self.assertRaisesRegex(ConflictError, "bound to auth profile"):
                    services.scheduler.create_thread(
                        "owner-a",
                        {"threadId": "chat-1", "profile": "other"},
                    )

                turn = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {
                        "profile": "work/team",
                        "idempotencyKey": "turn-1",
                        "input": [{"type": "text", "text": "same"}],
                    },
                )
                wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])
                with self.assertRaisesRegex(ConflictError, "bound to auth profile"):
                    services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {
                            "profile": "other",
                            "idempotencyKey": "turn-1",
                            "input": [{"type": "text", "text": "must not return cached turn"}],
                        },
                    )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_profile_mismatch_is_rejected_before_steering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.3))
            try:
                thread = services.scheduler.create_thread("owner-a", {"profile": "work"})
                active = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "active"}]},
                )
                with self.assertRaisesRegex(ConflictError, "bound to auth profile"):
                    services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {
                            "profile": "other",
                            "mode": "steer",
                            "input": [{"type": "text", "text": "must not steer"}],
                        },
                    )
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], active["turnId"])["status"], "completed")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_concurrent_duplicate_thread_creates_cannot_cross_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            barrier = threading.Barrier(2)

            def create(profile: str) -> tuple[str, str]:
                barrier.wait()
                try:
                    thread = services.scheduler.create_thread(
                        "owner-a",
                        {"threadId": "raced", "profile": profile},
                    )
                    return "created", thread["profile"]
                except ConflictError as exc:
                    return "conflict", str(exc)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(create, ("work", "other")))
                self.assertEqual(sorted(result[0] for result in results), ["conflict", "created"])
                stored = services.state.get_thread(services.auth.hash_owner("owner-a"), "raced")
                assert stored is not None
                self.assertIn(stored["profile"], {"work", "other"})
                self.assertEqual(
                    [row["profile"] for row in services.state.list_profiles(services.auth.hash_owner("owner-a"))],
                    [stored["profile"]],
                )
            finally:
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
