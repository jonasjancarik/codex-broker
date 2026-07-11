from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Any


JsonSchema = dict[str, Any]
RefFactory = Callable[[str], dict[str, str]]
ResponseFactory = Callable[[JsonSchema, str], JsonSchema]
RequestBodyFactory = Callable[..., JsonSchema]


def handle_auth_route(
    handler: Any,
    method: str,
    tail: list[str],
    owner_id: str,
    query: dict[str, list[str]],
) -> bool:
    if method == "GET" and tail == ["profiles"]:
        principal_id = selector(query, None, "authPrincipalId")
        handler._json(handler.broker.auth.list_profiles(owner_id, principal_id))
        return True
    if method == "GET" and tail == ["status"]:
        profile = handler.broker.auth.profile_key(selector(query, None, "profile", "default"))
        principal_id = selector(query, None, "authPrincipalId")
        scope = handler.broker.auth.resolve_scope(owner_id, principal_id)
        with handler.broker.auth.profile_guard(scope.auth_principal_hash, profile):
            handler._json(handler.broker.auth.status(owner_id, profile, principal_id))
        return True
    if method != "POST":
        return False
    tail_key = tuple(tail)
    if tail_key not in {
        ("probe",),
        ("device", "start"),
        ("device", "submit"),
        ("api-key",),
        ("runtime", "invalidate"),
        ("logout",),
    }:
        return False
    body = handler._read_json(allow_empty=tail_key not in {("device", "submit"), ("api-key",)})
    profile = handler.broker.auth.profile_key(selector(query, body, "profile", "default"))
    principal_id = selector(query, body, "authPrincipalId")
    scope = handler.broker.auth.resolve_scope(owner_id, principal_id)
    with handler.broker.auth.profile_guard(scope.auth_principal_hash, profile):
        if tail == ["probe"]:
            result = handler.broker.auth.probe(owner_id, profile, principal_id)
            if result["state"] == "refresh_failed" or result.get("previousAuthFingerprint") != result.get(
                "authFingerprint"
            ):
                handler.broker.pool.close_profile(scope.auth_principal_hash, profile)
            handler._json(result)
            return True
        if tail == ["device", "start"]:
            result = handler.broker.auth.start_device_auth(owner_id, profile, principal_id)
            handler._json({**scope.public(), **result}, HTTPStatus.ACCEPTED)
            return True
        if tail == ["device", "submit"]:
            result = handler.broker.auth.submit_device_code(
                owner_id,
                str(body.get("code") or ""),
                profile=profile,
                session_id=body.get("sessionId") if isinstance(body.get("sessionId"), str) else None,
                auth_principal_id=principal_id,
            )
            handler._json({**scope.public(), **result})
            return True
        if tail == ["api-key"]:
            handler.broker.pool.close_profile(scope.auth_principal_hash, profile)
            result = handler.broker.auth.login_api_key(
                owner_id,
                str(body.get("apiKey") or ""),
                profile,
                principal_id,
            )
            handler._json(result)
            return True
        if tail == ["runtime", "invalidate"]:
            handler.broker.pool.close_profile(scope.auth_principal_hash, profile)
            handler.broker.state.append_audit(
                scope.owner_hash,
                "auth.runtime.invalidate",
                {},
                auth_principal_hash=scope.auth_principal_hash,
                profile=profile,
            )
            handler._json({**scope.public(), "profile": profile, "invalidated": True})
            return True
        delete_profile = body.get("deleteProfile", False)
        if not isinstance(delete_profile, bool):
            raise ValueError("deleteProfile must be a boolean.")
        handler.broker.pool.close_profile(scope.auth_principal_hash, profile)
        handler._json(
            handler.broker.auth.logout(
                owner_id,
                profile,
                delete_profile=delete_profile,
                auth_principal_id=principal_id,
            )
        )
        return True


