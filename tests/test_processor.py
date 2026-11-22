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
    h = Harness(tmp_path, "nosignal:500;1080p60:yuv10:sdr:150;2160p23976:yuv10:pq:100000:1000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="overlay", data="{\\an5}{\\fs42}{\\bord2}No signal"), what="no-signal overlay")

            sdr = await h.until(lambda: [e for e in h.entries(event="loadfile") if "bt.709" in e["options"]["vf"]], what="SDR load")
            assert sdr[0]["options"]["demuxer-rawvideo-w"] == "1920"
            assert h.entries(event="start")[-1]["mode"] == "3840x2160@60"

            hdr = await h.until(lambda: [e for e in h.entries(event="loadfile") if "gamma=pq" in e["options"]["vf"]], what="HDR load")
            assert "max-luma=1000" in hdr[0]["options"]["vf"]
            starts = h.entries(event="start")
            assert starts[-1]["mode"] == "3840x2160@23.976"
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


def test_mode_the_display_lacks_falls_back(tmp_path, monkeypatch):
    # Like a PC monitor: no 23.976 Hz mode in the list, so mpv isn't even tried in it.
    monkeypatch.setenv("FAKE_MPV_MODES", "3840x2160@60,3840x2160@50,1920x1080@60")
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            status = h.processor.status()
            assert status["output"]["unsupported_modes"] == ["3840x2160@23.976"]
            assert any("has no 3840x2160@23.976 mode" in e["message"] for e in status["errors"])
            # Falls back to the output size at its highest rate, not the display's own preference.
            assert [e["mode"] for e in h.entries(event="start")] == ["3840x2160@60"]
        finally:
            await h.processor.stop()

    run(scenario())


def test_drm_mode_that_fails_falls_back(tmp_path, monkeypatch):
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch, bad_modes="3840x2160@23.976",
                gpu_api="opengl", gpu_context="drm")

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            assert h.processor.status()["output"]["unsupported_modes"] == ["3840x2160@23.976"]
            assert h.entries(event="start")[-1]["mode"] == "3840x2160"
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
    monkeypatch.setenv("FAKE_MPV_MODES", "1920x1080@60,1920x1080@23.976")
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch, output_size_override="1920x1080")

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            modes = [e["mode"] for e in h.entries(event="start")]
            assert modes == ["1920x1080@60", "1920x1080@23.976"]
            assert h.processor.status()["errors"] == []
        finally:
            await h.processor.stop()

    run(scenario())


def test_choosing_an_input_restarts_capture_on_it_and_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DEVICES", ",".join(f"DeckLink Quad HDMI Recorder ({n})" for n in range(1, 5)))
    h = Harness(tmp_path, "1080p60:yuv10:sdr:100000", monkeypatch)
    capture = lambda: h.processor.status()["capture"]  # noqa: E731

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: (capture()["device"] or {}).get("index") == 0, what="the first input")
            assert len(capture()["devices"]) == 4
            assert (capture()["selected"], capture()["selected_in"]) == ("0", "env")

            with pytest.raises(KeyError):
                await h.processor.select_input("id:9999")
            with pytest.raises(ValueError):
                await h.processor.select_input("Quad")

            await h.processor.select_input("id:1002")
            await h.until(lambda: (capture()["device"] or {}).get("index") == 2, what="the third input")
            assert (capture()["selected"], capture()["selected_in"]) == ("id:1002", "web")
        finally:
            await h.processor.stop()

        # A new processor (as after a restart of the container) starts on the chosen input.
        again = Processor(h.config, PresetStore(h.config.data_dir / "presets.json"))
        assert again.inputs.device == "id:1002"

    run(scenario())


def test_choosing_an_input_ends_the_wait_after_a_missing_device(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DEVICES", "DeckLink Quad HDMI Recorder (1),DeckLink Quad HDMI Recorder (2)")
    h = Harness(tmp_path, "1080p60:yuv10:sdr:100000", monkeypatch, device="id:4242")
    capture = lambda: h.processor.status()["capture"]  # noqa: E731

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: any("capture device not found" in e["message"] for e in h.processor.status()["errors"]),
                          what="the missing-device error")
            await h.processor.select_input("id:1001")
            # Well within the 30 s retry wait.
            await h.until(lambda: (capture()["device"] or {}).get("index") == 1, timeout=5, what="the chosen input")
        finally:
            await h.processor.stop()

    run(scenario())


