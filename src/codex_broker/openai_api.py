from __future__ import annotations

from http import HTTPStatus
import time
from typing import Any
from urllib.parse import unquote

from .app_server import AppServerError
from .openai_auth import OpenAICompatAuthError, OpenAICompatBinding
from .openai_protocol import (
    OpenAIProtocolError,
    ParsedOpenAIRequest,
    authentication_error,
    chat_completion_object,
    chat_id_from_turn,
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
    response_object,
    response_usage,
    server_error,
    turn_id_from_response,
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
    resolved_model = _resolve_model(services, binding, parsed.requested_model)
    history: list[dict[str, Any]] = []
    if parsed.previous_response_id:
        history.extend(_previous_history(services, binding, parsed.previous_response_id))
    history.extend(dict(item) for item in parsed.request_history)
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
    }
    return services.scheduler.start_openai_turn(
        binding.owner_id,
        str(thread["threadId"]),
        body,
        metadata=metadata,
        history_items=history,
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
        segment.extend(raw_output_items(events))
        segments.append(segment)
        previous = compat.get("previousResponseId")
        current = previous if isinstance(previous, str) else None
    else:
        raise invalid("previous_response_id chain is too deep.", "previous_response_id")
    history: list[dict[str, Any]] = []
    for segment in reversed(segments):
        history.extend(segment)
    return history


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
    while True:
        events = handler.broker.state.list_events(owner_hash, thread_id, after=after, turn_id=turn_id, limit=100)
        for event in events:
            after = int(event["id"])
            if event["event_type"] == "message.delta":
                payload = event.get("payload") or {}
                if not chunk({"content": str(payload.get("delta") or "")}):
                    return
        turn = handler.broker.state.get_turn(owner_hash, thread_id, turn_id)
        if not turn:
            return
        if is_terminal(turn):
            if str(turn["status"]) == "completed":
                if not chunk({}, "stop"):
                    return
                if include_usage:
                    try:
                        usage = response_usage(_turn_events(handler.broker, binding, turn), required=True)
                    except OpenAIProtocolError as exc:
                        _write_data(handler, exc.payload())
                        _finish_sse(handler)
                        return
                    assert usage is not None
                    usage_payload = {
                        "prompt_tokens": usage["input_tokens"],
                        "completion_tokens": usage["output_tokens"],
                        "total_tokens": usage["total_tokens"],
                        "prompt_tokens_details": usage["input_tokens_details"],
                        "completion_tokens_details": usage["output_tokens_details"],
                    }
                    payload = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": usage_payload,
                    }
                    if not _write_data(handler, payload):
                        return
                _write_literal_data(handler, "[DONE]")
            else:
                error = server_error("The completion failed.")
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
        client = services.pool.get(
            auth_principal_hash=scope.auth_principal_hash,
            profile=profile,
            codex_home=services.auth.profile_home(scope.auth_principal_hash, profile),
            runtime_home=services.auth.runtime_home(scope.auth_principal_hash, profile),
            config_profile=binding.config_profile,
            mcp_servers=(),
            tenant_scope_hash=scope.owner_hash,
            auth_fingerprint=services.auth.auth_fingerprint(scope.auth_principal_hash, profile),
        )
        result = client.request("model/list", {"limit": 500, "includeHidden": False})
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


def _resolve_model(services: Any, binding: OpenAICompatBinding, requested: str) -> str:
    visible = _visible_models(services, binding)
    resolved = visible.get(requested)
    if resolved is None:
        raise OpenAIProtocolError(
            f"The model {requested!r} does not exist or is not available to this compatibility key.",
            status=HTTPStatus.NOT_FOUND,
            param="model",
            code="model_not_found",
        )
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
                    "500": error_response("Completion failed"),
                    "502": error_response("Codex App Server unavailable"),
                },
            }
        },
    }


def openapi_schemas() -> dict[str, Any]:
    open_object = {"type": "object", "additionalProperties": True}
    return {
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
            "required": ["model", "input"],
            "additionalProperties": False,
            "properties": {
                "model": {"type": "string"},
                "input": {},
                "instructions": {"type": "string"},
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
            "required": ["model", "messages"],
            "additionalProperties": False,
            "properties": {
                "model": {"type": "string"},
                "messages": {"type": "array", "items": open_object},
                "stream": {"type": "boolean"},
                "stream_options": open_object,
                "reasoning_effort": {"type": "string"},
                "service_tier": {"type": "string"},
                "response_format": open_object,
                "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
                "store": {"const": True},
            },
        },
        "OpenAIChatCompletion": open_object,
    }


def _response_id_parameter() -> dict[str, Any]:
    return {"name": "responseId", "in": "path", "required": True, "schema": {"type": "string"}}
