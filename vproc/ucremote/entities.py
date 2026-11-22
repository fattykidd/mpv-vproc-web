"""What the remote sees: entity definitions and how vproc's status maps onto them.

Everything here is plain data, so it can be tested without a remote.
"""

from __future__ import annotations

from typing import Any

from ..inputs import spec_for

# Entity identifiers. Keep them stable: the remote stores them in its activities.
PRESET = "preset"
INPUT = "input_signal"
OUTPUT = "output_mode"
AUTO_SWITCH = "auto_preset"
SHOW_INFO = "show_info"
STATS = "stats_overlay"
RESTART_OUTPUT = "restart_output"
CAPTURE_INPUT = "capture_input"
DROPPED = "dropped_frames"
LATENCY = "processing_latency"

NAMES: dict[str, str] = {
    PRESET: "Picture preset",
    INPUT: "Input signal",
    OUTPUT: "Output mode",
    AUTO_SWITCH: "Auto preset by signal",
    SHOW_INFO: "Show signal info",
    STATS: "Stats overlay",
    RESTART_OUTPUT: "Restart video output",
    CAPTURE_INPUT: "Capture input",
    DROPPED: "Dropped frames",
    LATENCY: "Processing latency",
}

# Attribute keys and state values, as the Integration API spells them.
STATE, VALUE, OPTIONS, CURRENT_OPTION = "state", "value", "options", "current_option"
ON, OFF, UNAVAILABLE = "ON", "OFF", "UNAVAILABLE"


def input_text(status: dict[str, Any]) -> str:
    signal = status.get("signal") or {}
    if signal.get("label"):
        return signal["label"]
    if signal.get("present") is False:
        return "No signal"
    return "Waiting for input"


def capture_inputs(status: dict[str, Any]) -> list[dict[str, Any]]:
    """The card's inputs that can capture, as dlcap lists them."""
    devices = (status.get("capture") or {}).get("devices") or []
    return [d for d in devices if d.get("can_capture", True) and d.get("name")]


def current_input(status: dict[str, Any]) -> str:
    """Name of the input in use: the open one, or else the one asked for (it may be missing)."""
    capture = status.get("capture") or {}
    inputs = capture_inputs(status)
    open_index = (capture.get("device") or {}).get("index")
    selected = capture.get("selected")
    for device in inputs:
        if device.get("index") == open_index:
            return device["name"]
    for device in inputs:
        if selected in (spec_for(device), str(device.get("index"))):
            return device["name"]
    return ""


def input_selector(status: dict[str, Any], name: str) -> str | None:
    """The selector vproc's API takes for the input with this name."""
    for device in capture_inputs(status):
        if device["name"] == name:
            return spec_for(device)
    return None


def frames_text(status: dict[str, Any]) -> str:
    """Frames lost where they would show: dropped by mpv, skipped during playback, missed by the card.

    Frames skipped while the output was starting up are left out; nothing was on screen.
    """
    output = status.get("output") or {}
    capture = status.get("capture") or {}
    dropped = (output.get("dropped_frames") or 0) + (output.get("decoder_dropped_frames") or 0)
    skipped = (capture.get("skipped") or {}).get("playback") or 0
    missed = capture.get("missed") or 0
    return f"{dropped} dropped · {skipped} skipped · {missed} missed"


def latency_text(status: dict[str, Any]) -> str:
    latency = (status.get("output") or {}).get("latency_ms")
    return f"≈ {latency} ms" if latency is not None else "Not playing"


def output_text(status: dict[str, Any]) -> str:
    output = status.get("output") or {}
    if not output.get("running"):
        return "Not running"
    mode = output.get("display_mode")
    refresh = output.get("display_fps")
    if mode and mode != "preferred":
        return mode.replace("@", " @ ") + " Hz"
    if refresh:
        return f"Display default @ {refresh:g} Hz"
    return "Display default"


def attributes_for(status: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Entity attributes for a vproc status snapshot (None = vproc unreachable)."""
    if status is None:
        return {
            PRESET: {STATE: UNAVAILABLE},
            INPUT: {STATE: UNAVAILABLE},
            OUTPUT: {STATE: UNAVAILABLE},
            AUTO_SWITCH: {STATE: UNAVAILABLE},
            CAPTURE_INPUT: {STATE: UNAVAILABLE},
            DROPPED: {STATE: UNAVAILABLE},
            LATENCY: {STATE: UNAVAILABLE},
        }
    presets = status.get("presets") or {}
    return {
        PRESET: {
            STATE: ON,
            OPTIONS: list((presets.get("presets") or {}).keys()),
            CURRENT_OPTION: presets.get("active", ""),
        },
        INPUT: {STATE: ON, VALUE: input_text(status)},
        OUTPUT: {STATE: ON, VALUE: output_text(status)},
        AUTO_SWITCH: {STATE: ON if presets.get("auto_switch") else OFF},
        CAPTURE_INPUT: {
            STATE: ON,
            OPTIONS: [d["name"] for d in capture_inputs(status)],
            CURRENT_OPTION: current_input(status),
        },
        DROPPED: {STATE: ON, VALUE: frames_text(status)},
        LATENCY: {STATE: ON, VALUE: latency_text(status)},
    }


def changed(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """The attributes in `current` that differ from `previous`."""
    if previous is None:
        return dict(current)
    return {k: v for k, v in current.items() if previous.get(k) != v}


def step_option(options: list[str], current: str, command: str) -> str | None:
    """Resolves select_next/previous/first/last to an option name."""
    if not options:
        return None
    if command == "select_first":
        return options[0]
    if command == "select_last":
        return options[-1]
    index = options.index(current) if current in options else -1
    if command == "select_next":
        return options[(index + 1) % len(options)]
    if command == "select_previous":
        return options[(index - 1) % len(options)]
    return None
