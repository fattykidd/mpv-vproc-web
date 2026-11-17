from vproc.displays import DisplayMode, find_mode, parse_vulkan_modes

# Trimmed output of `mpv --gpu-context=displayvk --vulkan-display-mode=help`
# from a PC monitor on an RTX 5060 Ti.
MONITOR = """
Display 0: HDMI-0
    Modes:
      Mode 00: 1920x1080 (60.000 Hz)
      Mode 01: 1920x1080 (59.940 Hz)
      Mode 02: 1280x720 (59.940 Hz)
      Mode 06: 1920x1080 (50.000 Hz)
      Mode 12: 1920x1080 (60.000 Hz)
      Mode 22: 1920x1080 (75.001 Hz)
    Planes:
      Plane: 0
Display 1: DP-1
    Modes:
      Mode 00: 3840x2160 (23.976 Hz)
"""


def test_parse_reads_one_display():
    modes = parse_vulkan_modes(MONITOR)
    assert modes[0] == DisplayMode(0, 1920, 1080, 60.0)
    assert [m.index for m in modes] == [0, 1, 2, 6, 12, 22]
    assert parse_vulkan_modes(MONITOR, display=1) == [DisplayMode(0, 3840, 2160, 23.976)]


def test_parse_without_display_headers():
    assert parse_vulkan_modes("      Mode 03: 720x480 (59.940 Hz)") == [DisplayMode(3, 720, 480, 59.94)]


def test_find_mode():
    modes = parse_vulkan_modes(MONITOR)
    assert find_mode(modes, "1920x1080@60").index == 0          # first of the duplicates
    assert find_mode(modes, "1920x1080@59.94").index == 1
    assert find_mode(modes, "1920x1080@50").index == 6
    assert find_mode(modes, "1920x1080").index == 22             # no rate: the highest
    assert find_mode(modes, "1920x1080@23.976") is None          # monitors rarely do film rates
    assert find_mode(modes, "3840x2160@60") is None
    assert find_mode(modes, "garbage") is None
    assert DisplayMode(1, 3840, 2160, 23.976).label == "3840x2160@23.976"
