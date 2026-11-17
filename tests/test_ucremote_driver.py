"""The Unfolded Circle driver, run with the real ucapi library against a fake
vproc, with this test playing the part of the remote over the Integration API."""

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vproc.ucremote import entities as e

ucapi = pytest.importorskip("ucapi")
websockets = pytest.importorskip("websockets")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeVproc:
    """Records API calls on an HTTP port and streams status on a WebSocket port."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.status = json.loads(json.dumps(STATUS))
        self.clients: set = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                fake.calls.append((self.command, self.path, body))
                code = 404 if "nope" in self.path else 200
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_PUT = _handle

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.http_url = f"http://127.0.0.1:{self.http.server_address[1]}"

    async def serve_ws(self):
        async def handler(socket):
            self.clients.add(socket)
            try:
                await socket.send(json.dumps(self.status))
                await socket.wait_closed()
            finally:
                self.clients.discard(socket)

        self.ws_server = await websockets.serve(handler, "127.0.0.1", 0)
        self.ws_url = f"ws://127.0.0.1:{self.ws_server.sockets[0].getsockname()[1]}/api/ws"

    async def push(self):
        for client in list(self.clients):
            await client.send(json.dumps(self.status))


class Remote:
    """Speaks the remote's side of the Integration API."""

    def __init__(self, socket):
        self.socket = socket
        self.next_id = 1
        self.events: list[dict] = []

    async def _answer(self, req: dict) -> None:
        """Requests the driver sends to the remote."""
        if req.get("msg") == "get_supported_entity_types":
            types = ["button", "switch", "sensor", "select", "media_player", "remote", "light"]
            await self.socket.send(json.dumps({"kind": "resp", "req_id": req["id"], "code": 200,
                                               "msg": "supported_entity_types", "msg_data": types}))

    async def _receive(self, timeout: float) -> dict:
        message = json.loads(await asyncio.wait_for(self.socket.recv(), timeout))
        if message.get("kind") == "req":
            await self._answer(message)
        return message

    async def request(self, msg: str, data: dict | None = None) -> dict:
        rid = self.next_id
        self.next_id += 1
        await self.socket.send(json.dumps({"kind": "req", "id": rid, "msg": msg, "msg_data": data or {}}))
        while True:
            reply = await self._receive(5)
            if reply.get("kind") == "resp" and reply.get("req_id") == rid:
                return reply
            if reply.get("kind") == "event":
                self.events.append(reply)

    async def event(self, msg: str, entity_id: str, timeout: float = 5.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for ev in self.events:
                if ev.get("msg") == msg and ev["msg_data"].get("entity_id") == entity_id:
                    self.events.remove(ev)
                    return ev
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"no {msg} for {entity_id}")
            try:
                message = await self._receive(remaining)
                if message.get("kind") == "event":
                    self.events.append(message)
            except asyncio.TimeoutError:
                pass


STATUS = {
    "signal": {"present": True, "label": "2160p23.976 · HDR10 · BT.2020 · 4:2:2 10-bit"},
    "output": {"running": True, "display_mode": "3840x2160@23.976", "display_fps": 23.976},
    "presets": {"presets": {"SDR": {}, "HDR to SDR": {}, "Night": {}}, "active": "HDR to SDR", "auto_switch": True},
}


