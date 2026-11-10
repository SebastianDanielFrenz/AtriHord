"""Turn a WD-style timm tagger (model.safetensors + selected_tags.csv) into the
model directory the vision worker loads: model.onnx with the interface of
SmilingWolf's WD v3 ONNX files, plus selected_tags.csv and SHA256SUMS.

The worker's tagger_prep sends NHWC float32 BGR 0-255, white-padded square,
bicubic. This wrapper takes exactly that and does the model's own
normalisation inside the graph, so the worker needs no per-model code:

  --norm wd     x / 127.5 - 1     (the WD EVA02 family, incl. the 2026 canary)
  --norm none   x / 255
  --rgb         flip BGR to RGB first (for a model trained on RGB)

The output is sigmoid probabilities, one per row of selected_tags.csv.

  python -I export_timm_tagger.py <snapshot dir> <out dir>
         [--arch eva02_large_patch14_448] [--size 448] [--norm wd] [--rgb]

<snapshot dir> is the downloaded Hugging Face repo (huggingface-cli download
<repo> --local-dir <dir>). Then check the export (--check runs the torch model
and the ONNX file on random input and prints the largest difference; the 2026
canary gave 0.00019) and follow vision/tagger/TAGGER_UPGRADE.md.
"""
import argparse
import csv
import hashlib
import os
import shutil

import numpy as np
import torch
import timm
from safetensors.torch import load_file


class Wrapped(torch.nn.Module):
    def __init__(self, inner, norm, rgb):
        super().__init__()
        self.inner, self.norm, self.rgb = inner, norm, rgb

    def forward(self, x):                      # N,H,W,3 BGR 0-255
        if self.rgb:
            x = x.flip(-1)
        x = x.permute(0, 3, 1, 2)
        x = x / 127.5 - 1.0 if self.norm == "wd" else x / 255.0
        return torch.sigmoid(self.inner(x))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("snapshot")
    ap.add_argument("out")
    ap.add_argument("--arch", default="eva02_large_patch14_448")
    ap.add_argument("--size", type=int, default=448)
    ap.add_argument("--norm", choices=("wd", "none"), default="wd")
    ap.add_argument("--rgb", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    tags_csv = os.path.join(args.snapshot, "selected_tags.csv")
    with open(tags_csv, encoding="utf-8") as fh:
        n = sum(1 for _ in csv.DictReader(fh))
    m = timm.create_model(args.arch, pretrained=False, num_classes=n)
    m.load_state_dict(load_file(os.path.join(args.snapshot, "model.safetensors")), strict=True)
    w = Wrapped(m.eval(), args.norm, args.rgb).eval()

    os.makedirs(args.out, exist_ok=True)
    onnx_path = os.path.join(args.out, "model.onnx")
    torch.onnx.export(w, torch.zeros(2, args.size, args.size, 3), onnx_path,
                      input_names=["input"], output_names=["output"],
                      dynamic_axes={"input": {0: "batch"}, "output": {0: "batch"}},
                      opset_version=17, dynamo=False)
    shutil.copy(tags_csv, os.path.join(args.out, "selected_tags.csv"))
    with open(os.path.join(args.out, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        for name in ("model.onnx", "selected_tags.csv"):
            h = hashlib.sha256()
            with open(os.path.join(args.out, name), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            fh.write(f"{h.hexdigest()}  {name}\n")
    print(f"exported {os.path.getsize(onnx_path) >> 20} MiB, {n} tags -> {args.out}")

    if args.check:
        import onnxruntime as ort
        x = np.random.default_rng(0).uniform(0, 255, (2, args.size, args.size, 3)).astype(np.float32)
        with torch.no_grad():
            ref = w(torch.from_numpy(x)).numpy()
        got = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"]).run(None, {"input": x})[0]
        print(f"largest difference torch vs onnx: {np.abs(ref - got).max():.6f}")


if __name__ == "__main__":
    main()
