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
    assert anon.get("/assets/app.js").status_code == 200
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
