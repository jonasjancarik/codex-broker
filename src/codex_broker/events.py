from __future__ import annotations

from typing import Any


DECISION_APPROVAL_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
LEGACY_APPROVAL_REQUEST_METHODS = {
    "applyPatchApproval",
    "execCommandApproval",
}
PERMISSIONS_APPROVAL_REQUEST_METHOD = "item/permissions/requestApproval"
APPROVAL_REQUEST_METHODS = (
    DECISION_APPROVAL_REQUEST_METHODS
    | LEGACY_APPROVAL_REQUEST_METHODS
    | {PERMISSIONS_APPROVAL_REQUEST_METHOD}
)
USER_INPUT_REQUEST_METHOD = "item/tool/requestUserInput"
USER_INPUT_RESOLVED_METHOD = "item/tool/requestUserInput/resolved"
MCP_ELICITATION_REQUEST_METHOD = "mcpServer/elicitation/request"
MCP_ELICITATION_RESOLVED_METHOD = "mcpServer/elicitation/resolved"


def approval_kind(method: str | None) -> str | None:
    if method == "item/commandExecution/requestApproval":
        return "commandExecution"
    if method == "item/fileChange/requestApproval":
        return "fileChange"
    if method == PERMISSIONS_APPROVAL_REQUEST_METHOD:
        return "permissions"
    if method == "applyPatchApproval":
        return "applyPatch"
    if method == "execCommandApproval":
        return "execCommand"
    return None


def is_tool_item(item: dict[str, Any]) -> bool:
    item_type = str(item.get("type") or "").lower()
    return any(marker in item_type for marker in ("tool", "command", "function", "mcp"))


def reasoning_summary_id(params: dict[str, Any]) -> str | None:
    item_id = params.get("itemId")
    summary_index = params.get("summaryIndex")
    if not isinstance(item_id, str) or not isinstance(summary_index, int):
        return None
    return f"{item_id}:{summary_index}"


