from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stderr
from datetime import datetime, timezone
from http import HTTPStatus
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from codex_broker.auth import AuthManager, DeviceAuthSession, extract_expires_at
from codex_broker.app_server import AppServerClient, AppServerError, AppServerPool
from codex_broker.bundles import BundleError, BundleRegistry
from codex_broker.config import BrokerConfig
from codex_broker.http_api import BrokerHandler, BrokerServices, is_unauthenticated_path, metric_path_template
from codex_broker.scheduler import ActiveTurnError, BrokerTurnContext, ConflictError, TurnScheduler
from codex_broker.state import StateStore
from codex_broker.util import json_log


ROOT = Path(__file__).resolve().parent
FAKE_CODEX = ROOT / "fake_codex.py"


def config_for(tmp: Path, *, turn_delay: float = 0.01) -> BrokerConfig:
    os.environ["FAKE_CODEX_TURN_DELAY"] = str(turn_delay)
    os.environ.pop("FAKE_CODEX_CRASH_ON_TURN", None)
    os.environ.pop("FAKE_CODEX_CRASH_ON_TURN_ONCE", None)
    os.environ.pop("FAKE_CODEX_HANG_ON_TURN_START_ONCE", None)
    os.environ.pop("FAKE_CODEX_REQUEST_APPROVAL", None)
    os.environ.pop("FAKE_CODEX_DEVICE_AUTH_DELAY", None)
    os.environ.pop("FAKE_CODEX_DEVICE_AUTH_SECRET_OUTPUT", None)
    workspace = tmp / "workspace"
    bundles = tmp / "bundles"
    workspace.mkdir(parents=True, exist_ok=True)
    bundles.mkdir(parents=True, exist_ok=True)
    return BrokerConfig(
        host="127.0.0.1",
        port=0,
        data_dir=tmp / "data",
        internal_key="test-key",
        allow_unauthenticated=False,
        owner_hash_secret="hash-secret",
        allowed_workspace_roots=(workspace.resolve(),),
        allowed_bundle_roots=(bundles.resolve(),),
        max_active_turns=0,
        pool_idle_ttl_seconds=900,
        codex_command=(sys.executable, str(FAKE_CODEX)),
        allowed_tool_commands=("python",),
        allowed_hosted_tool_url_prefixes=("http://127.0.0.1", "http://localhost", "http://host.docker.internal"),
        credential_store="file",
        request_timeout_seconds=5,
        turn_timeout_seconds=5,
        enable_inline_bundles=False,
        inline_bundle_max_bytes=262_144,
        debug_raw_events=True,
        raw_event_retention_seconds=7 * 24 * 60 * 60,
        json_logs=False,
        shutdown_mode="interrupt",
        shutdown_drain_timeout_seconds=1,
    )


