from __future__ import annotations

import io
import json
import os
import threading
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from codex_broker.app_server import AppServerClient, AppServerError, AppServerPool
from codex_broker.bundles import McpServerRef
from codex_broker.config import BrokerConfig
from codex_broker.state import StateStore


ROOT = Path(__file__).resolve().parent
FAKE_CODEX = ROOT / "fake_codex.py"


def config_for(tmp: Path) -> BrokerConfig:
    os.environ.pop("FAKE_CODEX_REQUEST_USER_INPUT", None)
    os.environ.pop("FAKE_CODEX_REQUEST_MCP_ELICITATION", None)
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
        allowed_hosted_tool_url_prefixes=("http://127.0.0.1",),
        credential_store="file",
        request_timeout_seconds=5,
        host_response_timeout_seconds=0.05,
        turn_timeout_seconds=5,
        enable_inline_bundles=False,
        inline_bundle_max_bytes=262_144,
        debug_raw_events=True,
        raw_event_retention_seconds=7 * 24 * 60 * 60,
        json_logs=False,
        shutdown_mode="interrupt",
        shutdown_drain_timeout_seconds=1,
    )


class FakeContext:
    owner_hash = "owner_hash"
    thread_id = "thread_1"
    turn_id = "turn_1"
    product_correlation_id: str | None = None
    codex_turn_id: str | None = None

    def __init__(self) -> None:
        self.codex_thread_id: str | None = None
        self.notifications: list[tuple[str, dict[str, Any], bool]] = []

    def register_thread(self, codex_thread_id: str) -> None:
        self.codex_thread_id = codex_thread_id

    def register_turn(self, codex_turn_id: str) -> None:
        self.codex_turn_id = codex_turn_id

    def handle_notification(self, method: str, params: dict[str, Any], *, ambiguous: bool) -> None:
        self.notifications.append((method, params, ambiguous))

    def record_tool_requested(self, method: str, params: dict[str, Any], *, ambiguous: bool) -> None:
        self.notifications.append(("tool.requested", {"method": method, "params": params}, ambiguous))

    def fail(self, message: str) -> None:
        raise AssertionError(message)


class RecordingContext(FakeContext):
    def __init__(self) -> None:
        super().__init__()
        self.failures: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)


class FakeProcess:
    pid = 4242