def normalize_app_server_event(
    method: str,
    params: dict[str, Any],
    *,
    codex_thread_id: str | None,
    codex_turn_id: str | None,
) -> tuple[str, dict[str, Any]]:
    if method == "thread/started":
        return "thread.started", {"thread": params.get("thread"), "threadId": codex_thread_id}
    if method == "thread/resumed":
        return "thread.resumed", {"thread": params.get("thread"), "threadId": codex_thread_id}
    if method == "thread/status/changed":
        return "thread.status.changed", {"status": params.get("status"), "threadId": params.get("threadId")}
    if method == "thread/settings/updated":
        settings = params.get("threadSettings") if isinstance(params.get("threadSettings"), dict) else {}
        return "thread.settings.updated", {
            "threadId": params.get("threadId") or codex_thread_id,
            "settings": settings,
            "collaborationMode": settings.get("collaborationMode"),
        }
    if method == "thread/goal/updated":
        return "goal.updated", {
            "threadId": params.get("threadId") or codex_thread_id,
            "turnId": params.get("turnId"),
            "goal": params.get("goal"),
        }
    if method == "thread/goal/cleared":
        return "goal.cleared", {
            "threadId": params.get("threadId") or codex_thread_id,
            "turnId": params.get("turnId"),
        }
    if method == "turn/started":
        return "turn.started", {"turn": params.get("turn"), "turnId": codex_turn_id}
    if method == "turn/completed":
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        status = str(turn.get("status") or "completed")
        return ("turn.completed" if status == "completed" else "turn.failed"), {"turn": turn}
    if method == "turn/interrupted":
        return "turn.interrupted", {"params": params}
    if method == "turn/diff/updated":
        return "diff.updated", {"threadId": params.get("threadId"), "turnId": params.get("turnId"), "diff": params.get("diff")}
    if method == "turn/plan/updated":
        return "plan.updated", {
            "threadId": params.get("threadId") or codex_thread_id,
            "turnId": params.get("turnId") or codex_turn_id,
            "explanation": params.get("explanation"),
            "plan": params.get("plan") if isinstance(params.get("plan"), list) else [],
        }
    if method == "item/plan/delta":
        return "plan.delta", {
            "threadId": params.get("threadId") or codex_thread_id,
            "turnId": params.get("turnId") or codex_turn_id,
            "itemId": params.get("itemId"),
            "delta": params.get("delta"),
        }
    if method == "item/agentMessage/delta":
        return "message.delta", {"delta": params.get("delta"), "itemId": params.get("itemId")}
    if method == "item/reasoning/summaryPartAdded":
        return "reasoning.summary.started", {
            "itemId": params.get("itemId"),
            "summaryIndex": params.get("summaryIndex"),
            "summaryId": reasoning_summary_id(params),
        }
    if method == "item/reasoning/summaryTextDelta":
        return "reasoning.summary.delta", {
            "itemId": params.get("itemId"),
            "summaryIndex": params.get("summaryIndex"),
            "summaryId": reasoning_summary_id(params),
            "delta": params.get("delta"),
        }
    if method == "item/autoApprovalReview/started":
        return "approval.review.started", {"params": params}
    if method == "item/autoApprovalReview/completed":
        return "approval.review.completed", {"params": params}
    if method == "item/started":
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        if is_tool_item(item):
            return "tool.started", {"item": item}
        return "item.started", {"item": params.get("item")}
    if method == "item/completed":
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        item_type = item.get("type")
        if item_type == "agentMessage":
            return "message.completed", {"item": item}
        if item_type == "reasoning":
            return "reasoning.completed", {"item": item}
        if item_type == "enteredReviewMode":
            return "review.entered", {"item": item}
        if item_type == "exitedReviewMode":
            return "review.exited", {"item": item}
        if is_tool_item(item):
            return "tool.completed", {"item": item}
        return "item.completed", {"item": item}
    if method in APPROVAL_REQUEST_METHODS:
        return "approval.requested", {
            "kind": approval_kind(method),
            "method": method,
            "interactionId": params.get("interactionId"),
            "params": params,
        }
    if method == "approval/resolved":
        resolved_method = params.get("method")
        return "approval.resolved", {
            "kind": approval_kind(resolved_method if isinstance(resolved_method, str) else None),
            "method": resolved_method,
            "interactionId": params.get("interactionId"),
            "decision": params.get("decision"),
            "response": params.get("response") if isinstance(params.get("response"), dict) else None,
            "source": params.get("source"),
        }
    if method == USER_INPUT_REQUEST_METHOD:
        return "user_input.requested", {
            "method": method,
            "interactionId": params.get("interactionId"),
            "itemId": params.get("itemId"),
            "questions": params.get("questions") if isinstance(params.get("questions"), list) else [],
            "autoResolutionMs": params.get("autoResolutionMs"),
            "params": params,
        }
    if method == USER_INPUT_RESOLVED_METHOD:
        return "user_input.resolved", {
            "method": params.get("method") or USER_INPUT_REQUEST_METHOD,
            "interactionId": params.get("interactionId"),
            "answers": params.get("answers") if isinstance(params.get("answers"), dict) else {},
            "response": params.get("response") if isinstance(params.get("response"), dict) else None,
            "source": params.get("source"),
            "params": params.get("params"),
        }
    if method == MCP_ELICITATION_REQUEST_METHOD:
        return "mcp.elicitation.requested", {
            "method": method,
            "interactionId": params.get("interactionId"),
            "serverName": params.get("serverName"),
            "mode": params.get("mode"),
            "message": params.get("message"),
            "request": params,
        }
    if method == MCP_ELICITATION_RESOLVED_METHOD:
        return "mcp.elicitation.resolved", {
            "method": params.get("method") or MCP_ELICITATION_REQUEST_METHOD,
            "interactionId": params.get("interactionId"),
            "action": params.get("action"),
            "response": params.get("response") if isinstance(params.get("response"), dict) else None,
            "source": params.get("source"),
            "request": params.get("params"),
        }
    if method == "serverRequest/resolved":
        return "server_request.resolved", {
            "threadId": params.get("threadId") or codex_thread_id,
            "requestId": params.get("requestId"),
        }
    if method == "item/commandExecution/outputDelta":
        return "tool.output.delta", {"delta": params.get("delta"), "itemId": params.get("itemId")}
    if method == "item/fileChange/outputDelta":
        return "tool.output.delta", {"delta": params.get("delta"), "itemId": params.get("itemId")}
    if method == "error":
        return "error", {"error": params.get("error") or params}
    if method.startswith("item/") and "tool" in method.lower():
        return "tool.requested", {"method": method, "params": params}
    return "item.completed" if method.endswith("/completed") else "item.started", {"method": method, "params": params}
