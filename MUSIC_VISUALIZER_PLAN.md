# Music page: Apple-Music-style line visualizer (plan)

## Revision 2 (2026-11-12): layered scene driven by a pseudo-MIDI of the track

Owner after P0: lines alone are not enough. Different shapes should track different parts of the music, ideally from
a rough, on-the-fly pseudo-MIDI (which instrument plays what, when). The view should keep **zooming into** the
patterns: ring shapes, jagged rings and more. The two reference videos (reddit) could not be opened here: the
browser pane refuses `packaged-media.redd.it`. Screenshots or a local copy are needed before the look is tuned.

### Reference videos (looked at 2026-11-12, local copies in Downloads)
Both are **MilkDrop-style** visuals. Video 1 is the DI.FM player's visualizer, with preset numbers and a "1min"
preset timer in its bar; video 2 shows the same family.
- **Ground:** a full-screen feedback image, not lines on black. Each frame is the previous frame zoomed in, rotated
  and warped, with new shapes drawn on top. That is the endless "zooming into the pattern".
- **Symmetry:** radial and kaleidoscopic (4-, 8- and 12-fold flowers, crosses, mirrored tunnels).
- **Rings** expand from the centre on the beat, alongside spirals, starbursts, jagged petals, ribbon loops
  (Lissajous-like), plasma clouds and painterly smears.
- **Colour:** saturated, often hue-cycling or inverted, with whole-screen colour fields (blue/purple, orange/red,
  cyan).
- **Presets:** each "look" is a preset; the player switches presets every so often with a blend.

**Consequence:** this is exactly what the open-source **Butterchurn** engine renders. It is a WebGL port of MilkDrop
(MIT licence, used in Webamp) that runs thousands of community MilkDrop presets in the browser and takes a Web Audio
node as input. Two ways from here:
1. **Butterchurn as the engine (recommended first step).** It gets the reference look almost 1:1, quickly, and
   drops into the same side tap and delay.
   - Our pseudo-MIDI then becomes the conductor: preset changes on section changes and drops, blend lengths on the
     bar grid, a curated preset list per energy level.
   - We also write our own presets for the ring / jagged-ring / bass-lobe ideas.
   - Open: the licences of individual community presets (the `butterchurn-presets` package bundles them); fine for a
     private server, to be checked before the open-source release.
2. **Our own MilkDrop-like engine** (feedback warp + shape and wave layers + composite shaders, B below): full
   control and instrument-specific layers, but weeks of work to reach the variety of the preset library.

Plan: try 1 in the preview first. Build our own layers only where the presets cannot follow the pseudo-MIDI.

### Butterchurn trial (2026-11-12): `tests/ui/vis_butterchurn.html`
- **Result:** 367 presets, 60 fps at full resolution, any aspect ratio, a delay node for the owner's late speakers.
- **Owner's picks** (the trial page's starting favourites):
  - `_Mig_004_version2`, `_Mig_028`, `_Mig_079`
  - `_Rovastar + Geiss - Hurricane Nightmare (Posterize Mix)`, the only mix of that name in the packs
  - `$$$ Royal - Mashup (197)`, `(220)`, `(253)`
  - Round 2:
    - `$$$ Royal - Mashup (273)`
    - `_Krash + Eo.S. - Photographic Sentinel` (list position 11)
    - `Flexi + Martin - cascading decay swing` (list position 158)
    - `A Tribute to Martin - bombyx mori ...`
    - `Adam Eatit Mashup FX 2 martin - disco mix + ... + Aderrasi 2_1` (the first of its three versions)
    - `Cope - Passage (mandala mix)`
    - `cope - the drain to heaven`
- **What the picks share:**
  - Their look comes from custom **warp and composite shaders** (6 of 7) and long feedback (`decay` ~1, video echo
    with mirroring), not from shapes.
  - The drawn source is mostly **waveforms** (wave modes 2/3/5/7, spectrum waves in Royal 220). Only Royal 197 and
    253 use textured shapes (triangles, x4 in 253).
  - The motion is slow feedback zoom and rotation with bass-timed phase (`basstime += bass`).
  - Royal 220 inverts and darkens.
