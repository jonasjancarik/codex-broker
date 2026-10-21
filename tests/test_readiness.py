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


class ConcurrentReadinessTests(unittest.TestCase):
    def test_concurrent_readiness_requests_do_not_remove_each_others_probe(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as raw:
            services = BrokerServices.build(config_for(Path(raw)))
            barrier = threading.Barrier(2)
            original_write = Path.write_text
            def simultaneous_legacy_write(path, *args, **kwargs):
                result = original_write(path, *args, **kwargs)
                if path.name == ".write-probe":
                    barrier.wait(timeout=3)
                return result
            def readiness(_):
                handler = BrokerHandler.__new__(BrokerHandler)
                handler.broker = services
                captured = {}
                handler._json = lambda payload, status: captured.update(payload=payload, status=status)
                handler._readyz()
                return captured["payload"]
            try:
                with patch.object(Path, "write_text", simultaneous_legacy_write):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        reports = list(executor.map(readiness, range(2)))
                self.assertEqual([report["status"] for report in reports], ["ready", "ready"])
                self.assertEqual(list(services.config.auth_root.glob(".write-probe*")), [])
            finally:
                services.pool.close_all()
                services.state.close()

    def test_client_disconnect_is_not_retried_as_internal_server_error(self) -> None:
        from types import SimpleNamespace
        for failure in (BrokenPipeError, ConnectionResetError):
            with self.subTest(failure=failure):
                handler = BrokerHandler.__new__(BrokerHandler)
                handler.path = "/healthz"
                handler.headers = {}
                handler.close_connection = False
                metrics = []
                handler.broker = SimpleNamespace(
                    config=SimpleNamespace(json_logs=False), sanitizer=None,
                    scheduler=SimpleNamespace(note_http_request=lambda *args: metrics.append(args)),
                )
                responses = []
                def disconnect(payload, status=HTTPStatus.OK):
                    responses.append(status)
                    handler._metric_status = status
                    raise failure("client disconnected")
                handler._json = disconnect
                handler._dispatch("GET")
                self.assertEqual(responses, [HTTPStatus.OK])
                self.assertTrue(handler.close_connection)
                self.assertEqual(metrics[0][1], HTTPStatus.OK)


if __name__ == "__main__":
    unittest.main()
