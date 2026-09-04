from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_broker.bundles import BundleError
from codex_broker.config import BrokerConfig
from codex_broker.http_api import BrokerServices
from codex_broker.openai_auth import OpenAICompatAuth, compatibility_key_digest
from codex_broker import scheduler_config
from test_broker import config_for


class ConfigProfileTests(unittest.TestCase):
    def test_security_configuration_defaults_and_runtime_home_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {"CODEX_BROKER_DATA_DIR": tmp_raw},
            clear=True,
        ):
            config = BrokerConfig.from_env()

        self.assertEqual(config.event_sanitization_mode, "safe")
        self.assertEqual(config.sandbox_preflight_mode, "required" if sys.platform == "linux" else "warn")
        self.assertEqual(config.runtime_home_root, Path(tmp_raw).resolve() / "workspaces" / "runtime-homes")
        self.assertEqual(config.sandbox_deny_paths, ())
        self.assertIsNone(config.danger_full_access_key)
        self.assertEqual(config.max_pooled_app_servers, 0)

    def test_pool_child_limit_loads_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {
                "CODEX_BROKER_DATA_DIR": tmp_raw,
                "CODEX_BROKER_MAX_POOLED_APP_SERVERS": "2",
            },
            clear=True,
        ):
            self.assertEqual(BrokerConfig.from_env().max_pooled_app_servers, 2)

    def test_danger_full_access_key_loads_only_from_file_and_is_not_repr_visible(self) -> None:
        secret = "danger-full-access-secret"
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = Path(tmp_raw) / "danger-full-access.key"
            path.write_text(secret + "\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "CODEX_BROKER_DATA_DIR": tmp_raw,
                    "CODEX_BROKER_DANGER_FULL_ACCESS_KEY": "inline-secret-must-not-load",
                    "CODEX_BROKER_DANGER_FULL_ACCESS_KEY_FILE": str(path),
                },
                clear=True,
            ):
                config = BrokerConfig.from_env()

        self.assertEqual(config.danger_full_access_key, secret)
        self.assertNotIn(secret, repr(config))
        self.assertNotIn("inline-secret-must-not-load", repr(config))

    def test_danger_full_access_key_file_fails_closed_when_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            missing = Path(tmp_raw) / "missing.key"
            directory = Path(tmp_raw) / "directory"
            directory.mkdir()
            empty = Path(tmp_raw) / "empty.key"
            empty.write_text(" \n", encoding="utf-8")
            cases = (
                (missing, FileNotFoundError, "does not exist or is not a file"),
                (directory, FileNotFoundError, "does not exist or is not a file"),
                (empty, ValueError, "is empty"),
            )
            for path, error, message in cases:
                with self.subTest(path=path), patch.dict(
                    os.environ,
                    {
                        "CODEX_BROKER_DATA_DIR": tmp_raw,
                        "CODEX_BROKER_DANGER_FULL_ACCESS_KEY_FILE": str(path),
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(error, message):
                        BrokerConfig.from_env()

    def test_security_configuration_loads_extra_absolute_deny_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {
                "CODEX_BROKER_DATA_DIR": tmp_raw,
                "CODEX_BROKER_EVENT_SANITIZATION_MODE": "raw",
                "CODEX_BROKER_SANDBOX_PREFLIGHT": "disabled",
                "CODEX_BROKER_SANDBOX_DENY_PATHS": os.pathsep.join(
                    [str(Path(tmp_raw) / "one"), str(Path(tmp_raw) / "two")]
                ),
            },
            clear=True,
        ):
            config = BrokerConfig.from_env()

        self.assertEqual(config.event_sanitization_mode, "raw")
        self.assertEqual(config.sandbox_preflight_mode, "disabled")
        self.assertEqual(
            config.sandbox_deny_paths,
            (Path(tmp_raw, "one").resolve(), Path(tmp_raw, "two").resolve()),
        )

    def test_security_configuration_rejects_invalid_modes_and_relative_deny_paths(self) -> None:
        cases = (
            ("CODEX_BROKER_EVENT_SANITIZATION_MODE", "unsafe", "safe, raw"),
            ("CODEX_BROKER_SANDBOX_PREFLIGHT", "optional", "required, warn, disabled"),
            ("CODEX_BROKER_SANDBOX_DENY_PATHS", "relative/path", "must be absolute"),
        )
        for name, value, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
                os.environ,
                {"CODEX_BROKER_DATA_DIR": tmp_raw, name: value},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, message):
                    BrokerConfig.from_env()

    def test_openai_compat_bindings_load_from_digest_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            path = Path(tmp_raw) / "bindings.json"
            digest = compatibility_key_digest("compat-secret")
            path.write_text(
                json.dumps({digest: {"ownerId": "owner", "modelAliases": {"gpt": "gpt-5.6-sol"}}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "CODEX_BROKER_DATA_DIR": tmp_raw,
                    "CODEX_BROKER_OPENAI_COMPAT_BINDINGS_FILE": str(path),
                },
                clear=True,
            ):
                config = BrokerConfig.from_env()

        resolver = OpenAICompatAuth(config.openai_compat_bindings)
        binding = resolver.resolve_authorization("Bearer compat-secret")
        self.assertEqual(binding.owner_id, "owner")
        self.assertEqual(binding.model_aliases["gpt"], "gpt-5.6-sol")
        self.assertNotIn("compat-secret", repr(config.openai_compat_bindings))

    def test_openai_compat_bindings_reject_raw_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "sha256"):
            OpenAICompatAuth({"sk-raw-secret": {"ownerId": "owner"}})

    def test_missing_config_profile_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {
                "CODEX_BROKER_DATA_DIR": tmp_raw,
                "CODEX_BROKER_CONFIG_PROFILES_FILE": str(Path(tmp_raw) / "missing.json"),
            },
            clear=True,
        ):
            with self.assertRaises(FileNotFoundError):
                BrokerConfig.from_env()

    def test_generated_owner_hash_key_survives_internal_api_key_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            with patch.dict(
                os.environ,
                {"CODEX_BROKER_DATA_DIR": tmp_raw, "CODEX_BROKER_INTERNAL_KEY": "first-api-key"},
                clear=True,
            ):
                first = BrokerConfig.from_env()
            with patch.dict(
                os.environ,
                {"CODEX_BROKER_DATA_DIR": tmp_raw, "CODEX_BROKER_INTERNAL_KEY": "rotated-api-key"},
                clear=True,
            ):
                second = BrokerConfig.from_env()

            self.assertEqual(first.owner_hash_secret, second.owner_hash_secret)
            self.assertNotEqual(first.owner_hash_secret, "first-api-key")
            self.assertEqual((Path(tmp_raw) / "state" / "owner-hash.key").stat().st_mode & 0o777, 0o600)

    def test_config_profiles_load_from_env_json(self) -> None:
        payload = {
            "review": {
                "model": "gpt-5",
                "approvalPolicy": "on-request",
                "allowedWorkspaceRoots": ["/workspaces/review"],
                "enabledBundles": ["review-bundle"],
            }
        }
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {"CODEX_BROKER_DATA_DIR": tmp_raw, "CODEX_BROKER_CONFIG_PROFILES_JSON": json.dumps(payload)},
            clear=True,
        ):
            config = BrokerConfig.from_env()

        self.assertEqual(config.config_profiles["review"]["model"], "gpt-5")
        self.assertEqual(config.config_profiles["review"]["enabledBundles"], ["review-bundle"])

    def test_passthrough_env_loads_from_env_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {
                "CODEX_BROKER_DATA_DIR": tmp_raw,
                "CODEX_BROKER_PASSTHROUGH_ENV": "ESTF_ARCHIVER_API_URL, ESTF_ARCHIVER_API_KEY",
            },
            clear=True,
        ):
            config = BrokerConfig.from_env()

        self.assertEqual(
            config.codex_passthrough_env,
            ("ESTF_ARCHIVER_API_URL", "ESTF_ARCHIVER_API_KEY"),
        )

    def test_auth_principal_mappings_load_from_trusted_host_config(self) -> None:
        mapping = {"owner-a": "shared", "owner-b": "shared"}
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {
                "CODEX_BROKER_DATA_DIR": tmp_raw,
                "CODEX_BROKER_INTERNAL_KEY": "trusted-host-key",
                "CODEX_BROKER_AUTH_PRINCIPAL_MAP_JSON": json.dumps(mapping),
            },
            clear=True,
        ):
            config = BrokerConfig.from_env()

        self.assertEqual(config.auth_principal_mappings, mapping)

    def test_auth_principal_mapping_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw, patch.dict(
            os.environ,
            {
                "CODEX_BROKER_DATA_DIR": tmp_raw,
                "CODEX_BROKER_AUTH_PRINCIPAL_MAP_JSON": '{"owner-a":""}',
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "non-empty string"):
                BrokerConfig.from_env()

    def test_openai_compat_binding_rejects_unknown_policy_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown OpenAI compatibility binding field"):
            OpenAICompatAuth(
                {
                    compatibility_key_digest("compat-key"): {
                        "ownerId": "owner-a",
                        "config_profile": "misspelled",
                    }
                }
            )

    def test_config_profile_defaults_and_request_overrides_feed_app_server_params(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw)),
                config_profiles={
                    "review": {
                        "model": "gpt-5",
                        "sandbox": "workspace-write",
                        "personality": "concise",
                        "serviceTier": "flex",
                        "effort": "high",
                        "summary": "auto",
                        "outputSchema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                        },
                        "webSearch": "live",
                        "modelVerbosity": "medium",
                        "imageGeneration": True,
                        "features": {"multi_agent": True},
                    }
                },
            )
            services = BrokerServices.build(config)
            try:
                profile = services.scheduler._config_profile_config("review")
                cwd = config.allowed_workspace_roots[0]

                thread_params = services.scheduler._thread_params(
                    cwd,
                    {"codexOptions": {"model": "gpt-5.1"}},
                    None,
                    profile,
                )
                self.assertEqual(thread_params["cwd"], str(cwd))
                self.assertEqual(thread_params["model"], "gpt-5.1")
                self.assertEqual(
                    thread_params["approvalPolicy"],
                    {
                        "granular": {
                            "mcp_elicitations": True,
                            "request_permissions": True,
                            "rules": True,
                            "sandbox_approval": False,
                            "skill_approval": False,
                        }
                    },
                )
                self.assertEqual(thread_params["permissions"], "broker-workspace-write")
                self.assertEqual(thread_params["runtimeWorkspaceRoots"], [str(cwd.resolve())])
                self.assertNotIn("sandbox", thread_params)
                self.assertEqual(thread_params["personality"], "concise")

                turn_params = scheduler_config.turn_params(
                    services.scheduler,
                    "codex_thread_1",
                    [{"type": "text", "text": "review"}],
                    {
                        "codexOptions": {"model": "gpt-5.1-codex", "serviceTier": "fast"},
                        "runtime": {"reasoningEffort": "medium", "reasoningSummary": "concise"},
                    },
                    profile,
                    cwd=cwd,
                )
                self.assertEqual(turn_params["model"], "gpt-5.1-codex")
                self.assertEqual(turn_params["serviceTier"], "fast")
                self.assertEqual(turn_params["effort"], "medium")
                self.assertEqual(turn_params["personality"], "concise")
                self.assertEqual(turn_params["summary"], "concise")
                self.assertEqual(turn_params["outputSchema"], profile["outputSchema"])
                self.assertEqual(turn_params["permissions"], "broker-workspace-write")
                self.assertEqual(turn_params["runtimeWorkspaceRoots"], [str(cwd.resolve())])

                process_args = services.scheduler._codex_process_config_args(
                    {
                        "runtime": {
                            "webSearch": "disabled",
                            "modelVerbosity": "low",
                            "imageGeneration": False,
                            "reasoningEffort": "minimal",
                            "features": {"multi_agent": False},
                        }
                    },
                    profile,
                )
                self.assertEqual(
                    process_args,
                    (
                        ("web_search", "disabled"),
                        ("model_verbosity", "low"),
                        ("model_reasoning_effort", "minimal"),
                        ("features.image_generation", "false"),
                        ("features.multi_agent", "false"),
                    ),
                )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_sandbox_modes_characterize_permission_profile_wire_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            services = BrokerServices.build(config)
            try:
                cwd = config.allowed_workspace_roots[0]

                default_policy = services.scheduler._thread_params(cwd, {}, None)
                self.assertEqual(default_policy["permissions"], "broker-read-only")
                self.assertEqual(default_policy["runtimeWorkspaceRoots"], [str(cwd.resolve())])

                read_only = services.scheduler._thread_params(
                    cwd,
                    {"codexOptions": {"sandbox": "read-only"}},
                    None,
                )
                self.assertEqual(read_only["permissions"], "broker-read-only")
                self.assertNotIn("sandbox", read_only)
                self.assertEqual(read_only["runtimeWorkspaceRoots"], [str(cwd.resolve())])

                workspace_write = services.scheduler._thread_params(
                    cwd,
                    {"codexOptions": {"sandbox": "workspace-write"}},
                    None,
                )
                self.assertEqual(workspace_write["permissions"], "broker-workspace-write")
                self.assertNotIn("sandbox", workspace_write)
                self.assertEqual(workspace_write["runtimeWorkspaceRoots"], [str(cwd.resolve())])
                self.assertTrue(all(Path(root).is_absolute() for root in workspace_write["runtimeWorkspaceRoots"]))
                self.assertEqual(workspace_write["runtimeWorkspaceRoots"].count(str(cwd.resolve())), 1)

                overlay = config.overlay_root / "turn-test"
                overlay.mkdir(parents=True)
                with_overlay = services.scheduler._thread_params(
                    cwd,
                    {"codexOptions": {"sandbox": "workspace-write"}},
                    None,
                    runtime_read_root=overlay,
                )
                self.assertEqual(
                    with_overlay["runtimeWorkspaceRoots"],
                    [str(cwd.resolve()), str(overlay.resolve())],
                )

                with self.assertRaisesRegex(ValueError, "requires separate authorization"):
                    services.scheduler._thread_params(
                        cwd,
                        {"codexOptions": {"sandbox": "danger-full-access"}},
                        None,
                    )
                full_access = scheduler_config.thread_params(
                    services.scheduler,
                    cwd,
                    {"codexOptions": {"sandbox": "danger-full-access"}},
                    None,
                    danger_full_access_authorized=True,
                )
                self.assertEqual(full_access["sandbox"], "danger-full-access")
                self.assertNotIn("permissions", full_access)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_config_profiles_cannot_inject_low_level_sandbox_fields(self) -> None:
        for field, value in (
            ("permissions", "danger-full-access"),
            ("runtimeWorkspaceRoots", ["/"]),
            ("permissionProfile", "danger-full-access"),
            ("sandboxPolicy", {"type": "dangerFullAccess"}),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp_raw:
                config = replace(
                    config_for(Path(tmp_raw)),
                    config_profiles={"unsafe": {"sandbox": "workspace-write", field: value}},
                )
                services = BrokerServices.build(config)
                try:
                    with self.assertRaisesRegex(ValueError, f"field {field} is managed by the broker"):
                        services.scheduler._config_profile_config("unsafe")
                finally:
                    services.pool.close_all()
                    services.state.close()

    def test_thread_and_turn_params_characterize_auto_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            services = BrokerServices.build(config)
            try:
                thread_params = services.scheduler._thread_params(
                    config.allowed_workspace_roots[0],
                    {
                        "codexOptions": {
                            "sandbox": "workspace-write",
                            "approvalsReviewer": "user",
                        }
                    },
                    None,
                )
                self.assertEqual(thread_params["permissions"], "broker-workspace-write")
                self.assertEqual(thread_params["approvalsReviewer"], "user")
                self.assertNotIn("sandbox", thread_params)

                turn_params = scheduler_config.turn_params(
                    services.scheduler,
                    "codex_thread_1",
                    [{"type": "text", "text": "review"}],
                    {
                        "codexOptions": {
                            "sandbox": "workspace-write",
                            "approvalsReviewer": "user",
                        }
                    },
                    cwd=config.allowed_workspace_roots[0],
                )
                self.assertEqual(turn_params["permissions"], "broker-workspace-write")
                self.assertEqual(turn_params["approvalsReviewer"], "user")
                self.assertEqual(turn_params["runtimeWorkspaceRoots"], [str(config.allowed_workspace_roots[0].resolve())])
                self.assertNotIn("sandboxPolicy", turn_params)

                auto_review = services.scheduler._turn_params(
                    "codex_thread_1",
                    [{"type": "text", "text": "review"}],
                    {"codexOptions": {"approvalsReviewer": "auto_review"}},
                    cwd=config.allowed_workspace_roots[0],
                )
                self.assertEqual(auto_review["approvalsReviewer"], "auto_review")

                with self.assertRaisesRegex(ValueError, "approvalsReviewer.*user.*auto_review"):
                    services.scheduler._thread_params(
                        config.allowed_workspace_roots[0],
                        {"codexOptions": {"approvalsReviewer": "not-a-reviewer"}},
                        None,
                    )
                with self.assertRaisesRegex(ValueError, "approvalPolicy.*never.*auto_review"):
                    services.scheduler._thread_params(
                        config.allowed_workspace_roots[0],
                        {
                            "codexOptions": {
                                "approvalPolicy": "never",
                                "approvalsReviewer": "auto_review",
                            }
                        },
                        None,
                    )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_managed_sandbox_rejects_low_level_overrides_and_outside_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw, tempfile.TemporaryDirectory() as outside_raw:
            config = config_for(Path(tmp_raw))
            services = BrokerServices.build(config)
            try:
                cwd = config.allowed_workspace_roots[0]
                for field in ("permissions", "runtimeWorkspaceRoots", "permissionProfile", "sandboxPolicy"):
                    with self.subTest(field=field), self.assertRaisesRegex(ValueError, f"{field} is managed"):
                        services.scheduler._thread_params(
                            cwd,
                            {"codexOptions": {"sandbox": "read-only", field: "caller-value"}},
                            None,
                        )
                with self.assertRaisesRegex(BundleError, "outside broker workspace roots"):
                    services.scheduler._thread_params(
                        Path(outside_raw),
                        {"codexOptions": {"sandbox": "read-only"}},
                        None,
                    )
                with self.assertRaisesRegex(BundleError, "outside allowed workspace roots"):
                    services.bundles.validate_cwd(str(config.overlay_root), None)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_runtime_profile_alias_is_accepted_for_urad_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw)),
                config_profiles={"urad": {"enabledBundles": ["allowed-bundle"]}},
            )
            services = BrokerServices.build(config)
            try:
                bundle_dir = config.allowed_bundle_roots[0] / "allowed-bundle"
                bundle_dir.mkdir(parents=True)
                (bundle_dir / "bundle.json").write_text(
                    '{"id":"allowed-bundle","allowedPaths":[]}',
                    encoding="utf-8",
                )
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"runtimeProfile": "urad", "bundleId": "allowed-bundle"},
                )
                self.assertEqual(thread["configProfile"], "urad")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_config_profile_restricts_enabled_bundles_and_workspace_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            base = config_for(Path(tmp_raw))
            safe = base.allowed_workspace_roots[0] / "safe"
            unsafe = base.allowed_workspace_roots[0] / "unsafe"
            safe.mkdir()
            unsafe.mkdir()
            for bundle_id in ("allowed-bundle", "denied-bundle"):
                bundle_dir = base.allowed_bundle_roots[0] / bundle_id
                bundle_dir.mkdir()
                (bundle_dir / "bundle.json").write_text(json.dumps({"id": bundle_id}), encoding="utf-8")

            config = replace(
                base,
                config_profiles={
                    "locked": {
                        "enabledBundles": ["allowed-bundle"],
                        "allowedWorkspaceRoots": [str(safe)],
                    }
                },
            )
            services = BrokerServices.build(config)
            try:
                with self.assertRaises(BundleError):
                    services.scheduler.create_thread(
                        "owner-a",
                        {"configProfile": "locked", "bundleId": "denied-bundle", "cwd": str(safe)},
                    )
                with self.assertRaises(BundleError):
                    services.scheduler.create_thread(
                        "owner-a",
                        {"configProfile": "locked", "bundleId": "allowed-bundle", "cwd": str(unsafe)},
                    )
                with self.assertRaises(ValueError):
                    services.scheduler.create_thread(
                        "owner-a",
                        {"configProfile": "missing", "bundleId": "allowed-bundle", "cwd": str(safe)},
                    )

                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"configProfile": "locked", "bundleId": "allowed-bundle", "cwd": str(safe)},
                )
                self.assertEqual(thread["configProfile"], "locked")
                self.assertEqual(thread["bundleId"], "allowed-bundle")
            finally:
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
