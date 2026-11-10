# Switching the image tagger

The AI index tags every picture and video frame with characters, general tags
and a rating. The tagger is a model directory plus a config block on the
vision worker. Switching to a newer tagger re-tags the library on its own;
nothing has to be wiped. This file is the procedure, written so an AI agent
or an admin can follow it step by step.

## How it fits together

- **The worker** (`vision/vision_worker.py`) loads the tagger named by the
  `"tagger"` block of `vision_config.json`. Leave out the block to get the
  built-in default (`DEFAULT_TAGGER` in the worker):

  ```json
  "tagger": {
    "id": "wd-eva02-2026-canary",
    "source": "ashen-sensored/wd-eva02-tagger-2026-canary",
    "dir": "models/wd-eva02-2026-canary",
    "thresholds": {"character": 0.5, "general": 0.5, "rating": 0.5}
  }
  ```

  - `dir` holds `model.onnx` and `selected_tags.csv`, either absolute or
    relative to the worker's directory.
  - `id` is the tagger's short name. It is reported in `/v1/status` and in
    every `/v1/analyze` reply.
  - `source` is where the model came from; it is only for people reading the
    config.
  - `thresholds` sets the minimum score per category.
- **The indexer** (`media_indexer.py`) asks the worker's `/v1/status` for
  `tagger.id` at the start of each sweep. It stamps every tagged file with that
  id (`media.tag_model`). A file whose stamp differs from the worker's id gets
  re-tagged, newest first, during the tags pass of the indexer's rotation.
- **A restart at any time is safe.** Each walk re-reads the stamps from the
  database, so a stopped or crashed indexer resumes where it was, and finished
  files are not redone.
- **The old tags stay until a file is re-tagged.** Search, character pages and
  the People grid keep working during the run, which can take days on a large
  library.
- **A config change mid-sweep:** if the worker's tagger changes while a sweep
  runs, the indexer sees the new id in the reply, stops that sweep and starts
  over with the new id. Tags are never stored under the wrong stamp.

Changing only the `thresholds` (same `id`) does not re-tag anything. It applies
to files tagged from then on. To re-tag everything at the new thresholds, give
the tagger a new `id` (for example `wd-eva02-2026-canary-t045`).

## 1. Find candidates

Look for taggers newer than the current one: a later Danbooru cutoff and more
character classes. Check:
- the licence (it must allow your use);
- the format (ONNX directly, or timm safetensors that can be exported);
- the input (size, padding, colour order, normalisation).

## 2. Build a comparison set

On the machine that has the index and the library:

```bash
python -I vision/tagger/make_eval_set.py <media-dl dir> <eval dir>
```

This copies about 65 "known" pictures (characters the current tagger names
confidently) and 70 "unknown" ones (solo pictures where it names nobody), and
writes `tagsel.json`. Move the eval dir to wherever you run the models. The
pictures are your library's, so keep the set private and delete it afterwards.

## 3. Run every candidate on the set

```bash
HF_HOME=<eval dir>/hf huggingface-cli download <repo>      # per candidate
python -I vision/tagger/eval_run.py <eval dir> <model>      # per candidate, incl. the current one
python -I vision/tagger/eval_report.py <eval dir> [model=threshold ...]
```

`eval_run.py` already knows how to run the WD v3 and 2026 canary, PixAI
v0.9 / v1.0 and Camie v2 models. For any other candidate, add one branch to
`build()`.

Run the current tagger as well. It must reproduce the index's own characters
on nearly every known picture (the canary switch: 140 of 141). If it doesn't,
the preprocessing in the script is wrong and the comparison is meaningless.

## 4. Decide

A candidate is better when:
- on the known set it finds the expected character as often as the current
  tagger does;
- on the unknown set it names characters, and the names are right when you
  look at the pictures;
- where two candidates disagree, the confused characters look alike. Two
  similar-looking characters mixed up is acceptable, because the current
  tagger makes that kind of mistake too.

Also weigh cost. Every library picture goes through the tagger once, so a
model with 5× the compute takes 5× as long to re-tag.

Pick the thresholds on the set. Count general tags per picture at a few
thresholds and match the current tagger's count. For characters, look at the
names that only appear between two thresholds; if they are mostly wrong, use
the higher threshold.

Record the decision with the numbers, for example in the project's decision
log. The 2026-11-10 decision is the worked example: WD v3 → 2026 canary.

## 5. Build the model directory

If the candidate already ships an ONNX file with the WD v3 interface (input
NHWC BGR 0–255 at its size, white-padded square; output sigmoid; plus
`selected_tags.csv`), copy the two files into a new directory under the
worker's `models/`.

If it is a timm safetensors model, export it:

```bash
python -I vision/tagger/export_timm_tagger.py <snapshot dir> <out dir> --arch <timm arch> --size <px> --norm wd --check
```

`--check` prints the largest difference between the torch model and the ONNX
file; it should be around 1e-4. A model with a different interface, such as
another input size, padding colour or normalisation, needs its own wrapper in
that script. The worker's `tagger_prep` stays the same for every model:
the export adapts to it.

## 6. Switch

On the worker host:
1. Copy the directory to `models/<new id>` and verify `SHA256SUMS`.
2. Set the `"tagger"` block in `vision_config.json`: new `id`, `source`,
   `dir`, `thresholds`.
3. Restart the worker. Its log must show `tagger <new id> loaded` and no
   `fell back to CPU` warning.
4. Send one picture with a known new character through `/v1/analyze`
   (`tasks=tags`, `urgent=1`). The reply's `models.tags` must be the new id.

Nothing changes on the indexer. Its next sweep reads the new id and starts the
re-tag. The indexer log shows `N files to do` for the tags pass, and the admin
status page shows the progress against the new id.

## Rolling back

Put the old `"tagger"` block back (or remove the block to get the built-in
default) and restart the worker. Files already re-tagged then carry the new
id, so they get tagged again with the old model. Keep the old model directory
until the new tagger has proven itself.

## Not covered

- Training our own tagger or a character head is a separate project.
- A tagger whose output is not one score per row of `selected_tags.csv`, with
  categories 0 general, 4 character and 9 rating, needs code in the worker's
  `tag_images`.
