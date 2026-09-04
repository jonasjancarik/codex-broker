from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_broker.auth import render_managed_codex_config
from codex_broker.config import BrokerConfig
from codex_broker.runtime_errors import SANDBOX_UNAVAILABLE, classify_runtime_error
from codex_broker.sandbox_probe import PROBE_PROFILE, SandboxProbe, SandboxProbeResult
from codex_broker.services import BrokerServices, SandboxPreflightError


ROOT = Path(__file__).resolve().parent
FAKE_CODEX = ROOT / "fake_codex.py"


def config_for(tmp: Path, *, mode: str = "required") -> BrokerConfig:
    workspace = tmp / "workspace"
    bundles = tmp / "bundles"
    workspace.mkdir()
    bundles.mkdir()
    return BrokerConfig(
        host="127.0.0.1", port=0, data_dir=tmp / "data", internal_key="key",
        allow_unauthenticated=False, owner_hash_secret="secret", allowed_workspace_roots=(workspace,),
        allowed_bundle_roots=(bundles,), max_active_turns=0, pool_idle_ttl_seconds=1,
        codex_command=(sys.executable, str(FAKE_CODEX)), allowed_tool_commands=("python",),
        allowed_hosted_tool_url_prefixes=(), credential_store="file", request_timeout_seconds=1,
        host_response_timeout_seconds=1, turn_timeout_seconds=1, enable_inline_bundles=False,
        inline_bundle_max_bytes=1, debug_raw_events=False, raw_event_retention_seconds=1,
        json_logs=False, shutdown_mode="interrupt", shutdown_drain_timeout_seconds=1,
        sandbox_preflight_mode=mode,
    )


