"""DDC helpers that don't need hardware."""

from pathlib import Path

from vproc.ddc import is_real_device, link_locked


def test_lock_state_from_status_flags():
    assert link_locked(0x0F)           # clock detected, all three channels locked
    assert link_locked(0x0E)
    assert not link_locked(0x01)       # clock detected, channels not locked (the black screen)
    assert not link_locked(0x07)       # one channel missing


def test_dev_null_and_files_are_not_a_ddc_bus(tmp_path):
    assert not is_real_device(Path("/dev/null"))
    (tmp_path / "file").write_text("")
    assert not is_real_device(tmp_path / "file")
    assert not is_real_device(tmp_path / "missing")
