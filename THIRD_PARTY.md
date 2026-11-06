# Third-party software, models and binaries

AtriHord itself is GPL-3.0-or-later (see `LICENSE`). It uses the following. Nothing in this table is bundled
unless it says so; models and the Lossless Scaling DLL are never part of this repository.

## IMPORTANT: restrictions you must know about

| Item | Restriction | What it means for you |
|---|---|---|
| **InsightFace models** (`buffalo_l`, ArcFace / RetinaFace), used by the optional face pass | **Non-commercial research use only** (InsightFace model licence). | Do not run the face pass in a commercial product or service. It is optional and off unless you install `insightface` and enable it. Other face models can replace it. |
| **Lossless Scaling DLL** (`lsfg-vk.dll`), required for **smooth motion** (video frame generation) | Proprietary software of the paid Steam app *Lossless Scaling*. **Not included and not redistributable.** | The smooth-motion feature does not work without it. See "Getting the Lossless Scaling DLL" below. Everything else works without it. |
| **Pixiv, Twitter, YouTube etc. content** | Downloaded content belongs to its creators; the sites' terms may restrict automated access. | You are responsible for what you download and how you use it. The software does not grant rights to any content. |

### Getting the Lossless Scaling DLL (for smooth motion)

1. Buy and install *Lossless Scaling* on Steam (Windows). You need your own licence.
2. In Steam: right-click Lossless Scaling → Properties → Betas → select the branch `lsfg-vk`. Steam downloads
   the beta; the default branch does not contain the file.
3. Find `lsfg-vk.dll` in the game folder (usually `Steam\steamapps\common\Lossless Scaling\`; the beta
   branch from step 2 is what puts it there).
4. Copy it to the GPU machine and pass it to the installer:
   `bash vision/vidinterp/install.sh --dll /path/to/lsfg-vk.dll`.
   Without `--dll` the installer builds everything else and the feature stays unavailable.

## Python packages (server)

| Package | Licence |
|---|---|
| Flask, Werkzeug, Jinja2 | BSD-3-Clause |
| psutil | BSD-3-Clause |
| mutagen | GPL-2.0-or-later |
| Pillow | HPND |
| numpy | BSD-3-Clause |
| requests | Apache-2.0 |
| yt-dlp | Unlicense |
| gallery-dl | GPL-2.0 |
| textual (terminal UI only) | MIT |
| curl_cffi (optional, pixiv follow sync) | MIT |

`mutagen` and `gallery-dl` are GPL, which is why the server is distributed as GPL-3.0-or-later.

## Python packages and models (vision worker)

| Item | Licence |
|---|---|
| PyTorch | BSD-3-Clause |
| onnxruntime | MIT |
| transformers, huggingface_hub, safetensors | Apache-2.0 |
| spandrel | MIT |
| waitress | ZPL-2.1 |
| WD EVA02-large tagger v3 (SmilingWolf) | Apache-2.0 |
| Florence-2-large (Microsoft) | MIT |
| Real-ESRGAN weights (via spandrel) | BSD-3-Clause |
| insightface + `buffalo_l` models | code MIT, **models non-commercial** (see above) |

## Other tools

| Tool | Licence | Use |
|---|---|---|
| PixivUtil2 (clone separately into `PixivUtil2/`) | BSD-2-Clause | pixiv downloads and the follow sync |
| ffmpeg | LGPL/GPL (your distribution's build) | thumbnails, muxing, conversion |
| deno | MIT | yt-dlp's JavaScript challenge solver |
| lsfg-vk (built by `install.sh`, patched) | MIT | smooth-motion backend; needs the Lossless Scaling DLL |
| three.js | MIT | VR / 3D viewer (vendored) |
| Leaflet | BSD-2-Clause | map (vendored) |
