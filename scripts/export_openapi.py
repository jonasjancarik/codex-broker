from __future__ import annotations

import json
from pathlib import Path

from codex_broker.http_api import openapi_document


def main() -> None:
    output_path = Path("fern/openapi/openapi.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
