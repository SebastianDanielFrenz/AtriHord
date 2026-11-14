# Optional modules: design (approved 2026-11-14, built)

Owner, 2026-11-14: *"If the feature could be option during the install, that would be great. There should then be
an option in the settings where additional modules can be installed, source is the online repo."*

The first module is the music visualizer. It already works as a folder, `web_files/js/modules/visualizer/`: its
manifest `module.js` is loaded by `music.js`, and if the manifest is missing the page shows no Visualizer entry.
This design turns that into a system.

## What a module is

- **Client-only for now**: browser files (`.js` only, because guests may load nothing else from `web_files`).
  A module never runs code on the server. Server-side modules would be a different and much riskier thing, and are
  out of scope.
- **Repo source** in `modules/<id>/`: the module's files plus `module.json`, which the server and CLI read
  (`id`, `name`, `version`, `description`, `install_default` and the file list). The client manifest `module.js` stays as it
  is, so the music page needs no server support.
- **Installed** means present in `web_files/js/modules/<id>/`. That folder is **git-ignored**, so `git pull` never
  installs, removes or overwrites a module. Install, update and remove are deliberate acts.

## One engine, two front ends

`module_manager.py` holds the logic. Both front ends use it, so they cannot drift apart:

| Front end | Use |
|---|---|
| `python3 module_manager.py list / install <id> / update <id> / remove <id> [--from-repo DIR]` | the installer, scripts, SSH |
| Settings → Server → **Modules** (admin only) | the same actions from the browser |

Operations:
- **list**: the installed modules (from their `module.json`) plus the ones available online (the online index,
  cached for 10 minutes like the bridge's), with version, size and description, and an "update available" mark.
- **install / update**: fetch the index, check it, download each file, check each file's SHA-256 against the index,
  write the module to a temp folder, then swap it in with one `os.replace`. The old version is kept as
  `<id>.previous` until the next update, so a bad update can be undone by hand.
- **remove**: move the folder to `web_files/js/modules/.removed/<id>-<date>` (cheap undo; the next install clears it).

## The online source

The repo on GitHub, branch `atrihord-public`: `modules/index.json` lists every module, its version and each file's
SHA-256. Files are read from `raw.githubusercontent.com/<repo>/<branch>/modules/<id>/<file>`. Repo and branch are
set by `MEDIA_MODULES_REPO` / `MEDIA_MODULES_BRANCH`, so forks can point at their own.

A publish script, `tools/publish_modules.py`, recomputes the hashes, bumps nothing by itself (versions are set by
hand in `module.json`), writes `modules/index.json` and signs it (see question 1).

If GitHub cannot be reached, the page says so and lists the installed modules only. Nothing breaks.

## The installer

Today installing means following the steps in the README. Proposed `install.sh`:
- runs the README's quick-start steps (venv, pip install);
- then asks **"Optional modules"** as a checklist, with each module's name and description from `modules/*/module.json`;
- installs the picked ones from the **local checkout**, with no network needed.

For non-interactive use: `--modules visualizer,…`, `--modules all` or `--modules none`. Re-running it is safe.

## Endpoints (server.py, gated deploy)

| Endpoint | Who | Does |
|---|---|---|
| `GET /api/modules` | admin | installed + available + last error |
| `POST /api/modules/<id>/install` | admin | install or update from the online index |
| `POST /api/modules/<id>/remove` | admin | remove (move aside) |

Every write takes a lock. Ids must match `^[a-z0-9-]{1,32}$`, and file paths in an index must be plain relative
`.js` paths: no `..`, no absolute paths, at most 64 files and 8 MB per module. These checks go on top of the
signature, not instead of it.

## Migration

Prod already has `web_files/js/modules/visualizer/` by hand. It shows up as installed with no further step. The
repo gains `modules/visualizer/` (the current files + `module.json`) and the `.gitignore` line.

## Decisions (owner, 2026-11-14: all four as recommended)

1. **Integrity of what is downloaded.** Modules are JavaScript that runs in admins' browsers with their session, so a
   tampered module can do anything an admin can.
   - (a) **Signed index**: Ed25519 signature over `index.json`, with the public key in `module_manager.py` and the
     private key only on the maintainer's machine, like the bridge's update key. Pro: a hijacked GitHub account or
     branch cannot push code. Contra: one key to keep, one more step when publishing, and the `cryptography`
     package as a new dependency.
   - (b) **Hashes over HTTPS only.** Pro: simplest. Contra: whoever can push to the branch can ship code to every
     install that presses Update.
   - Recommendation: (a), consistent with the bridge. **Chosen.** Built without `cryptography`: `atri_ed25519.py` is a
     pure-Python RFC 8032 verifier (checked against the RFC vectors, ~3 ms per verify), so no install needs a new
     package. Secret key: `S:/claude_code/atrihord_module_keys/modules_ed25519.key` (never in the repo).
2. **Where the online files come from.**
   - (a) **Branch files** (`raw.githubusercontent`). Pro: publishing is commit + script. Contra: a branch moves; the
     per-file hashes catch a half-moved state, and the install is then retried once.
   - (b) **GitHub release assets** (a zip per module). Pro: immutable. Contra: a release for every module change.
   - Recommendation: (a). **Chosen.** `MEDIA_MODULES_URL` can point at a mirror (the signature is what is trusted).
3. **Installer form.**
   - (a) `install.sh` with the checklist plus the CLI.
   - (b) The CLI only, with a README step `python3 module_manager.py install visualizer`.
   - Recommendation: (a). It is what "an option during the install" asks for. **Chosen.**
4. **Default in the installer.** Visualizer pre-ticked? Recommendation: yes. It is small (~330 KB), client-only and
   costs nothing until it is opened. **Chosen** (`install_default` in module.json).

## Phases

| Phase | Content | Deploy |
|---|---|---|
| M1 | `modules/visualizer/` + `module.json`, `.gitignore`, `module_manager.py` (CLI, local install), `install.sh` with the checklist | repo only |
| M2 | online index + publish script (+ signing), CLI install from online | repo only |
| M3 | endpoints in server.py + Settings → Modules section | server.py: **gated**, needs the owner's OK (`deploy/modules_deploy.sh`) |

Checks: `py -3 tests/server_checks/module_manager_check.py` (40 checks: RFC vectors, local/online install, update
detection, refusal of tampered files/index, foreign key, hostile paths). Settings page without the real server:
`py -3 tests/ui/modules_settings_stub.py` -> http://127.0.0.1:8792/settings.html#modules.
