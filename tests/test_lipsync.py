"""Lip-sync delay sent to an AV processor's HTTP API."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vproc.config import Config, ConfigError
from vproc.lipsync import LipSync


class FakeProcessor:
    """Records requests, like the Hyperion's /api/atlas/input/N/lipsync/{ms} endpoint."""

    def __init__(self, status=200):
        self.requests: list[tuple[str, str]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                fake.requests.append((self.command, self.path))
                self.send_response(fake.status)
                self.end_headers()
                self.wfile.write(b'{"success": true}')

            do_POST = do_PUT = do_GET = _answer

            def log_message(self, *args):
                pass

        self.status = status
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def avp():
    fake = FakeProcessor()
    yield fake
    fake.close()


def test_delay_is_latency_plus_offset_rounded_and_never_negative():
    sync = LipSync(("http://x/{delay_ms}",), offset_ms=40)
    assert sync.target_for(63) == 105      # 65 + 40
    assert sync.target_for(87.4) == 125    # 85 + 40
    assert LipSync(("http://x/{delay_ms}",), offset_ms=-200).target_for(60) == 0


def test_sends_to_every_url_once_per_change(avp):
    sync = LipSync((avp.url("/api/atlas/input/1/lipsync/{delay_ms}"), avp.url("/api/atlas/input/2/lipsync/{delay_ms}")), 40)

    async def scenario():
        assert await sync.apply(63)
        assert not await sync.apply(64)   # rounds to the same delay: nothing sent
        assert await sync.apply(100)

    asyncio.run(scenario())
    assert avp.requests == [
        ("POST", "/api/atlas/input/1/lipsync/105"), ("POST", "/api/atlas/input/2/lipsync/105"),
        ("POST", "/api/atlas/input/1/lipsync/140"), ("POST", "/api/atlas/input/2/lipsync/140"),
    ]
    assert sync.status() == {"enabled": True, "delay_ms": 140, "offset_ms": 40, "error": None}


def test_failure_is_reported_and_retried():
    fake = FakeProcessor(status=500)
    try:
        sync = LipSync((fake.url("/lipsync/{delay_ms}"),))

        async def scenario():
            await sync.apply(63)
            assert sync.error and sync.sent_ms is None
            fake.status = 200
            assert await sync.apply(63)  # same delay, but the last attempt failed: sent again
            assert sync.error is None and sync.sent_ms == 65

        asyncio.run(scenario())
    finally:
        fake.close()


def test_settings():
    config = Config.from_env({"VPROC_LIPSYNC_URLS": "http://a/1/{delay_ms}, http://a/2/{delay_ms}",
                              "VPROC_LIPSYNC_OFFSET": "-20"})
    assert config.lipsync_urls == ("http://a/1/{delay_ms}", "http://a/2/{delay_ms}")
    assert config.lipsync_offset == -20
    with pytest.raises(ConfigError):
        Config.from_env({"VPROC_LIPSYNC_URLS": "http://a/lipsync"})  # no placeholder
    with pytest.raises(ConfigError):
        Config.from_env({"VPROC_LIPSYNC_METHOD": "DELETE"})
