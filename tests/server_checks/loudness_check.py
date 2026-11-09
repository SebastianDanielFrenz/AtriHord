"""music_loudness (vault KSF-058): the measurement on real audio made with
ffmpeg, the target and gain rule on made-up numbers, the cache and the
background measurer. The server's use of it is in music_check.py.

  venv/bin/python loudness_check.py <staged server.py>
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

SERVER = os.path.abspath(sys.argv[1])
STAGE = os.path.dirname(SERVER)
sys.path.insert(0, STAGE)
import music_loudness as mlo  # noqa: E402

assert os.path.dirname(os.path.abspath(mlo.__file__)) == STAGE, mlo.__file__
tmp = tempfile.mkdtemp(prefix="loudness_check_")
ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""), flush=True)


def tone(name, gain_db=0.0, seconds=3, silent=False, video_only=False):
    path = os.path.join(tmp, name)
    if video_only:
        src = ["-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-c:v", "mjpeg"]
    elif silent:
        src = ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono", "-t", str(seconds), "-c:a", "libopus"]
    else:
        src = ["-f", "lavfi", "-i", f"sine=frequency=1000:duration={seconds}", "-af", f"volume={gain_db}dB",
               "-c:a", "libopus"]
    subprocess.run(["ffmpeg", "-v", "error", "-y"] + src + [path], check=True)
    return path


# ---- measuring ---------------------------------------------------------------
quiet = mlo.measure(tone("quiet.mka"))
loud = mlo.measure(tone("loud.mka", 12))
check("a file is measured: loudness in LUFS and a true peak", all(isinstance(x, float) for x in quiet + loud),
      (quiet, loud))
check("... 12 dB louder reads 12 dB louder, in loudness and in peak",
      abs((loud[0] - quiet[0]) - 12) <= 0.3 and abs((loud[1] - quiet[1]) - 12) <= 0.3, (quiet, loud))
# ffmpeg's sine is 1/8 of full scale: -18 dBFS; a 1 kHz sine's loudness is its RMS, 3 dB under the peak.
check("... at the level the tone was made at (sine at -18 dBFS: about -21 LUFS, peak about -18)",
      abs(quiet[0] + 21) <= 1.0 and abs(quiet[1] + 18) <= 1.0, quiet)
check("silence has no loudness to match", mlo.measure(tone("silent.mka", silent=True))[0] is None)
check("a file with no sound in it at all: nothing", mlo.measure(tone("picture.mkv", video_only=True)) == [None, None])
check("a file that is not there: nothing", mlo.measure(os.path.join(tmp, "nope.mka")) == [None, None])

# ---- the rule ---------------------------------------------------------------------
check("the ceiling: as loud as the file goes before its true peak reaches 0 dB",
      mlo.ceiling([-14.0, -9.0]) == -5.0 and mlo.ceiling([None, None]) is None and mlo.ceiling([-14.0, None]) == -14.0)
check("a gain is rounded DOWN, never up into clipping", mlo.gain_db([-13.96, -20.0], -6.0) == 7.9
      and mlo.gain_db([-14.0, -20.0], -6.0) == 8.0, (mlo.gain_db([-13.96, -20.0], -6.0), mlo.gain_db([-14.0, -20.0], -6.0)))
check("... a file too loud for the target is turned down", mlo.gain_db([-4.0, 1.0], -8.0) == -4.0)
check("... a file that cannot reach it is turned up only to its ceiling", mlo.gain_db([-20.0, -3.0], -6.0) == 3.0)
check("... a file with no loudness has no gain", mlo.gain_db([None, None], -6.0) is None)

# Twenty files: nineteen could go up to -5 or -6 LUFS, one very dynamic one only to -20.
lib = {("r", f"song{n}.opus"): [-14.0 - n * 0.1, -9.0 - (n % 2)] for n in range(19)}
lib[("r", "classical.flac")] = [-30.0, -10.0]
target = mlo.choose_target(lib.values())
tops = sorted(mlo.ceiling(v) for v in lib.values())
check("the target: the highest loudness that 95 % of files reach without clipping, on a 0.5 dB step",
      target == -7.0 and -7.0 < tops[1] < -6.5, (target, tops[:3]))
p = mlo.plan(lib)
peaks_after = [lib[k][1] + p["gains"][k[0]][k[1]] for k in lib]
check("no file is turned up past a true peak of 0 dB", max(peaks_after) <= 0.0, max(peaks_after))
reached = [lib[k][0] + p["gains"][k[0]][k[1]] for k in lib if mlo.ceiling(lib[k]) >= target]
check("every file that can reach the target plays at it (within the 0.1 dB rounding)",
      all(target - 0.1 - 1e-9 <= x <= target + 1e-9 for x in reached) and len(reached) == 19, reached)
check("... the one that cannot plays at its own ceiling",
      p["gains"]["r"]["classical.flac"] == 10.0, p["gains"]["r"]["classical.flac"])
check("an unmeasured file is never turned up (it may peak anywhere)", p["unknown"] == 0.0, p["unknown"])
masters = {("r", f"v{n}.mkv"): [-4.0, 1.0] for n in range(10)}
masters[("r", "quiet.opus")] = [-20.0, -14.0]
p2 = mlo.plan(masters)
check("... but turned down with the rest when most files are turned down (loud masters)",
      p2["target"] == -6.0 and p2["unknown"] == -2.0, p2)
check("nothing measured: no target, no gains", mlo.plan({}) == {"target": None, "gains": {}, "unknown": 0.0}
      and mlo.choose_target([[None, None]]) is None)
check("the rule is the one the module states", mlo.PEAK_CEILING_DBTP == 0.0 and mlo.REACH == 0.95)

# ---- the cache ------------------------------------------------------------------------
cpath = os.path.join(tmp, "cache.json")
c = mlo.LoudnessCache(cpath)
a, b = os.path.join(tmp, "quiet.mka"), os.path.join(tmp, "loud.mka")
link = os.path.join(tmp, "link.mka")
os.link(a, link)
check("a hard link is the same file: one key", c.key(os.stat(a)) == c.key(os.stat(link)))
check("... the version is part of it (a new measure() measures again)",
      c.key(os.stat(a)).startswith(f"{mlo.LOUDNESS_VERSION}:"))
c.put(os.stat(a), quiet)
c.put(os.stat(b), loud)
c.save()
c2 = mlo.LoudnessCache(cpath)
check("saved and read back", c2.get(os.stat(link)) == quiet and c2.get(os.stat(b)) == loud)
os.utime(b, (1, 1))
check("a changed file is not known", c2.get(os.stat(b)) is None)
c2.prune({c2.key(os.stat(a))})
c2.save()
check("pruned to the files that are still there", list(json.load(open(cpath))) == [c2.key(os.stat(a))])
open(os.path.join(tmp, "bad.json"), "w").write("{not json")
check("an unreadable cache file starts empty", mlo.LoudnessCache(os.path.join(tmp, "bad.json")).data == {})

# ---- the measurer ------------------------------------------------------------------------
files = []
for n in range(25):
    f = os.path.join(tmp, f"f{n}.opus")
    open(f, "w").write(str(n))
    files.append((f, os.stat(f)))
late = []
for n in range(3):
    f = os.path.join(tmp, f"late{n}.opus")
    open(f, "w").write("late" + str(n))
    late.append((f, os.stat(f)))
seen, running, peak_running = [], [0], [0]
lock = threading.Lock()
batches = []
m = None


def fake(path):
    with lock:
        running[0] += 1
        peak_running[0] = max(peak_running[0], running[0])
        seen.append(os.path.basename(path))
        n = len(seen)
    time.sleep(0.02)
    if n == 24:
        # Work that arrives while the last workers are finishing must still be done.
        m.want(late)
    with lock:
        running[0] -= 1
    return [-10.0, -3.0]


mc = mlo.LoudnessCache(os.path.join(tmp, "m.json"))
mc.put(files[0][1], [-9.0, -1.0])
m = mlo.Measurer(mc, workers=3, batch=10, on_batch=lambda: batches.append(m.status()["measured"]), measure_fn=fake)
added = m.want(files + files[:5])
check("queued: what the cache lacks, each file once", added == 24, added)
deadline = time.time() + 30
while time.time() < deadline:
    s = m.status()
    if s["measured"] == 27 and not s["queued"] and not s["measuring"]:
        break
    time.sleep(0.05)
time.sleep(0.2)
check("every file measured once, the late ones too", sorted(seen) == sorted(os.path.basename(f) for f, _ in files[1:] + late),
      sorted(seen))
check("no more than `workers` at a time", 1 < peak_running[0] <= 3, peak_running[0])
check("the catalogue is told every `batch` results and once at the end, with everything done",
      len(batches) == 3 and batches[0] >= 10 and batches[-1] == 27, batches)
check("... and the results are on disk", len(json.load(open(os.path.join(tmp, "m.json")))) == 28)
check("nothing left running", m.running == 0 and m.status() == {"queued": 0, "measuring": 0, "measured": 27},
      (m.running, m.status()))
check("asked again for the same files: nothing to do", m.want(files) == 0 and m.running == 0)

shutil.rmtree(tmp, True)
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
