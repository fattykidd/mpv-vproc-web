"""Display modes for mpv's Vulkan direct-display output (--gpu-context=displayvk).

mpv selects Vulkan display modes by their index in the driver's list, not by
resolution and refresh rate, so vproc asks mpv for the list and looks up the
index of the mode it wants.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("vproc.displays")

_DISPLAY = re.compile(r"^\s*Display\s+(\d+)\b")
_MODE = re.compile(r"^\s*Mode\s+(\d+):\s*(\d+)x(\d+)\s*\(([\d.]+)\s*Hz\)")
_SPEC = re.compile(r"^(\d+)x(\d+)(?:@([\d.]+))?$")

# Two rates closer than this are the same mode (23.976 vs 23.9760239...).
RATE_TOLERANCE = 0.02


@dataclass(frozen=True)
class DisplayMode:
    index: int
    width: int
    height: int
    refresh: float

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}@{self.refresh:.3f}".rstrip("0").rstrip(".")


def parse_vulkan_modes(text: str, display: int = 0) -> list[DisplayMode]:
    """Reads the output of `mpv --vulkan-display-mode=help` for one display."""
    modes: list[DisplayMode] = []
    current = 0
    for line in text.splitlines():
        if m := _DISPLAY.match(line):
            current = int(m.group(1))
        elif (m := _MODE.match(line)) and current == display:
            modes.append(DisplayMode(int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))))
    return modes


def find_mode(modes: list[DisplayMode], spec: str) -> DisplayMode | None:
    """The first mode matching "WIDTHxHEIGHT" or "WIDTHxHEIGHT@RATE"."""
    m = _SPEC.match(spec.strip())
    if not m:
        return None
    width, height = int(m.group(1)), int(m.group(2))
    rate = float(m.group(3)) if m.group(3) else None
    candidates = [d for d in modes if d.width == width and d.height == height]
    if rate is None:
        return max(candidates, key=lambda d: d.refresh, default=None)
    return next((d for d in candidates if abs(d.refresh - rate) < RATE_TOLERANCE), None)


async def probe_vulkan_modes(mpv_bin: str, gpu_api: str, display: int = 0, timeout: float = 15.0) -> list[DisplayMode]:
    """Asks mpv which modes the display offers. Needs the display to be free."""
    args = [
        mpv_bin, "--no-config", "--vo=gpu-next", f"--gpu-api={gpu_api}", "--gpu-context=displayvk",
        "--vulkan-display-mode=help",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("could not list the display's modes: %s", exc)
        return []
    modes = parse_vulkan_modes(out.decode(errors="replace"), display)
    if not modes:
        log.warning("mpv listed no display modes for display %d", display)
    return modes
