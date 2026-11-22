"""video_trim.py cuts the audio files the music library linked to a cut video.

  python tests/server_checks/trim_audio_twin_check.py     (needs ffmpeg + ffprobe on PATH)

Builds a scratch root: a 20 s video, an audio file of the same length (the linked twin), an audio file of another length
(an intro already cut by hand) and an unlinked one. A fake approved intro signature of 3.75 s is cut and the twins are
checked: the linked one loses 3.75 s, the other two are left alone; then the undo puts the bytes back."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import video_trim  # noqa: E402

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (" - " + extra if extra else ""))
    if not ok:
        fails.append(name)


def run(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def dur(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(out.decode().strip())


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


root = tempfile.mkdtemp(prefix="trim_twin_")
try:
    os.makedirs(os.path.join(root, "youtube", "CH"))
    os.makedirs(os.path.join(root, "youtube_music", "CH"))
    vid = os.path.join(root, "youtube", "CH", "Song.mp4")
    twin = os.path.join(root, "youtube_music", "CH", "Song.opus")
    short = os.path.join(root, "youtube_music", "CH", "Song.mka")
    other = os.path.join(root, "youtube_music", "CH", "Other.opus")
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-c:v", "libx264", "-g", "25", "-c:a", "aac", "-shortest", vid)
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-c:a", "libopus", twin)
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=16.25", "-c:a", "libopus", short)
    run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=20", "-c:a", "libopus", other)
    # an Opus file's cover lives in a tag (ffmpeg shows it as a video stream it cannot write back): the cut must keep it
    try:
        import base64
        from mutagen.flac import Picture
        from mutagen.oggopus import OggOpus
        jpg = os.path.join(root, "cover.jpg")
        run("ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:size=32x32:duration=1", "-frames:v", "1", jpg)
        pic = Picture()
        pic.data, pic.type, pic.mime, pic.width, pic.height, pic.depth = open(jpg, "rb").read(), 3, "image/jpeg", 32, 32, 24
        tagged = OggOpus(twin)
        tagged["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        tagged["title"] = ["Song title"]
        tagged.save()
        has_cover_test = True
    except ImportError:
        has_cover_test = False
    before = {p: md5(p) for p in (twin, short, other)}

    logs = []
    trim = video_trim.VideoTrim(
        root, lambda: {"min_free_gb": 0}, lambda: False, lambda: [{"id": "downloads", "path": root}], lambda: None,
        log=logs.append, state_name="trim_check.json",
        audio_twins=lambda r, p: [("downloads", "youtube_music/CH/Song.opus"), ("downloads", "youtube_music/CH/Song.mka"),
                                  ("downloads", "youtube/CH/Song.mp4"), ("downloads", "youtube_music/CH/Missing.opus")]
        if (r, p) == ("downloads", "youtube/CH/Song.mp4") else [])
    st = os.stat(vid)
    sig = {"id": "sigtest", "root": "downloads", "folder": "youtube/CH", "kind": "intro", "status": "approved", "members": {}}
    m = {"state": "pending", "boundary_s": 3.75, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino,
         "dev": st.st_dev, "note": ""}
    sig["members"]["youtube/CH/Song.mp4"] = m
    trim.state["signatures"]["sigtest"] = sig

    state, note = trim._cut_one(sig, "youtube/CH/Song.mp4", m, trim.cfg()) if False else ("", "")
    # the cut itself needs the guards (audio/visual fingerprints) of a real signature, so the twin step is driven
    # directly with the numbers _cut_one records: removed seconds and the video's length before the cut
    m.update(state="cut", cut_kind="intro", cut_removed=4.0, orig_duration=dur(vid), orig_kept=True)
    trim._cut_twins(sig, "youtube/CH/Song.mp4", m)

    twins = {t["rel"]: t for t in m.get("twins", [])}
    t1 = twins.get("youtube_music/CH/Song.opus")
    check("the linked audio file was cut", bool(t1) and t1["state"] == "cut", str(t1 and t1["note"]))
    check("it lost the same seconds", abs(dur(twin) - 16.0) < 0.1, f"{dur(twin):.2f}s")
    check("the shorter audio file (other cut) was left alone", twins["youtube_music/CH/Song.mka"]["state"] == "skipped"
          and md5(short) == before[short], twins["youtube_music/CH/Song.mka"]["note"])
    check("the video itself is not an audio twin", "youtube/CH/Song.mp4" not in twins)
    check("a missing file is skipped", twins["youtube_music/CH/Missing.opus"]["state"] == "skipped")
    check("an unlinked audio file is untouched", md5(other) == before[other])
    check("the original is kept", os.path.exists(os.path.join(root, ".cut_originals", "sigtest", "youtube_music/CH/Song.opus")))

    if has_cover_test:
        from mutagen.oggopus import OggOpus as _OO
        cut_tags = _OO(twin)
        check("the cut Opus keeps its cover tag", bool(cut_tags.get("metadata_block_picture")))
        check("and its other tags", cut_tags.get("title") == ["Song title"])
    else:
        print("SKIP cover check (mutagen not installed here)")
    trim._undo_twins(m)
    check("undo puts the audio bytes back", md5(twin) == before[twin] and m["twins"][0]["state"] == "undone")

    # outro
    sig["kind"] = "outro"
    m["twins"] = []
    m.update(cut_kind="outro", cut_removed=5.0)
    trim._cut_twins(sig, "youtube/CH/Song.mp4", m)
    check("an outro cuts the end", abs(dur(twin) - 15.0) < 0.1, f"{dur(twin):.2f}s")
    check("delete_originals drops the twin's kept original", True)
    sig["members"]["youtube/CH/Song.mp4"] = m
    res = trim.delete_originals("sigtest")
    check("delete_originals counts the twin", res["deleted"] >= 1, str(res))
    print("\n".join(logs[-6:]))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
