#!/usr/bin/env bash
# Fill in mpv-vproc-web's .env from what this computer reports, and check the
# prerequisites from docs/INSTALL.md along the way.
#
#   bash setup-env.sh            # in the folder that holds compose.yml
#
# It only reads from the system; the one file it writes is .env (an existing
# .env is kept as .env.bak first). Run it again whenever the hardware changes.

set -euo pipefail

ENV_FILE=${ENV_FILE:-.env}
TEMPLATE=${TEMPLATE:-.env.example}
TEMPLATE_URL=https://raw.githubusercontent.com/fattykidd/mpv-vproc-web/main/.env.example
DRM_SYSFS=${DRM_SYSFS:-/sys/class/drm}
DEFAULT_LIB=/usr/lib/libDeckLinkAPI.so

problems=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; return 0; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; problems=$((problems + 1)); }

# Set NAME=VALUE in .env: replaces the first "NAME=" or "#NAME=" line, else appends.
set_var() {
    local name=$1 value=$2
    if grep -qE "^#?${name}=" "$ENV_FILE"; then
        awk -v n="$name" -v v="$value" '
            !done && $0 ~ "^#?" n "=" { print n "=" v; done = 1; next } { print }
        ' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
    else
        printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
    fi
}

gid_of() { getent group "$1" | cut -d: -f3; }

echo "Preparing $ENV_FILE"
if [ ! -f "$TEMPLATE" ]; then
    curl -fsSL -o "$TEMPLATE" "$TEMPLATE_URL"
fi
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$ENV_FILE.bak"
    echo "  (your previous settings are saved in $ENV_FILE.bak)"
else
    cp "$TEMPLATE" "$ENV_FILE"
fi

echo
echo "Graphics card"
video_gid=$(gid_of video || true)
render_gid=$(gid_of render || true)
if [ -n "$video_gid" ]; then
    set_var VIDEO_GID "$video_gid"; ok "video group id: $video_gid"
else
    fail "no 'video' group on this computer" "is the graphics driver installed? (step 3)"
fi
if [ -n "$render_gid" ]; then
    set_var RENDER_GID "$render_gid"; ok "render group id: $render_gid"
else
    warn "no 'render' group on this computer" "older systems may not have one; the video group is usually enough"
fi
if command -v nvidia-smi > /dev/null; then
    ok "NVIDIA driver: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -n 1)"
    modeset=$(cat /sys/module/nvidia_drm/parameters/modeset 2>/dev/null || echo "?")
    if [ "$modeset" = "Y" ]; then
        ok "modesetting is on"
    else
        fail "modesetting is off" "turn it on (step 3, 'Make sure modesetting is on')"
    fi
fi

echo
echo "Display"
connected=()
for status in "$DRM_SYSFS"/card*-*/status; do
    [ -f "$status" ] || continue
    [ "$(cat "$status")" = "connected" ] && connected+=("$(dirname "$status")")
done
if [ ${#connected[@]} -eq 0 ]; then
    fail "no connected display found" "check the cable and that the projector/TV is switched on, then run this again"
else
    if [ ${#connected[@]} -gt 1 ]; then
        for dir in "${connected[@]}"; do
            name=$(basename "$dir")
            echo "        connected: ${name#card*-} ($(head -n 1 "$dir/modes" 2>/dev/null || echo "no modes"))"
        done
        warn "several displays are connected; the resolution below is the first one's" \
             "if that's not your projector/TV, change VPROC_OUTPUT_SIZE in .env, and set VPROC_VULKAN_DISPLAY if the picture appears on the wrong one"
    fi
    port=$(basename "${connected[0]}"); port=${port#card*-}
    size=$(head -n 1 "${connected[0]}/modes" 2>/dev/null || true)
    if [ -n "$size" ]; then
        set_var VPROC_OUTPUT_SIZE "$size"; ok "$port: $size"
    else
        warn "$port is connected but reports no modes" "set VPROC_OUTPUT_SIZE in .env by hand"
    fi
fi

echo
echo "Capture card"
if [ -d /dev/blackmagic ]; then
    ok "inputs: $(ls /dev/blackmagic | tr '\n' ' ')"
else
    fail "no /dev/blackmagic" "install Desktop Video and reboot (step 4)"
fi
if command -v BlackmagicFirmwareUpdater > /dev/null; then
    firmware=$(BlackmagicFirmwareUpdater status 2>/dev/null || true)
    if grep -q PLEASE_UPDATE <<< "$firmware"; then
        fail "the card's firmware needs updating; until it is, no program can use the card" \
             "sudo BlackmagicFirmwareUpdater update 0, then power off completely and unplug for 10 s (step 4)"
    elif [ -n "$firmware" ]; then
        ok "card firmware is up to date"
    fi
fi
lib=$(ldconfig -p 2>/dev/null | awk '/libDeckLinkAPI\.so / { print $NF; exit }')
if [ -z "$lib" ]; then
    fail "libDeckLinkAPI.so not found" "install Desktop Video (step 4)"
elif [ "$lib" = "$DEFAULT_LIB" ]; then
    ok "capture library: $lib"
else
    set_var DECKLINK_LIB "$lib"; ok "capture library: $lib"
fi

echo
echo "Docker"
if ! command -v docker > /dev/null; then
    fail "Docker isn't installed" "step 5"
elif ! docker info > /dev/null 2>&1; then
    fail "can't talk to Docker" "log out and back in after 'usermod -aG docker' (step 5), or start it: sudo systemctl enable --now docker"
else
    ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
    if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
        ok "NVIDIA Container Toolkit"
    elif command -v nvidia-smi > /dev/null; then
        fail "Docker doesn't know about the NVIDIA Container Toolkit" "step 6"
    fi
fi

port=$(grep -E '^VPROC_PORT=' "$ENV_FILE" | cut -d= -f2 || true)
port=${port:-8000}
if command -v ss > /dev/null && ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx vproc; then
        ok "port $port is used by mpv-vproc-web itself"
    else
        warn "another program is using port $port" "set VPROC_PORT in .env to a free port, for example 8080"
    fi
fi

echo
if [ "$problems" -eq 0 ]; then
    echo "Done. $ENV_FILE is filled in. Next: docker compose pull && docker compose up -d"
else
    echo "$problems problem(s) above. Fix them and run this again; $ENV_FILE has what could be filled in."
    exit 1
fi
