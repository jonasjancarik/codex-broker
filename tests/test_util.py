from __future__ import annotations

import unittest

from codex_broker.util import redact


class RedactionTests(unittest.TestCase):
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