class SandboxProbeTests(unittest.TestCase):
    def test_required_failed_preflight_aborts_service_startup(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch("codex_broker.services.SandboxProbe") as probe_type:
            config = config_for(Path(raw))
            probe_type.return_value.run_once.return_value = SandboxProbeResult(
                status="failed",
                platform="linux",
                backend="bubblewrap",
                codex_version="codex-cli 0.153.0",
                permission_profile=PROBE_PROFILE,
                checked_at="2026-09-04T00:00:00Z",
                duration_seconds=0.1,
                admin_diagnostic="Bubblewrap: user namespaces are disabled",
            )

            with self.assertRaisesRegex(SandboxPreflightError, "status=failed") as raised:
                BrokerServices.build(config)

            self.assertIn("user namespaces are disabled", str(raised.exception))
            probe_type.return_value.run_once.assert_called_once_with()
            self.assertFalse(config.state_db_path.exists())

    def test_required_healthy_preflight_allows_service_startup(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch("codex_broker.services.SandboxProbe") as probe_type:
            config = config_for(Path(raw))
            probe_type.return_value.run_once.return_value = SandboxProbeResult(
                status="healthy",
                platform="linux",
                backend="bubblewrap",
                codex_version="codex-cli 0.153.0",
                permission_profile=PROBE_PROFILE,
                checked_at="2026-09-04T00:00:00Z",
                duration_seconds=0.1,
            )
            services = BrokerServices.build(config)
            try:
                self.assertIs(services.sandbox_probe, probe_type.return_value)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_warn_and_disabled_modes_allow_failed_preflight(self) -> None:
        for mode in ("warn", "disabled"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw, patch(
                "codex_broker.services.SandboxProbe"
            ) as probe_type:
                config = config_for(Path(raw), mode=mode)
                probe_type.return_value.run_once.return_value = SandboxProbeResult(
                    status="failed",
                    platform="linux",
                    backend="bubblewrap",
                    codex_version=None,
                    permission_profile=PROBE_PROFILE,
                    checked_at="2026-09-04T00:00:00Z",
                    duration_seconds=0.1,
                    admin_diagnostic="probe failed",
                )
                services = BrokerServices.build(config)
                try:
                    self.assertIs(services.sandbox_probe, probe_type.return_value)
                finally:
                    services.pool.close_all()
                    services.state.close()

    def test_required_unhealthy_preflight_finalizes_managed_turn_before_model_contact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = replace(
                config_for(Path(raw)),
                config_profiles={"managed": {}},
            )
            with patch.object(SandboxProbe, "run_once", return_value=self._healthy_result()):
                services = BrokerServices.build(config)
            try:
                services.sandbox_probe.mark_unhealthy("Bubblewrap: user namespaces are disabled")
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"cwd": str(config.allowed_workspace_roots[0]), "configProfile": "managed"},
                )
                with patch.object(services.pool, "get", side_effect=AssertionError("model process must not start")):
                    started = services.scheduler.start_turn(
                        "owner-a",
                        thread["threadId"],
                        {"input": [{"type": "text", "text": "do not contact Codex"}]},
                    )
                    turn = self._wait_for_terminal_turn(services, "owner-a", thread["threadId"], started["turnId"])

                self.assertEqual(turn["status"], "failed")
                self.assertEqual(turn["errorCode"], SANDBOX_UNAVAILABLE)
                self.assertEqual(services.scheduler.metrics()["turns_rejected_sandbox_unavailable"], 1)
            finally:
                services.pool.close_all()
                services.state.close()

    def test_authorized_danger_full_access_bypasses_required_preflight_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config = replace(
                config_for(Path(raw)),
                config_profiles={"danger": {"sandbox": "danger-full-access"}},
            )
            with patch.object(SandboxProbe, "run_once", return_value=self._healthy_result()):
                services = BrokerServices.build(config)
            try:
                services.sandbox_probe.mark_unhealthy("Bubblewrap: user namespaces are disabled")
                thread = services.scheduler.create_thread(
                    "owner-a",
                    {"cwd": str(config.allowed_workspace_roots[0]), "configProfile": "danger"},
                )
                started = services.scheduler.start_turn(
                    "owner-a",
                    thread["threadId"],
                    {"input": [{"type": "text", "text": "authorized bypass"}]},
                    danger_full_access_authorized=True,
                )
                turn = self._wait_for_terminal_turn(services, "owner-a", thread["threadId"], started["turnId"])

                self.assertEqual(turn["status"], "completed")
                self.assertNotEqual(turn["errorCode"], SANDBOX_UNAVAILABLE)
                self.assertEqual(services.scheduler.metrics()["turns_rejected_sandbox_unavailable"], 0)
            finally:
                services.pool.close_all()
                services.state.close()

    def _wait_for_terminal_turn(
        self,
        services: BrokerServices,
        owner_id: str,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, object]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            turn = services.scheduler.get_turn(owner_id, thread_id, turn_id)
            if turn["status"] in {"completed", "failed", "timed_out", "interrupted"}:
                return turn
            time.sleep(0.01)
        self.fail(f"turn {turn_id} did not finish")

    @staticmethod
    def _healthy_result() -> SandboxProbeResult:
        return SandboxProbeResult(
            status="healthy",
            platform=sys.platform,
            backend="bubblewrap",
            codex_version="codex-cli 0.153.0",
            permission_profile=PROBE_PROFILE,
            checked_at="2026-09-04T00:00:00Z",
            duration_seconds=0.1,
        )

    def test_linux_probe_succeeds_and_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            probe = SandboxProbe(config_for(Path(raw)), platform_name="linux")
            result = probe.run_once()
            self.assertEqual(result.status, "healthy")
            self.assertEqual(result.permission_profile, PROBE_PROFILE)
            self.assertEqual(result.public()["status"], "healthy")
            self.assertNotIn("adminDiagnostic", result.public())
            self.assertIs(probe.run_once(), result)

    def test_failed_linux_probe_does_not_touch_production_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ,
            {"FAKE_CODEX_PROBE_CANARY_READABLE": "1"},
        ):
            config = config_for(Path(raw))
            production_marker = config.data_dir / "production-marker"
            production_marker.parent.mkdir(parents=True)
            production_marker.write_bytes(b"production-state")

            def snapshot() -> tuple[tuple[str, str, int, bytes | None], ...]:
                if not config.data_dir.exists():
                    return ()
                entries: list[tuple[str, str, int, bytes | None]] = []
                for path in sorted(config.data_dir.rglob("*")):
                    relative = str(path.relative_to(config.data_dir))
                    mode = path.stat().st_mode & 0o777
                    if path.is_dir():
                        entries.append((relative, "directory", mode, None))
                    else:
                        entries.append((relative, "file", mode, path.read_bytes()))
                return tuple(entries)

            before = snapshot()
            result = SandboxProbe(config, platform_name="linux").run_once()

            self.assertEqual(result.status, "failed")
            self.assertIn("protected canary", result.admin_diagnostic or "")
            self.assertEqual(snapshot(), before)
            self.assertEqual(production_marker.read_bytes(), b"production-state")

    def test_probe_keeps_broker_state_denies_under_configured_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = replace(config_for(root), data_dir=root / "configured-data")
            fixture_state = root / "broker-data" / "state"
            probe_config = replace(
                config,
                allowed_bundle_roots=(root / "bundle-fixture",),
                sandbox_deny_paths=(root / "workspace" / "control-plane",),
            )
            rendered = render_managed_codex_config(probe_config)

            self.assertIn(json.dumps(str((root / "configured-data" / "state").resolve())), rendered)
            self.assertNotIn(json.dumps(str(fixture_state.resolve())), rendered)

    def test_readable_canary_fails_without_leaking_contents(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"FAKE_CODEX_PROBE_CANARY_READABLE": "1"}):
            result = SandboxProbe(config_for(Path(raw)), platform_name="linux").run_once()
        self.assertEqual(result.status, "failed")
        self.assertIn("protected canary", result.admin_diagnostic or "")
        self.assertNotIn("sandbox-probe-canary", result.admin_diagnostic or "")

    def test_attached_skill_and_sibling_job_canaries_fail_without_path_leaks(self) -> None:
        cases = (
            ("FAKE_CODEX_PROBE_SKILL_UNREADABLE", "attached skill overlay"),
            ("FAKE_CODEX_PROBE_SKILL_SNAPSHOT_MUTABLE", "attached skill snapshot"),
            ("FAKE_CODEX_PROBE_SNAPSHOT_CONTENT_CHANGED", "attached skill snapshot content changed"),
            ("FAKE_CODEX_PROBE_SNAPSHOT_MODE_CHANGED", "attached skill snapshot mode changed"),
            ("FAKE_CODEX_PROBE_SKILL_CONTENT_CHANGED", "mounted skill target content changed"),
            ("FAKE_CODEX_PROBE_SKILL_MODE_CHANGED", "mounted skill target mode changed"),
            ("FAKE_CODEX_PROBE_SIBLING_READABLE", "sibling job"),
        )
        for environment, capability in cases:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as raw, patch.dict(
                os.environ,
                {environment: "1"},
            ):
                result = SandboxProbe(config_for(Path(raw)), platform_name="linux").run_once()
            self.assertEqual(result.status, "failed")
            self.assertIn(capability, result.admin_diagnostic or "")
            self.assertNotIn(str(Path(raw)), result.admin_diagnostic or "")

    def test_read_only_profile_must_reject_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ,
            {"FAKE_CODEX_PROBE_READ_ONLY_WRITABLE": "1"},
        ):
            result = SandboxProbe(config_for(Path(raw)), platform_name="linux").run_once()
        self.assertEqual(result.status, "failed")
        self.assertIn("read-only sandbox could write", result.admin_diagnostic or "")

    def test_start_failure_and_timeout_reap_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"FAKE_CODEX_PROBE_START_FAILURE": "1"}):
            failed = SandboxProbe(config_for(Path(raw)), platform_name="linux").run_once()
        self.assertEqual(failed.status, "failed")
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"FAKE_CODEX_PROBE_HANG": "1"}):
            timed = SandboxProbe(config_for(Path(raw)), platform_name="linux", request_timeout_seconds=0.02).run_once()
        self.assertEqual(timed.status, "failed")
        self.assertIn("timed out", timed.admin_diagnostic or "")

    def test_disabled_and_non_linux_results(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(SandboxProbe(config_for(Path(raw), mode="disabled"), platform_name="linux").run_once().status, "skipped")
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(SandboxProbe(config_for(Path(raw)), platform_name="darwin").run_once().status, "unsupported")

    def test_marks_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            probe = SandboxProbe(config_for(Path(raw)), platform_name="linux")
            probe.run_once()
            self.assertEqual(probe.mark_unhealthy("Bubblewrap: permission denied").status, "failed")

    def test_exact_bubblewrap_classification(self) -> None:
        self.assertEqual(classify_runtime_error("Bubblewrap: user namespaces are disabled").code, SANDBOX_UNAVAILABLE)
        self.assertEqual(classify_runtime_error("Bubblewrap failed to start").code, "codex_runtime_error")
        self.assertEqual(classify_runtime_error("user namespace operation not permitted").code, "codex_runtime_error")


if __name__ == "__main__":
    unittest.main()
