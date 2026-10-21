from __future__ import annotations

from http import HTTPStatus
import time
from typing import Any
from urllib.parse import unquote

from .app_server import AppServerError
from .openai_auth import OpenAICompatAuthError, OpenAICompatBinding
from .openai_protocol import (
    TOOL_CALL_EVENT,
    OpenAIProtocolError,
    ParsedOpenAIRequest,
    authentication_error,
    chat_completion_object,
    chat_id_from_turn,
    chat_tool_call,
    chat_usage,
    compatibility_metadata,
    invalid,
    is_terminal,
    not_found,
    output_text,
    parse_chat_request,
    parse_responses_request,
    public_output_items,
    raw_output_items,
    response_id_from_turn,
    response_function_call,
    response_object,
    response_usage,
    server_error,
    tool_calls,
    turn_failure_error,
    turn_id_from_response,
    validate_tool_history,
)
from .scheduler_errors import ActiveTurnError, ConflictError, NotFoundError
from .util import json_dumps, json_log


_TERMINAL_RESPONSE_EVENTS = {
    "completed": "response.completed",
    "failed": "response.failed",
    "cancelled": "response.cancelled",
    "incomplete": "response.incomplete",
}


def is_openai_compat_path(path: str) -> bool:
    return (
        path == "/v1/models"
        or path.startswith("/v1/models/")
        or path == "/v1/responses"
        or path.startswith("/v1/responses/")
        or path == "/v1/chat/completions"
    )


def handle_openai_route(
    handler: Any,
    method: str,
    path: str,
    query: dict[str, list[str]],
) -> bool:
    if not is_openai_compat_path(path):
        return False
    try:
        try:
            binding = handler.broker.openai_auth.resolve_authorization(handler.headers.get("Authorization"))
        except OpenAICompatAuthError as exc:
            raise authentication_error() from exc
        if method == "GET" and path == "/v1/models":
            handler._json(_model_list(handler.broker, binding))
            return True
        if method == "GET" and path.startswith("/v1/models/"):
            model_id = unquote(path[len("/v1/models/") :])
            handler._json(_model_get(handler.broker, binding, model_id))
            return True
        if method == "POST" and path == "/v1/responses":
            parsed = parse_responses_request(_read_body(handler))
            turn = _start_request(
                handler.broker,
                binding,
                parsed,
                danger_full_access_authorized=handler._danger_full_access_authorized(),
            )
            if parsed.stream:
                _stream_response(handler, binding, turn)
            else:
                turn = _wait_for_terminal(handler.broker, binding, str(turn["threadId"]), str(turn["turnId"]))
                handler._json(response_object(turn, _turn_events(handler.broker, binding, turn)))
            return True
        if path.startswith("/v1/responses/"):
            return _handle_response_resource(handler, method, path, query, binding)
        if method == "POST" and path == "/v1/chat/completions":
            parsed = parse_chat_request(_read_body(handler))
            turn = _start_request(
                handler.broker,
                binding,
                parsed,
                danger_full_access_authorized=handler._danger_full_access_authorized(),
            )
            if parsed.stream:
                _stream_chat(handler, binding, turn, include_usage=parsed.include_usage)
            else:
                turn = _wait_for_terminal(handler.broker, binding, str(turn["threadId"]), str(turn["turnId"]))
                handler._json(chat_completion_object(turn, _turn_events(handler.broker, binding, turn)))
            return True
        raise not_found("The requested resource was not found.")
    except OpenAIProtocolError as exc:
        handler._json(exc.payload(), exc.status)
        return True
    except (ActiveTurnError, ConflictError, NotFoundError, ValueError) as exc:
        error = OpenAIProtocolError(str(exc), status=HTTPStatus.BAD_REQUEST, code="invalid_request")
        handler._json(error.payload(), error.status)
        return True
    except AppServerError as exc:
        json_log(
            handler.broker.config.json_logs,
            "openai_compat.app_server_error",
            sanitizer=handler.broker.sanitizer,
            message=str(exc),
        )
        error = OpenAIProtocolError(
            "Codex is temporarily unavailable.",
            status=HTTPStatus.BAD_GATEWAY,
            error_type="server_error",
            code="app_server_error",
        )
        handler._json(error.payload(), error.status)
        return True
    except Exception as exc:  # noqa: BLE001 - compatibility boundary must not leak internals.
        json_log(
            handler.broker.config.json_logs,
            "openai_compat.error",
            sanitizer=handler.broker.sanitizer,
            message=str(exc),
        )
        error = server_error("The server encountered an error while processing the request.")
        handler._json(error.payload(), error.status)
        return True


