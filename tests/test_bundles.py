from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_broker import bundles as bundles_module
from codex_broker.bundles import BundleError, SkillSnapshotLimits, materialized_skill_path
from codex_broker.http_api import BrokerServices
from test_broker import config_for, wait_turn


class BundleRegistryTests(unittest.TestCase):
    def test_skill_file_symlink_cannot_escape_an_allowed_bundle_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            skill_dir = config.allowed_bundle_roots[0] / "linked-skill"
            skill_dir.mkdir()
            outside_skill = tmp / "outside-skill.md"
            outside_skill.write_text("outside", encoding="utf-8")
            (skill_dir / "SKILL.md").symlink_to(outside_skill)
            bundle_dir = config.allowed_bundle_roots[0] / "linked-bundle"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                '{"id":"linked-bundle","skills":[{"name":"linked","source":{"type":"mount","path":"'
                + str(skill_dir)
                + '"}}]}',
                encoding="utf-8",
            )
            services = BrokerServices.build(config)
            try:
                with self.assertRaisesRegex(BundleError, "SKILL.md is outside allowed bundle roots"):
                    services.bundles.resolve("linked-bundle")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_skill_snapshot_is_per_turn_digest_verified_and_never_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            skill_dir = config.allowed_bundle_roots[0] / "materialized-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("skill-v1", encoding="utf-8")
            (skill_dir / "references").mkdir()
            (skill_dir / "references" / "guide.md").write_text("guide-v1", encoding="utf-8")
            bundle_dir = config.allowed_bundle_roots[0] / "materialized-bundle"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                '{"id":"materialized-bundle","skills":[{"name":"materialized","source":{"type":"mount","path":"'
                + str(skill_dir)
                + '"}}]}',
                encoding="utf-8",
            )
            services = BrokerServices.build(config)
            try:
                bundle = services.bundles.resolve("materialized-bundle")
                assert bundle is not None
                first = services.bundles.materialize(bundle, "first-turn")
                assert first is not None
                first_skill = materialized_skill_path(first, bundle.skills[0])
                self.assertFalse(first_skill.parent.is_symlink())
                self.assertEqual(first_skill.read_text(encoding="utf-8"), "skill-v1")
                self.assertEqual((first_skill.parent / "references" / "guide.md").read_text(encoding="utf-8"), "guide-v1")
                self.assertNotEqual(first_skill.stat().st_ino, (skill_dir / "SKILL.md").stat().st_ino)

                (skill_dir / "SKILL.md").write_text("skill-v2", encoding="utf-8")
                second = services.bundles.materialize(bundle, "second-turn")
                assert second is not None
                second_skill = materialized_skill_path(second, bundle.skills[0])
                self.assertEqual(first_skill.read_text(encoding="utf-8"), "skill-v1")
                self.assertEqual(second_skill.read_text(encoding="utf-8"), "skill-v2")
                self.assertNotEqual(first_skill.stat().st_ino, second_skill.stat().st_ino)
                self.assertFalse(first_skill.parent.is_symlink())
                self.assertFalse(second_skill.parent.is_symlink())

                services.bundles.cleanup_overlay("first-turn")
                self.assertFalse(first.exists())
                self.assertTrue(second.exists())
            finally:
                services.pool.close_all()
                services.state.close()

    def test_skill_snapshot_rejects_symlinked_supporting_files_and_removes_partial_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            skill_dir = config.allowed_bundle_roots[0] / "linked-supporting-file"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
            outside = Path(tmp_raw) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (skill_dir / "reference.txt").symlink_to(outside)
            bundle_dir = config.allowed_bundle_roots[0] / "linked-supporting-bundle"
            bundle_dir.mkdir()
            (bundle_dir / "bundle.json").write_text(
                '{"id":"linked-supporting-bundle","skills":[{"name":"linked","source":{"type":"mount","path":"'
                + str(skill_dir)
                + '"}}]}',
                encoding="utf-8",
            )
            services = BrokerServices.build(config)
            try:
                bundle = services.bundles.resolve("linked-supporting-bundle")
                assert bundle is not None
                with self.assertRaisesRegex(BundleError, "symbolic link"):
                    services.bundles.materialize(bundle, "partial-overlay")
                self.assertFalse((config.overlay_root / "partial-overlay").exists())
            finally:
                services.pool.close_all()
                services.state.close()

    @unittest.skipUnless(os.name == "posix", "descriptor-relative snapshots require POSIX")
    def test_skill_snapshot_rejects_entry_swapped_to_symlink_after_enumeration_without_leaking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            skill_dir, bundle = self._bundle_with_skill(config, "raced-symlink")
            (skill_dir / "checked.txt").write_text("safe", encoding="utf-8")
            outside = Path(tmp_raw) / "outside-secret.txt"
            outside.write_text("outside-snapshot-secret", encoding="utf-8")
            services = BrokerServices.build(config)
            try:
                resolved = services.bundles.resolve(bundle)
                assert resolved is not None
                original_open_child = bundles_module._open_child
                swapped = False

                def swap_after_enumeration(parent_fd: int, name: str, relative: str) -> int:
                    nonlocal swapped
                    if name == "checked.txt" and not swapped:
                        swapped = True
                        (skill_dir / name).unlink()
                        (skill_dir / name).symlink_to(outside)
                    return original_open_child(parent_fd, name, relative)

                with patch.object(bundles_module, "_open_child", side_effect=swap_after_enumeration):
                    with self.assertRaisesRegex(BundleError, "symbolic link") as raised:
                        services.bundles.materialize(resolved, "raced-overlay")
                self.assertTrue(swapped)
                self.assertNotIn("outside-snapshot-secret", str(raised.exception))
                self.assertNotIn(str(outside), str(raised.exception))
                self.assertFalse((config.overlay_root / "raced-overlay").exists())
            finally:
                services.pool.close_all()
                services.state.close()

    @unittest.skipUnless(os.name == "posix", "descriptor-relative snapshots require POSIX")
    def test_skill_snapshot_rejects_fifo_swapped_after_enumeration_without_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            skill_dir, bundle = self._bundle_with_skill(config, "raced-fifo")
            (skill_dir / "checked.txt").write_text("safe", encoding="utf-8")
            services = BrokerServices.build(config)
            try:
                resolved = services.bundles.resolve(bundle)
                assert resolved is not None
                original_open_child = bundles_module._open_child

                def swap_after_enumeration(parent_fd: int, name: str, relative: str) -> int:
                    if name == "checked.txt":
                        (skill_dir / name).unlink()
                        os.mkfifo(skill_dir / name)
                    return original_open_child(parent_fd, name, relative)

                started = time.monotonic()
                with patch.object(bundles_module, "_open_child", side_effect=swap_after_enumeration):
                    with self.assertRaisesRegex(BundleError, "unsupported entry"):
                        services.bundles.materialize(resolved, "fifo-overlay")
                self.assertLess(time.monotonic() - started, 1)
                self.assertFalse((config.overlay_root / "fifo-overlay").exists())
            finally:
                services.pool.close_all()
                services.state.close()

    def test_skill_snapshot_budgets_fail_and_remove_partial_overlays(self) -> None:
        cases = (
            (
                "file-count",
                SkillSnapshotLimits(1, 10, 10, 1_024, 1_024),
                lambda skill: (skill / "extra.txt").write_text("extra", encoding="utf-8"),
                "file count",
            ),
            (
                "depth",
                SkillSnapshotLimits(10, 10, 1, 1_024, 1_024),
                lambda skill: (
                    (skill / "nested").mkdir(),
                    (skill / "nested" / "note.txt").write_text("note", encoding="utf-8"),
                ),
                "depth",
            ),
            (
                "total-bytes",
                SkillSnapshotLimits(10, 10, 10, 5, 5),
                lambda skill: (skill / "extra.txt").write_text("x", encoding="utf-8"),
                "total snapshot byte limit",
            ),
            (
                "file-bytes",
                SkillSnapshotLimits(10, 10, 10, 1_024, 1),
                lambda skill: None,
                "per-file snapshot byte limit",
            ),
        )
        for name, limits, prepare, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp_raw:
                config = config_for(Path(tmp_raw))
                skill_dir, bundle_id = self._bundle_with_skill(config, f"budget-{name}")
                prepare(skill_dir)
                services = BrokerServices.build(config)
                try:
                    services.bundles.skill_snapshot_limits = limits
                    bundle = services.bundles.resolve(bundle_id)
                    assert bundle is not None
                    with self.assertRaisesRegex(BundleError, message):
                        services.bundles.materialize(bundle, f"budget-{name}")
                    self.assertFalse((config.overlay_root / f"budget-{name}").exists())
                finally:
                    services.pool.close_all()
                    services.state.close()

    def test_skill_snapshot_provenance_is_audited_before_turn_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw), turn_delay=0.01)
            skill_dir, bundle_id = self._bundle_with_skill(config, "audited-snapshot")
            (skill_dir / "SKILL.md").write_text("private skill body", encoding="utf-8")
            services = BrokerServices.build(config)
            try:
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"bundleId": bundle_id, "cwd": str(config.allowed_workspace_roots[0])},
                )
                started = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "use the attached skill"}]},
                )
                completed = wait_turn(services, "owner-a", thread["threadId"], started["turnId"])
                self.assertEqual(completed["status"], "completed")
                audits = services.state.list_audit_logs(
                    services.auth.hash_owner("owner-a"),
                    action="security.bundle_skill_snapshot",
                    thread_id=thread["threadId"],
                    turn_id=started["turnId"],
                )
                self.assertEqual(len(audits), 1)
                payload = audits[0]["payload"]
                self.assertEqual(payload["bundleDigest"], services.state.get_turn(
                    services.auth.hash_owner("owner-a"), thread["threadId"], started["turnId"]
                )["bundle_digest"])
                self.assertEqual(payload["skills"][0]["name"], "audited-snapshot")
                self.assertEqual(payload["skills"][0]["sourcePath"], str(skill_dir.resolve()))
                self.assertRegex(payload["skills"][0]["sourceIdentity"], r"^posix:\d+:\d+$")
                self.assertRegex(payload["skills"][0]["snapshotSha256"], r"^[0-9a-f]{64}$")
                self.assertNotIn("private skill body", str(payload))
            finally:
                services.pool.close_all()
                services.state.close()

    @staticmethod
    def _bundle_with_skill(config: object, bundle_id: str) -> tuple[Path, str]:
        bundle_root = config.allowed_bundle_roots[0]  # type: ignore[attr-defined]
        skill_dir = bundle_root / bundle_id / "skills" / "attached"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("skill", encoding="utf-8")
        bundle_dir = bundle_root / bundle_id
        (bundle_dir / "bundle.json").write_text(
            '{"id":"' + bundle_id + '","skills":[{"name":"' + bundle_id + '","source":{"type":"mount","path":"'
            + str(skill_dir)
            + '"}}]}',
            encoding="utf-8",
        )
        return skill_dir, bundle_id

    def test_bundle_lookup_cannot_escape_mount_or_return_a_different_manifest_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            config = config_for(tmp)
            (tmp / "outside.json").write_text('{"id":"outside"}', encoding="utf-8")
            (config.allowed_bundle_roots[0] / "requested.json").write_text('{"id":"different"}', encoding="utf-8")
            services = BrokerServices.build(config)
            try:
                with self.assertRaises(BundleError):
                    services.bundles.resolve("../outside")
                with self.assertRaises(BundleError):
                    services.bundles.resolve("requested")
            finally:
                services.pool.close_all()
                services.state.close()

    def test_hosted_tools_require_an_endpoint_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = replace(
                config_for(Path(tmp_raw)),
                enable_inline_bundles=True,
                allowed_hosted_tool_url_prefixes=(),
            )
            services = BrokerServices.build(config)
            try:
                with self.assertRaises(BundleError):
                    services.bundles.accept_inline(
                        {
                            "id": "hosted",
                            "tools": [{"name": "host.call", "http": {"url": "http://127.0.0.1/tool"}}],
                        }
                    )
            finally:
                services.pool.close_all()
                services.state.close()

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
                secret_bundle_dir = config.allowed_bundle_roots[0] / "secret-policy"
                secret_bundle_dir.mkdir()
                (secret_bundle_dir / "bundle.json").write_text(
                    """
                    {
                      "id": "secret-policy",
                      "tools": [{
                        "name": "host.secret",
                        "http": {
                          "url": "http://127.0.0.1/tool",
                          "headers": {"X-Host-Tool-Key": "env:CODEX_HOST_TOOL_KEY"}
                        }
                      }]
                    }
                    """,
                    encoding="utf-8",
                )
                secret_bundle = services.bundles.resolve("secret-policy")
                assert secret_bundle is not None
                secret_overlay = services.bundles.materialize(secret_bundle, "turn_secret")
                servers = services.bundles.mcp_servers_for_bundle(secret_bundle, secret_overlay)
                self.assertEqual(servers[-1].env, {"CODEX_HOST_TOOL_KEY": "env:CODEX_HOST_TOOL_KEY"})
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