- **For our own presets:** start from these warp/comp shader patterns, and drive their `q` variables from the
  pseudo-MIDI (kick, snare, bass note, section) instead of plain bass/mid/treb.

### Design language "AtriHord Spectrum" (`web_files/js/vis/design.js`)
- **Fixed colours with roles:**

  | Role | Colours | Drawn by |
  |---|---|---|
  | ground | ink `#05060c`, night `#0d1030` | the background |
  | low | blue `#3987e5` (the site accent), violet `#8a4dff` | kick, bass |
  | mid | magenta `#ff3fa4` | snare, chords, lead |
  | high | cyan `#3fe0ff` | hats, sparks |
  | energy | amber `#ffb347` | drops only, rare |
  | peak | white `#f4f7ff` | flashes, capped at 3/s and 0.25 white |

- **Schemes** keep the roles and swap the hues: spectrum (default), aurora, ember, ice, orchid. The cover is one way
  to pick a scheme, not the source of the colours. A cover with too little colour (dark blue on black, grey) keeps
  spectrum; a colourful one picks the scheme nearest its hue.
- **Our presets draw into channels** (R = low, G = mid, B = high). The composite shader maps them through the
  design colours, so every animation shares the palette and a scheme change is a rebuild plus a blend.
- **Motion rules:** zoom always inward; changes on the beat grid; symmetry orders 1/2/4/6/8; flash cap over all
  layers.
- **First custom preset**, `vis/preset-rings.js` ("AtriHord - Ring Tunnel"):
  - kick rings carried outward by a zooming feedback tunnel
  - a jagged snare ring (triangle-wave radius)
  - hat sparks on the rim
  - a lobed bass body
  - a kaleidoscope order that changes every 16 kicks
  - It reads `window.__visIn` (kick / snare / hat / energy) when present: the hook for the pseudo-MIDI.

### A. Analysis: from audio to a pseudo-MIDI stream (`music-analysis.js`, an AudioWorklet)
The `AnalyserNode` only gives a spectrum per screen frame. Instead, an AudioWorklet on the same side tap gets the
samples themselves. It works at a 2048-sample FFT with a 512-sample hop (~10 ms at 48 kHz) and posts results to the
page. Every step is a known, cheap signal-processing technique; nothing needs a model.

1. **Harmonic / percussive split** (median filtering: across time keeps the tones, across frequency keeps the hits).
   Drums are read from the percussive half and pitches from the harmonic half, so a kick does not show up as a bass
   note.
2. **Drums** come from onsets per band in the percussive half; at most one per class every ~60 ms.

   | Class | What marks it |
   |---|---|
   | kick | energy below ~150 Hz, fast decay |
   | snare / clap | broadband 200 Hz-5 kHz, noisy (high spectral flatness) |
   | hat | above ~6 kHz, short |
   | cymbal / crash | above ~6 kHz, long tail |
   | tom | a pitched low-mid hit |

3. **Bass:** the f0 between 35 and 250 Hz by harmonic summation in the harmonic half, then turned into notes
   (pitch, velocity, start, length) with a stability rule (~3 frames).
4. **Lead / vocal:** the dominant pitch between 150 and 1500 Hz (harmonic summation, salience), as a note stream plus
   a continuous pitch contour. Vibrato is kept, because it is good motion.
5. **Harmony:** a 12-bin chroma vector smoothed over ~0.5 s, giving the chord root and quality (major/minor) by
   template match. A chord change is an event.
6. **Tempo and grid:** autocorrelation of the onset envelope gives the BPM, and a phase-locked beat tracker gives the
   beat and bar phase. The downbeat is the strongest kick pattern. With a grid the visuals can act ON the beat
   instead of reacting late.
7. **Structure:**

   | Event | What marks it |
   |---|---|
   | build | rising high-frequency noise energy (riser) + rising onset density |
   | drop | a bass and kick return after a build or a gap |
   | breakdown | kick gone for > 2 bars |
   | section change | novelty in a self-similarity of chroma and energy |

**Output:** one stream of events, `{t, voice, kind, pitch?, vel, dur?}`, with `voice` one of kick, snare, hat,
cymbal, tom, bass, lead, chord or section. Next to it comes a block of continuous values at frame rate (per-voice
energy, the lead's pitch contour, chroma[12], spectral centroid "brightness", tension = build progress 0..1, bpm,
beatPhase, barPhase). The visualizer's `delay` applies to both.

