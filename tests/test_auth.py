from __future__ import annotations

import os
import stat
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_broker.auth import AuthManager, normalize_profile, render_managed_codex_config
from codex_broker.http_api import BrokerServices
from codex_broker.runtime_errors import CODEX_AUTH_REQUIRES_ADMIN, classify_runtime_error
from codex_broker.security import SecretSanitizer
from codex_broker.state import StateStore
from test_broker import config_for, wait_turn


class AuthProfileTests(unittest.TestCase):
    def test_auth_secret_registration_refreshes_and_is_removed_on_logout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            state = StateStore(config.state_db_path)
            sanitizer = SecretSanitizer()
            auth = AuthManager(config, state, sanitizer=sanitizer)
            principal_hash = auth.hash_owner("owner-a")
            try:
                auth_file = auth.auth_file(principal_hash, "default")
                auth_file.write_text(
                    '{"tokens":{"access_token":"first-canary"},"metadata":"ordinary-value"}',
                    encoding="utf-8",
                )
                auth.refresh_profile_secrets(principal_hash, "default")
                self.assertEqual(sanitizer.sanitize_text("first-canary"), "<redacted>")
                self.assertEqual(sanitizer.sanitize_text("ordinary-value"), "ordinary-value")

                auth_file.write_text('{"refresh_token":"second-canary"}', encoding="utf-8")
                auth.refresh_profile_secrets(principal_hash, "default")
                self.assertEqual(sanitizer.sanitize_text("first-canary"), "first-canary")
                self.assertEqual(sanitizer.sanitize_text("second-canary"), "<redacted>")

                auth.logout("owner-a", "default")
                self.assertEqual(sanitizer.sanitize_text("second-canary"), "second-canary")
            finally:
                state.close()

    def test_runtime_home_is_separate_private_and_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            state = StateStore(config.state_db_path)
            auth = AuthManager(config, state)
            try:
                principal_hash = auth.hash_owner("owner-a")
                auth_home = auth.profile_home(principal_hash, "work/team")
                runtime_home = auth.runtime_home(principal_hash, "work/team")

                self.assertEqual(
                    runtime_home,
                    (config.runtime_home_root / principal_hash / "profiles" / "work_team").resolve(),
                )
                self.assertNotEqual(runtime_home, auth_home)
                self.assertFalse((runtime_home / "auth.json").exists())
                self.assertEqual(stat.S_IMODE(runtime_home.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(config.runtime_home_root.stat().st_mode), 0o700)
                with self.assertRaisesRegex(ValueError, "safe path component"):
                    auth.runtime_home("../outside", "default")
            finally:
                state.close()

    def test_managed_config_renders_parse_safe_default_deny_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = replace(
                config_for(tmp),
                data_dir=tmp / 'data "quoted"',
                credential_store='file "quoted"',
                sandbox_deny_paths=(tmp / 'extra "quoted"',),
            )
            rendered = render_managed_codex_config(config)
            parsed = tomllib.loads(rendered)

            self.assertEqual(parsed["default_permissions"], "broker-read-only")
            self.assertEqual(parsed["cli_auth_credentials_store"], 'file "quoted"')
            read_only = parsed["permissions"]["broker-read-only"]
            workspace_write = parsed["permissions"]["broker-workspace-write"]
            self.assertEqual(read_only["extends"], ":read-only")
            self.assertEqual(workspace_write["extends"], ":workspace")
            self.assertEqual(read_only["filesystem"][":root"], "deny")
            self.assertEqual(read_only["filesystem"][":minimal"], "read")
            self.assertEqual(read_only["filesystem"][":workspace_roots"]["."], "read")
            self.assertEqual(workspace_write["filesystem"][":workspace_roots"]["."], "write")
            self.assertEqual(workspace_write["filesystem"][":slash_tmp"], "deny")
            self.assertEqual(workspace_write["filesystem"][":tmpdir"], "deny")
            recursive_secret_globs = {
                "**/.env",
                "**/.env.*",
                "**/*.key",
                "**/*.pem",
                "**/*.p12",
                "**/*.pfx",
                "**/id_rsa",
                "**/id_ed25519",
                "**/id_dsa",
                "**/id_ecdsa",
            }
            redundant_root_masks = {
                ".env",
                ".env.*",
                "*.key",
                "*.pem",
                "*.p12",
                "*.pfx",
                "id_rsa",
                "id_ed25519",
                "id_dsa",
                "id_ecdsa",
            }
            for profile in (read_only, workspace_write):
                filesystem = profile["filesystem"]
                self.assertEqual(filesystem["glob_scan_max_depth"], 12)
                self.assertEqual(filesystem[str(config.auth_root.resolve())], "deny")
                self.assertEqual(filesystem[str(config.state_db_path.parent.resolve())], "deny")
                self.assertEqual(filesystem["/run/secrets"], "deny")
                workspace_denies = filesystem[":workspace_roots"]
                self.assertTrue(recursive_secret_globs.issubset(workspace_denies))
                self.assertTrue(all(workspace_denies[pattern] == "deny" for pattern in recursive_secret_globs))
                self.assertTrue(redundant_root_masks.isdisjoint(workspace_denies))

    def test_managed_config_file_is_private_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            state = StateStore(config.state_db_path)
            auth = AuthManager(config, state)
            try:
                home = auth.profile_home(auth.hash_owner("owner-a"), "default")
                config_path = home / "config.toml"
                first = config_path.read_text(encoding="utf-8")
                auth.profile_home(auth.hash_owner("owner-a"), "default")
                self.assertEqual(config_path.read_text(encoding="utf-8"), first)
                self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            finally:
                state.close()

    def test_dot_segment_profile_ids_are_rejected(self) -> None:
        for profile in (".", ".."):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                normalize_profile(profile)

    def test_token_invalidated_messages_are_codex_auth_failures(self) -> None:
        message = (
            "failed to refresh available models: unexpected status 401 Unauthorized: "
            "Your authentication token has been invalidated. Please try signing in again., "
            "auth error code: token_invalidated refresh_token_invalidated"
        )

        self.assertEqual(classify_runtime_error(message).code, CODEX_AUTH_REQUIRES_ADMIN)

    def test_profile_ids_are_canonicalized_for_auth_state_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            state = StateStore(config.state_db_path)
            auth = AuthManager(config, state)
            try:
                self.assertEqual(normalize_profile("work/team"), "work_team")
                result = auth.login_api_key("user@example.com", "sk-test", "work/team")
                owner_hash = result["ownerHash"]

                self.assertEqual(result["profile"], "work_team")
                self.assertTrue((config.auth_root / owner_hash / "profiles" / "work_team" / "codex-home" / "auth.json").exists())
                self.assertFalse((config.auth_root / owner_hash / "profiles" / "work" / "team").exists())
                self.assertEqual([row["profile"] for row in state.list_profiles(owner_hash)], ["work_team"])

                status = auth.status("user@example.com", "work/team")
                self.assertEqual(status["profile"], "work_team")
                self.assertEqual([row["profile"] for row in state.list_profiles(owner_hash)], ["work_team"])

                logout = auth.logout("user@example.com", "work/team", delete_profile=True)
                self.assertEqual(logout["profile"], "work_team")
                self.assertEqual(state.list_profiles(owner_hash), [])
                self.assertFalse((config.auth_root / owner_hash / "profiles" / "work_team").exists())
            finally:
                state.close()

    def test_scheduler_canonicalizes_and_keeps_thread_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw), turn_delay=0.01))
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"profile": "work/team", "cwd": str(services.config.allowed_workspace_roots[0])},
                )
                self.assertEqual(thread["profile"], "work_team")

                turn = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"profile": "work/team", "input": [{"type": "text", "text": "profile"}]},
                )
                self.assertEqual(turn["profile"], "work_team")
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"], "completed")

                owner_hash = services.auth.hash_owner("owner-a")
                self.assertEqual([row["profile"] for row in services.state.list_profiles(owner_hash)], ["work_team"])
                with self.assertRaisesRegex(Exception, "bound to auth profile"):
                    services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"profile": "ops/team", "input": [{"type": "text", "text": "switch"}]},
                    )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_active_probe_success_marks_profile_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                services.auth.login_api_key("owner-a", "sk-test", "default")

                result = services.auth.probe("owner-a", "default")

                self.assertEqual(result["state"], "authenticated")
                self.assertEqual(result["exitCode"], 0)
                self.assertTrue(result["authFilePresent"])
                self.assertIn("exec", result["command"])
                status = services.auth.status("owner-a", "default")
                self.assertEqual(status["state"], "authenticated")

                owner_hash = services.auth.hash_owner("owner-a")
                actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
                self.assertIn("auth.probe.start", actions)
                self.assertIn("auth.probe.success", actions)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_active_probe_token_invalidated_marks_refresh_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                services.auth.login_api_key("owner-a", "sk-test", "default")
                os.environ["FAKE_CODEX_AUTH_REFRESH_FAILURE"] = "1"

                result = services.auth.probe("owner-a", "default")

                self.assertEqual(result["state"], "refresh_failed")
                self.assertEqual(result["errorCode"], CODEX_AUTH_REQUIRES_ADMIN)
                self.assertIn("token_invalidated", result["output"])
                status = services.auth.status("owner-a", "default")
                self.assertEqual(status["state"], "refresh_failed")

                owner_hash = services.auth.hash_owner("owner-a")
                actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
                self.assertIn("auth.probe.failure", actions)
                self.assertIn("auth.runtime.failure", actions)
            finally:
                os.environ.pop("FAKE_CODEX_AUTH_REFRESH_FAILURE", None)
                services.pool.close_all()
                services.state.close()

    def test_api_key_auth_records_start_audit_and_auth_lifecycle_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                result = services.auth.login_api_key("owner-a", "sk-test", "default")
                self.assertEqual(result["state"], "authenticated")

                owner_hash = services.auth.hash_owner("owner-a")
                services.state.append_audit(
                    owner_hash,
                    "auth.device.failure",
                    {},
                    auth_principal_hash=owner_hash,
                    profile="default",
                )
                actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
                self.assertIn("auth.api_key.start", actions)
                self.assertIn("auth.api_key.success", actions)

                start_log = next(entry for entry in services.state.list_audit_logs(owner_hash) if entry["action"] == "auth.api_key.start")
                self.assertEqual(start_log["payload"], {})

                metrics = services.scheduler.metrics()
                self.assertEqual(metrics["auth_starts"], 1)
                self.assertEqual(metrics["auth_successes"], 1)
                self.assertEqual(metrics["auth_failures"], 1)
                self.assertEqual(metrics["audit_auth_api_key_start"], 1)
                self.assertEqual(metrics["audit_auth_api_key_success"], 1)
                self.assertEqual(metrics["audit_auth_device_failure"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_api_key_auth_spawn_failure_is_audited_without_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), codex_command=("missing-codex-binary",))
            services = BrokerServices.build(config)
            try:
                result = services.auth.login_api_key("owner-a", "sk-live-secret-value", "default")
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["exitCode"], -1)
                self.assertNotIn("sk-live-secret-value", result["output"])

                owner_hash = services.auth.hash_owner("owner-a")
                actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
                self.assertEqual(actions, ["auth.api_key.start", "auth.api_key.failure"])
                metrics = services.scheduler.metrics()
                self.assertEqual(metrics["auth_starts"], 1)
                self.assertEqual(metrics["auth_failures"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_device_auth_spawn_failure_is_audited_as_failed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), codex_command=("missing-codex-binary",))
            services = BrokerServices.build(config)
            try:
                started = services.auth.start_device_auth("owner-a", "default")
                self.assertEqual(started["state"], "failed")
                self.assertEqual(started["exitCode"], -1)
                self.assertIsNotNone(started["error"])

                owner_hash = services.auth.hash_owner("owner-a")
                actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
                self.assertEqual(actions, ["auth.device.start", "auth.device.failure"])
                metrics = services.scheduler.metrics()
                self.assertEqual(metrics["auth_starts"], 1)
                self.assertEqual(metrics["auth_failures"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_logout_removes_auth_file_even_when_codex_logout_fails_to_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), codex_command=("missing-codex-binary",))
            services = BrokerServices.build(config)
            try:
                owner_hash = services.auth.hash_owner("owner-a")
                home = services.auth.profile_home(owner_hash, "default")
                auth_file = home / "auth.json"
                auth_file.write_text('{"OPENAI_API_KEY":"local-secret"}', encoding="utf-8")

                result = services.auth.logout("owner-a", "default")
                self.assertEqual(result["state"], "unauthenticated")
                self.assertEqual(result["exitCode"], -1)
                self.assertFalse(auth_file.exists())
                self.assertNotIn("local-secret", result["output"])

                logs = services.state.list_audit_logs(owner_hash)
                self.assertEqual(logs[-1]["action"], "auth.logout")
                self.assertEqual(logs[-1]["payload"], {"exitCode": -1, "deleteProfile": False})
            finally:
                services.pool.close_all()
                services.state.close()

    def test_logout_delete_profile_continues_when_codex_logout_fails_to_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), codex_command=("missing-codex-binary",))
            services = BrokerServices.build(config)
            try:
                owner_hash = services.auth.hash_owner("owner-a")
                home = services.auth.profile_home(owner_hash, "work")
                home.joinpath("auth.json").write_text('{"OPENAI_API_KEY":"local-secret"}', encoding="utf-8")

                result = services.auth.logout("owner-a", "work", delete_profile=True)
                self.assertEqual(result["state"], "deleted")
                self.assertTrue(result["deleted"])
                self.assertEqual(result["exitCode"], -1)
                self.assertFalse(home.parent.exists())
                self.assertEqual(services.state.list_profiles(owner_hash), [])

                actions = [entry["action"] for entry in services.state.list_audit_logs(owner_hash)]
                self.assertEqual(actions[-2:], ["auth.profile.delete", "auth.logout"])
            finally:
                services.pool.close_all()
                services.state.close()

    def test_delete_profile_does_not_report_success_when_directory_removal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            try:
                result = services.auth.login_api_key("owner-a", "sk-test", "work")
                principal_hash = result["authPrincipalHash"]
                profile_dir = services.auth.profile_home(principal_hash, "work").parent

                with patch("codex_broker.auth.shutil.rmtree", side_effect=OSError("permission denied")):
                    with self.assertRaisesRegex(OSError, "permission denied"):
                        services.auth.logout("owner-a", "work", delete_profile=True)

                self.assertTrue(profile_dir.exists())
                self.assertIsNotNone(services.state.get_profile(principal_hash, "work"))
                actions = [entry["action"] for entry in services.state.list_audit_logs(result["ownerHash"])]
                self.assertNotIn("auth.profile.delete", actions)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_delete_profile_waits_for_active_device_auth_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            services = BrokerServices.build(config_for(Path(tmp_raw)))
            os.environ["FAKE_CODEX_DEVICE_AUTH_DELAY"] = "2"
            try:
                started = services.auth.start_device_auth("owner-a", "work")
                principal_hash = services.auth.hash_owner("owner-a")
                session = services.auth._session(principal_hash, "work")
                assert session is not None and session.process is not None

                result = services.auth.logout("owner-a", "work", delete_profile=True)

                self.assertTrue(result["deleted"])
                self.assertIsNotNone(session.process.poll())
                self.assertFalse((services.config.auth_root / principal_hash / "profiles" / "work").exists())
                self.assertIsNone(services.state.get_profile(principal_hash, "work"))
            finally:
                os.environ.pop("FAKE_CODEX_DEVICE_AUTH_DELAY", None)
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
