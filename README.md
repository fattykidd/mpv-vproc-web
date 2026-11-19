# mpv-vproc-web

A headless HDR video processor built from a DeckLink capture card (Blackmagic Design), mpv and libplacebo.
It takes HDMI in, tone-maps HDR10/HLG in a style like madVR's (with dynamic per-frame peak detection), scales
with libplacebo's high-quality filters and drives the display directly through Vulkan. No desktop is needed.
You control it from a web page, its HTTP API, or an Unfolded Circle remote.

> **New here?** Follow **[docs/INSTALL.md](docs/INSTALL.md)**: a step-by-step guide from a fresh Linux
> install to a picture on your screen, no programming knowledge needed. Then read
> **[docs/SETTINGS.md](docs/SETTINGS.md)**, which explains every picture setting in plain language: what tone
> mapping is, how the curves differ, which scaler to pick, and the three settings that matter most.

```
 source ─HDMI─▶ DeckLink ──▶ dlcap ──FIFO──▶ mpv (gpu-next / libplacebo) ──Vulkan──▶ HDMI ─▶ projector
                                │                     ▲
                                └── JSON events ──▶ vproc (Python) ──JSON IPC┘
                                                       ▲
                                              web UI / HTTP API
```

* **dlcap** (`capture/`): a small C++ program that captures one DeckLink input with automatic format
  detection. It reads the HDR metadata that arrives with each frame (EOTF, colorspace, mastering display,
  MaxCLL/MaxFALL), which FFmpeg's DeckLink input drops. It streams frames into a fresh FIFO for each
  *segment* (an unbroken run of signal in one format).
* **mpv** reads each segment with its rawvideo demuxer. vproc tags the frames with the source's real
  colorimetry, so libplacebo knows when to tone-map.
* **vproc** (`vproc/`): supervises both processes. It reacts to format changes, signal loss, HDR metadata
  changes and crashes, matches the display refresh rate to the source, switches presets by signal type,
  and serves the UI and API.

## Host setup (once)

The short version for experienced users; [docs/INSTALL.md](docs/INSTALL.md) has every step with commands.

1. **NVIDIA driver** with kernel modesetting enabled: add `nvidia-drm.modeset=1` to the kernel command line.
   Don't run a desktop on the GPU that feeds the display; vproc needs to own that output.
2. **[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)** for Docker.
3. **Blackmagic Desktop Video** (the driver). Check that the card appears: `ls /dev/blackmagic`.
   The container uses the host's own `libDeckLinkAPI.so` through a read-only mount.

## Run

```sh
bash scripts/setup-env.sh   # writes .env: VIDEO_GID, RENDER_GID, VPROC_OUTPUT_SIZE, DECKLINK_LIB
docker compose pull
docker compose up -d
docker compose exec vproc python3 -m vproc.doctor
```

This downloads the ready-built image from `ghcr.io/fattykidd/mpv-vproc-web`. To stay on a particular
release instead of the newest, set `VPROC_VERSION` (for example `0.1.0`) in `.env`. To update later:
`docker compose pull && docker compose up -d`.

Then open `http://<host>:8000`. The container uses the host's network directly (so an Unfolded Circle
Remote can find it); if port 8000 is taken on your server, set `VPROC_PORT` in `.env`.

### Building the image yourself

Building needs your own download of the DeckLink SDK from Blackmagic's developer site: set
`DECKLINK_SDK_INCLUDE` in `.env` to its `Linux/include` folder, then add `compose.build.yml`:

```sh
docker compose -f compose.yml -f compose.build.yml up -d --build
```

The first build compiles FFmpeg, libplacebo and mpv from their pinned release tags (`FFMPEG_VERSION`,
`LIBPLACEBO_VERSION`, `MPV_VERSION`), which takes a while. To upgrade one, change its version in `.env` and
rebuild.

