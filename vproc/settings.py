"""Picture settings: which mpv options may be changed, and saved presets.

Only options listed in SCHEMA can be set through the API, and every value is
validated against its definition before it reaches mpv.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .source import SIGNAL_KINDS

log = logging.getLogger("vproc.settings")

Value = str | int | float | bool


@dataclass(frozen=True)
class Option:
    name: str
    label: str
    group: str
    kind: str  # choice | number | toggle
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    keywords: tuple[str, ...] = ()  # non-numeric values a number option accepts, e.g. "auto"

    def validate(self, value: Any) -> Value:
        if self.kind == "toggle":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in {"yes", "no", "true", "false"}:
                return value.lower() in {"yes", "true"}
            raise ValueError(f"{self.name} must be true or false")

        if self.kind == "choice":
            if isinstance(value, str) and value in self.choices:
                return value
            raise ValueError(f"{self.name} must be one of: {', '.join(self.choices)}")

        if isinstance(value, str) and value in self.keywords:
            return value
        if isinstance(value, bool):
            raise ValueError(f"{self.name} must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.name} must be a number") from None
        if self.minimum is not None and number < self.minimum:
            raise ValueError(f"{self.name} must be at least {self.minimum:g}")
        if self.maximum is not None and number > self.maximum:
            raise ValueError(f"{self.name} must be at most {self.maximum:g}")
        return int(number) if self.step is not None and float(self.step).is_integer() else number

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "label": self.label, "group": self.group, "kind": self.kind}
        if self.help:
            d["help"] = self.help
        if self.choices:
            d["choices"] = list(self.choices)
        for key in ("minimum", "maximum", "step"):
            if (v := getattr(self, key)) is not None:
                d[key] = v
        if self.keywords:
            d["keywords"] = list(self.keywords)
        return d


_SCALERS = (
    "bilinear", "bicubic", "catmull_rom", "mitchell", "hermite", "spline16", "spline36", "spline64",
    "lanczos", "ewa_lanczos", "ewa_lanczossharp", "ewa_lanczos4sharpest", "ewa_hanning",
)
_PRIMARIES = (
    "auto", "bt.709", "bt.2020", "dci-p3", "display-p3", "bt.601-525", "bt.601-625", "adobe", "apple",
)
_TRANSFERS = ("auto", "bt.1886", "srgb", "gamma1.8", "gamma2.0", "gamma2.2", "gamma2.4", "gamma2.6", "linear", "pq", "hlg")

SCHEMA: tuple[Option, ...] = (
    # Tone and gamut mapping
    Option("tone-mapping", "Tone-mapping curve", "Tone mapping", "choice",
           "How HDR highlights are compressed into the display's range.",
           choices=("auto", "spline", "bt.2390", "bt.2446a", "st2094-40", "st2094-10", "reinhard", "mobius",
                    "hable", "gamma", "linear", "clip")),
    Option("tone-mapping-param", "Curve parameter", "Tone mapping", "number",
           "Curve-specific knob; 0 uses the curve's default.", minimum=0, maximum=10, step=0.01),
    Option("target-peak", "Display peak (nits)", "Tone mapping", "number",
           "Measured peak brightness of the screen. Projectors: typically 50–200.",
           minimum=10, maximum=10000, step=1, keywords=("auto",)),
    Option("target-contrast", "Display contrast", "Tone mapping", "number",
           "Native contrast ratio, used to place black.",
           minimum=10, maximum=1000000, step=1, keywords=("auto", "inf")),
    Option("hdr-compute-peak", "Dynamic peak detection", "Tone mapping", "choice",
           "Measure every frame's brightness (madVR-style) instead of trusting metadata.",
           choices=("auto", "yes", "no")),
    Option("hdr-peak-percentile", "Peak percentile", "Tone mapping", "number",
           "Ignore the brightest specks when measuring; 99.995 is a good start.",
           minimum=0, maximum=100, step=0.001),
    Option("hdr-peak-decay-rate", "Peak decay rate", "Tone mapping", "number",
           "How quickly the measured peak follows scene changes.", minimum=0, maximum=1000, step=1),
    Option("hdr-scene-threshold-low", "Scene threshold low", "Tone mapping", "number",
           minimum=0, maximum=20, step=0.1),
    Option("hdr-scene-threshold-high", "Scene threshold high", "Tone mapping", "number",
           minimum=0, maximum=20, step=0.1),
    Option("hdr-contrast-recovery", "Contrast recovery", "Tone mapping", "number",
           "Restores detail lost in compressed highlights.", minimum=0, maximum=2, step=0.01),
    Option("tone-mapping-max-boost", "Max boost", "Tone mapping", "number",
           "Allow brightening dark scenes, up to this factor.", minimum=1, maximum=10, step=0.1),
    Option("inverse-tone-mapping", "Inverse tone mapping", "Tone mapping", "toggle",
           "Expand SDR sources toward an HDR display's range."),
    Option("tone-mapping-visualize", "Visualize curve", "Tone mapping", "toggle",
           "Overlay the tone curve and measured peak (diagnostics)."),
    Option("gamut-mapping-mode", "Gamut mapping", "Tone mapping", "choice",
           "How colors outside the display gamut are handled.",
           choices=("auto", "perceptual", "relative", "saturation", "absolute", "desaturate", "darken",
                    "clip", "linear", "warn")),

    # Display
    Option("target-prim", "Display primaries", "Display", "choice",
           "Gamut the screen is calibrated to.", choices=_PRIMARIES),
    Option("target-trc", "Display transfer", "Display", "choice",
           "Gamma the screen is calibrated to (bt.1886 or gamma2.4 for projectors).", choices=_TRANSFERS),
    Option("dither-depth", "Dither depth", "Display", "choice", choices=("auto", "no", "8", "10")),
    Option("dither", "Dither algorithm", "Display", "choice", choices=("fruit", "ordered", "error-diffusion", "no")),

    # Scaling
    Option("scale", "Luma upscaler", "Scaling", "choice", choices=_SCALERS),
    Option("cscale", "Chroma upscaler", "Scaling", "choice", choices=_SCALERS),
    Option("dscale", "Downscaler", "Scaling", "choice", choices=_SCALERS),
    Option("scale-antiring", "Anti-ringing", "Scaling", "number", minimum=0, maximum=1, step=0.05),
    Option("sigmoid-upscaling", "Sigmoid upscaling", "Scaling", "toggle"),
    Option("correct-downscaling", "Correct downscaling", "Scaling", "toggle"),
    Option("linear-downscaling", "Linear-light downscaling", "Scaling", "toggle"),

    # Debanding
    Option("deband", "Deband", "Debanding", "toggle"),
    Option("deband-iterations", "Iterations", "Debanding", "number", minimum=1, maximum=16, step=1),
    Option("deband-threshold", "Threshold", "Debanding", "number", minimum=0, maximum=4096, step=1),
    Option("deband-range", "Range", "Debanding", "number", minimum=1, maximum=64, step=1),
    Option("deband-grain", "Grain", "Debanding", "number", minimum=0, maximum=4096, step=1),

    # Picture
    Option("brightness", "Brightness", "Picture", "number", minimum=-100, maximum=100, step=1),
    Option("contrast", "Contrast", "Picture", "number", minimum=-100, maximum=100, step=1),
    Option("saturation", "Saturation", "Picture", "number", minimum=-100, maximum=100, step=1),
    Option("gamma", "Gamma", "Picture", "number", minimum=-100, maximum=100, step=1),
    Option("hue", "Hue", "Picture", "number", minimum=-100, maximum=100, step=1),

    # Geometry (constant-height projection, overscan)
    Option("video-aspect-override", "Aspect override", "Geometry", "choice",
           choices=("no", "16:9", "4:3", "1.85:1", "2:1", "2.35:1", "2.39:1", "2.40:1")),
    Option("video-zoom", "Zoom (log2)", "Geometry", "number", minimum=-2, maximum=2, step=0.01),
    Option("video-pan-y", "Vertical shift", "Geometry", "number", minimum=-1, maximum=1, step=0.005),
    Option("panscan", "Pan & scan", "Geometry", "number", minimum=0, maximum=1, step=0.01),
)

OPTIONS: dict[str, Option] = {o.name: o for o in SCHEMA}


def validate_values(values: dict[str, Any]) -> dict[str, Value]:
    """Validates a partial settings dict; raises ValueError naming the first problem."""
    out: dict[str, Value] = {}
    for name, value in values.items():
        option = OPTIONS.get(name)
        if option is None:
            raise ValueError(f"unknown setting {name!r}")
        out[name] = option.validate(value)
    return out


# Starting points. Every value can be changed from the web UI.
DEFAULT_PRESETS: dict[str, dict[str, Value]] = {
    "SDR": {
        "target-prim": "bt.709",
        "target-trc": "bt.1886",
        "scale": "ewa_lanczossharp",
        "cscale": "ewa_lanczossharp",
        "dscale": "mitchell",
        "correct-downscaling": True,
        "sigmoid-upscaling": True,
        "dither-depth": "auto",
    },
    "HDR to SDR": {
        "tone-mapping": "spline",
        "target-peak": 100,
        "target-prim": "bt.709",
        "target-trc": "bt.1886",
        "hdr-compute-peak": "yes",
        "hdr-peak-percentile": 99.995,
        "hdr-contrast-recovery": 0.3,
        "gamut-mapping-mode": "perceptual",
        "scale": "ewa_lanczossharp",
        "cscale": "ewa_lanczossharp",
        "dscale": "mitchell",
        "correct-downscaling": True,
        "sigmoid-upscaling": True,
        "dither-depth": "auto",
    },
}
DEFAULT_AUTO = {"sdr": "SDR", "hdr10": "HDR to SDR", "hlg": "HDR to SDR"}

_NAME = re.compile(r"^[\w .+()-]{1,40}$")


def check_preset_name(name: str) -> str:
    name = name.strip()
    if not _NAME.fullmatch(name):
        raise ValueError("preset names are 1–40 letters, digits, spaces or . + - ( )")
    return name


@dataclass
class PresetState:
    presets: dict[str, dict[str, Value]] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_PRESETS.items()})
    active: str = "SDR"
    auto: dict[str, str | None] = field(default_factory=lambda: dict(DEFAULT_AUTO))
    auto_switch: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"presets": self.presets, "active": self.active, "auto": self.auto, "auto_switch": self.auto_switch}


class PresetStore:
    """Presets persisted as JSON, written atomically so a power cut can't corrupt them."""

    def __init__(self, path: Path):
        self.path = path
        self.state = self._load()

    def _load(self) -> PresetState:
        if not self.path.exists():
            return PresetState()
        try:
            raw = json.loads(self.path.read_text())
            presets = {}
            for name, values in raw.get("presets", {}).items():
                try:
                    presets[check_preset_name(name)] = validate_values(values)
                except ValueError as exc:
                    log.warning("dropping invalid preset %r: %s", name, exc)
            if not presets:
                return PresetState()
            auto = {k: (v if v in presets else None) for k, v in raw.get("auto", {}).items() if k in SIGNAL_KINDS}
            active = raw.get("active")
            return PresetState(
                presets=presets,
                active=active if active in presets else next(iter(presets)),
                auto={k: auto.get(k) for k in SIGNAL_KINDS},
                auto_switch=bool(raw.get("auto_switch", True)),
            )
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); using defaults", self.path, exc)
            return PresetState()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".presets-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.state.to_dict(), f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # --- mutations (callers persist with save()) ---

    def values(self, name: str | None = None) -> dict[str, Value]:
        return dict(self.state.presets[name or self.state.active])

    def update(self, name: str, values: dict[str, Any]) -> dict[str, Value]:
        if name not in self.state.presets:
            raise KeyError(name)
        clean = validate_values(values)
        self.state.presets[name].update(clean)
        return clean

    def reset_option(self, name: str, option: str) -> None:
        if name not in self.state.presets:
            raise KeyError(name)
        self.state.presets[name].pop(option, None)

    def create(self, name: str, values: dict[str, Any] | None = None, copy_from: str | None = None) -> str:
        name = check_preset_name(name)
        if name in self.state.presets:
            raise ValueError(f"preset {name!r} already exists")
        base = dict(self.state.presets[copy_from]) if copy_from else {}
        base.update(validate_values(values or {}))
        self.state.presets[name] = base
        return name

    def rename(self, old: str, new: str) -> str:
        if old not in self.state.presets:
            raise KeyError(old)
        new = check_preset_name(new)
        if new != old and new in self.state.presets:
            raise ValueError(f"preset {new!r} already exists")
        self.state.presets = {(new if k == old else k): v for k, v in self.state.presets.items()}
        if self.state.active == old:
            self.state.active = new
        self.state.auto = {k: (new if v == old else v) for k, v in self.state.auto.items()}
        return new

    def delete(self, name: str) -> None:
        if name not in self.state.presets:
            raise KeyError(name)
        if len(self.state.presets) == 1:
            raise ValueError("the last preset can't be deleted")
        del self.state.presets[name]
        if self.state.active == name:
            self.state.active = next(iter(self.state.presets))
        self.state.auto = {k: (None if v == name else v) for k, v in self.state.auto.items()}

    def set_auto(self, mapping: dict[str, str | None], enabled: bool | None = None) -> None:
        for kind, preset in mapping.items():
            if kind not in SIGNAL_KINDS:
                raise ValueError(f"unknown signal type {kind!r}")
            if preset is not None and preset not in self.state.presets:
                raise KeyError(preset)
            self.state.auto[kind] = preset
        if enabled is not None:
            self.state.auto_switch = enabled