def selector(
    query: dict[str, list[str]],
    body: dict[str, Any] | None,
    key: str,
    default: str | None = None,
) -> str | None:
    query_values = query.get(key, [])
    if len(query_values) > 1 and len(set(query_values)) > 1:
        raise ValueError(f"Conflicting {key} query values.")
    query_value = query_values[0] if query_values else None
    body_value = body.get(key) if body is not None else None
    for value in (query_value, body_value):
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{key} must be a non-empty string.")
    if query_value is not None and body_value is not None and query_value != body_value:
        raise ValueError(f"Conflicting {key} values in query and JSON body.")
    return body_value or query_value or default


def openapi_paths(
    owner_param: JsonSchema,
    ref: RefFactory,
    json_response: ResponseFactory,
    request_body: RequestBodyFactory,
) -> dict[str, Any]:
    auth_query = [
        owner_param,
        {"$ref": "#/components/parameters/profile"},
        {"$ref": "#/components/parameters/authPrincipalId"},
    ]
    forbidden = {"403": json_response(ref("Error"), "Auth principal not permitted")}
    return {
        "/v1/owners/{ownerId}/auth/profiles": {
            "get": {
                "parameters": [owner_param, {"$ref": "#/components/parameters/authPrincipalId"}],
                "responses": {"200": json_response(ref("AuthProfileList")), **forbidden},
            }
        },
        "/v1/owners/{ownerId}/auth/status": {
            "get": {
                "parameters": auth_query,
                "responses": {"200": json_response(ref("AuthStatus")), **forbidden},
            }
        },
        "/v1/owners/{ownerId}/auth/probe": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("AuthSelectorRequest"), required=False),
                "responses": {"200": json_response(ref("AuthProbeResult"), "Active auth probe result"), **forbidden},
            }
        },
        "/v1/owners/{ownerId}/auth/device/start": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("AuthSelectorRequest"), required=False),
                "responses": {"202": json_response(ref("DeviceAuthSession"), "Device auth started"), **forbidden},
            }
        },
        "/v1/owners/{ownerId}/auth/device/submit": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("DeviceCodeSubmitRequest")),
                "responses": {"200": json_response(ref("DeviceAuthSession"), "Device code submitted"), **forbidden},
            }
        },
        "/v1/owners/{ownerId}/auth/api-key": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("ApiKeyLoginRequest")),
                "responses": {"200": json_response(ref("AuthCommandResult"), "API key stored"), **forbidden},
            }
        },
        "/v1/owners/{ownerId}/auth/runtime/invalidate": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("AuthSelectorRequest"), required=False),
                "responses": {"200": json_response(ref("RuntimeInvalidationResult"), "Profile runtime invalidated"), **forbidden},
            }
        },
        "/v1/owners/{ownerId}/auth/logout": {
            "post": {
                "parameters": [owner_param],
                "requestBody": request_body(ref("LogoutRequest"), required=False),
                "responses": {"200": json_response(ref("AuthCommandResult"), "Profile logged out"), **forbidden},
            }
        },
    }