`vproc.doctor` ([explained here](docs/INSTALL.md#the-doctor)) checks everything the container depends on: GPU and DRM access, the NVIDIA GBM/Vulkan
pieces, connected displays and their modes, the DeckLink library, devices and driver version, and the mpv
build. For each problem it tells you what to fix on the host.

## Using it

**Presets.** Two presets are created to start with:
* **SDR** for SDR sources.
* **HDR to SDR** for HDR10 and HLG: `spline` tone mapping, dynamic peak detection (99.995th percentile),
  contrast recovery, perceptual gamut mapping, and output in BT.709 / BT.1886 at **100 nits**.

With *Switch preset by signal type* on, vproc picks the right preset as soon as the input changes. Every
change in the UI applies live and is saved to the active preset.

**Tune for your screen first.** Set *Display peak (nits)* to your projector's measured peak in its
calibrated mode. Set *Display primaries* / *Display transfer* to the gamut and gamma it's calibrated to
(for example `bt.2020` + `gamma2.4` in a wide-gamut mode). Those three settings matter more than anything
else. *Visualize curve* draws the tone curve and the measured peak over the picture.

**Refresh-rate matching.** With `VPROC_OUTPUT_SIZE` set, the display mode follows the source: 23.976 → 23.976 Hz,
25 → 50 Hz, and so on (`VPROC_REFRESH_MAP`). Without a signal, or if the display rejects a mode, vproc
uses the output size at its highest refresh rate (or `VPROC_DISPLAY_MODE` if set), not the display's own
"preferred" mode, which on many 4K projectors is 1080p. Rejected modes are remembered and reported in the UI. vproc reads the display's mode list, so a
mode the display doesn't offer (PC monitors rarely do 23.976 Hz) is skipped without a restart. Changing modes restarts mpv, which takes
about a second of black.

**Inputs.** 8-, 10- and 12-bit YCbCr 4:2:2 and RGB 4:4:4 are supported, progressive and interlaced (bwdif
deinterlacing). With a card that has several inputs, such as the Quad HDMI Recorder, pick the
input in the web UI (the menu next to **Input**); the choice is kept across restarts. `VPROC_DEVICE` sets the
default: an index, part of the name or `id:<persistent id>` (the doctor lists them). One container processes one
input at a time.

## Unfolded Circle Remote

vproc includes an optional integration driver for the Unfolded Circle Remote Two and Remote 3. It's **off
by default**: while off, nothing is announced on your network and no port is open.

1. In vproc's web page, under **Integrations**, switch **Unfolded Circle Remote** on. It shows
   `On · port 9095` once the driver is running.
2. In the remote's web configurator, open **Integrations → Add new**. *mpv-vproc-web* appears in the list
   of discovered integrations. Select it and finish the setup; there's nothing to enter.
3. Choose the entities you want:

| Entity | Type | What it does |
|---|---|---|
| Picture preset | Select | Pick a preset, or step through them |
| Capture input | Select | Pick the card's input (e.g. one of a Quad HDMI Recorder's four), or step through them |
| Input signal | Sensor | e.g. `2160p23.976 · HDR10 · BT.2020 · 4:2:2 10-bit`, or `No signal` |
| Output mode | Sensor | e.g. `3840x2160 @ 23.976 Hz` |
| Auto preset by signal | Switch | Turns automatic SDR/HDR preset switching on or off |
| Show signal info | Button | Shows the input signal on screen for 5 seconds |
| Stats overlay | Button | Toggles mpv's statistics overlay |
| Restart video output | Button | Restarts mpv |

If the remote doesn't list the integration, check that the remote and the server are on the same network
(mDNS doesn't cross VLANs or most guest networks) and that port 9095 isn't blocked by the server's firewall.
The port can be changed with `VPROC_UC_PORT`.

## HTTP API

Everything the UI does goes through the API. Interactive docs are at `/api/docs`. If `VPROC_API_TOKEN` is
set, send `Authorization: Bearer <token>`; for the WebSocket, use `?token=`.

| Method | Path | Body / purpose |
|---|---|---|
| GET | `/api/status` | Signal, output, capture, presets and recent errors |
| WS | `/api/ws` | The same status, pushed on every change |
| GET | `/api/schema` | Every adjustable setting with its type and range |
| PATCH | `/api/settings` | `{"values": {"target-peak": 120}, "preset": null}` |
| DELETE | `/api/settings/{option}` | Back to mpv's default |
| GET / POST | `/api/presets` | List / `{"name": "Night", "copy_from": "HDR to SDR"}` |
| POST | `/api/presets/{name}/activate` | |
| PATCH / DELETE | `/api/presets/{name}` | Rename (`{"name": ...}`) / delete |
| PUT | `/api/auto-switch` | `{"auto": {"hdr10": "Night"}, "enabled": true}` |
| POST | `/api/osd` | `{"message": "...", "duration_ms": 3000}` |
| POST | `/api/stats-overlay` | `{"page": null}` toggles mpv's stats overlay |
| POST | `/api/restart/{capture\|output}` | Restart dlcap or mpv |
| PUT | `/api/input` | `{"device": "id:1234"}` (or an index) captures from another input and remembers it; `null` returns to `VPROC_DEVICE` |
| PUT | `/api/integrations/uc-remote` | `{"enabled": true}` switches the remote integration on or off |
| GET | `/healthz` | Liveness, no token needed |

Only the options listed in `vproc/settings.py` can be changed, and each value is validated before it
reaches mpv.

## Configuration

All settings are environment variables in `.env`; `.env.example` documents each one. The ones you're most
likely to touch:

| Variable | Default | |
|---|---|---|
| `VPROC_OUTPUT_SIZE` | – | Display resolution; enables refresh-rate matching |
| `VPROC_DEVICE` | `0` | Which DeckLink input |
| `VPROC_DDC_DEVICE` | – | Host path of the display's DDC bus (`/dev/vproc-ddc`, set up by `scripts/enable-ddc.sh`): restarts the output when the display loses the HDMI lock |
| `VPROC_DISPLAY_MODE` | `preferred` | Mode used when refresh matching doesn't apply; `preferred` means the output size at its highest rate, or the display's own preference without `VPROC_OUTPUT_SIZE` |
| `VPROC_VULKAN_DISPLAY` | `0` | Which display, if the GPU has several connected |
| `VPROC_GPU_API` / `VPROC_GPU_CONTEXT` | `vulkan` / `displayvk` | `opengl` / `drm` as an alternative |
| `VPROC_API_TOKEN` | – | Protects the API |

## Troubleshooting

* **Black screen; the log shows "no video output came up"**: run the doctor. On NVIDIA, check that
  `nvidia-drm.modeset=1` is set on the host and that nothing else (a desktop, another mpv) holds the
  display. To see the modes the display offers, stop vproc and run
  `docker compose run --rm --entrypoint mpv vproc --no-config --vo=gpu-next --gpu-api=vulkan --gpu-context=displayvk --vulkan-display-mode=help`.
* **Black screen after the projector or TV switched inputs, or was turned off and on**: at 4K 50/60 Hz some
  displays forget the HDMI 2.0 link setup without telling the graphics card. Set up the HDMI link watch
  (`sudo bash scripts/enable-ddc.sh`, see [docs/INSTALL.md](docs/INSTALL.md#hdmi-link-watch-optional)) and vproc
  restores the picture by itself; until then, **Restart output** brings it back.
* **Picture freezes**: if mpv stops taking frames for a few seconds while the source keeps sending, vproc restarts
  the output by itself (`the display stopped taking frames` in the log). If it keeps happening, report it.
* **"Desktop Video driver not available"**: `DECKLINK_LIB` doesn't point at the host's `libDeckLinkAPI.so`,
  or the driver isn't loaded (`lsmod | grep blackmagic`).
* **"could not enable video input"**: another program (Media Express, OBS, a second container) has the input open.
* **HDR shows as SDR**: check that the source actually outputs HDR, and that the card is on a firmware
  version that reports HDR metadata (BlackmagicFirmwareUpdater).
* **Dropped frames**: the stats overlay (Tools → Toggle stats overlay) shows render times. Try cheaper
  scalers (`spline36`) or turn off `sigmoid-upscaling`.

## Development

dlcap and the service are tested without hardware. `tests/mock_sdk` is a small stand-in for the DeckLink SDK
(not Blackmagic code) that simulates a card from a script. The script covers format changes, HDR
metadata, signal loss and 8/10/12-bit YCbCr and RGB. `tests/fake_mpv.py` speaks mpv's IPC protocol and
checks that frames stay aligned.

```sh
make -C capture DECKLINK_SDK_INCLUDE=../tests/mock_sdk/include \
  CXXFLAGS="-O1 -g -fsanitize=address,undefined" LDLIBS="-ldl -pthread -fsanitize=address,undefined"
pip install -r requirements-dev.txt
DLCAP_BIN=capture/dlcap pytest
```

Build with `-DMOCK_DECKLINK_API_VERSION=0x0e030000` to test the SDK 14.3+ frame API as well; CI runs both.

### Releasing

The version lives in `vproc/__init__.py` (the example in `.env.example` follows it). Pushing a tag `vX.Y.Z` builds
the image and creates the GitHub release; the build refuses a tag that doesn't match the version.

```sh
git config core.hooksPath git-hooks   # once per clone: checks the version before a tag is pushed
scripts/release.sh 0.3.0              # sets the version, commits "Version 0.3.0", tags v0.3.0
git push origin main && git push origin v0.3.0
```

A version with a suffix (`0.3.0-rc1`) is published as a pre-release.

## License and credits

The code in this repository is released under the MIT License (see `LICENSE`). The container image also
contains mpv, libplacebo, FFmpeg and other third-party software under their own licenses. See
`THIRD_PARTY_NOTICES.md` for the full list, with credits and links to their source code.

This project is compatible with Blackmagic Design DeckLink capture cards and Unfolded Circle remotes. It is not
affiliated with or endorsed by Blackmagic Design or Unfolded Circle.
