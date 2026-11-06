"""Converts one just-downloaded YouTube Music video, the moment it lands.

Wired in as a yt-dlp --exec hook for music playlists, so a 1500-track playlist
produces audio as it goes instead of nothing for eleven hours and then
everything at once. Before this, extraction ran after proc.wait() - i.e. after
the WHOLE playlist - which also meant a run that was cancelled or died partway
left every video unconverted.

Where the song goes is what the end-of-run path (server.py
_music_extraction_cmd) does: stored once under youtube_music/<Artist>/, and
hard-linked into the playlist's folder. The playlist folder is the download
folder with youtube/ swapped for youtube_music/ (<Owner>/<Playlist>/) -
derived from the path rather than re-decided here, because two places
computing the same destination is two places to disagree. Before 2026-09-23
this hook wrote the song INTO the playlist folder, the layout from before the
song store (KSF-044), and nothing had run through it since.

Usage (from --exec, all fields quoted by yt-dlp's %(...)q):
  youtube_music_hook.py <filepath> <id> <title> <uploader> [thumbnail] [--keep-video]

--keep-video: a "yt-dual/" download (server.py) - the extractor keeps the
video in music_videos/ instead of deleting it.
"""
import os
import re
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRACTOR = os.path.join(BASE_DIR, "youtube_music_extractor.py")

SEP = os.sep
MARKER = SEP + "youtube" + SEP


def music_dir_for(video_path):
    """The youtube_music/ folder mirroring this video's youtube/ folder, or
    None if the file is not under a youtube/ tree at all."""
    parent = os.path.dirname(os.path.abspath(video_path))
    idx = parent.rfind(MARKER)
    if idx == -1:
        return None
    return parent[:idx] + SEP + "youtube_music" + SEP + parent[idx + len(MARKER):]


def clean_artist_name(name):
    """server.py's rule: "<Artist> - Topic" is YouTube Music's channel for an
    artist, not the artist's name."""
    name = name or "Unknown Artist"
    return re.sub(r"\s*-\s*Topic$", "", name, flags=re.IGNORECASE).strip() or name


def artist_dir_for(video_path, artist):
    """youtube_music/<Artist>/ beside this video's youtube/ tree - where the
    song itself is stored (server.py: sanitize_folder_name of the artist)."""
    parent = os.path.dirname(os.path.abspath(video_path))
    downloads = parent[:parent.rfind(MARKER)]
    folder = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
    return os.path.join(downloads, "youtube_music", folder)


def main():
    keep_video = "--keep-video" in sys.argv
    if keep_video:
        sys.argv.remove("--keep-video")
    if len(sys.argv) < 5:
        print("[music-hook] usage: youtube_music_hook.py <filepath> <id> "
              "<title> <uploader> [thumbnail]", file=sys.stderr)
        return 1

    video_path, video_id, title, uploader = sys.argv[1:5]
    thumbnail = sys.argv[5] if len(sys.argv) > 5 else ""

    if not os.path.isfile(video_path):
        # yt-dlp also fires --exec for a file it skipped as already present;
        # nothing to do, and not an error.
        return 0

    playlist_dir = music_dir_for(video_path)
    if not playlist_dir:
        print(f"[music-hook] not under a youtube/ folder, skipping: {video_path}",
              file=sys.stderr)
        return 0
    artist = clean_artist_name(uploader)
    output_dir = artist_dir_for(video_path, artist)

    cmd = [sys.executable, "-u", EXTRACTOR, video_path, output_dir,
           title or "Unknown Title", artist, video_id or ""]
    if thumbnail and thumbnail != "NA":
        cmd.append(thumbnail)
    if os.path.normpath(playlist_dir) != os.path.normpath(output_dir):
        cmd += ["--link", playlist_dir]
    if keep_video:
        cmd.append("--keep-video")

    # Inherited stdout/stderr on purpose: yt-dlp is being read line by line by
    # the task's log reader, so whatever the extractor says shows up in the
    # task log where someone is already looking.
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