def test_driver_end_to_end(tmp_path, monkeypatch):
    from vproc.ucremote.driver import run

    monkeypatch.delenv("UC_INTEGRATION_HTTP_PORT", raising=False)
    monkeypatch.setenv("UC_DISABLE_MDNS_PUBLISH", "true")
    port = free_port()

    async def scenario():
        fake = FakeVproc()
        await fake.serve_ws()
        driver = asyncio.create_task(run(version="9.9.9", port=port, vproc_url=fake.http_url, token="",
                                         config_dir=tmp_path / "uc", ws_url=fake.ws_url))
        try:
            for _ in range(50):
                try:
                    socket = await websockets.connect(f"ws://127.0.0.1:{port}")
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            remote = Remote(socket)

            version = await remote.request("get_driver_version")
            assert version["msg_data"]["version"]["driver"] == "9.9.9"

            # Give the driver a moment to receive vproc's first status.
            await asyncio.sleep(0.3)
            available = (await remote.request("get_available_entities"))["msg_data"]["available_entities"]
            by_id = {x["entity_id"]: x for x in available}
            assert set(by_id) == {e.PRESET, e.INPUT, e.OUTPUT, e.AUTO_SWITCH, e.SHOW_INFO, e.STATS, e.RESTART_OUTPUT}
            assert by_id[e.PRESET]["entity_type"] == "select"

            await remote.request("subscribe_events", {"entity_ids": list(by_id)})
            states = (await remote.request("get_entity_states"))["msg_data"]
            state = {x["entity_id"]: x["attributes"] for x in states}
            assert state[e.PRESET] == {"state": "ON", "options": ["SDR", "HDR to SDR", "Night"],
                                       "current_option": "HDR to SDR"}
            assert state[e.INPUT]["value"].startswith("2160p23.976")
            assert state[e.OUTPUT]["value"] == "3840x2160 @ 23.976 Hz"
            assert state[e.AUTO_SWITCH]["state"] == "ON"

            # Commands reach vproc's API.
            ok = await remote.request("entity_command", {"entity_id": e.PRESET, "cmd_id": "select_option",
                                                         "params": {"option": "Night"}})
            assert ok["code"] == 200
            assert fake.calls[-1] == ("POST", "/api/presets/Night/activate", {})
            await remote.request("entity_command", {"entity_id": e.PRESET, "cmd_id": "select_next"})
            assert fake.calls[-1][1] == "/api/presets/Night/activate"  # HDR to SDR -> Night
            bad = await remote.request("entity_command", {"entity_id": e.PRESET, "cmd_id": "select_option",
                                                          "params": {"option": "Nope"}})
            assert bad["code"] == 400

            await remote.request("entity_command", {"entity_id": e.AUTO_SWITCH, "cmd_id": "toggle"})
            assert fake.calls[-1] == ("PUT", "/api/auto-switch", {"enabled": False})
            await remote.request("entity_command", {"entity_id": e.SHOW_INFO, "cmd_id": "push"})
            assert fake.calls[-1][1] == "/api/osd" and fake.calls[-1][2]["message"].startswith("2160p23.976")
            await remote.request("entity_command", {"entity_id": e.STATS, "cmd_id": "push"})
            assert fake.calls[-1][:2] == ("POST", "/api/stats-overlay")
            await remote.request("entity_command", {"entity_id": e.RESTART_OUTPUT, "cmd_id": "push"})
            assert fake.calls[-1][:2] == ("POST", "/api/restart/output")

            # Status changes become entity_change events on the remote.
            fake.status["presets"]["active"] = "SDR"
            fake.status["signal"] = {"present": False}
            await fake.push()
            change = await remote.event("entity_change", e.PRESET)
            assert change["msg_data"]["attributes"] == {"current_option": "SDR"}
            change = await remote.event("entity_change", e.INPUT)
            assert change["msg_data"]["attributes"] == {"value": "No signal"}

            # Losing vproc marks the live entities unavailable.
            for client in list(fake.clients):
                await client.close()
            fake.ws_server.close()
            change = await remote.event("entity_change", e.OUTPUT)
            assert change["msg_data"]["attributes"] == {"state": "UNAVAILABLE"}
            await socket.close()
        finally:
            driver.cancel()
            with pytest.raises(asyncio.CancelledError):
                await driver
            fake.http.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 30))
    meta = json.loads((tmp_path / "uc" / "driver.json").read_text())
    assert meta["driver_id"] == "mpv_vproc_web" and meta["port"] == port
