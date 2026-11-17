# Installing mpv-vproc-web, step by step

This guide takes you from "I have Linux installed on a computer with a capture card and a graphics card" to
a picture on your projector or TV, with HDR tone-mapped the way you want it. No programming knowledge is
needed. You'll copy commands into a terminal; after each one, the guide says what you should see.

> **Words used in this guide**
> - **Terminal**: the text window where you type commands. On a server, it's what you see after logging in
>   (directly or over `ssh`).
> - **Host**: the computer itself, as opposed to the **container**, a self-contained package that holds
>   mpv-vproc-web and everything it needs. Docker runs the container.
> - `sudo` in front of a command runs it as administrator; it asks for your password.

**Contents**
1. [What you need](#1-what-you-need)
2. [Make sure no desktop is using the graphics card](#2-make-sure-no-desktop-is-using-the-graphics-card)
3. [Graphics driver](#3-graphics-driver)
4. [Blackmagic Desktop Video](#4-blackmagic-desktop-video)
5. [Docker](#5-docker)
6. [NVIDIA Container Toolkit](#6-nvidia-container-toolkit)
7. [Firewall](#7-firewall)
8. [Download mpv-vproc-web and fill in your settings](#8-download-mpv-vproc-web-and-fill-in-your-settings)
9. [Start it and run the doctor](#9-start-it-and-run-the-doctor)
10. [Your first picture](#10-your-first-picture)
11. [Unfolded Circle Remote (optional)](#11-unfolded-circle-remote-optional)
12. [Updating, stopping and removing](#12-updating-stopping-and-removing)
13. [When something doesn't work](#13-when-something-doesnt-work)

Once it's running, read **[SETTINGS.md](SETTINGS.md)**: it explains every picture setting in plain language.

---

## 1. What you need

**Hardware**
- **A graphics card with Vulkan support.** Developed and tested on an NVIDIA GeForce RTX 5060 Ti; any recent
  NVIDIA card should work. AMD and Intel cards with Vulkan *may* work but haven't been tested yet. Reports welcome.
- **A Blackmagic DeckLink capture card** supported by Desktop Video. Tested with the DeckLink Mini Recorder 4K.
- **Your projector or TV connected directly to the graphics card.** The source (player, streaming box) goes
  into the capture card's HDMI input.
- Audio isn't handled: take it off in your AV receiver before the video reaches the capture card.

**Software**
- **A 64-bit Linux installation** (x86_64). The commands below are given for **Ubuntu / Debian** and for
  **Fedora / RHEL** (and their relatives such as Rocky or AlmaLinux). Other distributions work too, but you'll
  need to translate the package commands.
- A "server" or "minimal" install without a desktop is ideal. A desktop is fine as long as it doesn't use the
  graphics card's output that feeds your display (see step 2).

**Secure Boot.** The NVIDIA and Blackmagic drivers are kernel modules built on your computer. If Secure Boot is
on in your BIOS/UEFI, Linux refuses to load them unless they're signed. The simplest fix is to turn Secure Boot
off. Alternatively, the driver installers offer to "enroll a key" (MOK); accept, set a password, and confirm it
in the blue screen at the next reboot.

---

## 2. Make sure no desktop is using the graphics card

mpv-vproc-web takes over the graphics card's video output directly, so nothing else may be showing a picture on it.

```sh
systemctl get-default
```

- `multi-user.target`: no desktop starts. Good, go on.
- `graphical.target`: a desktop starts at boot. Switch it off (you can switch it back the same way with
  `graphical.target`):
  ```sh
  sudo systemctl set-default multi-user.target
  sudo reboot
  ```

If you previously used mpv or another player on this output, stop it too.

---

## 3. Graphics driver

### Install it

**Ubuntu / Debian**
```sh
sudo apt update
sudo ubuntu-drivers install        # Ubuntu: installs the recommended NVIDIA driver
sudo reboot
```
On Debian, follow the Debian wiki page "NvidiaGraphicsDrivers".

**Fedora / RHEL**: follow NVIDIA's or RPM Fusion's instructions for your release (search "RPM Fusion NVIDIA" for
Fedora, "NVIDIA driver RHEL" for RHEL), then reboot.

### Check it

```sh
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
```
You should see your card's name and the driver version, for example `NVIDIA GeForce RTX 5060 Ti, 620.70.06`.

### Make sure "modesetting" is on

This lets programs drive the video output directly, without a desktop.

```sh
cat /sys/module/nvidia_drm/parameters/modeset
```
- `Y`: good, go on. (Recent drivers turn it on by default.)
- `N`: turn it on:
  - **Ubuntu / Debian:** open `/etc/default/grub` with `sudo nano /etc/default/grub`, find the line
    `GRUB_CMDLINE_LINUX_DEFAULT="..."` and add ` nvidia-drm.modeset=1` inside the quotes. Save
    (Ctrl+O, Enter, Ctrl+X), then:
    ```sh
    sudo update-grub
    sudo reboot
    ```
  - **Fedora / RHEL:**
    ```sh
    sudo grubby --update-kernel=ALL --args="nvidia-drm.modeset=1"
    sudo reboot
    ```
  Afterwards the `cat` command above should print `Y`.

### Check that your display is detected

```sh
for s in /sys/class/drm/card*-*/status; do echo "$(basename $(dirname $s)): $(cat $s)"; done
```
One line should say `connected`, for example `card1-HDMI-A-1: connected`. If none does, check the cable and
that the display is on.

---

## 4. Blackmagic Desktop Video

Desktop Video is Blackmagic's driver for the capture card.

1. On another computer, go to Blackmagic Design's **Support Center**, choose **Capture and Playback**, and
   download the latest **Desktop Video for Linux**. Copy the downloaded `.tar.gz` file to the server (for
   example with `scp`, or a USB stick).
2. The driver is compiled for your kernel during installation, so install the tools for that first:
   - **Ubuntu / Debian:**
     ```sh
     sudo apt install dkms linux-headers-$(uname -r)
     ```
   - **Fedora / RHEL** (on RHEL, `dkms` comes from the EPEL repository):
     ```sh
     sudo dnf install dkms kernel-devel-$(uname -r)
     ```
3. Unpack and install. The file and folder names include the version number; type the first few letters and
   press **Tab** to complete them.
   ```sh
   tar xzf Blackmagic_Desktop_Video_Linux_*.tar.gz
   cd Blackmagic_Desktop_Video_Linux_*/
   ls
   ```
   You'll see a `deb` and an `rpm` folder.
   - **Ubuntu / Debian:** `sudo apt install ./deb/x86_64/desktopvideo_*.deb`
   - **Fedora / RHEL:** `sudo dnf install ./rpm/x86_64/desktopvideo-*.rpm`
4. Reboot: `sudo reboot`

### Check it

```sh
ls /dev/blackmagic/
ldconfig -p | grep libDeckLinkAPI
```
- The first should list at least `io0` (one per input).
- The second shows where the capture library is. **Note the path**: on Ubuntu/Debian it's usually
  `/usr/lib/libDeckLinkAPI.so`, on Fedora/RHEL `/lib64/libDeckLinkAPI.so`. You'll need it in step 8.

Desktop Video also installs a firmware tool. If your card's firmware is out of date, the card may not work
properly until it's updated:
```sh
BlackmagicFirmwareUpdater status
```
If it reports that an update is available, run the update it suggests, then power the computer off completely
and on again.

---

## 5. Docker

Docker runs the mpv-vproc-web container.

**Ubuntu / Debian** (Docker's official install script):
```sh
curl -fsSL https://get.docker.com | sudo sh
```

**Fedora / RHEL:**
```sh
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo   # Fedora: .../linux/fedora/docker-ce.repo
sudo dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Then let your user run Docker without `sudo`, and log out and back in for it to take effect:
```sh
sudo usermod -aG docker $USER
exit
```

### Check it

After logging back in:
```sh
docker run --rm hello-world
docker compose version
```
The first prints "Hello from Docker!", the second a version number.

---

## 6. NVIDIA Container Toolkit

This lets containers use the NVIDIA graphics card. Follow NVIDIA's guide **"Installing the NVIDIA Container
Toolkit"** (docs.nvidia.com/datacenter/cloud-native/container-toolkit/) for your distribution: it's a few
commands to add their package source and install `nvidia-container-toolkit`. Then connect it to Docker:

```sh
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Check it

```sh
docker run --rm --gpus all ubuntu nvidia-smi
```
You should see the same table `nvidia-smi` shows on the host. If you get an error, the toolkit isn't
installed or Docker wasn't restarted.

---

## 7. Firewall

mpv-vproc-web uses port **8000** for its web page and, if you use an Unfolded Circle Remote, port **9095** plus
mDNS (how the remote finds it).

**Ubuntu (ufw)**: only needed if `sudo ufw status` says `active`:
```sh
sudo ufw allow 8000/tcp
sudo ufw allow 9095/tcp
sudo ufw allow 5353/udp
```

**Fedora / RHEL (firewalld)**:
```sh
sudo firewall-cmd --permanent --add-port=8000/tcp --add-port=9095/tcp --add-service=mdns
sudo firewall-cmd --reload
```

Check that nothing else already uses port 8000:
```sh
ss -ltn | grep ':8000 '
```
This should print nothing. If it prints a line, another program has that port; you can give mpv-vproc-web a
different one in step 8 (`VPROC_PORT`).

---

## 8. Download mpv-vproc-web and fill in your settings

You only need two files: `compose.yml` (tells Docker how to run the container) and `.env` (your settings).

```sh
mkdir -p ~/mpv-vproc-web && cd ~/mpv-vproc-web
curl -fsSLO https://raw.githubusercontent.com/fattykidd/mpv-vproc-web/main/compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/fattykidd/mpv-vproc-web/main/.env.example
ls -a
```
You should see `compose.yml` and `.env`.

Now collect three values.

**Your group IDs** (they allow the container to use the graphics card):
```sh
getent group video render
```
This prints something like `video:x:39:...` and `render:x:105:`. The numbers after the second colon are the IDs.

**Your display's resolution** (the first mode listed is the display's preferred one):
```sh
cat /sys/class/drm/card*-HDMI-A-*/modes 2>/dev/null | head -n 1
```
For a 4K projector this is `3840x2160`. Use a `DP` instead of `HDMI-A` if your display is connected by DisplayPort.

**The capture library path** from step 4.

Open the settings file:
```sh
nano .env
```
Change these lines near the top to your values:
```sh
VIDEO_GID=39
RENDER_GID=105
VPROC_OUTPUT_SIZE=3840x2160
```
If your capture library isn't at `/usr/lib/libDeckLinkAPI.so` (Fedora/RHEL), find the `#DECKLINK_LIB=` line,
remove the `#` and set your path:
```sh
DECKLINK_LIB=/lib64/libDeckLinkAPI.so
```
Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**. Everything else in the file is optional and
explained by the comments next to it.

---

## 9. Start it and run the doctor

```sh
cd ~/mpv-vproc-web
docker compose pull
docker compose up -d
```
`pull` downloads the image (about 1 GB the first time). `up -d` starts it in the background, and it starts
again automatically after a reboot.

### The doctor

The **doctor** is a built-in check of everything mpv-vproc-web depends on. Run it whenever something doesn't
work. It's the quickest way to find out why.

```sh
docker compose exec vproc python3 -m vproc.doctor
```

Each line starts with `ok`, `warn` or `FAIL`. Lines that need attention have a second, indented line telling
you what to do. A healthy system looks like this:

```
ok    /dev/dri/card1 is usable
ok    NVIDIA device nodes present
ok    NVIDIA Vulkan ICD
ok    display on HDMI-A-1
      modes: 3840x2160, 2560x1440, 1920x1080, ...
ok    Blackmagic device nodes present
ok    libDeckLinkAPI.so loads
ok    Desktop Video driver
      16.4 (dlcap built with SDK 16.0)
ok    device 0: DeckLink Mini Recorder 4K
      id:779489462
ok    mpv v0.42.0 Copyright © 2000-2026 mpv/MPlayer/mplayer2 projects
      libplacebo version: v7.387.0 (v7.387.0)
ok    /data is writable

Everything needed is in place.
```

What each check means, and what to do if it fails:

| Check | What it means | If it fails |
|---|---|---|
| `/dev/dri/cardN is usable` | The container may drive the graphics card's outputs | `VIDEO_GID` in `.env` doesn't match `getent group video` (step 8) |
| `/dev/dri/renderDN is not writable` (warn) | Graphics rendering permission | `RENDER_GID` doesn't match `getent group render` |
| `NVIDIA device nodes present` | The container sees the NVIDIA card | NVIDIA Container Toolkit not installed or Docker not restarted (step 6). On AMD/Intel this warning is normal |
| `nvidia-drm modesetting is off` | Direct output is disabled in the driver | Turn on modesetting (step 3) |
| `NVIDIA Vulkan ICD` | Vulkan, which draws the picture, is available | The toolkit isn't giving the container graphics access; re-check step 6 |
| `display on …` | A display is connected, with the modes it supports | `no connected display`: cable, input or power on the display |
| `several displays connected` (warn) | More than one output is in use | If the picture shows on the wrong one, set `VPROC_VULKAN_DISPLAY` to `1`, `2`, … in `.env` |
| `refresh-rate matching needs VPROC_OUTPUT_SIZE` (warn) | Without it, the display stays at one refresh rate | Set `VPROC_OUTPUT_SIZE` (step 8) |
| `Blackmagic device nodes present` | The container sees the capture card | Desktop Video not installed, or the computer wasn't rebooted after installing (step 4) |
| `libDeckLinkAPI.so loads` | The capture library is available | `DECKLINK_LIB` in `.env` points to the wrong place (step 4 shows the right path) |
| `Desktop Video driver` | Driver version, and the SDK version mpv-vproc-web was built with | The driver should be the **same or newer** than the SDK version shown |
| `device N: …` | Each capture input found, with its ID | `no DeckLink devices found`: check `BlackmagicFirmwareUpdater status` on the host |
| `mpv …` | The video engine and its version | Shouldn't fail with the published image; report it if it does |
| `/data is writable` | Presets can be saved | The `vproc-data` volume has the wrong owner; remove it with `docker compose down -v` and start again (this resets your presets) |

When the doctor says **"Everything needed is in place."**, go on.

### Watching the log

```sh
docker compose logs -f
```
Shows what mpv-vproc-web is doing as it happens (press **Ctrl+C** to stop watching; it keeps running). Useful
lines to look for:
- `starting mpv: …` and `starting dlcap: …`: the video output and the capture program started.
- `playing segment 1: 2160p23.976 · HDR10 · …`: a source was detected and is being shown.
- `the display has no 3840x2160@23.976 mode; using preferred instead`: your display can't switch to the
  source's frame rate, so it stays at its default. Normal for PC monitors, which rarely support 23.976/24 Hz.

---

## 10. Your first picture

1. On a phone or computer on the same network, open **`http://<server>:8000`**, where `<server>` is the
   server's name or IP address (find it on the server with `hostname -I`).
2. Without a source, the screen shows **No signal** and the web page shows **NO SIGNAL**.
3. Turn your source on. Within a second or two the picture appears. The web page shows the signal, for
   example **HDR10 · 2160p23.976 · BT.2020 · 4:2:2 10-bit**, and switches to the matching preset.
4. **Before anything else**, tell it about your display. In the **Tone mapping** tab of the **HDR to SDR**
   preset, set **Display peak (nits)** to your display's measured brightness, and in the **Display** tab set
   **Display primaries** and **Display transfer** to what it's calibrated to. **[SETTINGS.md](SETTINGS.md)**
   explains how to find these values. These three settings matter more than any other.

Changes apply instantly and are saved to the preset you're editing.

**Is it keeping up?** Under **Tools**, press **Toggle stats overlay**. mpv shows its statistics on screen,
including how long each frame takes to render. If frames are dropped (the web page's **Dropped** count rises),
choose lighter scalers; [SETTINGS.md](SETTINGS.md#scaling) explains which ones.

---

## 11. Unfolded Circle Remote (optional)

1. On the web page, under **Integrations**, switch **Unfolded Circle Remote** on. It shows `On · port 9095`.
2. In the remote's web configurator, open **Integrations → Add new**. **mpv-vproc-web** appears in the list of
   discovered integrations. Select it, finish the setup (there's nothing to fill in), and choose the entities you
   want: the picture preset selector, the input and output displays, the auto-preset switch and the buttons.

If it doesn't appear, check step 7's firewall settings, and that the remote and the server are on the same
network (not separated by a VLAN or a guest network).

---

## 12. Updating, stopping and removing

**Update to the newest release:**
```sh
cd ~/mpv-vproc-web
docker compose pull
docker compose up -d
```
Your presets and settings are kept.

To stay on a particular release instead, set `VPROC_VERSION=0.1.0` (for example) in `.env`. Releases are
listed at https://github.com/fattykidd/mpv-vproc-web/releases.

**Stop it** (until you start it again with `docker compose up -d`):
```sh
docker compose down
```

**Remove everything**, including saved presets:
```sh
docker compose down -v
docker image rm ghcr.io/fattykidd/mpv-vproc-web
```

---

## 13. When something doesn't work

Start with the doctor (step 9). Beyond that, these are the problems seen so far:

| What you see | Likely cause | What to do |
|---|---|---|
| The web page doesn't load | Firewall, or another program on port 8000 | Step 7. Test on the server itself with `curl -s localhost:8000/healthz`: if that works, it's the firewall |
| Black screen, log says `no video output came up` | Something else is using the display (desktop, an old mpv service) or modesetting is off | Step 2 and step 3; `pgrep -a mpv` should print nothing outside the container |
| Picture works but the log says `the display has no …@23.976 mode` | The display doesn't support that refresh rate | Normal for PC monitors; projectors and TVs usually do support it |
| `could not enable video input` | Another program has the capture card open (Media Express, OBS, another container) | Close it |
| `Desktop Video driver not available` | The capture library path is wrong | `DECKLINK_LIB` in `.env` (step 4/8) |
| HDR content shows as SDR | The source isn't sending HDR, or the card's firmware is old | Check the source's video settings; `BlackmagicFirmwareUpdater status` |
| Dropped frames | The graphics card can't keep up with the chosen scalers | Lighter scaler settings ([SETTINGS.md](SETTINGS.md#scaling)); other programs using the card heavily (for example media-server transcoding) also take a share |

If you're stuck, open an issue at https://github.com/fattykidd/mpv-vproc-web/issues with the doctor's output
and the last 100 lines of `docker compose logs vproc`.