def test_starts_at_the_output_size_when_the_display_prefers_less(tmp_path, monkeypatch):
    # Like many 4K projectors: 1080p is first ("preferred") in the mode list.
    monkeypatch.setenv("FAKE_MPV_MODES", "1920x1080@60,3840x2160@60,3840x2160@23.976,1920x1080@23.976")
    h = Harness(tmp_path, "nosignal:100000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="start"), what="mpv start")
            assert h.entries(event="start")[0]["mode"] == "3840x2160@60"
        finally:
            await h.processor.stop()

    run(scenario())


def test_output_size_the_display_lacks_falls_back_to_preferred(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MPV_MODES", "1920x1080@60,1920x1080@50")
    h = Harness(tmp_path, "nosignal:100000", monkeypatch)  # VPROC_OUTPUT_SIZE says 3840x2160

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="start"), what="mpv start")
            assert [e["mode"] for e in h.entries(event="start")] == ["preferred"]
            assert any("has no 3840x2160 mode" in e["message"] for e in h.processor.status()["errors"])
        finally:
            await h.processor.stop()

    run(scenario())


def test_restarts_an_output_that_stops_taking_frames(tmp_path, monkeypatch):
    # The first mpv freezes after 20 frames, like real mpv when the projector switches inputs.
    monkeypatch.setenv("FAKE_MPV_FREEZE", str(tmp_path / "froze"))
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frozen"), what="the frozen output")
            first_pid = h.entries(event="frozen")[0]["pid"]
            await h.until(lambda: [e for e in h.entries(event="frames", frames=10) if e["pid"] != first_pid],
                          timeout=20, what="playback on a restarted mpv")
            assert any("stopped taking frames" in e["message"] for e in h.processor.status()["errors"])
            # Same mode as before: only the output was restarted.
            assert [e["mode"] for e in h.entries(event="start")][-2:] == ["3840x2160@23.976"] * 2
        finally:
            await h.processor.stop()

    run(scenario())


class FakeDdc:
    """The display's SCDC status: locked, until `lost` is set; never answering while `off`."""

    def __init__(self):
        self.lost = False
        self.off = False
        self.reads = 0

    def status_flags(self) -> int:
        self.reads += 1
        if self.off:
            raise OSError("no answer")
        return 0x01 if self.lost else 0x0F


def test_restarts_the_output_when_the_display_loses_the_hdmi_lock(tmp_path, monkeypatch):
    import vproc.processor as processor_module

    monkeypatch.setattr(processor_module, "LINK_POLL", 0.1)
    monkeypatch.setattr(processor_module, "LINK_GRACE", 0.3)
    monkeypatch.setattr(processor_module, "STALL_RETRY", 1.0)
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch)
    ddc = FakeDdc()
    h.processor._ddc = ddc
    starts = lambda: len(h.entries(event="start"))  # noqa: E731
    link = lambda: h.processor.status()["output"]["hdmi_link"]["locked"]  # noqa: E731

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            await h.until(lambda: link() is True, what="a locked link")
            before = starts()

            # Switched off or in standby: no answer, so no action.
            ddc.off = True
            await h.until(lambda: link() is None, what="the display not answering")
            await asyncio.sleep(1.5)
            assert starts() == before
            ddc.off = False

            # The projector switched inputs and came back without its lock.
            ddc.lost = True
            await h.until(lambda: starts() > before, what="the output restart")
            assert any("lost the HDMI signal" in e["message"] for e in h.processor.status()["errors"])
            ddc.lost = False
            await h.until(lambda: link() is True, what="the link locked again")
        finally:
            await h.processor.stop()

    run(scenario())


def test_a_display_never_seen_locked_is_left_alone(tmp_path, monkeypatch):
    import vproc.processor as processor_module

    monkeypatch.setattr(processor_module, "LINK_POLL", 0.1)
    monkeypatch.setattr(processor_module, "LINK_GRACE", 0.3)
    monkeypatch.setattr(processor_module, "STALL_RETRY", 0.5)
    h = Harness(tmp_path, "2160p23976:yuv10:sdr:100000", monkeypatch)
    ddc = FakeDdc()
    ddc.lost = True  # reports "not locked" even while the picture is fine
    h.processor._ddc = ddc

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: h.entries(event="frames", frames=10), what="playback")
            started = len(h.entries(event="start"))
            reads = ddc.reads
            await h.until(lambda: ddc.reads > reads + 10, what="several readings")
            assert len(h.entries(event="start")) == started
        finally:
            await h.processor.stop()

    run(scenario())


