from __future__ import annotations

from typing import Any

from .events import (
    DECISION_APPROVAL_REQUEST_METHODS,
    LEGACY_APPROVAL_REQUEST_METHODS,
    MCP_ELICITATION_REQUEST_METHOD,
    MCP_ELICITATION_RESOLVED_METHOD,
    PERMISSIONS_APPROVAL_REQUEST_METHOD,
    USER_INPUT_REQUEST_METHOD,
    USER_INPUT_RESOLVED_METHOD,
)


INTERACTION_REQUEST_METHODS = (
    DECISION_APPROVAL_REQUEST_METHODS
    | LEGACY_APPROVAL_REQUEST_METHODS
    | {PERMISSIONS_APPROVAL_REQUEST_METHOD, USER_INPUT_REQUEST_METHOD, MCP_ELICITATION_REQUEST_METHOD}
)

DECISION_APPROVAL_VALUES = {"accept", "acceptForSession", "decline", "cancel"}
LEGACY_APPROVAL_VALUES = {"approved", "approved_for_session", "denied", "timed_out", "abort"}
MCP_ELICITATION_ACTIONS = {"accept", "decline", "cancel"}
PERMISSION_SCOPES = {"turn", "session"}


class InteractionResponseError(ValueError):
    pass


def interaction_kind(method: str) -> str:
    if method in DECISION_APPROVAL_REQUEST_METHODS or method in LEGACY_APPROVAL_REQUEST_METHODS:
        return "approval"
    if method == PERMISSIONS_APPROVAL_REQUEST_METHOD:
        return "permissions"
    if method == USER_INPUT_REQUEST_METHOD:
        return "userInput"
    if method == MCP_ELICITATION_REQUEST_METHOD:
        return "mcpElicitation"
    return "serverRequest"


def safe_response_for_method(method: str) -> dict[str, Any]:
    if method in DECISION_APPROVAL_REQUEST_METHODS:
        return {"decision": "decline"}
    if method in LEGACY_APPROVAL_REQUEST_METHODS:
        return {"decision": "denied"}
    if method == PERMISSIONS_APPROVAL_REQUEST_METHOD:
        return {"permissions": {}, "scope": "turn", "strictAutoReview": True}
    if method == USER_INPUT_REQUEST_METHOD:
        return {"answers": {}}
    if method == MCP_ELICITATION_REQUEST_METHOD:
        return {"action": "decline", "content": None, "_meta": None}
    raise InteractionResponseError(f"Unsupported interaction method: {method}")


def response_event_method(method: str) -> str:
    if method in DECISION_APPROVAL_REQUEST_METHODS or method in LEGACY_APPROVAL_REQUEST_METHODS:
        return "approval/resolved"
    if method == PERMISSIONS_APPROVAL_REQUEST_METHOD:
        return "approval/resolved"
    if method == USER_INPUT_REQUEST_METHOD:
        return USER_INPUT_RESOLVED_METHOD
    if method == MCP_ELICITATION_REQUEST_METHOD:
        return MCP_ELICITATION_RESOLVED_METHOD
    raise InteractionResponseError(f"Unsupported interaction method: {method}")


def resolved_notification_params(
    method: str,
    response: dict[str, Any],
    *,
    interaction_id: str,
    request_params: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "method": method,
        "interactionId": interaction_id,
        "source": source,
        "response": response,
        "params": request_params,
    }
    if method == USER_INPUT_REQUEST_METHOD:
        return base | {"answers": response.get("answers") if isinstance(response.get("answers"), dict) else {}}
    if method == MCP_ELICITATION_REQUEST_METHOD:
        return base | {"action": response.get("action")}
    return base | {"decision": approval_decision_for_response(method, response)}


def approval_decision_for_response(method: str, response: dict[str, Any]) -> Any:
    if method == PERMISSIONS_APPROVAL_REQUEST_METHOD:
        permissions = response.get("permissions")
        return "accept" if isinstance(permissions, dict) and bool(permissions) else "decline"
    return response.get("decision")


def validate_host_response(method: str, body: dict[str, Any]) -> dict[str, Any]:
    response = body.get("response") if isinstance(body.get("response"), dict) else body
    if method in DECISION_APPROVAL_REQUEST_METHODS:
        return _validate_decision_response(response, DECISION_APPROVAL_VALUES, "approval decision")
    if method in LEGACY_APPROVAL_REQUEST_METHODS:
        return _validate_decision_response(response, LEGACY_APPROVAL_VALUES, "legacy approval decision")
    if method == PERMISSIONS_APPROVAL_REQUEST_METHOD:
        return _validate_permissions_response(response)
    if method == USER_INPUT_REQUEST_METHOD:
        return _validate_user_input_response(response)
    if method == MCP_ELICITATION_REQUEST_METHOD:
        return _validate_mcp_elicitation_response(response)
    raise InteractionResponseError(f"Unsupported interaction method: {method}")


def _validate_decision_response(response: dict[str, Any], allowed: set[str], label: str) -> dict[str, Any]:
    decision = response.get("decision")
    if isinstance(decision, str) and decision in allowed:
        return {"decision": decision}
    if isinstance(decision, dict) and decision:
        return {"decision": decision}
    raise InteractionResponseError(f"response.decision must be a valid {label}.")


def _validate_permissions_response(response: dict[str, Any]) -> dict[str, Any]:
    permissions = response.get("permissions")
    if not isinstance(permissions, dict):
        raise InteractionResponseError("response.permissions must be an object.")
    result: dict[str, Any] = {"permissions": permissions}
    scope = response.get("scope", "turn")
    if scope not in PERMISSION_SCOPES:
        raise InteractionResponseError("response.scope must be turn or session.")
    result["scope"] = scope
    if "strictAutoReview" in response:
        strict = response.get("strictAutoReview")
        if strict is not None and not isinstance(strict, bool):
            raise InteractionResponseError("response.strictAutoReview must be a boolean or null.")
        result["strictAutoReview"] = strict
    return result


def _validate_user_input_response(response: dict[str, Any]) -> dict[str, Any]:
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise InteractionResponseError("response.answers must be an object.")
    for question_id, answer in answers.items():
        if not isinstance(question_id, str):
            raise InteractionResponseError("response.answers keys must be strings.")
        if not isinstance(answer, dict) or not isinstance(answer.get("answers"), list):
            raise InteractionResponseError("each user-input answer must be an object with an answers array.")
        if not all(isinstance(value, str) for value in answer["answers"]):
            raise InteractionResponseError("user-input answer arrays must contain strings.")
    return {"answers": answers}


def _validate_mcp_elicitation_response(response: dict[str, Any]) -> dict[str, Any]:
    action = response.get("action")
    if action not in MCP_ELICITATION_ACTIONS:
        raise InteractionResponseError("response.action must be accept, decline, or cancel.")
    result = {"action": action, "content": response.get("content"), "_meta": response.get("_meta")}
    if action != "accept":
        result["content"] = None
    return result
