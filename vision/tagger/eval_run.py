"""Run one candidate tagger over an eval set made by make_eval_set.py; write
character (and rating/copyright) scores per picture to <dir>/results_<model>.json.

usage: python -I eval_run.py <eval dir> <model>
models: wd3 canary pixai09 camie pixai10 - add a candidate as one more branch
of build() that returns predict(img) -> {category: {tag: p}}.

Models are read from <eval dir>/hf (HF_HOME, offline): download them first,
e.g. HF_HOME=<eval dir>/hf huggingface-cli download <repo>. ONNX models run on
DirectML when present (Windows, any GPU), else CPU. A model that needs
trust_remote_code (pixai10) runs code from its repo: read it before you run it.
Resumable: pictures already in the results file are skipped.
Runbook: vision/tagger/TAGGER_UPGRADE.md.
"""
import csv, glob, json, os, sys, time

import numpy as np
from PIL import Image

D, MODEL = sys.argv[1], sys.argv[2]
HF = os.path.join(D, "hf", "hub")
os.environ["HF_HOME"] = os.path.join(D, "hf")
os.environ["HF_HUB_OFFLINE"] = "1"


def snap(repo):
    base = os.path.join(HF, "models--" + repo.replace("/", "--"), "snapshots")
    return os.path.join(base, os.listdir(base)[0])


def flat_rgb(path):
    img = Image.open(path)
    img.load()
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255))
    bg.alpha_composite(img)
    return bg.convert("RGB")


