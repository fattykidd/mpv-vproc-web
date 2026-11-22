# syntax=docker/dockerfile:1.7
#
# vproc: DeckLink capture -> mpv/libplacebo tone mapping and scaling -> HDMI.
#
# FFmpeg, libplacebo and mpv are built from pinned release tags. The DeckLink
# SDK headers come from a build context you supply (see compose.yml); nothing
# from Blackmagic is stored in this repository or the published image.

ARG DEBIAN_RELEASE=trixie
ARG FFMPEG_VERSION=9.0.2
ARG LIBPLACEBO_VERSION=7.387.0
ARG MPV_VERSION=0.42.0
ARG PREFIX=/opt/vproc

# --------------------------------------------------------------------------
# FFmpeg -> libplacebo -> mpv
# --------------------------------------------------------------------------
FROM debian:${DEBIAN_RELEASE}-slim AS media-build
ARG FFMPEG_VERSION LIBPLACEBO_VERSION MPV_VERSION PREFIX

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates git meson ninja-build nasm pkg-config python3 \
        libvulkan-dev libshaderc-dev liblcms2-dev libxxhash-dev \
        libass-dev libfreetype-dev libfribidi-dev libharfbuzz-dev \
        libdrm-dev libgbm-dev libegl-dev libgl-dev liblua5.2-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PKG_CONFIG_PATH=${PREFIX}/lib/pkgconfig \
    LD_LIBRARY_PATH=${PREFIX}/lib
WORKDIR /src

# FFmpeg only decodes the raw capture formats here (v210/r210/uyvy/bgra) and
# provides bwdif for interlaced input; autodetection stays off so the build
# doesn't silently pick up libraries the runtime image lacks.
RUN git clone --depth 1 --branch "n${FFMPEG_VERSION}" https://git.ffmpeg.org/ffmpeg.git ffmpeg \
    && cd ffmpeg \
    && ./configure \
        --prefix="${PREFIX}" \
        --enable-gpl --enable-shared --disable-static \
        --disable-programs --disable-doc --disable-debug \
        --disable-autodetect --enable-zlib --enable-iconv \
        --disable-network \
    && make -j"$(nproc)" && make install

RUN git clone --depth 1 --recurse-submodules --shallow-submodules --branch "v${LIBPLACEBO_VERSION}" \
        https://code.videolan.org/videolan/libplacebo.git libplacebo \
    && meson setup libplacebo/build libplacebo \
        --prefix="${PREFIX}" --libdir=lib --buildtype=release \
        -Dvulkan=enabled -Dopengl=enabled -Dshaderc=enabled -Dlcms=enabled \
        -Dlibdovi=disabled -Ddemos=false -Dtests=false \
    && ninja -C libplacebo/build install

# mpv's DRM output reads the display's EDID through libdisplay-info.
RUN apt-get update && apt-get install -y --no-install-recommends libdisplay-info-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "v${MPV_VERSION}" https://github.com/mpv-player/mpv.git mpv \
    && meson setup mpv/build mpv \
        --prefix="${PREFIX}" --libdir=lib --buildtype=release \
        -Dlibmpv=false -Dcplayer=true -Dmanpage-build=disabled \
        -Dgl=enabled -Degl=enabled -Dgbm=enabled -Ddrm=enabled -Dvulkan=enabled -Dlua=enabled \
        -Dx11=disabled -Dwayland=disabled \
    && ninja -C mpv/build install \
    && "${PREFIX}/bin/mpv" --version

# --------------------------------------------------------------------------
# dlcap (needs the DeckLink SDK headers from the decklink_sdk build context)
# --------------------------------------------------------------------------
FROM debian:${DEBIAN_RELEASE}-slim AS dlcap-build
ARG PREFIX
RUN apt-get update && apt-get install -y --no-install-recommends g++ make \
    && rm -rf /var/lib/apt/lists/*
COPY --from=decklink_sdk . /opt/decklink-sdk/include/
COPY capture/ /src/capture/
RUN test -f /opt/decklink-sdk/include/DeckLinkAPI.h \
        || { echo >&2 "DECKLINK_SDK_INCLUDE must point at the SDK's Linux/include folder"; exit 1; } \
    && make -C /src/capture DECKLINK_SDK_INCLUDE=/opt/decklink-sdk/include PREFIX="${PREFIX}" install

# --------------------------------------------------------------------------
# Python dependencies
# --------------------------------------------------------------------------
FROM debian:${DEBIAN_RELEASE}-slim AS python-deps
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------
FROM debian:${DEBIAN_RELEASE}-slim
ARG PREFIX

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        libvulkan1 libshaderc1 liblcms2-2 libxxhash0 \
        libass9 libfreetype6 libfribidi0 libharfbuzz0b fonts-dejavu-core \
        libdrm2 libdisplay-info2 libgbm1 libegl1 libgl1 libglx0 liblua5.2-0 zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=media-build ${PREFIX} ${PREFIX}
COPY --from=dlcap-build ${PREFIX}/bin/dlcap ${PREFIX}/bin/dlcap
COPY --from=python-deps /opt/venv /opt/venv

# Fail the build now, not on the projector, if a shared library is missing.
RUN echo "${PREFIX}/lib" > /etc/ld.so.conf.d/vproc.conf && ldconfig \
    && ! ldd ${PREFIX}/bin/mpv | grep "not found" \
    && ${PREFIX}/bin/mpv --version | head -n 3

RUN useradd --system --uid 1000 --home-dir /data --shell /usr/sbin/nologin vproc \
    && install -d -o vproc -g vproc /data /run/vproc

WORKDIR /app
COPY vproc/ vproc/
COPY web/ web/
COPY shaders/ shaders/

ENV PATH=${PREFIX}/bin:/opt/venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    VPROC_WEB_DIR=/app/web \
    VPROC_DATA_DIR=/data \
    VPROC_RUN_DIR=/run/vproc

USER vproc
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('VPROC_PORT', '8000')}/healthz\", timeout=4)"]

CMD ["python3", "-m", "vproc"]
