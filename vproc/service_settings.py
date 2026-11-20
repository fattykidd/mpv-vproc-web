"""vproc's own settings, changeable from the web UI.

The .env file stays the starting point. A value saved in the UI is kept in
/data/service.json and takes over from .env when vproc (re)starts; resetting it
returns to the .env value. Settings Docker needs before the container starts
(group ids, device paths, ports, the image version) can only live in .env, and
the UI lists them as such.

Values are stored as the same text .env would hold, and checked by building a
Config from them, so the UI and .env accept exactly the same things.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, ConfigError

log = logging.getLogger("vproc.service_settings")

FILE_NAME = "service.json"


@dataclass(frozen=True)
class Field:
    key: str  # without the VPROC_ prefix
    attr: str  # Config attribute it sets
    label: str
    group: str
    help: str
    kind: str = "text"  # text | number | toggle | choice
    choices: tuple[str, ...] = ()
    placeholder: str = ""
    caution: str = ""  # shown before applying a change that can take the picture away


FIELDS: tuple[Field, ...] = (
    Field("OUTPUT_SIZE", "output_size", "Output size", "Display",
          "The resolution your display runs at; needed to match its refresh rate to the source.",
          placeholder="3840x2160", caution="A size the display doesn't offer can leave the screen black."),
    Field("MATCH_REFRESH", "match_refresh", "Match refresh rate", "Display",
          "Switch the display's refresh rate to suit the source (e.g. 23.976 Hz for films).", kind="toggle"),
    Field("REFRESH_MAP", "refresh_map", "Refresh-rate map", "Display",
          "Which display rate each source rate uses, e.g. 25=50 shows 25 fps at 50 Hz.",
          placeholder="23.976=23.976,24=24,25=50,29.97=59.94,30=60,50=50,59.94=59.94,60=60"),
    Field("DISPLAY_MODE", "display_mode", "Fallback display mode", "Display",
          "Mode used when the source doesn't decide one. \"preferred\" means the output size at its highest rate.",
          placeholder="preferred", caution="A mode the display doesn't offer can leave the screen black."),
    Field("VULKAN_DISPLAY", "vulkan_display", "Display number", "Display",
          "Which display to use when the graphics card has several connected (0, 1, ...).", kind="number",
          caution="Choosing another display moves the picture there."),
    Field("RGB_RANGE", "rgb_range", "RGB input range", "Input",
          "How RGB sources (PCs, consoles) are treated. Use limited if their blacks look grey.",
          kind="choice", choices=("full", "limited")),
    Field("QUEUE_DEPTH", "queue_depth", "Capture buffer (frames)", "Input",
          "Frames held between capture and display. 2 keeps latency low; more can help an overloaded system.",
          kind="number"),
    Field("LLDV", "lldv", "Recognise LLDV (Dolby Vision)", "Input",
          "Read SDR-flagged BT.2020 input as player-led Dolby Vision and tone-map it as HDR.", kind="toggle"),
    Field("LLDV_PEAK", "lldv_peak", "LLDV peak (nits)", "Input",
          "The brightness players map Dolby Vision to: the target in your EDID's Dolby Vision block.",
          kind="number", placeholder="1000"),
    Field("LIPSYNC_URLS", "lipsync_urls", "Lip-sync URLs", "Lip sync",
          "Where to send the audio delay, comma-separated, with {delay_ms} where the number goes.",
          placeholder="http://192.168.1.20:8080/api/atlas/input/1/lipsync/{delay_ms}"),
    Field("LIPSYNC_OFFSET", "lipsync_offset", "Lip-sync offset (ms)", "Lip sync",
          "Added to vproc's own delay for the display's processing. Find it with an A/V sync test video.",
          kind="number"),
    Field("LIPSYNC_METHOD", "lipsync_method", "Lip-sync HTTP method", "Lip sync",
          "How the URLs are called.", kind="choice", choices=("POST", "PUT", "GET")),
    Field("LOG_LEVEL", "log_level", "Log level", "Logging",
          "How much vproc writes to its log.", kind="choice", choices=("debug", "info", "warning", "error")),
    Field("MPV_LOG_LEVEL", "mpv_log_level", "mpv log level", "Logging",
          "How much mpv writes to vproc's log.", kind="choice", choices=("trace", "debug", "v", "info", "warn", "error", "fatal")),
    Field("MPV_EXTRA_ARGS", "mpv_extra_args", "Extra mpv options", "Advanced",
          "Extra command-line options for mpv, for experiments.", placeholder="--option=value",
          caution="A wrong option can stop the picture from starting."),
)
BY_KEY = {f.key: f for f in FIELDS}

# Only .env can set these: Docker uses them before the container starts.
HOST_ONLY = (
    ("VIDEO_GID / RENDER_GID", "Group ids that give the container the graphics card"),
    ("DECKLINK_LIB", "Where the capture card's library is on the host"),
    ("VPROC_DDC_DEVICE", "The display's DDC bus for the HDMI link watch"),
    ("VPROC_PORT / VPROC_UC_PORT", "Network ports"),
    ("VPROC_VERSION", "Which release runs"),
    ("VPROC_API_TOKEN", "The password for this page and the API"),
)


def text_of(value: Any) -> str:
    """A Config value written the way .env would hold it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict):
        return ",".join(f"{k:g}={v:g}" for k, v in value.items())
    if isinstance(value, tuple):
        return ",".join(str(v) for v in value) if value and "://" in str(value[0]) else " ".join(map(str, value))
    return str(value)


