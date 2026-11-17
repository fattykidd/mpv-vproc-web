"""What the remote sees: entity definitions and how vproc's status maps onto them.

Everything here is plain data, so it can be tested without a remote.
"""

from __future__ import annotations

from typing import Any

# Entity identifiers. Keep them stable: the remote stores them in its activities.
PRESET = "preset"
INPUT = "input_signal"
OUTPUT = "output_mode"
AUTO_SWITCH = "auto_preset"
SHOW_INFO = "show_info"
STATS = "stats_overlay"
RESTART_OUTPUT = "restart_output"

NAMES: dict[str, str] = {
    PRESET: "Picture preset",
    INPUT: "Input signal",
    OUTPUT: "Output mode",
    AUTO_SWITCH: "Auto preset by signal",
    SHOW_INFO: "Show signal info",
    STATS: "Stats overlay",
    RESTART_OUTPUT: "Restart video output",
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
