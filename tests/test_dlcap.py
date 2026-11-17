"""End-to-end tests for dlcap against the simulated card in tests/mock_sdk.

Build first (see tests/README.md); the tests are skipped when DLCAP_BIN is unset.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

DLCAP_BIN = os.environ.get("DLCAP_BIN")
pytestmark = pytest.mark.skipif(not DLCAP_BIN, reason="DLCAP_BIN not set")


class Dlcap:
    def __init__(self, tmp_path: Path, script: str, interval_ms: int = 2, extra_env: dict | None = None):
        env = dict(os.environ, MOCK_DECKLINK_SCRIPT=script, MOCK_FRAME_INTERVAL_MS=str(interval_ms))
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [DLCAP_BIN, "--fifo-dir", str(tmp_path / "fifo"), "--queue-depth", "2"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        self.events: queue.Queue[dict] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.events.put(json.loads(line))

    def wait_for(self, name: str, timeout: float = 10.0, **match) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=0.1)
            except queue.Empty:
                continue
            if event["event"] == name and all(event.get(k) == v for k, v in match.items()):
                return event
        raise AssertionError(f"no {name} event within {timeout}s")

    def send(self, cmd: str):
        self.proc.stdin.write(json.dumps({"cmd": cmd}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> int:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                raise
        stderr = self.proc.stderr.read()
        assert "ERROR: AddressSanitizer" not in stderr, stderr
        assert "runtime error" not in stderr, stderr
        return self.proc.returncode


def read_frames(path: str, frame_bytes: int, limit: int) -> list[tuple[int, int, int]]:
    """Reads whole frames from a segment FIFO and returns their mock headers."""
    headers = []
    with open(path, "rb", buffering=0) as fifo:
        while len(headers) < limit:
            buf = bytearray()
            while len(buf) < frame_bytes:
                chunk = fifo.read(frame_bytes - len(buf))
                if not chunk:
                    return headers
                buf += chunk
            magic, step, n, pixfmt, _width = struct.unpack_from("<8sQQII", buf)
            assert magic == b"DLCMOCK1", "frame boundary lost"
            headers.append((step, n, pixfmt))
    return headers


@pytest.fixture
def run(tmp_path):
    started = []

    def start(script: str, **kw) -> Dlcap:
        d = Dlcap(tmp_path, script, **kw)
        started.append(d)
        return d

    yield start
    for d in started:
        d.close()


def test_reports_devices_and_hello(run):
    d = run("1080p60:yuv10:sdr:5", extra_env={"MOCK_DEVICES": "DeckLink Quad HDMI Recorder (1),DeckLink Quad HDMI Recorder (2)"})
    hello = d.wait_for("hello")
    assert hello["driver"] == "mock-driver"
    devices = d.wait_for("devices")["devices"]
    assert [x["name"] for x in devices] == ["DeckLink Quad HDMI Recorder (1)", "DeckLink Quad HDMI Recorder (2)"]
    assert d.wait_for("device")["device"]["index"] == 0


def test_missing_driver_is_a_clear_error(tmp_path):
    d = Dlcap(tmp_path, "1080p60:yuv10:sdr:5", extra_env={"MOCK_NO_DRIVER": "1"})
    err = d.wait_for("error")
    assert err["code"] == "driver"
    assert d.close() == 2


def test_selects_device_by_name(tmp_path):
    env = dict(os.environ, MOCK_DEVICES="Mini Recorder,Quad HDMI Recorder (3)")
    out = subprocess.run([DLCAP_BIN, "--list"], env=env, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0
    p = subprocess.run(
        [DLCAP_BIN, "--device", "nope", "--fifo-dir", str(tmp_path)], env=env, capture_output=True, text=True,
        timeout=10, input="",
    )
    assert p.returncode == 3
    assert '"code":"no_device"' in p.stdout


def test_sdr_segment_frames_are_aligned(run):
    d = run("nosignal:5;1080p60:yuv10:sdr:200")
    assert d.wait_for("signal", present=False)
    seg = d.wait_for("segment")
    assert (seg["width"], seg["height"], seg["pixel_format"], seg["field"]) == (1920, 1080, "v210", "progressive")
    assert seg["row_bytes"] == 5120 and seg["frame_bytes"] == 5120 * 1080
    assert (seg["fps_num"], seg["fps_den"]) == (60000, 1000)
    assert seg["colorimetry"]["transfer"] == "sdr"
    frames = read_frames(seg["path"], seg["frame_bytes"], 50)
    assert len(frames) == 50
    assert all(step == 1 for step, _, _ in frames)


def test_hdr_metadata_and_format_change(run):
    d = run("1080p60:yuv8:sdr:30;2160p23976:yuv12:pq:400:1000")
    first = d.wait_for("segment")
    assert first["pixel_format"] == "2vuy" and first["bit_depth"] == 8
    second = d.wait_for("segment", timeout=15)
    assert second["segment"] > first["segment"]
    assert (second["width"], second["height"]) == (3840, 2160)
    assert (second["fps_num"], second["fps_den"]) == (24000, 1001)
    assert second["pixel_format"] == "v210" and second["bit_depth"] == 12
    c = second["colorimetry"]
    assert (c["matrix"], c["transfer"], c["max_cll"]) == ("bt2020", "pq", 1000)
    assert c["mastering"]["max_luminance"] == 1000
    frames = read_frames(second["path"], second["frame_bytes"], 10)
    assert len(frames) == 10 and all(step == 1 for step, _, _ in frames)


def test_colorimetry_change_within_segment(run):
    d = run("2160p23976:yuv10:pq:60:1000;2160p23976:yuv10:pq:400:4000")
    seg = d.wait_for("segment")
    reader = threading.Thread(target=read_frames, args=(seg["path"], seg["frame_bytes"], 10_000), daemon=True)
    reader.start()
    change = d.wait_for("colorimetry", timeout=15)
    assert change["segment"] == seg["segment"]
    assert change["colorimetry"]["max_cll"] == 4000


def test_signal_loss_ends_segment_with_eof(run):
    d = run("1080p60:rgb10:sdr:100;nosignal:100;1080p60:rgb10:sdr:400")
    seg = d.wait_for("segment")
    assert seg["pixel_format"] == "r210" and seg["signal"] == "rgb444"
    frames = read_frames(seg["path"], seg["frame_bytes"], 10_000)  # returns at EOF
    assert 0 < len(frames) <= 100
    assert d.wait_for("signal", present=False)
    assert d.wait_for("signal", present=True)
    nxt = d.wait_for("segment")
    assert nxt["segment"] > seg["segment"]
    assert read_frames(nxt["path"], nxt["frame_bytes"], 5)


def test_reader_gone_and_new_segment(run):
    d = run("1080p60:yuv10:sdr:100000")
    seg = d.wait_for("segment")
    assert len(read_frames(seg["path"], seg["frame_bytes"], 3)) == 3  # then close mid-stream
    gone = d.wait_for("reader_gone")
    assert gone["segment"] == seg["segment"]
    d.send("new_segment")
    nxt = d.wait_for("segment")
    assert nxt["segment"] > seg["segment"]
    assert not Path(seg["path"]).exists()
    assert len(read_frames(nxt["path"], nxt["frame_bytes"], 20)) == 20
    d.send("stats")
    stats = d.wait_for("stats")
    assert stats["written"] >= 21  # the last frames may still be in flight


def test_slow_reader_drops_instead_of_stalling(run):
    d = run("1080p60:yuv10:sdr:100000", interval_ms=1)
    seg = d.wait_for("segment")
    with open(seg["path"], "rb", buffering=0) as fifo:
        time.sleep(1.0)  # don't read: the queue must stay bounded
        fifo.read(16)
    d.send("stats")
    stats = d.wait_for("stats")
    assert stats["queue"] <= 2
    assert stats["dropped_queue"] > 0


def test_quit_command_cleans_up(run, tmp_path):
    d = run("1080p60:yuv10:sdr:100000")
    d.wait_for("segment")
    d.send("quit")
    assert d.wait_for("exit")["status"] == 0
    assert d.close() == 0
    assert not list((tmp_path / "fifo").glob("*.fifo"))
