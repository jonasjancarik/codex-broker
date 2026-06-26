from __future__ import annotations

import unittest

from codex_broker.http_api import openapi_document


class OpenApiTests(unittest.TestCase):
    def test_openapi_covers_product_facing_endpoints(self) -> None:
        document = openapi_document()
        paths = document["paths"]
        for path in [
            "/v1/owners/{ownerId}/auth/status",
            "/v1/owners/{ownerId}/auth/device/start",
            "/v1/owners/{ownerId}/auth/device/submit",
            "/v1/owners/{ownerId}/auth/api-key",
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
            "/v1/bundles/inline",
        ]:
            self.assertIn(path, paths)
        components = document["components"]
        self.assertIn("brokerKey", components["securitySchemes"])
        for schema in [
            "AuthStatus",
            "DeviceAuthSession",
            "AuditLog",
            "AuditLogList",
            "ThreadCreateRequest",
            "Thread",
            "TurnStartRequest",
            "Turn",
            "BrokerEvent",
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
        self.assertIn("configProfile", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("codexOptions", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("productThreadId", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertIn("productThreadId", components["schemas"]["Thread"]["properties"])
        self.assertIn("hostApp", components["schemas"]["ThreadCreateRequest"]["properties"])
        self.assertIn("hostApp", components["schemas"]["TurnStartRequest"]["properties"])
        self.assertIn("hostApp", components["schemas"]["Thread"]["properties"])
        self.assertIn("hostApp", components["schemas"]["Turn"]["properties"])
        self.assertIn("deleteProfile", components["schemas"]["ProfileRequest"]["properties"])
        self.assertIn("deleted", components["schemas"]["AuthCommandResult"]["properties"])
        self.assertIn("expiresAt", components["schemas"]["DeviceAuthSession"]["properties"])
        inline_bundle = paths["/v1/bundles/inline"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(inline_bundle["$ref"], "#/components/schemas/TaskBundle")
        self.assertEqual(paths["/healthz"]["get"]["security"], [])
        self.assertEqual(paths["/readyz"]["get"]["security"], [])
        self.assertNotIn("security", paths["/metrics"]["get"])
        self.assertNotIn("security", paths["/openapi.json"]["get"])


if __name__ == "__main__":
    unittest.main()