**Expectation:** good on electronic music and anything with clear drums and a bass, rough on dense mixes,
orchestral music and live drums. A wrong label only means a shape reacts to the wrong thing; nothing breaks.
**Quality step later (optional):** an offline analysis per song on the server's GPU host. Demucs separates drums,
bass, vocals and other, and Basic Pitch transcribes the notes. The result is cached as a real MIDI-like file per
song. It is accurate and, more importantly, it knows the FUTURE: a build can visually head for its drop. The live
worklet stays as the fallback for songs not analysed yet.

### B. Visuals: a zooming tunnel of layers (`music-visualizer.js` v2)
- **Camera:** the camera flies forward constantly. Shapes are born far away (small, dim) and grow toward and past
  the viewer, on a logarithmic depth scale so the zoom is seamless and endless.
  - The flight speed is locked to the tempo: one "depth unit" per beat.
  - It surges on a drop and almost stops in a breakdown.
  - A slow roll of the camera follows the chord root.
- **Feedback zoom:** each frame, the previous frame is drawn slightly enlarged and rotated under the new one, fading
  (as Milkdrop does). Everything leaves a trail that streams outward: the "always zooming in" feel at almost no
  cost. Strength and rotation follow the music.
- **Layers** (each one a voice of the pseudo-MIDI; instanced polylines in polar coordinates, `r(theta) =
  R * (1 + shape(theta))`, through the existing glow/bloom pipeline):

  | Voice | Shape |
  |---|---|
  | kick | a clean ring born at the centre on each kick, flying outward; width and brightness from the velocity |
  | snare / clap | a jagged ring (star polygon, noise spikes) that shatters as it passes |
  | hats | sparks or short ticks on the newest ring's rim, rotating with the beat phase |
  | cymbal / crash | a wide soft halo plus a burst of sparks |
  | bass | a thick, slowly deforming ring; the note decides the number of lobes and its rotation, and it pulses with the note length |
  | lead / vocal | the silk strands of P0, spiralling through the tunnel; the pitch contour bends them; a new note is a bright head running along them |
  | chords | background nebula and palette: the hue from the chord root, minor darker / major brighter; a change shifts the palette over a beat |
  | structure | build: rings speed up, edges get more jagged, saturation rises; drop: one strong burst (within the flash cap), kaleidoscope symmetry changes (n = 4/6/8), new seed; breakdown: lines only, slow, sparse |

- **Aspect:** everything is polar around the centre in short-side units, and rings grow past the corners, so any
  aspect ratio works with no special case. Portrait no longer needs the turned field.
- **Safety:** the flash cap (<= 3 per second, limited brightness step) now applies to the sum of all layers. A drop
  burst counts as one.
- **Fallback without a signal** (paused, iOS): a slow idle flight with the strands only.

### C. New phase order (replaces P1-P4 below; P0 is done)

| # | Content | Done when |
|---|---|---|
| P0a | Analysis worklet + a **live piano-roll / lane view** in the preview (voices as lanes, notes as bars, chroma, bpm and phases) | The owner judges the pseudo-MIDI on their own tracks; labels plausible on EDM |
| P0b | Tunnel v2: camera, feedback zoom, ring / jagged / bass / spark layers, the strands as the lead layer, driven by the event stream (a simulated stream first, then the live one) | The owner's sign-off on the look against the reference videos |
| P1 | Into the music page as the third stage mode (tap on `UP.input`, reconnect after `buildUpmixNow`, delay = output latency + bridge queue, cover palette as the base) | As before |
| P2 | Performance governor across layers, phone check, flash cap over all layers, reduced motion | Phone >= 30 fps |
| P3 (optional) | Offline per-song analysis on the GPU host (Demucs + Basic Pitch), cached; the page prefers it and the visuals anticipate builds and drops | Measurably better labels; drops hit on time |

The P0 renderer stays: its strands become the lead layer, and its bloom, delay and governor carry over.

---

# Original plan (revision 1)

