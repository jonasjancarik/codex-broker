from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_broker.bundles import BundleError
from codex_broker.config import BrokerConfig
from codex_broker.http_api import BrokerServices
from test_broker import config_for


class ConfigProfileTests(unittest.TestCase):
    def test_config_profiles_load_from_env_json(self) -> None:
        payload = {
            "review": {
                "model": "gpt-5",
                "approvalPolicy": "on-request",
                "allowedWorkspaceRoots": ["/workspaces/review"],
                "enabledBundles": ["review-bundle"],
            }
        }
        with patch.dict(os.environ, {"CODEX_BROKER_CONFIG_PROFILES_JSON": json.dumps(payload)}, clear=True):
            config = BrokerConfig.from_env()

        self.assertEqual(config.config_profiles["review"]["model"], "gpt-5")
        self.assertEqual(config.config_profiles["review"]["enabledBundles"], ["review-bundle"])

    def test_passthrough_env_loads_from_env_csv(self) -> None:
        with patch.dict(
            os.environ,
            {"CODEX_BROKER_PASSTHROUGH_ENV": "ESTF_ARCHIVER_API_URL, ESTF_ARCHIVER_API_KEY"},
            clear=True,
        ):
            config = BrokerConfig.from_env()

        self.assertEqual(
            config.codex_passthrough_env,
            ("ESTF_ARCHIVER_API_URL", "ESTF_ARCHIVER_API_KEY"),
        )

    def test_config_profile_defaults_and_request_overrides_feed_app_server_params(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw)),
                config_profiles={
                    "review": {
                        "model": "gpt-5",
                        "approvalPolicy": "on-request",
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
                self.assertEqual(thread_params["approvalPolicy"], "on-request")
                self.assertEqual(thread_params["sandbox"], "workspace-write")
                self.assertEqual(thread_params["personality"], "concise")

                turn_params = services.scheduler._turn_params(
                    "codex_thread_1",
                    [{"type": "text", "text": "review"}],
                    {"runtime": {"reasoningEffort": "medium", "reasoningSummary": "concise"}},
                    profile,
                )
                self.assertEqual(turn_params["model"], "gpt-5")
                self.assertEqual(turn_params["serviceTier"], "flex")
                self.assertEqual(turn_params["effort"], "medium")
                self.assertEqual(turn_params["personality"], "concise")
                self.assertEqual(turn_params["summary"], "concise")
                self.assertEqual(turn_params["outputSchema"], profile["outputSchema"])

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
