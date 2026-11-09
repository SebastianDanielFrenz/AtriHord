"""Checks for grain.py on synthetic pictures, no GPU and no library needed:
what counts as grain and what does not, the strength it measures, and that
restore() puts back what a smoothing upscale took - no more, deterministically,
leaving grainless and already-grainy pictures alone.

  .venv/bin/python grain_check.py [dir holding the grain.py to test]
"""
import io
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))
import grain  # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


def jpeg(rgb, q):
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=q)
    return np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))


rng = np.random.default_rng(3)
H = W = 640
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
base = 110 + 60 * (xx / W)                         # a soft gradient, like a painted background


def grey(a):
    return np.repeat(np.clip(a, 0, 255)[..., None], 3, 2).astype(np.uint8)


def drawing():
    """A flat-coloured picture with dark lines, like a cel-shaded drawing."""
    img = np.zeros((H, W, 3), np.float32)
    img[..., 0], img[..., 1], img[..., 2] = base, base * 0.8 + 20, 150
    for k in range(8):
        img[:, 60 + 70 * k:63 + 70 * k] = 20
    return img


mono6 = grey(base + rng.normal(0, 6, (H, W)))
art = drawing()
art_grain = np.clip(art + rng.normal(0, 5, (H, W, 1)), 0, 255).astype(np.uint8)
colour = np.clip(art + rng.normal(0, 5, (H, W, 3)), 0, 255).astype(np.uint8)
u, v = (xx + yy) / np.sqrt(2), (xx - yy) / np.sqrt(2)

# ---- what is grain --------------------------------------------------------
p = grain.analyse(mono6)
check("grain on a gradient is found", p and p["grain"], p and p["why_not"])
check("... at its strength (6 +- 15 %)", p and abs(p["sigma"] - 6) < 0.9, p and p["sigma"])
check("... as monochrome", p and p["mono"] > 0.95, p and p["mono"])
p = grain.analyse(jpeg(mono6, 80))
check("grain through JPEG q80 is still grain (JPEG's blocks are not a verdict)", p and p["grain"],
      p and p["why_not"])
p = grain.analyse(np.asarray(Image.fromarray(mono6).resize((533, 533), Image.BICUBIC)))
check("resized grain is still grain", p and p["grain"], p and p["why_not"])
p = grain.analyse(art_grain)
check("grain over a drawing with lines: found, lines do not inflate it (5 +- 20 %)",
      p and p["grain"] and abs(p["sigma"] - 5) < 1.0, p and (p["sigma"], p["why_not"]))
p = grain.analyse(colour)
check("coloured grain is found and called coloured", p and p["grain"] and p["mono"] < 0.3,
      p and (p["mono"], p["why_not"]))
faint = grey(base + rng.normal(0, 1.5, (H, W)))
check("faint grain (1.5): not enough on the pixels alone",
      not grain.analyse(faint)["grain"])
check("... but enough when the tagger says film_grain", grain.analyse(faint, hinted=True)["grain"],
      grain.analyse(faint, hinted=True)["why_not"])


def overlay_grain():
    """An overlay grain over a picture that is mostly shaded dark background:
    weak in the shadows (1.2), strong in the midtones (4.5) - Feixiao's shape,
    whose median over all flat patches (1.65) sat under the bar."""
    r = np.random.default_rng(5)
    dark = xx < 0.65 * W
    img = np.where(dark, 22 + 30 * (0.5 + 0.5 * np.sin(2 * np.pi * yy / 160)), 125 + 25 * (yy / H))
    return grey(np.where(dark, img + 1.2 * r.normal(0, 1, (H, W)), img + 4.5 * r.normal(0, 1, (H, W))))


over = overlay_grain()
p = grain.analyse(over)
check("overlay grain, weak in a large dark background: found by its midtones", p and p["grain"],
      p and (p.get("strength"), p["sigma"], p["why_not"]))
check("... the midtone strength decides (4.5 +- 15 %), the median stays the restore scale",
      p and abs(p.get("strength", p["sigma"]) - 4.5) < 0.7 and p["sigma"] < 2.0, p and (p.get("strength"), p["sigma"]))
speck = base.copy()
speck[200:264, 200:264] += rng.normal(0, 6, (64, 64))     # 4 patches of texture on a clean picture
p = grain.analyse(grey(speck))
check("a few textured patches do not make a clean picture grainy (band needs enough patches)",
      p and not p["grain"], p and (p.get("strength"), p["why_not"]))