class ServiceSettings:
    def __init__(self, data_dir: Path, env: Mapping[str, str] | None = None):
        self.path = data_dir / FILE_NAME
        self.env = dict(os.environ if env is None else env)
        self.saved: dict[str, str] = self._load()

    def config(self) -> tuple[Config, str | None]:
        """The Config to run with: .env plus saved values, or .env alone if those don't validate."""
        try:
            return Config.from_env(self.merged(self.saved)), None
        except ConfigError as exc:
            return Config.from_env(self.env), f"saved settings were ignored: {exc}"

    def merged(self, saved: Mapping[str, str]) -> dict[str, str]:
        return {**self.env, **{f"VPROC_{k}": v for k, v in saved.items()}}

    def update(self, changes: Mapping[str, str | None]) -> None:
        """Saves changes (None resets a setting to .env). Raises ConfigError, saving nothing, if any is invalid."""
        unknown = [k for k in changes if k not in BY_KEY]
        if unknown:
            raise ConfigError(f"not a setting that can be changed here: {', '.join(unknown)}")
        saved = dict(self.saved)
        for key, value in changes.items():
            if value is None:
                saved.pop(key, None)
                continue
            value = value.strip()
            field = BY_KEY[key]
            if field.kind == "choice" and value not in field.choices:
                raise ConfigError(f"{field.label} must be one of: {', '.join(field.choices)}")
            saved[key] = value
        Config.from_env(self.merged(saved))  # raises ConfigError with the reason
        self.saved = saved
        self._save()

    def describe(self, running: Config) -> dict[str, Any]:
        """Everything the settings page shows."""
        defaults = Config()
        fields = []
        for f in FIELDS:
            env_value = self.env.get(f"VPROC_{f.key}")
            saved = self.saved.get(f.key)
            fields.append({
                "key": f.key, "label": f.label, "group": f.group, "help": f.help, "kind": f.kind,
                "choices": list(f.choices), "placeholder": f.placeholder, "caution": f.caution,
                "value": saved if saved is not None else env_value if env_value is not None else text_of(getattr(defaults, f.attr)),
                "source": "saved" if saved is not None else "env" if env_value is not None else "default",
                "env_value": env_value,
                "running": text_of(getattr(running, f.attr)),
            })
        pending = self._effective(running) != self._effective(self.config()[0])
        return {"fields": fields, "restart_needed": pending,
                "host_only": [{"name": n, "help": h} for n, h in HOST_ONLY]}

    @staticmethod
    def _effective(config: Config) -> dict[str, str]:
        return {f.key: text_of(getattr(config, f.attr)) for f in FIELDS}

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); using .env only", self.path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: str(v) for k, v in data.items() if k in BY_KEY and isinstance(v, (str, int, float, bool))}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.saved, indent=2, sort_keys=True))
        os.replace(tmp, self.path)
