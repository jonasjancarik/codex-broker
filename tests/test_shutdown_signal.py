from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


class ShutdownSignalTests(unittest.TestCase):
    def test_sigterm_exits_cleanly_and_closes_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            env = dict(os.environ)
            env.update(
                {
                    "CODEX_BROKER_HOST": "127.0.0.1",
                    "CODEX_BROKER_PORT": str(port),
                    "CODEX_BROKER_DATA_DIR": str(tmp / "data"),
                    "CODEX_BROKER_INTERNAL_KEY": "test-key",
                    "CODEX_BROKER_ALLOWED_WORKSPACE_ROOTS": str(tmp),
                    "CODEX_BROKER_ALLOWED_BUNDLE_ROOTS": str(tmp),
                    "CODEX_BIN": f"{sys.executable} tests/fake_codex.py",
                    "CODEX_BROKER_JSON_LOGS": "false",
                }
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "codex_broker"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.2) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Broker did not become healthy before SIGTERM test deadline.")

                process.terminate()
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
                self.assertTrue((tmp / "data" / "state" / "broker.sqlite").is_file())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
