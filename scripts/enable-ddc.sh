#!/usr/bin/env bash
# Lets mpv-vproc-web read the display's HDMI link status, so it can bring the picture
# back by itself when a projector or TV switches inputs and returns without it.
#
#   sudo bash enable-ddc.sh      # in the folder that holds compose.yml and .env
#
# It finds the graphics card's DDC bus (the I2C channel in the HDMI cable) that the
# display answers on, and installs a udev rule that names it /dev/vproc-ddc and lets
# the video group read it. Undo: delete /etc/udev/rules.d/90-vproc-ddc.rules and
# /etc/modules-load.d/vproc-i2c-dev.conf, and remove VPROC_DDC_DEVICE from .env.

set -euo pipefail

RULE=/etc/udev/rules.d/90-vproc-ddc.rules
MODULES=/etc/modules-load.d/vproc-i2c-dev.conf
ENV_FILE=${ENV_FILE:-.env}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: sudo bash $0"
    exit 1
fi
if ! command -v i2cget > /dev/null; then
    echo "This needs i2cget. Install it first:"
    echo "  Ubuntu / Debian:  sudo apt install i2c-tools"
    echo "  Fedora / RHEL:    sudo dnf install i2c-tools"
    exit 1
fi

modprobe i2c-dev
echo i2c-dev > "$MODULES"   # load it at every boot too

found=""
for dev in /sys/class/i2c-dev/i2c-*; do
    name=$(cat "$dev/name")
    # Only graphics cards' display buses. Other buses (memory, sensors) can
    # answer on the same addresses, so they are never touched.
    case "$name" in
        NVIDIA*|AMDGPU*|amdgpu*|i915*|nouveau*|radeon*) ;;
        *) continue ;;
    esac
    bus=${dev##*/i2c-}
    # A display answers its EDID (0x50); HDMI 2.0 displays also have SCDC (0x54).
    [ "$(i2cget -y "$bus" 0x50 0x00 2>/dev/null)" = "0x00" ] || continue
    if ! i2cget -y "$bus" 0x54 0x40 > /dev/null 2>&1; then
        echo "  $name (i2c-$bus): a display, but it doesn't report its HDMI 2.0 link status"
        continue
    fi
    echo "  $name (i2c-$bus): display found, link status $(i2cget -y "$bus" 0x54 0x40)"
    if [ -n "$found" ]; then
        echo "More than one display reports its link status. Unplug the ones mpv-vproc-web doesn't use and run this again."
        exit 1
    fi
    found=$name
done

if [ -z "$found" ]; then
    echo "No display reporting its HDMI 2.0 link status was found. Is it switched on and showing the picture?"
    exit 1
fi

cat > "$RULE" <<EOF
# mpv-vproc-web: the display's DDC bus, readable by the video group (scripts/enable-ddc.sh).
SUBSYSTEM=="i2c-dev", ATTR{name}=="$found", SYMLINK+="vproc-ddc", GROUP="video", MODE="0660"
EOF
udevadm control --reload
udevadm trigger --subsystem-match=i2c-dev --action=add
udevadm settle
if [ ! -e /dev/vproc-ddc ]; then
    echo "The rule is installed, but /dev/vproc-ddc didn't appear. Reboot and check: ls -l /dev/vproc-ddc"
    exit 1
fi
echo "  /dev/vproc-ddc -> $(readlink -f /dev/vproc-ddc)"

# Tell compose.yml to pass it into the container. Written in place so .env keeps its owner.
if [ -f "$ENV_FILE" ]; then
    if grep -qE '^#?VPROC_DDC_DEVICE=' "$ENV_FILE"; then
        updated=$(awk '!done && /^#?VPROC_DDC_DEVICE=/ { print "VPROC_DDC_DEVICE=/dev/vproc-ddc"; done = 1; next } { print }' "$ENV_FILE")
        printf '%s\n' "$updated" > "$ENV_FILE"
    else
        printf 'VPROC_DDC_DEVICE=/dev/vproc-ddc\n' >> "$ENV_FILE"
    fi
    echo "  $ENV_FILE: VPROC_DDC_DEVICE=/dev/vproc-ddc"
else
    echo "No $ENV_FILE here: add VPROC_DDC_DEVICE=/dev/vproc-ddc to your .env yourself."
fi

echo
echo "Done. Apply it with: docker compose up -d"
