from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-mounted-skill.py"
SPEC = importlib.util.spec_from_file_location("mounted_skill_canary", SCRIPT)
assert SPEC and SPEC.loader
canary = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = canary
SPEC.loader.exec_module(canary)


class FakeBroker:
    def __init__(self, *, reuse_overlay: bool = False, bad_audit: bool = False) -> None:
        self.reuse_overlay = reuse_overlay
        self.bad_audit = bad_audit
        self.turns = 0
        self.interrupts: list[tuple[str, str, str]] = []

    def create_thread(self, owner_id: str, body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.thread_id = str(body["threadId"])
        return {"threadId": self.thread_id}

    def start_turn(self, owner_id: str, thread_id: str, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.turns += 1
        return {"turnId": f"turn-{self.turns}", "status": "running"}

    def get_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        return {"turnId": turn_id, "status": "completed"}

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.interrupts.append((owner_id, thread_id, turn_id))
        return {"turnId": turn_id, "status": "interrupting"}

    def stream_events(self, owner_id: str, thread_id: str, *, turn_id: str | None = None, **_: Any) -> Any:
        assert turn_id
        return iter(
            [
                {
                    "type": "tool.completed",
                    "turnId": turn_id,
                    "payload": {
                        "item": {
                            "type": "commandExecution",
                            "command": "sha256sum SKILL.md fixtures/current-snapshot.txt",
                            "exitCode": 0,
                            "aggregatedOutput": "Read instructions: do not inspect logs or transcripts.",
                        }
                    },
                },
                {"type": "turn.completed", "turnId": turn_id, "payload": {"turn": {"status": "completed"}}},
            ]
        )

    def list_audit_logs(self, owner_id: str, *, turn_id: str | None = None, **_: Any) -> dict[str, Any]:
        assert turn_id
        source = "/wrong/source" if self.bad_audit else "/bundles/mounted-skill-canary-v1/skills/mounted-skill-canary"
        return {
            "auditLogs": [
                {
                    "id": f"audit-{turn_id}",
                    "turnId": turn_id,
                    "payload": {
                        "skills": [
                            {"name": "mounted-skill-canary", "sourcePath": source, "snapshotSha256": "a" * 64}
                        ]
                    },
                }
            ]
        }


class TimeoutBroker(FakeBroker):
    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False

    def get_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        return {"turnId": turn_id, "status": "completed" if self.interrupted else "running"}

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.interrupted = True
        return super().interrupt_turn(owner_id, thread_id, turn_id)


class MountedSkillCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.fixture = self.root / "fixture"
        (self.fixture / "fixtures").mkdir(parents=True)
        (self.fixture / "SKILL.md").write_text("# Canary\n", encoding="utf-8")
        (self.fixture / "fixtures" / "current-snapshot.txt").write_text("fixture\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def config(self) -> Any:
        return canary.CanaryConfig(
            owner_id="release-owner",
            profile="existing-profile",
            auth_principal_id="existing-principal",
            bundle_id="mounted-skill-canary-v1",
            workspace=Path("/workspaces/skill-canary"),
            local_workspace=self.workspace,
            fixture_dir=self.fixture,
            mounted_skill_path=Path("/bundles/mounted-skill-canary-v1/skills/mounted-skill-canary"),
            poll_seconds=0.001,
        )

    def write_proofs(self, *, reuse_overlay: bool = False) -> None:
        expected = canary.expected_hashes(self.fixture)
        for sequence in (1, 2):
            turn_id = "turn-1" if reuse_overlay else f"turn-{sequence}"
            proof = canary._proof_path(self.config(), "run", sequence)
            proof.parent.mkdir(parents=True, exist_ok=True)
            skill = Path(f"/broker/data/workspaces/overlays/{turn_id}/.agents/skills/mounted-skill-canary/SKILL.md")
            proof.write_text(
                json.dumps(
                    {
                        "skillPath": str(skill),
                        "fixturePath": str(skill.parent / "fixtures/current-snapshot.txt"),
                        "skillSha256": expected.skill,
                        "fixtureSha256": expected.fixture,
                        "readMethod": "attached-skill-relative",
                    }
                ),
                encoding="utf-8",
            )

    def test_verifies_two_current_turn_skill_snapshots(self) -> None:
        self.write_proofs()
        result = canary.run_canary(FakeBroker(), self.config(), run_id="run", sleep=lambda _: None)
        self.assertTrue(result["verified"])
        self.assertEqual(result["turnIds"], ["turn-1", "turn-2"])

    def test_rejects_proof_reusing_an_old_turn_overlay(self) -> None:
        self.write_proofs(reuse_overlay=True)
        with self.assertRaisesRegex(canary.CanaryError, "attached skill path"):
            canary.run_canary(FakeBroker(), self.config(), run_id="run", sleep=lambda _: None)

    def test_rejects_missing_proof_without_discovering_an_alternative_file(self) -> None:
        with self.assertRaisesRegex(canary.CanaryError, "exact proof file"):
            canary.run_canary(FakeBroker(), self.config(), run_id="run", sleep=lambda _: None)

    def test_rejects_a_fixture_that_was_not_read_relative_to_the_attached_skill(self) -> None:
        self.write_proofs()
        proof_path = canary._proof_path(self.config(), "run", 1)
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["fixturePath"] = "/somewhere-else/current-snapshot.txt"
        proof_path.write_text(json.dumps(proof), encoding="utf-8")
        with self.assertRaisesRegex(canary.CanaryError, "relative to the attached skill"):
            canary.run_canary(FakeBroker(), self.config(), run_id="run", sleep=lambda _: None)

    def test_rejects_audit_for_a_different_mounted_source(self) -> None:
        self.write_proofs()
        with self.assertRaisesRegex(canary.CanaryError, "expected mounted skill"):
            canary.run_canary(FakeBroker(bad_audit=True), self.config(), run_id="run", sleep=lambda _: None)

    def test_timeout_interrupts_only_the_active_turn_and_still_fails(self) -> None:
        broker = TimeoutBroker()
        config = self.config()
        config = canary.CanaryConfig(**{**config.__dict__, "timeout_seconds": 0.1, "interrupt_grace_seconds": 5})
        times = iter((0.0, 1.0, 1.0))
        with self.assertRaisesRegex(canary.CanaryError, "exceeded its timeout"):
            canary.wait_for_terminal_turn(broker, config, "thread-a", "turn-a", clock=lambda: next(times), sleep=lambda _: None)
        self.assertEqual(broker.interrupts, [("release-owner", "thread-a", "turn-a")])

    def test_rejects_transcript_discovery_even_when_proofs_are_valid(self) -> None:
        self.write_proofs()
        broker = FakeBroker()
        events = list(broker.stream_events("release-owner", "thread", turn_id="turn-1"))
        events[0]["payload"]["item"]["command"] = "cat logs/events.jsonl; sha256sum SKILL.md fixtures/current-snapshot.txt"
        with patch.object(broker, "stream_events", return_value=iter(events)):
            with self.assertRaisesRegex(canary.CanaryError, "forbidden log"):
                canary.run_canary(broker, self.config(), run_id="run", sleep=lambda _: None)

    def test_rejects_permission_denial_in_native_command_output(self) -> None:
        broker = FakeBroker()
        events = list(broker.stream_events("release-owner", "thread", turn_id="turn-1"))
        events[0]["payload"]["item"]["aggregatedOutput"] = "cat: SKILL.md: Permission denied"
        with patch.object(broker, "stream_events", return_value=iter(events)):
            with self.assertRaisesRegex(canary.CanaryError, "could not read"):
                canary.verify_command_evidence(broker, self.config(), "thread", "turn-1")

    def test_unrelated_success_does_not_mask_a_failed_skill_read(self) -> None:
        broker = FakeBroker()
        events = list(broker.stream_events("release-owner", "thread", turn_id="turn-1"))
        events[0]["payload"]["item"]["exitCode"] = 1
        events.insert(1, {"type": "tool.completed", "turnId": "turn-1", "payload": {
            "item": {"type": "commandExecution", "command": "mkdir -p proof", "exitCode": 0}
        }})
        with patch.object(broker, "stream_events", return_value=iter(events)):
            with self.assertRaisesRegex(canary.CanaryError, "does not show reads"):
                canary.verify_command_evidence(broker, self.config(), "thread", "turn-1")
