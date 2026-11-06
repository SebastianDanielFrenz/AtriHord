"""
Standalone, independent background process for converting an already
downloaded YouTube Music video into a Jellyfin-compatible tagged audio
file (audio stream copied as-is where possible, cover art embedded).

Spawned via subprocess by server.py right after a successful single-video
YouTube download whose source URL was music.youtube.com. Runs completely
decoupled from the main download pipeline - no queueing, no rate
limiting, no shared in-process locks (server.py doesn't wait on it).

A per-channel-folder lock file (flock, safe across separate processes)
guards folder creation/writes/cleanup, so two songs from the same channel
finishing around the same time can't race each other when checking
whether the destination folder is empty.

A song is stored ONCE, under its artist, however many playlists hold it
(user, 2026-09-20). A playlist folder gets a hard link to that one file: every
name plays, every name can be deleted on its own, and the audio is on disk
once. The songs index (youtube_music/.songs.json) says which file a video id
is, so server.py can hard-link a song into a new playlist without downloading
it again.

Usage:
  python3 youtube_music_extractor.py <video_path> <output_dir> <title> <artist> <video_id> [thumbnail_url] [--link <dir>] [--keep-video]
"""
import base64
import fcntl
import shutil
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recent_media_store

CODEC_EXT_MAP = {
    "aac": "m4a",
    "opus": "opus",
    "mp3": "mp3",
    "vorbis": "ogg",
    "flac": "flac",
}


SONGS_INDEX_NAME = ".songs.json"


def songs_index_path(downloads_dir):
    return os.path.join(downloads_dir, "youtube_music", SONGS_INDEX_NAME)


