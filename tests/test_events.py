from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_broker.events import normalize_app_server_event
from codex_broker.http_api import BrokerHandler, BrokerServices
from codex_broker.scheduler import NotFoundError

try:
    from test_broker import config_for
except ModuleNotFoundError:  # pragma: no cover - direct module invocation path.
    from tests.test_broker import config_for


class EventStreamTests(unittest.TestCase):
    def test_sse_stream_uses_http_1_1_chunked_body_framing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"cwd": str(services.config.allowed_workspace_roots[0])},
                )
                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = services
                handler.send_response = mock.Mock()
                handler.send_header = mock.Mock()
                handler.end_headers = mock.Mock()
                handler._write_raw = mock.Mock(return_value=False)

                with mock.patch("codex_broker.http_api.time.monotonic", side_effect=[0.0, 11.0]):
                    handler._sse_events("owner-a", thread["threadId"], {})

                self.assertEqual(handler.protocol_version, "HTTP/1.1")
                handler.send_header.assert_any_call("Connection", "keep-alive")
                handler.send_header.assert_any_call("Transfer-Encoding", "chunked")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_sse_stream_writes_each_message_as_an_http_chunk(self) -> None:
        handler = BrokerHandler.__new__(BrokerHandler)
        handler.wfile = io.BytesIO()

        self.assertTrue(handler._write_raw("data: hello\n\n"))

        payload = b"data: hello\n\n"
        expected = f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n"
        self.assertEqual(handler.wfile.getvalue(), expected)

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


class AppServerEventNormalizationTests(unittest.TestCase):
    def normalize(self, method: str, params: dict) -> tuple[str, dict]:
        return normalize_app_server_event(
            method,
            params,
            codex_thread_id="codex_thread_1",
            codex_turn_id="codex_turn_1",
        )

    def test_plan_goal_and_settings_events_are_normalized(self) -> None:
        event_type, payload = self.normalize(
            "turn/plan/updated",
            {
                "threadId": "codex_thread_1",
                "turnId": "codex_turn_1",
                "explanation": "Need to inspect first.",
                "plan": [{"step": "Inspect", "status": "inProgress"}],
            },
        )
        self.assertEqual(event_type, "plan.updated")
        self.assertEqual(payload["plan"][0]["status"], "inProgress")

        event_type, payload = self.normalize("item/plan/delta", {"itemId": "plan_1", "delta": "Inspect"})
        self.assertEqual(event_type, "plan.delta")
        self.assertEqual(payload["turnId"], "codex_turn_1")

        event_type, payload = self.normalize(
            "thread/goal/updated",
            {"goal": {"objective": "Ship", "status": "active"}},
        )
        self.assertEqual(event_type, "goal.updated")
        self.assertEqual(payload["goal"]["objective"], "Ship")

        event_type, payload = self.normalize(
            "thread/settings/updated",
            {"threadSettings": {"collaborationMode": {"mode": "plan", "settings": {}}}},
        )
        self.assertEqual(event_type, "thread.settings.updated")
        self.assertEqual(payload["collaborationMode"]["mode"], "plan")

    def test_review_and_approval_events_are_normalized(self) -> None:
        event_type, payload = self.normalize(
            "item/completed",
            {"item": {"id": "review_1", "type": "enteredReviewMode", "review": "uncommitted"}},
        )
        self.assertEqual(event_type, "review.entered")
        self.assertEqual(payload["item"]["review"], "uncommitted")

        event_type, payload = self.normalize(
            "item/permissions/requestApproval",
            {"itemId": "perm_1", "permissions": {"network": None, "fileSystem": None}},
        )
        self.assertEqual(event_type, "approval.requested")
        self.assertEqual(payload["kind"], "permissions")

        event_type, payload = self.normalize(
            "approval/resolved",
            {"method": "execCommandApproval", "decision": "denied"},
        )
        self.assertEqual(event_type, "approval.resolved")
        self.assertEqual(payload["kind"], "execCommand")

    def test_user_input_and_mcp_elicitation_events_are_normalized(self) -> None:
        event_type, payload = self.normalize(
            "item/tool/requestUserInput",
            {"itemId": "input_1", "questions": [{"id": "choice"}], "autoResolutionMs": 60000},
        )
        self.assertEqual(event_type, "user_input.requested")
        self.assertEqual(payload["questions"][0]["id"], "choice")

        event_type, payload = self.normalize(
            "mcpServer/elicitation/request",
            {
                "serverName": "host",
                "mode": "form",
                "message": "Choose a workspace.",
                "requestedSchema": {"type": "object"},
            },
        )
        self.assertEqual(event_type, "mcp.elicitation.requested")
        self.assertEqual(payload["serverName"], "host")

        event_type, payload = self.normalize(
            "mcpServer/elicitation/resolved",
            {"method": "mcpServer/elicitation/request", "action": "decline"},
        )
        self.assertEqual(event_type, "mcp.elicitation.resolved")
        self.assertEqual(payload["action"], "decline")


if __name__ == "__main__":
    unittest.main()
