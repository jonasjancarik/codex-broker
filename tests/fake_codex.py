from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


stdout_lock = threading.Lock()
response_condition = threading.Condition()
server_responses: dict[int, dict[str, Any]] = {}
next_thread = 1
next_turn = 1


def send(message: dict[str, Any]) -> None:
    with stdout_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def request_server(method: str, request_id: int, params: dict[str, Any]) -> dict[str, Any]:
    send({"id": request_id, "method": method, "params": params})
    deadline = time.monotonic() + float(os.environ.get("FAKE_CODEX_SERVER_REQUEST_TIMEOUT", "5"))
    with response_condition:
        while request_id not in server_responses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"server request {method} timed out")
            response_condition.wait(timeout=remaining)
        return server_responses.pop(request_id)


def auth_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or ".codex").resolve()


def handle_login(args: list[str]) -> int:
    home = auth_home()
    home.mkdir(parents=True, exist_ok=True)
    auth_file = home / "auth.json"
    if args[:1] == ["status"]:
        if auth_file.exists():
            print("Logged in")
            return 0
        print("Not logged in", file=sys.stderr)
        return 1
    if "--with-api-key" in args:
        _ = sys.stdin.read()
        auth_file.write_text('{"OPENAI_API_KEY":"redacted"}', encoding="utf-8")
        print("Logged in with API key")
        return 0
    if "--device-auth" in args:
        line = "Open https://example.test/device and enter code ABCD-1234. This code expires in 15 minutes."
        if os.environ.get("FAKE_CODEX_DEVICE_AUTH_SECRET_OUTPUT") == "1":
            line += " access_token=secret-device-token"
        print(line)
        sys.stdout.flush()
        time.sleep(float(os.environ.get("FAKE_CODEX_DEVICE_AUTH_DELAY", "0.05")))
        auth_file.write_text('{"CHATGPT":"redacted"}', encoding="utf-8")
        print("Logged in")
        return 0
    return 0


def handle_logout() -> int:
    auth_file = auth_home() / "auth.json"
    if auth_file.exists():
        auth_file.unlink()
    print("Logged out")
    return 0


def handle_exec(args: list[str]) -> int:
    _ = args
    _ = sys.stdin.read()
    auth_file = auth_home() / "auth.json"
    if not auth_file.exists():
        print("Not logged in", file=sys.stderr)
        return 1
    if os.environ.get("FAKE_CODEX_AUTH_REFRESH_FAILURE") == "1":
        print(
            (
                "failed to refresh available models: unexpected status 401 Unauthorized: "
                "Your authentication token has been invalidated. Please try signing in again., "
                "auth error code: token_invalidated"
            ),
            file=sys.stderr,
        )
        print("refresh_token_invalidated", file=sys.stderr)
        return 1
    if os.environ.get("FAKE_CODEX_EXEC_FAILURE") == "1":
        print("Codex exec failed", file=sys.stderr)
        return 2
    print(json.dumps({"type": "thread.started", "thread_id": "probe_thread"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}))
    return 0


def complete_turn(thread_id: str, turn_id: str) -> None:
    delay = float(os.environ.get("FAKE_CODEX_TURN_DELAY", "0.01"))
    request_base = 9000 + int(turn_id.rsplit("_", 1)[-1]) * 10
    send({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id}}})
    if os.environ.get("FAKE_CODEX_REQUEST_APPROVAL") == "1":
        request_server(
            "item/commandExecution/requestApproval",
            request_base,
            {"threadId": thread_id, "turnId": turn_id, "command": "printf test"},
        )
    if os.environ.get("FAKE_CODEX_REQUEST_USER_INPUT") == "1":
        request_server(
            "item/tool/requestUserInput",
            request_base + 1,
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "input_1",
                "questions": [{"id": "color", "question": "Color?"}],
                "autoResolutionMs": 5000,
            },
        )
    if os.environ.get("FAKE_CODEX_REQUEST_MCP_ELICITATION") == "1":
        request_server(
            "mcpServer/elicitation/request",
            request_base + 2,
            {"threadId": thread_id, "turnId": turn_id, "serverName": "host", "mode": "form", "message": "Continue?"},
        )
    send({"method": "item/agentMessage/delta", "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "msg1", "delta": "hello"}})
    time.sleep(delay)
    send(
        {
            "method": "item/completed",
            "params": {"threadId": thread_id, "turnId": turn_id, "item": {"id": "msg1", "type": "agentMessage", "text": "hello"}},
        }
    )
    if os.environ.get("FAKE_CODEX_TURN_COMPLETED_ERROR"):
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "status": "failed",
                        "error": {"message": os.environ["FAKE_CODEX_TURN_COMPLETED_ERROR"]},
                    },
                },
            }
        )
        return
    send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}}})


