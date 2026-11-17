"""Entry point: python -m vproc.ucremote (started by vproc, not by hand)."""

from __future__ import annotations

import asyncio
import logging
import sys

from .. import __version__
from ..config import Config, ConfigError
from .driver import run


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
    # ucapi pins its own loggers to DEBUG, which logs every message to the remote.
    for name in ("ucapi.api", "ucapi.entities", "ucapi.entity"):
        logging.getLogger(name).setLevel(config.log_level.upper())
    try:
        asyncio.run(
            run(
                version=__version__,
                port=config.uc_port,
                vproc_url=f"http://127.0.0.1:{config.port}",
                token=config.api_token,
                config_dir=config.data_dir / "ucremote",
            )
        )
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
