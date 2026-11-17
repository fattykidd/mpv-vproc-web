"""The captured signal, and how to describe it to mpv.

dlcap reports each segment's geometry, pixel format and colorimetry. mpv's
rawvideo demuxer knows none of that, so we pass it as per-file options and a
`format` video filter that tags the frames with the right matrix, primaries
and transfer function. Tagging is what makes libplacebo tone-map HDR input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# mpv/libplacebo measure HDR relative to a 203 cd/m² reference white (BT.2408).
REFERENCE_WHITE_NITS = 203.0

SIGNAL_KINDS = ("sdr", "hdr10", "hlg")


@dataclass(frozen=True)
class Mastering:
    primaries: tuple[float, ...]
    white: tuple[float, ...]
    max_luminance: float
    min_luminance: float

    @classmethod
    def from_event(cls, data: dict[str, Any] | None) -> Mastering | None:
        if not data:
            return None
        return cls(
            primaries=tuple(float(v) for v in data.get("primaries", ())),
            white=tuple(float(v) for v in data.get("white", ())),
            max_luminance=float(data.get("max_luminance") or 0),
            min_luminance=float(data.get("min_luminance") or 0),
        )


@dataclass(frozen=True)
class Colorimetry:
    matrix: str = "bt709"  # bt601 | bt709 | bt2020
    transfer: str = "sdr"  # sdr | pq | hlg
    max_cll: float = 0.0
    max_fall: float = 0.0
    mastering: Mastering | None = None

    @classmethod
    def from_event(cls, data: dict[str, Any]) -> Colorimetry:
        return cls(
            matrix=str(data.get("matrix", "bt709")),
            transfer=str(data.get("transfer", "sdr")),
            max_cll=float(data.get("max_cll") or 0),
            max_fall=float(data.get("max_fall") or 0),
            mastering=Mastering.from_event(data.get("mastering")),
        )

    @property
    def kind(self) -> str:
        return {"pq": "hdr10", "hlg": "hlg"}.get(self.transfer, "sdr")

    @property
    def peak_nits(self) -> float:
        """Best available content peak, or 0 when the source doesn't say."""
        if self.transfer != "pq":
            return 0.0
        if self.max_cll > 0:
            return self.max_cll
        if self.mastering and self.mastering.max_luminance > 0:
            return self.mastering.max_luminance
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        m = self.mastering
        return {
            "kind": self.kind,
            "matrix": self.matrix,
            "transfer": self.transfer,
            "max_cll": self.max_cll,
            "max_fall": self.max_fall,
            "mastering": None
            if m is None
            else {"max_luminance": m.max_luminance, "min_luminance": m.min_luminance},
        }


@dataclass(frozen=True)
class Segment:
    id: int
    path: str
    mode: str
    width: int
    height: int
    row_bytes: int
    frame_bytes: int
    fps_num: int
    fps_den: int
    field: str  # progressive | tff | bff | psf
    pixel_format: str  # v210 | r210 | 2vuy | bgra
    signal: str  # ycbcr422 | rgb444
    bit_depth: int
    colorimetry: Colorimetry

    @classmethod
    def from_event(cls, e: dict[str, Any]) -> Segment:
        return cls(
            id=int(e["segment"]),
            path=str(e["path"]),
            mode=str(e.get("mode", "")),
            width=int(e["width"]),
            height=int(e["height"]),
            row_bytes=int(e["row_bytes"]),
            frame_bytes=int(e["frame_bytes"]),
            fps_num=int(e["fps_num"]),
            fps_den=int(e["fps_den"]),
            field=str(e.get("field", "progressive")),
            pixel_format=str(e["pixel_format"]),
            signal=str(e.get("signal", "ycbcr422")),
            bit_depth=int(e.get("bit_depth", 10)),
            colorimetry=Colorimetry.from_event(e.get("colorimetry") or {}),
        )

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den if self.fps_den else 0.0

    @property
    def interlaced(self) -> bool:
        return self.field in {"tff", "bff"}

    @property
    def output_rate(self) -> float:
        """Frames per second after deinterlacing (one frame per field)."""
        return self.fps * 2 if self.interlaced else self.fps

    @property
    def is_rgb(self) -> bool:
        return self.signal == "rgb444"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "field": self.field,
            "pixel_format": self.pixel_format,
            "signal": self.signal,
            "bit_depth": self.bit_depth,
            "label": describe(self),
        }


# ---------------------------------------------------------------------------
# mpv mapping
# ---------------------------------------------------------------------------

_MATRIX = {"bt601": "bt.601", "bt709": "bt.709", "bt2020": "bt.2020-ncl"}
_TRANSFER = {"sdr": "bt.1886", "pq": "pq", "hlg": "hlg"}

