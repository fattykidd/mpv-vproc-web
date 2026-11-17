"""Checks the container's view of the hardware: python -m vproc.doctor

Run it inside the container when something doesn't come up. Each failed check
says what to fix on the host.
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import Config, ConfigError

OK, WARN, FAIL = "ok", "warn", "FAIL"
results: list[tuple[str, str, str]] = []


def check(status: str, what: str, detail: str = "") -> None:
    results.append((status, what, detail))


def accessible(path: str) -> bool:
    return os.access(path, os.R_OK | os.W_OK)


def gpu_checks(config: Config) -> None:
    cards = sorted(glob.glob("/dev/dri/card*"))
    if not cards:
        check(FAIL, "DRM devices", "no /dev/dri/card*; pass /dev/dri into the container")
    for card in cards:
        if accessible(card):
            check(OK, f"{card} is usable")
        else:
            check(FAIL, f"{card} is not writable", "set VIDEO_GID in .env to the host's 'video' group id")
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        if not accessible(node):
            check(WARN, f"{node} is not writable", "set RENDER_GID in .env to the host's 'render' group id")

    if glob.glob("/dev/nvidia[0-9]*"):
        check(OK, "NVIDIA device nodes present")
        modeset = Path("/sys/module/nvidia_drm/parameters/modeset")
        if modeset.exists() and modeset.read_text().strip() != "Y":
            check(FAIL, "nvidia-drm modesetting is off", "add nvidia-drm.modeset=1 to the host kernel command line")
        if config.gpu_context == "drm":
            gbm = glob.glob("/usr/lib/*/gbm/nvidia-drm_gbm.so") + glob.glob("/usr/lib64/gbm/nvidia-drm_gbm.so")
            usable = any(Path(g).parent.as_posix() in os.environ.get("GBM_BACKENDS_PATH", "/usr/lib/x86_64-linux-gnu/gbm")
                         for g in gbm)
            check(OK if usable else FAIL, "NVIDIA GBM backend (only for VPROC_GPU_CONTEXT=drm)",
                  "" if usable else (f"found in {Path(gbm[0]).parent}; set GBM_BACKENDS_PATH to that folder" if gbm
                                     else "the container toolkit didn't provide it; use the default Vulkan output"))
        icd = glob.glob("/usr/share/vulkan/icd.d/nvidia*.json") + glob.glob("/etc/vulkan/icd.d/nvidia*.json")
        needed = config.gpu_api == "vulkan"
        check(OK if icd else (FAIL if needed else WARN), "NVIDIA Vulkan ICD",
              "" if icd else "NVIDIA_DRIVER_CAPABILITIES must include graphics")
    else:
        check(WARN, "no NVIDIA device nodes", "fine for AMD/Intel; for NVIDIA enable the GPU reservation in compose.yml")


def display_checks(config: Config) -> None:
    connected = []
    for status_file in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        connector = Path(status_file).parent
        if Path(status_file).read_text().strip() == "connected":
            modes = (connector / "modes").read_text().split() if (connector / "modes").exists() else []
            name = connector.name.split("-", 1)[1]
            connected.append(name)
            check(OK, f"display on {name}", "modes: " + ", ".join(dict.fromkeys(modes)) if modes else "")
    if not connected:
        check(FAIL, "no connected display", "is the projector/TV on and plugged into this GPU?")
    elif len(connected) > 1 and not config.drm_connector:
        check(WARN, "several displays connected", f"set VPROC_DRM_CONNECTOR to one of {connected}")
    if config.match_refresh and not config.output_size:
        check(WARN, "refresh-rate matching needs VPROC_OUTPUT_SIZE", "e.g. VPROC_OUTPUT_SIZE=3840x2160")


def decklink_checks(config: Config) -> None:
    if glob.glob("/dev/blackmagic/*"):
        check(OK, "Blackmagic device nodes present")
    else:
        check(FAIL, "no /dev/blackmagic devices", "install Desktop Video on the host and pass /dev/blackmagic in")
    try:
        ctypes.CDLL("libDeckLinkAPI.so")
        check(OK, "libDeckLinkAPI.so loads")
    except OSError as exc:
        check(FAIL, "libDeckLinkAPI.so does not load", f"mount it from the host ({exc})")
        return
    try:
        out = subprocess.run([config.dlcap_bin, "--list"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        check(FAIL, "dlcap --list", str(exc))
        return
    for line in out.stdout.splitlines():
        event = json.loads(line)
        if event.get("event") == "hello":
            check(OK, "Desktop Video driver", f"{event.get('driver')} (dlcap built with SDK {event.get('sdk')})")
        elif event.get("event") == "devices":
            devices = event["devices"]
            if not devices:
                check(FAIL, "no DeckLink devices found", "check the card shows up in BlackmagicFirmwareUpdater status")
            for d in devices:
                ok = d["can_capture"] and d["format_detection"]
                check(OK if ok else WARN, f"device {d['index']}: {d['name']}",
                      f"id:{d['persistent_id']}" + ("" if ok else " (no capture with format detection)"))
        elif event.get("event") == "error":
            check(FAIL, "dlcap", event.get("message", ""))


def software_checks(config: Config) -> None:
    try:
        out = subprocess.run([config.mpv_bin, "--version"], capture_output=True, text=True, timeout=10)
        lines = out.stdout.splitlines()
        check(OK, lines[0] if lines else "mpv", next((l.strip() for l in lines if "libplacebo" in l), ""))
    except (OSError, subprocess.TimeoutExpired) as exc:
        check(FAIL, "mpv", str(exc))
    data = config.data_dir
    check(OK if os.access(data, os.W_OK) else FAIL, f"{data} is writable",
          "" if os.access(data, os.W_OK) else "presets can't be saved; check the volume's ownership")


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"FAIL  configuration: {exc}")
        return 1
    gpu_checks(config)
    display_checks(config)
    decklink_checks(config)
    software_checks(config)

    for status, what, detail in results:
        print(f"{status:<5} {what}" + (f"\n      {detail}" if detail else ""))
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{failed} problem(s) found." if failed else "\nEverything needed is in place.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
