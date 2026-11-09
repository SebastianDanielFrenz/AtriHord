"""Film grain: find it in the original, put it back after the upscale.

Artists often lay a film-grain texture over a finished drawing (user,
2026-09-22: "Many anime artworks ... have film grain intentionally added over
the drawn works. This gets obliterated during upscaling"). The anime upscaler
was trained to treat noise as damage: on the user's own example (a song cover)
it took the grain from 5.08 to 0.22 - 96 % gone - together with the JPEG
artefacts. This module measures the grain on the original and lays a
statistically matching grain over the upscaled picture: the same strength,
grain size, colour and brightness response, NOT the same grain particles
(nobody can see which particle sat where; everyone can see a waxy picture).

analyse(rgb, hinted)    -> a profile (or None when there is too little to measure)
restore(out, profile)   -> (the upscaled picture with the grain it lost, what was done)

HOW THE GRAIN IS MEASURED. Grain is what is left where the drawing itself is
flat: the picture minus a smoothed copy of itself, in the flattest 32 px
patches (away from lines) and away from black and white (where grain clips).
Per patch the strength is a MEDIAN-based sigma, which a line or a sparkle
crossing the patch barely moves, and a patch counts as grain-like only when its
plain standard deviation agrees with that (a heavy tail is structure, not
noise). Then three tests, each learnt from a failure in the lab (vault KSF-045):
  * strength - a clean flat fill has almost no residual. Lower when the tagger
    already says film_grain; tagged pictures whose grain JPEG had erased
    measured under 1.0 and have nothing to lose anyway. Judged on the
    strongest brightness band that holds enough flat patches, not on the
    median over all of them: an overlay grain is weak in the shadows, and a
    picture whose flat areas are mostly a dark background had its median
    pulled below the bar by them (Feixiao: 1.65 overall, 4.8 on the skin and
    hair, where everyone sees it).
  * coverage - grain is everywhere, so nearly every flat patch carries it; line
    art, a star field or a lace texture only fill a few.
  * broadband - the residual's spectrum, split into its smooth part and its
    spikes. Spikes are not grain: a screentone's dot grid, JPEG's 8x8
    harmonics, the stripes a resize leaves in noise (twitter rescales
    uploads). A picture whose residual is mostly spikes is a pattern; for the
    rest, only the broadband part is measured and drawn back.
NOT a test: blockiness. Twitter's JPEG folds real grain into its 8x8 blocks
(the user's example measured 1.99, like a clean picture's JPEG noise).

HOW IT IS PUT BACK. White noise is shaped by the grain's own spectrum and
colour covariance (mono or coloured grain, per channel), scaled up to the
output size and scaled per pixel by the measured brightness curve. It is drawn
at FINER times the original's resolution (never finer than an output pixel):
drawn at the original's own resolution, as at first, every speck of grain came
out a soft blob of 4 output pixels at 4x - mottling rather than grain when
zoomed in (user, 2026-09-22: "The grain should be higher frequency, maybe
double or tripple"; of 2x and 3x, each at two strengths, the user chose 2x at
the original-scale strength: "2x finer A looks good"). Its strength is set so
that, looked at at the ORIGINAL's size (area-averaged, as remaining()
measures), it matches the original's grain: finer grain averages away more
when seen small, so it is drawn stronger per output pixel. The other
strength tried - the same per output pixel as the coarse grain - all but
vanished at the original's size (0.72 of 1.65 at 3x).
Only what the upscaler REMOVED is added: the grain still in the output,
measured the same way at the original's scale, is subtracted in variance, so a
model that kept some grain does not get it doubled. Seeded from the picture:
the same picture always gets the same grain.

COST, bounded: a large original is measured on nine crops (artwork grain is
the same all over), and the output is written in strips of rows, so a 64 MP
result never exists as floats in full - nor does its grain: _GrainField draws
the rows each strip needs from blocks of seeded noise, and the strips join
without a seam.
"""
from __future__ import annotations

import time
import zlib

import numpy as np
from PIL import Image

