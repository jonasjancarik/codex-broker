from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_broker.http_api import BrokerHandler, BrokerServices
from codex_broker.scheduler import NotFoundError
from test_broker import config_for


class EventStreamTests(unittest.TestCase):
    def test_sse_stream_rejects_unknown_thread_before_opening_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = services

                with self.assertRaisesRegex(NotFoundError, "Thread not found"):
                    handler._sse_events("owner-a", "missing-thread", {})
            finally:
                services.pool.close_all()
                services.state.close()

    def test_sse_stream_rejects_unknown_turn_filter_before_opening_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"cwd": str(services.config.allowed_workspace_roots[0])},
                )
                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = services

                with self.assertRaisesRegex(NotFoundError, "Turn not found"):
                    handler._sse_events("owner-a", thread["threadId"], {"turnId": ["missing-turn"]})
            finally:
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
