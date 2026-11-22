"""HTTP API tests. Need FastAPI + httpx (requirements-dev.txt) and DLCAP_BIN."""

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from vproc.api import create_app  # noqa: E402
from vproc.config import Config  # noqa: E402

DLCAP_BIN = os.environ.get("DLCAP_BIN")
pytestmark = pytest.mark.skipif(not DLCAP_BIN, reason="DLCAP_BIN not set")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_DECKLINK_SCRIPT", "2160p23976:yuv10:pq:100000:1000")
    monkeypatch.setenv("FAKE_MPV_LOG", str(tmp_path / "mpv.log"))
    config = Config(
        data_dir=tmp_path / "data", run_dir=tmp_path / "run", web_dir=ROOT / "web",
        dlcap_bin=DLCAP_BIN, mpv_bin=str(ROOT / "tests" / "fake_mpv.py"), output_size="3840x2160",
        api_token="s3cret",
    )
    with TestClient(create_app(config)) as c:
        c.headers["Authorization"] = "Bearer s3cret"
        yield c


def wait_for_playing(client, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = client.get("/api/status").json()
        if s["output"]["playing"]:
            return s
        time.sleep(0.1)
    raise AssertionError("never started playing")


def test_ui_and_health_need_no_token(client):
    anon = TestClient(client.app)
    assert anon.get("/").status_code == 200
    assets = anon.get("/assets/app.js")
    assert assets.status_code == 200
    assert assets.headers["cache-control"] == "no-cache"  # so an update reaches the browser
    assert anon.get("/healthz").json()["ok"] is True
    assert anon.get("/api/status").status_code == 401


def test_status_and_settings(client):
    s = wait_for_playing(client)
    assert s["signal"]["colorimetry"]["kind"] == "hdr10"
    assert s["presets"]["active"] == "HDR to SDR"

    r = client.patch("/api/settings", json={"values": {"target-peak": 120}})
    assert r.status_code == 200 and r.json()["applied"] == {"target-peak": 120}
    assert client.patch("/api/settings", json={"values": {"input-ipc-server": "x"}}).status_code == 422
    assert client.patch("/api/settings", json={"values": {}, "extra": 1}).status_code == 422
    assert client.delete("/api/settings/target-peak").status_code == 200
    assert client.delete("/api/settings/nope").status_code == 404


def test_preset_lifecycle(client):
    assert client.post("/api/presets", json={"name": "Night", "copy_from": "HDR to SDR"}).status_code == 201
    assert client.post("/api/presets", json={"name": "Night"}).status_code == 422
    assert client.post("/api/presets/Night/activate").json() == {"active": "Night"}
    assert client.patch("/api/presets/Night", json={"name": "Late"}).status_code == 200
    assert client.put("/api/auto-switch", json={"auto": {"hdr10": "Late"}, "enabled": True}).status_code == 200
    assert client.get("/api/presets").json()["auto"]["hdr10"] == "Late"
    assert client.delete("/api/presets/Late").status_code == 200
    assert client.post("/api/presets/Late/activate").status_code == 404


def test_osd_and_websocket(client):
    wait_for_playing(client)
    assert client.post("/api/osd", json={"message": "hi"}).status_code == 200
    with client.websocket_connect("/api/ws?token=s3cret") as ws:
        assert "signal" in ws.receive_json()


def test_remote_integration_toggle(client):
    assert client.get("/api/status").json()["integrations"]["uc_remote"]["enabled"] is False
    on = client.put("/api/integrations/uc-remote", json={"enabled": True})
    assert on.status_code == 200 and on.json()["enabled"] is True
    off = client.put("/api/integrations/uc-remote", json={"enabled": False}).json()
    assert off == {"enabled": False, "running": False, "port": 9095, "error": None}
    assert client.put("/api/integrations/uc-remote", json={"enabled": "maybe"}).status_code == 422


def test_service_settings_page(tmp_path, monkeypatch):
    from vproc.service_settings import ServiceSettings

    monkeypatch.setenv("MOCK_DECKLINK_SCRIPT", "nosignal:100000")
    monkeypatch.setenv("FAKE_MPV_LOG", str(tmp_path / "mpv.log"))
    env = {
        "VPROC_DATA_DIR": str(tmp_path / "data"), "VPROC_RUN_DIR": str(tmp_path / "run"),
        "VPROC_WEB_DIR": str(ROOT / "web"), "VPROC_DLCAP_BIN": DLCAP_BIN,
        "VPROC_MPV_BIN": str(ROOT / "tests" / "fake_mpv.py"), "VPROC_OUTPUT_SIZE": "3840x2160",
    }
    settings = ServiceSettings(tmp_path / "data", env=env)
    config, _ = settings.config()  # as __main__ builds it
    restarts = []

    with TestClient(create_app(config, settings=settings, request_restart=lambda: restarts.append(1))) as client:
        page = client.get("/api/service-settings").json()
        assert page["can_restart"] and not page["restart_needed"]
        fields = {f["key"]: f for f in page["fields"]}
        assert fields["OUTPUT_SIZE"]["source"] == "env" and fields["OUTPUT_SIZE"]["value"] == "3840x2160"

        r = client.put("/api/service-settings", json={"changes": {"LLDV": "true", "LLDV_PEAK": "1390"}})
        assert r.status_code == 200, r.text
        fields = {f["key"]: f for f in r.json()["fields"]}
        assert (fields["LLDV"]["value"], fields["LLDV"]["source"], fields["LLDV"]["running"]) == ("true", "saved", "false")
        assert r.json()["restart_needed"]

        bad = client.put("/api/service-settings", json={"changes": {"LLDV_PEAK": "50"}})
        assert bad.status_code == 400 and "VPROC_LLDV_PEAK" in bad.json()["detail"]
        assert client.put("/api/service-settings", json={"changes": {"API_TOKEN": "x"}}).status_code == 400

        # Lip-sync settings take effect when saved: nothing waits for a restart.
        r = client.put("/api/service-settings", json={"changes": {"LLDV": None, "LLDV_PEAK": None, "LIPSYNC_OFFSET": "325"}})
        fields = {f["key"]: f for f in r.json()["fields"]}
        assert fields["LIPSYNC_OFFSET"]["live"] and fields["LIPSYNC_OFFSET"]["running"] == "325"
        assert not r.json()["restart_needed"]
        assert client.get("/api/status").json()["lipsync"]["offset_ms"] == 325

        # Stepping the offset (the remote's buttons) saves it too, within its range.
        assert client.post("/api/lipsync/offset", json={"step_ms": 10}).json()["offset_ms"] == 335
        assert client.post("/api/lipsync/offset", json={"step_ms": -100}).json()["offset_ms"] == 235
        assert client.post("/api/lipsync/offset", json={"step_ms": 500}).status_code == 422
        fields = {f["key"]: f for f in client.get("/api/service-settings").json()["fields"]}
        assert (fields["LIPSYNC_OFFSET"]["value"], fields["LIPSYNC_OFFSET"]["source"]) == ("235", "saved")
        client.put("/api/service-settings", json={"changes": {"LIPSYNC_OFFSET": "995"}})
        assert client.post("/api/lipsync/offset", json={"step_ms": 10}).json()["offset_ms"] == 1000

        assert client.post("/api/service-settings/apply").json() == {"restarting": True}
        deadline = time.monotonic() + 3
        while not restarts and time.monotonic() < deadline:
            time.sleep(0.05)
        assert restarts == [1]

    # Without a way to restart (e.g. run directly), the page still works but can't apply.
    with TestClient(create_app(config, settings=settings)) as client:
        assert not client.get("/api/service-settings").json()["can_restart"]
        assert client.post("/api/service-settings/apply").status_code == 409
