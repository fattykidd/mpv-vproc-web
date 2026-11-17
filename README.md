# vproc

A headless HDR video processor built from a Blackmagic DeckLink capture card, mpv and libplacebo.
It takes HDMI in, tone-maps HDR10/HLG in a style like madVR's (with dynamic per-frame peak detection), scales
with libplacebo's high-quality filters and drives the display directly over KMS. No desktop is needed.
You control it from a web page or its HTTP API.

```
 source ─HDMI─▶ DeckLink ──▶ dlcap ──FIFO──▶ mpv (gpu-next / libplacebo) ──KMS──▶ HDMI ─▶ projector
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

1. **NVIDIA driver** with kernel modesetting enabled: add `nvidia-drm.modeset=1` to the kernel command line.
   Don't run a desktop on the GPU that feeds the display; vproc needs to own that output.
2. **[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)** for Docker.
3. **Blackmagic Desktop Video** (the driver). Check that the card appears: `ls /dev/blackmagic`.
4. **DeckLink SDK** from Blackmagic's support site. Unpack it anywhere; the build only needs its
   `Linux/include` folder, which is passed in as a separate build context. Nothing from Blackmagic is
   committed to this repository or baked into an image you could publish. At runtime the container uses the
   host's own `libDeckLinkAPI.so` through a read-only mount.

## Run

```sh
cp .env.example .env        # set DECKLINK_SDK_INCLUDE, VIDEO_GID, RENDER_GID, VPROC_OUTPUT_SIZE
docker compose up -d --build
docker compose exec vproc python3 -m vproc.doctor
```

Then open `http://<host>:8000`.

The first build compiles FFmpeg, libplacebo and mpv from their pinned release tags (`FFMPEG_VERSION`,
`LIBPLACEBO_VERSION`, `MPV_VERSION`), which takes a few minutes. To upgrade one, change its version in `.env`
and rebuild.

`vproc.doctor` checks everything the container depends on: GPU and DRM access, the NVIDIA GBM/Vulkan
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
25 → 50 Hz, and so on (`VPROC_REFRESH_MAP`). If the display rejects a mode, vproc falls back to
`VPROC_DRM_MODE`, remembers the failure and reports it in the UI. Changing modes restarts mpv, which takes
about a second of black.

**Inputs.** 8-, 10- and 12-bit YCbCr 4:2:2 and RGB 4:4:4 are supported, progressive and interlaced (bwdif
deinterlacing). For a Quad HDMI Recorder, choose the input with `VPROC_DEVICE`: an index, part of the name
or `id:<persistent id>` (the doctor lists them). Run one container per input you want to process.

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
| `VPROC_DRM_CONNECTOR` | first connected | e.g. `HDMI-A-1` |
| `VPROC_GPU_API` / `VPROC_GPU_CONTEXT` | `opengl` / `drm` | `vulkan` / `displayvk` as an alternative on NVIDIA |
| `VPROC_DRM_FORMAT` | `xrgb8888` | `xrgb2101010` for a 10-bit output path |
| `VPROC_API_TOKEN` | – | Protects the API |

## Troubleshooting

* **Black screen; the log shows "no video output came up"**: run the doctor. On NVIDIA, the usual causes
  are `nvidia-drm.modeset=1` missing on the host, or the container toolkit not injecting
  `nvidia-drm_gbm.so`. Try `VPROC_GPU_API=vulkan` with `VPROC_GPU_CONTEXT=displayvk`.
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
