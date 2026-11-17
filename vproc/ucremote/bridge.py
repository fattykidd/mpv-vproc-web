"""The driver's connection to vproc: live status over WebSocket, commands over HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

log = logging.getLogger("vproc.ucremote.bridge")

StatusHandler = Callable[[dict[str, Any] | None], Awaitable[None]]


class VprocError(RuntimeError):
    """vproc refused a command, or couldn't be reached (`unreachable`)."""

    def __init__(self, message: str, *, unreachable: bool = False):
        super().__init__(message)
        self.unreachable = unreachable


class VprocBridge:
    def __init__(self, base_url: str, token: str = "", ws_url: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        ws = ws_url or self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/api/ws"
        self.ws_url = ws + (f"?token={urllib.parse.quote(token)}" if token else "")
        self.status: dict[str, Any] | None = None

    async def follow(self, on_status: StatusHandler) -> None:
        """Streams vproc's status to `on_status` forever, reconnecting as needed.

        `on_status(None)` is called whenever the connection is lost.
        """
        delay = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url, max_size=1 << 22) as socket:
                    log.info("connected to vproc at %s", self.base_url)
                    delay = 1.0
                    async for message in socket:
                        self.status = json.loads(message)
                        await on_status(self.status)
            except (OSError, websockets.WebSocketException, json.JSONDecodeError) as exc:
                log.warning("vproc status connection lost (%s); retrying in %.0f s", exc, delay)
            if self.status is not None:
                self.status = None
                await on_status(None)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Sends one API request; runs in a thread so the event loop never blocks."""
        return await asyncio.to_thread(self._call, method, path, body)

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise VprocError(f"{method} {path}: {exc.code} {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise VprocError(f"{method} {path}: {exc}", unreachable=True) from None
