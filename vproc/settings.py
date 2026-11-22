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
    about: str = ""  # a fuller explanation, shown when hovering over or tapping the setting's name

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
        if self.about:
            d["about"] = self.about
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
                    "hable", "gamma", "linear", "clip"),
           about=("spline (the default) is smooth and natural; bt.2390 is the industry curve and a little more "
                  "conservative; st2094-40 and st2094-10 follow the dynamic HDR standards; reinhard, mobius and "
                  "hable are older curves; clip cuts highlights off without compressing them.")),
    Option("tone-mapping-param", "Curve parameter", "Tone mapping", "number",
           "Curve-specific knob; 0 uses the curve's default.", minimum=0, maximum=10, step=0.01,
           about=("What it changes depends on the curve, e.g. the knee or contrast of the spline. Leave it at 0 "
                  "unless you are fine-tuning a particular curve.")),
    Option("target-peak", "Display peak (nits)", "Tone mapping", "number",
           "Measured peak brightness of the screen. Projectors: typically 50–200.",
           minimum=10, maximum=10000, step=1, keywords=("auto",),
           about=("Measure it with a meter on a white window. Too low a value makes the picture bright but "
                  "flattens highlights; too high makes it dim with more highlight detail.")),
    Option("target-contrast", "Display contrast", "Tone mapping", "number",
           "Native contrast ratio, used to place black.",
           minimum=10, maximum=1000000, step=1, keywords=("auto", "inf"),
           about=("Used to place black, so shadow detail isn't crushed on displays with lifted blacks. Use the "
                  "measured native contrast; inf suits OLED and auto trusts the display.")),
    Option("hdr-compute-peak", "Dynamic peak detection", "Tone mapping", "choice",
           "Measure every frame's brightness (madVR-style) instead of trusting metadata.",
           choices=("auto", "yes", "no"),
           about=("Measures how bright each frame really is and tone-maps for that frame, instead of using one "
                  "fixed value from the metadata for the whole film. yes is recommended.")),
    Option("hdr-peak-percentile", "Peak percentile", "Tone mapping", "number",
           "Ignore the brightest specks when measuring; 99.995 is a good start.",
           minimum=0, maximum=100, step=0.001,
           about=("100 uses the single brightest pixel. Slightly lower ignores tiny specular specks so they don't "
                  "dim the whole scene.")),
    Option("hdr-peak-decay-rate", "Peak decay rate", "Tone mapping", "number",
           "How quickly the measured peak follows scene changes.", minimum=0, maximum=1000, step=1,
           about=("Higher values smooth the measured peak over more frames, so brightness changes are gentler but "
                  "slower to follow the picture.")),
    Option("hdr-scene-threshold-low", "Scene threshold low", "Tone mapping", "number",
           minimum=0, maximum=20, step=0.1,
           help="Brightness change between frames that starts a faster adjustment.",
           about="Below this change the peak drifts slowly; between this and the high threshold it adjusts faster."),
    Option("hdr-scene-threshold-high", "Scene threshold high", "Tone mapping", "number",
           minimum=0, maximum=20, step=0.1,
           help="Brightness change between frames treated as a scene cut.",
           about=("Above this change the measured peak resets at once, so a cut from a dark scene to a bright one "
                  "isn't blown out.")),
    Option("hdr-contrast-recovery", "Contrast recovery", "Tone mapping", "number",
           "Restores detail lost in compressed highlights.", minimum=0, maximum=2, step=0.01,
           about=("Puts back fine contrast that tone mapping squeezes out of highlights. Around 0.3 is subtle; "
                  "much higher can add halos.")),
    Option("tone-mapping-max-boost", "Max boost", "Tone mapping", "number",
           "Allow brightening dark scenes, up to this factor.", minimum=1, maximum=10, step=0.1,
           about=("1 never makes a scene brighter than the source intends. Higher values lift dark scenes on dim "
                  "displays.")),
    Option("inverse-tone-mapping", "Inverse tone mapping", "Tone mapping", "toggle",
           "Expand SDR sources toward an HDR display's range.",
           about="Only for HDR displays: stretches SDR into the HDR range. Leave it off when tone mapping to SDR."),
    Option("tone-mapping-visualize", "Visualize curve", "Tone mapping", "toggle",
           "Overlay the tone curve and measured peak (diagnostics).",
           about="Draws the tone curve and the measured peak over the picture. For setting up only."),
    Option("gamut-mapping-mode", "Gamut mapping", "Tone mapping", "choice",
           "How colors outside the display gamut are handled.",
           choices=("auto", "perceptual", "relative", "saturation", "absolute", "desaturate", "darken",
                    "clip", "linear", "warn"),
           about=("perceptual (recommended) gently compresses colours the display can't show; relative and clip "
                  "cut them off; darken and desaturate trade brightness or colour; warn highlights the pixels that"
                  " are out of gamut.")),

    # Display
    Option("target-prim", "Display primaries", "Display", "choice",
           "Gamut the screen is calibrated to.", choices=_PRIMARIES,
           about=("bt.709 for a display calibrated to the HD standard. Choose dci-p3 or bt.2020 only if the "
                  "display is calibrated to that wider gamut.")),
    Option("target-trc", "Display transfer", "Display", "choice",
           "Gamma the screen is calibrated to (bt.1886 or gamma2.4 for projectors).", choices=_TRANSFERS,
           about=("bt.1886 suits most projectors and dark rooms; gamma2.4 is close to it; gamma2.2 or srgb suit "
                  "brighter rooms.")),
    Option("dither-depth", "Dither depth", "Display", "choice", choices=("auto", "no", "8", "10"),
           help="Bit depth to dither to; auto uses the output's depth.",
           about=("Dithering hides banding when the picture is reduced to the output's bit depth. auto picks the "
                  "right depth; no turns dithering off.")),
    Option("dither", "Dither algorithm", "Display", "choice", choices=("fruit", "ordered", "error-diffusion", "no"),
           help="How the picture is dithered down to the output's bit depth.",
           about=("fruit (the default) is fine-grained and invisible; error-diffusion is a touch better but costs "
                  "much more GPU time; ordered is the cheapest.")),

    # Scaling
    Option("scale", "Luma upscaler", "Scaling", "choice", choices=_SCALERS,
           help="How the picture is enlarged, e.g. 1080p sources on a 4K display.",
           about=("ewa_lanczossharp is sharp and high quality; spline36 is a lighter alternative; bilinear is the "
                  "cheapest. Only used when the source is smaller than the display.")),
    Option("cscale", "Chroma upscaler", "Scaling", "choice", choices=_SCALERS,
           help="How colour detail is enlarged to full resolution (4:2:2 sources).",
           about=("Most video carries colour at half resolution. ewa_lanczossharp or spline36 keep colour edges "
                  "crisp.")),
    Option("dscale", "Downscaler", "Scaling", "choice", choices=_SCALERS,
           help="How the picture is shrunk when the source is larger than the display.",
           about="mitchell and hermite are soft and free of artefacts; lanczos is sharper but can ring."),
    Option("scale-antiring", "Anti-ringing", "Scaling", "number", minimum=0, maximum=1, step=0.05,
           help="Reduces the halos sharp scalers leave around edges.",
           about="0 is off; 0.5 to 0.8 removes most halos from lanczos-type scalers with little softening."),
    Option("sigmoid-upscaling", "Sigmoid upscaling", "Scaling", "toggle",
           help="Upscale in a way that limits ringing around bright edges.",
           about=("Reduces the dark and bright fringes sharp scalers can draw around high-contrast edges. "
                  "Recommended on.")),
    Option("correct-downscaling", "Correct downscaling", "Scaling", "toggle",
           help="Keep downscaling sharp and alias-free.",
           about="Widens the filter when shrinking the picture, avoiding aliasing and shimmer. Recommended on."),
    Option("linear-downscaling", "Linear-light downscaling", "Scaling", "toggle",
           help="Downscale in linear light for more faithful fine detail.",
           about=("Shrinks the picture in linear light, which keeps the brightness of fine detail right. Can look "
                  "harsher on some content.")),

    # Debanding
    Option("deband", "Deband", "Debanding", "toggle",
           help="Smooths the stepped bands compression leaves in skies and gradients.",
           about=("Smooths banding in skies, fog and dark gradients that compression leaves behind. Mostly helps "
                  "streamed and lower-bitrate sources.")),
    Option("deband-iterations", "Iterations", "Debanding", "number", minimum=1, maximum=16, step=1,
           help="Debanding passes; more removes stronger banding.",
           about="1 is light; 4 handles heavy banding. Each pass costs a little GPU time."),
    Option("deband-threshold", "Threshold", "Debanding", "number", minimum=0, maximum=4096, step=1,
           help="Debanding strength; higher also starts to soften fine detail.",
           about=("Higher values remove stronger banding but begin to blur fine, low-contrast detail. Around 48 to"
                  " 64 is typical.")),
    Option("deband-range", "Range", "Debanding", "number", minimum=1, maximum=64, step=1,
           help="How far debanding looks around each pixel.",
           about="Larger values catch wider bands. Around 16 is typical."),
    Option("deband-grain", "Grain", "Debanding", "number", minimum=0, maximum=4096, step=1,
           help="Noise added to hide any remaining banding; 0 keeps the picture clean.",
           about="A little grain hides any banding left over. 0 keeps the picture clean."),

    # Picture
    Option("brightness", "Brightness", "Picture", "number", minimum=-100, maximum=100, step=1,
           help="Raises or lowers black and everything above it. 0 leaves it unchanged.",
           about=("Like a TV's brightness control. Better set at the display or source if you can; use it here to "
                  "trim black.")),
    Option("contrast", "Contrast", "Picture", "number", minimum=-100, maximum=100, step=1,
           help="Stretches or flattens the picture around mid-grey. 0 leaves it unchanged.",
           about="Like a TV's contrast control. Set the display's own contrast first."),
    Option("saturation", "Saturation", "Picture", "number", minimum=-100, maximum=100, step=1,
           help="Colour intensity. 0 leaves it unchanged; -100 is black and white.",
           about="Like a TV's colour control. Keep it at 0 for an accurate picture."),
    Option("gamma", "Gamma", "Picture", "number", minimum=-100, maximum=100, step=1,
           help="Brightens or darkens mid-tones without moving black or white. 0 leaves it unchanged.",
           about="Brightens or darkens mid-tones only. Small values make a visible difference."),
    Option("hue", "Hue", "Picture", "number", minimum=-100, maximum=100, step=1,
           help="Rotates all colours. 0 leaves it unchanged.",
           about="Like a TV's tint control. Keep it at 0 for an accurate picture."),

    # Geometry (constant-height projection, overscan)
    Option("video-aspect-override", "Aspect override", "Geometry", "choice",
           choices=("no", "16:9", "4:3", "1.85:1", "2:1", "2.35:1", "2.39:1", "2.40:1"),
           help="Show the picture at a fixed aspect ratio.",
           about="For anamorphic lenses or a constant-height screen. no shows the source's own aspect."),
    Option("video-zoom", "Zoom (log2)", "Geometry", "number", minimum=-2, maximum=2, step=0.01,
           help="Enlarges or shrinks the picture; 0 is the normal size.",
           about=("On a log2 scale: 0.25 is about 19% larger and -0.25 about 16% smaller. Used with a constant-"
                  "height screen.")),
    Option("video-pan-y", "Vertical shift", "Geometry", "number", minimum=-1, maximum=1, step=0.005,
           help="Moves the picture up or down.",
           about="-1 to 1, as a share of the picture height. Positions a cropped picture on the screen."),
    Option("panscan", "Pan & scan", "Geometry", "number", minimum=0, maximum=1, step=0.01,
           help="Crops the picture to fill the screen; 0 shows it whole.",
           about="1 crops fully to fill the screen; 0 shows the whole picture with black bars."),
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
