from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
import re
from typing import Any


_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_TERMINAL_STATUSES = {"completed", "failed", "timed_out", "interrupted", "cancelled"}
_DATA_IMAGE_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+={0,2})$")

# Limits apply to decoded image bytes, rather than the larger base64 request body.
OPENAI_MAX_IMAGES_PER_REQUEST = 10
OPENAI_MAX_IMAGE_BYTES = 20 * 1024 * 1024
OPENAI_MAX_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024
_IMAGE_DETAILS = {"auto", "low", "high", "original"}
_MAX_DATA_IMAGE_URL_CHARS = len("data:image/jpeg;base64,") + 4 * ((OPENAI_MAX_IMAGE_BYTES + 2) // 3)


class OpenAIProtocolError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        error_type: str = "invalid_request_error",
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.param = param
        self.code = code

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "message": str(self),
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


@dataclass(frozen=True)
class ParsedOpenAIRequest:
    protocol: str
    requested_model: str
    canonical_input: tuple[dict[str, Any], ...]
    request_history: tuple[dict[str, Any], ...]
    current_text: str
    instructions: str | None
    base_instructions: str | None
    stream: bool
    previous_response_id: str | None
    metadata: dict[str, str]
    reasoning_effort: str | None
    reasoning_summary: str | None
    service_tier: str | None
    text_config: dict[str, Any]
    output_schema: dict[str, Any] | None
    include_usage: bool = False

    def turn_input(self) -> list[dict[str, Any]]:
        current = self.canonical_input[-1]
        input_items: list[dict[str, Any]] = []
        for part in current["content"]:
            if part["type"] == "input_text":
                input_items.append({"type": "text", "text": part["text"], "text_elements": []})
            elif part["type"] == "input_image":
                item = {"type": "image", "url": part["image_url"]}
                if part.get("detail") is not None:
                    item["detail"] = part["detail"]
                input_items.append(item)
            else:  # ParsedOpenAIRequest only exposes a final user message.
                raise AssertionError(f"Unsupported current input part {part['type']!r}.")
        return input_items


def parse_responses_request(body: dict[str, Any]) -> ParsedOpenAIRequest:
    allowed = {
        "model",
        "input",
        "instructions",
        "stream",
        "previous_response_id",
        "reasoning",
        "service_tier",
        "text",
        "metadata",
        "store",
    }
    _reject_unknown(body, allowed)
    model = _required_string(body, "model")
    if body.get("store", True) is False:
        raise unsupported("store=false is not supported because Codex Broker persists operational turn history.", "store")
    if body.get("store", True) is not True:
        raise invalid("store must be true when provided.", "store")
    stream = _optional_bool(body, "stream", False)
    previous = _optional_string(body, "previous_response_id")
    instructions = _optional_string(body, "instructions")
    metadata = _metadata(body.get("metadata"))
    reasoning = body.get("reasoning") or {}
    if not isinstance(reasoning, dict):
        raise invalid("reasoning must be an object.", "reasoning")
    _reject_unknown(reasoning, {"effort", "summary"}, prefix="reasoning")
    effort = _optional_string(reasoning, "effort")
    summary = _optional_string(reasoning, "summary")
    service_tier = _optional_string(body, "service_tier")
    text_config, output_schema = _parse_responses_text(body.get("text"))
    canonical, history, current = _parse_response_input(body.get("input"))
    return ParsedOpenAIRequest(
        protocol="responses",
        requested_model=model,
        canonical_input=tuple(canonical),
        request_history=tuple(history),
        current_text=current,
        instructions=instructions,
        base_instructions=None,
        stream=stream,
        previous_response_id=previous,
        metadata=metadata,
        reasoning_effort=effort,
        reasoning_summary=summary,
        service_tier=service_tier,
        text_config=text_config,
        output_schema=output_schema,
        include_usage=False,
    )


def parse_chat_request(body: dict[str, Any]) -> ParsedOpenAIRequest:
    allowed = {
        "model",
        "messages",
        "stream",
        "stream_options",
        "reasoning_effort",
        "service_tier",
        "response_format",
        "metadata",
        "store",
        # Client compatibility only: Codex has no output-token cap to enforce.
        # These values are deliberately neither validated nor forwarded.
        "max_tokens",
        "max_completion_tokens",
    }
    _reject_unknown(body, allowed)
    model = _required_string(body, "model")
    if body.get("store", True) is False:
        raise unsupported("store=false is not supported because Codex Broker persists operational turn history.", "store")
    if body.get("store", True) is not True:
        raise invalid("store must be true when provided.", "store")
    stream = _optional_bool(body, "stream", False)
    stream_options = body.get("stream_options")
    include_usage = False
    if stream_options is not None:
        if not isinstance(stream_options, dict):
            raise invalid("stream_options must be an object.", "stream_options")
        _reject_unknown(stream_options, {"include_usage"}, prefix="stream_options")
        if stream_options.get("include_usage") not in {None, True, False}:
            raise invalid("stream_options.include_usage must be a boolean.", "stream_options.include_usage")
        include_usage = bool(stream_options.get("include_usage", False))
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise invalid("messages must be a non-empty array.", "messages")
    base_parts: list[str] = []
    developer_parts: list[str] = []
    conversation: list[dict[str, Any]] = []
    image_counter = _ImageCounter()
    for index, raw in enumerate(messages):
        if not isinstance(raw, dict):
            raise invalid("Every message must be an object.", f"messages.{index}")
        role = raw.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            raise unsupported(f"Message role {role!r} is not supported.", f"messages.{index}.role")
        unsupported_keys = set(raw) - {"role", "content", "name"}
        if unsupported_keys:
            key = sorted(unsupported_keys)[0]
            raise unsupported(f"Message field {key!r} is not supported.", f"messages.{index}.{key}")
        content = _chat_content_parts(raw.get("content"), role, f"messages.{index}.content", image_counter)
        text = _parts_text(content)
        if role == "system":
            base_parts.append(text)
        elif role == "developer":
            developer_parts.append(text)
        else:
            conversation.append(_canonical_message(role, content))
    if not conversation or conversation[-1]["role"] != "user":
        raise unsupported("The final non-instruction Chat message must have role 'user'.", "messages")
    current = _message_text(conversation[-1])
    response_format = body.get("response_format")
    text_config, output_schema = _parse_chat_response_format(response_format)
    return ParsedOpenAIRequest(
        protocol="chat.completions",
        requested_model=model,
        canonical_input=tuple(conversation),
        request_history=tuple(conversation[:-1]),
        current_text=current,
        instructions="\n\n".join(developer_parts) or None,
        base_instructions="\n\n".join(base_parts) or None,
        stream=stream,
        previous_response_id=None,
        metadata=_metadata(body.get("metadata")),
        reasoning_effort=_optional_string(body, "reasoning_effort"),
        reasoning_summary=None,
        service_tier=_optional_string(body, "service_tier"),
        text_config=text_config,
        output_schema=output_schema,
        include_usage=include_usage,
    )


def response_id_from_turn(turn_id: str) -> str:
    return _external_id(turn_id, "resp")


def chat_id_from_turn(turn_id: str) -> str:
    return _external_id(turn_id, "chatcmpl")


def turn_id_from_response(response_id: str) -> str:
    return _turn_id(response_id, "resp")


def turn_id_from_chat(completion_id: str) -> str:
    return _turn_id(completion_id, "chatcmpl")


def response_object(turn: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    compat = compatibility_metadata(turn, "responses")
    status = response_status(str(turn["status"]))
    output = public_output_items(events)
    if status == "completed" and not output:
        raise server_error("Codex completed without a compatible response output item.")
    usage = response_usage(events, required=status == "completed")
    created = _unix_timestamp(str(turn["created_at"]))
    completed = _unix_timestamp(str(turn["completed_at"])) if turn.get("completed_at") else None
    error: dict[str, Any] | None = None
    if status == "failed":
        error = {
            "code": str(turn.get("error_code") or "server_error"),
            "message": "The response failed.",
        }
    reasoning = compat.get("reasoning") if isinstance(compat.get("reasoning"), dict) else {}
    text_config = compat.get("text") if isinstance(compat.get("text"), dict) else {"format": {"type": "text"}}
    return {
        "id": response_id_from_turn(str(turn["turn_id"])),
        "object": "response",
        "created_at": created,
        "completed_at": completed,
        "status": status,
        "background": False,
        "error": error,
        "incomplete_details": None,
        "instructions": compat.get("instructions"),
        "max_output_tokens": None,
        "max_tool_calls": None,
        "model": compat.get("requestedModel") or compat.get("resolvedModel"),
        "output": output,
        "parallel_tool_calls": False,
        "previous_response_id": compat.get("previousResponseId"),
        "reasoning": {
            "effort": reasoning.get("effort"),
            "summary": reasoning.get("summary"),
        },
        "service_tier": compat.get("serviceTier") or "default",
        "store": True,
        "temperature": None,
        "text": text_config,
        "tool_choice": "none",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage,
        "user": None,
        "metadata": compat.get("metadata") if isinstance(compat.get("metadata"), dict) else {},
    }


def chat_completion_object(turn: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    compat = compatibility_metadata(turn, "chat.completions")
    if str(turn["status"]) != "completed":
        raise server_error("The completion failed.")
    text = output_text(events)
    usage = response_usage(events, required=True)
    assert usage is not None
    return {
        "id": chat_id_from_turn(str(turn["turn_id"])),
        "object": "chat.completion",
        "created": _unix_timestamp(str(turn["created_at"])),
        "model": compat.get("requestedModel") or compat.get("resolvedModel"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                    "refusal": None,
                    "annotations": [],
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "prompt_tokens_details": usage["input_tokens_details"],
            "completion_tokens_details": usage["output_tokens_details"],
        },
        "service_tier": compat.get("serviceTier") or "default",
        "system_fingerprint": None,
    }


def compatibility_metadata(turn: dict[str, Any], expected_protocol: str | None = None) -> dict[str, Any]:
    resolved = turn.get("resolved_options")
    compat = resolved.get("openaiCompat") if isinstance(resolved, dict) else None
    if not isinstance(compat, dict):
        raise not_found("No compatible response was found.")
    if expected_protocol and compat.get("protocol") != expected_protocol:
        raise not_found("No compatible response was found.")
    return compat


def raw_output_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "compat.response.output_item":
            continue
        payload = event.get("payload")
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and item:
            items.append(dict(item))
    return items


def normalized_output_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct output when Codex omits rawResponseItem/completed."""
    output: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "message.completed":
            continue
        payload = event.get("payload")
        item = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        output.append(
            {
                "id": item.get("id") or f"msg_{len(output) + 1}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        )
    return output


def public_output_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_items = raw_output_items(events)
    if not raw_items:
        return normalized_output_items(events)

    output: list[dict[str, Any]] = []
    for item in raw_items:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        content: list[dict[str, Any]] = []
        raw_content = item.get("content")
        if not isinstance(raw_content, list):
            continue
        for part in raw_content:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                content.append(
                    {
                        "type": "output_text",
                        "text": part["text"],
                        "annotations": part.get("annotations") if isinstance(part.get("annotations"), list) else [],
                        "logprobs": part.get("logprobs") if isinstance(part.get("logprobs"), list) else [],
                    }
                )
        if not content:
            continue
        output.append(
            {
                "id": item.get("id") or f"msg_{len(output) + 1}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": content,
            }
        )
    return output


def output_text(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in public_output_items(events):
        for part in item["content"]:
            if part.get("type") == "output_text":
                parts.append(str(part.get("text") or ""))
    return "".join(parts)


def response_usage(events: list[dict[str, Any]], *, required: bool) -> dict[str, Any] | None:
    last: dict[str, Any] | None = None
    for event in events:
        if event.get("event_type") != "compat.response.usage":
            continue
        payload = event.get("payload")
        token_usage = payload.get("tokenUsage") if isinstance(payload, dict) else None
        candidate = token_usage.get("last") if isinstance(token_usage, dict) else None
        if isinstance(candidate, dict):
            last = candidate
    if last is None:
        if required:
            raise server_error("Codex completed without token usage.")
        return None
    fields = ("totalTokens", "inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
    if any(not isinstance(last.get(field), int) or int(last[field]) < 0 for field in fields):
        raise server_error("Codex returned malformed token usage.")
    return {
        "input_tokens": int(last["inputTokens"]),
        "input_tokens_details": {
            "cached_tokens": int(last["cachedInputTokens"]),
        },
        "output_tokens": int(last["outputTokens"]),
        "output_tokens_details": {
            "reasoning_tokens": int(last["reasoningOutputTokens"]),
        },
        "total_tokens": int(last["totalTokens"]),
    }


def response_status(turn_status: str) -> str:
    if turn_status in {"starting", "running"}:
        return "in_progress"
    if turn_status == "queued":
        return "queued"
    if turn_status == "completed":
        return "completed"
    if turn_status in {"interrupted", "cancelled"}:
        return "cancelled"
    if turn_status == "timed_out":
        return "incomplete"
    return "failed"


def is_terminal(turn: dict[str, Any]) -> bool:
    return str(turn.get("status")) in _TERMINAL_STATUSES


def invalid(message: str, param: str | None = None) -> OpenAIProtocolError:
    return OpenAIProtocolError(message, param=param, code="invalid_parameter")


def unsupported(message: str, param: str | None = None) -> OpenAIProtocolError:
    return OpenAIProtocolError(message, param=param, code="unsupported_parameter")


def not_found(message: str) -> OpenAIProtocolError:
    return OpenAIProtocolError(
        message,
        status=HTTPStatus.NOT_FOUND,
        error_type="invalid_request_error",
        code="resource_not_found",
    )


def server_error(message: str) -> OpenAIProtocolError:
    return OpenAIProtocolError(
        message,
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
        error_type="server_error",
        code="server_error",
    )


def authentication_error() -> OpenAIProtocolError:
    return OpenAIProtocolError(
        "Incorrect API key provided.",
        status=HTTPStatus.UNAUTHORIZED,
        error_type="invalid_request_error",
        code="invalid_api_key",
    )


def _parse_response_input(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if isinstance(value, str) and value:
        message = _canonical_message("user", value)
        return [message], [], value
    if not isinstance(value, list) or not value:
        raise invalid("input must be a non-empty string or array.", "input")
    canonical: list[dict[str, Any]] = []
    image_counter = _ImageCounter()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise invalid("Every input item must be an object.", f"input.{index}")
        item_type = item.get("type")
        if item_type not in {None, "message"}:
            raise unsupported(f"Input item type {item_type!r} is not supported.", f"input.{index}.type")
        role = item.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            raise unsupported(f"Input role {role!r} is not supported.", f"input.{index}.role")
        unsupported_keys = set(item) - {"type", "role", "content", "id", "status"}
        if unsupported_keys:
            key = sorted(unsupported_keys)[0]
            raise unsupported(f"Input item field {key!r} is not supported.", f"input.{index}.{key}")
        content = _response_content_parts(item.get("content"), role, f"input.{index}.content", image_counter)
        canonical.append(_canonical_message(role, content))
    if canonical[-1]["role"] != "user":
        raise unsupported("The final input item must have role 'user'.", "input")
    current = _message_text(canonical[-1])
    return canonical, canonical[:-1], current


def _canonical_message(role: str, content: str | list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(content, str):
        part_type = "output_text" if role == "assistant" else "input_text"
        content = [{"type": part_type, "text": content}]
    return {
        "type": "message",
        "role": role,
        "content": content,
    }


def _message_text(message: dict[str, Any]) -> str:
    return _parts_text(message["content"])


def _parts_text(parts: list[dict[str, Any]]) -> str:
    return "".join(str(part.get("text") or "") for part in parts)


@dataclass
class _ImageCounter:
    count: int = 0
    total_bytes: int = 0


def _response_content_parts(
    value: Any,
    role: str,
    param: str,
    image_counter: _ImageCounter,
) -> list[dict[str, Any]]:
    if isinstance(value, str) and value:
        return [{"type": "output_text" if role == "assistant" else "input_text", "text": value}]
    if not isinstance(value, list) or not value:
        raise invalid("Message content must be a non-empty string or array.", param)
    expected = "output_text" if role == "assistant" else "input_text"
    parts: list[dict[str, Any]] = []
    for index, part in enumerate(value):
        part_param = f"{param}.{index}"
        if not isinstance(part, dict):
            raise invalid("Every message content part must be an object.", part_param)
        part_type = part.get("type")
        if part_type == expected and isinstance(part.get("text"), str):
            parts.append({"type": expected, "text": part["text"]})
        elif part_type == "input_image":
            if role != "user":
                raise unsupported("Images are supported only in user messages.", part_param)
            _reject_unknown(part, {"type", "image_url", "detail"}, prefix=part_param)
            parts.append(
                _canonical_image(
                    part.get("image_url"),
                    part.get("detail"),
                    f"{part_param}.image_url",
                    f"{part_param}.detail",
                    image_counter,
                )
            )
        else:
            raise unsupported(f"Only {expected} content is supported.", part_param)
    if not _parts_text(parts) and not any(part["type"] == "input_image" for part in parts):
        raise invalid("Message content must include text or an image.", param)
    return parts


def _chat_content_parts(
    value: Any,
    role: str,
    param: str,
    image_counter: _ImageCounter,
) -> list[dict[str, Any]]:
    if isinstance(value, str) and value:
        return [{"type": "output_text" if role == "assistant" else "input_text", "text": value}]
    if not isinstance(value, list) or not value:
        raise invalid("Message content must be a non-empty string or text-part array.", param)
    expected = "output_text" if role == "assistant" else "input_text"
    parts: list[dict[str, Any]] = []
    for index, part in enumerate(value):
        part_param = f"{param}.{index}"
        if not isinstance(part, dict):
            raise invalid("Every message content part must be an object.", part_param)
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            parts.append({"type": expected, "text": part["text"]})
        elif part_type == "image_url":
            if role != "user":
                raise unsupported("Images are supported only in user messages.", part_param)
            _reject_unknown(part, {"type", "image_url"}, prefix=part_param)
            image = part.get("image_url")
            if not isinstance(image, dict):
                raise invalid("image_url must be an object with a data URL.", f"{part_param}.image_url")
            _reject_unknown(image, {"url", "detail"}, prefix=f"{part_param}.image_url")
            parts.append(
                _canonical_image(
                    image.get("url"),
                    image.get("detail"),
                    f"{part_param}.image_url.url",
                    f"{part_param}.image_url.detail",
                    image_counter,
                )
            )
        else:
            raise unsupported("Only Chat text and user image_url content parts are supported.", part_param)
    if not _parts_text(parts) and not any(part["type"] == "input_image" for part in parts):
        raise invalid("Message content must include text or an image.", param)
    return parts


def _canonical_image(
    url: Any,
    detail: Any,
    url_param: str,
    detail_param: str,
    image_counter: _ImageCounter,
) -> dict[str, Any]:
    if not isinstance(url, str):
        raise invalid("Image URL must be an inline base64 image data URL.", url_param)
    if not url.startswith("data:"):
        raise unsupported(
            "Only inline base64 image data URLs are supported; use data:image/png;base64,... instead of a remote URL, file path, or file ID.",
            url_param,
        )
    if len(url) > _MAX_DATA_IMAGE_URL_CHARS:
        raise invalid(f"Each image must be at most {OPENAI_MAX_IMAGE_BYTES} decoded bytes.", url_param)
    match = _DATA_IMAGE_URL_RE.fullmatch(url)
    if not match:
        raise unsupported(
            "Only inline base64 image data URLs are supported; use data:image/png;base64,... instead of a remote URL, file path, or file ID.",
            url_param,
        )
    encoded = match.group(2)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise invalid("Image data URL must contain valid base64 data.", url_param) from exc
    if len(decoded) > OPENAI_MAX_IMAGE_BYTES:
        raise invalid(f"Each image must be at most {OPENAI_MAX_IMAGE_BYTES} decoded bytes.", url_param)
    _validate_image_signature(match.group(1), decoded, url_param)
    image_counter.count += 1
    image_counter.total_bytes += len(decoded)
    if image_counter.count > OPENAI_MAX_IMAGES_PER_REQUEST:
        raise invalid(f"A request may contain at most {OPENAI_MAX_IMAGES_PER_REQUEST} images.", url_param)
    if image_counter.total_bytes > OPENAI_MAX_IMAGE_TOTAL_BYTES:
        raise invalid(
            f"Images in one request must total at most {OPENAI_MAX_IMAGE_TOTAL_BYTES} decoded bytes.",
            url_param,
        )
    if detail is not None and (not isinstance(detail, str) or detail not in _IMAGE_DETAILS):
        raise invalid("Image detail must be auto, low, high, or original.", detail_param)
    image = {"type": "input_image", "image_url": url}
    if detail is not None:
        image["detail"] = detail
    return image


def _validate_image_signature(mime_type: str, decoded: bytes, param: str) -> None:
    signatures = {
        "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
        "image/webp": lambda value: len(value) >= 12 and value.startswith(b"RIFF") and value[8:12] == b"WEBP",
        "image/gif": lambda value: value.startswith((b"GIF87a", b"GIF89a")),
    }
    if not signatures[mime_type](decoded):
        raise invalid("Image data does not match its declared MIME type.", param)


def _parse_responses_text(value: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if value is None:
        return {"format": {"type": "text"}}, None
    if not isinstance(value, dict):
        raise invalid("text must be an object.", "text")
    _reject_unknown(value, {"format", "verbosity"}, prefix="text")
    if value.get("verbosity") is not None:
        raise unsupported("text.verbosity is not supported.", "text.verbosity")
    format_value = value.get("format") or {"type": "text"}
    if not isinstance(format_value, dict):
        raise invalid("text.format must be an object.", "text.format")
    format_type = format_value.get("type", "text")
    if format_type == "text":
        _reject_unknown(format_value, {"type"}, prefix="text.format")
        return {"format": {"type": "text"}}, None
    if format_type != "json_schema":
        raise unsupported(f"text.format type {format_type!r} is not supported.", "text.format.type")
    _reject_unknown(format_value, {"type", "name", "description", "schema", "strict"}, prefix="text.format")
    schema = format_value.get("schema")
    if not isinstance(schema, dict):
        raise invalid("text.format.schema must be an object.", "text.format.schema")
    name = format_value.get("name")
    if not isinstance(name, str) or not name:
        raise invalid("text.format.name must be a non-empty string.", "text.format.name")
    normalized = {
        "type": "json_schema",
        "name": name,
        "schema": schema,
        "strict": bool(format_value.get("strict", True)),
    }
    if isinstance(format_value.get("description"), str):
        normalized["description"] = format_value["description"]
    return {"format": normalized}, schema


def _parse_chat_response_format(value: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if value is None:
        return {"format": {"type": "text"}}, None
    if not isinstance(value, dict):
        raise invalid("response_format must be an object.", "response_format")
    format_type = value.get("type")
    if format_type == "text":
        _reject_unknown(value, {"type"}, prefix="response_format")
        return {"format": {"type": "text"}}, None
    if format_type != "json_schema":
        raise unsupported(f"response_format type {format_type!r} is not supported.", "response_format.type")
    _reject_unknown(value, {"type", "json_schema"}, prefix="response_format")
    json_schema = value.get("json_schema")
    if not isinstance(json_schema, dict):
        raise invalid("response_format.json_schema must be an object.", "response_format.json_schema")
    return _parse_responses_text({"format": {"type": "json_schema", **json_schema}})


def _metadata(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise invalid("metadata must be an object.", "metadata")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise invalid("metadata keys and values must be strings.", "metadata")
        normalized[key] = item
    return normalized


def _reject_unknown(value: dict[str, Any], allowed: set[str], *, prefix: str | None = None) -> None:
    unknown = set(value) - allowed
    if unknown:
        key = sorted(unknown)[0]
        param = f"{prefix}.{key}" if prefix else key
        raise unsupported(f"Parameter {param!r} is not supported.", param)


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise invalid(f"{key} must be a non-empty string.", key)
    return item.strip()


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise invalid(f"{key} must be a non-empty string.", key)
    return item.strip()


def _optional_bool(value: dict[str, Any], key: str, default: bool) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise invalid(f"{key} must be a boolean.", key)
    return item


def _external_id(turn_id: str, prefix: str) -> str:
    if not turn_id.startswith("turn_"):
        raise server_error("Broker turn id cannot be represented as a compatible id.")
    token = turn_id[len("turn_") :]
    if not _TOKEN_RE.fullmatch(token):
        raise server_error("Broker turn id cannot be represented as a compatible id.")
    return f"{prefix}_{token}"


def _turn_id(value: str, prefix: str) -> str:
    marker = f"{prefix}_"
    if not isinstance(value, str) or not value.startswith(marker):
        raise not_found("No compatible response was found.")
    token = value[len(marker) :]
    if not _TOKEN_RE.fullmatch(token):
        raise not_found("No compatible response was found.")
    return f"turn_{token}"


def _unix_timestamp(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError) as exc:
        raise server_error("Broker response timestamp is invalid.") from exc
