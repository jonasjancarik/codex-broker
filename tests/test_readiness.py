from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from codex_broker.http_api import BrokerHandler, BrokerServices
from codex_broker.sandbox_probe import PROBE_PROFILE, SandboxProbe, SandboxProbeResult
from test_broker import config_for


class ReadinessTests(unittest.TestCase):
    def test_readyz_reports_cached_sandbox_preflight_without_changing_warn_or_disabled_readiness(self) -> None:
        expectations = {
            "required": HTTPStatus.SERVICE_UNAVAILABLE,
            "warn": HTTPStatus.OK,
            "disabled": HTTPStatus.OK,
        }
        for mode, expected_status in expectations.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp_raw:
                config = replace(config_for(Path(tmp_raw)), sandbox_preflight_mode=mode)
                # Required mode now aborts startup on an initial failure. Seed a
                # healthy cached result so this test can exercise a later
                # runtime invalidation and the resulting readiness response.
                build_context = (
                    patch.object(
                        SandboxProbe,
                        "run_once",
                        return_value=SandboxProbeResult(
                            status="healthy",
                            platform=sys.platform,
                            backend="bubblewrap",
                            codex_version="codex-cli 0.153.0",
                            permission_profile=PROBE_PROFILE,
                            checked_at="2026-09-04T00:00:00Z",
                            duration_seconds=0.1,
                        ),
                    )
                    if mode == "required"
                    else nullcontext()
                )
                with build_context:
                    services = BrokerServices.build(config)
                captured: dict[str, object] = {}

                def capture_json(payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
                    captured["payload"] = payload
                    captured["status"] = status

                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = services
                handler._json = capture_json
                try:
                    if mode != "disabled":
                        services.sandbox_probe.mark_unhealthy("Bubblewrap: user namespaces are disabled")
                    handler._readyz()

                    self.assertEqual(captured["status"], expected_status)
                    payload = captured["payload"]
                    assert isinstance(payload, dict)
                    preflight = payload["sandboxPreflight"]
                    assert isinstance(preflight, dict)
                    self.assertEqual(preflight["status"], "skipped" if mode == "disabled" else "failed")
                    self.assertIn("permissionProfile", preflight)
                    self.assertIn("checkedAt", preflight)
                    self.assertNotIn("adminDiagnostic", preflight)
                    errors = payload["errors"]
                    assert isinstance(errors, list)
                    self.assertEqual(
                        "The command sandbox could not start." in errors,
                        mode == "required",
                    )
                finally:
                    services.pool.close_all()
                    services.state.close()

    def test_readyz_requires_workspace_and_bundle_roots_to_be_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            config = config_for(Path(tmp_raw))
            services = BrokerServices.build(config)
            captured: dict[str, object] = {}

            def capture_json(payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
                captured["payload"] = payload
                captured["status"] = status

            handler = BrokerHandler.__new__(BrokerHandler)
            handler.broker = services
            handler._json = capture_json
            try:
                os.chmod(config.allowed_workspace_roots[0], 0)
                os.chmod(config.allowed_bundle_roots[0], 0)
                handler._readyz()

                self.assertEqual(captured["status"], HTTPStatus.SERVICE_UNAVAILABLE)
                payload = captured["payload"]
                assert isinstance(payload, dict)
                errors = payload["errors"]
                assert isinstance(errors, list)
                self.assertTrue(any(str(error).startswith("workspace root unreadable:") for error in errors))
                self.assertTrue(any(str(error).startswith("bundle root unreadable:") for error in errors))
            finally:
                os.chmod(config.allowed_workspace_roots[0], 0o755)
                os.chmod(config.allowed_bundle_roots[0], 0o755)
                services.pool.close_all()
                services.state.close()


if __name__ == "__main__":
    unittest.main()