class AppServerRoutingTests(unittest.TestCase):
    def test_pool_creation_lock_survives_failed_start_with_multiple_waiters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), pool_idle_ttl_seconds=0)
            pool = AppServerPool(config)
            first_started = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            release_second = threading.Event()
            calls = 0
            calls_lock = threading.Lock()

            class DummyClient:
                closed = False

                def close(self) -> None:
                    self.closed = True

            def create_client(*args: Any, **kwargs: Any) -> DummyClient:
                nonlocal calls
                with calls_lock:
                    calls += 1
                    call = calls
                if call == 1:
                    first_started.set()
                    release_first.wait(2)
                    raise AppServerError("first launch failed")
                if call == 2:
                    second_started.set()
                    release_second.wait(2)
                return DummyClient()

            results: list[Any] = []
            failures: list[BaseException] = []

            def get_client() -> None:
                try:
                    results.append(
                        pool.get(
                            owner_hash="owner",
                            profile="default",
                            codex_home=config.auth_root,
                            config_profile="default",
                            mcp_servers=(),
                        )
                    )
                except BaseException as exc:  # noqa: BLE001 - test records thread failures.
                    failures.append(exc)

            with patch.object(pool, "_codex_version", return_value="test"), patch(
                "codex_broker.app_server.AppServerClient",
                side_effect=create_client,
            ):
                first = threading.Thread(target=get_client)
                second = threading.Thread(target=get_client)
                first.start()
                self.assertTrue(first_started.wait(1))
                second.start()
                release_first.set()
                self.assertTrue(second_started.wait(1))
                third = threading.Thread(target=get_client)
                third.start()
                time.sleep(0.05)
                self.assertEqual(calls, 2)
                release_second.set()
                for worker in (first, second, third):
                    worker.join(2)

            self.assertEqual(calls, 2)
            self.assertEqual(len(results), 2)
            self.assertEqual(len(failures), 1)
            self.assertIs(results[0], results[1])
            pool.close_all()

    def test_pool_records_app_server_process_lifecycle_in_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            pool = AppServerPool(config, state)
            try:
                codex_home = config.auth_root / "owner_hash" / "profiles" / "default" / "codex-home"
                codex_home.mkdir(parents=True)
                client = pool.get(
                    owner_hash="owner_hash",
                    profile="default",
                    codex_home=codex_home,
                    config_profile="default",
                    mcp_servers=(),
                )

                records = state.list_app_server_processes()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["status"], "running")
                self.assertEqual(records[0]["owner_hash"], "owner_hash")
                self.assertEqual(records[0]["profile"], "default")
                self.assertEqual(records[0]["config_profile"], "default")
                self.assertEqual(records[0]["pid"], client._process.pid)

                pool.close_all()
                records = state.list_app_server_processes()
                self.assertEqual(records[0]["status"], "closed")
                self.assertIsNotNone(records[0]["closed_at"])
                self.assertIsNotNone(records[0]["exit_code"])
            finally:
                pool.close_all()
                state.close()

    def test_idle_sweep_does_not_close_active_app_server_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = replace(config_for(tmp), pool_idle_ttl_seconds=1)
            state = StateStore(config.state_db_path)
            pool = AppServerPool(config, state)
            try:
                codex_home = config.auth_root / "owner_hash" / "profiles" / "default" / "codex-home"
                codex_home.mkdir(parents=True)
                client = pool.get(
                    owner_hash="owner_hash",
                    profile="default",
                    codex_home=codex_home,
                    config_profile="default",
                    mcp_servers=(),
                )
                context = RecordingContext()
                client.register_context(context)
                client.last_used_at = time.monotonic() - 2

                other_home = config.auth_root / "other_hash" / "profiles" / "default" / "codex-home"
                other_home.mkdir(parents=True)
                other_client = pool.get(
                    owner_hash="other_hash",
                    profile="default",
                    codex_home=other_home,
                    config_profile="default",
                    mcp_servers=(),
                )

                self.assertIsNot(other_client, client)
                self.assertIsNone(client._process.poll())
                self.assertEqual(context.failures, [])
                self.assertEqual(pool.metrics()["active_app_server_children"], 2)
                client.unregister_context(context)
            finally:
                pool.close_all()
                state.close()

    def test_pool_key_includes_codex_binary_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            first_state = StateStore(config.state_db_path)
            second_state = StateStore(tmp / "data-second" / "state" / "broker.sqlite")
            first_pool: AppServerPool | None = None
            second_pool: AppServerPool | None = None
            try:
                codex_home = config.auth_root / "owner_hash" / "profiles" / "default" / "codex-home"
                codex_home.mkdir(parents=True)
                with patch.dict(os.environ, {"FAKE_CODEX_VERSION": "fake-codex 1.0"}):
                    first_pool = AppServerPool(config, first_state)
                    first_client = first_pool.get(
                        owner_hash="owner_hash",
                        profile="default",
                        codex_home=codex_home,
                        config_profile="default",
                        mcp_servers=(),
                    )
                with patch.dict(os.environ, {"FAKE_CODEX_VERSION": "fake-codex 2.0"}):
                    second_pool = AppServerPool(config, second_state)
                    second_client = second_pool.get(
                        owner_hash="owner_hash",
                        profile="default",
                        codex_home=codex_home,
                        config_profile="default",
                        mcp_servers=(),
                    )

                self.assertNotEqual(first_client.pool_key_hash, second_client.pool_key_hash)
            finally:
                if first_pool:
                    first_pool.close_all()
                if second_pool:
                    second_pool.close_all()
                first_state.close()
                second_state.close()

    def test_pool_key_includes_auth_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            pool = AppServerPool(config, state)
            try:
                codex_home = config.auth_root / "owner_hash" / "profiles" / "default" / "codex-home"
                codex_home.mkdir(parents=True)
                first_client = pool.get(
                    owner_hash="owner_hash",
                    profile="default",
                    codex_home=codex_home,
                    config_profile="default",
                    mcp_servers=(),
                    auth_fingerprint="sha256:first",
                )
                second_client = pool.get(
                    owner_hash="owner_hash",
                    profile="default",
                    codex_home=codex_home,
                    config_profile="default",
                    mcp_servers=(),
                    auth_fingerprint="sha256:second",
                )

                self.assertIsNot(first_client, second_client)
                self.assertNotEqual(first_client.pool_key_hash, second_client.pool_key_hash)
                self.assertTrue(first_client.closed)
                self.assertFalse(second_client.closed)
                self.assertEqual(pool.metrics()["active_app_server_children"], 1)
            finally:
                pool.close_all()
                state.close()

    def test_build_command_includes_codex_process_config_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            client = AppServerClient.__new__(AppServerClient)
            client.config = config_for(Path(tmp_raw))
            client.mcp_servers = ()
            client.codex_config_args = (
                ("web_search", "disabled"),
                ("model_verbosity", "low"),
                ("features.image_generation", "false"),
            )

            command = client._build_command()

            self.assertIn("web_search=disabled", command)
            self.assertIn("model_verbosity=low", command)
            self.assertIn("features.image_generation=false", command)

    def test_pool_key_includes_resolved_mcp_env_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            pool = AppServerPool(config, state)
            mcp_servers = (
                McpServerRef(
                    name="host_mcp",
                    command="python",
                    args=(),
                    env={"MCP_API_KEY": "env:MCP_SECRET_SOURCE"},
                    cwd=None,
                ),
            )
            try:
                codex_home = config.auth_root / "owner_hash" / "profiles" / "default" / "codex-home"
                codex_home.mkdir(parents=True)
                with patch.dict(os.environ, {"MCP_SECRET_SOURCE": "first-secret"}):
                    first_client = pool.get(
                        owner_hash="owner_hash",
                        profile="default",
                        codex_home=codex_home,
                        config_profile="default",
                        mcp_servers=mcp_servers,
                    )
                with patch.dict(os.environ, {"MCP_SECRET_SOURCE": "rotated-secret"}):
                    second_client = pool.get(
                        owner_hash="owner_hash",
                        profile="default",
                        codex_home=codex_home,
                        config_profile="default",
                        mcp_servers=mcp_servers,
                    )

                self.assertIsNot(first_client, second_client)
                self.assertNotEqual(first_client.pool_key_hash, second_client.pool_key_hash)
                self.assertEqual(pool.metrics()["active_app_server_children"], 2)
            finally:
                pool.close_all()
                state.close()

    def test_request_timeout_closes_child_and_fails_active_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            pool = AppServerPool(config, state)
            try:
                codex_home = config.auth_root / "owner_hash" / "profiles" / "default" / "codex-home"
                codex_home.mkdir(parents=True)
                with patch.dict(os.environ, {"FAKE_CODEX_HANG_ON_TURN_START_ONCE": "1"}):
                    client = pool.get(
                        owner_hash="owner_hash",
                        profile="default",
                        codex_home=codex_home,
                        config_profile="default",
                        mcp_servers=(),
                    )
                context = RecordingContext()
                client.register_context(context)

                with self.assertRaisesRegex(AppServerError, "Timed out waiting for App Server response to turn/start"):
                    client.request("turn/start", {"threadId": "thr_timeout"}, timeout=0.1)

                self.assertTrue(client.closed)
                self.assertEqual(context.failures, ["App Server closed"])
            finally:
                pool.close_all()
                state.close()

    def test_close_profile_keeps_other_owner_profiles_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            state = StateStore(config.state_db_path)
            pool = AppServerPool(config, state)
            try:
                default_home = config.auth_root / "owner_hash" / "profiles" / "default" / "codex-home"
                work_home = config.auth_root / "owner_hash" / "profiles" / "work" / "codex-home"
                default_home.mkdir(parents=True)
                work_home.mkdir(parents=True)
                default_client = pool.get(
                    owner_hash="owner_hash",
                    profile="default",
                    codex_home=default_home,
                    config_profile="default",
                    mcp_servers=(),
                )
                work_client = pool.get(
                    owner_hash="owner_hash",
                    profile="work",
                    codex_home=work_home,
                    config_profile="default",
                    mcp_servers=(),
                )

                pool.close_profile("owner_hash", "default")

                self.assertTrue(default_client.closed)
                self.assertFalse(work_client.closed)
                self.assertEqual(pool.metrics()["active_app_server_children"], 1)
            finally:
                pool.close_all()
                state.close()

    def test_app_server_stream_logs_are_structured_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            client = AppServerClient.__new__(AppServerClient)
            client.config = replace(config_for(Path(tmp_raw)), json_logs=True)
            client.owner_hash = "owner_hash"
            client.profile = "default"
            client.config_profile = "default"
            client.pool_key_hash = "pool_hash"
            client._process = FakeProcess()

            stream = io.StringIO()
            with redirect_stderr(stream):
                clean = client._log_stream_line(
                    "stderr",
                    "failed Authorization: Bearer live-token access_token=secret-token api_key=sk-live_secret",
                )

            self.assertNotIn("live-token", clean)
            self.assertNotIn("secret-token", clean)
            self.assertNotIn("sk-live_secret", clean)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["event"], "app_server.stderr")
            self.assertEqual(payload["ownerHash"], "owner_hash")
            self.assertEqual(payload["pid"], 4242)
            self.assertEqual(payload["poolKeyHash"], "pool_hash")
            self.assertIn("Authorization=<redacted>", payload["line"])
            self.assertNotIn("live-token", payload["line"])
            self.assertNotIn("secret-token", payload["line"])
            self.assertNotIn("sk-live_secret", payload["line"])

    def test_invalid_stdout_log_is_redacted_before_failure_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            client = AppServerClient.__new__(AppServerClient)
            client.config = replace(config_for(Path(tmp_raw)), json_logs=True)
            client.owner_hash = "owner_hash"
            client.profile = "default"
            client.config_profile = "default"
            client.pool_key_hash = "pool_hash"
            client._process = FakeProcess()

            stream = io.StringIO()
            with redirect_stderr(stream):
                clean = client._log_stream_line("stdout.invalid_json", "not json access_token=secret-token")

            self.assertEqual(clean, "not json access_token=<redacted>")
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["event"], "app_server.stdout.invalid_json")
            self.assertEqual(payload["line"], "not json access_token=<redacted>")
            self.assertNotIn("secret-token", stream.getvalue())

    def test_early_thread_notification_attaches_to_single_active_context_as_ambiguous(self) -> None:
        client = AppServerClient.__new__(AppServerClient)
        client._contexts_lock = threading.RLock()
        context = FakeContext()
        client._contexts = {context}
        client._contexts_by_thread = {}
        client._contexts_by_turn = {}

        client._handle_notification("thread/started", {"thread": {"id": "codex_thread_1"}})

        self.assertEqual(context.codex_thread_id, "codex_thread_1")
        self.assertEqual(client._contexts_by_thread["codex_thread_1"], context)
        self.assertEqual(context.notifications[0][0], "thread/started")
        self.assertTrue(context.notifications[0][2])

    def test_unknown_ids_do_not_guess_between_multiple_active_contexts(self) -> None:
        client = AppServerClient.__new__(AppServerClient)
        client._contexts_lock = threading.RLock()
        first = FakeContext()
        second = FakeContext()
        client._contexts = {first, second}
        client._contexts_by_thread = {}
        client._contexts_by_turn = {}
        client._pending_notifications_by_turn = {}

        client._handle_notification("item/agentMessage/delta", {"threadId": "unknown", "delta": "lost"})

        self.assertEqual(first.notifications, [])
        self.assertEqual(second.notifications, [])

    def test_early_turn_id_notification_is_buffered_until_turn_is_registered(self) -> None:
        client = AppServerClient.__new__(AppServerClient)
        client._contexts_lock = threading.RLock()
        first = FakeContext()
        second = FakeContext()
        client._contexts = {first, second}
        client._contexts_by_thread = {}
        client._contexts_by_turn = {}
        client._pending_notifications_by_turn = {}

        client._handle_notification(
            "item/agentMessage/delta",
            {"turnId": "codex_turn_1", "delta": "early"},
        )
        self.assertEqual(first.notifications, [])
        self.assertEqual(second.notifications, [])

        client.register_turn_for_context(second, "codex_turn_1")

        self.assertEqual(first.notifications, [])
        self.assertEqual(second.notifications, [("item/agentMessage/delta", {"turnId": "codex_turn_1", "delta": "early"}, True)])
        self.assertEqual(client._pending_notifications_by_turn, {})

    def test_host_can_resolve_pending_command_approval(self) -> None:
        result, notifications = self._host_resolved_request(
            "item/commandExecution/requestApproval",
            {"threadId": "codex_thread_1", "turnId": "codex_turn_1", "command": "printf test"},
            {"decision": "accept"},
        )

        self.assertEqual(result, {"decision": "accept"})
        resolved = [item for item in notifications if item[0] == "approval/resolved"][-1]
        self.assertEqual(resolved[1]["source"], "host")
        self.assertEqual(resolved[1]["response"], {"decision": "accept"})

    def test_host_can_resolve_pending_user_input_and_mcp_elicitation(self) -> None:
        user_input_result, user_input_notifications = self._host_resolved_request(
            "item/tool/requestUserInput",
            {"threadId": "codex_thread_1", "turnId": "codex_turn_1", "itemId": "input_1"},
            {"answers": {"color": {"answers": ["blue"]}}},
        )
        mcp_result, mcp_notifications = self._host_resolved_request(
            "mcpServer/elicitation/request",
            {"threadId": "codex_thread_1", "turnId": "codex_turn_1", "serverName": "host", "mode": "form"},
            {"action": "accept", "content": {"ok": True}, "_meta": {"source": "test"}},
        )

        self.assertEqual(user_input_result, {"answers": {"color": {"answers": ["blue"]}}})
        self.assertEqual(mcp_result, {"action": "accept", "content": {"ok": True}, "_meta": {"source": "test"}})
        self.assertEqual([item for item in user_input_notifications if item[0] == "item/tool/requestUserInput/resolved"][-1][1]["source"], "host")
        self.assertEqual([item for item in mcp_notifications if item[0] == "mcpServer/elicitation/resolved"][-1][1]["source"], "host")

    def _host_resolved_request(
        self,
        method: str,
        params: dict[str, Any],
        response: dict[str, Any],
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any], bool]]]:
        with tempfile.TemporaryDirectory() as tmp_raw:
            client = AppServerClient.__new__(AppServerClient)
            client.config = replace(config_for(Path(tmp_raw)), host_response_timeout_seconds=1)
            client.state = None
            client._contexts_lock = threading.RLock()
            client._stdin_lock = threading.Lock()
            client._stdin = io.StringIO()
            context = FakeContext()
            client._contexts = {context}
            client._contexts_by_thread = {"codex_thread_1": context}
            client._contexts_by_turn = {"codex_turn_1": context}
            client._pending_notifications_by_turn = {}
            thread = threading.Thread(target=client._handle_server_request, args=(method, 100, params))
            thread.start()
            interaction_id = self._wait_for_interaction_id(context, method)
            resolved = client.resolve_pending_interaction(interaction_id, response)
            self.assertIsNone(resolved)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            messages = [json.loads(line) for line in client._stdin.getvalue().splitlines()]
            return messages[-1]["result"], context.notifications

    @staticmethod
    def _wait_for_interaction_id(context: FakeContext, method: str) -> str:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            for notification_method, params, _ambiguous in context.notifications:
                if notification_method == method and isinstance(params.get("interactionId"), str):
                    return params["interactionId"]
            time.sleep(0.01)
        raise AssertionError(f"interaction id for {method} was not emitted")

    def test_server_requests_use_codex_0_142_3_safe_response_shapes(self) -> None:
        client = AppServerClient.__new__(AppServerClient)
        client._contexts_lock = threading.RLock()
        client._stdin_lock = threading.Lock()
        client._stdin = io.StringIO()
        context = FakeContext()
        client._contexts = {context}
        client._contexts_by_thread = {"codex_thread_1": context}
        client._contexts_by_turn = {"codex_turn_1": context}

        client._handle_server_request(
            "item/permissions/requestApproval",
            100,
            {"threadId": "codex_thread_1", "turnId": "codex_turn_1", "itemId": "perm_1"},
        )
        client._handle_server_request(
            "applyPatchApproval",
            101,
            {"threadId": "codex_thread_1", "turnId": "codex_turn_1", "itemId": "patch_1"},
        )
        client._handle_server_request(
            "item/tool/requestUserInput",
            102,
            {"threadId": "codex_thread_1", "turnId": "codex_turn_1", "itemId": "input_1"},
        )
        client._handle_server_request(
            "mcpServer/elicitation/request",
            103,
            {"threadId": "codex_thread_1", "turnId": "codex_turn_1", "serverName": "host", "mode": "form"},
        )

        responses = [json.loads(line) for line in client._stdin.getvalue().splitlines()]
        self.assertEqual(responses[0]["result"], {"permissions": {}, "scope": "turn", "strictAutoReview": True})
        self.assertEqual(responses[1]["result"], {"decision": "denied"})
        self.assertEqual(responses[2]["result"], {"answers": {}})
        self.assertEqual(responses[3]["result"], {"action": "decline", "content": None, "_meta": None})
        self.assertTrue(any(item[0] == "item/tool/requestUserInput/resolved" and item[1]["answers"] == {} for item in context.notifications))
        self.assertTrue(any(item[0] == "mcpServer/elicitation/resolved" and item[1]["action"] == "decline" for item in context.notifications))


if __name__ == "__main__":
    unittest.main()
