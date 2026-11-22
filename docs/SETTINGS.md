# Picture settings, explained

Video processing has a lot of jargon. This page explains what every setting in mpv-vproc-web's web page
does, in plain language, and which ones are worth your time. You don't need to understand all of it:
the presets are a good starting point, and **three settings matter far more than the rest** (see
[Start here](#start-here)).

Behind the scenes, mpv-vproc-web uses [mpv](https://mpv.io) and its rendering library
[libplacebo](https://code.videolan.org/videolan/libplacebo). The setting names in the web page match mpv's
option names (shown in `code`), so you can also look them up in the
[mpv manual](https://mpv.io/manual/stable/).

**Contents**
- [A two-minute primer](#a-two-minute-primer)
- [Start here: tell it about your display](#start-here)
- [Presets and automatic switching](#presets-and-automatic-switching)
- [Tone mapping](#tone-mapping)
- [Display](#display)
- [Scaling](#scaling)
- [Debanding](#debanding)
- [Picture](#picture)
- [Geometry](#geometry)
- [Checking your changes](#checking-your-changes)

---

## A two-minute primer

**Brightness is measured in nits** (candelas per square metre). A typical SDR TV shows around 100–300 nits,
a projected image on a screen often only 50–150, and a bright HDR TV 1,000 or more.

**SDR** (standard dynamic range) video is made for a display of about 100 nits. Your projector or TV can show
it more or less as-is.

**HDR10** video is different: it records *absolute* brightness, up to 1,000, 4,000 or even 10,000 nits, with
much more saturated colours (the **BT.2020** colour range instead of SDR's **BT.709**). No projector can show
that directly. Something has to squeeze the brightness and colours into what your display can actually do,
without crushing the picture into something dull or washing out the highlights. That squeezing is
**tone mapping** (for brightness) and **gamut mapping** (for colour), and doing it well is what a video
processor is for.

**HLG** is another HDR format, mostly used for TV broadcasts. It's handled the same way.

**Dynamic tone mapping** means measuring the brightness of every frame and adapting the curve scene by scene,
rather than using one curve for the whole film. A dark cave scene then isn't treated like a sunlit desert.
mpv-vproc-web does this with **Dynamic peak detection**.

**Scaling** changes the picture's resolution to your display's: 1080p up to 4K, or 4K down to 1080p. Good
scaling keeps detail sharp without halos or jagged edges.

---

## Start here

In the web page, hover over (or tap) any setting's name for a short explanation of what it does and what to
try.

These three settings, in the **HDR to SDR** preset, decide how HDR looks on *your* display. Everything else is
fine-tuning.

### Display peak (nits) — `target-peak`

How bright your display's white really is, in nits. The tone mapping uses it to decide how much room there is
for highlights. Too high, and the picture looks dark and flat; too low, and it looks bright but highlights are
crushed together.

How to find it:
- **Best:** measure a full white screen with a meter (a colorimeter or a light meter with calibration
  software such as HCFR or DisplayCAL), in the picture mode you'll actually use.
- **Projectors, as an estimate:** nits ≈ lumens × screen gain ÷ (screen area in m² × 3.14).
  Example: 1,500 lumens on a 120" 16:9 screen (2.66 m × 1.49 m ≈ 3.97 m²) with gain 1.0 gives
  1,500 ÷ (3.97 × 3.14) ≈ **120 nits**.
  Use your projector's *calibrated* brightness if you know it. The figure on the box is usually from its
  brightest, least accurate mode, often twice the real-world value.
- **Not sure?** 100 for a projector, 200 for a typical TV in SDR mode.

### Display primaries — `target-prim`

The colour range (gamut) your display is set to. It has to match the display's picture mode, otherwise colours
come out over- or under-saturated:
- `bt.709`: the normal colour range for SDR. Use this if your display is in a standard, Rec.709 or "Cinema"
  SDR mode.
- `display-p3`: a wider range, with the normal (D65) white. Many projectors' "wide colour" or "P3" modes are
  this one.
- `dci-p3`: the same wide range with the cinema projector's slightly green-tinted white point. Rare in home
  displays; choose it only if your calibration says so.
- `bt.2020`: the full HDR colour range. Only if your display is genuinely calibrated to it.

The other values in the list (`bt.601-…`, `adobe`, `apple`) are for old standards and computer workflows.

### Display transfer — `target-trc`

How your display turns signal values into brightness (its "gamma"). It shapes how dark the shadows and midtones
look:
- `bt.1886`: the standard for SDR video in a dark room. A good default for projectors.
- `gamma2.4`: similar, for dark rooms.
- `gamma2.2`: a little brighter in the shadows; for rooms with some light, and for most PC monitors.

The list has more values (`srgb`, other gammas, `pq`, `hlg` and so on) for computer monitors, special
calibrations and HDR output; you won't need them for a display in SDR mode.

Match what your display is calibrated to. If you don't know, `bt.1886` for a projector and `gamma2.2` for a
monitor are safe choices.

---

## Presets and automatic switching

A **preset** is a saved set of settings. Two come with mpv-vproc-web:
- **SDR** for normal video. It mostly sets the display and scaling.
- **HDR to SDR** for HDR10 and HLG. It adds dynamic tone mapping tuned for projectors.

With **Switch preset by signal type** on, mpv-vproc-web picks the preset automatically when the source changes
between SDR, HDR10 and HLG. Each signal type can have its own preset.

Any change you make applies immediately and is saved to the preset you're editing. A dot next to a setting's
name means the preset sets it; **Reset** removes it from the preset so mpv's default applies again.

Use **New** to try something without losing what you have: it copies the current preset, so you can compare
the two by switching between them.

---

## Tone mapping

### Tone-mapping curve — `tone-mapping`

The shape of the "squeeze" from HDR brightness to your display's. All of them leave dark and mid tones roughly
alone and differ in how they treat the highlights. There's no single right answer: try a few on scenes you
know well.

| Curve | In plain words |
|---|---|
| `auto` | Lets libplacebo pick a suitable curve for you. |
| `spline` | **The recommended all-rounder** and this project's default. A smooth curve that keeps shadows and midtones accurate and rolls highlights off gently. |
| `bt.2390` | The broadcast industry's reference curve (ITU-R BT.2390). Accurate midtones with a soft highlight roll-off; a good "reference" look to compare against. |
| `bt.2446a` | Another broadcast standard (ITU-R BT.2446, method A), designed for converting HDR TV to SDR. Tends to look a little brighter. |
| `st2094-40` | Modelled on the curve used by HDR10+. Often a slightly punchier image than `spline`. |
| `st2094-10` | Modelled on the curve from Dolby's SMPTE ST 2094-10 standard. |
| `reinhard` | A classic, simple curve. Compresses everything a little, so the picture looks flatter and lower in contrast. |
| `mobius` | Keeps everything exact up to a point, then compresses only the brightest parts. Bright, but highlights can look a bit abrupt. |
| `hable` | The "filmic" curve from video games. Contrasty, with darker midtones. |
| `gamma`, `linear` | Simple mathematical curves, mainly for experiments. `linear` makes everything uniformly dimmer. |
| `clip` | No squeezing at all: anything brighter than your display is cut off. Accurate below that, but bright skies and lamps turn into flat white blobs. |

**Curve parameter** (`tone-mapping-param`) fine-tunes the chosen curve; its meaning differs per curve. Leave it at
0 (the curve's own default) unless you're experimenting.

### Dynamic peak detection — `hdr-compute-peak`

When on (`yes`), every frame is measured and the curve adapts scene by scene. This is the "dynamic tone mapping"
from the primer, and the biggest single improvement for HDR on a projector. `auto` lets mpv decide; `no` relies
only on the brightness information in the video's metadata, which is often missing or wrong.

The next four settings fine-tune how the measurement behaves. The preset's values are good; change them only
if you notice a specific problem.

- **Peak percentile** (`hdr-peak-percentile`): which brightness counts as a scene's peak. At 100, the single
  brightest pixel decides, so one tiny bright spot (a lamp, a reflection) makes the whole scene darker. A value
  just below, like **99.995**, ignores those few specks. Lower values brighten scenes but can blow out small
  highlights.
- **Peak decay rate** (`hdr-peak-decay-rate`): how quickly the measured brightness follows changes. Higher values
  are steadier but slower to react, a bit like your eyes adjusting to a dark room. If you notice the picture
  "pumping" brighter and darker, raise it.
- **Scene threshold low / high** (`hdr-scene-threshold-low` / `-high`): how big a jump in brightness counts as a
  scene cut. Between the two values, adaptation speeds up; above the high one, it jumps instantly. This avoids
  slow fades at real scene changes.

### Contrast recovery — `hdr-contrast-recovery`

Squeezing highlights makes their fine detail (clouds, textures in bright areas) look soft. Contrast recovery puts
some of that detail back. 0 is off; around 0.3 is a good amount. Higher values can look over-sharpened.

### Max boost — `tone-mapping-max-boost`

Allows dark scenes to be brightened beyond their original level. 1 means never brighten (accurate). Values like
1.5–2 can help on a dim projector, at the cost of accuracy.

### Display contrast — `target-contrast`

Your display's native contrast ratio (for example 2000 for 2,000:1). It tells the tone mapping how deep your
display's black is, so shadow detail isn't crushed. `auto` assumes a typical value; set it if you've measured
your display, especially for projectors with very good (high number) or modest (low number) contrast.

### Gamut mapping — `gamut-mapping-mode`

HDR video can contain colours your display can't show. This decides what happens to them:

| Mode | In plain words |
|---|---|
| `auto` / `perceptual` | **Recommended.** Gently compresses out-of-range colours so they stay natural and keep their hue. |
| `relative` | Keeps in-range colours exact and moves out-of-range colours to the nearest one the display can show. |
| `saturation` | Keeps colours vivid at the expense of accuracy. |
| `absolute` | Like `relative`, but also keeps the source's exact white point. For calibration work. |
| `desaturate` | Fades out-of-range colours toward white. |
| `darken` | Darkens the image a little so bright saturated colours fit, instead of clipping them. |
| `clip` | Simply cuts colours off at the edge of the display's range. Can make saturated areas look flat or blotchy. |
| `linear` | Scales all colours down evenly. |
| `warn` | Marks out-of-range pixels in a bright colour, so you can see where gamut mapping is happening. A diagnostic tool, not for watching. |

### Inverse tone mapping — `inverse-tone-mapping`

Expands SDR video to HDR brightness. Only useful when outputting to an HDR display; leave it off when your
display is in SDR mode (the normal case with mpv-vproc-web).

### Visualize curve — `tone-mapping-visualize`

Draws the active tone curve and the live brightness measurement over the picture. Great for understanding what the
settings do; turn it off for watching.

---

## Display

The three most important ones (**Display primaries**, **Display transfer** and **Display peak**) are explained in
[Start here](#start-here). The others:

### Dither depth — `dither-depth`

mpv calculates the picture with much more precision than the video signal to your display can carry. Dithering
adds a fine, invisible noise pattern so the reduction to 8 or 10 bits doesn't cause banding. `auto` is right
for nearly everyone; `no` turns it off (not recommended).

### Dither algorithm — `dither`

How that noise pattern is made. `fruit` (a blue-noise pattern) is the default and looks very clean.
`error-diffusion` is the highest quality but costs noticeably more graphics-card time. `ordered` is the
cheapest.

---

## Scaling

Scaling converts the video's resolution to your display's. Each option chooses a **filter**, the method
used to calculate the new pixels.

- **Luma upscaler** (`scale`): the brightness detail, which our eyes are most sensitive to. Used when the video is
  smaller than the display, for example 1080p on a 4K projector. **The most important scaler.**
- **Chroma upscaler** (`cscale`): the colour detail. Almost all video stores colour at lower resolution than
  brightness (4:2:2 or 4:2:0), so this is used even when the video and the display have the same resolution.
- **Downscaler** (`dscale`): used when the video is bigger than the display, for example 4K on a 1080p display.

### Filters, from fastest to highest quality

| Filter | Character |
|---|---|
| `bilinear` | Fastest, soft. Only for very weak graphics cards. |
| `hermite`, `mitchell` | Soft, with almost no artefacts. Excellent **downscalers**. |
| `bicubic`, `catmull_rom` | Classic filters; `catmull_rom` is the sharper of the two. |
| `spline16`, `spline36`, `spline64` | Sharp with few artefacts. `spline36` is a great balance of quality and speed. |
| `lanczos` | Sharp; can show faint halos (ringing) around high-contrast edges. |
| `ewa_lanczos`, `ewa_hanning` | Work in two dimensions at once, giving smoother diagonals. Heavier. |
| `ewa_lanczossharp` | A sharper `ewa_lanczos`. The preset's choice for upscaling: excellent quality on a modern graphics card. |
| `ewa_lanczos4sharpest` | The sharpest and heaviest. Try it if your graphics card has time to spare. |

**If frames are dropped**, the scaler is the first thing to lighten: try `spline36` for the luma and chroma
upscalers.

### Chroma shader

**Chroma shader** (`glsl-shaders`) goes a step beyond the chroma upscaler. *Chroma from Luma* (CfL) uses the
full-resolution brightness detail to predict the colour detail a 4:2:2 source leaves out, so colour edges come
out as sharp as brightness edges. It shows most on red and blue edges, coloured text and credits.

| Choice | What it does |
|---|---|
| `off` | The chroma upscaler does the job (the default). |
| `cfl` | Full CfL. The best colour edges, and the most GPU time. |
| `cfl-lite` | A lighter CfL that's nearly as good. |

When CfL is on, it replaces the chroma upscaler. It runs on every frame, so after turning it on, play something
at your highest frame rate for a few minutes and watch **Dropped frames**. If they start to rise, use
`cfl-lite` or turn it off. It has no effect on RGB sources (PCs, consoles), which have no reduced colour detail.

The shaders are open source (MIT) and ship with vproc; `THIRD_PARTY_NOTICES.md` credits their author.

### The other scaling options

- **Anti-ringing** (`scale-antiring`): reduces the halos that sharp filters can leave around edges. 0 is off,
  1 is maximum. Around 0.5–0.7 is a common choice.
- **Sigmoid upscaling** (`sigmoid-upscaling`): upscales in a way that further reduces halos around bright-on-dark
  edges. Recommended on.
- **Correct downscaling** (`correct-downscaling`): makes downscaling filters work at their proper size, for
  noticeably better quality when shrinking. Recommended on.
- **Linear-light downscaling** (`linear-downscaling`): downscales in linear light, which is physically more
  accurate and keeps fine bright details (stars, text) from turning grey.

---

## Debanding

**Banding** shows as visible steps or contour lines in smooth gradients, such as skies, dark backgrounds or fog.
It's usually caused by compression in the source (streaming especially), not by the processor. **Deband**
smooths those steps out.

- **Deband** (`deband`): on or off. Worth trying with streaming sources; usually not needed with good Blu-ray
  sources.
- **Iterations** (`deband-iterations`): how many times it's applied. More is stronger and heavier.
- **Threshold** (`deband-threshold`): how big a step it treats as banding. Higher values remove more banding but
  start smoothing away real fine detail.
- **Range** (`deband-range`): how far around each pixel it looks. Larger catches wider bands.
- **Grain** (`deband-grain`): adds a little film-like noise afterwards, which hides any banding that's left.

mpv's defaults for the four numbers are a sensible start; just switch **Deband** on and see.

---

## Picture

**Brightness, Contrast, Saturation, Gamma, Hue**: the classic adjustments, from −100 to +100, where 0 means
unchanged. They're best left at 0: it's better to calibrate the display itself and set the
[Start here](#start-here) values to match. They're handy for a quick correction of a single source.

---

## Geometry

For special screen setups; most people never touch these.

- **Aspect override** (`video-aspect-override`): forces a picture shape. Useful for sources that are flagged
  wrongly, or for screens with an anamorphic lens that needs a stretched image. `no` uses the shape the source
  says it has.
- **Zoom** (`video-zoom`): enlarges or shrinks the picture. It's on a "power of two" scale: 0 is normal size,
  1 doubles it, −1 halves it, and 0.1 is about 7% bigger.
- **Vertical shift** (`video-pan-y`): moves the picture up or down, for example to centre a zoomed widescreen film
  on a cinemascope screen.
- **Pan & scan** (`panscan`): crops a widescreen picture's sides to fill a narrower screen. 0 is off (no
  cropping); 1 fills the screen completely.

---

## Checking your changes

- **Toggle stats overlay** (Tools) shows mpv's statistics on screen: frame timing, how long each processing
  step takes on the graphics card, and the colour information of the input and output. If the render times
  get close to the time per frame (about 41 ms at 24 fps, 16 ms at 60 fps), the graphics card is near its limit.
- The web page's **Output** panel shows **Dropped** frames. A rising number means the settings are too heavy:
  lighten the scalers, contrast recovery or dithering.
- **Visualize curve** (Tone mapping) shows what the tone mapping is doing, live.
- To compare two setups, make a copy with **New**, change one setting, and switch between the two presets while
  watching the same scene.

Settings are a matter of taste as much as accuracy. Start from the presets, set the three
[Start here](#start-here) values for your display, and change one thing at a time.
