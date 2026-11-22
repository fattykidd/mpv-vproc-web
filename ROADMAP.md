# Roadmap

What's planned for mpv-vproc-web, roughly in the order it's expected to arrive. Plans change: this is a
direction, not a promise. What has already shipped is in `CHANGELOG.md`.

## Next: diagnostics

Small features that make every later change easier to test and troubleshoot.

- **Frame grab**: save the current processed frame as an image, for before-and-after comparisons and bug
  reports.
- **Diagnostics bundle**: one button that collects the log, settings, signal information and timing statistics
  into a single file to attach to an issue.

## Display calibration

The largest remaining improvement to the picture itself.

- **Calibration LUT**: load a 3D LUT made with a colour meter and calibration software, to correct the display's
  colour.
- **Display black level**: tell the tone mapping how deep the display's black is, for more accurate shadows.
- **Tone-curve controls**: expose the tone curve's shape parameters, each with an explanation.
- **Test-pattern generator**: let calibration software show its patterns through vproc, so the whole chain is
  measured, not just the display.

## Smoother playback

The source and the display each run on their own clock. vproc now measures the source's (the **Source clock**
reading); these close the remaining gap on the display side.

- **Frame-queue tuning**: expose libplacebo's frame-queue settings that absorb small timing differences.
- **Frame blending for repeats**: when the clocks drift a frame apart, blend neighbouring frames instead of
  showing one twice, so the correction can't be seen.
- **Output refresh trimming**: run the display at a custom refresh rate that matches the source's measured rate
  exactly, so the clocks never drift apart. The most involved item: it depends on custom display modes through
  Vulkan on NVIDIA.

## Scope screens

- Presets for 2.35:1 and 2.40:1 screens (zoom, shift and masking), ideally switched automatically from the
  film's detected aspect ratio.

## Platforms and documentation

- **Unraid**: a guide to running vproc in a virtual machine with the graphics card and capture card passed
  through.

## Ideas welcome

Have a feature in mind? Open an issue on GitHub describing what you'd like it to do and why.
