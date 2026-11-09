"""Sideways videos seen upright by the AI passes (KSF-075 follow-up): frames are turned
before scaling, stamps carry "+t<deg>" so a changed turn re-does the video, faces keep
frame_turn for their crop, a re-index replaces the old face rows and leaves clusters alone.

Synthetic AI index + temp library with an ffmpeg-made video; worker faked; nothing under
/data2 is written.   venv/bin/python orient_index_check.py
"""
import base64, json, os, shutil, struct, subprocess, sys, tempfile

BASE = os.environ.get("ATRIHORD_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
tmp = tempfile.mkdtemp(prefix="orient_index_check_")
store = os.path.join(tmp, "ai"); os.makedirs(store)
os.environ["KSF_MEDIA_AI_DIR"] = store
lib = os.path.join(tmp, "lib"); os.makedirs(os.path.join(lib, "yt"))
import media_ai, media_indexer as mi  # noqa: E402

ok = True
def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))

vid = os.path.join(lib, "yt", "a.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=5:duration=4",
                "-pix_fmt", "yuv420p", vid], check=True)

def dims(jpeg):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", "-"], input=jpeg, capture_output=True)
    w, h = (int(x) for x in p.stdout.decode().strip().split(","))
    return w, h

# frames
for deg, want_portrait in ((0, False), (90, True), (270, True), (180, False)):
    fr = mi.video_frames(vid, "yt/a.mp4", deg)
    w, h = dims(fr[0][1])
    check(f"frames turn {deg}: portrait={want_portrait}", (h > w) == want_portrait and len(fr) >= 1, (w, h))

# stamps
m = {"tag_model": "T", "face_model": "F", "caption_model": "C", "ocr_model": "O", "orient_model": "X", "genre_model": "G"}
t = mi.turned_models(m, "video", 90)
check("stamps +t90 on frame passes only", t["tag_model"] == "T+t90" and t["face_model"] == "F+t90"
      and t["orient_model"] == "X" and t["genre_model"] == "G", t)
check("no turn / image: unchanged", mi.turned_models(m, "video", 0) == m and mi.turned_models(m, "image", 90) == m)

# effective turns: auto, then hand override wins, hand 0 = as filmed
conn = media_ai.connect(tmp if False else store)
media_ai.ensure_schema(conn) if hasattr(media_ai, "ensure_schema") else None
media_ai.set_orient(conn, "r", "yt/a.mp4", 90); conn.commit()
media_ai.upsert(conn, "r", "yt/a.mp4", os.path.getsize(vid), "video", {"orient_model": "x"}); conn.commit()
check("auto turn read", mi.effective_turns(conn, tmp).get(("r", "yt/a.mp4")) == 90)
json.dump({"r": {"yt/a.mp4": {"deg": 270, "by": "t", "at": 1}}}, open(os.path.join(tmp, "rotations.json"), "w"))
check("hand override wins", mi.effective_turns(conn, tmp).get(("r", "yt/a.mp4")) == 270)
json.dump({"r": {"yt/a.mp4": {"deg": 0, "by": "t", "at": 1}}}, open(os.path.join(tmp, "rotations.json"), "w"))
check("hand 0 = as filmed", mi.effective_turns(conn, tmp).get(("r", "yt/a.mp4")) == 0)
os.remove(os.path.join(tmp, "rotations.json"))

# flush with a fake worker
emb = base64.b64encode(struct.pack("512f", *([0.1] * 512))).decode()
def fake_post(cfg, items, urgent=False):
    return {"results": [{"name": l, "tags": {}, "faces": [{"bbox": [10, 10, 50, 50], "det_score": .9,
                         "quality": .5, "embedding_b64": emb}]} for l, _ in items], "failed": []}
mi.post_batch = fake_post
cfg = {"prep_workers": 2, "tasks": "tags,faces"}
models = mi.models_for("tags,faces")
size = os.path.getsize(vid)
def entry(): return ("r", "yt/a.mp4", vid, size, "video")

# 1. indexed upright first (no turn known yet)
mi.flush(conn, cfg, [entry()], models, False, False, {})
row = conn.execute("SELECT id, tag_model, face_model FROM media WHERE root='r' AND path='yt/a.mp4'").fetchone()
mid = row["id"]
check("plain stamps while upright", row["face_model"] == models["face_model"], tuple(row))
n0 = conn.execute("SELECT COUNT(*) FROM faces WHERE media_id=?", (mid,)).fetchone()[0]
conn.execute("INSERT INTO clusters (id, name) VALUES (7, 'someone')")
conn.execute("UPDATE faces SET cluster_id=7 WHERE media_id=?", (mid,)); conn.commit()

# 2. orient finds 90 -> the video is a candidate again; plain stamps no longer satisfy
turns = {("r", "yt/a.mp4"): 90}
cands = mi.iter_candidates([("r", lib)], conn, models, 1 << 30, turns)
check("turned video is a candidate again", [c[1] for c in cands] == ["yt/a.mp4"], cands)
check("not a candidate without a turn", mi.iter_candidates([("r", lib)], conn, models, 1 << 30, {}) == [])

# 3. redo: stamps +t90, faces replaced (same count, not doubled), frame_turn kept, cluster row alive
mi.flush(conn, cfg, [entry()], models, False, False, turns)
row = conn.execute("SELECT tag_model, face_model FROM media WHERE id=?", (mid,)).fetchone()
check("stamps carry +t90", row["face_model"].endswith("+t90") and row["tag_model"].endswith("+t90"), tuple(row))
fr = conn.execute("SELECT frame_turn, frame_w FROM faces WHERE media_id=?", (mid,)).fetchall()
check("faces replaced, not doubled", len(fr) == n0 and n0 > 0, (len(fr), n0))
check("faces remember the turn", all(r["frame_turn"] == 90 for r in fr))
check("cluster rows untouched", conn.execute("SELECT name FROM clusters WHERE id=7").fetchone()["name"] == "someone")
check("done: no longer a candidate", mi.iter_candidates([("r", lib)], conn, models, 1 << 30, turns) == [])

# 4. hand override back to 0 re-does it as filmed
cands = mi.iter_candidates([("r", lib)], conn, models, 1 << 30, {("r", "yt/a.mp4"): 0})
check("turn removed -> redone as filmed", len(cands) == 1)

# 5. old schema migrates
c2 = media_ai.connect(store)
check("faces.frame_turn exists", "frame_turn" in {r[1] for r in c2.execute("PRAGMA table_info(faces)")})

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
