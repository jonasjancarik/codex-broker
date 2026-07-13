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
    def test_account_methods_scope_requests_to_owner_and_profile(self) -> None:
        seen: list[dict[str, Any]] = []

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            seen.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "body": json.loads(req.data.decode("utf-8")) if req.data else None,
                }
            )
            return FakeResponse(b'{"ownerHash":"hash","profile":"work"}')

        client = CodexBrokerClient("http://broker.internal")
        with patch("urllib.request.urlopen", fake_urlopen):
            client.account_usage("owner/a", profile="work")
            client.account_rate_limits("owner/a", profile="work")
            client.consume_rate_limit_reset_credit("owner/a", "reset-123", profile="work")

        self.assertEqual([request["method"] for request in seen], ["GET", "GET", "POST"])
        self.assertIn("/v1/owners/owner%2Fa/auth/usage?profile=work", seen[0]["url"])
        self.assertIn("/v1/owners/owner%2Fa/auth/rate-limits?profile=work", seen[1]["url"])
        self.assertIn("/v1/owners/owner%2Fa/auth/rate-limit-reset-credit/consume", seen[2]["url"])
        self.assertEqual(seen[2]["body"], {"profile": "work", "idempotencyKey": "reset-123"})

    def test_model_list_sends_auth_scope_and_capability_filters(self) -> None:
        seen: dict[str, Any] = {}

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            return FakeResponse(b'{"models":[],"nextCursor":null}')

        client = CodexBrokerClient("http://broker.internal")
        with patch("urllib.request.urlopen", fake_urlopen):
            result = client.list_models(
                "owner/a",
                profile="work",
                auth_principal_id="shared/account",
                cursor="page/2",
                limit=25,
                include_hidden=True,
            )

        self.assertEqual(result["models"], [])
        self.assertEqual(seen["method"], "GET")
        self.assertIn("/v1/owners/owner%2Fa/auth/models", seen["url"])
        self.assertIn("profile=work", seen["url"])
        self.assertIn("authPrincipalId=shared%2Faccount", seen["url"])
        self.assertIn("cursor=page%2F2", seen["url"])
        self.assertIn("limit=25", seen["url"])
        self.assertIn("includeHidden=true", seen["url"])

    def test_auth_principal_selection_is_explicit_across_auth_and_thread_methods(self) -> None:
        seen: list[dict[str, Any]] = []

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            seen.append(
                {
                    "url": req.full_url,
                    "body": json.loads(req.data.decode("utf-8")) if req.data else None,
                }
            )
            return FakeResponse(b'{"ok":true}')

        client = CodexBrokerClient("http://broker.internal")
        with patch("urllib.request.urlopen", fake_urlopen):
            client.list_auth_profiles("owner/a", auth_principal_id="shared/account")
            client.auth_status("owner/a", profile="work", auth_principal_id="shared/account")
            client.probe_auth("owner/a", profile="work", auth_principal_id="shared/account")
            client.invalidate_auth_runtime("owner/a", profile="work", auth_principal_id="shared/account")
            client.create_thread(
                "owner/a",
                {"threadId": "thread-1"},
                profile="work",
                auth_principal_id="shared/account",
            )
            client.start_turn(
                "owner/a",
                "thread-1",
                {"input": [{"type": "text", "text": "hello"}]},
                profile="work",
                auth_principal_id="shared/account",
            )

        self.assertIn("authPrincipalId=shared%2Faccount", seen[0]["url"])
        self.assertIn("profile=work", seen[1]["url"])
        self.assertIn("authPrincipalId=shared%2Faccount", seen[1]["url"])
        for request in seen[2:]:
            self.assertEqual(request["body"]["authPrincipalId"], "shared/account")
            self.assertEqual(request["body"]["profile"], "work")

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
