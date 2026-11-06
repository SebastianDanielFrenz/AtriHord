"""Upscaling for media-dl's live upscaling.

Models, chosen per image by media-dl from the AI index (tags + caption) and,
for photos, by Settings (live_upscale.photo_model):

  anime                    RealESRGAN_x4plus_anime_6B  RRDBNet, 6 blocks, 4x   illustrations, manga
  general                  RealESRGAN_x2plus           RRDBNet, 23 blocks, 2x  photos until 2026-09-14
  nomoswebphoto-realplksr  4xNomosWebPhoto_RealPLKSR   RealPLKSR, 4x           photos (the default)
  nmkd-siax                4x_NMKD-Siax_200k           ESRGAN, 4x              photos, sharper
  ultrasharp               4x-UltraSharp               ESRGAN, 4x              photos, crisper
  nomos2-otf-esrgan        4xNomos2_otf_esrgan         ESRGAN, 4x              photos, softer

The four photo models are the user's shortlist from a bake-off on their own
photos (vault KSF-019): Real-ESRGAN wiped skin and fabric texture, and its 2x
model chained for 3-5x painted hair and invented an iris ring.

The Real-ESRGAN files come from the official xinntao/Real-ESRGAN releases
(BSD-3), the photo models from the sources OpenModelDB lists (Phhofm's GitHub
releases, Hugging Face mirrors), each checked against OpenModelDB's sha256. All
live in models/ next to this file:
  f872d837d3c90ed2e05227bed711af5671a6fd1c9f7d7e91c911a61f155e99da  anime_6B
  49fafd45f8fd7aa8d31ab2a22d14d91b536c34494a5cfe31eb5d89c2fa266abb  x2plus
  a9db66c9b674c6a5025b6ef3bee71a57c33b8605d8a2de0980470f89002efbbe  4xNomosWebPhoto_RealPLKSR  CC-BY-4.0
  560424d9f68625713fc47e9e7289a98aabe1d744e1cd6a9ae5a35e9957fd127e  4x_NMKD-Siax_200k          WTFPL
  a5812231fc936b42af08a5edba784195495d303d5b3248c24489ef0c4021fe01  4x-UltraSharp              CC-BY-NC-SA-4.0
  12db878907ed3a52ee97de552dfc0ce7ffd38559ca9760a7803cb2d06a737055  4xNomos2_otf_esrgan        CC-BY-4.0

Why the Real-ESRGAN network is written out here instead of `pip install
realesrgan`: that package drags in basicsr, which pins an old opencv and
imports a torchvision module removed in 0.17 - it would break this venv's torch
2.11 / cu128 stack that Florence, the tagger and the face models share. RRDBNet
is ~60 lines of plain torch. The photo models load through spandrel instead
(pure Python, no basicsr), which reads the architecture from the weights. Either
way the weights are read as tensors only - weights_only=True, or safetensors: a
.pth is a pickle, and spandrel's own file loader does not pass weights_only.

Precision per photo model is what the bake-off ran on this RTX 5060 and the user
judged: float16 for the ESRGAN ones, bfloat16 for RealPLKSR. A new model gets its
precision checked on real output - "it runs" is not "it is right": DySample's
sampling grid turns into blocks in bfloat16 (vault TOOL-011).

Any whole factor from 2 (plan_passes): the model runs whole passes while they
do not overshoot - the 4x models, and 2x for x2plus - and Lanczos takes the
result the rest of the way: down from one extra pass when a large part is left
(2x from a 4x model, 3x from two x2plus passes), up when only a sliver is (5x =
one 4x pass, then 1.25x). Tiles bound the VRAM an image can take on a card that
also runs a Minecraft server; the output is assembled on the CPU, never on the
GPU (a 4x 4K image would be ~800 MB there in half precision), and max_work_px
bounds how large an extra pass may grow it.
"""
from __future__ import annotations

import os
import time

import numpy as np
from PIL import Image

MODEL_SPECS = {
    "anime": {"file": "RealESRGAN_x4plus_anime_6B.pth", "scale": 4, "blocks": 6},
    "general": {"file": "RealESRGAN_x2plus.pth", "scale": 2, "blocks": 23},
    "nomoswebphoto-realplksr": {"file": "4xNomosWebPhoto_RealPLKSR.pth", "scale": 4,
                                "loader": "spandrel", "dtype": "bfloat16"},
    "nmkd-siax": {"file": "4x_NMKD-Siax_200k.pth", "scale": 4, "loader": "spandrel", "dtype": "float16"},
    "ultrasharp": {"file": "4x-UltraSharp.pth", "scale": 4, "loader": "spandrel", "dtype": "float16"},
    "nomos2-otf-esrgan": {"file": "4xNomos2_otf_esrgan.safetensors", "scale": 4,
                          "loader": "spandrel", "dtype": "float16"},
}

_NETS = {}          # model name -> network on the GPU, in its precision, eval