def wait_turn(services: BrokerServices, owner: str, thread_id: str, turn_id: str, timeout: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = services.scheduler.get_turn(owner, thread_id, turn_id)
        if turn["status"] in {"completed", "failed", "timed_out", "interrupted"}:
            return turn
        time.sleep(0.02)
    raise AssertionError(f"turn {turn_id} did not finish")


def wait_metric(services: BrokerServices, name: str, value: int, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if services.scheduler.metrics().get(name) == value:
            return
        time.sleep(0.02)
    raise AssertionError(f"metric {name} did not reach {value}: {services.scheduler.metrics()}")


class BrokerTests(unittest.TestCase):
    def test_json_log_is_structured_and_redacts_secrets(self) -> None:
        stream = io.StringIO()
        with redirect_stderr(stream):
            json_log(
                True,
                "unit.test",
                ownerHash="owner_hash",
                threadId="thread_1",
                accessToken="secret-token",
                nested={"Authorization": "Bearer live-token", "message": "ok"},
            )
        line = stream.getvalue().strip()
        payload = json.loads(line)
        self.assertEqual(payload["event"], "unit.test")
        self.assertEqual(payload["ownerHash"], "owner_hash")
        self.assertEqual(payload["threadId"], "thread_1")
        self.assertEqual(payload["accessToken"], "<redacted>")
        self.assertEqual(payload["nested"]["Authorization"], "<redacted>")
        self.assertEqual(payload["nested"]["message"], "ok")

    def test_owner_auth_home_is_hashed_and_file_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            auth = AuthManager(config, state)
            owner_hash = auth.hash_owner("user@example.com")
            home = auth.profile_home(owner_hash, "default")
            self.assertNotIn("user@example.com", str(home))
            self.assertTrue((home / "config.toml").read_text(encoding="utf-8").strip().endswith('"file"'))
            result = auth.login_api_key("user@example.com", "sk-test", "default")
            self.assertEqual(result["state"], "authenticated")
            self.assertTrue((home / "auth.json").exists())
            logout = auth.logout("user@example.com", "default")
            self.assertEqual(logout["state"], "unauthenticated")
            other_home = auth.profile_home(owner_hash, "throwaway")
            self.assertTrue(other_home.exists())
            deleted = auth.logout("user@example.com", "throwaway", delete_profile=True)
            self.assertEqual(deleted["state"], "deleted")
            self.assertTrue(deleted["deleted"])
            self.assertFalse(other_home.parent.exists())
            audit_actions = [entry["action"] for entry in state.list_audit_logs(owner_hash)]
            self.assertIn("auth.api_key.success", audit_actions)
            self.assertIn("auth.logout", audit_actions)
            self.assertIn("auth.profile.delete", audit_actions)

    def test_device_auth_public_shape_includes_expiry_when_available(self) -> None:
        now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            extract_expires_at("Open the URL. This code expires in 15 minutes.", now=now),
            "2026-06-25T12:15:00Z",
        )
        session = DeviceAuthSession(
            session_id="session_1",
            owner_hash="owner_hash",
            profile="default",
            command=["codex", "login", "--device-auth"],
            started_at="2026-06-25T12:00:00Z",
            updated_at="2026-06-25T12:00:00Z",
            login_url="https://example.test/device",
            user_code="ABCD-1234",
            expires_at="2026-06-25T12:15:00Z",
        )
        self.assertEqual(session.public()["expiresAt"], "2026-06-25T12:15:00Z")
        session.state = "completed"
        self.assertIsNone(session.public()["expiresAt"])

    def test_device_auth_flow_exposes_login_fields_without_token_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            os.environ["FAKE_CODEX_DEVICE_AUTH_DELAY"] = "0.2"
            os.environ["FAKE_CODEX_DEVICE_AUTH_SECRET_OUTPUT"] = "1"
            state = StateStore(config.state_db_path)
            auth = AuthManager(config, state)
            try:
                started = auth.start_device_auth("user@example.com", "default")
                self.assertEqual(started["profile"], "default")
                owner_hash = auth.hash_owner("user@example.com")
                deadline = time.monotonic() + 5
                waiting: dict[str, Any] | None = None
                while time.monotonic() < deadline:
                    session = auth._session(owner_hash, "default")
                    if session and session.user_code:
                        waiting = session.public()
                        break
                    time.sleep(0.01)
                assert waiting is not None
                self.assertEqual(waiting["loginUrl"], "https://example.test/device")
                self.assertEqual(waiting["userCode"], "ABCD-1234")
                self.assertIsNotNone(waiting["expiresAt"])
                rendered_output = "\n".join(waiting["output"])
                self.assertIn("access_token=<redacted>", rendered_output)
                self.assertNotIn("secret-device-token", rendered_output)

                authenticated: dict[str, Any] | None = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = auth.status("user@example.com", "default")
                    if status["state"] == "authenticated":
                        authenticated = status
                        break
                    time.sleep(0.01)
                assert authenticated is not None
                self.assertTrue(authenticated["authFilePresent"])
                actions: list[str] = []
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    actions = [entry["action"] for entry in state.list_audit_logs(owner_hash)]
                    if "auth.device.success" in actions:
                        break
                    time.sleep(0.01)
                self.assertIn("auth.device.start", actions)
                self.assertIn("auth.device.success", actions)
            finally:
                os.environ.pop("FAKE_CODEX_DEVICE_AUTH_DELAY", None)
                os.environ.pop("FAKE_CODEX_DEVICE_AUTH_SECRET_OUTPUT", None)

    def test_bundle_validates_paths_and_materializes_hosted_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            registry = BundleRegistry(config, state)
            skill = config.allowed_bundle_roots[0] / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill.\n---\n", encoding="utf-8")
            prompt = config.allowed_bundle_roots[0] / "prompts" / "legacy.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("Legacy host prompt.", encoding="utf-8")
            bundle_dir = config.allowed_bundle_roots[0] / "demo-bundle"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                json.dumps(
                    {
                        "id": "demo-bundle",
                        "instructions": ["Use host tools only through their declared adapter."],
                        "skills": [{"name": "demo", "source": {"type": "mount", "path": str(skill)}}],
                        "prompts": [{"name": "legacy", "source": {"type": "mount", "path": str(prompt)}}],
                        "mcpServers": [
                            {
                                "name": "host_mcp",
                                "command": "python",
                                "env": {"PUBLIC_FLAG": "1", "MCP_API_KEY": "env:MCP_SECRET_SOURCE"},
                            }
                        ],
                        "tools": [
                            {
                                "name": "host.search",
                                "type": "broker-hosted",
                                "description": "Host-owned search semantics.",
                                "inputSchema": {"type": "object"},
                                "context": {"capability": "evidence-search"},
                                "policy": {"approval": "on-request", "scope": "profile"},
                                "http": {
                                    "url": "http://127.0.0.1:9/search",
                                    "headers": {"X-Host-Tool-Key": "env:HOST_TOOL_KEY"},
                                },
                            }
                        ],
                        "allowedPaths": [str(config.allowed_workspace_roots[0])],
                    }
                ),
                encoding="utf-8",
            )
            bundle = registry.resolve("demo-bundle")
            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle.prompts[0].name, "legacy")
            overlay = registry.materialize(
                bundle,
                "turn_test",
                adapter_context={
                    "ownerHash": "owner_hash_1",
                    "threadId": "thread_1",
                    "turnId": "turn_1",
                    "hostApp": "host-app",
                    "configProfile": "default",
                    "profile": "default",
                    "productCorrelationId": "product-correlation-1",
                },
            )
            self.assertEqual((overlay / "prompts" / "legacy.md").read_text(encoding="utf-8"), "Legacy host prompt.")
            mcp_config = (overlay / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertIn("PUBLIC_FLAG", mcp_config)
            self.assertNotIn("MCP_API_KEY", mcp_config)
            self.assertNotIn("MCP_SECRET_SOURCE", mcp_config)
            self.assertTrue((overlay / "tool-adapters.json").exists())
            adapter_config = json.loads((overlay / "tool-adapters.json").read_text(encoding="utf-8"))
            self.assertEqual(adapter_config["tools"][0]["name"], "host.search")
            self.assertEqual(adapter_config["tools"][0]["context"], {"capability": "evidence-search"})
            self.assertEqual(adapter_config["tools"][0]["headers"], {"X-Host-Tool-Key": "env:HOST_TOOL_KEY"})
            self.assertEqual(adapter_config["tools"][0]["approvalPolicy"], "on-request")
            self.assertEqual(adapter_config["tools"][0]["scope"], "profile")
            self.assertEqual(adapter_config["brokerContext"]["hostApp"], "host-app")
            self.assertEqual(adapter_config["brokerContext"]["ownerHash"], "owner_hash_1")
            self.assertEqual(adapter_config["brokerContext"]["productCorrelationId"], "product-correlation-1")
            self.assertNotIn("ownerId", adapter_config["brokerContext"])
            scheduler = TurnScheduler(
                config=config,
                state=state,
                auth=AuthManager(config, state),
                bundles=registry,
                pool=AppServerPool(config),
            )
            input_items = scheduler._build_input([{"type": "text", "text": "Host turn.", "text_elements": []}], bundle)
            self.assertEqual(input_items[2]["name"], "legacy")
            self.assertEqual(input_items[2]["text"], "Legacy host prompt.")
            fake_client = AppServerClient.__new__(AppServerClient)
            fake_client.mcp_servers = bundle.mcp_servers
            with patch.dict(os.environ, {"MCP_SECRET_SOURCE": "resolved-secret"}):
                self.assertEqual(fake_client._mcp_process_env(), {"MCP_API_KEY": "resolved-secret"})
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(AppServerError):
                    fake_client._mcp_process_env()
            outside = bundle_dir / "bad.json"
            outside.write_text(json.dumps({"id": "bad", "allowedPaths": ["/etc"]}), encoding="utf-8")
            with self.assertRaises(BundleError):
                registry.resolve("bad")

            blocked_endpoint = config.allowed_bundle_roots[0] / "bad-tool-endpoint.json"
            blocked_endpoint.write_text(
                json.dumps({"id": "bad-tool-endpoint", "tools": [{"name": "host.bad", "http": {"url": "https://example.com/tool"}}]}),
                encoding="utf-8",
            )
            with self.assertRaises(BundleError):
                registry.resolve("bad-tool-endpoint")

            host_confusion_endpoint = config.allowed_bundle_roots[0] / "bad-tool-host-confusion.json"
            host_confusion_endpoint.write_text(
                json.dumps(
                    {
                        "id": "bad-tool-host-confusion",
                        "tools": [{"name": "host.confused", "http": {"url": "http://127.0.0.1.evil.test/tool"}}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BundleError):
                registry.resolve("bad-tool-host-confusion")
            self.assertTrue(
                registry._hosted_tool_url_matches(
                    "http://host.internal/api/tools/search",
                    "http://host.internal/api",
                )
            )
            self.assertFalse(
                registry._hosted_tool_url_matches(
                    "http://host.internal/apix/search",
                    "http://host.internal/api",
                )
            )

            literal_secret = config.allowed_bundle_roots[0] / "bad-tool-secret.json"
            literal_secret.write_text(
                json.dumps(
                    {
                        "id": "bad-tool-secret",
                        "tools": [
                            {
                                "name": "host.secret",
                                "http": {"url": "http://127.0.0.1/tool", "headers": {"Authorization": "Bearer literal"}},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BundleError):
                registry.resolve("bad-tool-secret")

            invalid_policy = config.allowed_bundle_roots[0] / "bad-tool-policy.json"
            invalid_policy.write_text(
                json.dumps(
                    {
                        "id": "bad-tool-policy",
                        "tools": [{"name": "host.policy", "approvalPolicy": "maybe", "http": {"url": "http://127.0.0.1/tool"}}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BundleError):
                registry.resolve("bad-tool-policy")

            literal_mcp_secret = config.allowed_bundle_roots[0] / "bad-mcp-secret.json"
            literal_mcp_secret.write_text(
                json.dumps(
                    {
                        "id": "bad-mcp-secret",
                        "mcpServers": [
                            {
                                "name": "bad_mcp",
                                "command": "python",
                                "env": {"MCP_API_KEY": "literal-secret"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BundleError):
                registry.resolve("bad-mcp-secret")

    def test_debug_raw_event_capture_redacts_secret_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            context = BrokerTurnContext(
                state=state,
                owner_hash="owner_hash",
                thread_id="thread_1",
                turn_id="turn_1",
                codex_thread_id="codex_thread_1",
                product_correlation_id="product_1",
                debug_raw_events=True,
            )
            context.handle_notification(
                "item/completed",
                {
                    "item": {
                        "type": "toolResult",
                        "apiKey": "sk-secret",
                        "headers": {"Authorization": "Bearer live-token"},
                        "output": "access_token=abc123 visible text",
                    }
                },
                ambiguous=False,
            )
            events = state.list_events("owner_hash", "thread_1")
            raw_params = events[0]["raw_params"]
            self.assertEqual(raw_params["item"]["apiKey"], "<redacted>")
            self.assertEqual(raw_params["item"]["headers"]["Authorization"], "<redacted>")
            self.assertEqual(raw_params["item"]["output"], "access_token=<redacted> visible text")

    def test_raw_event_retention_prunes_debug_fields_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path)
            event_id = state.append_event(
                "owner_hash",
                "thread_1",
                "turn_1",
                "item.completed",
                {"item": {"type": "agentMessage", "text": "visible"}},
                raw_method="item/completed",
                raw_params={"token": "secret"},
            )
            self.assertEqual(state.prune_raw_events_before("9999-01-01T00:00:00Z"), 1)
            event = state.list_events("owner_hash", "thread_1")[0]
            self.assertEqual(event["id"], event_id)
            self.assertEqual(event["payload"], {"item": {"type": "agentMessage", "text": "visible"}})
            self.assertIsNone(event["raw_method"])
            self.assertIsNone(event["raw_params"])

    def test_tool_item_events_are_normalized_to_tool_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path)
            context = BrokerTurnContext(
                state=state,
                owner_hash="owner_hash",
                thread_id="thread_1",
                turn_id="turn_1",
                codex_thread_id="codex_thread_1",
                product_correlation_id="product_1",
                debug_raw_events=False,
            )
            context.handle_notification(
                "item/started",
                {"item": {"id": "tool_1", "type": "commandExecution", "command": "printf test"}},
                ambiguous=False,
            )
            context.handle_notification(
                "item/commandExecution/outputDelta",
                {"itemId": "tool_1", "delta": "test"},
                ambiguous=False,
            )
            context.handle_notification(
                "item/completed",
                {"item": {"id": "tool_1", "type": "commandExecution", "exitCode": 0}},
                ambiguous=False,
            )
            context.handle_notification(
                "item/tool/requestUserInput",
                {"itemId": "tool_2"},
                ambiguous=True,
            )

            events = state.list_events("owner_hash", "thread_1")
            self.assertEqual([event["event_type"] for event in events], ["tool.started", "tool.output.delta", "tool.completed", "user_input.requested"])
            self.assertEqual(events[0]["payload"]["item"]["command"], "printf test")
            self.assertEqual(events[1]["payload"]["delta"], "test")
            self.assertTrue(events[3]["ambiguous"])

    def test_reasoning_summary_events_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            state = StateStore(config_for(Path(tmp_raw)).state_db_path)
            context = BrokerTurnContext(
                state=state,
                owner_hash="owner_hash",
                thread_id="thread_1",
                turn_id="turn_1",
                codex_thread_id="codex_thread_1",
                product_correlation_id="product_1",
                debug_raw_events=False,
            )
            context.handle_notification(
                "item/reasoning/summaryPartAdded",
                {"itemId": "reasoning_1", "summaryIndex": 0},
                ambiguous=False,
            )
            context.handle_notification(
                "item/reasoning/summaryTextDelta",
                {"itemId": "reasoning_1", "summaryIndex": 0, "delta": "thinking"},
                ambiguous=False,
            )
            context.handle_notification(
                "item/completed",
                {"item": {"id": "reasoning_1", "type": "reasoning"}},
                ambiguous=False,
            )

            events = state.list_events("owner_hash", "thread_1")
            self.assertEqual([event["event_type"] for event in events], [
                "reasoning.summary.started",
                "reasoning.summary.delta",
                "reasoning.completed",
            ])
            self.assertEqual(events[0]["payload"]["summaryId"], "reasoning_1:0")
            self.assertEqual(events[1]["payload"]["delta"], "thinking")

    def test_service_startup_recovers_incomplete_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            thread = state.create_thread(
                "owner_hash",
                thread_id=None,
                profile="default",
                config_profile="default",
                host_app=None,
                bundle_id=None,
                cwd=None,
            )
            state.set_codex_thread_id("owner_hash", thread["thread_id"], "codex_thread_1")
            turn = state.create_turn(
                "owner_hash",
                thread["thread_id"],
                profile="default",
                config_profile="default",
                host_app=None,
                bundle_id=None,
                cwd=None,
                mode="queue",
                input_items=[{"type": "text", "text": "work"}],
                idempotency_key="job-1",
                product_correlation_id="job-1",
                status="running",
            )
            state.close()

            services = BrokerServices.build(config)
            try:
                recovered = services.state.get_turn("owner_hash", thread["thread_id"], turn["turn_id"])
                assert recovered is not None
                self.assertEqual(recovered["status"], "failed")
                self.assertEqual(recovered["error"], "Broker restarted before the turn completed.")
                self.assertIsNotNone(recovered["completed_at"])
                events = services.state.list_events("owner_hash", thread["thread_id"])
                self.assertEqual(events[0]["event_type"], "turn.failed")
                self.assertEqual(events[0]["codex_thread_id"], "codex_thread_1")
                self.assertTrue(events[0]["payload"]["recovered"])
                self.assertEqual(services.scheduler.metrics()["turns_recovered"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_metrics_include_durable_audit_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            services = BrokerServices.build(config)
            try:
                services.auth.login_api_key("user@example.com", "sk-test", "default")
                services.auth.logout("user@example.com", "default")
                metrics = services.scheduler.metrics()
                self.assertEqual(metrics["audit_auth_api_key_success"], 1)
                self.assertEqual(metrics["audit_auth_logout"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_audit_log_api_is_owner_scoped_and_filterable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            services = BrokerServices.build(config)
            try:
                owner_hash = services.auth.hash_owner("owner/a")
                other_hash = services.auth.hash_owner("owner/b")
                services.state.append_audit(
                    owner_hash,
                    "turn.start",
                    {"bundleId": "bundle_1"},
                    profile="work",
                    thread_id="thread_1",
                    turn_id="turn_1",
                )
                services.state.append_audit(owner_hash, "auth.logout", {}, profile="work")
                services.state.append_audit(other_hash, "turn.start", {"bundleId": "other"}, thread_id="thread_1")

                captured: dict[str, Any] = {}

                def capture_json(payload: dict[str, Any], status: Any = None) -> None:
                    captured["payload"] = payload
                    captured["status"] = status

                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = services
                handler._json = capture_json
                handler._owner_route(
                    "GET",
                    ["audit-logs"],
                    "owner/a",
                    {"action": ["turn.start"], "threadId": ["thread_1"], "limit": ["10"]},
                )

                payload = captured["payload"]
                self.assertEqual(payload["ownerHash"], owner_hash)
                self.assertEqual(len(payload["auditLogs"]), 1)
                self.assertEqual(payload["auditLogs"][0]["action"], "turn.start")
                self.assertEqual(payload["auditLogs"][0]["profile"], "work")
                self.assertEqual(payload["auditLogs"][0]["threadId"], "thread_1")
                self.assertEqual(payload["auditLogs"][0]["turnId"], "turn_1")
                self.assertEqual(payload["auditLogs"][0]["payload"], {"bundleId": "bundle_1"})
                self.assertNotIn("owner/a", json.dumps(payload))
            finally:
                services.pool.close_all()
                services.state.close()

    def test_internal_api_key_is_required_unless_explicitly_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            base = replace(config_for(Path(tmp_raw)), internal_key=None, allow_unauthenticated=False)
            handler = BrokerHandler.__new__(BrokerHandler)
            handler.broker = type("Broker", (), {"config": base})()
            handler.headers = {}
            self.assertFalse(handler._authorized())

            handler.broker = type("Broker", (), {"config": replace(base, allow_unauthenticated=True)})()
            self.assertTrue(handler._authorized())

            handler.broker = type("Broker", (), {"config": replace(base, internal_key="test-key")})()
            handler.headers = {"Authorization": "Bearer test-key"}
            self.assertTrue(handler._authorized())
            handler.headers = {"X-Codex-Broker-Key": "test-key"}
            self.assertTrue(handler._authorized())
            handler.headers = {"Authorization": "Bearer wrong"}
            self.assertFalse(handler._authorized())

    def test_only_probe_endpoints_are_unauthenticated_by_default(self) -> None:
        self.assertTrue(is_unauthenticated_path("GET", "/healthz"))
        self.assertTrue(is_unauthenticated_path("GET", "/readyz"))
        self.assertFalse(is_unauthenticated_path("GET", "/metrics"))
        self.assertFalse(is_unauthenticated_path("GET", "/openapi.json"))
        self.assertFalse(is_unauthenticated_path("POST", "/healthz"))

    def test_readyz_reports_missing_internal_key_without_dev_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), internal_key=None, allow_unauthenticated=False)
            services = BrokerServices.build(config)
            try:
                captured: dict[str, Any] = {}

                def capture_json(payload: dict[str, Any], status: Any = HTTPStatus.OK) -> None:
                    captured["payload"] = payload
                    captured["status"] = status

                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = services
                handler._json = capture_json
                handler._readyz()
                self.assertEqual(captured["status"], HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertIn("internal API key not configured", captured["payload"]["errors"])
            finally:
                services.pool.close_all()
                services.state.close()

            dev_services = BrokerServices.build(replace(config, allow_unauthenticated=True))
            try:
                captured = {}
                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = dev_services
                handler._json = capture_json
                handler._readyz()
                self.assertEqual(captured["status"], HTTPStatus.OK)
                self.assertEqual(captured["payload"]["errors"], [])
            finally:
                dev_services.pool.close_all()
                dev_services.state.close()

    def test_observability_metrics_include_http_stream_and_turn_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                self.assertEqual(
                    metric_path_template("/v1/owners/alice/threads/thr_1/turns/turn_1/interrupt"),
                    "v1/owners/ownerId/threads/threadId/turns/turnId/interrupt",
                )
                services.scheduler.note_http_request(
                    "v1/owners/ownerId/threads/threadId/turns",
                    202,
                    0.25,
                )
                services.scheduler.note_event_stream_disconnect()
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"cwd": str(services.config.allowed_workspace_roots[0]), "hostApp": "chat-app"},
                )
                self.assertEqual(thread["hostApp"], "chat-app")
                turn = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "observe"}]})
                self.assertEqual(turn["hostApp"], "chat-app")
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"], "completed")
                metrics = services.scheduler.metrics()
                self.assertEqual(metrics["http_requests_total"], 1)
                self.assertEqual(metrics["http_requests_v1_owners_ownerid_threads_threadid_turns_status_202"], 1)
                self.assertGreaterEqual(metrics["http_request_duration_seconds_sum"], 0.25)
                self.assertEqual(metrics["event_stream_disconnects"], 1)
                self.assertEqual(metrics["turn_duration_seconds_count"], 1)
                self.assertGreater(metrics["turn_duration_seconds_sum"], 0)
                self.assertEqual(metrics["turn_duration_seconds_count_host_app_chat_app"], 1)
                self.assertGreater(metrics["turn_duration_seconds_sum_host_app_chat_app"], 0)
                services.scheduler.note_turn_duration("worker-app", "reports-v1", 0.25)
                metrics = services.scheduler.metrics()
                self.assertEqual(metrics["turn_duration_seconds_count_host_app_worker_app_bundle_reports_v1"], 1)
                self.assertGreater(metrics["turn_duration_seconds_sum_host_app_worker_app_bundle_reports_v1"], 0)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_inline_bundles_are_size_limited_and_path_validated_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = replace(config_for(tmp), enable_inline_bundles=True, inline_bundle_max_bytes=220)
            state = StateStore(config.state_db_path)
            registry = BundleRegistry(config, state)
            accepted = registry.accept_inline(
                {
                    "id": "inline-ok",
                    "instructions": ["Keep it small."],
                    "allowedPaths": [str(config.allowed_workspace_roots[0])],
                }
            )
            self.assertEqual(accepted.source, "inline")
            self.assertTrue((config.inline_bundle_root / accepted.digest / "bundle.json").exists())
            with self.assertRaises(BundleError):
                registry.accept_inline({"id": "inline-bad-path", "allowedPaths": ["/etc"]})
            with self.assertRaises(BundleError):
                registry.accept_inline({"id": "inline-too-large", "instructions": ["x" * 500]})

    def test_same_thread_rejects_concurrent_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.25))
            thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
            first = services.scheduler.start_turn(
                "owner-a",
                thread["threadId"],
                {"input": [{"type": "text", "text": "one"}], "mode": "reject"},
            )
            with self.assertRaises(ActiveTurnError):
                services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "two"}], "mode": "reject"},
                )
            done = wait_turn(services, "owner-a", thread["threadId"], first["turnId"])
            self.assertEqual(done["status"], "completed")

    def test_different_threads_run_concurrently_for_same_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.35))
            t1 = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
            t2 = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
            r1 = services.scheduler.start_turn("owner-a", t1["threadId"], {"input": [{"type": "text", "text": "one"}]})
            r2 = services.scheduler.start_turn("owner-a", t2["threadId"], {"input": [{"type": "text", "text": "two"}]})
            wait_metric(services, "active_turns", 2)
            self.assertEqual(wait_turn(services, "owner-a", t1["threadId"], r1["turnId"])["status"], "completed")
            self.assertEqual(wait_turn(services, "owner-a", t2["threadId"], r2["turnId"])["status"], "completed")

    def test_queue_mode_serializes_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.15))
            thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
            first = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "one"}]})
            second = services.scheduler.start_turn(
                "owner-a",
                thread["threadId"],
                {"input": [{"type": "text", "text": "two"}], "mode": "queue"},
            )
            self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], first["turnId"])["status"], "completed")
            self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], second["turnId"])["status"], "completed")

    def test_idempotency_key_reuses_turn_and_returns_encoded_stream_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                owner_id = "owner/a"
                thread = services.scheduler.create_thread(owner_id, {"cwd": str(services.config.allowed_workspace_roots[0])})
                first = services.scheduler.start_turn(
                    owner_id,
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "once"}], "idempotencyKey": "host-turn-1"},
                )
                self.assertEqual(wait_turn(services, owner_id, thread["threadId"], first["turnId"])["status"], "completed")
                second = services.scheduler.start_turn(
                    owner_id,
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "retry"}], "idempotencyKey": "host-turn-1"},
                )

                self.assertEqual(second["turnId"], first["turnId"])
                self.assertIn("/v1/owners/owner%2Fa/threads/", first["streamUrl"])
                self.assertIn("/v1/owners/owner%2Fa/threads/", second["streamUrl"])
                self.assertNotIn("/v1/owners/owner/a/threads/", first["streamUrl"])
                self.assertEqual(services.scheduler.metrics()["turns_started"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_archived_thread_rejects_new_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                archived = services.scheduler.archive_thread("owner-a", thread["threadId"])
                self.assertEqual(archived["status"], "archived")
                with self.assertRaises(ConflictError):
                    services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "late"}]})
            finally:
                services.pool.close_all()
                services.state.close()

    def test_shutdown_rejects_new_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                services.scheduler.shutdown("drain", timeout_seconds=0)
                with self.assertRaises(ConflictError):
                    services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "late"}]})
            finally:
                services.pool.close_all()
                services.state.close()

    def test_shutdown_interrupts_active_turn_and_persists_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=2.0))
            try:
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                turn = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "long"}]})
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if services.scheduler.get_turn("owner-a", thread["threadId"], turn["turnId"])["codexTurnId"]:
                        break
                    time.sleep(0.01)
                services.scheduler.shutdown("interrupt", timeout_seconds=2)
                interrupted = services.scheduler.get_turn("owner-a", thread["threadId"], turn["turnId"])
                self.assertEqual(interrupted["status"], "interrupted")
                self.assertIn("Broker shutting down", interrupted["error"])
                self.assertEqual(services.scheduler.metrics()["turns_interrupted"], 1)
                owner_hash = services.auth.hash_owner("owner-a")
                actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
                self.assertIn("turn.interrupt", actions)
                events = services.state.list_events(owner_hash, thread["threadId"], turn_id=turn["turnId"])
                self.assertTrue(any(event["event_type"] == "turn.interrupted" for event in events))
            finally:
                services.pool.close_all()
                services.state.close()

    def test_shutdown_drain_allows_active_turn_to_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.05))
            try:
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                turn = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "finish"}]})
                services.scheduler.shutdown("drain", timeout_seconds=2)
                finished = services.scheduler.get_turn("owner-a", thread["threadId"], turn["turnId"])
                self.assertEqual(finished["status"], "completed")
                with self.assertRaises(ConflictError):
                    services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "late"}]})
            finally:
                services.pool.close_all()
                services.state.close()

    def test_different_owners_run_concurrently_with_isolated_auth_homes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.35))
            t1 = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
            t2 = services.scheduler.create_thread("owner-b", {"cwd": str(services.config.allowed_workspace_roots[0])})
            r1 = services.scheduler.start_turn("owner-a", t1["threadId"], {"input": [{"type": "text", "text": "one"}]})
            r2 = services.scheduler.start_turn("owner-b", t2["threadId"], {"input": [{"type": "text", "text": "two"}]})
            wait_metric(services, "active_turns", 2)
            self.assertEqual(wait_turn(services, "owner-a", t1["threadId"], r1["turnId"])["status"], "completed")
            self.assertEqual(wait_turn(services, "owner-b", t2["threadId"], r2["turnId"])["status"], "completed")
            owner_a_hash = services.auth.hash_owner("owner-a")
            owner_b_hash = services.auth.hash_owner("owner-b")
            self.assertNotEqual(owner_a_hash, owner_b_hash)
            self.assertTrue(services.auth.profile_home(owner_a_hash).is_dir())
            self.assertTrue(services.auth.profile_home(owner_b_hash).is_dir())

    def test_idle_app_server_child_is_closed_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw), turn_delay=0.01), pool_idle_ttl_seconds=1)
            services = BrokerServices.build(config)
            try:
                first_thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                first = services.scheduler.start_turn("owner-a", first_thread["threadId"], {"input": [{"type": "text", "text": "first"}]})
                self.assertEqual(wait_turn(services, "owner-a", first_thread["threadId"], first["turnId"])["status"], "completed")
                first_client = next(iter(services.pool._clients.values()))
                first_pid = first_client._process.pid

                time.sleep(1.1)
                second_thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                second = services.scheduler.start_turn("owner-a", second_thread["threadId"], {"input": [{"type": "text", "text": "second"}]})
                self.assertEqual(wait_turn(services, "owner-a", second_thread["threadId"], second["turnId"])["status"], "completed")
                second_client = next(iter(services.pool._clients.values()))

                self.assertNotEqual(second_client._process.pid, first_pid)
                self.assertIsNotNone(first_client._process.poll())
                self.assertEqual(services.scheduler.metrics()["active_app_server_children"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_steer_mode_routes_to_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.25))
            thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
            first = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "one"}]})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if services.scheduler.get_turn("owner-a", thread["threadId"], first["turnId"])["codexTurnId"]:
                    break
                time.sleep(0.01)
            steered = services.scheduler.start_turn(
                "owner-a",
                thread["threadId"],
                {"input": [{"type": "text", "text": "more"}], "mode": "steer"},
            )
            self.assertEqual(steered["turnId"], first["turnId"])
            self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], first["turnId"])["status"], "completed")
            owner_hash = services.auth.hash_owner("owner-a")
            events = services.state.list_events(owner_hash, thread["threadId"], turn_id=first["turnId"], limit=100)
            self.assertTrue(any(event["payload"].get("steered") for event in events if event["event_type"] == "message.delta"))

    def test_crashed_app_server_fails_active_turn_and_restarts_for_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                os.environ["FAKE_CODEX_CRASH_ON_TURN_ONCE"] = "1"
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                first = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "crash"}]})
                failed = wait_turn(services, "owner-a", thread["threadId"], first["turnId"])
                self.assertEqual(failed["status"], "failed")
                self.assertIn("App Server", failed["error"])
                second = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "recover"}]})
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], second["turnId"])["status"], "completed")
                self.assertGreaterEqual(services.scheduler.metrics()["app_server_restarts"], 1)
            finally:
                os.environ.pop("FAKE_CODEX_CRASH_ON_TURN_ONCE", None)
                services.pool.close_all()
                services.state.close()

    def test_request_timeout_fails_turn_and_restarts_for_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw), turn_delay=0.01), request_timeout_seconds=1.0)
            services = BrokerServices.build(config)
            try:
                os.environ["FAKE_CODEX_HANG_ON_TURN_START_ONCE"] = "1"
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                first = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "hang"}]})
                failed = wait_turn(services, "owner-a", thread["threadId"], first["turnId"])
                self.assertEqual(failed["status"], "failed")
                self.assertIn("Timed out waiting for App Server response to turn/start", failed["error"])
                wait_metric(services, "turns_failed", 1)

                second = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "recover"}]})
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], second["turnId"])["status"], "completed")
                self.assertGreaterEqual(services.scheduler.metrics()["app_server_restarts"], 1)
            finally:
                os.environ.pop("FAKE_CODEX_HANG_ON_TURN_START_ONCE", None)
                services.pool.close_all()
                services.state.close()

    def test_turn_timeout_interrupts_and_persists_timed_out_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw), turn_delay=1.0), turn_timeout_seconds=0.05)
            services = BrokerServices.build(config)
            try:
                thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
                turn = services.scheduler.start_turn("owner-a", thread["threadId"], {"input": [{"type": "text", "text": "slow"}]})
                timed_out = wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])
                self.assertEqual(timed_out["status"], "timed_out")
                self.assertEqual(timed_out["error"], "Turn timed out.")
                self.assertEqual(services.scheduler.metrics()["turns_failed"], 1)
                owner_hash = services.auth.hash_owner("owner-a")
                events = services.state.list_events(owner_hash, thread["threadId"], turn_id=turn["turnId"], limit=100)
                self.assertTrue(any(event["event_type"] == "turn.failed" and event["payload"].get("message") == "Turn timed out." for event in events))
            finally:
                services.pool.close_all()
                services.state.close()

    def test_audit_log_records_turn_start_and_tool_approval_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            os.environ["FAKE_CODEX_REQUEST_APPROVAL"] = "1"
            thread = services.scheduler.create_thread("owner-a", {"cwd": str(services.config.allowed_workspace_roots[0])})
            turn = services.scheduler.start_turn(
                "owner-a",
                thread["threadId"],
                {
                    "input": [{"type": "text", "text": "approval"}],
                    "productCorrelationId": "host-correlation-1",
                },
            )
            self.assertEqual(turn["productCorrelationId"], "host-correlation-1")
            self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"], "completed")
            owner_hash = services.auth.hash_owner("owner-a")
            actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
            self.assertIn("turn.start", actions)
            self.assertIn("approval.requested", actions)
            self.assertIn("approval.resolved", actions)
            events = services.state.list_events(owner_hash, thread["threadId"], turn_id=turn["turnId"], limit=100)
            event_types = [event["event_type"] for event in events]
            self.assertLess(event_types.index("tool.requested"), event_types.index("approval.requested"))
            self.assertTrue(any(event.get("product_correlation_id") == "host-correlation-1" for event in events))
            self.assertTrue(any(event.get("codex_thread_id") for event in events))
            self.assertTrue(any(event.get("codex_turn_id") for event in events))
            os.environ.pop("FAKE_CODEX_REQUEST_APPROVAL", None)

if __name__ == "__main__":
    unittest.main()
