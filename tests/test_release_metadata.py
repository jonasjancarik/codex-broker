from __future__ import annotations

import re
import unittest
from pathlib import Path


class ReleaseMetadataTests(unittest.TestCase):
    def test_documented_codex_default_matches_dockerfile(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        dockerfile = (repository / "Dockerfile").read_text(encoding="utf-8")
        match = re.search(r"^ARG CODEX_VERSION=([^\s]+)$", dockerfile, re.MULTILINE)
        self.assertIsNotNone(match)
        assert match is not None
        version = match.group(1)

        for relative_path in (
            "fern/docs/pages/operations/deployment.mdx",
            "fern/docs/pages/operations/configuration-reference.mdx",
        ):
            with self.subTest(relative_path=relative_path):
                documentation = (repository / relative_path).read_text(encoding="utf-8")
                self.assertIn(f"| `CODEX_VERSION` | `{version}` |", documentation)


if __name__ == "__main__":
    unittest.main()
