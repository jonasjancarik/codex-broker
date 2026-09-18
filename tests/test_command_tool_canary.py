from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-command-tool.py"
SPEC = importlib.util.spec_from_file_location("command_tool_canary", SCRIPT)
assert SPEC and SPEC.loader
canary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = canary
SPEC.loader.exec_module(canary)


class FakeBroker:
    def __init__(self, workspace: Path, *, exit_code: int = 0, terminal_event: bool = True) -> None:
        self.workspace = workspace
        self.exit_code = exit_code
        self.terminal_event = terminal_event

    def create_thread(self, owner_id: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        assert body
        return {"threadId": str(body["threadId"])}

    def start_turn(self, owner_id: str, thread_id: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        prompt = str(body["input"][0]["text"])
        nonce = prompt.split('{"proof":"', 1)[1].split('"}', 1)[0]
        artifact = self.workspace / canary.artifact_relative_path("run")
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps({"proof": nonce}), encoding="utf-8")
        self.turn_body = body
        return {"turnId": "turn-1", "status": "running"}

    def get_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        return {"turnId": turn_id, "status": "completed"}

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        return {"turnId": turn_id, "status": "interrupting"}

    def stream_events(self, owner_id: str, thread_id: str, *, turn_id: str | None = None, **_: Any) -> Any:
        events = [
            {
                "type": "tool.completed",
                "turnId": turn_id,
                "payload": {
                    "item": {
                        "type": "commandExecution",
                        "command": "mkdir -p .codex-broker-command-canary/run && printf ... > result.json",
                        "exitCode": self.exit_code,
                    }
                },
            }
        ]
        if self.terminal_event:
            events.append({"type": "turn.completed", "turnId": turn_id, "payload": {}})
        return iter(events)


class CommandToolCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.config = canary.CanaryConfig(
            owner_id="release-owner",
            profile="release-profile",
            auth_principal_id="release-principal",
            workspace=Path("/workspaces/command-canary"),
            local_workspace=self.workspace,
            poll_seconds=0.001,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_verifies_native_command_success_and_exact_artifact(self) -> None:
        broker = FakeBroker(self.workspace)
        result = canary.run_canary(broker, self.config, run_id="run", nonce="test-nonce", sleep=lambda _: None)
        self.assertTrue(result["verified"])
        self.assertEqual(result["command"], {"type": "commandExecution", "exitCode": 0})
        self.assertEqual(
            broker.turn_body["codexOptions"],
            {
                "sandbox": "workspace-write",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
            },
        )

    def test_rejects_artifact_with_wrong_content(self) -> None:
        broker = FakeBroker(self.workspace)
        original = canary.command_evidence

        def corrupt_then_verify(*args: Any, **kwargs: Any) -> dict[str, Any]:
            artifact = self.workspace / canary.artifact_relative_path("run")
            artifact.write_text('{"proof":"wrong"}', encoding="utf-8")
            return original(*args, **kwargs)

        with patch.object(canary, "command_evidence", side_effect=corrupt_then_verify):
            with self.assertRaisesRegex(canary.CanaryError, "does not match"):
                canary.run_canary(broker, self.config, run_id="run", nonce="test-nonce", sleep=lambda _: None)

    def test_rejects_failed_command_even_when_artifact_exists(self) -> None:
        with self.assertRaisesRegex(canary.CanaryError, "no successful"):
            canary.run_canary(
                FakeBroker(self.workspace, exit_code=1),
                self.config,
                run_id="run",
                nonce="test-nonce",
                sleep=lambda _: None,
            )

    def test_rejects_missing_terminal_event(self) -> None:
        with self.assertRaisesRegex(canary.CanaryError, "terminal event"):
            canary.run_canary(
                FakeBroker(self.workspace, terminal_event=False),
                self.config,
                run_id="run",
                nonce="test-nonce",
                sleep=lambda _: None,
            )

    def test_requires_broker_key_for_cli(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(canary.CanaryError, "must explicitly provide"):
                canary.main(
                    [
                        "--base-url",
                        "http://127.0.0.1:3400",
                        "--owner",
                        "release-owner",
                        "--profile",
                        "release-profile",
                        "--workspace",
                        "/workspaces/command-canary",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