def _handle_response_resource(
    handler: Any,
    method: str,
    path: str,
    query: dict[str, list[str]],
    binding: OpenAICompatBinding,
) -> bool:
    tail = path[len("/v1/responses/") :]
    parts = [unquote(part) for part in tail.split("/") if part]
    if not parts:
        raise not_found("No compatible response was found.")
    response_id = parts[0]
    turn = _find_response(handler.broker, binding, response_id)
    if method == "GET" and len(parts) == 1:
        handler._json(response_object(turn, _turn_events(handler.broker, binding, turn)))
        return True
    if method == "GET" and parts[1:] == ["input_items"]:
        _ = query
        compat = compatibility_metadata(turn, "responses")
        items = compat.get("canonicalInput")
        data = items if isinstance(items, list) else []
        handler._json(
            {
                "object": "list",
                "data": data,
                "first_id": data[0].get("id") if data and isinstance(data[0], dict) else None,
                "last_id": data[-1].get("id") if data and isinstance(data[-1], dict) else None,
                "has_more": False,
            }
        )
        return True
    if method == "POST" and parts[1:] == ["cancel"]:
        if is_terminal(turn):
            handler._json(response_object(turn, _turn_events(handler.broker, binding, turn)))
            return True
        deadline = time.monotonic() + 2
        while True:
            try:
                handler.broker.scheduler.interrupt_turn(
                    binding.owner_id,
                    str(turn["thread_id"]),
                    str(turn["turn_id"]),
                )
                break
            except ActiveTurnError as exc:
                turn = _find_response(handler.broker, binding, response_id)
                if is_terminal(turn):
                    break
                if time.monotonic() >= deadline:
                    raise OpenAIProtocolError(
                        "The response is not currently cancellable.",
                        status=HTTPStatus.CONFLICT,
                        code="response_not_cancellable",
                    ) from exc
                handler.broker.state.wait_for_events(0.02)
        turn = _find_response(handler.broker, binding, response_id)
        handler._json(response_object(turn, _turn_events(handler.broker, binding, turn)))
        return True
    raise not_found("The requested response resource was not found.")


