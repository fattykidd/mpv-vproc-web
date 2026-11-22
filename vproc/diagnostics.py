"""Troubleshooting aids: frame grabs and the diagnostics bundle.

A frame grab saves the picture mpv is showing as a PNG in the data volume. The
diagnostics bundle is a zip of everything useful for a bug report: a readable
summary, the full status, settings, presets, versions and the recent log. The
API token is never included.
"""

from __future__ import annotations

import collections
import dataclasses
import io
import json
import logging
import re
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_LINES = 5000  # kept in memory for the bundle: several hours at the default log level

GRAB_DIR = "grabs"
GRAB_KINDS = {"display": "window", "source": "video"}  # our name -> mpv's screenshot mode
GRABS_KEPT = 20
GRAB_NAME = re.compile(r"^grab-\d{8}-\d{6}(-\d+)?-(display|source)\.png$")


# ---------------------------------------------------------------------- recent log

class RecentLog(logging.Handler):
    """Keeps the last LOG_LINES log lines, including mpv's and dlcap's, for the bundle."""

    def __init__(self, capacity: int = LOG_LINES) -> None:
        super().__init__()
        self._lines: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter(LOG_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def text(self) -> str:
        return "\n".join(self._lines) + "\n" if self._lines else ""


RECENT_LOG = RecentLog()


def keep_recent_log() -> None:
    """Starts keeping recent log lines. Safe to call more than once."""
    root = logging.getLogger()
    if RECENT_LOG not in root.handlers:
        root.addHandler(RECENT_LOG)


# ---------------------------------------------------------------------- frame grabs

def grab_path(data_dir: Path, kind: str) -> Path:
    """A new, unused file name for a grab, e.g. grab-20261122-191502-display.png."""
    if kind not in GRAB_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(GRAB_KINDS)}")
    folder = data_dir / GRAB_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"grab-{stamp}-{kind}.png"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"grab-{stamp}-{n}-{kind}.png"
    return path


def list_grabs(data_dir: Path) -> list[Path]:
    """Saved grabs, newest first."""
    folder = data_dir / GRAB_DIR
    if not folder.is_dir():
        return []
    grabs = [p for p in folder.iterdir() if GRAB_NAME.match(p.name)]
    return sorted(grabs, key=lambda p: p.stat().st_mtime, reverse=True)


def prune_grabs(data_dir: Path, keep: int = GRABS_KEPT) -> None:
    """Deletes all but the newest `keep` grabs, so they can't fill the disk."""
    for old in list_grabs(data_dir)[keep:]:
        old.unlink(missing_ok=True)


def find_grab(data_dir: Path, name: str) -> Path:
    """The grab with this file name. Raises KeyError for anything else, so no other file can be read."""
    path = data_dir / GRAB_DIR / name
    if not GRAB_NAME.match(name) or not path.is_file():
        raise KeyError(name)
    return path


# ---------------------------------------------------------------------- bundle

def bundle_name() -> str:
    return f"vproc-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"


def build_bundle(status: dict[str, Any], config: Config, service_settings: dict[str, Any]) -> bytes:
    """The diagnostics zip, as bytes."""
    files = {
        "summary.txt": summary(status),
        "status.json": _json(status),
        "settings.json": _json(service_settings),
        "config.json": _json(redacted(config)),
        "presets.json": _json(status.get("presets")),
        "versions.txt": versions(config),
        "log.txt": RECENT_LOG.text() or "(no log lines kept yet)\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def redacted(config: Config) -> dict[str, Any]:
    """The running configuration without secrets."""
    data = {k: str(v) if isinstance(v, Path) else v for k, v in dataclasses.asdict(config).items()}
    if data.get("api_token"):
        data["api_token"] = "(set, not included)"
    return data


def versions(config: Config) -> str:
    lines = [f"vproc {__version__}"]
    try:
        out = subprocess.run([config.mpv_bin, "--version"], capture_output=True, text=True, timeout=10)
        lines += [line for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired) as exc:
        lines.append(f"mpv --version failed: {exc}")
    return "\n".join(lines) + "\n"


def summary(status: dict[str, Any]) -> str:
    """The parts of the status that answer most questions, readable without tools."""
    signal = status.get("signal") or {}
    output = status.get("output") or {}
    capture = status.get("capture") or {}
    clock = capture.get("source_clock") or {}
    lipsync = status.get("lipsync") or {}
    device = capture.get("device") or {}
    skipped = capture.get("skipped") or {}
    rows = [
        ("vproc", f"{__version__}, running for {_duration(status.get('uptime') or 0)}"),
        ("Input signal", signal.get("label") or ("No signal" if signal.get("present") is False else "Waiting")),
        ("Capture device", device.get("name") or "not open"),
        ("Driver / dlcap", f"{capture.get('driver') or '?'} / {capture.get('dlcap') or '?'}"),
        ("Output", ("playing" if output.get("playing") else "running" if output.get("running") else "stopped")
         + (f", {output['display_mode']}" if output.get("display_mode") else "")),
        ("Display rate", f"{output['display_fps']:g} Hz" if output.get("display_fps") else "unknown"),
        ("Dropped (mpv)", output.get("dropped_frames") or 0),
        ("Skipped", f"{skipped.get('playback', 0)} during playback, {skipped.get('startup', 0)} at start"),
        ("Missed by card", capture.get("missed") or 0),
        ("Source clock", f"{clock['ppm']:+.1f} ppm over {clock['measured_for']:.0f} s"
         if clock.get("ppm") is not None else "not measured yet"),
        ("Latency", f"{output['latency_ms']} ms" if output.get("latency_ms") is not None else "unknown"),
        ("Lip sync", f"offset {lipsync.get('offset_ms', 0):+d} ms, audio delay {lipsync.get('delay_ms')} ms"
         if lipsync.get("enabled") else "off"),
        ("Preset", (status.get("presets") or {}).get("active", "?")),
    ]
    width = max(len(label) for label, _ in rows)
    text = [f"{label:<{width}}  {value}" for label, value in rows]
    errors = status.get("errors") or []
    text.append("")
    text.append(f"Recent errors ({len(errors)}):" if errors else "Recent errors: none")
    text += [f"  {_error_text(e)}" for e in errors]
    return "\n".join(text) + "\n"


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        when = error.get("time")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when)) + " " if isinstance(when, (int, float)) else ""
        return f"{stamp}{error.get('source', '')}: {error.get('message', '')}".strip()
    return str(error)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h {rest // 60} min" if hours else f"{rest // 60} min {rest % 60} s"


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"
