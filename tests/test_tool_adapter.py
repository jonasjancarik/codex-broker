from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from codex_broker.tool_adapter_mcp import ToolAdapterServer


class ToolAdapterTests(unittest.TestCase):
    def test_broker_hosted_adapter_does_not_follow_http_redirects(self) -> None:
        target_hits = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
                nonlocal target_hits
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                target_hits += 1
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_raw:
                path = Path(tmp_raw) / "adapter.json"
                path.write_text(
                    json.dumps(
                        {
                            "tools": [
                                {
                                    "name": "host.redirect",
                                    "endpoint": f"http://127.0.0.1:{server.server_port}/redirect",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                result = ToolAdapterServer(path).handle(
                    {"id": 1, "method": "tools/call", "params": {"name": "host.redirect"}}
                )
                self.assertTrue(result["result"]["isError"])
                self.assertEqual(target_hits, 0)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(1)

    def test_broker_hosted_adapter_proxies_tool_call_to_host_endpoint(self) -> None:
        received: list[dict[str, Any]] = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b'{"ok":true}'

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            received.append(json.loads(req.data.decode("utf-8")))
            self.assertEqual(req.get_header("X-host-tool-key"), "resolved-key")
            self.assertEqual(req.get_header("X-codex-broker-tool"), "host.search")
            self.assertEqual(timeout, 30)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp_raw:
            path = Path(tmp_raw) / "adapter.json"
            path.write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "host.search",
                                "description": "Search",
                                "inputSchema": {"type": "object"},
                                "endpoint": "http://host-app.internal/tool",
                                "headers": {"X-Host-Tool-Key": "env:HOST_TOOL_KEY"},
                                "context": {"capability": "evidence-search"},
                                "approvalPolicy": "on-request",
                                "scope": "profile",
                                "networkPolicy": {"mode": "host-allowlist", "matchedPrefix": "http://127.0.0.1"},
                            }
                        ],
                        "brokerContext": {
                            "ownerHash": "owner_hash_1",
                            "profile": "default",
                            "threadId": "thread_1",
                            "turnId": "turn_1",
                            "hostApp": "host-app",
                            "configProfile": "default",
                            "productCorrelationId": "product-correlation-1",
                        },
                    }
                ),
                encoding="utf-8",
            )
            adapter = ToolAdapterServer(path)
            listed = adapter.handle({"id": 1, "method": "tools/list"})
            self.assertEqual(listed["result"]["tools"][0]["name"], "host.search")
            with patch.dict(os.environ, {"HOST_TOOL_KEY": "resolved-key"}), patch(
                "codex_broker.tool_adapter_mcp.open_hosted_tool", fake_urlopen
            ):
                result = adapter.handle({"id": 2, "method": "tools/call", "params": {"name": "host.search", "arguments": {"q": "abc"}}})
            self.assertFalse(result["result"]["isError"])
            self.assertEqual(received[0]["tool"], "host.search")
            self.assertEqual(received[0]["arguments"], {"q": "abc"})
            self.assertEqual(received[0]["context"]["broker"]["hostApp"], "host-app")
            self.assertEqual(received[0]["context"]["broker"]["ownerHash"], "owner_hash_1")
            self.assertEqual(received[0]["context"]["broker"]["productCorrelationId"], "product-correlation-1")
            self.assertNotIn("ownerId", received[0]["context"]["broker"])
            self.assertEqual(received[0]["context"]["tool"], {"capability": "evidence-search"})
            self.assertEqual(
                received[0]["context"]["policy"],
                {
                    "approvalPolicy": "on-request",
                    "scope": "profile",
                    "networkPolicy": {"mode": "host-allowlist", "matchedPrefix": "http://127.0.0.1"},
                },
            )
            with patch.dict(os.environ, {}, clear=True):
                missing = adapter.handle({"id": 3, "method": "tools/call", "params": {"name": "host.search", "arguments": {}}})
            self.assertTrue(missing["result"]["isError"])
            self.assertIn("HOST_TOOL_KEY", missing["result"]["content"][0]["text"])

    def test_broker_hosted_adapter_passes_through_mcp_tool_results(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return json.dumps(
                    {
                        "content": [{"type": "text", "text": "Saved /workspaces/app/file.pdf"}],
                        "structuredContent": {
                            "artifacts": [
                                {"path": "/workspaces/app/file.pdf", "mimeType": "application/pdf"}
                            ]
                        },
                    }
                ).encode("utf-8")

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp_raw:
            path = Path(tmp_raw) / "adapter.json"
            path.write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "host.attachment",
                                "endpoint": "http://host-app.internal/tool",
                                "headers": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            adapter = ToolAdapterServer(path)
            with patch("codex_broker.tool_adapter_mcp.open_hosted_tool", fake_urlopen):
                result = adapter.handle({"id": 1, "method": "tools/call", "params": {"name": "host.attachment", "arguments": {}}})

            self.assertEqual(result["result"]["content"][0]["text"], "Saved /workspaces/app/file.pdf")
            self.assertEqual(
                result["result"]["structuredContent"],
                {"artifacts": [{"path": "/workspaces/app/file.pdf", "mimeType": "application/pdf"}]},
            )

    def test_broker_hosted_adapter_rejects_oversized_responses(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "text/plain"}

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b"too-large"

        with tempfile.TemporaryDirectory() as tmp_raw:
            path = Path(tmp_raw) / "adapter.json"
            path.write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "host.small",
                                "endpoint": "http://host-app.internal/tool",
                                "maxResponseBytes": 3,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            adapter = ToolAdapterServer(path)
            with patch("codex_broker.tool_adapter_mcp.open_hosted_tool", return_value=FakeResponse()):
                result = adapter.handle({"id": 1, "method": "tools/call", "params": {"name": "host.small"}})

            self.assertTrue(result["result"]["isError"])
            self.assertIn("exceeded 3 bytes", result["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