PATCH = 32
FLAT_SHARE = 0.30          # the flattest 30 % of patches are where grain is measured
MIN_FLAT_PATCHES = 12
LUMA_LOW, LUMA_HIGH = 14.0, 240.0
LUMA_BANDS = 5
CROP = 704                 # side of a measuring crop (22 patches); nine of them for a large picture
WHOLE_UP_TO = 4_200_000    # pixels measured whole
STRIP = 256                # output rows written at a time

MIN_SIGMA = 2.0            # robust flat-area grain needed on the pixels alone (0-255 luma)
MIN_SIGMA_HINTED = 1.1     # ... when the tagger says film_grain too
MIN_COVERAGE = 0.60        # share of flat patches that are grain-like and carry typical grain
MAX_TAIL = 1.35            # std / median-sigma of a patch: ~1.0 for grain, more for lines and sparkles
MIN_BROADBAND = 0.35       # share of the residual's energy outside spikes; a screen is nearly all spike
MIN_ADD = 0.5              # less than this to put back is not worth drawing
BAND_MIN_PATCHES = 24      # a brightness band decides the strength only with this many grain-like patches
BAND_MIN_SHARE = 0.05      # ... and this share of them (a few textured patches are not the picture)
FINER = 2.0                # grain drawn at this many times the original's resolution (module notes)
NOISE_BLOCK = 64           # rows of white noise drawn from one seed; a strip rebuilds the blocks it needs


def _box(a, r):
    """Mean over a (2r+1)^2 window, edges reflected, via a summed-area table.
    float64: a summed-area table of a large picture reaches ~4e9, where float32
    has no digits left for the difference of two neighbours."""
    p = np.pad(a.astype(np.float64), r, mode="reflect")
    c = p.cumsum(0).cumsum(1)
    c = np.pad(c, ((1, 0), (1, 0)))
    k = 2 * r + 1
    h, w = a.shape
    return ((c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)).astype(np.float32)


def _smooth(a):
    """Two 5x5 box passes: about a Gaussian of sigma 2 - wide enough to take
    grain out, narrow enough to keep the drawing's shading."""
    return _box(_box(a, 2), 2)


def _luma(rgb):
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _crops(h, w):
    """(y0, x0, y1, x1) to measure: the whole picture, or nine crops on a 3x3
    grid, on multiples of 32 so patches keep JPEG's 8x8 grid in place."""
    if h * w <= WHOLE_UP_TO or h < CROP or w < CROP:
        return [(0, 0, h, w)]
    out = []
    for fy in (1 / 6, 1 / 2, 5 / 6):
        for fx in (1 / 6, 1 / 2, 5 / 6):
            y0 = int(min(max(h * fy - CROP / 2, 0), h - CROP)) // PATCH * PATCH
            x0 = int(min(max(w * fx - CROP / 2, 0), w - CROP)) // PATCH * PATCH
            out.append((y0, x0, y0 + CROP, x0 + CROP))
    return out


def _mad_sigma(a):
    """The standard deviation a Gaussian would have given this median absolute
    deviation - blind to the few large values a line or a sparkle adds."""
    return 1.4826 * float(np.median(np.abs(a - np.median(a))))


def _rings(rad, step):
    r = step
    while r < rad.max() + step:
        yield (rad >= r) & (rad < r + step)
        r += step


def _despike(psd, rad, limit=4.0):
    """The spectrum with every bin clamped to `limit` times the median of its
    ring: the broadband part, which is the grain. The spikes above it are a
    screentone's dots, JPEG's block harmonics or a resize's stripes - one
    grainy artwork carried half its residual in one spike at 0.44 cycles per
    pixel - and none of those should be drawn into the upscale."""
    out = psd.copy()
    for ring in _rings(rad, 1.0 / PATCH):
        if ring.any():
            out[ring] = np.minimum(psd[ring], limit * float(np.median(psd[ring])))
    out[0, 0] = 0.0
    return out


