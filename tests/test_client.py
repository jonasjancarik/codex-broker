from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from codex_broker.client import CodexBrokerClient


class FakeResponse:
    def __init__(self, body: bytes | list[bytes]) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def read(self) -> bytes:
        assert isinstance(self.body, bytes)
        return self.body

    def __iter__(self) -> Any:
        assert isinstance(self.body, list)
        return iter(self.body)


class CodexBrokerClientTests(unittest.TestCase):
    def test_start_turn_posts_authorized_json(self) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["headers"] = dict(req.header_items())
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["timeout"] = timeout
            return FakeResponse(b'{"turnId":"turn_1","status":"running"}')

        client = CodexBrokerClient("http://broker.internal", internal_key="secret", timeout_seconds=7)
        with patch("urllib.request.urlopen", fake_urlopen):
            response = client.start_turn(
                "owner/a",
                "thread 1",
                {"input": [{"type": "text", "text": "hello"}], "mode": "reject"},
            )

        self.assertEqual(response["turnId"], "turn_1")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["timeout"], 7)
        self.assertIn("/v1/owners/owner%2Fa/threads/thread%201/turns", seen["url"])
        self.assertEqual(seen["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(seen["body"]["mode"], "reject")

    def test_logout_can_request_profile_deletion(self) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse(b'{"state":"deleted","deleted":true}')

        client = CodexBrokerClient("http://broker.internal")
        with patch("urllib.request.urlopen", fake_urlopen):
            response = client.logout("owner", profile="work", delete_profile=True)

        self.assertTrue(response["deleted"])
        self.assertEqual(seen["method"], "POST")
        self.assertIn("/v1/owners/owner/auth/logout", seen["url"])
        self.assertEqual(seen["body"], {"profile": "work", "deleteProfile": True})

    def test_list_audit_logs_sends_filters(self) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            return FakeResponse(b'{"auditLogs":[]}')

        client = CodexBrokerClient("http://broker.internal")
        with patch("urllib.request.urlopen", fake_urlopen):
            response = client.list_audit_logs(
                "owner/a",
                profile="work",
                action="turn.start",
                thread_id="thread 1",
                turn_id="turn 1",
                limit=25,
            )

        self.assertEqual(response["auditLogs"], [])
        self.assertEqual(seen["method"], "GET")
        self.assertIn("/v1/owners/owner%2Fa/audit-logs", seen["url"])
        self.assertIn("profile=work", seen["url"])
        self.assertIn("action=turn.start", seen["url"])
        self.assertIn("threadId=thread+1", seen["url"])
        self.assertIn("turnId=turn+1", seen["url"])
        self.assertIn("limit=25", seen["url"])

    def test_resolve_interaction_posts_host_response(self) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse(b'{"interactionId":"int_1","status":"resolved"}')

        client = CodexBrokerClient("http://broker.internal")
        with patch("urllib.request.urlopen", fake_urlopen):
            response = client.resolve_interaction("owner/a", "thread 1", "turn 1", "int 1", {"decision": "accept"})

        self.assertEqual(response["status"], "resolved")
        self.assertEqual(seen["method"], "POST")
        self.assertIn("/v1/owners/owner%2Fa/threads/thread%201/turns/turn%201/interactions/int%201/resolve", seen["url"])
        self.assertEqual(seen["body"], {"decision": "accept"})

    def test_stream_events_parses_sse_payloads(self) -> None:
        body = [
            b"id: 12\n",
            b"event: message.delta\n",
            b'data: {"payload":{"delta":"hi"}}\n',
            b"\n",
        ]

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            self.assertIn("after=5", req.full_url)
            self.assertIn("turnId=turn_1", req.full_url)
            return FakeResponse(body)

        client = CodexBrokerClient("http://broker.internal", internal_key="secret")
        with patch("urllib.request.urlopen", fake_urlopen):
            events = list(client.stream_events("owner", "thread", after=5, turn_id="turn_1"))

        self.assertEqual(events[0]["id"], 12)
        self.assertEqual(events[0]["type"], "message.delta")
        self.assertEqual(events[0]["payload"], {"delta": "hi"})


if __name__ == "__main__":
    unittest.main()