# ---- what is not ----------------------------------------------------------
check("a clean gradient is not grain", not grain.analyse(grey(base))["grain"])
check("a clean gradient through JPEG q85 is not grain", not grain.analyse(jpeg(grey(base), 85))["grain"])
check("a clean drawing is not grain", not grain.analyse(art.astype(np.uint8))["grain"])
why = grain.analyse(jpeg(grey(base), 85))["why_not"]
check("... and a clean picture is called clean, not a pattern (what the details panel shows)",
      why.startswith("flat areas are clean"), why)
check("a smooth screentone is a pattern, not grain",
      not grain.analyse(grey(base + 25 * (np.cos(2 * np.pi * u / 4) + np.cos(2 * np.pi * v / 4))))["grain"])
check("... through JPEG too",
      not grain.analyse(jpeg(grey(base + 20 * (np.cos(2 * np.pi * u / 3) + np.cos(2 * np.pi * v / 3))), 85))["grain"])
check("a dot halftone is not grain",
      not grain.analyse(grey(base - 70 * ((np.cos(2 * np.pi * u / 6) + np.cos(2 * np.pi * v / 6)) > 0.4)))["grain"])
check("a picture too small to measure: no profile", grain.analyse(mono6[:100, :100]) is None)

# ---- a large picture is measured on crops ---------------------------------
big = grey(np.tile(base, (5, 5))[:2400, :2400] + rng.normal(0, 4, (2400, 2400)))
check("a large picture is measured on nine crops", len(grain._crops(2400, 2400)) == 9)
p = grain.analyse(big)
check("... and still finds its grain (4 +- 15 %)", p and p["grain"] and abs(p["sigma"] - 4) < 0.6,
      p and (p["sigma"], p["why_not"]))


# ---- putting it back --------------------------------------------------------
def smooth_upscale(rgb, f):
    """What the anime model does to grain: a sharp-edged, grainless upscale."""
    img = Image.fromarray(rgb).filter(ImageFilter.GaussianBlur(2.5))
    return np.asarray(img.resize((rgb.shape[1] * f, rgb.shape[0] * f), Image.LANCZOS))


def at_source(out, size):
    q = grain.analyse(np.asarray(Image.fromarray(out).resize(size, Image.LANCZOS)))
    return q["sigma"] if q else 0.0


prof = grain.analyse(art_grain)
up = smooth_upscale(art_grain, 3)
check("the smoothing upscale really took the grain", at_source(up, (W, H)) < 1.0, at_source(up, (W, H)))
out, info = grain.restore(up, prof, source_rgb=art_grain)
check("restore puts grain back", info["added"] and out.shape == up.shape and out.dtype == np.uint8, info)
got = at_source(out, (W, H))
check("... to the original's strength at the same display size (+- 12 %)", abs(got - prof["sigma"]) < 0.12 * prof["sigma"],
      (got, prof["sigma"]))
check("... without moving the picture (mean luma within 1 level)",
      abs(float(out.mean()) - float(up.mean())) < 1.0, (float(out.mean()), float(up.mean())))
again, _ = grain.restore(up, prof, source_rgb=art_grain)
check("the same picture gets the same grain", np.array_equal(out, again))
cprof = grain.analyse(colour)
cout, cinfo = grain.restore(smooth_upscale(colour, 2), cprof, source_rgb=colour)
res = cout.astype(np.float32) - np.asarray(Image.fromarray(cout).filter(ImageFilter.GaussianBlur(3))).astype(np.float32)
flat_area = res[100:500, 5:55]                     # left of the first line
cc = np.corrcoef(flat_area.reshape(-1, 3).T)
check("coloured grain comes back coloured", cinfo["added"] and (cc[0, 1] + cc[0, 2] + cc[1, 2]) / 3 < 0.5,
      np.round(cc, 2).tolist())