def pad_square(img, size, color=(255, 255, 255), resample=Image.BICUBIC):
    side = max(img.size)
    sq = Image.new("RGB", (side, side), color)
    sq.paste(img, ((side - img.size[0]) // 2, (side - img.size[1]) // 2))
    return sq.resize((size, size), resample)


def wd_labels(csv_path):
    names, cats = [], []
    with open(csv_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            names.append(r["name"])
            cats.append(int(r["category"]))
    return names, np.array(cats)


def dml_session(path):
    import onnxruntime as ort
    have = ort.get_available_providers()
    return ort.InferenceSession(path, providers=[p for p in ("DmlExecutionProvider", "CUDAExecutionProvider",
                                                             "CPUExecutionProvider") if p in have])


def build():
    """Returns predict(img) -> {category: {tag: p}} with p >= 0.05."""
    if MODEL in ("wd3", "canary"):
        repo = {"wd3": "SmilingWolf/wd-eva02-large-tagger-v3",
                "canary": "ashen-sensored/wd-eva02-tagger-2026-canary"}[MODEL]
        names, cats = wd_labels(os.path.join(snap(repo), "selected_tags.csv"))
        if MODEL == "wd3":
            sess = dml_session(os.path.join(snap(repo), "model.onnx"))
            inp = sess.get_inputs()[0].name

            def logits(img):
                x = np.asarray(pad_square(img, 448), dtype=np.float32)[:, :, ::-1][None].copy()
                return sess.run(None, {inp: x})[0][0]  # already sigmoid
        else:
            import timm, torch
            from safetensors.torch import load_file
            torch.set_num_threads(os.cpu_count())
            m = timm.create_model("eva02_large_patch14_448", pretrained=False, num_classes=len(names))
            m.load_state_dict(load_file(os.path.join(snap(repo), "model.safetensors")), strict=True)
            m.eval()

            def logits(img):
                a = np.asarray(pad_square(img, 448), dtype=np.float32)[:, :, ::-1] / 127.5 - 1.0
                x = torch.from_numpy(a.copy()).permute(2, 0, 1).unsqueeze(0)
                with torch.no_grad():
                    return torch.sigmoid(m(x)[0]).numpy()

        def predict(img):
            p = logits(img)
            out = {}
            for cat, key in ((4, "character"), (9, "rating")):
                idx = np.where((cats == cat) & (p >= 0.05))[0]
                out[key] = {names[i]: float(p[i]) for i in idx}
            return out
        return predict

    if MODEL == "pixai09":
        root = snap("deepghs/pixai-tagger-v0.9-onnx")
        names, cats = wd_labels(os.path.join(root, "selected_tags.csv"))
        sess = dml_session(os.path.join(root, "model.onnx"))
        inp = sess.get_inputs()[0]
        print("pixai09 input", inp.name, inp.shape, [o.name for o in sess.get_outputs()])

        def predict(img):
            a = np.asarray(img.resize((448, 448), Image.BILINEAR), dtype=np.float32) / 255.0
            a = (a - 0.5) / 0.5
            x = a.transpose(2, 0, 1)[None].copy()
            p = sess.run(["prediction"], {inp.name: x})[0][0]
            idx = np.where((cats == 4) & (p >= 0.05))[0]
            return {"character": {names[i]: float(p[i]) for i in idx}}
        return predict

    if MODEL == "camie":
        root = snap("Camais03/camie-tagger-v2")
        meta = json.load(open(os.path.join(root, "camie-tagger-v2-metadata.json"), encoding="utf-8"))
        tm = meta["dataset_info"]["tag_mapping"]
        idx_to_tag = {int(k): v for k, v in tm["idx_to_tag"].items()}
        tag_cat = tm["tag_to_category"]
        size = meta["model_info"]["img_size"]
        n = max(idx_to_tag) + 1
        names = [idx_to_tag.get(i, "") for i in range(n)]
        cats = np.array([tag_cat.get(t, "") for t in names])
        sess = dml_session(os.path.join(root, "camie-tagger-v2.onnx"))
        inp = sess.get_inputs()[0].name
        mean = np.array([0.485, 0.456, 0.406], np.float32)
        std = np.array([0.229, 0.224, 0.225], np.float32)

        def predict(img):
            w, h = img.size
            s = size / max(w, h)
            r = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS)
            sq = Image.new("RGB", (size, size), (124, 116, 104))
            sq.paste(r, ((size - r.size[0]) // 2, (size - r.size[1]) // 2))
            a = (np.asarray(sq, dtype=np.float32) / 255.0 - mean) / std
            outs = sess.run(None, {inp: a.transpose(2, 0, 1)[None].copy()})
            lg = outs[1] if len(outs) >= 2 else outs[0]
            p = 1 / (1 + np.exp(-lg[0]))
            out = {}
            for key in ("character", "copyright"):
                idx = np.where((cats == key) & (p >= 0.05))[0]
                out[key] = {names[i]: float(p[i]) for i in idx}
            return out
        return predict

    if MODEL == "pixai10":
        import torch
        from transformers import pipeline
        torch.set_num_threads(os.cpu_count())
        root = snap("pixai-labs/pixai-tagger-v1.0")
        tagger = pipeline(model=root, image_processor=root, trust_remote_code=True, device="cpu")

        def predict(img):
            res = tagger(img, threshold=0.05)["results"]
            return {k: {t: float(p) for t, p in (v.items() if isinstance(v, dict) else v)}
                    for k, v in res.items() if k in ("character", "copyright")}
        return predict

    raise SystemExit("unknown model " + MODEL)


def main():
    predict = build()
    files = sorted(glob.glob(os.path.join(D, "img", "*")))
    out_path = os.path.join(D, f"results_{MODEL}.json")
    res = json.load(open(out_path)) if os.path.exists(out_path) else {}
    t0 = time.time()
    for n, f in enumerate(files):
        key = os.path.splitext(os.path.basename(f))[0]
        if key in res:
            continue
        try:
            r = predict(flat_rgb(f))
        except Exception as exc:  # one broken picture must not stop the run
            r = {"error": str(exc)}
        for k, v in list(r.items()):
            if isinstance(v, dict):
                r[k] = dict(sorted(v.items(), key=lambda kv: -kv[1])[:8])
        res[key] = r
        if n % 10 == 0:
            json.dump(res, open(out_path, "w"), indent=0)
            print(f"{MODEL} {n + 1}/{len(files)} {time.time() - t0:.0f}s", flush=True)
    json.dump(res, open(out_path, "w"), indent=0)
    print(f"{MODEL} done {len(res)} in {time.time() - t0:.0f}s")


main()
