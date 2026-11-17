"""The on/off switch for the remote driver: a real child process, persisted setting."""

import asyncio
import json
import sys

from vproc.config import Config
from vproc.integrations import RemoteIntegration

# Stands in for `python -m vproc.ucremote`: announces itself, then waits.
FAKE_DRIVER = [sys.executable, "-c", "import os, time; print('driver up', os.environ['UC_INTEGRATION_HTTP_PORT'], flush=True); time.sleep(600)"]


async def until(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


def test_toggle_starts_stops_and_remembers(tmp_path):
    config = Config(data_dir=tmp_path, uc_port=9123)
    changes = []

    async def scenario():
        remote = RemoteIntegration(config, on_change=lambda: changes.append(1), command=FAKE_DRIVER)
        assert remote.status() == {"enabled": False, "running": False, "port": 9123, "error": None}
        await remote.start()  # disabled: nothing runs
        assert not remote.running

        await remote.set_enabled(True)
        await until(lambda: remote.running)
        assert json.loads((tmp_path / "integrations.json").read_text()) == {"uc_remote": {"enabled": True}}

        await remote.set_enabled(False)
        assert not remote.running
        assert json.loads((tmp_path / "integrations.json").read_text()) == {"uc_remote": {"enabled": False}}

        await remote.set_enabled(True)
        await until(lambda: remote.running)
        await remote.stop()  # vproc shutting down: the setting stays on
        assert not remote.running

        again = RemoteIntegration(config, command=FAKE_DRIVER)
        assert again.enabled
        await again.start()
        await until(lambda: again.running)
        await again.stop()

    asyncio.run(asyncio.wait_for(scenario(), 30))
    assert changes


def test_crashed_driver_is_restarted(tmp_path):
    crash_once = [sys.executable, "-c", (
        "import pathlib, sys, time; p = pathlib.Path(sys.argv[1]);"
        "first = not p.exists(); p.touch();"
        "sys.exit(3) if first else time.sleep(600)"
    ), str(tmp_path / "ran")]

    async def scenario():
        remote = RemoteIntegration(Config(data_dir=tmp_path), command=crash_once)
        await remote.set_enabled(True)
        await until(lambda: remote.status()["error"] is not None, timeout=5)
        assert "status 3" in remote.status()["error"]
        await until(lambda: remote.running and remote.status()["error"] is None, timeout=10)
        await remote.stop()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_corrupt_settings_file_means_off(tmp_path):
    (tmp_path / "integrations.json").write_text("{nope")
    assert RemoteIntegration(Config(data_dir=tmp_path)).enabled is False
