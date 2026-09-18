#!/usr/bin/env python3
"""Opt-in real-model release canary for command execution through the Broker API.

The check uses an explicitly selected existing broker owner and auth profile. It
starts one workspace-write turn, requires native command-tool success evidence,
and independently verifies the exact artifact written in the diagnostic
workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from codex_broker.client import CodexBrokerClient


TERMINAL_STATUSES = {"completed", "failed", "interrupted", "timed_out"}


class CanaryError(RuntimeError):
    pass


class BrokerClient(Protocol):
    def create_thread(
        self,
        owner_id: str,
        body: dict[str, Any] | None = None,
        *,
        profile: str | None = None,
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]: ...

    def start_turn(
        self,
        owner_id: str,
        thread_id: str,
        body: dict[str, Any],
        *,
        profile: str | None = None,
        auth_principal_id: str | None = None,
    ) -> dict[str, Any]: ...

    def get_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]: ...

    def interrupt_turn(self, owner_id: str, thread_id: str, turn_id: str) -> dict[str, Any]: ...

    def stream_events(
        self,
        owner_id: str,
        thread_id: str,
        *,
        after: int = 0,
        turn_id: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class CanaryConfig:
    owner_id: str
    profile: str
    auth_principal_id: str | None
    workspace: Path
    local_workspace: Path
    timeout_seconds: float = 300
    interrupt_grace_seconds: float = 30
    poll_seconds: float = 1


def artifact_relative_path(run_id: str) -> Path:
    return Path(".codex-broker-command-canary") / run_id / "result.json"


def canary_prompt(path: Path, nonce: str) -> str:
    return (
        "Use the command tool to create the parent directory and write exactly one JSON object to "
        f"{path.as_posix()} relative to the current workspace. The object must be "
        f'{{"proof":"{nonce}"}} with no other keys. Use a shell command, do not use apply_patch, '
        "do not access credentials or the network, then reply only DONE."
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
            client.interrupt_turn(config.owner_id, thread_id, turn_id)
            interrupted = True
            deadline = clock() + config.interrupt_grace_seconds
        elif interrupted and clock() >= deadline:
            raise CanaryError(f"Timed out waiting for turn {turn_id} to stop after its interrupt request.")
        sleep(config.poll_seconds)


def command_evidence(
    client: BrokerClient,
    config: CanaryConfig,
    thread_id: str,
    turn_id: str,
    artifact_path: Path,
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
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
            commands.append(item)
    if not terminal_seen:
        raise CanaryError(f"Broker event stream did not contain a terminal event for turn {turn_id}.")
    if not commands:
        raise CanaryError(f"Turn {turn_id} has no native command-tool completion evidence.")
    successful = [item for item in commands if item.get("exitCode") == 0]
    if not successful:
        raise CanaryError(f"Turn {turn_id} has no successful native command-tool completion.")
    artifact_name = artifact_path.name.lower()
    matching = [item for item in successful if artifact_name in str(item.get("command") or "").lower()]
    if not matching:
        raise CanaryError(f"Successful command evidence does not name {artifact_path.name}.")
    item = matching[-1]
    return {"type": str(item.get("type") or ""), "exitCode": item.get("exitCode")}


def verify_artifact(path: Path, nonce: str) -> None:
    if not path.is_file():
        raise CanaryError(f"Turn completed without its exact artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanaryError(f"Artifact is not valid JSON: {path}") from exc
    if payload != {"proof": nonce}:
        raise CanaryError(f"Artifact content does not match the canary nonce: {path}")


def run_canary(
    client: BrokerClient,
    config: CanaryConfig,
    *,
    run_id: str | None = None,
    nonce: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    run_id = run_id or uuid.uuid4().hex
    nonce = nonce or secrets.token_hex(24)
    relative_artifact = artifact_relative_path(run_id)
    local_artifact = config.local_workspace / relative_artifact
    if local_artifact.exists():
        raise CanaryError(f"Refusing to reuse an existing canary artifact: {local_artifact}")
    thread = client.create_thread(
        config.owner_id,
        {"threadId": f"command-tool-canary-{run_id}", "cwd": str(config.workspace)},
        profile=config.profile,
        auth_principal_id=config.auth_principal_id,
    )
    thread_id = thread.get("threadId")
    if not isinstance(thread_id, str) or not thread_id:
        raise CanaryError("Broker did not return a threadId for the canary thread.")
    turn = client.start_turn(
        config.owner_id,
        thread_id,
        {
            "input": [{"type": "text", "text": canary_prompt(relative_artifact, nonce)}],
            "codexOptions": {
                "sandbox": "workspace-write",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
            },
        },
        profile=config.profile,
        auth_principal_id=config.auth_principal_id,
    )
    turn_id = turn.get("turnId")
    if not isinstance(turn_id, str) or not turn_id:
        raise CanaryError("Broker did not return a turnId for the canary turn.")
    completed = wait_for_terminal_turn(client, config, thread_id, turn_id, clock=clock, sleep=sleep)
    if completed.get("status") != "completed":
        raise CanaryError(f"Canary turn {turn_id} ended as {completed.get('status')!r}: {completed.get('error')!r}")
    evidence = command_evidence(client, config, thread_id, turn_id, relative_artifact)
    verify_artifact(local_artifact, nonce)
    return {
        "verified": True,
        "threadId": thread_id,
        "turnId": turn_id,
        "artifactPath": str(local_artifact),
        "command": evidence,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CODEX_BROKER_BASE_URL"),
        required=os.environ.get("CODEX_BROKER_BASE_URL") is None,
    )
    parser.add_argument(
        "--owner",
        default=os.environ.get("CODEX_BROKER_OWNER_ID"),
        required=os.environ.get("CODEX_BROKER_OWNER_ID") is None,
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("CODEX_BROKER_PROFILE"),
        required=os.environ.get("CODEX_BROKER_PROFILE") is None,
        help="Existing Codex auth profile to use; it must be selected explicitly.",
    )
    parser.add_argument("--auth-principal-id", default=os.environ.get("CODEX_BROKER_AUTH_PRINCIPAL_ID"))
    parser.add_argument("--workspace", type=Path, required=True, help="Absolute diagnostic workspace visible to the broker.")
    parser.add_argument("--local-workspace", type=Path, help="Same workspace as mounted at this process; defaults to --workspace.")
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--interrupt-grace-seconds", type=float, default=30)
    parser.add_argument("--poll-seconds", type=float, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.timeout_seconds <= 0 or args.interrupt_grace_seconds <= 0 or args.poll_seconds <= 0:
        raise CanaryError("Timeout, interrupt grace, and poll intervals must be greater than zero.")
    if not args.workspace.is_absolute():
        raise CanaryError("Broker workspace must be absolute.")
    internal_key = os.environ.get("CODEX_BROKER_INTERNAL_KEY")
    if not internal_key:
        raise CanaryError("CODEX_BROKER_INTERNAL_KEY must explicitly provide the broker credential.")
    config = CanaryConfig(
        owner_id=args.owner,
        profile=args.profile,
        auth_principal_id=args.auth_principal_id,
        workspace=args.workspace,
        local_workspace=(args.local_workspace or args.workspace).resolve(),
        timeout_seconds=args.timeout_seconds,
        interrupt_grace_seconds=args.interrupt_grace_seconds,
        poll_seconds=args.poll_seconds,
    )
    client = CodexBrokerClient(args.base_url, internal_key=internal_key)
    print(json.dumps(run_canary(client, config), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanaryError as exc:
        print(f"command tool canary failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
