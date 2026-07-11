from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from codex_broker.http_api import BrokerHandler
from codex_broker.scheduler import ConflictError, NotFoundError
from codex_broker.services import BrokerHTTPServer, BrokerServices
from test_broker import config_for, wait_turn


class AuthPrincipalTests(unittest.TestCase):
    def test_omitted_principal_preserves_owner_scoped_auth_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                scope = services.auth.resolve_scope("owner-a")
                self.assertEqual(scope.owner_hash, scope.auth_principal_hash)
                home = services.auth.profile_home(scope.auth_principal_hash, "default")
                self.assertEqual(
                    home,
                    (
                        services.config.auth_root
                        / scope.owner_hash
                        / "profiles"
                        / "default"
                        / "codex-home"
                    ).resolve(),
                )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_profile_listing_is_read_only_and_shared_by_mapped_owners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw)),
                auth_principal_mappings={"owner-a": "shared-account", "owner-b": "shared-account"},
            )
            services = BrokerServices.build(config)
            try:
                principal_hash = services.auth.hash_auth_principal("shared-account")
                principal_dir = config.auth_root / principal_hash
                empty = services.auth.list_profiles("owner-a", "shared-account")
                self.assertEqual(empty["profiles"], [])
                self.assertFalse(principal_dir.exists())

                services.auth.login_api_key("owner-a", "sk-test", "work", "shared-account")
                owner_a = services.auth.list_profiles("owner-a", "shared-account")
                owner_b = services.auth.list_profiles("owner-b", "shared-account")

                self.assertEqual(owner_a["authPrincipalHash"], principal_hash)
                self.assertEqual(owner_a["profiles"], owner_b["profiles"])
                self.assertTrue(owner_a["sharedAuthPrincipal"])
                self.assertEqual(owner_a["profiles"][0]["profile"], "work")
                self.assertEqual(owner_a["profiles"][0]["state"], "authenticated")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_shared_principal_device_auth_completes_in_principal_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw)),
                auth_principal_mappings={"owner-a": "shared-account", "owner-b": "shared-account"},
            )
            services = BrokerServices.build(config)
            try:
                shared_hash = services.auth.hash_auth_principal("shared-account")
                started = services.auth.start_device_auth("owner-a", "work", "shared-account")
                self.assertEqual(started["authPrincipalHash"], shared_hash)
                self.assertTrue(started["sharedAuthPrincipal"])
                deadline = time.monotonic() + 3
                status = started
                while status["state"] not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
                    time.sleep(0.02)
                    status = services.auth.status("owner-b", "work", "shared-account")["deviceAuth"]
                self.assertEqual(status["state"], "completed")
                principal_hash = services.auth.hash_auth_principal("shared-account")
                self.assertEqual(services.state.get_profile(principal_hash, "work")["auth_status"], "authenticated")
                owner_a_hash = services.auth.hash_owner("owner-a")
                owner_b_hash = services.auth.hash_owner("owner-b")
                self.assertIn(
                    "auth.device.success",
                    [row["action"] for row in services.state.list_audit_logs(owner_a_hash)],
                )
                self.assertNotIn(
                    "auth.device.success",
                    [row["action"] for row in services.state.list_audit_logs(owner_b_hash)],
                )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_shared_principal_reuses_pool_without_sharing_broker_state_or_audits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw), turn_delay=0.01),
                auth_principal_mappings={"owner-a": "shared-account", "owner-b": "shared-account"},
            )
            services = BrokerServices.build(config)
            try:
                services.auth.login_api_key("owner-a", "sk-test", "work", "shared-account")
                a = services.scheduler.create_thread(
                    "owner-a",
                    {"threadId": "thread-a", "authPrincipalId": "shared-account", "profile": "work"},
                )
                b = services.scheduler.create_thread(
                    "owner-b",
                    {"threadId": "thread-b", "authPrincipalId": "shared-account", "profile": "work"},
                )
                turn_a = services.scheduler.start_turn(
                    "owner-a",
                    a["threadId"],
                    {"authPrincipalId": "shared-account", "input": [{"type": "text", "text": "a"}]},
                )
                turn_b = services.scheduler.start_turn(
                    "owner-b",
                    b["threadId"],
                    {"authPrincipalId": "shared-account", "input": [{"type": "text", "text": "b"}]},
                )
                self.assertEqual(wait_turn(services, "owner-a", a["threadId"], turn_a["turnId"])["status"], "completed")
                self.assertEqual(wait_turn(services, "owner-b", b["threadId"], turn_b["turnId"])["status"], "completed")

                self.assertEqual(a["authPrincipalHash"], b["authPrincipalHash"])
                self.assertEqual(len(services.pool._clients), 1)
                process_rows = services.state.list_app_server_processes()
                self.assertEqual(len(process_rows), 1)
                self.assertEqual(process_rows[0]["auth_principal_hash"], a["authPrincipalHash"])
                with self.assertRaises(NotFoundError):
                    services.scheduler.get_thread("owner-b", a["threadId"])
                owner_a_hash = services.auth.hash_owner("owner-a")
                owner_b_hash = services.auth.hash_owner("owner-b")
                a_actions = [row["action"] for row in services.state.list_audit_logs(owner_a_hash)]
                b_actions = [row["action"] for row in services.state.list_audit_logs(owner_b_hash)]
                self.assertIn("auth.api_key.success", a_actions)
                self.assertNotIn("auth.api_key.success", b_actions)
                self.assertTrue(
                    all(row["auth_principal_hash"] == a["authPrincipalHash"] for row in services.state.list_audit_logs(owner_a_hash))
                )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_distinct_principals_keep_auth_homes_and_pool_children_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw), turn_delay=0.01),
                auth_principal_mappings={"owner-a": "account-a", "owner-b": "account-b"},
            )
            services = BrokerServices.build(config)
            try:
                login_a = services.auth.login_api_key("owner-a", "sk-a", "work", "account-a")
                login_b = services.auth.login_api_key("owner-b", "sk-b", "work", "account-b")
                self.assertNotEqual(login_a["authPrincipalHash"], login_b["authPrincipalHash"])
                self.assertNotEqual(
                    services.auth.profile_home(login_a["authPrincipalHash"], "work"),
                    services.auth.profile_home(login_b["authPrincipalHash"], "work"),
                )
                for owner, principal in (("owner-a", "account-a"), ("owner-b", "account-b")):
                    thread = services.scheduler.create_thread(
                        owner,
                        {"authPrincipalId": principal, "profile": "work"},
                    )
                    turn = services.scheduler.start_turn(
                        owner,
                        thread["threadId"],
                        {"authPrincipalId": principal, "input": [{"type": "text", "text": owner}]},
                    )
                    self.assertEqual(wait_turn(services, owner, thread["threadId"], turn["turnId"])["status"], "completed")
                self.assertEqual(len(services.pool._clients), 2)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_mapping_change_requires_a_new_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            base = config_for(Path(tmp_raw))
            first = BrokerServices.build(replace(base, auth_principal_mappings={"owner-a": "principal-one"}))
            try:
                thread = first.scheduler.create_thread(
                    "owner-a",
                    {"threadId": "stable", "authPrincipalId": "principal-one", "profile": "work"},
                )
            finally:
                first.pool.close_all()
                first.state.close()

            second = BrokerServices.build(replace(base, auth_principal_mappings={"owner-a": "principal-two"}))
            try:
                with self.assertRaisesRegex(ConflictError, "different auth principal"):
                    second.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"authPrincipalId": "principal-two", "input": [{"type": "text", "text": "continue"}]},
                    )
            finally:
                second.pool.close_all()
                second.state.close()

    def test_profile_replacement_invalidates_old_and_queued_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                services.auth.login_api_key("owner-a", "sk-first", "work")
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"threadId": "old-thread", "profile": "work"},
                )
                completed = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "before replacement"}]},
                )
                wait_turn(services, "owner-a", thread["threadId"], completed["turnId"])
                owner_hash = services.auth.hash_owner("owner-a")
                stored = services.state.get_thread(owner_hash, thread["threadId"])
                assert stored is not None
                queued = services.state.create_turn(
                    owner_hash,
                    thread["threadId"],
                    auth_principal_hash=stored["auth_principal_hash"],
                    auth_profile_instance_id=stored["auth_profile_instance_id"],
                    profile="work",
                    config_profile="default",
                    host_app=None,
                    bundle_id=None,
                    cwd=None,
                    mode="queue",
                    input_items=[{"type": "text", "text": "queued before replacement"}],
                    idempotency_key=None,
                    product_correlation_id=None,
                    status="queued",
                )

                with services.auth.profile_guard(stored["auth_principal_hash"], "work"):
                    services.pool.close_profile(stored["auth_principal_hash"], "work")
                    services.auth.logout("owner-a", "work", delete_profile=True)
                    services.auth.login_api_key("owner-a", "sk-second", "work")

                with self.assertRaisesRegex(ConflictError, "removed or replaced"):
                    services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"input": [{"type": "text", "text": "unsafe resume"}]},
                    )

                services.scheduler._run_turn(
                    owner_hash,
                    thread["threadId"],
                    queued["turn_id"],
                    {"input": queued["input"]},
                )
                queued_after = services.state.get_turn(owner_hash, thread["threadId"], queued["turn_id"])
                assert queued_after is not None
                self.assertEqual(queued_after["status"], "failed")

                replacement = services.scheduler.create_thread(
                    "owner-a",
                    {"threadId": "replacement-thread", "profile": "work"},
                )
                self.assertNotEqual(replacement["threadId"], thread["threadId"])
                fresh_turn = services.scheduler.start_turn(
                    "owner-a",
                    replacement["threadId"],
                    {"input": [{"type": "text", "text": "replacement"}]},
                )
                self.assertEqual(
                    wait_turn(services, "owner-a", replacement["threadId"], fresh_turn["turnId"])["status"],
                    "completed",
                )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_deleted_profile_stays_deleted_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            first = BrokerServices.build(config)
            try:
                first.auth.login_api_key("owner-a", "sk-test", "work")
                thread = first.scheduler.create_thread(
                    "owner-a",
                    {"threadId": "historical-thread", "profile": "work"},
                )
                first.auth.logout("owner-a", "work", delete_profile=True)
            finally:
                first.pool.close_all()
                first.state.close()

            second = BrokerServices.build(config)
            try:
                principal_hash = second.auth.hash_owner("owner-a")
                self.assertEqual(second.state.list_profiles(principal_hash), [])
                with self.assertRaisesRegex(ConflictError, "removed or replaced"):
                    second.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"input": [{"type": "text", "text": "must remain fenced"}]},
                    )
            finally:
                second.pool.close_all()
                second.state.close()

    def test_unapproved_http_principal_is_forbidden_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), auth_principal_mappings={"owner-a": "allowed"})
            services = BrokerServices.build(config)
            server, worker, base_url = self._start_server(services)
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self._request(
                        base_url,
                        "POST",
                        "/v1/owners/owner-a/threads",
                        {"authPrincipalId": "attacker-chosen", "profile": "work"},
                    )
                self.assertEqual(raised.exception.code, 403)
                attacker_hash = services.auth.hash_auth_principal("attacker-chosen")
                self.assertEqual(services.state.list_profiles(attacker_hash), [])
                self.assertFalse((config.auth_root / attacker_hash).exists())
            finally:
                server.shutdown()
                server.server_close()
                worker.join(1)
                services.pool.close_all()
                services.state.close()

    def test_shared_account_totals_report_principal_while_mutation_audit_stays_owner_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw)),
                auth_principal_mappings={"owner-a": "shared-account", "owner-b": "shared-account"},
            )
            services = BrokerServices.build(config)
            services.auth.login_api_key("owner-a", "sk-test", "work", "shared-account")
            server, worker, base_url = self._start_server(services)
            try:
                a = self._request(
                    base_url,
                    "GET",
                    "/v1/owners/owner-a/auth/usage?profile=work&authPrincipalId=shared-account",
                )
                b = self._request(
                    base_url,
                    "GET",
                    "/v1/owners/owner-b/auth/usage?profile=work&authPrincipalId=shared-account",
                )
                self.assertNotEqual(a["ownerHash"], b["ownerHash"])
                self.assertEqual(a["authPrincipalHash"], b["authPrincipalHash"])
                self.assertTrue(a["sharedAuthPrincipal"])
                self.assertEqual(a["usage"], b["usage"])

                self._request(
                    base_url,
                    "POST",
                    "/v1/owners/owner-b/auth/rate-limit-reset-credit/consume",
                    {
                        "profile": "work",
                        "authPrincipalId": "shared-account",
                        "idempotencyKey": "shared-reset",
                    },
                )
                owner_a_hash = services.auth.hash_owner("owner-a")
                owner_b_hash = services.auth.hash_owner("owner-b")
                self.assertEqual(
                    services.state.list_audit_logs(
                        owner_a_hash,
                        action="auth.rate_limit_reset_credit.consume",
                    ),
                    [],
                )
                b_logs = services.state.list_audit_logs(
                    owner_b_hash,
                    action="auth.rate_limit_reset_credit.consume",
                )
                self.assertEqual(len(b_logs), 1)
                self.assertEqual(b_logs[0]["auth_principal_hash"], a["authPrincipalHash"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(1)
                services.pool.close_all()
                services.state.close()

    @staticmethod
    def _start_server(services: BrokerServices) -> tuple[BrokerHTTPServer, threading.Thread, str]:
        class Handler(BrokerHandler):
            broker = services

        server = BrokerHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        return server, worker, f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def _request(
        base_url: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer test-key",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
