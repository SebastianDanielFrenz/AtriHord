# AtriHord

A self-hosted media hub: download from many sites (YouTube, Pixiv, Twitter/X, Bluesky, DeviantArt, Patreon,
Civitai, Bilibili, Instagram, ...), and browse the result as a photo/video/music library with timeline, map,
people, search, shares, trash, upload, a VR viewer, 2D→3D conversion and smooth-motion video. An optional GPU
**vision worker** tags, captions, OCRs, face-clusters and upscales the library.

VR in Chrome on a PC with Steam Frame controllers needs a SteamVR binding, installed with one command:
see [`vr-bindings/README.md`](vr-bindings/README.md).

> Licence GPL-3.0-or-later. Read [`THIRD_PARTY.md`](THIRD_PARTY.md) first: some optional models are
> **non-commercial only**, and smooth motion needs a proprietary DLL from Lossless Scaling you must obtain yourself.

## What you need

* Linux (Debian/Ubuntu tested; WSL2 works), Python 3.10+, `ffmpeg`, `git`.
* For YouTube: [deno](https://deno.com) on `PATH` (yt-dlp's challenge solver).
* A reverse proxy for sign-in when anyone but you can reach it (below), or `MEDIA_SINGLE_USER` for a private trial.
* Optional: a machine with an NVIDIA GPU for the vision worker.

## Quick start (private machine, no login)

```bash
git clone <this repository> atrihord && cd atrihord
sudo apt install -y python3-venv ffmpeg git
python3 -m venv venv
venv/bin/pip install -r requirements.txt
MEDIA_SINGLE_USER=me venv/bin/python server.py
```

Open <http://127.0.0.1:8000/>. The server binds to localhost only unless you set `MEDIA_BIND`.
Downloaded files go to `web_files/downloads/`; more libraries can be added as "roots" in the settings page.
`MEDIA_SINGLE_USER` treats **every** request as an admin: never use it on a machine others can reach.

Pixiv additionally needs [PixivUtil2](https://github.com/Nandaka/PixivUtil2) in its own virtualenv:

```bash
git clone https://github.com/Nandaka/PixivUtil2 PixivUtil2
python3 -m venv PixivUtil2/pixiv_venv && PixivUtil2/pixiv_venv/bin/pip install -r PixivUtil2/requirements.txt
```

Site logins/cookies (Twitter, YouTube, Pixiv) go in the files the settings page names (`cookies_*.txt`,
PixivUtil2's `config.ini`); they are git-ignored and never leave your machine.

## Running it for real: service + sign-in

AtriHord has **no login of its own**. It trusts the `Remote-User` / `Remote-Groups` headers of a reverse proxy
(only from `MEDIA_PROXY_IPS`), the way [Authelia](https://www.authelia.com) forward auth sets them. Groups map to
roles: `MEDIA_ADMIN_GROUPS` = everything, `MEDIA_GUEST_GROUPS` = browse and watch only. Scripts and phones can use
a per-user API token (`X-API-Key`; create it with `api_token.py`).

1. Copy `deploy/atrihord.env.example` to `atrihord.env` and edit it.
2. Install `deploy/atrihord.service` (adjust paths/user) and `systemctl enable --now atrihord`.
3. Put Caddy in front: `deploy/Caddyfile.example` (forward auth to Authelia, public share links let through) and
   the Authelia rule in `deploy/authelia-access-control.example.yml`.

Anything that can reach port 8000 directly can forge the headers if its address is in `MEDIA_PROXY_IPS`: keep the
server on `127.0.0.1` (or a private network) and list only the proxy.

## Vision worker (GPU)

```bash
python3 -m venv vision-venv
vision-venv/bin/pip install torch            # pick the build for your GPU: https://pytorch.org
vision-venv/bin/pip install -r requirements-vision.txt
vision-venv/bin/python vision/vision_worker.py
```

It listens on 127.0.0.1:8410. To serve another machine set `KSF_VISION_HOST=0.0.0.0` **and** a `token` in its config (the same token in the server's settings); the worker refuses to start exposed without one. Tell the server where it is (`MEDIA_VISION_WORKER=http://gpu-host:8410` or the settings
page). Models download on first use (WD EVA02 tagger, Florence-2, Real-ESRGAN). The face pass needs `insightface`
and is **non-commercial**. Work runs in a configurable night-time window; `urgent` requests get through anyway.
Smooth motion: `vision/vidinterp/install.sh` and the DLL recipe in `THIRD_PARTY.md`.

## Companion app: Spatial Bridge (optional, Windows)

Lets the music page play through Windows Spatial Sound (Dolby Atmos, DTS:X, height speakers). It is a separate
project with its own repository and licence (MIT): [AtriBridge](https://github.com/SebastianDanielFrenz/AtriBridge).
AtriHord only talks to it on localhost from the music page, and can host its signed updates. The same repository
ships a Chrome / Edge extension that upmixes other tabs (YouTube, YouTube Music) through the bridge; the music page
and the extension share the bridge, and whichever page the user last pressed play on holds it while the other is paused.

By default this server serves the newest GitHub release of the bridge itself: the music page's "Spatial app" link
(`/app/ksf-spatial-bridge.exe`, signed-in users, saved as `ksf-spatial-bridge-<version>.exe`) and the update files for
older installed copies (served without login at `/api/public/spatial-bridge/`, bridges up to 1.2.5 update from there;
1.2.6 and later read GitHub directly). The server looks at the project's `releases/latest/download/latest.json` at most
every ten minutes, downloads the exe when the version changed, checks its size and SHA-256 against that manifest and
keeps both in `web_files/app/spatial-bridge-cache/`; if GitHub is unreachable it serves the last cached copy. Nothing has
to be copied after a release. `MEDIA_BRIDGE_GITHUB_REPO` (default `SebastianDanielFrenz/AtriBridge`) names another
repository.

To offer your own build instead, set `MEDIA_BRIDGE_SOURCE=local`: the server then serves the signed `latest.json` and
`ksf-spatial-bridge.exe` you put in `web_files/app/spatial-bridge/` (not part of this repository; copy them again after
each release; sign with the project's `tools/publish.py`). Installed copies then update from
`--update-url https://your.server/api/public/spatial-bridge/latest.json`, and need
`--origin https://your.server` (or `origin=` in `spatial-bridge.conf`) to accept connections from your music page:
the built-in allow-list only covers media-dl.ksfhost.de, music.ksfhost.de and localhost.
Without an exe from the chosen source the link redirects to `MEDIA_BRIDGE_DOWNLOAD_URL` if you set one, else the music page hides it.

## Styling

The look comes from `web_files/css/atrihord.css` (design tokens and `.ah-*` components) and `js/atrihord-user.js`
(the identity/theme menu). They are this project's own files and need no external host. They descend from a
private design system but are no longer synced with it: change them here.

## Configuration

Environment (see `deploy/atrihord.env.example`): `MEDIA_BIND`, `MEDIA_PORT`, `MEDIA_PROXY_IPS`,
`MEDIA_ADMIN_GROUPS`, `MEDIA_GUEST_GROUPS`, `MEDIA_SINGLE_USER`, `MEDIA_AUTH_URL`, `MEDIA_PUBLIC_URL`,
`MEDIA_VISION_WORKER`, `MEDIA_LIBRARY_DIR`, `MEDIA_CACHE_DIR`, `MEDIA_PREVIEW_DIR`, `MEDIA_3D_CACHE` (stored media, big rebuildable media, thumbnails; the interpolation cache is set on the vision worker with `interp_cache_dir`). Everything else is in the settings page and stored in `config.json`,
`roots.json`, `users.json` (all git-ignored).

## Layout

`server.py` (Flask app) and its modules (`media_*`, `music_*`, `song_*`, ...), `web_files/` (HTML/JS/CSS; the
current UI is `media-next.html`), `vision/` (GPU worker, upscaler, 3D, `vidinterp` smooth motion),
`pixiv_follow_sync.py` (daemon that keeps followed Pixiv artists up to date), `tests/server_checks/`.
`WHAT_BELONGS_WHERE.md` explains how the review/categorisation rules treat content.

## Contributing / security

Issues and pull requests welcome once the project is public. Report security problems privately to the maintainer.
