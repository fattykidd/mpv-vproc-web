"""The whole service against dlcap (on the simulated card) and a fake mpv."""

import asyncio
import json
import os
import signal
from pathlib import Path

import pytest

from vproc.config import Config
from vproc.processor import Processor
from vproc.settings import PresetStore

DLCAP_BIN = os.environ.get("DLCAP_BIN")
FAKE_MPV = str(Path(__file__).with_name("fake_mpv.py"))
pytestmark = pytest.mark.skipif(not DLCAP_BIN, reason="DLCAP_BIN not set")


class Harness:
    def __init__(self, tmp_path: Path, script: str, monkeypatch, bad_modes: str = "",
                 output_size_override: str = "3840x2160", **config):
        self.log = tmp_path / "mpv.log"
        monkeypatch.setenv("MOCK_DECKLINK_SCRIPT", script)
        monkeypatch.setenv("MOCK_FRAME_INTERVAL_MS", "4")
        monkeypatch.setenv("FAKE_MPV_LOG", str(self.log))
        monkeypatch.setenv("FAKE_MPV_BAD_MODES", bad_modes)
        self.config = Config(
            data_dir=tmp_path / "data", run_dir=tmp_path / "run", dlcap_bin=DLCAP_BIN, mpv_bin=FAKE_MPV,
            output_size=output_size_override, **config,
        )
        self.processor = Processor(self.config, PresetStore(self.config.data_dir / "presets.json"))

    def entries(self, **match):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in rows if all(r.get(k) == v for k, v in match.items())]

    async def until(self, predicate, timeout=15.0, what="condition"):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = predicate()
            if result:
                return result
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}; mpv log: {self.entries()[-15:]}")


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 90))


def test_sdr_then_hdr_switches_mode_preset_and_tagging(tmp_path, monkeypatch):
    h = Harness(tmp_path, "nosignal:20;1080p60:yuv10:sdr:150;2160p23976:yuv10:pq:100000:1000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="overlay", data="{\\an5}{\\fs42}{\\bord2}No signal"), what="no-signal overlay")

            sdr = await h.until(lambda: [e for e in h.entries(event="loadfile") if "bt.709" in e["options"]["vf"]], what="SDR load")
            assert sdr[0]["options"]["demuxer-rawvideo-w"] == "1920"
            assert h.entries(event="start")[-1]["drm_mode"] == "3840x2160@60"

            hdr = await h.until(lambda: [e for e in h.entries(event="loadfile") if "gamma=pq" in e["options"]["vf"]], what="HDR load")
            assert "max-luma=1000" in hdr[0]["options"]["vf"]
            starts = h.entries(event="start")
            assert starts[-1]["drm_mode"] == "3840x2160@23.976"
            assert len({s["pid"] for s in starts}) >= 2  # mpv restarted for the new refresh rate

            await h.until(lambda: h.entries(event="frames", path=hdr[0]["url"], frames=50), what="HDR frames")
            assert not h.entries(event="misaligned")

            status = h.processor.status()
            assert status["presets"]["active"] == "HDR to SDR"
            assert status["signal"]["colorimetry"]["kind"] == "hdr10"
            assert status["signal"]["label"].startswith("2160p23.976 · HDR10")
            assert status["output"]["playing"]
        finally:
            await h.processor.stop()

    run(scenario())


def test_unsupported_mode_falls_back(tmp_path, monkeypatch):
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch, bad_modes="3840x2160@23.976")

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            assert h.processor.status()["output"]["unsupported_modes"] == ["3840x2160@23.976"]
            assert h.entries(event="start")[-1]["drm_mode"] == "preferred"
        finally:
            await h.processor.stop()

    run(scenario())


def test_recovers_from_mpv_crash(tmp_path, monkeypatch):
    h = Harness(tmp_path, "1080p60:yuv10:sdr:100000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            first = await h.until(lambda: h.entries(event="frames", frames=10), what="first playback")
            os.kill(first[0]["pid"], signal.SIGKILL)
            again = await h.until(
                lambda: [e for e in h.entries(event="frames", frames=10) if e["pid"] != first[0]["pid"]],
                timeout=20, what="playback after crash",
            )
            assert again[0]["path"] != first[0]["path"]  # fresh segment FIFO
            assert any("exited unexpectedly" in e["message"] for e in h.processor.status()["errors"])
        finally:
            await h.processor.stop()

    run(scenario())


def test_live_settings_and_hdr_metadata_updates(tmp_path, monkeypatch):
    h = Harness(tmp_path, "2160p23976:yuv10:pq:150:1000;2160p23976:yuv10:pq:100000:4000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            await h.processor.update_settings({"target-peak": 140, "tone-mapping": "bt.2390"})
            assert h.entries(event="set", name="target-peak", value=140)
            assert h.entries(event="set", name="tone-mapping", value="bt.2390")
            with pytest.raises(ValueError):
                await h.processor.update_settings({"input-ipc-server": "/tmp/evil"})

            vf = await h.until(lambda: h.entries(event="set", name="vf"), what="live vf update")
            assert "max-luma=4000" in vf[-1]["value"]

            await h.processor.activate_preset("SDR")
            # Options the SDR preset doesn't set go back to mpv's default.
            assert h.entries(event="set", name="tone-mapping", value="default")
            await h.processor.show_message("hello", 1000)
            assert h.entries(event="show-text")
        finally:
            await h.processor.stop()
        saved = json.loads((h.config.data_dir / "presets.json").read_text())
        assert saved["presets"]["HDR to SDR"]["target-peak"] == 140

    run(scenario())


def test_signal_loss_and_return(tmp_path, monkeypatch):
    h = Harness(tmp_path, "1080p60:yuv10:sdr:150;nosignal:150;1080p60:yuv10:sdr:100000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            first = await h.until(lambda: h.entries(event="frames", frames=10), what="first playback")
            await h.until(lambda: h.entries(event="overlay", data="{\\an5}{\\fs42}{\\bord2}No signal"), what="overlay")
            await h.until(lambda: h.entries(event="end-file", reason="eof"), what="EOF on signal loss")
            await h.until(lambda: [e for e in h.entries(event="frames", frames=10) if e["path"] != first[0]["path"]],
                          what="playback after signal returns")
            assert h.entries(event="overlay", format="none")
            # Same format when the signal returns, so mpv keeps running in the same mode.
            assert {e["pid"] for e in h.entries(event="frames")} == {first[0]["pid"]}
        finally:
            await h.processor.stop()

    run(scenario())


def test_mode_switch_waits_for_mpv_to_finish_starting(tmp_path, monkeypatch):
    # Real mpv needs a moment to bring up the display; a segment that arrives
    # meanwhile must not tear mpv down halfway through its start-up.
    monkeypatch.setenv("FAKE_MPV_VO_DELAY", "0.6")
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch, output_size_override="1920x1080")

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            modes = [e["drm_mode"] for e in h.entries(event="start")]
            assert modes == ["preferred", "1920x1080@23.976"]
            assert h.processor.status()["errors"] == []
        finally:
            await h.processor.stop()

    run(scenario())