def handle_app_server() -> int:
    global next_thread, next_turn
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if request_id is not None and ("result" in message or "error" in message):
            with response_condition:
                server_responses[int(request_id)] = message
                response_condition.notify_all()
            continue
        if method is None:
            continue
        if method == "initialized":
            continue
        if method == "initialize":
            send({"id": request_id, "result": {"serverInfo": {"name": "fake-codex"}}})
        elif method == "model/list":
            models = [
                {
                    "id": "sol-preset",
                    "model": "gpt-5.6-sol",
                    "upgrade": None,
                    "upgradeInfo": None,
                    "availabilityNux": None,
                    "displayName": "GPT-5.6 Sol",
                    "description": "Fast coding model.",
                    "hidden": False,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Lower latency"},
                        {"reasoningEffort": "max", "description": "Maximum reasoning"},
                        {"reasoningEffort": "ultra", "description": "Maximum reasoning with delegation"},
                    ],
                    "defaultReasoningEffort": "medium",
                    "inputModalities": ["text", "image"],
                    "supportsPersonality": True,
                    "additionalSpeedTiers": ["fast"],
                    "serviceTiers": [
                        {"id": "fast", "name": "Fast", "description": "Lower-latency execution"},
                    ],
                    "defaultServiceTier": None,
                    "isDefault": True,
                },
                {
                    "id": "terra-preset",
                    "model": "gpt-5.6-terra",
                    "upgrade": None,
                    "upgradeInfo": None,
                    "availabilityNux": None,
                    "displayName": "GPT-5.6 Terra",
                    "description": "Hidden test model.",
                    "hidden": True,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "medium", "description": "Balanced reasoning"},
                    ],
                    "defaultReasoningEffort": "medium",
                    "inputModalities": ["text", "image"],
                    "supportsPersonality": False,
                    "additionalSpeedTiers": [],
                    "serviceTiers": [],
                    "defaultServiceTier": None,
                    "isDefault": False,
                },
            ]
            if not params.get("includeHidden"):
                models = [model for model in models if not model["hidden"]]
            offset = int(params.get("cursor") or 0)
            limit = int(params.get("limit") or 20)
            page = models[offset : offset + limit]
            next_cursor = str(offset + limit) if offset + limit < len(models) else None
            send({"id": request_id, "result": {"data": page, "nextCursor": next_cursor}})
        elif method == "account/usage/read":
            send(
                {
                    "id": request_id,
                    "result": {
                        "totalTokens": 1200,
                        "daily": [{"date": "2026-07-11", "tokens": 1200}],
                    },
                }
            )
        elif method == "account/rateLimits/read":
            send(
                {
                    "id": request_id,
                    "result": {
                        "primary": {"usedPercent": 25, "resetsAt": "2026-07-11T16:00:00Z"},
                        "resetCredits": 1,
                    },
                }
            )
        elif method == "account/rateLimitResetCredit/consume":
            send(
                {
                    "id": request_id,
                    "result": {
                        "consumed": True,
                        "idempotencyKey": params.get("idempotencyKey"),
                    },
                }
            )
        elif method == "thread/start":
            thread_id = f"thr_fake_{next_thread}"
            next_thread += 1
            send({"id": request_id, "result": {"thread": {"id": thread_id}}})
            send({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
        elif method == "thread/resume":
            send({"id": request_id, "result": {"thread": {"id": params.get("threadId")}}})
            send({"method": "thread/resumed", "params": {"threadId": params.get("threadId"), "thread": {"id": params.get("threadId")}}})
        elif method == "turn/start":
            if os.environ.get("FAKE_CODEX_HANG_ON_TURN_START_ONCE") == "1":
                marker = auth_home() / ".fake-hung-turn-start-once"
                if not marker.exists():
                    marker.write_text("hung", encoding="utf-8")
                    while True:
                        time.sleep(60)
            if os.environ.get("FAKE_CODEX_CRASH_ON_TURN_ONCE") == "1":
                marker = auth_home() / ".fake-crashed-once"
                if not marker.exists():
                    marker.write_text("crashed", encoding="utf-8")
                    os._exit(42)
            if os.environ.get("FAKE_CODEX_CRASH_ON_TURN") == "1":
                os._exit(42)
            turn_id = f"turn_fake_{next_turn}"
            next_turn += 1
            thread_id = str(params.get("threadId"))
            send({"id": request_id, "result": {"turn": {"id": turn_id}}})
            if os.environ.get("FAKE_CODEX_AUTH_REFRESH_FAILURE") == "1":
                send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "status": "failed",
                                "error": {
                                    "message": (
                                        "Your access token could not be refreshed because your refresh token was "
                                        "already used. Please log out and sign in again."
                                    )
                                },
                            },
                        },
                    }
                )
                continue
            threading.Thread(target=complete_turn, args=(thread_id, turn_id), daemon=True).start()
        elif method == "turn/steer":
            send({"id": request_id, "result": {"accepted": True}})
        elif method == "turn/interrupt":
            send({"id": request_id, "result": {"accepted": True}})
            send({"method": "turn/interrupted", "params": {"threadId": params.get("threadId"), "turnId": params.get("turnId")}})
        else:
            send({"id": request_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print(os.environ.get("FAKE_CODEX_VERSION", "fake-codex 0.1"))
        return 0
    if args[:1] == ["login"]:
        return handle_login(args[1:])
    if args[:1] == ["logout"]:
        return handle_logout()
    if "exec" in args:
        return handle_exec(args)
    if "app-server" in args:
        return handle_app_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
