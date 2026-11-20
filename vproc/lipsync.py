"""Keeps an AV processor's audio delay matched to vproc's video delay.

vproc delays the picture by about a frame or two, more at 24 fps than at 60, so
audio that is in sync at one frame rate drifts out at another. When a new signal
has played long enough for its delay to settle, the delay (plus a fixed offset for
the display's own processing) is sent to each configured URL, e.g. an AudioControl
Hyperion's per-input lip-sync endpoint:

    VPROC_LIPSYNC_URLS=http://hyperion.local:8080/api/atlas/input/1/lipsync/{delay_ms}
    VPROC_LIPSYNC_OFFSET=40
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request

log = logging.getLogger("vproc.lipsync")

PLACEHOLDER = "{delay_ms}"
ROUND_TO = 5  # ms; finer steps would only chase noise
TIMEOUT = 3.0


def rounded(ms: float) -> int:
    return int(ROUND_TO * round(ms / ROUND_TO))


class LipSync:
    def __init__(self, urls: tuple[str, ...], offset_ms: int = 0, method: str = "POST"):
        self.urls = urls
        self.offset_ms = offset_ms
        self.method = method
        self.sent_ms: int | None = None  # last delay every URL accepted
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.urls)

    def target_for(self, video_latency_ms: float) -> int:
        """Audio delay for a measured video latency; never negative."""
        return max(0, rounded(video_latency_ms) + self.offset_ms)

    async def apply(self, video_latency_ms: float) -> bool:
        """Sends the delay if it differs from the last one sent. True when something was sent."""
        delay = self.target_for(video_latency_ms)
        if delay == self.sent_ms and self.error is None:
            return False
        failures = []
        for url in self.urls:
            try:
                await asyncio.to_thread(self._send, url.replace(PLACEHOLDER, str(delay)))
            except (OSError, urllib.error.URLError) as exc:
                failures.append(f"{url.split('/')[2]}: {exc}")
        if failures:
            self.error = "; ".join(failures)
            return True
        log.info("audio delay set to %d ms (video %d ms + offset %d ms)", delay, rounded(video_latency_ms), self.offset_ms)
        self.sent_ms, self.error = delay, None
        return True

    def _send(self, url: str) -> None:
        body = None if self.method == "GET" else b"{}"
        request = urllib.request.Request(url, data=body, method=self.method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read()

    def status(self) -> dict:
        return {"enabled": self.enabled, "delay_ms": self.sent_ms, "offset_ms": self.offset_ms, "error": self.error}
