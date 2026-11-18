"""The video processor: keeps dlcap and mpv running and in step with the input.

Flow: dlcap announces a segment (a FIFO plus the signal's format and HDR
metadata) -> we make sure mpv runs at a matching refresh rate -> mpv loads the
FIFO with options that describe the signal -> libplacebo tone-maps and scales.
Signal loss, format changes, HDR metadata changes and crashes of either
process are all handled here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Any

from .capture import CaptureProcess
from .config import Config
from .mpv import MpvError, MpvProcess, build_args
from .settings import OPTIONS, PresetStore, Value
from .displays import DisplayMode, find_mode, probe_vulkan_modes
from .inputs import InputChoice, check_spec, matches
from .integrations import RemoteIntegration
from .source import Colorimetry, Segment, UnsupportedSignal, describe, display_mode_for, load_options, video_filter

log = logging.getLogger("vproc.processor")

OVERLAY_ID = 1
OBSERVED = (
    "video-out-params",
    "video-target-params",
    "display-fps",
    "estimated-vf-fps",
    "frame-drop-count",
    "decoder-frame-drop-count",
    "vo-delayed-frame-count",
)
# dlcap exit codes that mean "fix the setup", not "try again right away".
SETUP_ERRORS = {2: "Desktop Video driver not available", 3: "capture device not found", 4: "device can't capture"}


class Backoff:
    def __init__(self, first: float = 1.0, maximum: float = 30.0, stable_after: float = 60.0):
        self.first, self.maximum, self.stable_after = first, maximum, stable_after
        self.delay = first
        self.started = time.monotonic()

    def mark_started(self) -> None:
        self.started = time.monotonic()

    async def sleep(self) -> None:
        if time.monotonic() - self.started > self.stable_after:
            self.delay = self.first
        await asyncio.sleep(self.delay)
        self.delay = min(self.delay * 2, self.maximum)


class Processor:
    def __init__(self, config: Config, store: PresetStore):
        self.config = config
        self.store = store

        self._mpv: MpvProcess | None = None
        self._capture: CaptureProcess | None = None
        self._output_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._restart_output = False
        self._output_ready = False  # mpv is up, its display is on and it's been configured
        self._started_at = time.time()

        # Input state, as reported by dlcap.
        self._signal: bool | None = None
        self._segment: Segment | None = None
        self._colorimetry: Colorimetry | None = None
        self._capture_info: dict[str, Any] = {}
        self._capture_stats: dict[str, Any] = {}
        self.inputs = InputChoice(config.data_dir / "input.json", config.device)
        self._capture_wake = asyncio.Event()  # cuts a retry wait short when the input changes

        # Output state.
        self._vulkan_modes: list[DisplayMode] = []
        self._bad_modes: set[str] = set()
        self._display_mode = self._home_mode
        self._given_upto = 0  # highest segment id handed to an mpv reader
        self._loaded_segment: int | None = None
        self._loaded_entry: int | None = None
        self._new_segment_asked: int | None = None
        self._reload_times: deque[float] = deque(maxlen=6)
        self._defaults: dict[str, Any] = {}
        self._applied: dict[str, Value] = {}
        self._props: dict[str, Any] = {}
        self._overlay: str | None = None

        self._errors: deque[dict[str, Any]] = deque(maxlen=20)
        self.remote = RemoteIntegration(config, on_change=self._notify)
        self._subscribers: set[asyncio.Queue] = set()
        self._dirty = asyncio.Event()
        self._save_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.config.run_dir.mkdir(parents=True, exist_ok=True)
        self._tasks = [
            asyncio.create_task(self._supervise_output(), name="output"),
            asyncio.create_task(self._supervise_capture(), name="capture"),
            asyncio.create_task(self._broadcast(), name="broadcast"),
        ]
        await self.remote.start()

    async def stop(self) -> None:
        self._stopping = True
        await self.remote.stop()
        if self._capture:
            await self._capture.stop()
        if self._mpv:
            await self._mpv.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            try:
                self.store.save()
            except OSError as exc:
                log.error("could not save presets: %s", exc)

    # ------------------------------------------------------------------ public API

    def status(self) -> dict[str, Any]:
        seg = self._segment if self._signal else None
        colorimetry = self._colorimetry if seg else None
        mpv_up = bool(self._mpv and self._mpv.client.connected)
        return {
            "uptime": round(time.time() - self._started_at),
            "signal": {
                "present": self._signal,
                "segment": seg.to_dict() if seg else None,
                "colorimetry": colorimetry.to_dict() if colorimetry else None,
                "label": describe(seg, colorimetry) if seg else None,
            },
            "output": {
                "running": mpv_up,
                "playing": mpv_up and self._loaded_segment is not None,
                "display_mode": self._display_mode if self._switches_modes else None,
                "unsupported_modes": sorted(self._bad_modes),
                "display_fps": self._props.get("display-fps"),
                "estimated_fps": self._props.get("estimated-vf-fps"),
                "dropped_frames": self._props.get("frame-drop-count"),
                "decoder_dropped_frames": self._props.get("decoder-frame-drop-count"),
                "delayed_frames": self._props.get("vo-delayed-frame-count"),
                "video_params": self._props.get("video-out-params"),
                "target_params": self._props.get("video-target-params"),
            },
            "capture": {
                "running": bool(self._capture and self._capture.running),
                "device": self._capture_info.get("device"),
                "devices": self._capture_info.get("devices", []),
                "selected": self.inputs.device,
                "selected_in": "web" if self.inputs.chosen else "env",
                "driver": self._capture_info.get("driver"),
                "dlcap": self._capture_info.get("version"),
                "stats": self._capture_stats,
            },
            "presets": self.store.state.to_dict(),
            "integrations": {"uc_remote": self.remote.status()},
            "errors": list(self._errors),
        }

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def activate_preset(self, name: str) -> None:
        if name not in self.store.state.presets:
            raise KeyError(name)
        self.store.state.active = name
        await self._apply_settings()
        self._persist()

    async def update_settings(self, values: dict[str, Any], preset: str | None = None) -> dict[str, Value]:
        """Changes values in a preset (the active one by default) and applies them live."""
        target = preset or self.store.state.active
        clean = self.store.update(target, values)
        if target == self.store.state.active:
            await self._apply_settings()
        self._persist()
        return clean

    async def reset_setting(self, option: str, preset: str | None = None) -> None:
        if option not in OPTIONS:
            raise KeyError(option)
        target = preset or self.store.state.active
        self.store.reset_option(target, option)
        if target == self.store.state.active:
            await self._apply_settings()
        self._persist()

    async def presets_changed(self) -> None:
        """Call after creating/renaming/deleting presets through the store."""
        await self._apply_settings()
        self._persist()

    async def show_message(self, text: str, duration_ms: int = 3000) -> None:
        client = self._client()
        await client.command("show-text", text, duration_ms)

    async def toggle_stats(self, page: int | None = None) -> None:
        client = self._client()
        if page is None:
            await client.command("script-binding", "stats/display-stats-toggle")
        else:
            await client.command("script-binding", f"stats/display-page-{page}")

    async def select_input(self, spec: str | None) -> None:
        """Captures from another input (None: back to VPROC_DEVICE). The choice is kept across restarts."""
        check_spec(spec)
        devices = self._capture_info.get("devices", [])
        if spec is not None and devices and not any(matches(spec, d) for d in devices):
            raise KeyError(spec)
        before = self.inputs.device
        self.inputs.set(spec)
        if self.inputs.device != before:
            log.info("switching capture input to %s", self.inputs.device)
            self._capture_wake.set()
            if self._capture:
                await self._capture.stop()  # the supervisor starts dlcap again on the new input
        self._notify()

    async def restart_capture(self) -> None:
        if self._capture:
            await self._capture.stop()  # the supervisor starts a fresh one

    async def restart_output(self) -> None:
        if self._mpv:
            self._restart_output = True
            await self._mpv.stop()

    # ------------------------------------------------------------------ output (mpv)

    def _client(self):
        if not self._mpv or not self._mpv.client.connected:
            raise MpvError("the video output is not running")
        return self._mpv.client

    @property
    def _switches_modes(self) -> bool:
        """Whether vproc picks the display mode (direct-to-display outputs only)."""
        return self.config.gpu_context in {"displayvk", "drm"}

    @property
    def _home_mode(self) -> str:
        """The mode to use when the source doesn't decide one.

        VPROC_DISPLAY_MODE if set; otherwise the output size at its highest refresh rate, because a display's
        own "preferred" mode can be lower (many 4K projectors prefer 1080p); "preferred" as the last resort.
        """
        if self.config.display_mode != "preferred":
            return self.config.display_mode
        if self.config.output_size and self.config.output_size not in self._bad_modes:
            return self.config.output_size
        return "preferred"

    def _desired_mode(self) -> str:
        if not (self._switches_modes and self.config.match_refresh):
            return self._home_mode
        if not (self._signal and self._segment):
            # Keep whatever is showing, to avoid needless mode switches; unless it turned out not to work.
            return self._home_mode if self._display_mode in self._bad_modes else self._display_mode
        mode = display_mode_for(self._segment, self.config.output_size, self.config.refresh_map, self._home_mode)
        return self._home_mode if mode in self._bad_modes else mode

    async def _vulkan_mode_index(self, mode: str) -> int | None:
        """Index of a mode in the display's list; None means mpv's default.

        Raises LookupError when the display doesn't offer the mode.
        """
        # Re-read the list on every start: the display may have been swapped.
        self._vulkan_modes = await probe_vulkan_modes(self.config.mpv_bin, self.config.gpu_api, self.config.vulkan_display)
        if mode == "preferred" or not self._vulkan_modes:
            return None
        found = find_mode(self._vulkan_modes, mode)
        if found is None:
            raise LookupError(mode)
        return found.index

    def _display_lacks(self, mode: str) -> bool:
        """True when the last mode list shows the display can't do `mode`."""
        return (
            self.config.gpu_context == "displayvk"
            and mode != "preferred"
            and bool(self._vulkan_modes)
            and find_mode(self._vulkan_modes, mode) is None
        )

    def _reject_mode(self, mode: str) -> None:
        self._bad_modes.add(mode)
        self._error("output", f"the display has no {mode} mode; using {self._home_mode} instead")

    async def _supervise_output(self) -> None:
        backoff = Backoff()
        while not self._stopping:
            mode = self._desired_mode()
            vulkan_index = None
            if self.config.gpu_context == "displayvk":
                try:
                    vulkan_index = await self._vulkan_mode_index(mode)
                except LookupError:
                    if mode != self.config.display_mode:
                        self._reject_mode(mode)
                        continue
                    self._error("output", f"the display has no {mode} mode (VPROC_DISPLAY_MODE); using its default")
            self._mpv = MpvProcess(
                build_args(
                    mpv_bin=self.config.mpv_bin,
                    socket_path=self.config.mpv_socket,
                    gpu_api=self.config.gpu_api,
                    gpu_context=self.config.gpu_context,
                    display_mode=mode,
                    vulkan_display=self.config.vulkan_display,
                    vulkan_mode_index=vulkan_index,
                    drm_format=self.config.drm_format,
                    drm_device=self.config.drm_device,
                    drm_connector=self.config.drm_connector,
                    log_level=self.config.mpv_log_level,
                    options=self.store.values(),
                    extra_args=self.config.mpv_extra_args,
                ),
                self.config.mpv_socket,
                self._on_mpv_event,
            )
            self._output_ready = False
            self._loaded_segment = None
            self._loaded_entry = None
            try:
                await self._mpv.start()
                await self._prepare_mpv()
            except MpvError as exc:
                await self._mpv.stop()
                if self._stopping:
                    return
                if mode != self.config.display_mode:
                    self._bad_modes.add(mode)
                    self._error("output", f"display mode {mode} failed; using {self._home_mode} instead")
                    continue
                self._error("output", f"mpv failed to start: {exc}")
                await backoff.sleep()
                continue

            self._display_mode = mode
            self._output_ready = True
            backoff.mark_started()
            self._notify()
            await self._sync_output()

            code = await self._mpv.wait()
            self._output_ready = False
            await self._mpv.stop()
            self._props.clear()
            self._notify()
            if self._stopping:
                return
            if self._restart_output:
                self._restart_output = False
                continue
            self._error("output", f"mpv exited unexpectedly (status {code}); restarting")
            await backoff.sleep()

    async def _prepare_mpv(self) -> None:
        client = self._mpv.client
        if self._switches_modes:
            # A display mode that won't come up leaves mpv idle without a video output.
            for _ in range(30):
                if await client.get("current-vo"):
                    break
                await asyncio.sleep(0.1)
            else:
                raise MpvError("no video output came up")
        for prop in OBSERVED:
            await client.observe(prop)
        if not self._defaults:
            defaults: dict[str, Any] = {}
            for name in OPTIONS:
                try:
                    defaults[name] = await client.command("get_property", f"option-info/{name}/default-value")
                except MpvError:
                    if not client.connected:
                        raise
                    log.warning("this mpv has no %s option; it can't be reset to a default", name)
            self._defaults = defaults
        self._applied = self.store.values()
        self._overlay = None

    async def _apply_settings(self) -> None:
        """Brings mpv's options in line with the active preset."""
        target = self.store.values()
        changes: dict[str, Any] = {}
        for name in set(target) | set(self._applied):
            value = target.get(name, self._defaults.get(name))
            if value is None:
                continue
            if self._applied.get(name) != value or name not in target:
                changes[name] = value
        self._notify()
        if not (self._mpv and self._mpv.client.connected):
            return
        failed = []
        for name, value in changes.items():
            try:
                await self._mpv.client.set(name, value)
            except MpvError as exc:
                failed.append(f"{name}: {exc}")
        self._applied = target
        if failed:
            self._error("settings", "mpv rejected " + "; ".join(failed))

    async def _maybe_auto_switch(self) -> None:
        state = self.store.state
        if not (state.auto_switch and self._colorimetry):
            return
        preset = state.auto.get(self._colorimetry.kind)
        if preset and preset != state.active and preset in state.presets:
            log.info("signal is %s; switching to preset %r", self._colorimetry.kind, preset)
            state.active = preset
            await self._apply_settings()
            self._persist()

    async def _sync_output(self) -> None:
        """Makes mpv show the current segment, or the no-signal screen."""
        async with self._output_lock:
            # While mpv is still starting, leave it alone; the output supervisor
            # calls this again as soon as the display is up.
            if not (self._output_ready and self._mpv and self._mpv.client.connected):
                return
            client = self._mpv.client
            seg = self._segment

            if self._signal is False or seg is None:
                await self._set_overlay("No signal" if self._signal is False else "Waiting for input")
                return

            if self._display_lacks(self._desired_mode()):
                self._reject_mode(self._desired_mode())  # no restart needed to find that out
            if self._desired_mode() != self._display_mode:
                log.info("switching display mode %s -> %s", self._display_mode, self._desired_mode())
                self._restart_output = True
                await self._mpv.stop()  # the output supervisor restarts mpv in the new mode
                return

            if self._loaded_segment == seg.id:
                return
            if seg.id <= self._given_upto:
                # This FIFO already had a reader; a FIFO can't be rejoined mid-stream.
                if self._new_segment_asked != seg.id and self._capture:
                    self._new_segment_asked = seg.id
                    await self._capture.send("new_segment")
                return

            await self._maybe_auto_switch()
            try:
                options = load_options(seg, self._colorimetry, self.config.rgb_range)
            except UnsupportedSignal as exc:
                self._error("input", str(exc))
                await self._set_overlay("Unsupported input format")
                return

            self._given_upto = seg.id
            try:
                result = await client.command_named("loadfile", url=seg.path, flags="replace", options=options)
            except MpvError as exc:
                self._error("output", f"could not load the capture stream: {exc}")
                return
            self._loaded_segment = seg.id
            self._loaded_entry = (result or {}).get("playlist_entry_id")
            await self._set_overlay(None)
            log.info("playing segment %d: %s", seg.id, seg.to_dict()["label"])
            self._notify()

    async def _set_overlay(self, text: str | None) -> None:
        if text == self._overlay or not (self._mpv and self._mpv.client.connected):
            return
        try:
            if text is None:
                await self._mpv.client.command_named("osd-overlay", id=OVERLAY_ID, format="none", data="")
            else:
                ass = "{\\an5}{\\fs42}{\\bord2}" + text.replace("{", "(").replace("}", ")")
                await self._mpv.client.command_named(
                    "osd-overlay", id=OVERLAY_ID, format="ass-events", data=ass, res_x=0, res_y=720
                )
            self._overlay = text
        except MpvError as exc:
            log.warning("could not update the overlay: %s", exc)

    async def _on_mpv_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "property-change":
            self._props[event.get("name")] = event.get("data")
            self._notify()
        elif name == "end-file":
            if event.get("playlist_entry_id") == self._loaded_entry and self._loaded_entry is not None:
                reason = event.get("reason")
                self._loaded_segment = None
                self._loaded_entry = None
                if reason == "error":
                    self._error("output", f"mpv stopped playing: {event.get('file_error', 'unknown error')}")
                    asyncio.create_task(self._retry_after_error())
                self._notify()

    async def _retry_after_error(self) -> None:
        now = time.monotonic()
        self._reload_times.append(now)
        if len(self._reload_times) == self._reload_times.maxlen and now - self._reload_times[0] < 30:
            self._error("output", "playback keeps failing for this input; waiting for the signal to change")
            return
        await asyncio.sleep(0.5)
        await self._sync_output()

    # ------------------------------------------------------------------ capture (dlcap)

    async def _supervise_capture(self) -> None:
        backoff = Backoff()
        while not self._stopping:
            self._capture_wake.clear()
            self._capture = CaptureProcess(
                self.config.dlcap_bin,
                self.inputs.device,
                self.config.fifo_dir,
                self.config.queue_depth,
                self._on_capture_event,
            )
            try:
                await self._capture.start()
            except OSError as exc:
                self._error("capture", f"could not run dlcap: {exc}")
                await backoff.sleep()
                continue
            backoff.mark_started()
            self._notify()
            code = await self._capture.wait()
            if self._stopping:
                return

            self._signal = None
            self._segment = None
            self._colorimetry = None
            self._capture_stats = {}
            self._notify()
            await self._sync_output()
            if code in SETUP_ERRORS:
                self._error("capture", f"{SETUP_ERRORS[code]}; retrying in 30 s")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._capture_wake.wait(), 30)
            elif code != 0:
                self._error("capture", f"dlcap exited with status {code}; restarting")
                await backoff.sleep()

    async def _on_capture_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "segment":
            self._segment = Segment.from_event(event)
            self._colorimetry = self._segment.colorimetry
            self._signal = True
            self._notify()
            await self._sync_output()
        elif name == "colorimetry":
            await self._on_colorimetry(event)
        elif name == "signal":
            self._signal = bool(event.get("present"))
            if not self._signal:
                self._segment = None
                self._colorimetry = None
            self._notify()
            await self._sync_output()
        elif name == "reader_gone":
            if event.get("segment") == self._loaded_segment:
                self._loaded_segment = None
            self._notify()
        elif name == "stats":
            self._capture_stats = {k: v for k, v in event.items() if k != "event"}
            self._notify()
        elif name == "hello":
            self._capture_info.update(version=event.get("version"), driver=event.get("driver"))
        elif name == "devices":
            self._capture_info["devices"] = event.get("devices", [])
        elif name == "device":
            self._capture_info["device"] = event.get("device")
            self._notify()
        elif name == "error":
            self._error("capture", event.get("message", "unknown dlcap error"))
        elif name == "log":
            level = logging.WARNING if event.get("level") == "warning" else logging.INFO
            log.log(level, "dlcap: %s", event.get("message"))

    async def _on_colorimetry(self, event: dict[str, Any]) -> None:
        if not self._segment or event.get("segment") != self._segment.id:
            return
        colorimetry = Colorimetry.from_event(event.get("colorimetry") or {})
        previous_kind = self._colorimetry.kind if self._colorimetry else None
        self._colorimetry = colorimetry
        self._notify()
        if colorimetry.kind != previous_kind:
            await self._maybe_auto_switch()
        if self._loaded_segment == self._segment.id and self._mpv and self._mpv.client.connected:
            try:
                await self._mpv.client.set("vf", video_filter(self._segment, colorimetry, self.config.rgb_range))
            except MpvError as exc:
                self._error("output", f"could not apply new HDR metadata: {exc}")

    # ------------------------------------------------------------------ plumbing

    def _error(self, source: str, message: str) -> None:
        log.error("%s: %s", source, message)
        self._errors.appendleft({"time": time.time(), "source": source, "message": message})
        self._notify()

    def _notify(self) -> None:
        self._dirty.set()

    async def _broadcast(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            snapshot = self.status()
            for queue in list(self._subscribers):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()  # slow client: keep only the newest state
                queue.put_nowait(snapshot)
            await asyncio.sleep(0.25)

    def _persist(self) -> None:
        """Saves presets shortly after the last change (slider drags send many)."""
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()

        async def save_later() -> None:
            await asyncio.sleep(1.0)
            try:
                self.store.save()
            except OSError as exc:
                self._error("settings", f"could not save presets: {exc}")

        self._save_task = asyncio.create_task(save_later())
