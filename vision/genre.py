"""Music genre and style for media-dl: Discogs-EffNet (MTG / Essentia), run
here under onnxruntime.

The model predicts the 400 most common Discogs styles ("Electronic---House",
"Pop---J-pop", "Stage & Screen---Soundtrack", ...) and gives a 1280-number
embedding per track - what the genre map in media-dl lays out. Chosen over
LAION CLAP after a bake-off on 64 library clips (vault KSF-042): EffNet put
59% of clips next to a clip of the same group and named them plausibly,
while CLAP's audio embeddings came out nearly identical for a soundtrack and
dubstep (cosine 0.905) and its zero-shot answer was "reggae" for everything.

Essentia is not installed here, so its input stage is rebuilt in numpy:
TensorflowPredictEffnetDiscogs = 16 kHz mono, frames of 512 with hop 256, a
Hann window, the power spectrum through 96 Slaney mel bands (unit_tri), then
log10(1 + 10000 x); patches of 128 frames with a hop of 62.
"""
from __future__ import annotations

import json
import os

import numpy as np

MODEL_NAME = "discogs-effnet-bsdynamic-1"
SAMPLE_RATE = 16000
PATCH_FRAMES = 128
PATCH_HOP = 62
BATCH = 64                      # patches per session.run: bounds GPU memory


def _slaney_mel(sr=SAMPLE_RATE, n_fft=512, n_mels=96, fmin=0.0, fmax=8000.0):
    """librosa.filters.mel(htk=False, norm='slaney'), which is Essentia's
    MelBands(warpingFormula='slaneyMel', weighting='linear', normalize='unit_tri')."""
    def hz_to_mel(f):
        f = np.asarray(f, dtype=np.float64)
        return np.where(f >= 1000.0, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (np.log(6.4) / 27.0),
                        f / (200.0 / 3))

    def mel_to_hz(m):
        m = np.asarray(m, dtype=np.float64)
        return np.where(m >= 15.0, 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0)), m * (200.0 / 3))

    fft_freqs = np.linspace(0, sr / 2, 1 + n_fft // 2)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_freqs[None, :]
    weights = np.zeros((n_mels, len(fft_freqs)))
    for i in range(n_mels):
        weights[i] = np.maximum(0, np.minimum(-ramps[i] / fdiff[i], ramps[i + 2] / fdiff[i + 1]))
    return (weights * (2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels]))[:, None]).astype(np.float32)


_MEL = _slaney_mel()
_HANN = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(512) / 511)).astype(np.float32)


def patches(audio: np.ndarray) -> np.ndarray:
    """(n, 128, 96) model input from 16 kHz mono float audio in [-1, 1]."""
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    if len(audio) < 512:
        return np.zeros((0, PATCH_FRAMES, 96), dtype=np.float32)
    n = 1 + (len(audio) - 512) // 256
    frames = np.lib.stride_tricks.as_strided(audio, shape=(n, 512),
                                             strides=(audio.strides[0] * 256, audio.strides[0]))
    mel = np.log10(1 + 10000 * ((np.abs(np.fft.rfft(frames * _HANN, axis=1)) ** 2) @ _MEL.T)).astype(np.float32)
    starts = range(0, mel.shape[0] - PATCH_FRAMES + 1, PATCH_HOP)
    if not len(starts):
        return np.zeros((0, PATCH_FRAMES, 96), dtype=np.float32)
    return np.stack([mel[s:s + PATCH_FRAMES] for s in starts])


def load(model_dir, vram_mb=512):
    """{session, styles, input, outputs}. CUDA when the provider loads, else CPU
    - which it does silently, hence the returned provider for the caller to log."""
    import onnxruntime as ort
    sess = ort.InferenceSession(
        os.path.join(model_dir, MODEL_NAME + ".onnx"),
        providers=[("CUDAExecutionProvider", {
            "gpu_mem_limit": int(vram_mb) * 1024 * 1024,
            "arena_extend_strategy": "kSameAsRequested",
        }), "CPUExecutionProvider"])
    with open(os.path.join(model_dir, MODEL_NAME + ".json"), encoding="utf-8") as fh:
        styles = json.load(fh)["classes"]
    outputs = {o.name: o.shape[-1] for o in sess.get_outputs()}
    return {"session": sess, "styles": styles, "input": sess.get_inputs()[0].name,
            "scores_out": next(k for k, v in outputs.items() if v == len(styles)),
            "embedding_out": next(k for k, v in outputs.items() if v != len(styles)),
            "provider": sess.get_providers()[0]}


def analyse(model, audio: np.ndarray):
    """(style scores [400], embedding [1280], patches) for one track, each the
    mean over its patches. None when the audio is shorter than one patch
    (about 2 s)."""
    x = patches(audio)
    if not len(x):
        return None
    scores, embs = [], []
    for i in range(0, len(x), BATCH):
        chunk = x[i:i + BATCH]
        n = len(chunk)
        # Every run the same shape: the CUDA arena keeps one set of blocks.
        # A last batch of each track's own size made it claim new ones per
        # size until gpu_mem_limit ran out, after some 40 tracks (KSF-042).
        if n < BATCH:
            chunk = np.concatenate([chunk, np.zeros((BATCH - n,) + chunk.shape[1:], np.float32)])
        s, e = model["session"].run([model["scores_out"], model["embedding_out"]],
                                    {model["input"]: chunk})
        scores.append(s[:n])
        embs.append(e[:n])
    return np.concatenate(scores).mean(0), np.concatenate(embs).mean(0), len(x)