kept = np.asarray(Image.fromarray(art_grain).resize((W * 2, H * 2), Image.NEAREST))
_, kinfo = grain.restore(kept, prof, source_rgb=art_grain)
check("an upscale that kept the grain gets none added", not kinfo["added"] and "kept" in kinfo.get("why", ""), kinfo)
clean = art.astype(np.uint8)
cup = smooth_upscale(clean, 2)
same, ninfo = grain.restore(cup, grain.analyse(clean), source_rgb=clean)
check("a grainless picture is left exactly as it was", not ninfo["added"] and np.array_equal(same, cup), ninfo)
nothing, zinfo = grain.restore(cup, None)
check("no profile at all: left as it was", not zinfo["added"] and np.array_equal(nothing, cup))
oprof = grain.analyse(over)
oout, oinfo = grain.restore(smooth_upscale(over, 3), oprof, source_rgb=over)
back = np.asarray(Image.fromarray(oout).resize((W, H), Image.BOX)).astype(np.float32)[..., 1]
bres = back - grain._smooth(back)
mid_got, dark_got = grain._mad_sigma(bres[64:576, 448:620]), grain._mad_sigma(bres[64:576, 32:380])
check("overlay grain comes back strong in the midtones (4.5 +- 20 %) and weak in the dark (< 2)",
      oinfo["added"] and abs(mid_got - 4.5) < 0.9 and dark_got < 2.0, (round(mid_got, 2), round(dark_got, 2), oinfo))
odd = smooth_upscale(art_grain[:333, :517], 3)
oout, oinfo = grain.restore(odd, grain.analyse(art_grain[:333, :517]), source_rgb=art_grain[:333, :517])
check("an odd size, several strips: whole, finite, grain added",
      oinfo["added"] and oout.shape == odd.shape and np.isfinite(oout.astype(np.float32)).all(), oinfo)



# ---- finer than the original's pixels (FINER) ------------------------------
def half_energy(noise):
    """Cycles per pixel below which half of a noise image's energy lies."""
    win = np.outer(np.hanning(32), np.hanning(32))
    psd = np.zeros((32, 32))
    n = 0
    for y0 in range(0, noise.shape[0] - 31, 32):
        for x0 in range(0, noise.shape[1] - 31, 32):
            p = noise[y0:y0 + 32, x0:x0 + 32]
            psd += np.abs(np.fft.fft2((p - p.mean()) * win)) ** 2
            n += 1
    psd[0, 0] = 0
    rad = np.sqrt(np.fft.fftfreq(32)[None, :] ** 2 + np.fft.fftfreq(32)[:, None] ** 2).ravel()
    order = np.argsort(rad)
    cum = np.cumsum(psd.ravel()[order])
    return float(rad[order][np.searchsorted(cum, cum[-1] / 2)])


flat_up = smooth_upscale(mono6, 3)
fout, finfo = grain.restore(flat_up, grain.analyse(mono6), source_rgb=mono6)
added = fout.astype(np.float32)[..., 1] - flat_up.astype(np.float32)[..., 1]
grain_size = half_energy(mono6.astype(np.float32)[..., 1] - grain._smooth(mono6.astype(np.float32)[..., 1]))
got_size, coarse_size = half_energy(added), grain_size / 3
check("at 3x the grain is drawn 2x finer than the original's pixels, and says so",
      finfo.get("fine") == 2.0 and 1.6 * coarse_size < got_size < 2.4 * coarse_size,
      (finfo.get("fine"), round(got_size, 3), round(coarse_size, 3)))
held = np.asarray(Image.fromarray(mono6).filter(ImageFilter.GaussianBlur(2.5))
                  .resize((mono6.shape[1] * 3 // 2, mono6.shape[0] * 3 // 2), Image.LANCZOS))
hout, hinfo = grain.restore(held, grain.analyse(mono6), source_rgb=mono6)
check("an upscale held at 1.5x gets grain at its own pixels, never finer",
      hinfo.get("added") and hinfo.get("fine") == 1.5, hinfo)
field = grain._GrainField(grain.analyse(mono6), 700, 500, 12345)
whole = field.rows(-5, 300)
parts = np.concatenate([field.rows(-5, 61), field.rows(61, 137), field.rows(137, 300)])
check("the grain drawn in bands is the grain drawn whole (strips join without a seam)",
      whole.shape == (305, 700, 3) and np.allclose(whole, parts, atol=1e-4),
      float(np.abs(whole - parts).max()))
check("... with unit luma variance", abs(float(grain._luma(whole).std()) - 1.0) < 0.1,
      float(grain._luma(whole).std()))

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
