from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .auth_api import selector


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
    if method == "GET" and tail == ["usage"]:
        profile = selector(query, None, "profile", "default") or "default"
        principal_id = selector(query, None, "authPrincipalId")
        scope, profile_key = _account_scope(handler, owner_id, profile, principal_id)
        with handler.broker.auth.profile_guard(scope.auth_principal_hash, profile_key):
            client = _account_client(handler, scope, profile_key)
            usage = client.request("account/usage/read")
        handler._json({**scope.public(), "profile": profile_key, "usage": usage})
        return True
    if method == "GET" and tail == ["rate-limits"]:
        profile = selector(query, None, "profile", "default") or "default"
        principal_id = selector(query, None, "authPrincipalId")
        scope, profile_key = _account_scope(handler, owner_id, profile, principal_id)
        with handler.broker.auth.profile_guard(scope.auth_principal_hash, profile_key):
            client = _account_client(handler, scope, profile_key)
            limits = client.request("account/rateLimits/read")
        handler._json({**scope.public(), "profile": profile_key, "rateLimits": limits})
        return True
    if method == "POST" and tail == ["rate-limit-reset-credit", "consume"]:
        body = handler._read_json()
        idempotency_key = body.get("idempotencyKey")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotencyKey must be a non-empty string.")
        idempotency_key = idempotency_key.strip()
        if len(idempotency_key) > 256:
            raise ValueError("idempotencyKey must be at most 256 characters.")
        requested_profile = selector(query, body, "profile", "default") or "default"
        principal_id = selector(query, body, "authPrincipalId")
        scope, profile_key = _account_scope(handler, owner_id, requested_profile, principal_id)
        with handler.broker.auth.profile_guard(scope.auth_principal_hash, profile_key):
            client = _account_client(handler, scope, profile_key)
            result = client.request(
                "account/rateLimitResetCredit/consume",
                {"idempotencyKey": idempotency_key},
            )
        handler.broker.state.append_audit(
            scope.owner_hash,
            "auth.rate_limit_reset_credit.consume",
            {"idempotencyKey": idempotency_key},
            auth_principal_hash=scope.auth_principal_hash,
            profile=profile_key,
        )
        handler._json({**scope.public(), "profile": profile_key, "resetCredit": result})
        return True
    return False


def _account_scope(
    handler: Any,
    owner_id: str,
    profile: str,
    auth_principal_id: str | None,
) -> tuple[Any, str]:
    scope = handler.broker.auth.resolve_scope(owner_id, auth_principal_id)
    profile_key = handler.broker.auth.profile_key(profile)
    return scope, profile_key


def _account_client(handler: Any, scope: Any, profile_key: str) -> Any:
    return handler.broker.pool.get(
        auth_principal_hash=scope.auth_principal_hash,
        profile=profile_key,
        codex_home=handler.broker.auth.profile_home(scope.auth_principal_hash, profile_key),
        config_profile="default",
        mcp_servers=(),
        auth_fingerprint=handler.broker.auth.auth_fingerprint(scope.auth_principal_hash, profile_key),
    )


def openapi_paths(
    owner_param: JsonSchema,
    ref: RefFactory,
    json_response: ResponseFactory,
    request_body: RequestBodyFactory,
) -> dict[str, Any]:
    return {
        "/v1/owners/{ownerId}/auth/usage": {
            "get": {
                "parameters": [
                    owner_param,
                    {"$ref": "#/components/parameters/profile"},
                    {"$ref": "#/components/parameters/authPrincipalId"},
                ],
                "responses": {
                    "200": json_response(ref("AccountUsageResponse"), "Account usage"),
                    "403": json_response(ref("Error"), "Auth principal not permitted"),
                },
            }
        },
        "/v1/owners/{ownerId}/auth/rate-limits": {
            "get": {
                "parameters": [
                    owner_param,
                    {"$ref": "#/components/parameters/profile"},
                    {"$ref": "#/components/parameters/authPrincipalId"},
                ],
                "responses": {
                    "200": json_response(ref("AccountRateLimitsResponse"), "Account rate limits"),
                    "403": json_response(ref("Error"), "Auth principal not permitted"),
                },
            }
        },
        "/v1/owners/{ownerId}/auth/rate-limit-reset-credit/consume": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("RateLimitResetCreditConsumeRequest")),
                "responses": {
                    "200": json_response(ref("RateLimitResetCreditConsumeResponse"), "Rate-limit reset credit consumed"),
                    "403": json_response(ref("Error"), "Auth principal not permitted"),
                },
            }
        },
    }


def openapi_schemas() -> dict[str, Any]:
    account_payload = {"type": "object", "additionalProperties": True}
    scope = {
        "ownerHash": {"type": "string"},
        "authPrincipalHash": {"type": "string"},
        "sharedAuthPrincipal": {"type": "boolean"},
        "profile": {"type": "string"},
    }
    return {
        "AccountUsageResponse": {
            "type": "object",
            "required": ["ownerHash", "authPrincipalHash", "sharedAuthPrincipal", "profile", "usage"],
            "properties": {**scope, "usage": account_payload},
            "description": "Upstream totals for authPrincipalHash + profile; totals may be shared by several owners.",
        },
        "AccountRateLimitsResponse": {
            "type": "object",
            "required": ["ownerHash", "authPrincipalHash", "sharedAuthPrincipal", "profile", "rateLimits"],
            "properties": {**scope, "rateLimits": account_payload},
            "description": "Upstream limits for authPrincipalHash + profile; limits may be shared by several owners.",
        },
        "RateLimitResetCreditConsumeRequest": {
            "type": "object",
            "required": ["idempotencyKey"],
            "properties": {
                "profile": {"type": "string", "default": "default"},
                "authPrincipalId": {"type": "string"},
                "idempotencyKey": {"type": "string", "minLength": 1, "maxLength": 256},
            },
        },
        "RateLimitResetCreditConsumeResponse": {
            "type": "object",
            "required": ["ownerHash", "authPrincipalHash", "sharedAuthPrincipal", "profile", "resetCredit"],
            "properties": {**scope, "resetCredit": account_payload},
        },
    }
