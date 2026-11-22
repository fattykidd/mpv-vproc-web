# Changelog

What changed in each release of mpv-vproc-web. The newest release is at the top.

New changes go under **Unreleased** as they're made. `scripts/release.sh` turns that heading into the release's
version and date, and the release on GitHub shows the same notes.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Grab frame** (Tools): saves the current picture as a PNG, either as displayed (scaled and tone-mapped) or as
  captured, and downloads it. The newest 20 are kept in the data volume's `grabs` folder.
- **Download diagnostics** (Tools): one zip with a readable summary, the full status, settings, presets,
  software versions and the recent log, to attach to a bug report. The API token is never included.
- `POST /api/frame-grab`, `GET /api/frame-grabs`, `GET /api/frame-grabs/{name}` and `GET /api/diagnostics`.

### Documentation

- `ROADMAP.md` lists what's planned next.
- Credits for outside projects are gathered in `THIRD_PARTY_NOTICES.md`.

## [0.2.5] - 2026-11-22

### Added

- **Lip-sync settings take effect straight away.** Saving the offset, URLs or HTTP method under System settings
  sends the new audio delay at once; no **Apply** or restart is needed.
- **Lip-sync buttons on the Unfolded Circle remote.** *Lip-sync offset +10 ms* and *−10 ms* move the offset,
  send the new audio delay and show the new value on screen for 2 seconds. A *Lip-sync offset* sensor shows the
  offset and the delay last sent, e.g. `+325 ms · audio 430 ms`.
- `POST /api/lipsync/offset` moves the lip-sync offset by a step (`{"step_ms": 10}`) and saves it.
- **LLDV colour primaries** setting (`VPROC_LLDV_PRIMARIES`: `bt2020`, `p3` or `bt709`) for players that send
  LLDV colours in a smaller gamut than BT.2020. The default, `bt2020`, is unchanged. When set, the input label
  shows it, e.g. `Dolby Vision (LLDV) · P3`.
- **Chroma shader** picture setting: Chroma from Luma (CfL) prediction rebuilds the colour detail of 4:2:2
  sources from their brightness detail, for sharper colour edges than any chroma upscaler. Choose `cfl` or the
  lighter `cfl-lite`. The shaders are open source (MIT), ship in `shaders/` and are credited in
  `THIRD_PARTY_NOTICES.md`.

## [0.2.4] - 2026-11-22

### Added

- **Source clock**: the capture panel shows how far the source's frame rate is from nominal, in parts per
  million, once a signal has played for a minute. It shows how often the source's clock and the display's will
  drift a frame apart.
- **Skipped** and **Missed by card** counters. Skipped frames are split into those lost while the output was
  starting (nothing on screen yet) and those lost during playback, which can show as a stutter. Missed frames
  are ones the capture card never delivered.
- The remote's *Dropped frames* sensor now reads `0 dropped · 0 skipped · 0 missed`, leaving out start-up
  skips.
- **Offer HLG to sources** (`VPROC_EDID_HLG`, on by default): the capture card's EDID tells sources it accepts
  HLG, so HLG arrives as HLG instead of SDR.
- Every setting explains itself: hover over (or tap) a setting's name for a fuller description. System settings
  also show their `.env` name.

### Changed

- Processing latency is averaged over half a minute of settled playback, so the reading no longer flips between
  values.
- The lip-sync delay is worked out only once playback has settled, and from the average of several readings,
  so the queue filling at start-up no longer inflates it.

### Fixed

- The web page no longer runs wider than a phone's screen.

## [0.2.3] - 2026-11-19

### Added

- **LLDV (player-led Dolby Vision)** without an HDFury in the chain: with `VPROC_LLDV=true`, SDR-flagged BT.2020
  input is read as HDR at `VPROC_LLDV_PEAK` nits and tone-mapped like HDR10.
- **Lip sync with an AV processor**: vproc sends its own video delay, plus `VPROC_LIPSYNC_OFFSET`, to the URLs
  in `VPROC_LIPSYNC_URLS` (for example an AudioControl Hyperion's per-input lip-sync setting) whenever a new
  signal settles.
- **System settings** panel in the web page: display, input, lip-sync and logging settings can be changed
  there instead of in `.env`, and applied with a quick restart inside the container.
- The log records vproc's reading of each new signal.

### Fixed

- Lip-sync readings are only taken once the display's refresh rate is known, so the first estimate is no longer
  too low.

## [0.2.2] - 2026-11-18

### Added

- Choose the capture input from the Unfolded Circle remote (*Capture input*).
- *Dropped frames* and *Processing latency* sensors for the remote.

## [0.2.1] - 2026-11-17

### Fixed

- The HDMI link watch reads the display's SCDC status with SMBus byte reads, which work with more graphics
  drivers.

### Documentation

- How to recover from a black screen after switching inputs or power-cycling the display.

## [0.2.0] - 2026-11-17

### Added

- Choose the capture input (for example one of a Quad HDMI Recorder's four) in the web page; the choice is
  kept across restarts.
- The output restarts by itself when the display stops taking frames while the source keeps sending.
- **HDMI link watch** (`VPROC_DDC_DEVICE`): when the display loses the HDMI lock, vproc restores the picture.
- Beginner install and settings guides (`docs/INSTALL.md`, `docs/SETTINGS.md`), and a setup helper that also
  checks the capture card's firmware.
- Each version tag creates a GitHub release.

### Changed

- With no mode set, vproc picks the display's largest 16:9 mode instead of its "preferred" mode.
- Browsers always fetch the newest web page script and stylesheet after an update.

## [0.1.0] - 2026-11-16

The first public release.

- Captures from a Blackmagic DeckLink card and processes the picture with mpv and libplacebo: HDR10 and HLG
  tone mapping, gamut mapping, scaling and debanding.
- Outputs through Vulkan direct display, choosing modes from the display's own list and matching its refresh
  rate to the source.
- A web page and HTTP API for picture settings and presets, with automatic preset switching by signal type.
- An optional Unfolded Circle Remote integration, off by default.
- A hardware-free simulation (`compose.sim.yml`) for trying it without a capture card.
- Container images built and published on GitHub.

[Unreleased]: https://github.com/fattykidd/mpv-vproc-web/compare/v0.2.5...HEAD
[0.2.5]: https://github.com/fattykidd/mpv-vproc-web/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/fattykidd/mpv-vproc-web/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/fattykidd/mpv-vproc-web/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/fattykidd/mpv-vproc-web/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/fattykidd/mpv-vproc-web/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/fattykidd/mpv-vproc-web/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fattykidd/mpv-vproc-web/releases/tag/v0.1.0
