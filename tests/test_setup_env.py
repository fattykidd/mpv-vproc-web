"""scripts/setup-env.sh fills in .env from a (simulated) host."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "setup-env.sh"

FAKE_LDCONFIG = '#!/bin/sh\necho "\tlibDeckLinkAPI.so (libc6,x86-64) => /lib64/libDeckLinkAPI.so"\n'
FAKE_DOCKER = """#!/bin/sh
case "$1 $2" in
  "info --format") echo '{"nvidia":{},"runc":{}}' ;;
  "version --format") echo 29.1.0 ;;
esac
"""


def env_values(path: Path) -> dict[str, str]:
    lines = path.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#") and "=" in line)


@pytest.fixture
def host(tmp_path):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    connector = tmp_path / "drm" / "card1-HDMI-A-1"
    connector.mkdir(parents=True)
    (connector / "status").write_text("connected\n")
    (connector / "modes").write_text("3840x2160\n1920x1080\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"ldconfig": FAKE_LDCONFIG, "docker": FAKE_DOCKER}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return tmp_path


def run(host: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, DRM_SYSFS=str(host / "drm"), PATH=f"{host / 'bin'}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(SCRIPT)], cwd=host, env=env, capture_output=True, text=True)


def test_fills_in_detected_values(host):
    run(host)
    values = env_values(host / ".env")
    assert values["VPROC_OUTPUT_SIZE"] == "3840x2160"
    assert values["DECKLINK_LIB"] == "/lib64/libDeckLinkAPI.so"
    # Every other line of the template is kept.
    assert len((host / ".env").read_text().splitlines()) == len((host / ".env.example").read_text().splitlines())


def test_rerun_keeps_a_backup_and_changes_nothing(host):
    run(host)
    first = (host / ".env").read_text()
    run(host)
    assert (host / ".env").read_text() == first
    assert (host / ".env.bak").read_text() == first


def test_no_display_is_a_problem(host):
    (host / "drm" / "card1-HDMI-A-1" / "status").write_text("disconnected\n")
    result = run(host)
    assert result.returncode == 1
    assert "no connected display" in result.stdout


def test_outdated_firmware_is_a_problem(host):
    updater = host / "bin" / "BlackmagicFirmwareUpdater"
    updater.write_text('#!/bin/sh\necho "0:\t/dev/blackmagic/io0 [DeckLink Quad HDMI Recorder]\t0x121\tPLEASE_UPDATE"\n')
    updater.chmod(0o755)
    result = run(host)
    assert result.returncode == 1
    assert "firmware needs updating" in result.stdout
