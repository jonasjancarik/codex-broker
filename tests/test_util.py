from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from codex_broker.util import clean_process_env, redact


class RedactionTests(unittest.TestCase):
    def test_clean_process_env_only_passes_explicit_secret_names(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "ESTF_ARCHIVER_API_KEY": "secret-value",
            "ESTF_ARCHIVER_API_URL": "http://archiver/api",
        }
        with patch.dict(os.environ, env, clear=True):
            clean = clean_process_env()
            self.assertNotIn("ESTF_ARCHIVER_API_KEY", clean)
            self.assertNotIn("ESTF_ARCHIVER_API_URL", clean)

            clean = clean_process_env(("ESTF_ARCHIVER_API_KEY", "ESTF_ARCHIVER_API_URL"))
            self.assertEqual(clean["ESTF_ARCHIVER_API_KEY"], "secret-value")
            self.assertEqual(clean["ESTF_ARCHIVER_API_URL"], "http://archiver/api")

    def test_redact_covers_bearer_headers_and_openai_api_keys(self) -> None:
        text = "\n".join(
            [
                "Authorization: Bearer live-token-123",
                "api_key=sk-proj-live_secret",
                "refresh_token: refresh-secret",
                '{"access_token":"json-token","cookie":"session-cookie"}',
                "'password': 'quoted-password'",
                "plain sk-test visible",
            ]
        )
        redacted = redact(text)

        self.assertIn("Authorization=<redacted>", redacted)
        self.assertIn("api_key=<redacted>", redacted)
        self.assertIn("refresh_token=<redacted>", redacted)
        self.assertIn("access_token=<redacted>", redacted)
        self.assertIn("cookie=<redacted>", redacted)
        self.assertIn("password=<redacted>", redacted)
        self.assertIn("plain <redacted> visible", redacted)
        self.assertNotIn("live-token-123", redacted)
        self.assertNotIn("sk-proj-live_secret", redacted)
        self.assertNotIn("refresh-secret", redacted)
        self.assertNotIn("json-token", redacted)
        self.assertNotIn("session-cookie", redacted)
        self.assertNotIn("quoted-password", redacted)
        self.assertNotIn("sk-test", redacted)


if __name__ == "__main__":
    unittest.main()
