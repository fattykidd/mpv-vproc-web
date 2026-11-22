import pytest

from vproc.timing import CLOCK_MIN_SPAN, FrameAccount, QueueAverage, SourceClock, source_clock


def stats(queue=0, no_reader=0, stale=0, **extra):
    return {"dropped_queue": queue, "dropped_no_reader": no_reader, "dropped_stale": stale, **extra}


def test_skips_before_playback_settles_count_as_start_up():
    frames = FrameAccount()
    frames.update(stats(no_reader=90, queue=20), settled=False)
    frames.update(stats(no_reader=90, queue=20), settled=True)
    frames.update(stats(no_reader=90, queue=23), settled=True)
    assert frames.to_dict() == {"startup": 110, "playback": 3}


def test_a_restarted_capture_counts_from_zero_again():
    frames = FrameAccount()
    frames.update(stats(queue=50), settled=True)
    frames.update(stats(queue=4), settled=True)  # dlcap restarted: its counters went back to 0
    assert frames.playback == 54


def test_segment_changes_count_as_start_up():
    frames = FrameAccount()
    frames.update(stats(), settled=True)
    frames.update(stats(stale=2), settled=False)
    assert frames.to_dict() == {"startup": 2, "playback": 0}


def test_queue_average_smooths_the_flip_between_zero_and_one():
    queue = QueueAverage(window=4)
    assert queue.value() is None
    for value in (0.0, 1.0, 0.0, 1.0, 1.0):
        queue.add({"queue_avg": value})
    assert queue.value() == pytest.approx(0.75)  # only the last four count
    queue.reset()
    assert queue.value() is None


def test_queue_average_falls_back_to_the_instant_reading():
    queue = QueueAverage()
    queue.add({"queue": 1})
    queue.add({"queue_avg": None, "queue": 0})
    assert queue.value() == 0.5


def test_source_clock_offset_waits_for_a_long_enough_measurement():
    nominal = 24000 / 1001
    early = SourceClock(fps=nominal * (1 + 20e-6), nominal=nominal, span=CLOCK_MIN_SPAN - 1)
    late = SourceClock(fps=nominal * (1 + 20e-6), nominal=nominal, span=CLOCK_MIN_SPAN)
    assert early.ppm is None and early.to_dict()["ppm"] is None
    assert late.ppm == pytest.approx(20, abs=0.01)
    assert late.to_dict() == {"fps": round(nominal * (1 + 20e-6), 6), "nominal": round(nominal, 6),
                              "ppm": 20.0, "measured_for": 60}


def test_source_clock_needs_a_measured_rate():
    assert source_clock({}, 24000, 1001) is None
    assert source_clock({"source_fps": None}, 24000, 1001) is None
    assert source_clock({"source_fps": 23.976, "source_span": 5}, 0, 0) is None
    assert source_clock({"source_fps": 23.976, "source_span": 5}, 24000, 1001).span == 5
