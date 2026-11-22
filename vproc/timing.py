"""Frame accounting and timing figures, derived from dlcap's stats events (one every 2 s)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

# Skips this soon after a segment starts playing belong to the start-up: mpv is still
# opening the stream and the display may still be switching modes.
STARTUP_GRACE = 5.0
# Stats events the latency estimate averages over: half a minute.
LATENCY_WINDOW = 15
# How long the source's frame rate is measured before its offset from nominal is shown.
CLOCK_MIN_SPAN = 60.0


def skipped_total(stats: dict[str, Any]) -> int:
    """Every frame dlcap captured but didn't hand to mpv, whatever the reason."""
    return sum(int(stats.get(k) or 0) for k in ("dropped_queue", "dropped_no_reader", "dropped_stale"))


class FrameAccount:
    """Splits the frames dlcap skipped into those lost while the output was starting and
    those lost during playback, which are the ones that can show as a stutter."""

    def __init__(self) -> None:
        self.startup = 0
        self.playback = 0
        self._last: int | None = None

    def update(self, stats: dict[str, Any], settled: bool) -> None:
        """Counts the skips since the previous stats event.

        settled: a segment has been playing for longer than STARTUP_GRACE.
        """
        total = skipped_total(stats)
        # dlcap's counters start again from zero when it restarts.
        new = total if self._last is None or total < self._last else total - self._last
        self._last = total
        if settled:
            self.playback += new
        else:
            self.startup += new

    def to_dict(self) -> dict[str, int]:
        return {"startup": self.startup, "playback": self.playback}


class QueueAverage:
    """The frames waiting in dlcap's queue, averaged over the last LATENCY_WINDOW stats events.

    The queue is 0 or 1 frames deep from one moment to the next, depending on where each
    frame lands against the display's refresh; any single reading flips between the two.
    """

    def __init__(self, window: int = LATENCY_WINDOW) -> None:
        self._readings: deque[float] = deque(maxlen=window)

    def reset(self) -> None:
        self._readings.clear()

    def add(self, stats: dict[str, Any]) -> None:
        # dlcap averages over every frame since the previous event; older builds only
        # report the queue at that instant.
        value = stats.get("queue_avg")
        if value is None:
            value = stats.get("queue")
        if value is not None:
            self._readings.append(float(value))

    def value(self) -> float | None:
        return sum(self._readings) / len(self._readings) if self._readings else None


@dataclass(frozen=True)
class SourceClock:
    """How fast the source really sends frames, by the server's clock, against its nominal rate."""

    fps: float
    nominal: float
    span: float

    @property
    def ppm(self) -> float | None:
        """Parts per million away from nominal, once it has been measured long enough to trust."""
        if self.span < CLOCK_MIN_SPAN:
            return None
        return (self.fps / self.nominal - 1) * 1e6

    def to_dict(self) -> dict[str, Any]:
        ppm = self.ppm
        return {
            "fps": round(self.fps, 6),
            "nominal": round(self.nominal, 6),
            "ppm": round(ppm, 1) if ppm is not None else None,
            "measured_for": round(self.span),
        }


def source_clock(stats: dict[str, Any], fps_num: int, fps_den: int) -> SourceClock | None:
    fps = stats.get("source_fps")
    if not fps or not fps_num or not fps_den:
        return None
    return SourceClock(fps=float(fps), nominal=fps_num / fps_den, span=float(stats.get("source_span") or 0))
