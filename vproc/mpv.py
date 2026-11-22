"""mpv: the output process and an asyncio client for its JSON IPC socket."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("vproc.mpv")

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class MpvError(RuntimeError):
    """mpv rejected a command or could not be reached."""


class MpvClient:
    """Asynchronous JSON IPC client.

    Replies are matched by request_id, so commands may run concurrently, and
    unsolicited events (property changes, end-file, ...) go to `on_event`.
    """

    def __init__(self, socket_path: Path | str, on_event: EventHandler | None = None):
        self._path = str(socket_path)
        self._on_event = on_event
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._observe_ids = itertools.count(1)
        self._read_task: asyncio.Task | None = None
        self.closed = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self.closed.is_set()

    async def connect(self, timeout: float = 10.0) -> None:
        """Connects, retrying while mpv is still starting up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(self._path, limit=1 << 22)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if loop.time() >= deadline:
                    raise MpvError(f"mpv IPC socket {self._path} did not come up") from None
                await asyncio.sleep(0.1)
        self.closed.clear()
        self._read_task = asyncio.create_task(self._read_loop(), name="mpv-ipc-read")

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        self._teardown()

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        return await self._request({"command": list(args)}, timeout)

    async def command_named(self, name: str, timeout: float = 5.0, **kwargs: Any) -> Any:
        """Named-argument form, immune to positional changes between mpv versions."""
        return await self._request({"command": {"name": name, **kwargs}}, timeout)

    async def get(self, prop: str, default: Any = None) -> Any:
        try:
            return await self.command("get_property", prop)
        except MpvError:
            return default

    async def set(self, prop: str, value: Any) -> None:
        await self.command("set_property", prop, value)

    async def observe(self, prop: str) -> int:
        oid = next(self._observe_ids)
        await self.command("observe_property", oid, prop)
        return oid

    async def _request(self, payload: dict[str, Any], timeout: float) -> Any:
        if not self.connected:
            raise MpvError("not connected to mpv")
        rid = next(self._ids)
        payload["request_id"] = rid
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            self._writer.write(json.dumps(payload).encode() + b"\n")
            await self._writer.drain()
            reply = await asyncio.wait_for(future, timeout)
        except (ConnectionError, OSError) as exc:
            raise MpvError(f"lost connection to mpv: {exc}") from exc
        except asyncio.TimeoutError:
            raise MpvError(f"mpv did not answer {payload['command']!r}") from None
        finally:
            self._pending.pop(rid, None)
        if reply.get("error") != "success":
            raise MpvError(f"{payload['command']!r}: {reply.get('error')}")
        return reply.get("data")

    async def _read_loop(self) -> None:
        try:
            while line := await self._reader.readline():
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("ignoring malformed IPC line: %r", line[:200])
                    continue
                if "request_id" in msg and "event" not in msg:
                    future = self._pending.get(msg["request_id"])
                    if future and not future.done():
                        future.set_result(msg)
                elif "event" in msg and self._on_event:
                    try:
                        result = self._on_event(msg)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        log.exception("mpv event handler failed for %s", msg.get("event"))
        except (ConnectionError, OSError):
            pass
        finally:
            self._teardown()

    def _teardown(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MpvError("mpv connection closed"))
        self._pending.clear()
        if self._writer:
            self._writer.close()
        self._writer = None
        self.closed.set()


def build_args(
    *,
    mpv_bin: str,
    socket_path: Path,
    gpu_api: str,
    gpu_context: str,
    display_mode: str = "preferred",
    vulkan_display: int = 0,
    vulkan_mode_index: int | None = None,
    drm_format: str = "xrgb8888",
    drm_device: str = "",
    drm_connector: str = "",
    log_level: str = "warn",
    options: dict[str, Any] | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Command line for a headless, low-latency mpv that waits for segments."""
    args = [
        mpv_bin,
        "--no-config",
        "--idle=yes",
        "--force-window=yes",
        f"--input-ipc-server={socket_path}",
        "--vo=gpu-next",
        f"--gpu-api={gpu_api}",
        f"--gpu-context={gpu_context}",
        "--hwdec=no",
        "--audio=no",
        "--osc=no",
        "--input-default-bindings=no",
        "--input-vo-keyboard=no",
        "--keep-open=no",
        # Live input: show each frame as soon as it's decoded, buffer nothing.
        "--untimed=yes",
        "--cache=no",
        "--demuxer-readahead-secs=0",
        "--video-latency-hacks=yes",
        "--interpolation=no",
        # vproc tone-maps for the display itself, so mpv must not switch the
        # output to HDR or describe the picture to the display on its own.
        "--target-colorspace-hint=no",
        "--osd-level=1",
        "--osd-duration=2500",
        "--terminal=yes",
        "--msg-color=no",
        f"--msg-level=all={log_level}",
    ]
    if gpu_context == "displayvk":
        args.append(f"--vulkan-display-display={vulkan_display}")
        if vulkan_mode_index is not None:
            args.append(f"--vulkan-display-mode={vulkan_mode_index}")
    elif gpu_context == "drm":
        args += [f"--drm-mode={display_mode}", f"--drm-format={drm_format}"]
        if drm_device:
            args.append(f"--drm-device={drm_device}")
        if drm_connector:
            args.append(f"--drm-connector={drm_connector}")
    for name, value in (options or {}).items():
        args.append(f"--{name}={format_value(value)}")
    args += list(extra_args)
    return args


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return ":".join(map(str, value))  # mpv's path-list separator on Linux
    return str(value)


class MpvProcess:
    """One running mpv instance plus its IPC connection."""

    def __init__(self, args: list[str], socket_path: Path, on_event: EventHandler):
        self.args = args
        self.socket_path = socket_path
        self.client = MpvClient(socket_path, on_event)
        self.proc: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, timeout: float = 10.0) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        log.info("starting mpv: %s", " ".join(self.args))
        self.proc = await asyncio.create_subprocess_exec(
            *self.args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._log_task = asyncio.create_task(self._pump_logs(), name="mpv-log")
        connect = asyncio.create_task(self.client.connect(timeout))
        exited = asyncio.create_task(self.proc.wait())
        done, _ = await asyncio.wait({connect, exited}, return_when=asyncio.FIRST_COMPLETED)
        if exited in done:
            connect.cancel()
            raise MpvError(f"mpv exited during start-up with status {self.proc.returncode}")
        exited.cancel()
        connect.result()  # re-raises a connection failure

    async def wait(self) -> int:
        assert self.proc is not None
        return await self.proc.wait()

    async def stop(self, timeout: float = 3.0) -> None:
        if self.client.connected:
            with contextlib.suppress(MpvError):
                await self.client.command("quit", timeout=1.0)
        await self.client.close()
        if self.proc and self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout)
            except asyncio.TimeoutError:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    try:
                        await asyncio.wait_for(self.proc.wait(), timeout)
                    except asyncio.TimeoutError:
                        # Stuck in the kernel (usually the GPU driver); nothing more we can do.
                        log.error("mpv (pid %d) did not exit even after SIGKILL", self.proc.pid)
        if self._log_task:
            try:
                await asyncio.wait_for(asyncio.gather(self._log_task, return_exceptions=True), timeout)
            except asyncio.TimeoutError:
                self._log_task.cancel()

    async def _pump_logs(self) -> None:
        assert self.proc and self.proc.stdout
        while line := await self.proc.stdout.readline():
            text = line.decode(errors="replace").rstrip()
            if text:
                log.info("%s", text)
