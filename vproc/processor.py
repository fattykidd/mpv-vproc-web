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
from .ddc import DdcBus, is_real_device, link_locked
from .inputs import InputChoice, check_spec, matches
from .lipsync import LipSync
from .integrations import RemoteIntegration
from .source import Colorimetry, Segment, UnsupportedSignal, describe, display_mode_for, load_options, video_filter
from .timing import STARTUP_GRACE, FrameAccount, QueueAverage, source_clock

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
# A frozen output: frames keep arriving but mpv hasn't taken one for this long. Seen when a
# projector switches to another input: the NVIDIA driver stops completing frames on that
# output ("Flip event timeout") without any hotplug event, and mpv waits forever.
STALL_AFTER = 4.0
STALL_RETRY = 10.0  # minimum time between restarts while it stays frozen
# HDMI link watch (see ddc.py): how often to read the display's lock state, how long a
# freshly started output gets to lock, and how many unlocked readings in a row count.
LINK_POLL = 2.0
LINK_GRACE = 6.0
LINK_UNLOCKED_READINGS = 2
# Lip sync: how long a new signal plays before its delay counts as settled, and how many
# latency readings (one per capture stats event, every 2 s) are averaged into the delay.
LIPSYNC_SETTLE = 10.0
LIPSYNC_READINGS = 5
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
        self._frames = FrameAccount()
        self._queue_avg = QueueAverage()
        self._stall_since: float | None = None
        self._last_stall_restart = 0.0
        self._output_since = 0.0
        self._ddc = DdcBus(config.ddc_device) if is_real_device(config.ddc_device) else None
        self._link: dict[str, Any] = {"watched": self._ddc is not None, "locked": None}
        self.lipsync = LipSync(config.lipsync_urls, config.lipsync_offset, config.lipsync_method)
        self._lipsync_segment: int | None = None  # segment the readings below belong to
        self._lipsync_readings: list[int] = []
        self._lipsync_since = 0.0
        self._lipsync_done = False
        self.inputs = InputChoice(config.data_dir / "input.json", config.device)
        self._capture_wake = asyncio.Event()  # cuts a retry wait short when the input changes

        # Output state.
        self._loaded_at = 0.0
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
        if self._ddc:
            self._tasks.append(asyncio.create_task(self._watch_hdmi_link(), name="hdmi-link"))
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
                "hdmi_link": dict(self._link),
                "latency_ms": self._processing_latency_ms(),
            },
            "lipsync": self.lipsync.status(),
            "capture": {
                "running": bool(self._capture and self._capture.running),
                "device": self._capture_info.get("device"),
                "devices": self._capture_info.get("devices", []),
                "selected": self.inputs.device,
                "selected_in": "web" if self.inputs.chosen else "env",
                "driver": self._capture_info.get("driver"),
                "dlcap": self._capture_info.get("version"),
                "stats": self._capture_stats,
                "skipped": self._frames.to_dict(),
                "missed": self._capture_stats.get("missed", 0),
                "source_clock": self._source_clock(),
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
            self._output_since = time.monotonic()
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
            self._loaded_at = time.monotonic()
            self._queue_avg.reset()
            self._loaded_entry = (result or {}).get("playlist_entry_id")
            await self._set_overlay(None)
            log.info("playing segment %d: %s", seg.id, describe(seg, self._colorimetry))
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
                edid_hlg=self.config.edid_hlg,
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

    async def _watch_hdmi_link(self) -> None:
        """Restarts the output when the display reports it has lost the signal lock.

        Acts only once the display has been seen locked, so a display that reports its
        state oddly can't cause a restart loop. While the display stays away (on another
        input), it retries every STALL_RETRY seconds and reports the problem only once.
        """
        assert self._ddc
        seen_locked = False
        unlocked = 0
        reported = False
        while not self._stopping:
            await asyncio.sleep(LINK_POLL)
            if not self._output_ready or time.monotonic() - self._output_since < LINK_GRACE:
                unlocked = 0
                continue
            try:
                flags = await asyncio.to_thread(self._ddc.status_flags)
            except OSError:
                # Off, in standby or not answering: nothing to judge.
                unlocked = 0
                self._set_link(None)
                continue
            if link_locked(flags):
                seen_locked, unlocked, reported = True, 0, False
                self._set_link(True)
                continue
            self._set_link(False)
            unlocked += 1
            clock = time.monotonic()
            if not seen_locked or unlocked < LINK_UNLOCKED_READINGS or clock - self._last_stall_restart < STALL_RETRY:
                continue
            unlocked = 0
            self._last_stall_restart = clock
            if reported:
                log.info("the display is still not locked onto the signal; restarting the output again")
            else:
                self._error("output", "the display lost the HDMI signal (did it switch inputs?); restarting the output")
                reported = True
            await self.restart_output()

    def _set_link(self, locked: bool | None) -> None:
        if self._link["locked"] != locked:
            self._link["locked"] = locked
            self._notify()

    def _processing_latency_ms(self) -> int | None:
        """vproc's own delay from the capture card to the display, estimated.

        One source frame to arrive whole before the card hands it over, the frames
        waiting in dlcap's queue, then mpv rendering and presenting at the next
        display refresh. It can't include the source's or the display's own
        processing; measuring the whole chain needs a light sensor at the screen.
        """
        segment = self._segment
        if not (self._signal and segment and self._loaded_segment is not None and segment.fps_num):
            return None
        frame_ms = 1000 * segment.fps_den / segment.fps_num
        display_fps = self._props.get("display-fps") or 0
        refresh_ms = 1000 / display_fps if display_fps > 0 else frame_ms
        queued = self._queue_avg.value()
        if queued is None:
            queued = self._capture_stats.get("queue", 0)
        return round(frame_ms * (1 + queued) + refresh_ms)

    def _playback_settled(self) -> bool:
        """A segment has been playing long enough that any skip would show on screen."""
        return (
            self._loaded_segment is not None
            and self._segment is not None
            and self._loaded_segment == self._segment.id
            and time.monotonic() - self._loaded_at >= STARTUP_GRACE
        )

    def _source_clock(self) -> dict[str, Any] | None:
        segment = self._segment
        if not (self._signal and segment):
            return None
        clock = source_clock(self._capture_stats, segment.fps_num, segment.fps_den)
        return clock.to_dict() if clock else None

    def _track_lipsync(self) -> None:
        """Collects latency readings for the playing signal; once settled, sends the delay once."""
        if not self.lipsync.enabled:
            return
        latency = self._processing_latency_ms()
        if latency is None:
            return
        if self._loaded_segment != self._lipsync_segment:
            self._lipsync_segment = self._loaded_segment
            self._lipsync_readings, self._lipsync_since, self._lipsync_done = [], time.monotonic(), False
        if self._lipsync_done:
            return
        if not (self._props.get("display-fps") or 0) > 0:
            # Right after a start or mode switch the display rate is unknown, and the
            # estimate would fall back to a source frame instead of a display refresh.
            return
        if not self._playback_settled():
            return  # the queue is still filling as mpv opens the stream
        self._lipsync_readings.append(latency)
        if len(self._lipsync_readings) < LIPSYNC_READINGS or time.monotonic() - self._lipsync_since < LIPSYNC_SETTLE:
            return
        self._lipsync_done = True
        settled = round(sum(self._lipsync_readings) / len(self._lipsync_readings))
        asyncio.create_task(self._apply_lipsync(settled), name="lipsync")

    async def update_lipsync(self, urls: tuple[str, ...], offset_ms: int, method: str) -> None:
        """Takes new lip-sync settings and, if the playing signal's delay is already known, resends it."""
        self.lipsync.configure(urls, offset_ms, method)
        if self._lipsync_done and self.lipsync.enabled and self.lipsync.video_ms is not None:
            await self._apply_lipsync(self.lipsync.video_ms)
        else:
            self._notify()

    async def _apply_lipsync(self, latency_ms: float) -> None:
        if await self.lipsync.apply(latency_ms) and self.lipsync.error:
            self._error("lipsync", f"could not set the audio delay: {self.lipsync.error}")
        self._notify()

    def _check_output_stall(self, before: dict[str, Any], now: dict[str, Any]) -> None:
        """Restarts the output when frames keep arriving but mpv has stopped taking them."""
        playing = self._output_ready and self._signal and self._loaded_segment is not None
        arriving = now.get("captured", 0) > before.get("captured", 0)
        taken = now.get("written", 0) > before.get("written", 0)
        if not playing or not arriving or taken:
            self._stall_since = None
            return
        clock = time.monotonic()
        if self._stall_since is None:
            self._stall_since = clock
            return
        if clock - self._stall_since < STALL_AFTER or clock - self._last_stall_restart < STALL_RETRY:
            return
        self._stall_since = None
        self._last_stall_restart = clock
        self._error("output", "the display stopped taking frames (did it switch inputs?); restarting the output")
        asyncio.create_task(self.restart_output(), name="stall-restart")

    async def _on_capture_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "segment":
            self._segment = Segment.from_event(event)
            self._colorimetry = self._interpret(self._segment.colorimetry)
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
            previous = self._capture_stats
            self._capture_stats = {k: v for k, v in event.items() if k != "event"}
            settled = self._playback_settled()
            self._frames.update(self._capture_stats, settled=settled)
            if settled:
                # Not before: while mpv opens the stream, frames back up and the queue reads full.
                self._queue_avg.add(self._capture_stats)
            self._check_output_stall(previous, self._capture_stats)
            self._track_lipsync()
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

    def _interpret(self, colorimetry: Colorimetry) -> Colorimetry:
        """Applies VPROC_LLDV: SDR-flagged BT.2020 input is LLDV that nothing relabelled as HDR10."""
        if self.config.lldv and colorimetry.looks_like_lldv:
            return colorimetry.as_lldv(self.config.lldv_peak)
        return colorimetry

    async def _on_colorimetry(self, event: dict[str, Any]) -> None:
        if not self._segment or event.get("segment") != self._segment.id:
            return
        colorimetry = self._interpret(Colorimetry.from_event(event.get("colorimetry") or {}))
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

    def report(self, source: str, message: str) -> None:
        """Adds a problem to the web page's list, for parts of vproc outside the processor."""
        self._error(source, message)

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
