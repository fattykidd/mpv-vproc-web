#!/usr/bin/env python3
"""A stand-in for mpv in tests: speaks the JSON IPC protocol and really reads
the segment FIFOs, checking that every frame starts on a boundary.

FAKE_MPV_LOG        append a JSON line per notable action to this file
FAKE_MPV_BAD_MODES  comma-separated modes that "fail" (no VO)
FAKE_MPV_MODES      the display's modes for --vulkan-display-mode, as WxH@RATE,...
FAKE_MPV_VO_DELAY   seconds before the video output comes up
"""

import asyncio
import json
import os
import struct
import sys
import threading

LOG = os.environ.get("FAKE_MPV_LOG")
_log_lock = threading.Lock()


def record(**entry):
    entry["pid"] = os.getpid()
    if LOG:
        with _log_lock, open(LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")


DEFAULT_MODES = "3840x2160@60,3840x2160@23.976,3840x2160@24,3840x2160@50,3840x2160@59.94,1920x1080@60"


def display_modes():
    return [m for m in os.environ.get("FAKE_MPV_MODES", DEFAULT_MODES).split(",") if m]


def print_mode_list():
    """Mimics `mpv --gpu-context=displayvk --vulkan-display-mode=help`."""
    print("Display 0: FAKE-PROJECTOR")
    print("    Modes:")
    for i, mode in enumerate(display_modes()):
        size, rate = mode.split("@")
        print(f"      Mode {i:02d}: {size} ({float(rate):.3f} Hz)")
    print("    Planes:\n      Plane: 0")


def mode_label(opts):
    if opts.get("gpu-context") == "displayvk":
        index = opts.get("vulkan-display-mode")
        return display_modes()[int(index)] if index is not None else "preferred"
    return opts.get("drm-mode", "")


def parse_args(argv):
    opts = {}
    for a in argv:
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            opts[k] = v
    return opts


class Reader(threading.Thread):
    def __init__(self, path, frame_bytes, on_end):
        super().__init__(daemon=True)
        self.path, self.frame_bytes, self.on_end = path, frame_bytes, on_end
        self.stop_flag = False
        self.frames = 0
        self.fd = None

    def run(self):
        reason = "eof"
        try:
            self.fd = os.open(self.path, os.O_RDONLY)
            while not self.stop_flag:
                buf = bytearray()
                while len(buf) < self.frame_bytes and not self.stop_flag:
                    chunk = os.read(self.fd, self.frame_bytes - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                if len(buf) < self.frame_bytes:
                    break
                if buf[:8] != b"DLCMOCK1":
                    record(event="misaligned", path=self.path, frames=self.frames)
                    reason = "error"
                    break
                self.frames += 1
                if self.frames in (1, 10, 50):
                    step = struct.unpack_from("<Q", buf, 8)[0]
                    record(event="frames", path=self.path, frames=self.frames, step=step)
        except OSError as exc:
            record(event="read_error", error=str(exc))
            reason = "error"
        finally:
            if self.fd is not None:
                os.close(self.fd)
            if self.stop_flag:
                reason = "stop"
            self.on_end(reason, self.frames)


class FakeMpv:
    def __init__(self, opts):
        self.opts = opts
        self.props = {"current-vo": "gpu-next", "display-fps": 60.0, "vf": ""}
        for k, v in opts.items():
            self.props[k] = v
        self.observers = []
        self.writers = set()
        self.reader = None
        self.entry = 0
        self.loop = None
        self.done = asyncio.Event()

    def broadcast(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        for w in list(self.writers):
            try:
                w.write(data)
            except Exception:
                self.writers.discard(w)

    def end_file(self, entry, reason, frames):
        record(event="end-file", entry=entry, reason=reason, frames=frames)
        msg = {"event": "end-file", "reason": reason, "playlist_entry_id": entry}
        if reason == "error":
            msg["file_error"] = "misaligned frames"
        self.loop.call_soon_threadsafe(self.broadcast, msg)

    def handle(self, cmd):
        if isinstance(cmd, dict):
            name = cmd["name"]
            args = cmd
        else:
            name, args = cmd[0], cmd[1:]

        if name == "get_property":
            prop = args[0]
            if prop.startswith("option-info/") and prop.endswith("/default-value"):
                return "success", "default"
            if prop in self.props:
                return "success", self.props[prop]
            return "property unavailable", None
        if name == "set_property":
            self.props[args[0]] = args[1]
            record(event="set", name=args[0], value=args[1])
            return "success", None
        if name == "observe_property":
            self.observers.append(args[1])
            return "success", None
        if name == "loadfile":
            options = args.get("options", {})
            record(event="loadfile", url=args["url"], options=options)
            self.stop_reader()
            self.entry += 1
            entry = self.entry
            self.reader = Reader(args["url"], int(options["demuxer-rawvideo-size"]),
                                 lambda reason, frames: self.end_file(entry, reason, frames))
            self.reader.start()
            return "success", {"playlist_entry_id": entry}
        if name == "osd-overlay":
            record(event="overlay", format=args["format"], data=args.get("data", ""))
            return "success", None
        if name in {"show-text", "script-binding"}:
            record(event=name, args=args)
            return "success", None
        if name == "quit":
            self.loop.call_soon(self.done.set)
            return "success", None
        return "unknown command", None

    def stop_reader(self):
        if self.reader and self.reader.is_alive():
            self.reader.stop_flag = True
            self.reader.join(timeout=5)

    async def client(self, reader, writer):
        self.writers.add(writer)
        try:
            while line := await reader.readline():
                msg = json.loads(line)
                error, data = self.handle(msg["command"])
                reply = {"error": error, "data": data, "request_id": msg.get("request_id", 0)}
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionError, json.JSONDecodeError):
            pass
        finally:
            self.writers.discard(writer)

    async def main(self):
        self.loop = asyncio.get_running_loop()
        mode = mode_label(self.opts)
        record(event="start", mode=mode, options={k: v for k, v in self.opts.items() if k in {"target-peak", "scale"}})
        if mode in filter(None, os.environ.get("FAKE_MPV_BAD_MODES", "").split(",")):
            self.props["current-vo"] = None
        elif os.environ.get("FAKE_MPV_VO_DELAY"):
            # Real mpv needs a moment to bring up the display after IPC is ready.
            self.props["current-vo"] = None
            self.loop.call_later(float(os.environ["FAKE_MPV_VO_DELAY"]), self.props.__setitem__, "current-vo", "gpu-next")
        server = await asyncio.start_unix_server(self.client, path=self.opts["input-ipc-server"])
        async with server:
            await self.done.wait()
        self.stop_reader()
        record(event="exit")


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    if options.get("vulkan-display-mode") == "help":
        print_mode_list()
    else:
        asyncio.run(FakeMpv(options).main())
