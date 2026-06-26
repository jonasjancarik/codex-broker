from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from codex_broker.bundles import BundleError
from codex_broker.http_api import BrokerServices
from test_broker import config_for, wait_turn


class BundleRegistryTests(unittest.TestCase):
    def test_inline_bundle_can_be_resolved_and_used_after_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw), turn_delay=0.01), enable_inline_bundles=True, inline_bundle_max_bytes=1024)
            services = BrokerServices.build(config)
            try:
                payload = {
                    "id": "inline-ok",
                    "instructions": ["Inline instructions."],
                    "allowedPaths": [str(config.allowed_workspace_roots[0])],
                }
                accepted = services.bundles.accept_inline(payload)

                resolved = services.bundles.resolve("inline-ok")
                assert resolved is not None
                self.assertEqual(resolved.source, "inline")
                self.assertEqual(resolved.digest, accepted.digest)
                self.assertEqual(resolved.instructions, ("Inline instructions.",))

                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"bundleId": "inline-ok", "cwd": str(config.allowed_workspace_roots[0])},
                )
                turn = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "use inline bundle"}]},
                )
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"], "completed")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_inline_bundle_id_is_immutable_once_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), enable_inline_bundles=True, inline_bundle_max_bytes=1024)
            services = BrokerServices.build(config)
            try:
                payload = {
                    "id": "inline-ok",
                    "instructions": ["Original."],
                    "allowedPaths": [str(config.allowed_workspace_roots[0])],
                }
                accepted = services.bundles.accept_inline(payload)
                accepted_again = services.bundles.accept_inline(payload)
                self.assertEqual(accepted_again.digest, accepted.digest)

                with self.assertRaises(BundleError):
                    services.bundles.accept_inline(
                        {
                            "id": "inline-ok",
                            "instructions": ["Changed."],
                            "allowedPaths": [str(config.allowed_workspace_roots[0])],
                        }
                    )
            finally:
                services.pool.close_all()
                services.state.close()

    def test_rejected_inline_bundle_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), enable_inline_bundles=True, inline_bundle_max_bytes=1024)
            services = BrokerServices.build(config)
            try:
                with self.assertRaises(BundleError):
                    services.bundles.accept_inline({"id": "bad-inline", "allowedPaths": ["/etc"]})

                self.assertEqual(list(config.inline_bundle_root.iterdir()), [])
                self.assertIsNone(services.state.get_bundle_record("bad-inline"))
            finally:
                services.pool.close_all()
                services.state.close()

    def test_inline_bundle_id_cannot_shadow_mounted_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(config_for(Path(tmp_raw)), enable_inline_bundles=True, inline_bundle_max_bytes=1024)
            bundle_dir = config.allowed_bundle_roots[0] / "shared-id"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text('{"id":"shared-id","instructions":["mounted"]}', encoding="utf-8")
            services = BrokerServices.build(config)
            try:
                with self.assertRaises(BundleError):
                    services.bundles.accept_inline({"id": "shared-id", "instructions": ["inline"]})
            finally:
                services.pool.close_all()
                services.state.close()

    def test_hosted_adapter_turn_closes_per_turn_app_server_child_before_overlay_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw), turn_delay=0.01)
            bundle_dir = config.allowed_bundle_roots[0] / "hosted-bundle"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                """
                {
                  "id": "hosted-bundle",
                  "tools": [
                    {
                      "name": "host.search",
                      "type": "broker-hosted",
                      "http": { "url": "http://127.0.0.1/tool" }
                    }
                  ],
                  "allowedPaths": []
                }
                """,
                encoding="utf-8",
            )
            services = BrokerServices.build(config)
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"bundleId": "hosted-bundle", "cwd": str(config.allowed_workspace_roots[0])},
                )
                turn = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "hosted adapter"}]},
                )
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"], "completed")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if (
                        services.scheduler.metrics()["active_app_server_children"] == 0
                        and not (config.overlay_root / turn["turnId"]).exists()
                        and services.scheduler._worker_count() == 0
                    ):
                        break
                    time.sleep(0.02)
                self.assertEqual(services.scheduler.metrics()["active_app_server_children"], 0)
                self.assertFalse((config.overlay_root / turn["turnId"]).exists())
                self.assertEqual(services.scheduler._worker_count(), 0)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_bundle_turn_without_host_cwd_runs_from_broker_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw), turn_delay=0.01)
            bundle_dir = config.allowed_bundle_roots[0] / "overlay-bundle"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                """
                {
                  "id": "overlay-bundle",
                  "instructions": ["Use the broker materialized overlay."],
                  "allowedPaths": []
                }
                """,
                encoding="utf-8",
            )
            services = BrokerServices.build(config)
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"bundleId": "overlay-bundle"},
                )
                turn = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "use overlay"}]},
                )
                self.assertEqual(wait_turn(services, "owner-a", thread["threadId"], turn["turnId"])["status"], "completed")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if not (config.overlay_root / turn["turnId"]).exists() and services.scheduler._worker_count() == 0:
                        break
                    time.sleep(0.02)
                self.assertFalse((config.overlay_root / turn["turnId"]).exists())
            finally:
                services.pool.close_all()
                services.state.close()

    def test_mcp_absolute_command_path_must_be_explicitly_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            command = config.allowed_bundle_roots[0] / "tools" / "host-mcp"
            command.parent.mkdir(parents=True)
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            command.chmod(0o755)
            bundle_dir = config.allowed_bundle_roots[0] / "absolute-mcp"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                f"""
                {{
                  "id": "absolute-mcp",
                  "mcpServers": [
                    {{
                      "name": "host_mcp",
                      "command": "{command.resolve()}"
                    }}
                  ]
                }}
                """,
                encoding="utf-8",
            )

            services = BrokerServices.build(config)
            try:
                with self.assertRaises(BundleError):
                    services.bundles.resolve("absolute-mcp")
            finally:
                services.pool.close_all()
                services.state.close()

            allowed_config = replace(config, data_dir=tmp / "allowed-data", allowed_tool_commands=(str(command.resolve()),))
            allowed_services = BrokerServices.build(allowed_config)
            try:
                bundle = allowed_services.bundles.resolve("absolute-mcp")
                assert bundle is not None
                self.assertEqual(bundle.mcp_servers[0].command, str(command.resolve()))
            finally:
                allowed_services.pool.close_all()
                allowed_services.state.close()

    def test_hosted_tool_network_policy_is_validated_and_materialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            bundle_dir = config.allowed_bundle_roots[0] / "network-policy"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                """
                {
                  "id": "network-policy",
                  "tools": [
                    {
                      "name": "host.search",
                      "type": "broker-hosted",
                      "networkPolicy": { "mode": "host-allowlist" },
                      "http": { "url": "http://127.0.0.1/tool" }
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )
            services = BrokerServices.build(config)
            try:
                bundle = services.bundles.resolve("network-policy")
                assert bundle is not None
                self.assertEqual(bundle.hosted_tools[0].network_policy["mode"], "host-allowlist")
                self.assertEqual(bundle.hosted_tools[0].network_policy["matchedPrefix"], "http://127.0.0.1")

                overlay = services.bundles.materialize(bundle, "turn_policy")
                adapter_config = (overlay / "tool-adapters.json").read_text(encoding="utf-8")
                self.assertIn('"networkPolicy"', adapter_config)
                self.assertIn('"matchedPrefix": "http://127.0.0.1"', adapter_config)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_hosted_tool_rejects_unsupported_network_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            bundle_dir = config.allowed_bundle_roots[0] / "bad-network-policy"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                """
                {
                  "id": "bad-network-policy",
                  "tools": [
                    {
                      "name": "host.search",
                      "type": "broker-hosted",
                      "networkPolicy": { "mode": "unrestricted" },
                      "http": { "url": "http://127.0.0.1/tool" }
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )
            services = BrokerServices.build(config)
            try:
                with self.assertRaises(BundleError):
                    services.bundles.resolve("bad-network-policy")
            finally:
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
