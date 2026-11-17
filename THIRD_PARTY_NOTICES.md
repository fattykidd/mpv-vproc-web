# Third-party software and credits

mpv-vproc-web stands on the work of many people. This file lists the software it uses or ships, under
which license, and where to get its source. The project's own code is MIT-licensed (see `LICENSE`).

## Built into the container image

| Component | Used for | License | Source |
|---|---|---|---|
| [mpv](https://mpv.io) | Video output, rendering pipeline | GPL-2.0-or-later (built with GPL FFmpeg) | https://github.com/mpv-player/mpv |
| [libplacebo](https://code.videolan.org/videolan/libplacebo) | Tone mapping, gamut mapping, scaling | LGPL-2.1-or-later | https://code.videolan.org/videolan/libplacebo |
| [FFmpeg](https://ffmpeg.org) | Decoding the raw capture formats, deinterlacing | GPL-2.0-or-later (built with `--enable-gpl`) | https://git.ffmpeg.org/ffmpeg.git |
| Debian 13 "trixie" packages | Base system, Python, Vulkan/EGL loaders, fonts and libraries | Various free software licenses | https://sources.debian.org |

The exact versions are pinned in the `Dockerfile` (`FFMPEG_VERSION`, `LIBPLACEBO_VERSION`, `MPV_VERSION`),
and the image is built from those upstream release tags without modification. The corresponding source
code is available at the links above. If you received a built image and need the source for a specific
release, open an issue and it will be provided.

### Python packages

| Package | Used for | License |
|---|---|---|
| [ucapi](https://github.com/unfoldedcircle/integration-python-library) by Unfolded Circle | Unfolded Circle Remote integration driver | MPL-2.0 |
| [FastAPI](https://fastapi.tiangolo.com) | HTTP API | MIT |
| [Starlette](https://www.starlette.io) | HTTP framework under FastAPI | BSD-3-Clause |
| [Pydantic](https://docs.pydantic.dev) | Request validation | MIT |
| [Uvicorn](https://www.uvicorn.org) | Web server | BSD-3-Clause |
| [websockets](https://websockets.readthedocs.io) | WebSocket connections | BSD-3-Clause |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | mDNS discovery for the remote (via ucapi) | LGPL-2.1-or-later |
| [pyee](https://github.com/jfhbrook/pyee) | Event handling (via ucapi) | MIT |

These and their dependencies are installed from PyPI at the versions in `requirements.txt`.

## Blackmagic Design DeckLink SDK

`dlcap`, the capture program, is compiled against the DeckLink SDK's `Linux/include` files
(`DeckLinkAPI*.h`, `LinuxCOM.h`, `DeckLinkAPIDispatch.cpp`). Copyright © Blackmagic Design. Those files
carry their own license text, which is kept intact wherever they appear. The rest of the SDK (samples,
documentation, tools) is not used or distributed.

The Desktop Video driver and `libDeckLinkAPI.so` are **not** part of this project or its image. Install them
from Blackmagic Design's website; the container uses the copy installed on your computer.

Blackmagic Design and DeckLink are trademarks of Blackmagic Design Pty. Ltd. This project is compatible with
Blackmagic Design DeckLink capture cards. It is not affiliated with or endorsed by Blackmagic Design.

## Unfolded Circle

Unfolded Circle, Remote Two and Remote 3 are trademarks of Unfolded Circle ApS. The remote integration
follows the [Unfolded Circle Integration API](https://github.com/unfoldedcircle/core-api). This project is
not affiliated with or endorsed by Unfolded Circle.

## NVIDIA

At runtime the NVIDIA Container Toolkit makes the host's NVIDIA driver libraries available inside the
container. They are not part of the image. NVIDIA is a trademark of NVIDIA Corporation.
