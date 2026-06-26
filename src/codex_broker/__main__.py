from __future__ import annotations

from .config import BrokerConfig
from .http_api import serve


def main() -> None:
    serve(BrokerConfig.from_env())


if __name__ == "__main__":
    main()
