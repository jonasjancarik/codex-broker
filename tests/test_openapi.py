from __future__ import annotations

import unittest

from codex_broker.http_api import openapi_document


class OpenApiTests(unittest.TestCase):
    def test_openapi_covers_product_facing_endpoints(self) -> None:
        document = openapi_document()
        paths = document["paths"]
        for path in [
            "/v1/owners/{ownerId}/auth/status",
            "/v1/owners/{ownerId}/auth/profiles",
            "/v1/owners/{ownerId}/auth/models",
            "/v1/owners/{ownerId}/auth/usage",
            "/v1/owners/{ownerId}/auth/rate-limits",
            "/v1/owners/{ownerId}/auth/rate-limit-reset-credit/consume",
            "/v1/owners/{ownerId}/auth/probe",
            "/v1/owners/{ownerId}/auth/device/start",
            "/v1/owners/{ownerId}/auth/device/submit",
            "/v1/owners/{ownerId}/auth/api-key",
            "/v1/owners/{ownerId}/auth/runtime/invalidate",
            "/v1/owners/{ownerId}/auth/logout",
            "/v1/owners/{ownerId}/audit-logs",
            "/v1/owners/{ownerId}/threads",
            "/v1/owners/{ownerId}/threads/{threadId}",
            "/v1/owners/{ownerId}/threads/{threadId}/archive",
            "/v1/owners/{ownerId}/threads/{threadId}/turns",
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}",
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/steer",
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interrupt",
            "/v1/owners/{ownerId}/threads/{threadId}/events",
            "/v1/owners/{ownerId}/threads/{threadId}/interactions",
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions",
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions/{interactionId}",
            "/v1/owners/{ownerId}/threads/{threadId}/turns/{turnId}/interactions/{interactionId}/resolve",
            "/v1/bundles/inline",
        ]:
            self.assertIn(path, paths)
        components = document["components"]
        self.assertIn("brokerKey", components["securitySchemes"])
        for schema in [
            "AuthStatus",
            "AuthProfile",
            "AuthProfileList",
            "ReasoningEffortOption",
            "ModelServiceTier",
            "CodexModel",
            "ModelListResponse",
            "AccountUsageResponse",
            "AccountRateLimitsResponse",
            "RateLimitResetCreditConsumeRequest",
            "RateLimitResetCreditConsumeResponse",
            "AuthProbeResult",
            "DeviceAuthSession",
            "AuditLog",
            "AuditLogList",
            "ThreadCreateRequest",
            "Thread",
            "TurnStartRequest",
            "Turn",
            "BrokerEvent",
            "Interaction",
            "InteractionList",
            "InteractionResolveRequest",
            "TaskBundle",
            "BundleAccepted",
            "Error",
        ]:
            self.assertIn(schema, components["schemas"])
        turn_start = paths["/v1/owners/{ownerId}/threads/{threadId}/turns"]["post"]
        self.assertEqual(
            turn_start["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/TurnStartRequest",
        )
        self.assertIn("mode", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("stream", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("configProfile", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertIn("runtimeProfile", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertIn("configProfile", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("codexOptions", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("runtime", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("webSearch", components["schemas"]["CodexOptions"]["properties"])
        self.assertIn("serviceTier", components["schemas"]["CodexOptions"]["properties"])
        self.assertIn("serviceTiers", components["schemas"]["CodexModel"]["properties"])
        self.assertIn("defaultServiceTier", components["schemas"]["CodexModel"]["properties"])
        self.assertIn("threadId", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertNotIn("productThreadId", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertNotIn("productThreadId", components["schemas"]["Thread"]["properties"])
        self.assertIn("hostApp", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertIn("hostApp", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("hostApp", components["schemas"]["Thread"]["properties"])
        self.assertIn("hostApp", components["schemas"]["Turn"]["properties"])
        self.assertIn("deleteProfile", components["schemas"]["LogoutRequest"]["properties"])
        self.assertNotIn("deleteProfile", components["schemas"]["AuthSelectorRequest"]["properties"])
        self.assertIn("authPrincipalId", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertIn("authPrincipalId", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("authPrincipalHash", components["schemas"]["Thread"]["properties"])
        self.assertIn("authPrincipalHash", components["schemas"]["Turn"]["properties"])
        self.assertIn("deleted", components["schemas"]["AuthCommandResult"]["properties"])
        self.assertIn("errorCode", components["schemas"]["AuthProbeResult"]["properties"])
        self.assertIn("authFingerprint", components["schemas"]["AuthStatus"]["properties"])
        self.assertIn("errorCode", components["schemas"]["Turn"]["properties"])
        self.assertIn("publicMessage", components["schemas"]["Turn"]["properties"])
        self.assertIn("adminMessage", components["schemas"]["Turn"]["properties"])
        self.assertIn("streamUrl", components["schemas"]["Turn"]["required"])
        self.assertIn("expiresAt", components["schemas"]["DeviceAuthSession"]["properties"])
        self.assertIn("interactionId", components["parameters"])
        self.assertIn("fallbackResponse", components["schemas"]["Interaction"]["properties"])
        inline_bundle = paths["/v1/bundles/inline"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(inline_bundle["$ref"], "#/components/schemas/TaskBundle")
        self.assertEqual(paths["/healthz"]["get"]["security"], [])
        self.assertEqual(paths["/readyz"]["get"]["security"], [])
        self.assertNotIn("security", paths["/metrics"]["get"])
        self.assertNotIn("security", paths["/openapi.json"]["get"])


if __name__ == "__main__":
    unittest.main()
