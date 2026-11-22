"""Entry point: python -m vproc

Runs the web server and processor until stopped. When the settings page applies
new settings, the server shuts down cleanly and starts again with them, inside
the same container: no Docker restart needed.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .api import create_app
from .config import Config, ConfigError
from .diagnostics import LOG_FORMAT, keep_recent_log
from .service_settings import ServiceSettings

log = logging.getLogger("vproc")


def main() -> int:
    try:
        base = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(format=LOG_FORMAT, stream=sys.stdout)
    keep_recent_log()  # for the diagnostics bundle

    while True:
        settings = ServiceSettings(base.data_dir)
        config, warning = settings.config()
        logging.getLogger().setLevel(config.log_level.upper())
        if warning:
            log.error(warning)

        restart = False
        server: uvicorn.Server | None = None

        def request_restart() -> None:
            nonlocal restart
            restart = True
            if server:
                server.should_exit = True

        app = create_app(config, settings=settings, request_restart=request_restart, startup_warning=warning)
        server = uvicorn.Server(uvicorn.Config(
            app, host=config.host, port=config.port, log_level=config.log_level.lower(),
            access_log=False, ws_ping_interval=20, timeout_graceful_shutdown=5,
        ))
        server.run()
        if not restart:
            return 0
        log.info("restarting with the new settings")


if __name__ == "__main__":
    sys.exit(main())
