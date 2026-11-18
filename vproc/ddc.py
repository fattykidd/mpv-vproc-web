"""Reads the display's HDMI link status over DDC (the I2C channel in the HDMI cable).

HDMI 2.0 displays report in their SCDC registers whether they are locked onto the
signal. Some projectors lose that lock when they switch inputs and come back, without
any hotplug event: the graphics card keeps sending, mpv keeps rendering, and the screen
stays black until the output is set up again. Reading the lock state is how vproc
notices.

The device is /dev/vproc-ddc in the container: the host's DDC bus for the display's
output (scripts/enable-ddc.sh sets it up), or /dev/null when that isn't set up.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import stat
from pathlib import Path

# From linux/i2c-dev.h and linux/i2c.h.
I2C_SLAVE = 0x0703
I2C_SMBUS = 0x0720
I2C_SMBUS_READ = 1
I2C_SMBUS_BYTE_DATA = 2
EDID_ADDRESS = 0x50
SCDC_ADDRESS = 0x54
SCDC_SINK_VERSION = 0x01
SCDC_SCRAMBLER_STATUS = 0x21
SCDC_STATUS_FLAGS = 0x40
CHANNELS_LOCKED = 0x0E  # Ch0, Ch1 and Ch2 locked (bit 0 is "clock detected")


class _SmbusData(ctypes.Union):
    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * 34)]


class _SmbusTransfer(ctypes.Structure):
    _fields_ = [
        ("read_write", ctypes.c_uint8),
        ("command", ctypes.c_uint8),
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(_SmbusData)),
    ]


def is_real_device(path: Path) -> bool:
    """True for a character device other than /dev/null (the placeholder when DDC isn't set up)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) and st.st_rdev != os.stat("/dev/null").st_rdev


def link_locked(status_flags: int) -> bool:
    return status_flags & CHANNELS_LOCKED == CHANNELS_LOCKED


class DdcBus:
    """One I2C bus, read with SMBus "read byte data" transfers, the same as i2cget.

    (A combined I2C_RDWR write-then-read returned wrong values on NVIDIA's DDC buses.)
    """

    def __init__(self, path: Path):
        self.path = path

    def read(self, address: int, offset: int) -> int:
        data = _SmbusData()
        transfer = _SmbusTransfer(I2C_SMBUS_READ, offset, I2C_SMBUS_BYTE_DATA, ctypes.pointer(data))
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, I2C_SLAVE, address)
            fcntl.ioctl(fd, I2C_SMBUS, transfer)
        finally:
            os.close(fd)
        return data.byte

    def status_flags(self) -> int:
        """SCDC status flags. Raises OSError when the display doesn't answer (off, standby, no SCDC)."""
        return self.read(SCDC_ADDRESS, SCDC_STATUS_FLAGS)

    def scrambling(self) -> bool:
        return bool(self.read(SCDC_ADDRESS, SCDC_SCRAMBLER_STATUS) & 0x01)