def _peakiness(psd, rad):
    """How far the strongest bin stands above the others in its own ring
    (information only; broadband decides). Against the ring, not the whole
    spectrum: JPEG empties the high frequencies, so "max over median" only
    measured how steeply the spectrum falls."""
    k = np.arange(PATCH)
    kk = np.minimum(k, PATCH - k)
    jpeg = (k % (PATCH // 8) == 0)
    ok = (kk[:, None] > 1) & (kk[None, :] > 1) & ~(jpeg[:, None] | jpeg[None, :])
    best = 0.0
    for lo in np.arange(0.10, 0.46, 0.05):
        ring = ok & (rad >= lo) & (rad < lo + 0.05)
        if ring.sum() >= 8:
            vals = psd[ring]
            best = max(best, float(vals.max() / max(np.median(vals), 1e-9)))
    return best


def _blockiness(patches):
    """Mean luma step across the 8x8 block grid over the step inside blocks,
    in the flat patches (information only - see the module notes)."""
    on, off = [], []
    for p in patches:
        dx = np.abs(np.diff(p, axis=1))
        dy = np.abs(np.diff(p, axis=0))
        cols = np.arange(dx.shape[1])
        on += [dx[:, cols % 8 == 7].mean(), dy[cols % 8 == 7, :].mean()]
        off += [dx[:, cols % 8 != 7].mean(), dy[cols % 8 != 7, :].mean()]
    return float(np.mean(on) / max(np.mean(off), 1e-6))


def analyse(rgb, hinted=False):
    """The grain profile of a uint8 HxWx3 picture, or None when it has too few
    flat patches to say. profile["grain"] says whether there is grain worth
    putting back; the rest describes it either way. `hinted`: the tagger
    already called it film_grain, so less pixel evidence is needed."""
    rgb = np.asarray(rgb)[..., :3]
    h, w = rgb.shape[:2]
    if h < PATCH * 4 or w < PATCH * 4:
        return None
    # Every candidate patch from every crop: (flatness, luma residual, RGB residual, mean luma).
    cands = []
    for cy0, cx0, cy1, cx1 in _crops(h, w):
        f = rgb[cy0:cy1, cx0:cx1].astype(np.float32)
        y = _luma(f)
        ys = _smooth(y)
        yres = y - ys
        res = np.stack([f[..., c] - _smooth(f[..., c]) for c in range(3)], axis=-1)
        grad = np.abs(np.diff(ys, axis=0, prepend=ys[:1])) + np.abs(np.diff(ys, axis=1, prepend=ys[:, :1]))
        for y0 in range(0, y.shape[0] - PATCH + 1, PATCH):
            for x0 in range(0, y.shape[1] - PATCH + 1, PATCH):
                m = float(ys[y0:y0 + PATCH, x0:x0 + PATCH].mean())
                if LUMA_LOW <= m <= LUMA_HIGH:
                    cands.append((float(grad[y0:y0 + PATCH, x0:x0 + PATCH].mean()),
                                  yres[y0:y0 + PATCH, x0:x0 + PATCH].copy(),
                                  res[y0:y0 + PATCH, x0:x0 + PATCH].copy(),
                                  y[y0:y0 + PATCH, x0:x0 + PATCH].copy(), m))
    cands.sort(key=lambda c: c[0])
    flat = cands[:max(MIN_FLAT_PATCHES, int(len(cands) * FLAT_SHARE))]
    if len(flat) < MIN_FLAT_PATCHES:
        return None

    mad = np.array([_mad_sigma(c[1]) for c in flat])
    std = np.array([float(c[1].std()) for c in flat])
    grainlike = std / np.maximum(mad, 0.05) <= MAX_TAIL
    sigma = float(np.median(mad[grainlike])) if grainlike.any() else 0.0
    coverage = float((grainlike & (mad >= 0.5 * sigma)).mean()) if sigma > 0 else 0.0
    # From here on only the grain-like patches describe the grain.
    if grainlike.any():
        flat, sig = [c for c, ok in zip(flat, grainlike) if ok], mad[grainlike]
    else:
        sig = mad
    block = _blockiness([c[3] for c in flat])
    cov = np.cov(np.concatenate([c[2].reshape(-1, 3) for c in flat]).astype(np.float64), rowvar=False)
    win = np.outer(np.hanning(PATCH), np.hanning(PATCH)).astype(np.float32)
    psd = np.zeros((PATCH, PATCH), np.float64)
    for c in flat:
        psd += np.abs(np.fft.fft2(c[1] * win)) ** 2
    psd /= len(flat)
    psd[0, 0] = 0.0

    # Strength per brightness band, relative to the overall median.
    edges = np.linspace(LUMA_LOW, LUMA_HIGH, LUMA_BANDS + 1)
    means = np.array([c[4] for c in flat])
    curve = []
    band_sigmas = []           # the bands populated enough to speak for the picture
    for i in range(LUMA_BANDS):
        sel = sig[(means >= edges[i]) & (means < edges[i + 1])]
        curve.append(float(np.median(sel)) / sigma if len(sel) >= 3 and sigma > 0 else None)
        if len(sel) >= max(BAND_MIN_PATCHES, BAND_MIN_SHARE * len(sig)):
            band_sigmas.append(float(np.median(sel)))
    known = [c for c in curve if c is not None]
    fill = float(np.median(known)) if known else 1.0
    curve = [c if c is not None else fill for c in curve]

    fy = np.fft.fftfreq(PATCH)[:, None]
    fx = np.fft.fftfreq(PATCH)[None, :]
    rad = np.sqrt(fx ** 2 + fy ** 2)
    clean_psd = _despike(psd, rad)
    total = float(psd.sum())
    broadband = float(clean_psd.sum()) / total if total > 0 else 1.0
    grain_sigma = sigma * broadband ** 0.5      # variances add: only the broadband part is grain
    # What decides: the strongest well-populated band (see the module notes).
    # grain_sigma stays what restore() scales by - the curve is relative to it.
    strength = max([sigma] + band_sigmas) * broadband ** 0.5
    # Grain size: the frequency below which half the grain's energy lies.
    order = np.argsort(rad.ravel())
    cum = np.cumsum(clean_psd.ravel()[order])
    half = float(rad.ravel()[order][np.searchsorted(cum, cum[-1] / 2)]) if cum[-1] > 0 else 0.0
    corr = cov / np.sqrt(np.outer(np.diag(cov), np.diag(cov)) + 1e-9)
    mono = float((corr[0, 1] + corr[0, 2] + corr[1, 2]) / 3)

    need = MIN_SIGMA_HINTED if hinted else MIN_SIGMA
    # Strength first: a clean picture's residual is next to nothing, and what
    # there is of it is JPEG's and rounding's spikes - "a regular pattern"
    # would be true of that remainder and misleading about the picture.
    reasons = []
    if strength < need:
        reasons.append("flat areas are clean (%.2f < %.2f)" % (strength, need))
    if broadband < MIN_BROADBAND:
        reasons.append("a regular pattern, not grain (%.0f %% of it in spikes)" % ((1 - broadband) * 100))
    if coverage < MIN_COVERAGE:
        reasons.append("only %.0f %% of flat areas carry grain" % (coverage * 100))
    return {
        "grain": not reasons,
        "why_not": "; ".join(reasons),
        "sigma": round(grain_sigma, 3),
        "sigma_raw": round(sigma, 3),
        "strength": round(strength, 3),
        "broadband": round(broadband, 3),
        "coverage": round(coverage, 3),
        "blockiness": round(block, 3),
        "peak": round(_peakiness(psd, rad), 2),
        "hinted": bool(hinted),
        "mono": round(mono, 3),
        "size": round(half, 4),            # cycles per pixel at half the energy; lower = coarser
        "cov": cov.tolist(),
        "psd": clean_psd.astype(np.float32),
        "curve": [round(c, 3) for c in curve],
        "flat_patches": len(flat),
        "source_size": (w, h),
    }


class _GrainField:
    """The grain as an image of fw x fh field pixels - white noise shaped by
    the grain's spectrum and mixed by its colour covariance, luma variance 1 -
    drawn a band of rows at a time, so a large output never holds all of it.

    The spectrum is applied as a PATCH x PATCH convolution kernel (it was
    measured on PATCH-pixel patches, so that is all the detail it has). White
    noise comes in blocks of NOISE_BLOCK rows, each from its own seed, so every
    band rebuilds exactly the noise its rows depend on: bands drawn separately
    are the same rows as one band drawn whole, and strips join without a seam.
    Rows past the top or bottom are simply more of the same grain."""

    def __init__(self, profile, fw, fh, seed):
        self.fw, self.fh, self.seed = fw, fh, seed
        amp = np.sqrt(np.maximum(np.asarray(profile["psd"], np.float64), 0.0))    # FFT order, DC at [0, 0]
        kernel = np.real(np.fft.fftshift(np.fft.ifft2(amp)))
        norm = float(np.sqrt((kernel ** 2).sum()))
        self.kernel = kernel / norm if norm > 0 else kernel      # white noise through it keeps variance 1
        cov = np.array(profile["cov"], np.float64)
        ycoef = np.array([0.299, 0.587, 0.114])
        try:
            chol = np.linalg.cholesky(cov / max(float(ycoef @ cov @ ycoef), 1e-9) + np.eye(3) * 1e-6)
        except np.linalg.LinAlgError:
            chol = np.eye(3)
        self.mix = chol.astype(np.float32)
        self._spectra = {}

    def _noise(self, channel, r0, r1):
        """Unit white noise of rows r0..r1 and PATCH - 1 columns more than the
        field, as the 'valid' convolution needs them."""
        b0, b1 = r0 // NOISE_BLOCK, (r1 - 1) // NOISE_BLOCK
        z = np.concatenate([
            np.random.default_rng([self.seed, channel, b + (1 << 20)]).standard_normal(
                (NOISE_BLOCK, self.fw + PATCH - 1), dtype=np.float32)
            for b in range(b0, b1 + 1)])
        return z[r0 - b0 * NOISE_BLOCK:r1 - b0 * NOISE_BLOCK]

    def rows(self, f0, f1):
        """Field rows f0..f1 as float32 (f1 - f0, fw, 3)."""
        # Circular convolution over the band plus PATCH - 1 rows and columns:
        # from index PATCH - 1 on nothing has wrapped, which is exactly f0..f1.
        size = (f1 - f0 + PATCH - 1, self.fw + PATCH - 1)
        spectrum = self._spectra.get(size)
        if spectrum is None:
            spectrum = self._spectra[size] = np.fft.rfft2(self.kernel, s=size)
        shaped = []
        for c in range(3):
            z = self._noise(c, f0 - PATCH // 2, f1 + PATCH // 2 - 1)
            shaped.append(np.fft.irfft2(np.fft.rfft2(z) * spectrum, s=size)[PATCH - 1:, PATCH - 1:]
                          .astype(np.float32))
        m = self.mix
        return np.stack([m[i, 0] * shaped[0] + m[i, 1] * shaped[1] + m[i, 2] * shaped[2] for i in range(3)],
                        axis=-1)


def _strength_gain(field, source_size, out_size):
    """The factor that makes the field, drawn into the output, measure 1 when
    the output is looked at at the ORIGINAL's size (area-averaged, as
    remaining() measures): bicubic upsampling softens grain, and grain finer
    than the original's pixels averages away there. Measured on a piece of
    128 original pixels, so the full-size field never has to exist."""
    (w, h), (W, H) = source_size, out_size
    k = field.fw / w
    pw, ph = min(field.fw, round(128 * k)), min(field.fh, round(128 * k))
    piece = np.ascontiguousarray(_luma(field.rows(0, ph)[:, :pw]))
    up = Image.fromarray(piece, "F").resize((max(1, round(pw * W / field.fw)), max(1, round(ph * H / field.fh))),
                                            Image.BICUBIC)
    seen = np.asarray(up.resize((max(1, round(pw * w / field.fw)), max(1, round(ph * h / field.fh))), Image.BOX))
    return float(piece.std()) / max(float(seen.std()), 1e-6)


def _seed(rgb):
    small = np.asarray(Image.fromarray(np.asarray(rgb)[..., :3]).resize((32, 32), Image.BILINEAR))
    return zlib.crc32(small.tobytes())


def remaining(out_rgb, source_size):
    """Grain the upscaled picture still has, measured like the original's but
    at the original's scale - what a viewer sees at the same display size.
    Averaged down by area (BOX), as the added grain is calibrated: Lanczos'
    wider filter averages neighbouring grain away and read an upscale that
    had KEPT all its grain (4.81) as 3.87 - which would then get 2.86 more."""
    small = Image.fromarray(np.asarray(out_rgb)[..., :3]).resize(source_size, Image.BOX)
    prof = analyse(np.asarray(small))
    return prof["sigma"] if prof else 0.0


def restore(out_rgb, profile, source_rgb=None, strength=1.0):
    """(uint8 HxWx3, info): the upscale `out_rgb` with the grain it lost laid
    back, or unchanged when the profile has no grain or the upscale kept it.
    info says what was done, for the details panel."""
    started = time.time()
    if not profile or not profile.get("grain"):
        return out_rgb, {"added": False, "why": (profile or {}).get("why_not") or "no grain found"}
    out_rgb = np.asarray(out_rgb)
    w, h = profile["source_size"]
    H, W = out_rgb.shape[:2]
    kept = remaining(out_rgb, (w, h))
    target = profile["sigma"] * strength
    add = float(np.sqrt(max(0.0, target ** 2 - kept ** 2)))
    info = {"sigma": profile["sigma"], "kept": round(kept, 2), "mono": profile["mono"],
            "size": profile["size"], "hinted": profile.get("hinted", False)}
    if add < MIN_ADD:
        return out_rgb, {**info, "added": False,
                         "why": "the upscale kept the grain (%.2f of %.2f)" % (kept, target)}
    # Finer than the original, but never finer than an output pixel (an upscale
    # held under 2x by a megapixel limit gets grain at its own pixels).
    fine = min(FINER, W / w, H / h)
    fw, fh = max(1, round(w * fine)), max(1, round(h * fine))
    field = _GrainField(profile, fw, fh, _seed(source_rgb if source_rgb is not None else out_rgb))
    gain = add * _strength_gain(field, (w, h), (W, H))
    centres = (np.linspace(LUMA_LOW, LUMA_HIGH, LUMA_BANDS + 1)[:-1] + np.linspace(LUMA_LOW, LUMA_HIGH, LUMA_BANDS + 1)[1:]) / 2
    result = np.empty_like(out_rgb)
    for r0 in range(0, H, STRIP):
        r1 = min(H, r0 + STRIP)
        # The field rows under these output rows, plus three on each side for
        # the bicubic filter to read past the strip's edges - with the band
        # drawn seamlessly, the strips join without a seam.
        top, bottom = r0 * fh / H, r1 * fh / H
        f0, f1 = int(np.floor(top)) - 3, int(np.ceil(bottom)) + 3
        band = field.rows(f0, f1)
        box = (0.0, top - f0, float(fw), bottom - f0)
        g = np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(band[..., c]), "F")
                                 .resize((W, r1 - r0), Image.BICUBIC, box=box)) for c in range(3)], axis=-1)
        o = out_rgb[r0:r1].astype(np.float32)
        y = _luma(o)
        curve = np.interp(y, centres, profile["curve"]).astype(np.float32)
        # Fade out into black and white, where grain clips in the original too.
        fade = np.clip(np.minimum(y / LUMA_LOW, (255.0 - y) / (255.0 - LUMA_HIGH)), 0, 1).astype(np.float32)
        o += g * (gain * curve * fade)[..., None]
        result[r0:r1] = np.clip(o + 0.5, 0, 255).astype(np.uint8)
    return result, {**info, "added": True, "added_sigma": round(add, 2), "fine": round(fine, 2),
                    "seconds": round(time.time() - started, 2)}
