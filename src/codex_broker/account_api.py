from __future__ import annotations

from collections.abc import Callable
from typing import Any


JsonSchema = dict[str, Any]
RefFactory = Callable[[str], dict[str, str]]
ResponseFactory = Callable[[JsonSchema, str], JsonSchema]
RequestBodyFactory = Callable[..., JsonSchema]


def handle_account_route(
    handler: Any,
    method: str,
    tail: list[str],
    owner_id: str,
    query: dict[str, list[str]],
) -> bool:
    profile = query.get("profile", ["default"])[0]
    if method == "GET" and tail == ["usage"]:
        owner_hash, profile_key, client = _account_client(handler, owner_id, profile)
        usage = client.request("account/usage/read")
        handler._json({"ownerHash": owner_hash, "profile": profile_key, "usage": usage})
        return True
    if method == "GET" and tail == ["rate-limits"]:
        owner_hash, profile_key, client = _account_client(handler, owner_id, profile)
        limits = client.request("account/rateLimits/read")
        handler._json({"ownerHash": owner_hash, "profile": profile_key, "rateLimits": limits})
        return True
    if method == "POST" and tail == ["rate-limit-reset-credit", "consume"]:
        body = handler._read_json()
        idempotency_key = body.get("idempotencyKey")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotencyKey must be a non-empty string.")
        idempotency_key = idempotency_key.strip()
        if len(idempotency_key) > 256:
            raise ValueError("idempotencyKey must be at most 256 characters.")
        requested_profile = str(body.get("profile") or profile)
        owner_hash, profile_key, client = _account_client(handler, owner_id, requested_profile)
        result = client.request(
            "account/rateLimitResetCredit/consume",
            {"idempotencyKey": idempotency_key},
        )
        handler.broker.state.append_audit(
            owner_hash,
            "auth.rate_limit_reset_credit.consume",
            {"idempotencyKey": idempotency_key},
            profile=profile_key,
        )
        handler._json({"ownerHash": owner_hash, "profile": profile_key, "resetCredit": result})
        return True
    return False


def _account_client(handler: Any, owner_id: str, profile: str) -> tuple[str, str, Any]:
    owner_hash = handler.broker.auth.hash_owner(owner_id)
    profile_key = handler.broker.auth.profile_key(profile)
    client = handler.broker.pool.get(
        owner_hash=owner_hash,
        profile=profile_key,
        codex_home=handler.broker.auth.profile_home(owner_hash, profile_key),
        config_profile="default",
        mcp_servers=(),
        auth_fingerprint=handler.broker.auth.auth_fingerprint(owner_hash, profile_key),
    )
    return owner_hash, profile_key, client


def openapi_paths(
    owner_param: JsonSchema,
    ref: RefFactory,
    json_response: ResponseFactory,
    request_body: RequestBodyFactory,
) -> dict[str, Any]:
    return {
        "/v1/owners/{ownerId}/auth/usage": {
            "get": {
                "parameters": [owner_param, {"$ref": "#/components/parameters/profile"}],
                "responses": {"200": json_response(ref("AccountUsageResponse"), "Account usage")},
            }
        },
        "/v1/owners/{ownerId}/auth/rate-limits": {
            "get": {
                "parameters": [owner_param, {"$ref": "#/components/parameters/profile"}],
                "responses": {"200": json_response(ref("AccountRateLimitsResponse"), "Account rate limits")},
            }
        },
        "/v1/owners/{ownerId}/auth/rate-limit-reset-credit/consume": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("RateLimitResetCreditConsumeRequest")),
                "responses": {
                    "200": json_response(ref("RateLimitResetCreditConsumeResponse"), "Rate-limit reset credit consumed")
                },
            }
        },
    }


def openapi_schemas() -> dict[str, Any]:
    account_payload = {"type": "object", "additionalProperties": True}
    scope = {
        "ownerHash": {"type": "string"},
        "profile": {"type": "string"},
    }
    return {
        "AccountUsageResponse": {
            "type": "object",
            "required": ["ownerHash", "profile", "usage"],
            "properties": {**scope, "usage": account_payload},
        },
        "AccountRateLimitsResponse": {
            "type": "object",
            "required": ["ownerHash", "profile", "rateLimits"],
            "properties": {**scope, "rateLimits": account_payload},
        },
        "RateLimitResetCreditConsumeRequest": {
            "type": "object",
            "required": ["idempotencyKey"],
            "properties": {
                "profile": {"type": "string", "default": "default"},
                "idempotencyKey": {"type": "string", "minLength": 1, "maxLength": 256},
            },
        },
        "RateLimitResetCreditConsumeResponse": {
            "type": "object",
            "required": ["ownerHash", "profile", "resetCredit"],
            "properties": {**scope, "resetCredit": account_payload},
        },
    }