Goal: a third stage mode next to **Song | Video** - **Visualizer** - that fills the stage (and the screen in
fullscreen) at any aspect ratio with procedurally generated, softly glowing lines that flow and swell with the
music, coloured from the song's cover. Look reference: the macOS Music app's visualizer - dark ground, bundles of
thin luminous strands like silk ribbons, additive glow, slow drift, swelling on the bass, shimmering on the highs.

## What the page already has (music.js)

- **Audio graph:** both media elements go into `UP.input` (stereo bus) through `LOUD.ctx`. That bus feeds the
  browser output and the bridge tap alike, so an analyser there sees the music on either route.
- **Gotcha:** `buildUpmixNow()` calls `UP.input.disconnect()`, which also cuts any analyser hung on it. The speaker
  meter's splitter is reconnected afterwards (`SPK.sp`, ~line 1256); the visualizer's tap must be reconnected the same
  way, or it goes silent after every output switch.
- **Stage:** `#muStage` / `#muStageFit`, mode pill `#muStageModes` (`renderModes`, `setVideoMode`, stored with
  `VIDEO_MODE_KEY`), `toggleFullscreen()` fullscreens the whole document, so a canvas sized by its box gets the
  screen's aspect ratio for free.
- **Cover colour:** `ambientFrom(img)` (16x16 average with lifted saturation) - the palette code can start from it.
- **No Web Audio on iOS** (`IOS`, `LOUD.off`): there the visualizer has no live signal.

## Architecture

### 1. Audio features (`VisFeatures`)
- One `AnalyserNode` (fftSize 2048, smoothing 0) tapped on `UP.input`, connected only while the visualizer runs.
- Per frame: `getFloatFrequencyData` -> band energies (bass 20-150 Hz, low-mid, mid, high), RMS, and **onsets** from
  spectral flux with an adaptive threshold (median of the last ~1 s x 1.5) -> `beat` pulse 0..1.
- Each value gets an envelope (fast attack ~30 ms, slow release ~300 ms), so lines move smoothly, never jitter.
  Long-term loudness (~3 s) sets the overall speed.
- **Latency compensation:** features go into a short ring buffer and are read `delay` ms late:
  `ctx.outputLatency` on the browser route, plus `BR.queuedMs` on the bridge route (its queue makes the sound
  arrive later than the analyser sees it).
- Paused / no signal: envelopes decay to an idle drift (lines keep breathing slowly, no freeze).

### 2. Line generation (procedural, in the vertex shader)
- **Shape:** 6-10 **bundles**, each 20-60 **strands**; each strand is a polyline of 128-256 points across the
  screen. Roughly 300-600 strands, 50-150k vertices: cheap for a GPU, nothing per-vertex on the CPU.
- **Curve of a bundle:** `y(x, t) = base + sum of 3-4 sine octaves (phase advancing with time)` plus domain-warped
  3D simplex noise in `(x, bundle, t)`.
- **Strands of a bundle:** the same curve, offset by a spread that widens with the mids, plus a small per-strand
  noise. This is the "silk" look: lines that braid, part and close.
- **Drawing:** instanced quads per segment (WebGL lines are 1 px); the width is in CSS px x DPR, and the fragment
  shader fades by distance from the centre line, which antialiases and gives the soft core.
- **Aspect ratio:** the shader works in a space where the height is 1 and x spans `[-aspect/2 - margin,
  aspect/2 + margin]`. The point count follows the width, and line widths are in pixels, so 21:9, 16:9, 4:3 and 9:16
  all look native, not stretched. In portrait (aspect < 1) the field is turned 90 deg so the lines run along the
  long side.
- **Music mapping:**
  - bass -> amplitude and vertical swell of the bundles
  - mids -> strand spread and wave frequency
  - highs -> fine shimmer (high-frequency noise, brightness flicker of single strands)
  - beat -> short brightness lift and a phase kick
  - loudness -> drift speed
- **Time:** time is integrated (`phase += speed * dt`), so a speed change never makes the picture jump.
- **Per-song seed:** a hash of the song id gives each track its own but repeatable layout. A song change crossfades
  between two seeds over ~1.5 s.

### 3. Rendering (WebGL2)
- Lines go with additive blending into an offscreen buffer at render scale (RGBA16F where
  `EXT_color_buffer_float` exists, else RGBA8).
