from __future__ import annotations

import io
import json
from types import SimpleNamespace
import unittest

from codex_broker.http_api import BrokerHandler


class ImageBodyLimitTests(unittest.TestCase):
    def test_input_routes_accept_json_larger_than_old_limit(self) -> None:
        payload = {"input": "x" * 1_000_000}
        body = json.dumps(payload).encode()
        for path in (
            "/v1/responses",
            "/v1/chat/completions",
            "/v1/owners/alice/threads/thread_1/turns",
            "/v1/owners/alice/threads/thread_1/turns/turn_1/steer",
        ):
            with self.subTest(path=path):
                handler = SimpleNamespace(
                    path=path, headers={"Content-Length": str(len(body))}, rfile=io.BytesIO(body)
                )
                self.assertEqual(BrokerHandler._read_json(handler), payload)

    def test_oversized_bodies_close_connection_without_reading(self) -> None:
        for path, limit in (
            ("/v1/responses", 32 * 1024 * 1024),
            ("/v1/chat/completions", 32 * 1024 * 1024),
            ("/v1/owners/alice/threads/thread_1/turns", 32 * 1024 * 1024),
            ("/v1/owners/alice/threads/thread_1/turns/turn_1/steer", 32 * 1024 * 1024),
            ("/v1/owners/alice/threads", 1_000_000),
            ("/v1/bundles/inline", 1_000_000),
            ("/v1/responses/resp_1/cancel", 1_000_000),
        ):
            with self.subTest(path=path):
                handler = SimpleNamespace(
                    path=path,
                    headers={"Content-Length": str(limit + 1)},
                    rfile=io.BytesIO(b"{}"),
                    close_connection=False,
                )
                with self.assertRaisesRegex(ValueError, "too large"):
                    BrokerHandler._read_json(handler)
                self.assertTrue(handler.close_connection)
                self.assertEqual(handler.rfile.tell(), 0)
