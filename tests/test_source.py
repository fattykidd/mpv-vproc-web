import pytest

from vproc.config import DEFAULT_REFRESH_MAP, Config, ConfigError
from vproc.source import Colorimetry, Segment, UnsupportedSignal, describe, drm_mode_for, load_options, video_filter


def segment(**over):
    event = {
        "segment": 3, "path": "/run/vproc/fifo/seg-3.fifo", "mode": "2160p23.98", "width": 3840, "height": 2160,
        "row_bytes": 10240, "frame_bytes": 10240 * 2160, "fps_num": 24000, "fps_den": 1001,
        "field": "progressive", "pixel_format": "v210", "signal": "ycbcr422", "bit_depth": 10,
        "colorimetry": {"matrix": "bt709", "transfer": "sdr", "max_cll": 0, "max_fall": 0, "mastering": None},
    }
    event.update(over)
    return Segment.from_event(event)


HDR10 = {
    "matrix": "bt2020", "transfer": "pq", "max_cll": 1000, "max_fall": 400,
    "mastering": {"primaries": [0.708, 0.292, 0.17, 0.797, 0.131, 0.046], "white": [0.3127, 0.329],
                  "max_luminance": 4000, "min_luminance": 0.005},
}


def test_sdr_options():
    opts = load_options(segment())
    assert opts["demuxer"] == "rawvideo"
    assert opts["demuxer-rawvideo-codec"] == "v210"
    assert opts["demuxer-rawvideo-size"] == str(10240 * 2160)
    assert opts["demuxer-rawvideo-fps"].startswith("23.976023")
    assert opts["vf"] == "format=colormatrix=bt.709:colorlevels=limited:primaries=bt.709:gamma=bt.1886"


def test_hdr10_tags_pq_and_peak_from_maxcll():
    vf = video_filter(segment(colorimetry=HDR10))
    assert "colormatrix=bt.2020-ncl" in vf and "primaries=bt.2020" in vf and "gamma=pq" in vf
    assert "max-luma=1000" in vf


def test_peak_falls_back_to_mastering_luminance():
    c = Colorimetry.from_event({**HDR10, "max_cll": 0})
    assert c.peak_nits == 4000


def test_hlg_has_no_peak_hint():
    vf = video_filter(segment(colorimetry={"matrix": "bt2020", "transfer": "hlg"}))
    assert "gamma=hlg" in vf and "sig-peak" not in vf


def test_rgb_input_uses_range_not_matrix():
    seg = segment(pixel_format="r210", signal="rgb444", row_bytes=15360, frame_bytes=15360 * 2160)
    opts = load_options(seg, rgb_range="full")
    assert opts["demuxer-rawvideo-codec"] == "r210"
    assert opts["vf"].startswith("format=colorlevels=full:primaries=bt.709")
    assert "colormatrix" not in opts["vf"]


def test_8bit_formats_use_mp_format_and_reject_padding():
    seg = segment(pixel_format="2vuy", width=1920, height=1080, row_bytes=3840, frame_bytes=3840 * 1080)
    assert load_options(seg)["demuxer-rawvideo-mp-format"] == "uyvy422"
    with pytest.raises(UnsupportedSignal):
        load_options(segment(pixel_format="2vuy", width=1920, row_bytes=4096))
    with pytest.raises(UnsupportedSignal):
        load_options(segment(pixel_format="R12B"))


def test_interlaced_gets_bwdif_and_sd_gets_601():
    seg = segment(width=720, height=480, field="bff", fps_num=30000, fps_den=1001,
                  colorimetry={"matrix": "bt601", "transfer": "sdr"})
    vf = video_filter(seg)
    assert "colormatrix=bt.601" in vf and "primaries=bt.601-525" in vf
    assert vf.endswith(",lavfi=[bwdif=mode=send_field:parity=bff:deint=all]")
    assert seg.output_rate == pytest.approx(59.94, abs=0.01)


def test_describe():
    assert describe(segment(colorimetry=HDR10, bit_depth=12)) == "2160p23.976 · HDR10 · BT.2020 · 4:2:2 12-bit · MaxCLL 1000"
    assert describe(segment(field="tff", height=1080, fps_num=30000, fps_den=1001)).startswith("1080i59.94")


def test_drm_mode_matching():
    m = DEFAULT_REFRESH_MAP
    assert drm_mode_for(segment(), "3840x2160", m, "preferred") == "3840x2160@23.976"
    assert drm_mode_for(segment(fps_num=25, fps_den=1), "3840x2160", m, "preferred") == "3840x2160@50"
    assert drm_mode_for(segment(field="tff", fps_num=30000, fps_den=1001), "1920x1080", m, "p") == "1920x1080@59.94"
    assert drm_mode_for(segment(fps_num=48, fps_den=1), "3840x2160", m, "preferred") == "preferred"
    assert drm_mode_for(segment(), "", m, "preferred") == "preferred"


def test_config_from_env():
    c = Config.from_env({"VPROC_PORT": "9000", "VPROC_MATCH_REFRESH": "no", "VPROC_REFRESH_MAP": "24=48",
                         "VPROC_MPV_EXTRA_ARGS": "--foo=1 '--bar=a b'", "VPROC_OUTPUT_SIZE": "3840x2160"})
    assert c.port == 9000 and not c.match_refresh and c.refresh_map == {24.0: 48.0}
    assert c.mpv_extra_args == ("--foo=1", "--bar=a b")
    with pytest.raises(ConfigError):
        Config.from_env({"VPROC_OUTPUT_SIZE": "4k"})
    with pytest.raises(ConfigError):
        Config.from_env({"VPROC_MATCH_REFRESH": "maybe"})