def _rrdbnet(torch, scale, num_block, num_feat=64, num_grow_ch=32):
    """The standard RRDBNet (ESRGAN), key-compatible with the released
    state dicts (conv_first, body.N.rdbK.convM, conv_body, conv_up1/2,
    conv_hr, conv_last)."""
    nn = torch.nn
    F = torch.nn.functional

    class ResidualDenseBlock(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
            self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
            self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self):
            super().__init__()
            self.rdb1 = ResidualDenseBlock()
            self.rdb2 = ResidualDenseBlock()
            self.rdb3 = ResidualDenseBlock()

        def forward(self, x):
            return self.rdb3(self.rdb2(self.rdb1(x))) * 0.2 + x

    class RRDBNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.scale = scale
            in_ch = 3 * (4 if scale == 2 else 16 if scale == 1 else 1)
            self.conv_first = nn.Conv2d(in_ch, num_feat, 3, 1, 1)
            self.body = nn.Sequential(*[RRDB() for _ in range(num_block)])
            self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
            self.conv_last = nn.Conv2d(num_feat, 3, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

        def forward(self, x):
            if self.scale == 2:
                feat = F.pixel_unshuffle(x, 2)
            elif self.scale == 1:
                feat = F.pixel_unshuffle(x, 4)
            else:
                feat = x
            feat = self.conv_first(feat)
            feat = feat + self.conv_body(self.body(feat))
            feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
            feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
            return self.conv_last(self.lrelu(self.conv_hr(feat)))

    return RRDBNet()


def _read_weights(torch, path):
    """A state dict, read as tensors only."""
    if path.endswith(".safetensors"):
        from safetensors.torch import load_file
        state = load_file(path)
    else:
        state = torch.load(path, map_location="cpu", weights_only=True)
    for key in ("params_ema", "params"):
        if isinstance(state.get(key), dict):
            return state[key]
    return state


def _spandrel_net(torch, spec, state):
    """A spandrel ImageModelDescriptor on the GPU in the spec's precision."""
    from spandrel import ImageModelDescriptor, ModelLoader
    net = ModelLoader().load_from_state_dict(state)
    if not isinstance(net, ImageModelDescriptor) or net.scale != spec["scale"]:
        raise RuntimeError("%s is not a %dx image model" % (spec["file"], spec["scale"]))
    if net.size_requirements.square:
        raise RuntimeError("%s needs square input, which the tiler does not make" % spec["file"])
    net.to(torch.device("cuda"))
    try:
        getattr(net, {"float16": "half", "bfloat16": "bfloat16"}[spec["dtype"]])()
    except AttributeError:
        net.model.to(getattr(torch, spec["dtype"]))
    net.eval()
    return net


def load(torch, name, model_dir):
    if name in _NETS:
        return _NETS[name]
    spec = MODEL_SPECS[name]
    state = _read_weights(torch, os.path.join(model_dir, spec["file"]))
    if spec.get("loader") == "spandrel":
        net = _spandrel_net(torch, spec, state)
    else:
        net = _rrdbnet(torch, spec["scale"], spec["blocks"])
        net.load_state_dict(state, strict=True)
        net = net.half().cuda().eval()
    _NETS[name] = net
    print("[vision] upscaler %s loaded" % name, flush=True)
    return net


def loaded():
    return sorted(_NETS)


def unload(reason=""):
    if not _NETS:
        return False
    _NETS.clear()
    import gc
    gc.collect()
    print("[vision] upscalers unloaded (%s)" % reason, flush=True)
    return True


def _run_tiled(torch, net, scale, rgb, tile, pad, dtype, mod=1, min_side=0):
    """uint8 HxWx3 in, uint8 (H*scale)x(W*scale)x3 out, a tile at a time. A tile
    is padded by replication to a multiple of `mod` and at least `min_side` -
    what the network needs - and the padding is cut off again."""
    F = torch.nn.functional
    h, w = rgb.shape[:2]
    src = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)          # 1x3xHxW uint8, CPU
    out = np.empty((h * scale, w * scale, 3), dtype=np.uint8)
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            y1, x1 = min(y0 + tile, h), min(x0 + tile, w)
            py0, px0 = max(y0 - pad, 0), max(x0 - pad, 0)
            py1, px1 = min(y1 + pad, h), min(x1 + pad, w)
            patch = src[:, :, py0:py1, px0:px1].cuda().to(dtype).div_(255)
            ph, pw = patch.shape[2], patch.shape[3]
            th = -(-max(ph, min_side) // mod) * mod
            tw = -(-max(pw, min_side) // mod) * mod
            if th != ph or tw != pw:
                patch = F.pad(patch, (0, tw - pw, 0, th - ph), mode="replicate")
            with torch.inference_mode():
                res = net(patch)
            cy0, cx0 = (y0 - py0) * scale, (x0 - px0) * scale
            piece = res[0, :, cy0:cy0 + (y1 - y0) * scale, cx0:cx0 + (x1 - x0) * scale]
            # Out of place: for a model that answers in float32, .float() hands
            # back the inference tensor itself, and an in-place clamp outside
            # inference_mode raises (vault TOOL-011).
            out[y0 * scale:y1 * scale, x0 * scale:x1 * scale] = (
                piece.float().clamp(0, 1).mul_(255).round_().byte().permute(1, 2, 0).cpu().numpy())
            del patch, res, piece
    return out


# What must be left over before one more pass (and Lanczos down) beats
# Lanczos up from the passes already made: 1.5x is clearly sharper from a
# pass, 1.25x or 1.125x is hard to tell apart and a pass would cost 4-16x more.
EXTRA_PASS_FROM = 1.5


def plan_passes(scale, factor, width, height, max_work_px):
    """How many passes of a `scale`x model a `factor` gets - a whole number, or
    the fractional scale of a target size. Whole passes
    while they do not overshoot; one more for a large remainder when the
    overshoot stays within max_work_px pixels. The first pass always runs,
    even when it overshoots - that is the upscale."""
    passes, reached = 0, 1
    while reached * scale <= factor:
        passes, reached = passes + 1, reached * scale
    rest = factor / reached
    grown = reached * scale
    if rest > 1 and (passes == 0 or (rest >= EXTRA_PASS_FROM
                                     and width * height * grown * grown <= max_work_px)):
        passes += 1
    return passes


def final_size(target, made, source, keep_larger=False, max_px=None, max_scale=None):
    """The size to hand back. Normally the target. With keep_larger, a result
    the passes made LARGER than the target keeps its size (user, 2026-09-14:
    "If an upscaler has to scale more than is needed, it should send the client
    the unnecessarily large image without downscaling it") - shrunk in
    proportion only as far as max_px pixels and max_scale times the source
    require, and never below the target."""
    tw, th = target
    mw, mh = made
    if not keep_larger or mw * mh <= tw * th:
        return (tw, th)
    s = 1.0
    if max_px:
        s = min(s, (max_px / (mw * mh)) ** 0.5)
    if max_scale:
        s = min(s, max_scale * source[0] / mw, max_scale * source[1] / mh)
    if s >= 1:
        return (mw, mh)
    w, h = int(mw * s), int(mh * s)
    return (w, h) if w * h >= tw * th else (tw, th)


def upscale(torch, image, name, factor, model_dir, tile=384, pad=16, max_work_px=256_000_000, target=None,
            keep_larger=False, max_px=None, max_scale=None):
    """(PIL image, seconds): `factor` times the size, or exactly `target`
    (width, height) - any scale above 1, which is what media-dl asks for when a
    megapixel limit binds. Whole model passes (plan_passes), then Lanczos to the
    exact size - or, with keep_larger, to final_size: an overshooting pass is
    handed back as it came out, within max_px and max_scale. Transparency is
    carried over with a Lanczos-resized alpha channel; the models only see RGB."""
    started = time.time()
    spec = MODEL_SPECS[name]
    net = load(torch, name, model_dir)
    if spec.get("loader") == "spandrel":
        req = net.size_requirements
        dtype, mod, min_side = getattr(torch, spec["dtype"]), max(1, req.multiple_of), req.minimum
    else:
        # pixel_unshuffle folds 2x2 (x2plus) or 4x4 (a 1x model) away
        dtype, mod, min_side = torch.float16, 2 if spec["scale"] == 2 else 4 if spec["scale"] == 1 else 1, 0
    alpha = None
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        alpha = image.convert("RGBA").getchannel("A")
    size = (int(target[0]), int(target[1])) if target else (image.width * factor, image.height * factor)
    src = image.convert("RGB")
    # A picture so large that even ONE pass would pass max_work_px (a 64 MP
    # input with the 2x model) is shrunk first so that pass lands at the limit:
    # softer than upscaling the original, but it stays inside RAM.
    first = src.width * src.height * spec["scale"] ** 2
    if first > max_work_px:
        shrink = (max_work_px / first) ** 0.5
        src = src.resize((max(1, int(src.width * shrink)), max(1, int(src.height * shrink))), Image.LANCZOS)
    rgb = np.asarray(src)
    scale = max(size[0] / src.width, size[1] / src.height)
    for _ in range(plan_passes(spec["scale"], scale, src.width, src.height, max_work_px)):
        rgb = _run_tiled(torch, net, spec["scale"], rgb, tile, pad, dtype, mod, min_side)
    result = Image.fromarray(rgb)
    size = final_size(size, result.size, image.size, keep_larger, max_px, max_scale)
    if result.size != size:
        result = result.resize(size, Image.LANCZOS)
    if alpha is not None:
        result = result.convert("RGBA")
        result.putalpha(alpha.resize(size, Image.LANCZOS))
    return result, time.time() - started