def read_songs_index(downloads_dir):
    """{video id: path relative to the downloads dir}. A missing or broken
    index is an empty one: it is a cache of what the tags already say."""
    try:
        with open(songs_index_path(downloads_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_song(downloads_dir, video_id, path):
    """Puts one song in the index, under its own lock (several extractions can
    run at once) and with an atomic replace, so a crash cannot leave half a
    file behind."""
    if not (downloads_dir and video_id):
        return
    index_path = songs_index_path(downloads_dir)
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    rel = os.path.relpath(path, downloads_dir).replace(os.sep, "/")
    with open(index_path + ".lock", "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            data = read_songs_index(downloads_dir)
            data[video_id] = rel
            tmp = index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=0, sort_keys=True)
            os.replace(tmp, index_path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def link_into(song_path, link_dir, video_id):
    """A hard link to the song in link_dir (a playlist folder): the same file
    under another name, so the bytes are stored once. Returns (path, created):
    the name the song has there and whether this call made it; (None, False)
    when there is nothing to do or it cannot be made.

    An existing name there is left alone unless it is this same file already
    (same inode): a different song that happens to share the title keeps its
    place, and this song takes the "<title> [id]" name instead - the rule
    resolve_final_path follows."""
    if not link_dir:
        return None, False
    try:
        os.makedirs(link_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(song_path))
        for candidate in (os.path.join(link_dir, base + ext),
                          os.path.join(link_dir, "{} [{}]{}".format(base, video_id, ext))):
            if os.path.exists(candidate):
                if os.path.samefile(candidate, song_path):
                    return candidate, False   # already linked here
                continue                      # someone else's name: try the next
            os.link(song_path, candidate)
            return candidate, True
    except OSError as exc:
        print(f"[youtube-music] Could not link into {link_dir}: {exc}", file=sys.stderr)
    return None, False


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def ffprobe_audio_codec(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "json", path],
        capture_output=True, text=True
    )
    try:
        return json.loads(result.stdout)["streams"][0]["codec_name"]
    except Exception:
        return None


def extract_audio(video_path, tmp_dir):
    """Stream-copies the source audio track whenever the codec is one
    Jellyfin (and most players) already understand natively, preserving
    the original bitrate/encoding exactly. Only re-encodes as a last
    resort for an unrecognized codec."""
    codec = ffprobe_audio_codec(video_path)
    ext = CODEC_EXT_MAP.get(codec)

    if ext:
        audio_path = os.path.join(tmp_dir, f"audio.{ext}")
        cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "copy", audio_path]
    else:
        audio_path = os.path.join(tmp_dir, "audio.m4a")
        cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-c:a", "aac", "-b:a", "256k", audio_path]

    subprocess.run(cmd, check=True, capture_output=True)
    return audio_path


def remove_source_video(video_path, final_path):
    """Deletes the downloaded video now that its audio has been extracted -
    the tagged audio file is the artifact worth keeping, and the video is
    several times its size.

    Deliberately paranoid, because this is the one irreversible step here:
    the replacement must exist, be non-empty, and probe as a real audio
    stream first, so a failed or half-written conversion can never cost the
    source. Re-downloading is not a fallback either - the video's id stays
    in yt-dlp's --download-archive, so it would simply be skipped."""
    try:
        if not os.path.isfile(final_path) or os.path.getsize(final_path) == 0:
            print(f"[youtube-music] Keeping source: no usable audio at {final_path}", file=sys.stderr)
            return
        if not ffprobe_audio_codec(final_path):
            print(f"[youtube-music] Keeping source: {final_path} has no readable audio stream",
                  file=sys.stderr)
            return
        os.remove(video_path)
        print(f"[youtube-music] Removed source video: {video_path}")
    except OSError as e:
        print(f"[youtube-music] Could not remove source video {video_path}: {e}", file=sys.stderr)


# Where a "yt-dual/" download keeps its videos (server.py, user 2026-09-23:
# "download a playlist as both music and videos"): one folder per artist
# beside the song store, the video id in every name, so the music catalogue
# matches each video to its song by id. Not under youtube/, where the video
# was downloaded: a later plain music download of the same playlist looks
# there for videos to convert - and deletes each one it converts.
MUSIC_VIDEOS_NAME = "music_videos"


def keep_source_video(video_path, downloads_dir, artist):
    """Moves the downloaded video into music_videos/<artist>/ instead of
    deleting it. Returns the new path, or None when it stayed where it was
    (never deleted either way)."""
    if not downloads_dir:
        print(f"[youtube-music] Keeping video in place (no downloads folder above it): {video_path}",
              file=sys.stderr)
        return None
    artist = re.sub(r"\s*-\s*Topic$", "", (artist or "").strip(), flags=re.IGNORECASE).strip()
    folder = os.path.join(downloads_dir, MUSIC_VIDEOS_NAME, sanitize_filename(artist) or "Unknown Artist")
    dest = os.path.join(folder, os.path.basename(video_path))
    try:
        os.makedirs(folder, exist_ok=True)
        shutil.move(video_path, dest)
        print(f"[youtube-music] Kept video: {dest}")
        return dest
    except OSError as e:
        print(f"[youtube-music] Could not move video {video_path}: {e} - left where it is", file=sys.stderr)
        return None


def image_dimensions(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", path],
        capture_output=True, text=True
    )
    try:
        stream = json.loads(result.stdout)["streams"][0]
        return stream.get("width", 0), stream.get("height", 0)
    except Exception:
        return 0, 0


# YouTube's CDN serves a ~120x90 grey placeholder with HTTP 200 (not a 404)
# whenever a requested thumbnail resolution variant doesn't exist for a
# given video - anything that small is that placeholder, not a real image.
MIN_VALID_THUMB_DIMENSION = 200


def _try_candidate(path):
    width, height = image_dimensions(path)
    if os.path.getsize(path) > 0 and width >= MIN_VALID_THUMB_DIMENSION and height >= MIN_VALID_THUMB_DIMENSION:
        return width * height
    return 0


def get_thumbnail(video_path, thumbnail_url, video_id, tmp_dir):
    # Every possible source ends up with a different resolution depending on
    # the video (maxresdefault isn't generated for every upload, the
    # flat-playlist metadata thumbnail may be small, and a 10s frame grab is
    # only possible when there's an actual video track) - so rather than
    # guessing a priority order, fetch/generate whichever are available and
    # just keep the biggest one.
    candidates = []
    if video_id:
        candidates.append(f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg")
    if thumbnail_url:
        candidates.append(thumbnail_url)

    best_path, best_area = None, 0
    for i, url in enumerate(candidates):
        path = os.path.join(tmp_dir, f"thumb_candidate_{i}.jpg")
        try:
            urllib.request.urlretrieve(url, path)
        except Exception:
            continue
        area = _try_candidate(path)
        if area > best_area:
            best_path, best_area = path, area

    frame_path = os.path.join(tmp_dir, "thumb_frame.jpg")
    cmd = ["ffmpeg", "-y", "-ss", "10", "-i", video_path, "-vframes", "1", frame_path]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode == 0 and os.path.exists(frame_path):
        area = _try_candidate(frame_path)
        if area > best_area:
            best_path, best_area = frame_path, area

    return best_path


def embed_tags(audio_path, thumb_path, title, artist, video_id=None):
    """Writes the tags Jellyfin reads, plus the source URL.

    The URL matters beyond bookkeeping: extraction deletes the source
    video, whose filename was the only place the video id appeared, while
    the id stays in yt-dlp's download archive. Without it recorded here,
    deciding later whether this track may be downloaded again means
    grepping a log file that nothing guarantees still exists. PURL is the
    field yt-dlp itself uses for this."""
    ext = os.path.splitext(audio_path)[1].lower()
    purl = f"https://www.youtube.com/watch?v={video_id}" if video_id else None

    if ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(audio_path)
        audio["\xa9nam"] = [title]
        audio["\xa9ART"] = [artist]
        if purl:
            audio["\xa9cmt"] = [purl]
        if thumb_path:
            with open(thumb_path, "rb") as f:
                audio["covr"] = [MP4Cover(f.read(), imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()

    elif ext in (".opus", ".ogg"):
        from mutagen.flac import Picture
        if ext == ".opus":
            from mutagen.oggopus import OggOpus
            audio = OggOpus(audio_path)
        else:
            from mutagen.oggvorbis import OggVorbis
            audio = OggVorbis(audio_path)
        audio["title"] = [title]
        audio["artist"] = [artist]
        if purl:
            audio["purl"] = [purl]
        if thumb_path:
            pic = Picture()
            with open(thumb_path, "rb") as f:
                pic.data = f.read()
            pic.type = 3
            pic.mime = "image/jpeg"
            audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        audio.save()

    elif ext == ".mp3":
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TIT2, TPE1, APIC, WOAS
        audio = MP3(audio_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        if purl:
            audio.tags["WOAS"] = WOAS(url=purl)
        if thumb_path:
            with open(thumb_path, "rb") as f:
                audio.tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=f.read())
        audio.save()

    elif ext == ".flac":
        from mutagen.flac import FLAC, Picture
        audio = FLAC(audio_path)
        audio["title"] = [title]
        audio["artist"] = [artist]
        if purl:
            audio["purl"] = [purl]
        if thumb_path:
            pic = Picture()
            with open(thumb_path, "rb") as f:
                pic.data = f.read()
            pic.type = 3
            pic.mime = "image/jpeg"
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()


def existing_video_id(path):
    """The video id already written into a file's tags, or None.

    Only used to tell "this is the same track again" from "this is a
    different song that happens to share a title"."""
    try:
        import mutagen
        audio = mutagen.File(path)
        if not audio:
            return None
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
            match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", text)
            if match:
                return match.group(1)
    except Exception:
        pass
    return None


def resolve_final_path(output_dir, title, ext, video_id):
    """Where this track's audio goes, without ever landing on top of a
    different song.

    Two tracks in one playlist really can share a title - a remix and its
    original, a cover, or just two songs called "Alarm" - and that got much
    more likely when a playlist started filing under <Owner>/<Playlist>/
    instead of splitting by uploader. shutil.move overwrites silently, and
    the loss would be permanent in the worst way: the overwritten track's id
    stays in yt-dlp's download archive, so it is never fetched again. There
    would be nothing left to notice, either - just one fewer file.

    The same video arriving again is a different case and must still replace
    itself, or every re-run would litter copies. Runs under the caller's
    flock, so the existence check cannot race another extractor."""
    base = sanitize_filename(title)
    path = os.path.join(output_dir, base + ext)
    if not os.path.exists(path):
        return path
    if existing_video_id(path) == video_id:
        return path
    return os.path.join(output_dir, "{} [{}]{}".format(base, video_id, ext))


def derive_app_dirs(output_dir):
    """Finds (downloads_dir, base_dir) by walking up from output_dir to the
    "downloads" component - the app root is two levels above that
    (<base>/web_files/downloads). Returns (None, None) if not found, which
    just disables recent-media recording rather than failing the whole job."""
    cur = os.path.abspath(output_dir)
    while cur != os.path.dirname(cur):
        if os.path.basename(cur) == "downloads":
            return cur, os.path.dirname(os.path.dirname(cur))
        cur = os.path.dirname(cur)
    return None, None


def main():
    argv = list(sys.argv[1:])
    link_dir = None
    # --keep-video: a "yt-dual/" download - the video is kept, not deleted.
    keep_video = "--keep-video" in argv
    if keep_video:
        argv.remove("--keep-video")
    if "--link" in argv:
        i = argv.index("--link")
        link_dir = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
    if len(argv) < 5:
        print("[youtube-music] Usage: youtube_music_extractor.py <video_path> <output_dir> <title> <artist> <video_id> [thumbnail_url] [--link <dir>]", file=sys.stderr)
        sys.exit(1)

    video_path, output_dir, title, artist, video_id = argv[:5]
    thumbnail_url = argv[5] if len(argv) > 5 else None

    # output_dir nests to a variable depth - youtube_music/<artist> for a
    # single video, youtube_music/<artist>/<playlist> for a playlist track -
    # so walk up to the "downloads" component rather than assuming a fixed
    # number of levels.
    downloads_dir, base_dir = derive_app_dirs(output_dir)

    parent_dir = os.path.dirname(output_dir)
    os.makedirs(parent_dir, exist_ok=True)
    lock_path = os.path.join(parent_dir, os.path.basename(output_dir) + ".lock")

    # Never delete this lock file - it's a permanent per-channel mutex.
    # Deleting it while another process holds/awaits a flock on the same
    # inode would let a third process create a *new* file at the same
    # path and lock that instead, defeating the whole point.
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if not os.path.exists(video_path):
                print(f"[youtube-music] Source video not found: {video_path}", file=sys.stderr)
                return

            # Kept as both, and the song is already here: only the video was
            # missing (a playlist downloaded as music before). Nothing to
            # extract - the song keeps its file, its tags and its links, and
            # gets this playlist's name for it now rather than at the end of
            # the run (server.py link_stored_songs would, too).
            if keep_video and downloads_dir and video_id:
                known = read_songs_index(downloads_dir).get(video_id)
                if known and os.path.isfile(os.path.join(downloads_dir, known)):
                    print(f"[youtube-music] Song already stored ({known}) - keeping only the video")
                    linked, made = link_into(os.path.join(downloads_dir, known), link_dir, video_id)
                    if made:
                        print(f"[youtube-music] Linked: {linked}")
                    kept = keep_source_video(video_path, downloads_dir, artist)
                    if kept and base_dir:
                        try:
                            rel = os.path.relpath(kept, downloads_dir).replace(os.sep, "/")
                            recent_media_store.record_recent_media(base_dir, [rel])
                        except Exception as e:
                            print(f"[youtube-music] Failed to record recent media: {e}", file=sys.stderr)
                    return

            os.makedirs(output_dir, exist_ok=True)

            # Keep the temp workspace on the same filesystem as the final
            # destination so the finishing move is a cheap same-fs rename
            # instead of a cross-device copy (shutil.move below still
            # handles it safely either way, this is just for efficiency).
            with tempfile.TemporaryDirectory(dir=output_dir) as tmp_dir:
                try:
                    audio_path = extract_audio(video_path, tmp_dir)
                    thumb_path = get_thumbnail(video_path, thumbnail_url, video_id, tmp_dir)
                    embed_tags(audio_path, thumb_path, title, artist, video_id)

                    final_path = resolve_final_path(
                        output_dir, title, os.path.splitext(audio_path)[1], video_id)
                    # A song that already carries playlist names is replaced
                    # IN PLACE: moving a new file over it would take the name
                    # but leave every playlist pointing at the old file - two
                    # files on disk again, and the playlists stuck with the
                    # audio from last time.
                    if os.path.exists(final_path) and os.stat(final_path).st_nlink > 1:
                        with open(audio_path, "rb") as src, open(final_path, "r+b") as dst:
                            shutil.copyfileobj(src, dst)
                            dst.truncate()
                        os.remove(audio_path)
                    else:
                        # shutil.move (not os.replace) - the temp dir and the
                        # final destination can be on different filesystems
                        # (e.g. /tmp vs. a mounted downloads drive), where a
                        # plain rename() fails with "Invalid cross-device link"
                        shutil.move(audio_path, final_path)
                    print(f"[youtube-music] Saved: {final_path}")

                    # The song is stored once; a playlist that holds it gets
                    # another name for the same file.
                    linked, made = link_into(final_path, link_dir, video_id)
                    if made:
                        print(f"[youtube-music] Linked: {linked}")
                    if downloads_dir:
                        record_song(downloads_dir, video_id, final_path)

                    # This file is produced outside the main pipeline, so
                    # server.py's own post-task scan never sees it - register
                    # it in the recent-media list from here instead.
                    try:
                        if downloads_dir and base_dir:
                            rel_path = os.path.relpath(linked or final_path, downloads_dir).replace(os.sep, "/")
                            recent_media_store.record_recent_media(base_dir, [rel_path])
                    except Exception as e:
                        print(f"[youtube-music] Failed to record recent media: {e}", file=sys.stderr)

                    # Last, so nothing that still needs the video runs
                    # after it: the audio is in place and registered by
                    # this point. Kept rather than deleted for a "yt-dual/"
                    # download.
                    if keep_video:
                        kept = keep_source_video(video_path, downloads_dir, artist)
                        if kept and downloads_dir and base_dir:
                            try:
                                rel = os.path.relpath(kept, downloads_dir).replace(os.sep, "/")
                                recent_media_store.record_recent_media(base_dir, [rel])
                            except Exception as e:
                                print(f"[youtube-music] Failed to record recent media: {e}", file=sys.stderr)
                    else:
                        remove_source_video(video_path, final_path)
                except Exception as e:
                    print(f"[youtube-music] Failed to process {video_path}: {e}", file=sys.stderr)
        finally:
            # Clean up the channel folder if this attempt left it empty
            # (e.g. it failed before producing a file). Safe under the
            # flock: a concurrent job for the same channel is either
            # waiting for this lock (so hasn't created anything yet) or
            # already finished (so its file is already there and this
            # check won't fire).
            try:
                if os.path.isdir(output_dir) and not os.listdir(output_dir):
                    os.rmdir(output_dir)
            except OSError:
                pass
            fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
