"""Supervises the dlcap capture bridge and relays its JSON events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("vproc.capture")

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CaptureProcess:
    """One running dlcap. Events arrive in order on `on_event`."""

    def __init__(self, dlcap_bin: str, device: str, fifo_dir: Path, queue_depth: int, on_event: EventHandler):
        self.args = [
            dlcap_bin,
            "--device", device,
            "--fifo-dir", str(fifo_dir),
            "--queue-depth", str(queue_depth),
        ]
        self.fifo_dir = fifo_dir
        self._on_event = on_event
        self.proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.fifo_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.fifo_dir.glob("seg-*.fifo"):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
        log.info("starting dlcap: %s", " ".join(self.args))
        self.proc = await asyncio.create_subprocess_exec(
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tasks = [
            asyncio.create_task(self._read_events(), name="dlcap-events"),
            asyncio.create_task(self._read_stderr(), name="dlcap-stderr"),
        ]

    async def wait(self) -> int:
        assert self.proc is not None
        code = await self.proc.wait()
        # Deliver every event dlcap printed before it exited (errors especially).
        await asyncio.gather(*self._tasks, return_exceptions=True)
        return code

    async def send(self, cmd: str) -> None:
        if not self.running or not self.proc.stdin:
            return
        try:
            self.proc.stdin.write(json.dumps({"cmd": cmd}).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.warning("dlcap is gone; dropped command %s", cmd)

    async def stop(self, timeout: float = 5.0) -> None:
        if not self.proc:
            return
        if self.proc.returncode is None:
            await self.send("quit")
            if self.proc.stdin:
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except asyncio.TimeoutError:
                log.warning("dlcap did not exit; killing it")
                self.proc.kill()
                await self.proc.wait()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _read_events(self) -> None:
        assert self.proc and self.proc.stdout
        while line := await self.proc.stdout.readline():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning("dlcap: unparseable line %r", line[:200])
                continue
            try:
                await self._on_event(event)
            except Exception:
                log.exception("handling dlcap event %s failed", event.get("event"))

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            log.warning("dlcap: %s", line.decode(errors="replace").rstrip())