def _start_request(
    services: Any,
    binding: OpenAICompatBinding,
    parsed: ParsedOpenAIRequest,
    *,
    danger_full_access_authorized: bool = False,
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    if parsed.previous_response_id:
        history.extend(_previous_history(services, binding, parsed.previous_response_id))
    history.extend(dict(item) for item in parsed.request_history)
    current_items = [dict(parsed.canonical_input[-1])] if parsed.canonical_input[-1].get("type") == "message" else []
    if parsed.protocol == "responses":
        validate_tool_history([*history, *current_items], "input")
    uses_tools = bool(parsed.tools) or any(item.get("type") == "function_call" for item in history)
    requires_images = any(
        part.get("type") == "input_image"
        for item in [*history, *parsed.canonical_input]
        for part in (item.get("content") or [])
        if isinstance(part, dict)
    )
    resolved_model = _resolve_model(
        services, binding, parsed.requested_model, requires_images=requires_images
    )
    thread_body: dict[str, Any] = {
        "profile": binding.profile,
        "configProfile": binding.config_profile,
        "hostApp": binding.host_app,
    }
    if binding.auth_principal_id:
        thread_body["authPrincipalId"] = binding.auth_principal_id
    if binding.bundle_id:
        thread_body["bundleId"] = binding.bundle_id
    if binding.cwd:
        thread_body["cwd"] = binding.cwd
    thread = services.scheduler.create_thread(binding.owner_id, thread_body)
    codex_options: dict[str, Any] = {"model": resolved_model}
    if parsed.instructions:
        codex_options["developerInstructions"] = parsed.instructions
    if parsed.base_instructions:
        codex_options["baseInstructions"] = parsed.base_instructions
    if parsed.reasoning_effort:
        codex_options["effort"] = parsed.reasoning_effort
    if parsed.reasoning_summary:
        codex_options["summary"] = parsed.reasoning_summary
    if parsed.service_tier:
        codex_options["serviceTier"] = parsed.service_tier
    if parsed.output_schema:
        codex_options["outputSchema"] = parsed.output_schema
    body: dict[str, Any] = {
        "input": parsed.turn_input(),
        "mode": "reject",
        "profile": binding.profile,
        "configProfile": binding.config_profile,
        "hostApp": binding.host_app,
        "codexOptions": codex_options,
    }
    if binding.auth_principal_id:
        body["authPrincipalId"] = binding.auth_principal_id
    if binding.bundle_id:
        body["bundleId"] = binding.bundle_id
    if binding.cwd:
        body["cwd"] = binding.cwd
    metadata = {
        "version": 1,
        "protocol": parsed.protocol,
        "canonicalInput": [dict(item) for item in parsed.canonical_input],
        "previousResponseId": parsed.previous_response_id,
        "requestedModel": parsed.requested_model,
        "resolvedModel": resolved_model,
        "instructions": parsed.instructions,
        "baseInstructions": parsed.base_instructions,
        "reasoning": {
            "effort": parsed.reasoning_effort,
            "summary": parsed.reasoning_summary,
        },
        "serviceTier": parsed.service_tier,
        "text": parsed.text_config,
        "metadata": parsed.metadata,
        "tools": [dict(tool) for tool in parsed.tools],
        "toolChoice": parsed.tool_choice,
    }
    return services.scheduler.start_openai_turn(
        binding.owner_id,
        str(thread["threadId"]),
        body,
        metadata=metadata,
        history_items=history,
        dynamic_tools=parsed.dynamic_tools(),
        # Without a reviewed bundle, a caller that brings its own functions gets
        # a model that can only call them, not one that acts on the broker host.
        isolate_builtin_tools=uses_tools and not binding.bundle_id,
        danger_full_access_authorized=danger_full_access_authorized,
    )


def _previous_history(services: Any, binding: OpenAICompatBinding, response_id: str) -> list[dict[str, Any]]:
    owner_hash = services.auth.hash_owner(binding.owner_id)
    segments: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    current: str | None = response_id
    for _ in range(100):
        if current is None:
            break
        if current in seen:
            raise invalid("previous_response_id chain contains a cycle.", "previous_response_id")
        seen.add(current)
        turn_id = turn_id_from_response(current)
        turn = services.state.find_turn_by_turn_id(owner_hash, turn_id)
        if not turn:
            raise not_found("The previous response was not found.")
        compat = compatibility_metadata(turn, "responses")
        events = services.state.list_events(
            owner_hash,
            str(turn["thread_id"]),
            turn_id=str(turn["turn_id"]),
            limit=services.config.max_events_per_turn,
        )
        if str(turn["status"]) != "completed":
            raise invalid("previous_response_id must refer to a completed response.", "previous_response_id")
        canonical = compat.get("canonicalInput")
        if not isinstance(canonical, list):
            raise server_error("The previous response does not contain reconstructable input.")
        segment = [dict(item) for item in canonical if isinstance(item, dict)]
        segment.extend(_history_output_items(events))
        segments.append(segment)
        previous = compat.get("previousResponseId")
        current = previous if isinstance(previous, str) else None
    else:
        raise invalid("previous_response_id chain is too deep.", "previous_response_id")
    history: list[dict[str, Any]] = []
    for segment in reversed(segments):
        history.extend(segment)
    return history


def _history_output_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replayable model output: assistant messages, then returned tool calls.

    Reasoning items are left out: they belong to the exact items the model produced,
    which a returned tool call can replace (for example a code-mode script).
    """
    items = [
        item
        for item in raw_output_items(events)
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    items.extend(
        {"type": "function_call", "call_id": call["callId"], "name": call["name"], "arguments": call["arguments"]}
        for call in tool_calls(events)
    )
    return items


def _find_response(services: Any, binding: OpenAICompatBinding, response_id: str) -> dict[str, Any]:
    owner_hash = services.auth.hash_owner(binding.owner_id)
    turn = services.state.find_turn_by_turn_id(owner_hash, turn_id_from_response(response_id))
    if not turn:
        raise not_found("No compatible response was found.")
    compatibility_metadata(turn, "responses")
    return turn


def _wait_for_terminal(
    services: Any,
    binding: OpenAICompatBinding,
    thread_id: str,
    turn_id: str,
) -> dict[str, Any]:
    owner_hash = services.auth.hash_owner(binding.owner_id)
    while True:
        turn = services.state.get_turn(owner_hash, thread_id, turn_id)
        if not turn:
            raise not_found("No compatible response was found.")
        if is_terminal(turn):
            return turn
        services.state.wait_for_events(0.25)


def _turn_events(services: Any, binding: OpenAICompatBinding, turn: dict[str, Any]) -> list[dict[str, Any]]:
    return services.state.list_events(
        services.auth.hash_owner(binding.owner_id),
        str(turn["thread_id"]),
        turn_id=str(turn["turn_id"]),
        limit=services.config.max_events_per_turn,
    )


def _stream_response(handler: Any, binding: OpenAICompatBinding, public_turn: dict[str, Any]) -> None:
    owner_hash = handler.broker.auth.hash_owner(binding.owner_id)
    thread_id = str(public_turn["threadId"])
    turn_id = str(public_turn["turnId"])
    turn = handler.broker.state.get_turn(owner_hash, thread_id, turn_id)
    if not turn:
        raise not_found("No compatible response was found.")
    _start_sse(handler)
    sequence = 0

    def emit(event_type: str, payload: dict[str, Any]) -> bool:
        nonlocal sequence
        sequence += 1
        payload = {"type": event_type, "sequence_number": sequence, **payload}
        return _write_sse(handler, event_type, payload)

    initial = response_object({**turn, "status": "running", "completed_at": None}, [])
    if not emit("response.created", {"response": initial}):
        return
    if not emit("response.in_progress", {"response": initial}):
        return
    after = 0
    started_items: set[str] = set()
    while True:
        events = handler.broker.state.list_events(owner_hash, thread_id, after=after, turn_id=turn_id, limit=100)
        for event in events:
            after = int(event["id"])
            if event["event_type"] == "message.delta":
                payload = event.get("payload") or {}
                item_id = str(payload.get("itemId") or "msg_1")
                if item_id not in started_items:
                    started_items.add(item_id)
                    if not emit(
                        "response.output_item.added",
                        {
                            "output_index": 0,
                            "item": {
                                "id": item_id,
                                "type": "message",
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [],
                            },
                        },
                    ):
                        return
                    if not emit(
                        "response.content_part.added",
                        {
                            "item_id": item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    ):
                        return
                if not emit(
                    "response.output_text.delta",
                    {
                        "item_id": item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": str(payload.get("delta") or ""),
                        "logprobs": [],
                    },
                ):
                    return
            elif event["event_type"] == "compat.response.output_item":
                items = public_output_items([event])
                for output_index, item in enumerate(items):
                    item_id = str(item["id"])
                    if item_id not in started_items:
                        started_items.add(item_id)
                        if not emit(
                            "response.output_item.added",
                            {
                                "output_index": output_index,
                                "item": {
                                    "id": item_id,
                                    "type": "message",
                                    "status": "in_progress",
                                    "role": "assistant",
                                    "content": [],
                                },
                            },
                        ):
                            return
                        if not emit(
                            "response.content_part.added",
                            {
                                "item_id": item_id,
                                "output_index": output_index,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []},
                            },
                        ):
                            return
                    text = "".join(str(part.get("text") or "") for part in item["content"])
                    if not emit(
                        "response.output_text.done",
                        {
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "text": text,
                            "logprobs": [],
                        },
                    ):
                        return
                    part = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
                    if not emit(
                        "response.content_part.done",
                        {
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "part": part,
                        },
                    ):
                        return
                    if not emit("response.output_item.done", {"output_index": output_index, "item": item}):
                        return
            elif event["event_type"] == TOOL_CALL_EVENT:
                for call in tool_calls([event]):
                    item = response_function_call(call)
                    output_index = len(started_items)
                    started_items.add(str(item["id"]))
                    if not emit(
                        "response.output_item.added",
                        {"output_index": output_index, "item": {**item, "status": "in_progress", "arguments": ""}},
                    ):
                        return
                    for event_type, payload in (
                        ("response.function_call_arguments.delta", {"delta": item["arguments"]}),
                        ("response.function_call_arguments.done", {"arguments": item["arguments"]}),
                    ):
                        if not emit(event_type, {"item_id": item["id"], "output_index": output_index, **payload}):
                            return
                    if not emit("response.output_item.done", {"output_index": output_index, "item": item}):
                        return
        turn = handler.broker.state.get_turn(owner_hash, thread_id, turn_id)
        if not turn:
            return
        if is_terminal(turn):
            try:
                final = response_object(turn, _turn_events(handler.broker, binding, turn))
                event_type = _TERMINAL_RESPONSE_EVENTS.get(str(final["status"]), "response.failed")
                emit(event_type, {"response": final})
            except OpenAIProtocolError as exc:
                emit("error", exc.payload()["error"])
            _finish_sse(handler)
            return
        handler.broker.state.wait_for_events(0.25)


def _stream_chat(
    handler: Any,
    binding: OpenAICompatBinding,
    public_turn: dict[str, Any],
    *,
    include_usage: bool,
) -> None:
    owner_hash = handler.broker.auth.hash_owner(binding.owner_id)
    thread_id = str(public_turn["threadId"])
    turn_id = str(public_turn["turnId"])
    turn = handler.broker.state.get_turn(owner_hash, thread_id, turn_id)
    if not turn:
        raise not_found("No compatible completion was found.")
    compat = compatibility_metadata(turn, "chat.completions")
    completion_id = chat_id_from_turn(turn_id)
    created = int(time.time())
    model = compat.get("requestedModel") or compat.get("resolvedModel")
    _start_sse(handler)

    def chunk(delta: dict[str, Any], finish_reason: str | None = None, usage: dict[str, Any] | None = None) -> bool:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "logprobs": None,
                    "finish_reason": finish_reason,
                }
            ],
            "service_tier": compat.get("serviceTier") or "default",
            "system_fingerprint": None,
        }
        if usage is not None:
            payload["usage"] = usage
        return _write_data(handler, payload)

    if not chunk({"role": "assistant", "content": ""}):
        return
    after = 0
    tool_call_count = 0
    while True:
        events = handler.broker.state.list_events(owner_hash, thread_id, after=after, turn_id=turn_id, limit=100)
        for event in events:
            after = int(event["id"])
            if event["event_type"] == "message.delta":
                payload = event.get("payload") or {}
                if not chunk({"content": str(payload.get("delta") or "")}):
                    return
            elif event["event_type"] == TOOL_CALL_EVENT:
                for call in tool_calls([event]):
                    complete = chat_tool_call(call)
                    opening = {**complete, "function": {"name": call["name"], "arguments": ""}}
                    arguments = {"function": {"arguments": call["arguments"]}}
                    for part in (opening, arguments):
                        if not chunk({"tool_calls": [{"index": tool_call_count, **part}]}):
                            return
                    tool_call_count += 1
        turn = handler.broker.state.get_turn(owner_hash, thread_id, turn_id)
        if not turn:
            return
        if is_terminal(turn):
            if str(turn["status"]) == "completed":
                if not chunk({}, "tool_calls" if tool_call_count else "stop"):
                    return
                if include_usage:
                    try:
                        events = _turn_events(handler.broker, binding, turn)
                        usage = response_usage(events, required=not tool_call_count)
                    except OpenAIProtocolError as exc:
                        _write_data(handler, exc.payload())
                        _finish_sse(handler)
                        return
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": chat_usage(usage) if usage is not None else None,
                    }
                    if not _write_data(handler, payload):
                        return
                _write_literal_data(handler, "[DONE]")
            else:
                error = turn_failure_error(turn)
                _write_data(handler, error.payload())
            _finish_sse(handler)
            return
        handler.broker.state.wait_for_events(0.25)


def _start_sse(handler: Any) -> None:
    handler._metric_status = HTTPStatus.OK
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    handler.close_connection = True


def _write_sse(handler: Any, event_type: str, payload: dict[str, Any]) -> bool:
    return handler._write_raw(f"event: {event_type}\ndata: {json_dumps(payload)}\n\n")


def _write_data(handler: Any, payload: dict[str, Any]) -> bool:
    return handler._write_raw(f"data: {json_dumps(payload)}\n\n")


def _write_literal_data(handler: Any, payload: str) -> bool:
    return handler._write_raw(f"data: {payload}\n\n")


def _finish_sse(handler: Any) -> None:
    try:
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _read_body(handler: Any) -> dict[str, Any]:
    try:
        return handler._read_json()
    except (ValueError, TypeError) as exc:
        raise invalid(str(exc)) from exc


def _app_server_models(services: Any, binding: OpenAICompatBinding) -> list[dict[str, Any]]:
    scope = services.auth.resolve_scope(binding.owner_id, binding.auth_principal_id)
    profile = services.auth.profile_key(binding.profile)
    with services.auth.profile_guard(scope.auth_principal_hash, profile):
        client = services.pool.checkout(
            auth_principal_hash=scope.auth_principal_hash,
            profile=profile,
            codex_home=services.auth.profile_home(scope.auth_principal_hash, profile),
            runtime_home=services.auth.runtime_home(scope.auth_principal_hash, profile),
            config_profile=binding.config_profile,
            mcp_servers=(),
            tenant_scope_hash=scope.owner_hash,
            auth_fingerprint=services.auth.auth_fingerprint(scope.auth_principal_hash, profile),
        )
        try:
            result = client.request("model/list", {"limit": 500, "includeHidden": False})
        finally:
            services.pool.release(client)
    models = result.get("data")
    if not isinstance(models, list):
        raise AppServerError("App Server model/list response is missing its model list.")
    return [model for model in models if isinstance(model, dict)]


def _visible_models(services: Any, binding: OpenAICompatBinding) -> dict[str, str]:
    available: dict[str, str] = {}
    for model in _app_server_models(services, binding):
        model_id = model.get("model") or model.get("id")
        if isinstance(model_id, str) and model_id:
            available[model_id] = model_id
    for alias, target in binding.model_aliases.items():
        if target in available:
            available[alias] = target
    return available


def _resolve_model(
    services: Any, binding: OpenAICompatBinding, requested: str, *, requires_images: bool = False
) -> str:
    catalog = {
        item.get("model") or item.get("id"): item
        for item in _app_server_models(services, binding)
        if isinstance(item.get("model") or item.get("id"), str)
    }
    target = binding.model_aliases.get(requested)
    resolved = target if target in catalog else requested
    model = catalog.get(resolved)
    if model is None:
        raise OpenAIProtocolError(
            f"The model {requested!r} does not exist or is not available to this compatibility key.",
            status=HTTPStatus.NOT_FOUND,
            param="model",
            code="model_not_found",
        )
    if requires_images and "image" not in (model.get("inputModalities") or []):
        raise invalid("The selected model does not advertise image input support.", "model")
    return resolved


def _model_list(services: Any, binding: OpenAICompatBinding) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 0, "owned_by": "codex-broker"}
            for model_id in sorted(_visible_models(services, binding))
        ],
    }


def _model_get(services: Any, binding: OpenAICompatBinding, model_id: str) -> dict[str, Any]:
    _resolve_model(services, binding, model_id)
    return {"id": model_id, "object": "model", "created": 0, "owned_by": "codex-broker"}


def openapi_paths(ref: Any, json_response: Any, request_body: Any) -> dict[str, Any]:
    security = [{"openaiCompatBearer": []}]
    danger_full_access_header = {
        "name": "X-Codex-Broker-Danger-Full-Access-Key",
        "in": "header",
        "required": False,
        "schema": {"type": "string", "format": "password"},
        "description": (
            "Separate deployment credential required only when the resolved broker configuration "
            "selects danger-full-access. The OpenAI compatibility key is not sufficient."
        ),
    }

    def error_response(description: str) -> dict[str, Any]:
        return {
            "description": description,
            "content": {"application/json": {"schema": ref("OpenAIError")}},
        }

    return {
        "/v1/models": {
            "get": {
                "security": security,
                "responses": {
                    "200": json_response(ref("OpenAIModelList")),
                    "401": error_response("Invalid compatibility key"),
                },
            }
        },
        "/v1/models/{model}": {
            "get": {
                "security": security,
                "parameters": [{"name": "model", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": json_response(ref("OpenAIModel")),
                    "401": error_response("Invalid compatibility key"),
                    "404": error_response("Model not found"),
                },
            }
        },
        "/v1/responses": {
            "post": {
                "security": security,
                "parameters": [danger_full_access_header],
                "requestBody": request_body(ref("OpenAIResponseCreateRequest")),
                "responses": {
                    "200": {
                        "description": "Response or typed event stream",
                        "content": {
                            "application/json": {"schema": ref("OpenAIResponse")},
                            "text/event-stream": {"schema": {"type": "string"}},
                        },
                    },
                    "400": error_response("Invalid or unsupported request"),
                    "401": error_response("Invalid compatibility key"),
                    "404": error_response("Model or previous response not found"),
                    "500": error_response("Compatible output could not be reconstructed"),
                    "502": error_response("Codex App Server unavailable"),
                },
            }
        },
        "/v1/responses/{responseId}": {
            "get": {
                "security": security,
                "parameters": [_response_id_parameter()],
                "responses": {
                    "200": json_response(ref("OpenAIResponse")),
                    "401": error_response("Invalid compatibility key"),
                    "404": error_response("Response not found"),
                    "500": error_response("Compatible output could not be reconstructed"),
                },
            }
        },
        "/v1/responses/{responseId}/input_items": {
            "get": {
                "security": security,
                "parameters": [_response_id_parameter()],
                "responses": {
                    "200": json_response(ref("OpenAIItemList")),
                    "401": error_response("Invalid compatibility key"),
                    "404": error_response("Response not found"),
                },
            }
        },
        "/v1/responses/{responseId}/cancel": {
            "post": {
                "security": security,
                "parameters": [_response_id_parameter()],
                "responses": {
                    "200": json_response(ref("OpenAIResponse")),
                    "401": error_response("Invalid compatibility key"),
                    "404": error_response("Response not found"),
                    "409": error_response("Response is not cancellable"),
                    "500": error_response("Compatible output could not be reconstructed"),
                },
            }
        },
        "/v1/chat/completions": {
            "post": {
                "security": security,
                "parameters": [danger_full_access_header],
                "requestBody": request_body(ref("OpenAIChatCreateRequest")),
                "responses": {
                    "200": {
                        "description": "Chat completion or event stream",
                        "content": {
                            "application/json": {"schema": ref("OpenAIChatCompletion")},
                            "text/event-stream": {"schema": {"type": "string"}},
                        },
                    },
                    "400": error_response("Invalid or unsupported request"),
                    "401": error_response("Invalid compatibility key"),
                    "404": error_response("Model not found"),
                    "429": error_response("Upstream rate limit exceeded"),
                    "500": error_response("Completion failed"),
                    "502": error_response("Codex App Server unavailable"),
                    "503": error_response("Upstream model at capacity or overloaded"),
                },
            }
        },
    }


def openapi_schemas() -> dict[str, Any]:
    open_object = {"type": "object", "additionalProperties": True}
    image_detail = {
        "type": "string",
        "enum": ["auto", "low", "high", "original"],
        "description": (
            "Low is executed as high because the pinned Codex runtime can omit low-detail images. "
            "Stored input retains the requested value; OpenAI low-detail token costs are not preserved."
        ),
    }
    tool_choice = {
        "enum": ["auto", "none"],
        "description": "Codex cannot force a tool call; required and named functions are rejected.",
    }
    parallel_tool_calls = {
        "const": True,
        "description": "The model may return several tool calls at once; false is rejected.",
    }
    function_parameters = {
        "type": "object",
        "description": "JSON Schema object for the arguments. Defaults to an empty object schema.",
        "additionalProperties": True,
    }
    function_strict = {"const": False, "description": "Strict schemas are not enforced; true is rejected."}
    tool_name = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}
    ignored_cap = {
        "description": (
            "Accepted for client compatibility and discarded, regardless of its value. "
            "Codex Broker does not enforce an output-token limit."
        )
    }
    return {
        "OpenAIImageDataURL": {
            "type": "string",
            "pattern": "^data:image/(png|jpeg|webp|gif);base64,",
            "description": (
                "Inline base64 PNG, JPEG, WebP, or GIF. Remote URLs, local paths, and file IDs "
                "are unsupported. At most 10 images and 20 MiB of decoded image data per request."
            ),
        },
        "OpenAIResponseContentPart": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["type", "text"],
                    "properties": {
                        "type": {"enum": ["input_text", "output_text"]},
                        "text": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "required": ["type", "image_url"],
                    "additionalProperties": False,
                    "properties": {
                        "type": {"const": "input_image"},
                        "image_url": {"$ref": "#/components/schemas/OpenAIImageDataURL"},
                        "detail": image_detail,
                    },
                },
            ],
        },
        "OpenAIResponseFunctionCall": {
            "type": "object",
            "required": ["type", "call_id", "name", "arguments"],
            "properties": {
                "type": {"const": "function_call"},
                "call_id": {"type": "string"},
                "name": tool_name,
                "arguments": {"type": "string"},
                "id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
        "OpenAIResponseFunctionCallOutput": {
            "type": "object",
            "required": ["type", "call_id", "output"],
            "properties": {
                "type": {"const": "function_call_output"},
                "call_id": {"type": "string"},
                "output": {"type": "string", "description": "A string or an array of input_text parts."},
                "id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
        "OpenAIResponseInputMessage": {
            "type": "object",
            "required": ["role", "content"],
            "description": (
                "User messages accept ordered text/image parts or images alone. Other roles "
                "accept text only; assistant parts use output_text, all others use input_text. "
                "The input must end with a user message or a function_call_output item."
            ),
            "properties": {
                "type": {"const": "message"},
                "role": {"enum": ["system", "developer", "user", "assistant"]},
                "content": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array", "minItems": 1,
                            "items": {"$ref": "#/components/schemas/OpenAIResponseContentPart"},
                        },
                    ]
                },
            },
        },
        "OpenAIChatMessage": {
            "type": "object",
            "required": ["role"],
            "description": (
                "Only user messages accept image parts. Text and images retain their order. "
                "Assistant messages may carry tool_calls with null content; tool messages answer "
                "an earlier call by tool_call_id. The last message must be a user or tool message."
            ),
            "properties": {
                "role": {"enum": ["system", "developer", "user", "assistant", "tool"]},
                "name": {"type": "string"},
                "tool_call_id": {"type": "string"},
                "tool_calls": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["id", "function"],
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"const": "function"},
                            "function": {
                                "type": "object",
                                "required": ["name", "arguments"],
                                "properties": {"name": tool_name, "arguments": {"type": "string"}},
                            },
                        },
                    },
                },
                "content": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array", "minItems": 1,
                            "items": {
                                "oneOf": [
                                    {
                                        "type": "object",
                                        "required": ["type", "text"],
                                        "properties": {
                                            "type": {"const": "text"}, "text": {"type": "string"},
                                        },
                                    },
                                    {
                                        "type": "object",
                                        "required": ["type", "image_url"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "type": {"const": "image_url"},
                                            "image_url": {
                                                "type": "object",
                                                "required": ["url"],
                                                "additionalProperties": False,
                                                "properties": {
                                                    "url": {"$ref": "#/components/schemas/OpenAIImageDataURL"},
                                                    "detail": image_detail,
                                                },
                                            },
                                        },
                                    },
                                ]
                            },
                        },
                    ]
                },
            },
        },
        "OpenAIError": {
            "type": "object",
            "required": ["error"],
            "properties": {"error": open_object},
        },
        "OpenAIModel": {
            "type": "object",
            "required": ["id", "object", "created", "owned_by"],
            "properties": {
                "id": {"type": "string"},
                "object": {"const": "model"},
                "created": {"type": "integer"},
                "owned_by": {"type": "string"},
            },
        },
        "OpenAIModelList": {
            "type": "object",
            "required": ["object", "data"],
            "properties": {
                "object": {"const": "list"},
                "data": {"type": "array", "items": {"$ref": "#/components/schemas/OpenAIModel"}},
            },
        },
        "OpenAIResponseCreateRequest": {
            "type": "object",
            "description": "Accepts text and inline images with an image-capable model. JSON body limit: 32 MiB.",
            "required": ["model", "input"],
            "additionalProperties": False,
            "properties": {
                "model": {"type": "string"},
                "input": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array", "minItems": 1,
                            "items": {
                                "oneOf": [
                                    {"$ref": "#/components/schemas/OpenAIResponseInputMessage"},
                                    {"$ref": "#/components/schemas/OpenAIResponseFunctionCall"},
                                    {"$ref": "#/components/schemas/OpenAIResponseFunctionCallOutput"},
                                ]
                            },
                        },
                    ]
                },
                "instructions": {"type": "string"},
                "tools": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "object",
                        "required": ["type", "name"],
                        "additionalProperties": False,
                        "properties": {
                            "type": {"const": "function"},
                            "name": tool_name,
                            "description": {"type": "string"},
                            "parameters": function_parameters,
                            "strict": function_strict,
                        },
                    },
                },
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "stream": {"type": "boolean"},
                "previous_response_id": {"type": "string"},
                "reasoning": open_object,
                "service_tier": {"type": "string"},
                "text": open_object,
                "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
                "store": {"const": True},
            },
        },
        "OpenAIResponse": open_object,
        "OpenAIItemList": open_object,
        "OpenAIChatCreateRequest": {
            "type": "object",
            "description": "Accepts text and inline images with an image-capable model. JSON body limit: 32 MiB.",
            "required": ["model", "messages"],
            "additionalProperties": False,
            "properties": {
                "model": {"type": "string"},
                "messages": {
                    "type": "array", "minItems": 1,
                    "items": {"$ref": "#/components/schemas/OpenAIChatMessage"},
                },
                "stream": {"type": "boolean"},
                "stream_options": open_object,
                "reasoning_effort": {"type": "string"},
                "service_tier": {"type": "string"},
                "response_format": open_object,
                "tools": {
                    "type": "array",
                    "maxItems": 128,
                    "items": {
                        "type": "object",
                        "required": ["type", "function"],
                        "additionalProperties": False,
                        "properties": {
                            "type": {"const": "function"},
                            "function": {
                                "type": "object",
                                "required": ["name"],
                                "additionalProperties": False,
                                "properties": {
                                    "name": tool_name,
                                    "description": {"type": "string"},
                                    "parameters": function_parameters,
                                    "strict": function_strict,
                                },
                            },
                        },
                    },
                },
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "max_tokens": ignored_cap,
                "max_completion_tokens": ignored_cap,
                "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
                "store": {"const": True},
            },
        },
        "OpenAIChatCompletion": open_object,
    }


def _response_id_parameter() -> dict[str, Any]:
    return {"name": "responseId", "in": "path", "required": True, "schema": {"type": "string"}}