def openapi_schemas(ref: RefFactory) -> dict[str, Any]:
    scope = {
        "ownerHash": {"type": "string"},
        "authPrincipalHash": {"type": "string"},
        "sharedAuthPrincipal": {"type": "boolean"},
    }
    selector_properties = {
        "profile": {"type": "string", "default": "default"},
        "authPrincipalId": {
            "type": "string",
            "description": "Optional assertion of the trusted host's configured auth principal for this owner.",
        },
    }
    scope_required = ["ownerHash", "authPrincipalHash", "sharedAuthPrincipal"]
    return {
        "AuthSelectorRequest": {"type": "object", "properties": selector_properties},
        "LogoutRequest": {
            "type": "object",
            "properties": {**selector_properties, "deleteProfile": {"type": "boolean", "default": False}},
        },
        "AuthProfile": {
            "type": "object",
            "required": ["profile", "state", "createdAt", "updatedAt"],
            "properties": {
                "profile": {"type": "string"},
                "state": {"type": "string", "description": "Last recorded auth state; listing does not run an active probe."},
                "authType": {"type": ["string", "null"]},
                "authFingerprint": {"type": ["string", "null"]},
                "createdAt": {"type": "string"},
                "updatedAt": {"type": "string"},
            },
        },
        "AuthProfileList": {
            "type": "object",
            "required": [*scope_required, "profiles"],
            "properties": {**scope, "profiles": {"type": "array", "items": ref("AuthProfile")}},
        },
        "DeviceCodeSubmitRequest": {
            "type": "object",
            "required": ["code"],
            "properties": {"code": {"type": "string"}, **selector_properties, "sessionId": {"type": "string"}},
        },
        "ApiKeyLoginRequest": {
            "type": "object",
            "required": ["apiKey"],
            "properties": {"apiKey": {"type": "string", "writeOnly": True}, **selector_properties},
        },
        "DeviceAuthSession": {
            "type": "object",
            "required": [*scope_required, "sessionId", "state", "profile", "expiresAt"],
            "properties": {
                **scope,
                "sessionId": {"type": "string"},
                "state": {"type": "string"},
                "profile": {"type": "string"},
                "command": {"type": "array", "items": {"type": "string"}},
                "startedAt": {"type": "string"},
                "updatedAt": {"type": "string"},
                "completedAt": {"type": ["string", "null"]},
                "loginUrl": {"type": ["string", "null"]},
                "userCode": {"type": ["string", "null"]},
                "expiresAt": {"type": ["string", "null"]},
                "output": {"type": "array", "items": {"type": "string"}},
                "exitCode": {"type": ["integer", "null"]},
                "error": {"type": ["string", "null"]},
            },
        },
        "AuthStatus": {
            "type": "object",
            "required": [*scope_required, "profile", "state", "authFilePresent"],
            "properties": {
                **scope,
                "profile": {"type": "string"},
                "state": {
                    "enum": ["missing", "present_unverified", "authenticated", "refresh_failed", "invalid", "failed", "unknown"]
                },
                "deviceAuth": {"anyOf": [ref("DeviceAuthSession"), {"type": "null"}]},
                "authFilePresent": {"type": "boolean"},
                "authFingerprint": {"type": "string"},
                "loginStatusExitCode": {"type": ["integer", "null"]},
                "loginStatusOutput": {"type": "string"},
            },
        },
        "AuthCommandResult": {
            "type": "object",
            "required": [*scope_required, "profile", "state", "exitCode", "output"],
            "properties": {
                **scope,
                "profile": {"type": "string"},
                "state": {"type": "string"},
                "deleted": {"type": "boolean"},
                "authFingerprint": {"type": "string"},
                "exitCode": {"type": "integer"},
                "output": {"type": "string"},
            },
        },
        "AuthProbeResult": {
            "type": "object",
            "required": [
                *scope_required,
                "profile",
                "state",
                "authFilePresent",
                "authFingerprint",
                "previousAuthFingerprint",
                "command",
                "startedAt",
                "completedAt",
                "durationMs",
                "output",
            ],
            "properties": {
                **scope,
                "profile": {"type": "string"},
                "state": {"enum": ["missing", "authenticated", "refresh_failed", "invalid", "failed"]},
                "authFilePresent": {"type": "boolean"},
                "authFingerprint": {"type": "string"},
                "previousAuthFingerprint": {"type": "string"},
                "command": {"type": "array", "items": {"type": "string"}},
                "startedAt": {"type": "string"},
                "completedAt": {"type": "string"},
                "durationMs": {"type": "number"},
                "exitCode": {"type": ["integer", "null"]},
                "output": {"type": "string"},
                "errorCode": {"type": ["string", "null"]},
                "publicMessage": {"type": ["string", "null"]},
                "adminMessage": {"type": ["string", "null"]},
            },
        },
        "RuntimeInvalidationResult": {
            "type": "object",
            "required": [*scope_required, "profile", "invalidated"],
            "properties": {**scope, "profile": {"type": "string"}, "invalidated": {"type": "boolean"}},
        },
    }