def test_processing_latency_estimate(tmp_path, monkeypatch):
    h = Harness(tmp_path, "nosignal:2;2160p23976:yuv10:sdr:100000", monkeypatch)
    latency = lambda: h.processor.status()["output"]["latency_ms"]  # noqa: E731

    async def scenario():
        assert latency() is None  # not playing yet
        await h.processor.start()
        try:
            await h.until(lambda: latency() is not None, what="a latency estimate")
            frame, refresh = 1000 / 23.976, 1000 / 60  # the fake mpv reports a 60 Hz display
            # The frame arriving plus one refresh, and up to a full queue (depth 2) of frames on top.
            assert frame + refresh - 1 <= latency() <= 3 * frame + refresh + 1
        finally:
            await h.processor.stop()

    run(scenario())


def test_reports_frame_accounting_and_the_source_clock(tmp_path, monkeypatch):
    h = Harness(tmp_path, "nosignal:2;2160p23976:yuv10:sdr:100000", monkeypatch)
    capture = lambda: h.processor.status()["capture"]  # noqa: E731

    async def scenario():
        await h.processor.start()
        try:
            await h.until(lambda: capture()["source_clock"] is not None, what="a source clock reading")
            clock = capture()["source_clock"]
            assert clock["nominal"] == pytest.approx(24000 / 1001, abs=1e-6)
            assert clock["fps"] > 0
            assert clock["ppm"] is None  # far too early to trust
            assert set(capture()["skipped"]) == {"startup", "playback"}
            assert capture()["missed"] == 0
        finally:
            await h.processor.stop()

    run(scenario())


def test_lldv_is_read_as_hdr10_when_enabled(tmp_path, monkeypatch):
    # LLDV without an HDR InfoFrame: SDR-flagged, BT.2020.
    h = Harness(tmp_path, "nosignal:100;2160p23976:yuv10:lldv:100000", monkeypatch, lldv=True, lldv_peak=1390.0)
    signal = lambda: h.processor.status()["signal"]  # noqa: E731

    async def scenario():
        await h.processor.start()
        try:
            hdr = await h.until(lambda: [e for e in h.entries(event="loadfile") if "gamma=pq" in e["options"]["vf"]],
                                what="LLDV loaded as PQ")
            vf = hdr[0]["options"]["vf"]
            assert "primaries=bt.2020" in vf and "max-luma=1390" in vf
            assert signal()["colorimetry"]["kind"] == "hdr10" and signal()["colorimetry"]["lldv"] is True
            assert "Dolby Vision (LLDV)" in signal()["label"]
            # It counts as HDR for automatic preset switching.
            await h.until(lambda: h.processor.status()["presets"]["active"] == "HDR to SDR", what="HDR preset")
        finally:
            await h.processor.stop()

    run(scenario())


def test_lldv_is_left_alone_when_disabled(tmp_path, monkeypatch):
    h = Harness(tmp_path, "nosignal:100;2160p23976:yuv10:lldv:100000", monkeypatch)

    async def scenario():
        await h.processor.start()
        try:
            loads = await h.until(lambda: h.entries(event="loadfile"), what="a load")
            assert "gamma=pq" not in loads[0]["options"]["vf"]
            assert h.processor.status()["signal"]["colorimetry"]["kind"] == "sdr"
        finally:
            await h.processor.stop()

    run(scenario())


def test_sends_the_settled_delay_once_per_signal(tmp_path, monkeypatch):
    import vproc.processor as processor_module
    from test_lipsync import FakeProcessor

    monkeypatch.setattr(processor_module, "LIPSYNC_SETTLE", 0.5)
    avp = FakeProcessor()
    try:
        h = Harness(tmp_path, "nosignal:100;2160p23976:yuv10:sdr:100000", monkeypatch,
                    lipsync_urls=(avp.url("/api/atlas/input/1/lipsync/{delay_ms}"),), lipsync_offset=40)

        async def scenario():
            await h.processor.start()
            try:
                await h.until(lambda: avp.requests, timeout=20, what="the lip-sync request")
                await asyncio.sleep(4.5)  # two more stats events: nothing new to send
                assert len(avp.requests) == 1
                method, path = avp.requests[0]
                delay = int(path.rsplit("/", 1)[1])
                frame, refresh = 1000 / 23.976, 1000 / 60
                assert method == "POST" and delay % 5 == 0
                assert frame + refresh + 40 - 5 <= delay <= 3 * frame + refresh + 40 + 5
                assert h.processor.status()["lipsync"]["delay_ms"] == delay

                # A new offset is sent at once for the signal already playing.
                await h.processor.update_lipsync(h.processor.lipsync.urls, 300, "POST")
                assert avp.requests[1] == ("POST", path.rsplit("/", 1)[0] + f"/{delay - 40 + 300}")
                assert h.processor.status()["lipsync"]["offset_ms"] == 300
            finally:
                await h.processor.stop()

        run(scenario())
    finally:
        avp.close()
