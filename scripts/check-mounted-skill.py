#!/usr/bin/env python3
"""Opt-in real-model regression canary for per-turn mounted skill snapshots.

This intentionally talks only to the public broker HTTP API.  It does not
create or copy credentials, and it is not a unit-test fixture: invoking it
starts two real Codex turns with the selected existing profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from codex_broker.client import CodexBrokerClient


DEFAULT_BUNDLE_ID = "mounted-skill-canary-v1"
DEFAULT_SKILL_NAME = "mounted-skill-canary"
FIXTURE_RELATIVE_PATH = Path("fixtures/current-snapshot.txt")
TERMINAL_STATUSES = {"completed", "failed", "interrupted", "timed_out"}
DISCOVERY_MARKERS = ("events.jsonl", "transcript", "/logs/", "\\logs\\")


class CanaryError(RuntimeError):
    pass


class BrokerClient(Protocol):
    def create_thread(self, owner_id: str, body: dict[str, Any] | None = None, *, profile: str | None = None,
                      auth_principal_id: str | None = None) -> dict[str, Any]: ...

    def start_turn(self, owner_id: str, thread_id: str, body: dict[str, Any], *, profile: str | None = None,
                   auth_principal_id: str | None = None) -> dict[str, Any]: ...

    def get_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]: ...

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]: ...

    def list_audit_logs(self, owner_id: str, *, profile: str | None = None, action: str | None = None,
                        thread_id: str | None = None, turn_id: str | None = None, after: int = 0,
                        limit: int | None = None) -> dict[str, Any]: ...

    def stream_events(self, owner_id: str, thread_id: str, *, after: int = 0,
                      turn_id: str | None = None) -> Any: ...


@dataclass(frozen=True)
class CanaryConfig:
    owner_id: str
    profile: str
    auth_principal_id: str | None
    bundle_id: str
    workspace: Path
    local_workspace: Path
    fixture_dir: Path
    mounted_skill_path: Path
    skill_name: str = DEFAULT_SKILL_NAME
    timeout_seconds: float = 300
    interrupt_grace_seconds: float = 30
    poll_seconds: float = 1


@dataclass(frozen=True)
class ExpectedHashes:
    skill: str
    fixture: str


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_hashes(fixture_dir: Path) -> ExpectedHashes:
    skill = fixture_dir / "SKILL.md"
    fixture = fixture_dir / FIXTURE_RELATIVE_PATH
    if not skill.is_file() or not fixture.is_file():
        raise CanaryError(
            f"Fixture directory must contain SKILL.md and {FIXTURE_RELATIVE_PATH}: {fixture_dir}"
        )
    return ExpectedHashes(skill=sha256(skill), fixture=sha256(fixture))


def proof_prompt(proof_path: Path) -> str:
    # Deliberately do not disclose a skill path, fixture path, or expected bytes.
    return (
        "Run the operator-mounted-skill regression check. Treat only the current turn's attached skill as "
        "authoritative and follow its canary instructions. Write the required JSON proof to "
        f"{proof_path.as_posix()} relative to the current diagnostic workspace. Do not inspect logs, events, "
        "transcripts, other workspaces, or prior-turn paths. Return only a terse completion status."
    )


def wait_for_terminal_turn(
    client: BrokerClient,
    config: CanaryConfig,
    thread_id: str,
    turn_id: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = clock() + config.timeout_seconds
    interrupted = False
    while True:
        turn = client.get_turn(config.owner_id, thread_id, turn_id)
        status = str(turn.get("status") or "")
        if status in TERMINAL_STATUSES:
            if interrupted:
                raise CanaryError(f"Canary turn {turn_id} exceeded its timeout and was interrupted ({status}).")
            return turn
        if not interrupted and clock() >= deadline:
            # Never interrupt a whole thread: an active follow-up must remain identifiable.
            client.interrupt_turn(config.owner_id, thread_id, turn_id)
            interrupted = True
            deadline = clock() + config.interrupt_grace_seconds
        elif interrupted and clock() >= deadline:
            raise CanaryError(f"Timed out waiting for turn {turn_id} to stop after its interrupt request.")
        sleep(config.poll_seconds)


def verify_command_evidence(client: BrokerClient, config: CanaryConfig, thread_id: str, turn_id: str) -> None:
    """Reject log discovery from the actual completed-turn tool events, not model claims."""
    command_items: list[dict[str, Any]] = []
    terminal_seen = False
    for event in client.stream_events(config.owner_id, thread_id, turn_id=turn_id):
        if event.get("turnId") not in {None, turn_id}:
            continue
        event_type = event.get("type")
        if event_type in {"turn.completed", "turn.failed"}:
            terminal_seen = True
            break
        if event_type != "tool.completed":
            continue
        payload = event.get("payload")
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and "command" in str(item.get("type") or "").lower():
            command_items.append(item)
    if not terminal_seen:
        raise CanaryError(f"Broker event stream did not contain a terminal event for turn {turn_id}.")
    if not command_items:
        raise CanaryError(f"Turn {turn_id} has no shell/tool evidence for reading the attached skill.")
    commands = [str(item.get("command") or "") for item in command_items]
    command_text = "\n".join(commands).lower()
    output_text = "\n".join(
        json.dumps(item.get("aggregatedOutput", item.get("output", "")), ensure_ascii=False)
        for item in command_items
    ).lower()
    if any(marker in command_text for marker in DISCOVERY_MARKERS):
        raise CanaryError(f"Turn {turn_id} shell command used forbidden log or transcript discovery.")
    if "permission denied" in output_text:
        raise CanaryError(f"Turn {turn_id} shell command could not read the attached skill.")
    successful_commands = "\n".join(
        str(item.get("command") or "") for item in command_items if item.get("exitCode") == 0
    ).lower()
    if "skill.md" not in successful_commands or FIXTURE_RELATIVE_PATH.name.lower() not in successful_commands:
        raise CanaryError(f"Turn {turn_id} tool evidence does not show reads of the skill and relative fixture.")


def proof_relative_path(run_id: str, sequence: int) -> Path:
    return Path(".codex-broker-skill-canary") / run_id / f"proof-{sequence}.json"


def _proof_path(config: CanaryConfig, run_id: str, sequence: int) -> Path:
    return config.local_workspace / proof_relative_path(run_id, sequence)


def read_and_verify_proof(path: Path, expected: ExpectedHashes, config: CanaryConfig, turn_id: str) -> dict[str, str]:
    if not path.is_file():
        raise CanaryError(f"Turn completed without its exact proof file: {path}")
    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryError(f"Proof is not valid JSON: {path}") from exc
    if not isinstance(proof, dict):
        raise CanaryError(f"Proof must be a JSON object: {path}")
    required = ("skillPath", "fixturePath", "skillSha256", "fixtureSha256", "readMethod")
    if any(not isinstance(proof.get(key), str) for key in required):
        raise CanaryError(f"Proof is missing required string fields: {path}")
    skill_path = Path(str(proof["skillPath"]))
    fixture_path = Path(str(proof["fixturePath"]))
    expected_tail = Path(turn_id) / ".agents" / "skills" / config.skill_name / "SKILL.md"
    if not skill_path.is_absolute() or skill_path.parts[-len(expected_tail.parts):] != expected_tail.parts:
        raise CanaryError(f"Proof did not use the broker's attached skill path: {skill_path}")
    if fixture_path != skill_path.parent / FIXTURE_RELATIVE_PATH:
        raise CanaryError("Proof did not read the fixture relative to the attached skill directory.")
    if proof["skillSha256"] != expected.skill or proof["fixtureSha256"] != expected.fixture:
        raise CanaryError("Proof hashes do not match the expected mounted-skill fixture bytes.")
    if proof["readMethod"] != "attached-skill-relative":
        raise CanaryError("Proof did not attest to reading the attached skill and relative fixture.")
    if any(marker in str(value).lower() for marker in DISCOVERY_MARKERS for value in (skill_path, fixture_path)):
        raise CanaryError("Proof path indicates forbidden log or transcript discovery.")
    return {key: str(proof[key]) for key in required}


def verify_snapshot_audit(client: BrokerClient, config: CanaryConfig, thread_id: str, turn_id: str) -> dict[str, Any]:
    response = client.list_audit_logs(
        config.owner_id,
        profile=config.profile,
        action="security.bundle_skill_snapshot",
        thread_id=thread_id,
        turn_id=turn_id,
        limit=20,
    )
    logs = response.get("auditLogs")
    if not isinstance(logs, list):
        raise CanaryError("Broker returned malformed skill snapshot audit logs.")
    matching = [entry for entry in logs if isinstance(entry, dict) and entry.get("turnId") == turn_id]
    if len(matching) != 1:
        raise CanaryError(f"Expected exactly one skill snapshot audit record for turn {turn_id}.")
    payload = matching[0].get("payload")
    skills = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(skills, list) or len(skills) != 1 or not isinstance(skills[0], dict):
        raise CanaryError(f"Skill snapshot audit record for {turn_id} has no single mounted skill provenance.")
    skill = skills[0]
    if skill.get("name") != config.skill_name or skill.get("sourcePath") != str(config.mounted_skill_path):
        raise CanaryError(f"Skill snapshot audit record for {turn_id} did not identify the expected mounted skill.")
    digest = skill.get("snapshotSha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise CanaryError(f"Skill snapshot audit record for {turn_id} has no valid snapshot digest.")
    return matching[0]


def run_canary(
    client: BrokerClient,
    config: CanaryConfig,
    *,
    run_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    expected = expected_hashes(config.fixture_dir)
    run_id = run_id or uuid.uuid4().hex
    thread = client.create_thread(
        config.owner_id,
        {"threadId": f"mounted-skill-canary-{run_id}", "bundleId": config.bundle_id, "cwd": str(config.workspace)},
        profile=config.profile,
        auth_principal_id=config.auth_principal_id,
    )
    thread_id = thread.get("threadId")
    if not isinstance(thread_id, str) or not thread_id:
        raise CanaryError("Broker did not return a threadId for the canary thread.")
    proofs: list[dict[str, str]] = []
    turns: list[str] = []
    audits: list[dict[str, Any]] = []
    for sequence in (1, 2):
        proof_path = _proof_path(config, run_id, sequence)
        turn = client.start_turn(
            config.owner_id,
            thread_id,
            {"input": [{"type": "text", "text": proof_prompt(proof_relative_path(run_id, sequence))}]},
            profile=config.profile,
            auth_principal_id=config.auth_principal_id,
        )
        turn_id = turn.get("turnId")
        if not isinstance(turn_id, str) or not turn_id:
            raise CanaryError(f"Broker did not return a turnId for canary sequence {sequence}.")
        completed = wait_for_terminal_turn(client, config, thread_id, turn_id, clock=clock, sleep=sleep)
        if completed.get("status") != "completed":
            raise CanaryError(f"Canary turn {turn_id} ended as {completed.get('status')!r}: {completed.get('error')!r}")
        verify_command_evidence(client, config, thread_id, turn_id)
        proofs.append(read_and_verify_proof(proof_path, expected, config, turn_id))
        audits.append(verify_snapshot_audit(client, config, thread_id, turn_id))
        turns.append(turn_id)
    if proofs[0]["skillPath"] == proofs[1]["skillPath"]:
        raise CanaryError("The follow-up turn reused the first turn's attached skill snapshot path.")
    return {
        "verified": True,
        "threadId": thread_id,
        "turnIds": turns,
        "proofPaths": [str(_proof_path(config, run_id, sequence)) for sequence in (1, 2)],
        "snapshotAuditIds": [audit.get("id") for audit in audits],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("CODEX_BROKER_BASE_URL"), required=os.environ.get("CODEX_BROKER_BASE_URL") is None)
    parser.add_argument("--owner", default=os.environ.get("CODEX_BROKER_OWNER_ID"), required=os.environ.get("CODEX_BROKER_OWNER_ID") is None)
    parser.add_argument("--profile", default=os.environ.get("CODEX_BROKER_PROFILE", "default"))
    parser.add_argument("--auth-principal-id", default=os.environ.get("CODEX_BROKER_AUTH_PRINCIPAL_ID"))
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--workspace", type=Path, required=True, help="Absolute diagnostic workspace path visible to the broker.")
    parser.add_argument("--local-workspace", type=Path, help="Same workspace as mounted at this process; defaults to --workspace.")
    parser.add_argument("--fixture-dir", type=Path, required=True, help="Local copy of the exact mounted skill directory.")
    parser.add_argument("--mounted-skill-path", type=Path, required=True, help="Absolute sourcePath reported by broker skill snapshot audits.")
    parser.add_argument("--skill-name", default=DEFAULT_SKILL_NAME)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--interrupt-grace-seconds", type=float, default=30)
    parser.add_argument("--poll-seconds", type=float, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.timeout_seconds <= 0 or args.interrupt_grace_seconds <= 0 or args.poll_seconds <= 0:
        raise CanaryError("Timeout, interrupt grace, and poll intervals must be greater than zero.")
    if not args.workspace.is_absolute() or not args.mounted_skill_path.is_absolute():
        raise CanaryError("Broker workspace and mounted-skill paths must be absolute.")
    config = CanaryConfig(
        owner_id=args.owner,
        profile=args.profile,
        auth_principal_id=args.auth_principal_id,
        bundle_id=args.bundle_id,
        # This path belongs to the broker's filesystem, not this process's host.
        workspace=args.workspace,
        local_workspace=(args.local_workspace or args.workspace).resolve(),
        fixture_dir=args.fixture_dir.resolve(),
        mounted_skill_path=args.mounted_skill_path,
        skill_name=args.skill_name,
        timeout_seconds=args.timeout_seconds,
        interrupt_grace_seconds=args.interrupt_grace_seconds,
        poll_seconds=args.poll_seconds,
    )
    client = CodexBrokerClient(args.base_url, internal_key=os.environ.get("CODEX_BROKER_INTERNAL_KEY"))
    print(json.dumps(run_canary(client, config), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanaryError as exc:
        print(f"mounted skill canary failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
