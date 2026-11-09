"""One song, one file: gives every song in youtube_music its name under its
artist, turns the playlist copies into names for that same file, and builds the
songs index the downloader uses to fill a playlist without downloading again
(KSF-044).

Nothing is deleted: a copy that really is a second file is moved to the trash
(<root>/.trash/<batch>/), where the media page can restore it, and a hard link
to the kept song takes its place. Hard links mean no file ever moves, so
shares, favorites and the AI index keep working.

  venv/bin/python music_dedup.py [--apply] [--downloads DIR] [--limit N]

Without --apply it only prints the plan.
"""
import argparse
import json
import os
import re
import sys
import uuid
import time

sys.path.insert(0, "/data2/ksf_web_dl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

AUDIO_EXTS = (".opus", ".m4a", ".mp3", ".ogg", ".flac", ".mka", ".aac", ".wav")
ID_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
NAME_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
TRASH_DIR_NAME = ".trash"


def sanitize(name):
    """As server.sanitize_folder_name / extractor.sanitize_filename: the same
    names the downloader would write."""
    return re.sub(r'[\\/*?:"<>|]', "", name or "").strip()


def tags_of(path):
    """(video id, artist, title) from a file's own tags, each possibly None."""
    vid = artist = title = None
    try:
        import mutagen
        audio = mutagen.File(path)
        if audio is not None:
            tags = audio.tags or {}
            for key in ("purl", "PURL", "comment", "\xa9cmt", "WOAS", "description"):
                try:
                    value = tags.get(key)
                except Exception:
                    continue
                if not value:
                    continue
                item = value[0] if isinstance(value, list) else value
                text = getattr(item, "url", None) or str(item)
                m = ID_RE.search(text)
                if m:
                    vid = m.group(1)
                    break
        easy = mutagen.File(path, easy=True)
        if easy is not None and easy.tags:
            artist = (easy.tags.get("artist") or [None])[0]
            title = (easy.tags.get("title") or [None])[0]
    except Exception:
        pass
    if not vid:
        m = NAME_ID_RE.search(os.path.basename(path))       # "<title> [id].ext"
        if m:
            vid = m.group(1)
    return vid, artist, title


def scan(music_dir):
    songs = {}          # video id -> [(path, stat)]
    skipped = []
    for base, dirs, names in os.walk(music_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            if not n.lower().endswith(AUDIO_EXTS):
                continue
            path = os.path.join(base, n)
            try:
                st = os.stat(path)
            except OSError:
                continue
            vid, artist, title = tags_of(path)
            if not vid:
                skipped.append((path, "no video id in its tags or name"))
                continue
            songs.setdefault(vid, []).append((path, st, artist, title))
    return songs, skipped


def plan(music_dir, songs):
    """[(video id, kept path, canonical path, [copies to replace])], notes."""
    out, notes = [], []
    taken = {}          # canonical path -> video id, so two songs never collide
    for vid, entries in sorted(songs.items()):
        artist = next((a for _p, _s, a, _t in entries if a), None)
        title = next((t for _p, _s, _a, t in entries if t), None)
        if not artist or not title:
            notes.append((entries[0][0], "no artist/title tag: left where it is"))
            continue
        ext = os.path.splitext(entries[0][0])[1]
        artist_dir = os.path.join(music_dir, sanitize(artist))
        canonical = os.path.join(artist_dir, sanitize(title) + ext)
        if taken.get(canonical, vid) != vid or (
                os.path.exists(canonical) and not any(os.path.samefile(canonical, p) for p, _s, _a, _t in entries
                                                      if os.path.exists(p))):
            canonical = os.path.join(artist_dir, f"{sanitize(title)} [{vid}]{ext}")
        taken[canonical] = vid
        # Keep the copy that is already the canonical one, else the biggest.
        kept = next((p for p, _s, _a, _t in entries if os.path.abspath(p) == canonical), None)
        if not kept:
            kept = max(entries, key=lambda e: e[1].st_size)[0]
        kept_ino = os.stat(kept).st_ino
        copies = [p for p, st, _a, _t in entries if st.st_ino != kept_ino]
        out.append((vid, kept, canonical, copies))
    return out, notes


def apply_plan(music_dir, downloads, steps, batch_dir, limit=None):
    made = replaced = 0
    freed = 0
    manifest = []
    index = {}
    for vid, kept, canonical, copies in steps:
        if limit and made + replaced >= limit:
            break
        # 1. the song's name under its artist
        if not os.path.exists(canonical):
            os.makedirs(os.path.dirname(canonical), exist_ok=True)
            try:
                os.link(kept, canonical)
                made += 1
            except OSError as exc:
                print(f"  ! could not link {canonical}: {exc}")
                continue
        song = canonical if os.path.exists(canonical) else kept
        index[vid] = os.path.relpath(song, downloads).replace(os.sep, "/")
        # 2. a copy that is a second file becomes a name for the kept one
        for copy in copies:
            if not os.path.exists(copy) or os.path.samefile(copy, song):
                continue
            size = os.stat(copy).st_size
            rel = os.path.relpath(copy, downloads)
            grave = os.path.join(batch_dir, rel)
            os.makedirs(os.path.dirname(grave), exist_ok=True)
            os.rename(copy, grave)                  # to the trash, never deleted
            try:
                # Same stem, the kept song's extension: a name must say what
                # format is inside (see fix_link_extensions.py).
                stem, ext = os.path.splitext(copy)[0], os.path.splitext(song)[1]
                right, n = stem + ext, 2
                while os.path.lexists(right) and not os.path.samefile(right, song):
                    right, n = f"{stem} ({n}){ext}", n + 1
                if not os.path.lexists(right):
                    os.link(song, right)
            except OSError as exc:                  # put it back rather than lose a name
                os.rename(grave, copy)
                print(f"  ! could not link over {copy}: {exc}")
                continue
            manifest.append({"path": rel.replace(os.sep, "/"), "size": size,
                             "why": f"another copy of {vid}, now a link to {index[vid]}"})
            replaced += 1
            freed += size
    return made, replaced, freed, manifest, index


def backfill_inodes(downloads, base_dir="/data2/ksf_web_dl"):
    """Tells the AI index which FILE each of its music rows is about, before
    the new names appear. Without it the index would take a song's artist name
    for a new file and analyse the song a second time."""
    import media_ai
    conn = media_ai.connect(base_dir)
    roots = {"downloads": downloads}
    try:
        with open(os.path.join(base_dir, "roots.json"), encoding="utf-8") as fh:
            for r in json.load(fh):
                if r.get("id") and r.get("path"):
                    roots[r["id"]] = r["path"]
    except (OSError, ValueError):
        pass
    done = 0
    rows = conn.execute("SELECT id, root, path FROM media WHERE kind = 'audio' AND ino IS NULL").fetchall()
    for row in rows:
        base = roots.get(row["root"])
        if not base:
            continue
        try:
            st = os.stat(os.path.join(base, row["path"]))
        except OSError:
            continue
        conn.execute("UPDATE media SET ino = ? WHERE id = ?", (f"{st.st_dev}:{st.st_ino}", row["id"]))
        done += 1
    conn.commit()
    return done, len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--downloads", default="/data2/ksf_web_dl/web_files/downloads")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, help="stop after this many changes (a trial run)")
    args = ap.parse_args()

    downloads = os.path.abspath(args.downloads)
    music_dir = os.path.join(downloads, "youtube_music")
    songs, skipped = scan(music_dir)
    steps, notes = plan(music_dir, songs)
    names_needed = [s for s in steps if not os.path.exists(s[2])]
    copies = [(vid, c) for vid, _k, _c, cs in steps for c in cs]
    print(f"{sum(len(v) for v in songs.values())} files, {len(songs)} songs")
    print(f"  {len(names_needed)} songs need their name under their artist")
    print(f"  {len(copies)} copies are a second file and would become links")
    print(f"  {len(skipped)} files carry no video id, {len(notes)} no artist/title: left alone")
    for vid, c in copies[:10]:
        print(f"      copy: {os.path.relpath(c, downloads)}")
    for p, why in (skipped[:5] + notes[:5]):
        print(f"      left: {os.path.relpath(p, downloads)} ({why})")
    if not args.apply:
        print("\n(dry run - nothing changed; pass --apply to do it)")
        return 0

    filled, seen = backfill_inodes(downloads)
    print(f"AI index: {filled} of {seen} music rows now say which file they are about")
    # The trash page's restore and empty both refuse a batch that is not
    # <date>-<time>-<6 hex>, so a home-made name could not be emptied there.
    batch = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    batch_dir = os.path.join(downloads, TRASH_DIR_NAME, batch)
    made, replaced, freed, manifest, index = apply_plan(music_dir, downloads, steps, batch_dir, args.limit)
    if manifest:
        os.makedirs(batch_dir, exist_ok=True)
        with open(os.path.join(batch_dir, "_manifest.json"), "w", encoding="utf-8") as fh:
            # The app's own shape (items are plain paths, deleted_at dates the
            # batch); the detail per file goes under notes.
            json.dump({"batch": batch, "mode": "release", "deleted_at": time.time(),
                       "deleted_by": "media-dl song migration",
                       "items": [i["path"] for i in manifest],
                       "reason": "duplicate song replaced by a link", "notes": manifest},
                      fh, ensure_ascii=False, indent=1)
    # The index the downloader reads to fill a playlist without downloading.
    import youtube_music_extractor as music_store
    for vid, rel in index.items():
        music_store.record_song(downloads, vid, os.path.join(downloads, rel))
    print(f"\n{made} artist names made, {replaced} copies replaced by links, "
          f"{freed / 1e9:.2f} GB now in the trash ({batch})")
    print(f"songs index: {len(index)} songs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
