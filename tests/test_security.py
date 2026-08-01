from __future__ import annotations

import io
import threading
import unittest
from contextlib import redirect_stderr

from codex_broker.security import REDACTED, SecretSanitizer, StreamingSecretSanitizer
from codex_broker.util import json_log


class SecretSanitizerTests(unittest.TestCase):
    def test_recursive_fields_and_labelled_strings(self) -> None:
        sanitizer = SecretSanitizer()
        value = {"accessToken": "canary-access", "nested": [{"message": "Bearer canary-bearer"}], "cookie": "canary-cookie"}
        clean = sanitizer.sanitize(value)
        self.assertEqual(clean["accessToken"], REDACTED)
        self.assertEqual(clean["cookie"], REDACTED)
        self.assertEqual(clean["nested"][0]["message"], f"Bearer {REDACTED}")

    def test_registered_values_overlap_longest_first_and_are_scoped(self) -> None:
        sanitizer = SecretSanitizer()
        sanitizer.register("profile", ["canary-secret", "canary-secret-extra"])
        self.assertEqual(sanitizer.sanitize_text("x canary-secret-extra y"), f"x {REDACTED} y")
        sanitizer.replace_scope("profile", "rotated-canary")
        self.assertEqual(sanitizer.sanitize_text("canary-secret rotated-canary"), f"canary-secret {REDACTED}")
        sanitizer.remove_scope("profile")
        self.assertEqual(sanitizer.sanitize_text("rotated-canary"), "rotated-canary")

    def test_replacement_counter_counts_changes_not_calls(self) -> None:
        sanitizer = SecretSanitizer()
        sanitizer.register("profile", "exact-canary")
        self.assertEqual(sanitizer.replacement_count, 0)
        sanitizer.sanitize({"message": "ordinary"})
        self.assertEqual(sanitizer.replacement_count, 0)
        sanitizer.sanitize({"message": "exact-canary", "password": "field-canary"})
        self.assertEqual(sanitizer.replacement_count, 2)
        sanitizer.sanitize({"message": REDACTED, "password": REDACTED})
        self.assertEqual(sanitizer.replacement_count, 2)

    def test_safe_preserves_ordinary_code_and_raw_preserves_payload(self) -> None:
        ordinary = "const tokenCount = 42; // ordinary code"
        safe = SecretSanitizer()
        raw = SecretSanitizer("raw")
        payload = {"message": ordinary}
        self.assertEqual(safe.sanitize(payload), payload)
        self.assertIs(raw.sanitize(payload), payload)

    def test_raw_mode_keeps_logs_redacted(self) -> None:
        sanitizer = SecretSanitizer("raw")
        sanitizer.register("profile", "unlabelled-exact-canary")
        self.assertEqual(sanitizer.sanitize_text("access_token=canary"), "access_token=canary")
        stream = io.StringIO()
        with redirect_stderr(stream):
            json_log(
                True,
                "test",
                sanitizer=sanitizer,
                accessToken="canary",
                message="Bearer canary-bearer and unlabelled-exact-canary",
            )
        rendered = stream.getvalue()
        self.assertNotIn("canary-bearer", rendered)
        self.assertNotIn('"canary"', rendered)
        self.assertNotIn("unlabelled-exact-canary", rendered)

    def test_concurrent_registration_and_sanitization(self) -> None:
        sanitizer = SecretSanitizer()
        failures: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for iteration in range(100):
                    value = f"canary-{index}-{iteration}"
                    sanitizer.replace_scope(str(index), value)
                    self.assertNotIn(value, sanitizer.sanitize_text(value))
                    sanitizer.remove_scope(str(index))
            except BaseException as exc:  # pragma: no cover - assertion handoff
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])


class StreamingSecretSanitizerTests(unittest.TestCase):
    def _streamed(self, parts: list[str], *, registered: str | None = None) -> str:
        sanitizer = SecretSanitizer()
        if registered:
            sanitizer.register("turn", registered)
        stream = StreamingSecretSanitizer(sanitizer)
        output = "".join(stream.sanitize_delta("turn", "item", "text", part) for part in parts)
        return output + stream.flush("turn", "item", "text")

    def test_every_two_delta_split_for_credential_shapes(self) -> None:
        for candidate in ("Bearer canarybearer123", "api_key=canaryapikey123", "access_token=canaryaccess123", "registered-canary-value"):
            for split in range(1, len(candidate)):
                kwargs = {"registered": candidate} if candidate == "registered-canary-value" else {}
                rendered = self._streamed([candidate[:split], candidate[split:]], **kwargs)
                self.assertNotIn(candidate.split("=", 1)[-1].split(" ")[-1], rendered)
                self.assertIn(REDACTED, rendered)

    def test_multi_delta_and_independent_item_streams(self) -> None:
        sanitizer = SecretSanitizer()
        sanitizer.register("turn", "split-canary")
        stream = StreamingSecretSanitizer(sanitizer)
        self.assertEqual(stream.sanitize_delta("turn", "one", "text", "split-can"), "")
        self.assertEqual(stream.sanitize_delta("turn", "two", "text", "ary"), "ary")
        self.assertEqual(stream.sanitize_delta("turn", "one", "text", "ary"), "")
        self.assertEqual(stream.flush("turn", "one", "text"), REDACTED)

    def test_flushes_on_item_turn_interruption_and_failure(self) -> None:
        sanitizer = SecretSanitizer()
        sanitizer.register("turn", "flush-canary")
        stream = StreamingSecretSanitizer(sanitizer)
        stream.sanitize_delta("turn", "item", "text", "flush-canary")
        self.assertEqual(stream.flush_item("turn", "item"), {"text": REDACTED})
        stream.sanitize_delta("turn", "item", "text", "flush-canary")
        self.assertEqual(stream.flush_turn("turn"), {("item", "text"): REDACTED})
        stream.sanitize_delta("turn", "item", "text", "flush-canary")
        self.assertEqual(stream.interrupt("turn"), {("item", "text"): REDACTED})
        stream.sanitize_delta("turn", "item", "text", "flush-canary")
        self.assertEqual(stream.fail("turn"), {("item", "text"): REDACTED})

    def test_raw_mode_preserves_delta_boundaries(self) -> None:
        stream = StreamingSecretSanitizer(SecretSanitizer("raw"))
        parts = ["Bearer ", "raw-canary", " tail"]
        self.assertEqual(
            [stream.sanitize_delta("turn", "item", "text", part) for part in parts],
            parts,
        )
        self.assertEqual(stream.flush_turn("turn"), {})


if __name__ == "__main__":
    unittest.main()
