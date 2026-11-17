"""How vproc's status maps onto the remote's entities (no ucapi needed)."""

from vproc.ucremote import entities as e

STATUS = {
    "signal": {"present": True, "label": "2160p23.976 · HDR10 · BT.2020 · 4:2:2 10-bit"},
    "output": {"running": True, "display_mode": "3840x2160@23.976", "display_fps": 23.976},
    "presets": {"presets": {"SDR": {}, "HDR to SDR": {}, "Night": {}}, "active": "HDR to SDR", "auto_switch": True},
}


def test_attributes_for_status():
    attrs = e.attributes_for(STATUS)
    assert attrs[e.PRESET] == {"state": "ON", "options": ["SDR", "HDR to SDR", "Night"], "current_option": "HDR to SDR"}
    assert attrs[e.INPUT]["value"].startswith("2160p23.976 · HDR10")
    assert attrs[e.OUTPUT]["value"] == "3840x2160 @ 23.976 Hz"
    assert attrs[e.AUTO_SWITCH] == {"state": "ON"}


def test_attributes_when_idle_or_unreachable():
    idle = {"signal": {"present": False}, "output": {"running": True, "display_mode": "preferred", "display_fps": 60.0},
            "presets": {"presets": {"SDR": {}}, "active": "SDR", "auto_switch": False}}
    attrs = e.attributes_for(idle)
    assert attrs[e.INPUT]["value"] == "No signal"
    assert attrs[e.OUTPUT]["value"] == "Display default @ 60 Hz"
    assert attrs[e.AUTO_SWITCH]["state"] == "OFF"
    assert all(a == {"state": "UNAVAILABLE"} for a in e.attributes_for(None).values())


def test_changed_and_stepping():
    assert e.changed(None, {"a": 1}) == {"a": 1}
    assert e.changed({"a": 1, "b": 2}, {"a": 1, "b": 3}) == {"b": 3}
    opts = ["SDR", "HDR to SDR", "Night"]
    assert e.step_option(opts, "Night", "select_next") == "SDR"
    assert e.step_option(opts, "SDR", "select_previous") == "Night"
    assert e.step_option(opts, "?", "select_first") == "SDR"
    assert e.step_option(opts, "SDR", "select_last") == "Night"
    assert e.step_option([], "SDR", "select_next") is None
