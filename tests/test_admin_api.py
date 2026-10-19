from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Any

from codex_broker.http_api import BrokerHandler, BrokerServices, metric_path_template
from codex_broker.openai_auth import compatibility_key_digest
from codex_broker.services import BrokerHTTPServer
from test_broker import config_for, wait_turn

OPERATOR_KEY = "operator-test-key"
OPENAI_KEY = "openai-compatible-test-key"
INPUT_CANARY = "admin-dashboard-input-canary-7f3c"
SECRET_CANARY = "admin-dashboard-secret-canary-91ab"


class AdminServer:
    def __init__(self, tmp: Path, **overrides: Any) -> None:
        config = replace(
            config_for(tmp),
            operator_key=OPERATOR_KEY,
            openai_compat_bindings={compatibility_key_digest(OPENAI_KEY): {"ownerId": "openai-owner"}},
        )
        self.services = BrokerServices.build(replace(config, **overrides))
        services = self.services

        class Handler(BrokerHandler):
            broker = services

        self.server = BrokerHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def request(
        self,
        method: str,
        path: str,
        *,
        key: str | None = OPERATOR_KEY,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            request_headers = dict(headers or {})
            if key is not None:
                request_headers["Authorization"] = f"Bearer {key}"
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                request_headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        status, _, body = self.request("GET", path, **kwargs)
        if status != HTTPStatus.OK:
            raise AssertionError(f"{path} returned {status}: {body!r}")
        return json.loads(body)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.services.pool.close_all()
        self.services.state.close()


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.admin: AdminServer | None = None

    def tearDown(self) -> None:
        if self.admin:
            self.admin.close()
        self._tmp.cleanup()

    def start(self, **overrides: Any) -> AdminServer:
        self.admin = AdminServer(self.tmp, **overrides)
        return self.admin

    def run_turn(self, owner: str, text: str = "hello") -> dict[str, Any]:
        assert self.admin
        scheduler = self.admin.services.scheduler
        thread = scheduler.create_thread(owner, {})
        started = scheduler.start_turn(owner, thread["threadId"], {"input": [{"type": "text", "text": text}]})
        return wait_turn(self.admin.services, owner, thread["threadId"], started["turnId"])

    def test_admin_routes_do_not_exist_without_an_operator_key(self) -> None:
        admin = self.start(operator_key=None)
        for path in ("/admin", "/admin/app.js", "/admin/api/overview"):
            with self.subTest(path=path):
                status, _, _ = admin.request("GET", path, key="test-key")
                self.assertEqual(status, HTTPStatus.NOT_FOUND)

    def test_only_the_operator_key_opens_admin_api(self) -> None:
        admin = self.start()
        for key in (None, "test-key", OPENAI_KEY, "wrong"):
            with self.subTest(key=key):
                status, _, _ = admin.request("GET", "/admin/api/overview", key=key)
                self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        status, _, _ = admin.request(
            "GET", "/admin/api/overview", key=None, headers={"X-Codex-Broker-Key": OPERATOR_KEY}
        )
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        status, headers, _ = admin.request("GET", "/admin/api/overview")
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(headers["cache-control"], "no-store")

    def test_operator_key_is_rejected_on_host_routes_even_when_unauthenticated_is_allowed(self) -> None:
        for overrides in ({}, {"internal_key": None, "allow_unauthenticated": True}):
            with self.subTest(overrides=overrides):
                admin = self.start(**overrides)
                try:
                    for path in ("/metrics", "/v1/owners/owner-a/threads/missing"):
                        status, _, _ = admin.request("GET", path)
                        self.assertEqual(status, HTTPStatus.UNAUTHORIZED, path)
                        status, _, _ = admin.request("GET", path, key=None, headers={"X-Codex-Broker-Key": OPERATOR_KEY})
                        self.assertEqual(status, HTTPStatus.UNAUTHORIZED, path)
                    status, _, _ = admin.request("GET", "/v1/models")
                    self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                    status, _, _ = admin.request("GET", "/metrics", key="test-key" if overrides == {} else None)
                    self.assertEqual(status, HTTPStatus.OK)
                finally:
                    admin.close()
                    self.admin = None

    def test_page_is_served_without_auth_and_with_a_strict_policy(self) -> None:
        admin = self.start()
        for path, kind in (("/admin", "text/html"), ("/admin/app.js", "text/javascript"), ("/admin/app.css", "text/css")):
            with self.subTest(path=path):
                status, headers, body = admin.request("GET", path, key=None)
                self.assertEqual(status, HTTPStatus.OK)
                self.assertTrue(headers["content-type"].startswith(kind))
                self.assertIn("script-src 'self'", headers["content-security-policy"])
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
                self.assertGreater(len(body), 0)
        html = admin.request("GET", "/admin", key=None)[2].decode("utf-8")
        self.assertNotIn("<script>", html)
        self.assertNotIn(" style=", html)
        self.assertEqual(admin.request("GET", "/admin/other", key=None)[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(admin.request("POST", "/admin", key=None)[0], HTTPStatus.NOT_FOUND)

    def test_overview_owners_and_detail(self) -> None:
        admin = self.start()
        self.run_turn("owner-a")
        self.run_turn("owner-a")
        self.run_turn("owner-b")
        owner_a = admin.services.auth.hash_owner("owner-a")

        overview = admin.get_json("/admin/api/overview")
        self.assertEqual(overview["ownersTotal"], 2)
        self.assertEqual(overview["ownersActiveLast24h"], 2)
        self.assertEqual(overview["turnsLast24hByStatus"], {"completed": 3})
        self.assertEqual(sum(overview["threadsByStatus"].values()), 3)
        self.assertIn(overview["readiness"]["status"], {"ready", "not_ready"})
        self.assertIn("activeTurns", overview["live"])

        owners = admin.get_json("/admin/api/owners")
        self.assertEqual({item["ownerHash"] for item in owners["items"]}, {owner_a, admin.services.auth.hash_owner("owner-b")})
        first = next(item for item in owners["items"] if item["ownerHash"] == owner_a)
        self.assertEqual(first["threadCount"], 2)
        self.assertEqual(first["turnsLast24hByStatus"], {"completed": 2})
        self.assertEqual(len(first["authPrincipalHashes"]), 1)

        page = admin.get_json("/admin/api/owners?limit=1")
        self.assertEqual(len(page["items"]), 1)
        self.assertIsNotNone(page["nextCursor"])
        rest = admin.get_json(f"/admin/api/owners?limit=1&cursor={page['nextCursor']}")
        self.assertEqual(len(rest["items"]), 1)
        self.assertIsNone(rest["nextCursor"])
        self.assertNotEqual(page["items"][0]["ownerHash"], rest["items"][0]["ownerHash"])

        detail = admin.get_json(f"/admin/api/owners/{owner_a}")
        self.assertEqual(detail["owner"]["ownerHash"], owner_a)
        self.assertEqual(len(detail["threads"]), 2)
        self.assertEqual(len(detail["recentTurns"]), 2)
        self.assertIsInstance(detail["authProfiles"], list)
        self.assertEqual(admin.request("GET", "/admin/api/owners/" + "0" * 64)[0], HTTPStatus.NOT_FOUND)

    def test_turns_processes_and_audit_lists(self) -> None:
        admin = self.start()
        for _ in range(3):
            self.run_turn("owner-a")
        turns = admin.get_json("/admin/api/turns?limit=2")
        self.assertEqual(len(turns["items"]), 2)
        more = admin.get_json(f"/admin/api/turns?limit=2&cursor={turns['nextCursor']}")
        self.assertEqual(len(more["items"]), 1)
        self.assertIsNone(more["nextCursor"])
        self.assertEqual(admin.get_json("/admin/api/turns?status=failed")["items"], [])
        self.assertEqual(len(admin.get_json("/admin/api/turns?limit=500")["items"]), 3)

        processes = admin.get_json("/admin/api/processes")
        self.assertIn("activeAppServerChildren", processes["live"])
        self.assertGreaterEqual(len(processes["items"]), 1)
        self.assertIn("exitCode", processes["items"][0])

        audit = admin.get_json("/admin/api/audit")
        self.assertIn("actionCounts", audit)
        ids = [entry["id"] for entry in audit["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        action = audit["items"][0]["action"]
        filtered = admin.get_json(f"/admin/api/audit?action={action}")
        self.assertTrue(all(entry["action"] == action for entry in filtered["items"]))

        self.assertEqual(admin.request("GET", "/admin/api/turns?cursor=%%%")[0], HTTPStatus.BAD_REQUEST)
        self.assertEqual(admin.request("GET", "/admin/api/unknown")[0], HTTPStatus.NOT_FOUND)
        self.assertEqual(admin.request("POST", "/admin/api/overview", body={})[0], HTTPStatus.NOT_FOUND)

    def test_no_admin_endpoint_returns_conversation_content_or_secrets(self) -> None:
        admin = self.start()
        admin.services.sanitizer.register("admin-test", SECRET_CANARY)
        turn = self.run_turn("owner-a", text=f"{INPUT_CANARY} {SECRET_CANARY}")
        owner_hash = admin.services.auth.hash_owner("owner-a")
        with admin.services.state._lock, admin.services.state._conn:
            admin.services.state._conn.execute(
                "update turns set status = 'failed', error_code = 'test_failure', admin_message = ?, public_message = ?",
                (f"admin saw {SECRET_CANARY}", f"public saw {SECRET_CANARY}"),
            )
        paths = [
            "/admin/api/overview",
            "/admin/api/owners",
            f"/admin/api/owners/{owner_hash}",
            "/admin/api/turns",
            "/admin/api/processes",
            "/admin/api/audit",
        ]
        for path in paths:
            with self.subTest(path=path):
                status, _, body = admin.request("GET", path)
                self.assertEqual(status, HTTPStatus.OK)
                text = body.decode("utf-8")
                self.assertNotIn(INPUT_CANARY, text)
                self.assertNotIn(SECRET_CANARY, text)
                self.assertNotIn("owner-a", text)
                self.assertNotIn('"input"', text)
        failed = admin.get_json("/admin/api/turns?status=failed")["items"][0]
        self.assertEqual(failed["turnId"], turn["turnId"])
        self.assertIn("admin saw", failed["adminMessage"])

    def test_owner_lookup_hashes_without_echoing_or_logging_the_id(self) -> None:
        admin = self.start(json_logs=True)
        self.run_turn("owner-known")
        output = io.StringIO()
        with redirect_stdout(output):
            known = json.loads(admin.request("POST", "/admin/api/owners/lookup", body={"ownerId": "owner-known"})[2])
            unknown = json.loads(admin.request("POST", "/admin/api/owners/lookup", body={"ownerId": "owner-unknown"})[2])
        self.assertEqual(known, {"ownerHash": admin.services.auth.hash_owner("owner-known"), "known": True})
        self.assertFalse(unknown["known"])
        self.assertNotIn("owner-known", output.getvalue())
        self.assertNotIn("owner-unknown", output.getvalue())
        self.assertEqual(
            admin.request("POST", "/admin/api/owners/lookup", body={"ownerId": ""})[0], HTTPStatus.BAD_REQUEST
        )
        self.assertEqual(
            admin.request("POST", "/admin/api/owners/lookup", key="test-key", body={"ownerId": "x"})[0],
            HTTPStatus.UNAUTHORIZED,
        )

    def test_admin_metric_paths_are_templated(self) -> None:
        self.assertEqual(metric_path_template("/admin"), "admin")
        self.assertEqual(metric_path_template("/admin/app.js"), "admin")
        self.assertEqual(metric_path_template("/admin/api/overview"), "admin/api/overview")
        self.assertEqual(metric_path_template("/admin/api/owners/abc123"), "admin/api/owners/ownerHash")
        self.assertEqual(metric_path_template("/admin/api/owners/lookup"), "admin/api/owners/lookup")
        self.assertEqual(metric_path_template("/admin/api/turns"), "admin/api/turns")


if __name__ == "__main__":
    unittest.main()
