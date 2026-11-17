"""Entry point: python -m vproc"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .api import create_app
from .config import Config, ConfigError


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        access_log=False,
        ws_ping_interval=20,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