- **Optional trails:** the previous frame x 0.85 is fed back in, which gives the silky motion blur.
- **Bloom:** a dual-Kawase down/up chain, 4-5 levels.
- **Composite:** tone mapping, a light vignette, and 1-bit dither or grain against banding in the dark gradients.
- **Background:** near-black with a faint cover-tinted gradient (from `ambientFrom`).
- **Palette:** 3-5 colours from the cover (k-means on a 32x32 downsample, saturation lifted like `ambientFrom`), one
  hue per bundle with small variation per strand. The palette crossfades on a song change. Without a cover, a fixed
  cool palette is used.
- **Performance governor:**
  - DPR capped at 2.
  - Frame time measured over ~1 s: above budget, the render scale (1.0 -> 0.75 -> 0.5) and then the strand count
    drop; with headroom they rise again.
  - Bloom runs at half resolution anyway.
- **Without WebGL2:** the mode is not offered. A Canvas 2D path without bloom would not look like the reference.

### 4. Integration
- **New file:** `web_files/js/music-visualizer.js`. It exposes
  `createVisualizer(canvas, { features })` with `start()`, `stop()`, `resize()`, `setPalette()`, `setSeed()`.
  `music.js` only wires it up.
- **Stage:**
  - A `<canvas id="muStageVis">` covers the stage (`position:absolute; inset:0`) under the title and controls overlay.
  - A `ResizeObserver` sets the drawing buffer to box x DPR x scale.
  - A third pill, `muModeVis`, is offered for every song. The stored mode becomes `song | video | vis` and
    migrates the old boolean.
- **Lifecycle:**
  - The render loop and the analyser tap run only while the stage is open, the mode is Visualizer and the tab is
    visible (`visibilitychange`). Otherwise both are stopped and disconnected.
  - `buildUpmixNow()` reconnects the tap (see the gotcha above).
- **Safety:**
  - Beat flashes are capped at 3 per second with a limited brightness step (photosensitivity, WCAG 2.3.1).
  - `prefers-reduced-motion`: slower drift, no flashes.
- **The audio must not change:** the analyser is a side tap only. The page's diagnostics (`DG`) should show no new
  `tap-gap` or `main-late` while the visualizer runs on the bridge route.

## Phases

| # | Content | Done when |
|---|---|---|
| P0 | Standalone prototype `tests/ui/vis_preview.html` (+ the existing preview server): file input or test signal, sliders for the mapping, to tune the look against reference screenshots | It looks right at 16:9, 21:9, 4:3 and 9:16 in screenshots; 60 fps on the desktop GPU |
| P1 | Renderer + features in the music page as the third stage mode (desktop browsers) | Mode switch, fullscreen and resize work; the tap survives output/Heights switches; no console errors |
| P2 | Cover palette, per-song seed, crossfade on song change, latency compensation (browser and bridge route) | Lines hit the beat by ear on both routes |
| P3 | Performance governor, visibility pause, reduced motion, flash cap; check on a phone (Android Chrome) | Phone holds >= 30 fps; CPU idle when the mode is off |
| P4 | Polish: trails, settings in the player menu (intensity, density), optionally an iOS path | The owner's sign-off on the look |

**Optional iOS path (P4):** without Web Audio, reactive input could come from a per-song envelope precomputed on the
server, next to `music_loudness.py`: ~20 Hz band energies plus an onset list, read in step with `currentTime`.
Without that, iOS gets the idle drift only.

## Verification
- P0 and P1 run in the desktop preview, with screenshots at four aspect ratios.
- Frame time is read through `performance` marks in the page.
- The audio checks are the `DG` events and a by-ear check of the beat sync on both routes.
- Deploy is the usual way (md5 compare, `.bak-<tag>`, temp file + `mv`), and only after the preview check.

## Open decisions (owner)
1. **Which Apple look:** the macOS Music visualizer (flowing luminous lines, assumed here) or the iOS fluid colour
   gradient behind the lyrics?
2. **Colours:** from the cover (assumed) or a fixed Apple-like palette?
3. **Mode placement:** a third mode for every song (assumed), or automatically in place of the cover when there is
   no video?
4. **iOS:** is the server-side envelope worth it, or is the idle drift enough there?
