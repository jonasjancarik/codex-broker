from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from codex_broker.http_api import BrokerHandler
from codex_broker.services import BrokerHTTPServer, BrokerServices
from test_broker import config_for


class AccountApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config = config_for(Path(self._tmp.name))
        self.services = BrokerServices.build(config)
        self.services.auth.login_api_key("owner/a", "sk-test", "work")
        services = self.services

        class Handler(BrokerHandler):
            broker = services

        self.server = BrokerHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(1)
        self.services.scheduler.shutdown("interrupt", 1)
        self.services.pool.close_all()
        self.services.state.close()
        self._tmp.cleanup()

    def test_reads_usage_and_rate_limits_for_owner_profile(self) -> None:
        usage = self._request("GET", "/v1/owners/owner%2Fa/auth/usage?profile=work")
        limits = self._request("GET", "/v1/owners/owner%2Fa/auth/rate-limits?profile=work")

        owner_hash = self.services.auth.hash_owner("owner/a")
        self.assertEqual(usage["ownerHash"], owner_hash)
        self.assertEqual(usage["profile"], "work")
        self.assertEqual(usage["usage"]["totalTokens"], 1200)
        self.assertEqual(limits["ownerHash"], owner_hash)
        self.assertEqual(limits["rateLimits"]["primary"]["usedPercent"], 25)
        self.assertEqual(limits["rateLimits"]["resetCredits"], 1)

    def test_consumes_reset_credit_with_idempotency_and_audit(self) -> None:
        result = self._request(
            "POST",
            "/v1/owners/owner%2Fa/auth/rate-limit-reset-credit/consume",
            {"profile": "work", "idempotencyKey": "reset-123"},
        )

        self.assertEqual(result["profile"], "work")
        self.assertTrue(result["resetCredit"]["consumed"])
        self.assertEqual(result["resetCredit"]["idempotencyKey"], "reset-123")
        logs = self.services.state.list_audit_logs(
            self.services.auth.hash_owner("owner/a"),
            action="auth.rate_limit_reset_credit.consume",
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["payload"]["idempotencyKey"], "reset-123")

    def test_reset_credit_requires_idempotency_key(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self._request(
                "POST",
                "/v1/owners/owner%2Fa/auth/rate-limit-reset-credit/consume",
                {"profile": "work"},
            )
        self.assertEqual(raised.exception.code, 400)
        error = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(error["error"], "idempotencyKey must be a non-empty string.")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": "Bearer test-key", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
