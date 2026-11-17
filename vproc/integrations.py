"""Optional integrations, switched on and off from the web UI.

Currently just the Unfolded Circle Remote driver. It runs as a child process so
that switching it off really stops it: no open port, no network announcements.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger("vproc.integrations")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class RemoteIntegration:
    """Supervises `python -m vproc.ucremote` while the integration is enabled."""

    def __init__(self, config: Config, on_change: Callable[[], None] = lambda: None,
                 command: list[str] | None = None):
        self.config = config
        self._on_change = on_change
        self._command = command or [sys.executable, "-m", "vproc.ucremote"]
        self._settings_path = config.data_dir / "integrations.json"
        self.enabled = self._load()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ settings

    def _load(self) -> bool:
        try:
            return bool(json.loads(self._settings_path.read_text()).get("uc_remote", {}).get("enabled", False))
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); the remote integration stays off", self._settings_path, exc)
            return False

    def _save(self) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"uc_remote": {"enabled": self.enabled}}, indent=2))
        os.replace(tmp, self._settings_path)

    # ------------------------------------------------------------------ control

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "port": self.config.uc_port,
            "error": self._last_error,
        }

    async def start(self) -> None:
        if self.enabled and not self._task:
            self._task = asyncio.create_task(self._supervise(), name="uc-remote")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._terminate()

    async def set_enabled(self, enabled: bool) -> None:
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self._save()
        self._last_error = None
        if enabled:
            await self.start()
        else:
            await self.stop()
        log.info("Unfolded Circle remote integration %s", "enabled" if enabled else "disabled")
        self._on_change()

    # ------------------------------------------------------------------ process

    async def _supervise(self) -> None:
        delay = 1.0
        while self.enabled:
            started = time.monotonic()
            env = dict(
                os.environ,
                UC_CONFIG_HOME=str(self.config.data_dir / "ucremote"),
                UC_INTEGRATION_HTTP_PORT=str(self.config.uc_port),
            )
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._command, cwd=PACKAGE_ROOT, env=env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as exc:
                self._last_error = f"could not start the remote driver: {exc}"
                log.error("%s", self._last_error)
                self._on_change()
                await asyncio.sleep(30)
                continue

            log.info("remote driver started (pid %d, port %d)", self._proc.pid, self.config.uc_port)
            self._last_error = None
            self._on_change()
            await self._pump_logs(self._proc)
            code = await self._proc.wait()
            self._on_change()
            if not self.enabled:
                return
            self._last_error = f"the remote driver stopped (status {code}); restarting"
            log.warning("%s", self._last_error)
            if time.monotonic() - started > 60:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _pump_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        while line := await proc.stdout.readline():
            log.info("ucremote: %s", line.decode(errors="replace").rstrip())

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if not proc or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        log.info("remote driver stopped")
