"""Runtime configuration, read once from VPROC_* environment variables."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Source frame rate -> display refresh rate. Rates most displays can't show
# natively (25, 30) are doubled so every frame is repeated evenly.
DEFAULT_REFRESH_MAP: dict[float, float] = {
    23.976: 23.976,
    24.0: 24.0,
    25.0: 50.0,
    29.97: 59.94,
    30.0: 60.0,
    50.0: 50.0,
    59.94: 59.94,
    60.0: 60.0,
}

OFFSET_MIN, OFFSET_MAX = -1000, 1000  # ms, the lip-sync offset's range


class ConfigError(ValueError):
    """An environment variable holds a value we can't use."""


def _bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def _int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None


def _float(value: str, name: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}") from None


def _refresh_map(value: str, name: str) -> dict[float, float]:
    """Parses "23.976=23.976,25=50,..." into a source->display rate map."""
    result: dict[float, float] = {}
    for item in filter(None, (p.strip() for p in value.split(","))):
        try:
            src, dst = item.split("=", 1)
            result[float(src)] = float(dst)
        except ValueError:
            raise ConfigError(f"{name}: expected source=display pairs, got {item!r}") from None
    return result


@dataclass(frozen=True)
class Config:
    # Web/API
    host: str = "0.0.0.0"
    port: int = 8000
    api_token: str = ""

    # Paths
    data_dir: Path = Path("/data")
    run_dir: Path = Path("/run/vproc")
    web_dir: Path = Path(__file__).resolve().parent.parent / "web"

    # Capture
    dlcap_bin: str = "dlcap"
    device: str = "0"
    queue_depth: int = 2

    # Output (mpv). Vulkan direct display by default; OpenGL over DRM/GBM is the alternative.
    mpv_bin: str = "mpv"
    gpu_api: str = "vulkan"
    gpu_context: str = "displayvk"
    display_mode: str = "preferred"  # "preferred", WIDTHxHEIGHT or WIDTHxHEIGHT@RATE
    vulkan_display: int = 0
    drm_device: str = ""
    drm_connector: str = ""
    drm_format: str = "xrgb8888"
    output_size: str = ""  # e.g. 3840x2160; required for refresh-rate matching
    match_refresh: bool = True
    refresh_map: dict[float, float] = field(default_factory=lambda: dict(DEFAULT_REFRESH_MAP))
    rgb_range: str = "full"  # how to treat RGB input: full | limited
    # Treat SDR-flagged BT.2020 input as LLDV (player-led Dolby Vision without an HDR
    # InfoFrame) mapped to lldv_peak nits: the Dolby Vision block in the source's EDID.
    lldv: bool = False
    lldv_peak: float = 1000.0
    # Have the capture card's HDMI EDID offer HLG as well as SDR and HDR10. Off leaves
    # the card's own EDID untouched (which leaves HLG out).
    edid_hlg: bool = True
    # Keep an AV processor's audio delay matched to vproc's video delay (see lipsync.py).
    lipsync_urls: tuple[str, ...] = ()
    lipsync_offset: int = 0  # ms added for the display's own processing; may be negative
    lipsync_method: str = "POST"
    ddc_device: Path = Path("/dev/vproc-ddc")  # display's DDC bus, for the HDMI link watch
    mpv_log_level: str = "warn"
    mpv_extra_args: tuple[str, ...] = ()

    # Unfolded Circle Remote integration (switched on and off in the web UI)
    uc_port: int = 9095

    log_level: str = "info"

    @property
    def mpv_socket(self) -> Path:
        return self.run_dir / "mpv.sock"

    @property
    def fifo_dir(self) -> Path:
        return self.run_dir / "fifo"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def get(key: str) -> str | None:
            return env.get(f"VPROC_{key}")

        kw: dict = {}
        strings = {
            "HOST": "host", "API_TOKEN": "api_token", "DLCAP_BIN": "dlcap_bin", "DEVICE": "device",
            "MPV_BIN": "mpv_bin", "GPU_API": "gpu_api", "GPU_CONTEXT": "gpu_context",
            "DRM_DEVICE": "drm_device", "DRM_CONNECTOR": "drm_connector", "DRM_FORMAT": "drm_format",
            "OUTPUT_SIZE": "output_size", "RGB_RANGE": "rgb_range",
            "MPV_LOG_LEVEL": "mpv_log_level", "LOG_LEVEL": "log_level",
        }
        for key, attr in strings.items():
            if (v := get(key)) is not None:
                kw[attr] = v.strip()
        for key, attr in {"DATA_DIR": "data_dir", "RUN_DIR": "run_dir", "WEB_DIR": "web_dir"}.items():
            if (v := get(key)) is not None:
                kw[attr] = Path(v)
        # VPROC_DRM_MODE is the older name for VPROC_DISPLAY_MODE.
        if (v := get("DISPLAY_MODE") or get("DRM_MODE")) is not None:
            kw["display_mode"] = v.strip()
        integers = {"PORT": "port", "QUEUE_DEPTH": "queue_depth", "VULKAN_DISPLAY": "vulkan_display", "UC_PORT": "uc_port"}
        for key, attr in integers.items():
            if (v := get(key)) is not None:
                kw[attr] = _int(v, f"VPROC_{key}")
        if (v := get("LLDV")) is not None:
            kw["lldv"] = _bool(v, "VPROC_LLDV")
        if (v := get("EDID_HLG")) is not None:
            kw["edid_hlg"] = _bool(v, "VPROC_EDID_HLG")
        if (v := get("LLDV_PEAK")) is not None:
            kw["lldv_peak"] = _float(v, "VPROC_LLDV_PEAK")
        if (v := get("LIPSYNC_URLS")) is not None:
            kw["lipsync_urls"] = tuple(u.strip() for u in v.split(",") if u.strip())
        if (v := get("LIPSYNC_OFFSET")) is not None:
            kw["lipsync_offset"] = _int(v, "VPROC_LIPSYNC_OFFSET")
        if (v := get("LIPSYNC_METHOD")) is not None:
            kw["lipsync_method"] = v.strip().upper()
        if (v := get("MATCH_REFRESH")) is not None:
            kw["match_refresh"] = _bool(v, "VPROC_MATCH_REFRESH")
        if (v := get("REFRESH_MAP")) is not None:
            kw["refresh_map"] = _refresh_map(v, "VPROC_REFRESH_MAP")
        if (v := get("MPV_EXTRA_ARGS")) is not None:
            kw["mpv_extra_args"] = tuple(shlex.split(v))

        config = cls(**kw)
        config.validate()
        return config

    def validate(self) -> None:
        if self.rgb_range not in {"full", "limited"}:
            raise ConfigError("VPROC_RGB_RANGE must be full or limited")
        if self.output_size and not _is_size(self.output_size):
            raise ConfigError("VPROC_OUTPUT_SIZE must look like 3840x2160")
        if self.gpu_context == "displayvk" and self.gpu_api != "vulkan":
            raise ConfigError("VPROC_GPU_CONTEXT=displayvk needs VPROC_GPU_API=vulkan")
        if not 100 <= self.lldv_peak <= 10000:
            raise ConfigError("VPROC_LLDV_PEAK must be between 100 and 10000 (nits)")
        for url in self.lipsync_urls:
            if not url.startswith(("http://", "https://")) or "{delay_ms}" not in url:
                raise ConfigError(f"VPROC_LIPSYNC_URLS: {url!r} must be an http(s) URL containing {{delay_ms}}")
        if self.lipsync_method not in {"POST", "PUT", "GET"}:
            raise ConfigError("VPROC_LIPSYNC_METHOD must be POST, PUT or GET")
        if not OFFSET_MIN <= self.lipsync_offset <= OFFSET_MAX:
            raise ConfigError(f"VPROC_LIPSYNC_OFFSET must be between {OFFSET_MIN} and {OFFSET_MAX} (ms)")
        if not 1 <= self.queue_depth <= 16:
            raise ConfigError("VPROC_QUEUE_DEPTH must be between 1 and 16")


def _is_size(value: str) -> bool:
    w, _, h = value.partition("x")
    return w.isdigit() and h.isdigit()
