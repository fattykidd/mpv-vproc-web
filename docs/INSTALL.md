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

> **HDCP is not supported.** HDCP is the copy protection many sources put on their HDMI output: most streaming
> boxes and apps, UHD Blu-ray players and many TV boxes. DeckLink cards are made for professional video and
> don't accept HDCP-protected signals, so a protected source shows **NO SIGNAL**. That's a limit of the capture
> card, and mpv-vproc-web can't change it. Some sources, such as game consoles and PCs, have a setting to turn
> HDCP off; with others, check what your player offers before you buy hardware for this project.

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

### Going back to a desktop

If you need the desktop again later, for example to use Blackmagic's setup app in step 4, stop mpv-vproc-web first
so the two don't fight over the display:

```sh
cd ~/mpv-vproc-web && docker compose stop    # skip this if you haven't installed it yet
```

Then either start the desktop **just this once** (it's gone again after the next reboot):
```sh
sudo systemctl isolate graphical.target
```
or make it start **at every boot** again:
```sh
sudo systemctl set-default graphical.target
sudo reboot
```

To return to mpv-vproc-web afterwards, switch back to `multi-user.target` as above, reboot, and run
`docker compose start` in `~/mpv-vproc-web`.

---

## 3. Graphics driver

### Install it

**Ubuntu / Debian**
```sh
sudo apt update
sudo ubuntu-drivers install        # Ubuntu: installs the recommended NVIDIA driver
sudo reboot
```
On Debian, follow the Debian wiki's [NvidiaGraphicsDrivers](https://wiki.debian.org/NvidiaGraphicsDrivers) page.

**Fedora**: follow RPM Fusion's [NVIDIA howto](https://rpmfusion.org/Howto/NVIDIA), then reboot.

**RHEL** (and Rocky, AlmaLinux): follow NVIDIA's
[driver installation guide](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/) for your
release, then reboot.

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

1. On another computer, open Blackmagic Design's
   [Capture and Playback support page](https://www.blackmagicdesign.com/support/family/capture-and-playback)
   and download the latest **Desktop Video** for **Linux** (it asks for a few registration details first). Copy the downloaded `.tar.gz` file to the server (for
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

### Update the card's firmware

The firmware is the software that runs on the card itself. Each Desktop Video version expects a particular one,
and **a card that is new to this computer usually needs updating**. Until it is, the driver won't let any program
use the card: mpv-vproc-web will only say `no DeckLink devices found`, even though `ls /dev/blackmagic/` lists the
inputs. Check it:
```sh
BlackmagicFirmwareUpdater status
```
Each input gets a line ending in `OK` or `PLEASE_UPDATE`. If any says `PLEASE_UPDATE`:
1. Update the card (it takes a minute or two; don't interrupt it):
   ```sh
   sudo BlackmagicFirmwareUpdater update 0
   ```
   On cards with several inputs, such as the Quad HDMI Recorder, this updates the whole card. Run `status`
   again, and if any line still says `PLEASE_UPDATE`, update that number too.
2. The new firmware only loads once the card has been without power, so a reboot isn't enough. Turn the
   computer off, unplug it for about 10 seconds, and start it again:
   ```sh
   sudo poweroff
   ```
3. `BlackmagicFirmwareUpdater status` should now say `OK` everywhere.

Do this again after updating Desktop Video: a newer driver can expect newer firmware. (`sudo dmesg | grep -i
blackmagic` shows a `firmware version mismatch` warning when it does.)

### Choose the card's input

Some cards have more than one kind of input. The DeckLink Mini Recorder 4K, for example, has both SDI and HDMI,
and it has to be told to use **HDMI**, otherwise it reports no signal even with a source connected. Cards with
only HDMI inputs, such as the DeckLink Quad HDMI Recorder, have nothing to choose and work straight away, so
you can skip this.

The input is chosen in Blackmagic's **Desktop Video Setup** app. Blackmagic doesn't make a text (terminal) version
of it: it's a graphical app, so on a server without a desktop you have two ways to reach it. You only need to do
this once per card: the choice is stored on the computer and kept across reboots and updates.

First install the app. It's in the `desktopvideo-gui` package, in the same folder you installed from above:
- **Ubuntu / Debian:** `sudo apt install ./deb/x86_64/desktopvideo-gui_*.deb`
- **Fedora / RHEL:** `sudo dnf install ./rpm/x86_64/desktopvideo-gui-*.rpm`

**Way 1: show its window on your own computer, over SSH** (nothing changes on the server's display).
This uses "X forwarding": the app runs on the server, and its window is drawn on the computer you're sitting at.
1. On the server, install the small helper this needs:
   - Ubuntu / Debian: `sudo apt install xauth`
   - Fedora / RHEL: `sudo dnf install xorg-x11-xauth`
2. On the computer you're sitting at, you need something that can draw the window:
   - **Linux** with a desktop: nothing to install.
   - **Windows**: use [MobaXterm](https://mobaxterm.mobatek.net/) (free Home edition) as your SSH program;
     it draws forwarded windows on its own.
   - **macOS**: install [XQuartz](https://www.xquartz.org/), then log out and back in.
3. Connect with `-X` added (in MobaXterm, just start an SSH session; X forwarding is on by default):
   ```sh
   ssh -X you@server
   ```
4. Start the app:
   ```sh
   BlackmagicDesktopVideoSetup
   ```
   After a few seconds its window opens on your computer. If you see `cannot open display` or `could not connect
   to display`, X forwarding isn't working: check you connected with `-X` and installed `xauth` on the server.

**Way 2: start the desktop for a moment.** If the server has a desktop installed and a screen and mouse attached,
start it with `sudo systemctl isolate graphical.target` (see [Going back to a desktop](#going-back-to-a-desktop)),
open **Blackmagic Desktop Video Setup** from the applications menu, and reboot when you're done.

In the app:
1. The home page shows your card. Click its picture (or the round settings icon below it).
2. In the **Video** tab, under **Video Input**, choose **HDMI**.
3. Click **Save**, and close the app.

With a source connected and playing, the home page shows the detected format under the input icon, for example
`2160p23.98`. That's a good sign everything up to the card works.

The **DeckLink Quad HDMI Recorder** shows up as four separate cards, one per HDMI input, named
`DeckLink Quad HDMI Recorder (1)` to `(4)`. Once mpv-vproc-web is running, choose the input in the web page: the
menu next to **Input** lists them all. Your choice is remembered, even after restarts and updates.

mpv-vproc-web starts on the first input until you choose another. To change that starting point instead, put the
input's `id:` from the doctor (step 9) in `.env`, for example `VPROC_DEVICE=id:3255474416`. The id stays the same
even if the card moves to another slot, so it's safer than the input's number.

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

The first time you run `hello-world`, it starts with `Unable to find image 'hello-world:latest' locally` and a
few `Pull complete` lines. That isn't an error: Docker didn't have that tiny test container yet, so it downloaded
it before running it. If the "Hello from Docker!" message follows, Docker can download and run containers, which
is everything it needs to do for mpv-vproc-web. (If you get `permission denied`, you haven't logged out and back
in since the `usermod` command.)

---

## 6. NVIDIA Container Toolkit

This lets containers use the NVIDIA graphics card. Follow NVIDIA's guide
**[Installing the NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)**
for your distribution (it has a section each for apt and dnf): it's a few commands to add their package source and
install `nvidia-container-toolkit`. Then connect it to Docker:

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

You need two files: `compose.yml` (tells Docker how to run the container) and `.env` (your settings). A helper
script fills in `.env` for you.

```sh
mkdir -p ~/mpv-vproc-web && cd ~/mpv-vproc-web
curl -fsSLO https://raw.githubusercontent.com/fattykidd/mpv-vproc-web/main/compose.yml
curl -fsSLO https://raw.githubusercontent.com/fattykidd/mpv-vproc-web/main/.env.example
curl -fsSLO https://raw.githubusercontent.com/fattykidd/mpv-vproc-web/main/scripts/setup-env.sh
bash setup-env.sh
```

The script looks up your group IDs, your display's resolution and where the capture library is, writes them into
`.env`, and double-checks steps 3 to 6 on the way. It only reads from your system; `.env` is the one file it
writes. The output looks like this:

```
Graphics card
  ok    video group id: 39
  ok    render group id: 105
  ok    NVIDIA driver: NVIDIA GeForce RTX 5060 Ti, 620.70.06
  ok    modesetting is on

Display
  ok    HDMI-A-1: 3840x2160

Capture card
  ok    inputs: io0
  ok    capture library: /lib64/libDeckLinkAPI.so

Docker
  ok    Docker 29.1.0
  ok    NVIDIA Container Toolkit

Done. .env is filled in. Next: docker compose pull && docker compose up -d
```

A `FAIL` line names the step to go back to. Fix it and run `bash setup-env.sh` again; it keeps your previous
`.env` as `.env.bak` each time.

Running it again only updates the values it detects; anything else you changed in `.env` stays as it is. If you
delete `.env` and start over, the script begins from the template, so add back any lines you changed yourself.

**Check the resolution.** If more than one display is connected, the script lists them all and uses the first
one's resolution. If that isn't your projector or TV, change `VPROC_OUTPUT_SIZE` (below).

Everything else in `.env` is optional and explained by the comments next to it. To look at or change it:
```sh
nano .env
```
Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

<details>
<summary><b>Filling in .env by hand instead</b></summary>

Copy the template: `cp .env.example .env`. Then collect three values.

**Your group IDs** (they allow the container to use the graphics card):
```sh
getent group video render
```
This prints something like `video:x:39:...` and `render:x:105:`. The numbers after the second colon are the IDs.

**Your display's resolution**: the largest 16:9 size it offers.
```sh
cat /sys/class/drm/card*-HDMI-A-*/modes 2>/dev/null | sort -u
```
For a 4K projector or TV this is `3840x2160`. Don't go by the first line: many 4K projectors list `1920x1080` first
as their "preferred" mode. Choose `3840x2160` rather than the wider cinema `4096x2160` if both are listed. Use `DP`
instead of `HDMI-A` if your display is connected by DisplayPort.

**The capture library path** from step 4.

Open `.env` with `nano .env` and change these lines near the top to your values:
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
</details>

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
| `device N: …` | Each capture input found, with its ID | `no DeckLink devices found`: the card's firmware needs updating ([step 4](#update-the-cards-firmware)), or the card was changed since the container was created (run `docker compose up -d --force-recreate`) |
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
- `the display has no 3840x2160@23.976 mode; using 3840x2160 instead`: your display can't switch to the
  source's frame rate, so it stays at its usual one. Normal for PC monitors, which rarely support 23.976/24 Hz.

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

### After changing the capture card

Docker gives the container the capture inputs that existed when it was created. After swapping, adding or
removing a card, a restart isn't enough; recreate the container:
```sh
cd ~/mpv-vproc-web
docker compose up -d --force-recreate
```
Check the card's firmware first ([step 4](#update-the-cards-firmware)), then choose the input again in the web page (and
update `VPROC_DEVICE` if you set it).

### Updating automatically with Watchtower

[Watchtower](https://hub.docker.com/r/nickfedor/watchtower) checks for new releases on a schedule and updates the
container for you. (This is Nicholas Fedor's maintained continuation of the original Watchtower project;
documentation at [watchtower.nickfedor.com](https://watchtower.nickfedor.com).)

mpv-vproc-web's `compose.yml` already carries the label that tells Watchtower it may update it. Set Watchtower up
in its own folder:

```sh
mkdir -p ~/watchtower && cd ~/watchtower
timedatectl show -p Timezone --value
nano compose.yml
```
The `timedatectl` command prints your time zone, for example `America/Edmonton`. Paste this into the empty file,
put your time zone on the `TZ` line, then save and exit (**Ctrl+O**, **Enter**, **Ctrl+X**):

```yaml
services:
  watchtower:
    image: nickfedor/watchtower
    container_name: watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      TZ: America/Edmonton
      # Only update containers that ask for it (mpv-vproc-web does), not everything on this computer.
      WATCHTOWER_LABEL_ENABLE: "true"
      # Check every day at 4 in the morning (seconds, minutes, hours, day, month, weekday).
      WATCHTOWER_SCHEDULE: "0 0 4 * * *"
      # Delete the old image after updating, so they don't fill up the disk.
      WATCHTOWER_CLEANUP: "true"
```

Start it and check that it's happy:
```sh
docker compose up -d
docker compose logs
```
The log should say Watchtower started and when it will next check.

Good to know:
- **`WATCHTOWER_LABEL_ENABLE` matters.** Without it, Watchtower updates *every* container on the computer, including
  ones you may not want changed unattended (a media server, for example).
- An update restarts mpv-vproc-web, so the picture goes black for a few seconds. That's why the example checks at
  4 a.m.; pick a time when nobody's watching.
- With `VPROC_VERSION` set in `.env`, Watchtower keeps you on that release (and picks up nothing new). Remove the line
  to follow the newest release.
- Your presets and settings are kept, just as with a manual update.

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
| **NO SIGNAL** although the source is playing | The source uses HDCP copy protection (most streaming boxes, UHD Blu-ray players), or the card's input is set to SDI | HDCP isn't supported (see [step 1](#1-what-you-need)); try another source or turn HDCP off in the source's settings if it has that option. For the input, see [Choose the card's input](#choose-the-cards-input) |
| `capture device not found` / `no DeckLink devices found` | Card firmware out of date, or the capture card was swapped or added | [Update the firmware](#update-the-cards-firmware); after changing cards, `docker compose up -d --force-recreate` |
| `could not enable video input` | Another program has the capture card open (Media Express, OBS, another container) | Close it |
| `Desktop Video driver not available` | The capture library path is wrong | `DECKLINK_LIB` in `.env` (step 4/8) |
| HDR content shows as SDR | The source isn't sending HDR, or the card's firmware is old | Check the source's video settings; `BlackmagicFirmwareUpdater status` |
| Dropped frames | The graphics card can't keep up with the chosen scalers | Lighter scaler settings ([SETTINGS.md](SETTINGS.md#scaling)); other programs using the card heavily (for example media-server transcoding) also take a share |

If you're stuck, open an issue at https://github.com/fattykidd/mpv-vproc-web/issues with the doctor's output
and the last 100 lines of `docker compose logs vproc`.
