"""Frame-grab housekeeping and the diagnostics bundle (no mpv or capture card needed)."""

import io
import json
import logging
import os
import zipfile

import pytest

from vproc import diagnostics as d
from vproc.config import Config


def test_recent_log_keeps_the_newest_lines():
    recent = d.RecentLog(capacity=3)
    logger = logging.getLogger("test.recent")
    logger.addHandler(recent)
    logger.setLevel(logging.INFO)
    try:
        for n in range(5):
            logger.info("line %d", n)
    finally:
        logger.removeHandler(recent)
    lines = recent.text().splitlines()
    assert len(lines) == 3 and lines[0].endswith("line 2") and lines[-1].endswith("line 4")
    assert "INFO" in lines[0] and "test.recent" in lines[0]


def test_keep_recent_log_installs_once():
    d.keep_recent_log()
    d.keep_recent_log()
    assert logging.getLogger().handlers.count(d.RECENT_LOG) == 1


def test_grab_names_pruning_and_lookup(tmp_path):
    first = d.grab_path(tmp_path, "display")
    first.write_bytes(b"png")
    second = d.grab_path(tmp_path, "display")  # same second: still a new name
    assert second != first and d.GRAB_NAME.match(second.name)
    assert d.grab_path(tmp_path, "source").name.endswith("-source.png")
    with pytest.raises(ValueError):
        d.grab_path(tmp_path, "window")

    for n in range(5):
        path = tmp_path / d.GRAB_DIR / f"grab-20261122-1200{n:02d}-display.png"
        path.write_bytes(b"png")
        os.utime(path, (1000 + n, 1000 + n))
    (tmp_path / d.GRAB_DIR / "notes.txt").write_text("not a grab")
    d.prune_grabs(tmp_path, keep=2)
    kept = [p.name for p in d.list_grabs(tmp_path)]
    assert len(kept) == 2 and (tmp_path / d.GRAB_DIR / "notes.txt").exists()

    assert d.find_grab(tmp_path, kept[0]).is_file()
    for bad in ("../presets.json", "notes.txt", "grab-20261122-120000-display.png.bak", "grab-x-display.png"):
        with pytest.raises(KeyError):
            d.find_grab(tmp_path, bad)


STATUS = {
    "uptime": 3725,
    "signal": {"present": True, "label": "2160p23.976 · HDR10 · BT.2020 · 4:2:2 10-bit"},
    "output": {"running": True, "playing": True, "display_mode": "3840x2160@23.976", "display_fps": 23.976,
               "dropped_frames": 0, "latency_ms": 104},
    "capture": {"device": {"name": "DeckLink Quad HDMI Recorder (1)"}, "driver": "15.0", "dlcap": "0.2.5",
                "skipped": {"startup": 110, "playback": 2}, "missed": 0,
                "source_clock": {"fps": 23.976, "nominal": 23.976, "ppm": -0.4, "measured_for": 600}},
    "lipsync": {"enabled": True, "delay_ms": 430, "offset_ms": 325, "error": None},
    "presets": {"active": "HDR to SDR", "presets": {"HDR to SDR": {}}},
    "errors": [{"time": 1790000000, "source": "output", "message": "mpv exited unexpectedly"}],
}


def test_summary_reads_well():
    text = d.summary(STATUS)
    assert "1 h 2 min" in text
    assert "2160p23.976 · HDR10" in text
    assert "2 during playback, 110 at start" in text
    assert "-0.4 ppm over 600 s" in text
    assert "offset +325 ms, audio delay 430 ms" in text
    assert "output: mpv exited unexpectedly" in text
    quiet = d.summary({"signal": {"present": False}})
    assert "No signal" in quiet and "not measured yet" in quiet and "Recent errors: none" in quiet


def test_bundle_has_everything_but_the_token(tmp_path):
    config = Config(data_dir=tmp_path, api_token="s3cret", mpv_bin="/nonexistent/mpv")
    logging.getLogger("vproc.test").error("something to find in the log")
    d.keep_recent_log()
    logging.getLogger("vproc.test").error("something to find in the log")
    data = d.build_bundle(STATUS, config, {"fields": []})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert names == {"summary.txt", "status.json", "settings.json", "config.json", "presets.json",
                         "versions.txt", "log.txt"}
        everything = b"".join(zf.read(n) for n in names)
        assert b"s3cret" not in everything
        assert json.loads(zf.read("config.json"))["api_token"] == "(set, not included)"
        assert json.loads(zf.read("status.json"))["output"]["latency_ms"] == 104
        assert b"something to find in the log" in zf.read("log.txt")
        assert b"mpv --version failed" in zf.read("versions.txt")
    assert d.bundle_name().startswith("vproc-diagnostics-") and d.bundle_name().endswith(".zip")