# Decoder input for each DeckLink pixel format. v210 and r210 carry row padding
# that FFmpeg's decoders understand; the 8-bit formats are tightly packed.
_RAW_FORMAT = {
    "v210": ("demuxer-rawvideo-codec", "v210"),
    "r210": ("demuxer-rawvideo-codec", "r210"),
    "2vuy": ("demuxer-rawvideo-mp-format", "uyvy422"),
    "bgra": ("demuxer-rawvideo-mp-format", "bgra"),
}


class UnsupportedSignal(ValueError):
    pass


def _primaries(segment: Segment, c: Colorimetry) -> str:
    if c.matrix == "bt2020" or c.transfer in {"pq", "hlg"}:
        return "bt.2020"
    if c.matrix == "bt601":
        # 525-line (NTSC) systems run at ~30/60 Hz, 625-line (PAL) at 25/50 Hz.
        return "bt.601-625" if round(segment.fps) in {25, 50} else "bt.601-525"
    return "bt.709"


def video_filter(segment: Segment, colorimetry: Colorimetry | None = None, rgb_range: str = "full") -> str:
    """Builds the --vf chain: tag the colorimetry, then deinterlace if needed."""
    c = colorimetry or segment.colorimetry
    params = []
    if segment.is_rgb:
        params.append(f"colorlevels={rgb_range}")
    else:
        matrix = "bt.2020-ncl" if c.transfer in {"pq", "hlg"} else _MATRIX.get(c.matrix, "bt.709")
        params += [f"colormatrix={matrix}", "colorlevels=limited"]
    params += [f"primaries={_primaries(segment, c)}", f"gamma={_TRANSFER.get(c.transfer, 'bt.1886')}"]
    if c.peak_nits > 0:
        params.append(f"sig-peak={c.peak_nits / REFERENCE_WHITE_NITS:.4f}")

    chain = ["format=" + ":".join(params)]
    if segment.interlaced:
        parity = "tff" if segment.field == "tff" else "bff"
        chain.append(f"lavfi=[bwdif=mode=send_field:parity={parity}:deint=all]")
    return ",".join(chain)


def load_options(segment: Segment, colorimetry: Colorimetry | None = None, rgb_range: str = "full") -> dict[str, str]:
    """Per-file options for `loadfile` that make mpv read a segment FIFO."""
    try:
        key, value = _RAW_FORMAT[segment.pixel_format]
    except KeyError:
        raise UnsupportedSignal(f"unsupported pixel format {segment.pixel_format!r}") from None
    if segment.pixel_format in {"2vuy", "bgra"}:
        packed = segment.width * (2 if segment.pixel_format == "2vuy" else 4)
        if segment.row_bytes != packed:
            raise UnsupportedSignal(f"{segment.pixel_format} rows are padded ({segment.row_bytes} bytes)")

    return {
        "demuxer": "rawvideo",
        "demuxer-rawvideo-w": str(segment.width),
        "demuxer-rawvideo-h": str(segment.height),
        "demuxer-rawvideo-fps": f"{segment.fps:.9f}",
        "demuxer-rawvideo-size": str(segment.frame_bytes),
        key: value,
        "vf": video_filter(segment, colorimetry, rgb_range),
    }


def describe(segment: Segment, colorimetry: Colorimetry | None = None) -> str:
    """Human-readable summary, e.g. "2160p23.976 · HDR10 · BT.2020 · 4:2:2 12-bit"."""
    c = colorimetry or segment.colorimetry
    scan = "i" if segment.interlaced else "p"
    rate = segment.fps * 2 if segment.interlaced else segment.fps
    rate_text = f"{rate:.3f}".rstrip("0").rstrip(".")
    kind = {"hdr10": "HDR10", "hlg": "HLG", "sdr": "SDR"}[c.kind]
    gamut = {"bt2020": "BT.2020", "bt601": "BT.601"}.get(c.matrix, "BT.709")
    sampling = "RGB 4:4:4" if segment.is_rgb else "4:2:2"
    parts = [f"{segment.height}{scan}{rate_text}", kind, gamut, f"{sampling} {segment.bit_depth}-bit"]
    if c.max_cll > 0:
        parts.append(f"MaxCLL {c.max_cll:g}")
    return " · ".join(parts)


def refresh_for(rate: float, refresh_map: dict[float, float]) -> float | None:
    """Display refresh for a content frame rate, or None when it isn't mapped."""
    for source, display in refresh_map.items():
        if abs(source - rate) < 0.01:
            return display
    return None


def drm_mode_for(segment: Segment | None, output_size: str, refresh_map: dict[float, float], fallback: str) -> str:
    """mpv --drm-mode value that matches the display refresh to the content."""
    if segment is None or not output_size:
        return fallback
    rate = refresh_for(segment.output_rate, refresh_map)
    if rate is None:
        return fallback
    return f"{output_size}@{rate:.3f}".rstrip("0").rstrip(".")
