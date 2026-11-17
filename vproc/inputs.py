"""Which capture input to use.

VPROC_DEVICE in .env is the default. An input chosen in the web UI is kept in
/data/input.json and takes over from it; clearing that choice returns to the
.env setting. Inputs are stored by their persistent id where the card has one,
so the choice survives the card moving to another slot.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("vproc.inputs")

_SPEC = re.compile(r"(?:id:-?\d+|\d+)")


def check_spec(spec: str | None) -> None:
    """Raises ValueError unless spec is None, an index ("0") or a persistent id ("id:1234")."""
    if spec is not None and not _SPEC.fullmatch(spec):
        raise ValueError(f"not an input selector: {spec!r} (use an index like 0 or an id like id:1234)")


def spec_for(device: dict[str, Any]) -> str:
    """The selector for one entry of dlcap's device list."""
    persistent_id = device.get("persistent_id", -1)
    return f"id:{persistent_id}" if persistent_id is not None and persistent_id >= 0 else str(device["index"])


def matches(spec: str, device: dict[str, Any]) -> bool:
    """Whether a selector picks this device. Name selectors (only possible from .env) never match here."""
    if spec.startswith("id:"):
        return str(device.get("persistent_id")) == spec[3:]
    return spec.isdigit() and device.get("index") == int(spec)


class InputChoice:
    def __init__(self, path: Path, default: str):
        self.path = path
        self.default = default
        self.chosen: str | None = self._load()

    @property
    def device(self) -> str:
        """The selector dlcap is started with."""
        return self.chosen or self.default

    def set(self, spec: str | None) -> None:
        check_spec(spec)
        self.chosen = spec
        self._save()

    def _load(self) -> str | None:
        try:
            spec = json.loads(self.path.read_text()).get("device")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError) as exc:
            log.error("could not read %s (%s); using VPROC_DEVICE", self.path, exc)
            return None
        if spec is not None and not (isinstance(spec, str) and _SPEC.fullmatch(spec)):
            log.error("ignoring the input saved in %s: %r", self.path, spec)
            return None
        return spec

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"device": self.chosen}, indent=2))
        os.replace(tmp, self.path)
