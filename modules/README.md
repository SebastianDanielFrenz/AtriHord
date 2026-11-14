# Optional modules

Each folder here is one optional module: browser files only (`.js`), plus `module.json`
(`id`, `name`, `version`, `description`, `files`). Installing a module copies it to `web_files/js/modules/<id>/`,
which is git-ignored, so `git pull` never installs, updates or removes one.

- At install time: `install.sh` asks which ones to install (or `--modules visualizer,…|all|none`).
- Later: Settings → Server → Modules (admin), or `python3 module_manager.py list|install|update|remove`.

`index.json` + `index.json.sig` are the signed list the Settings page installs from. Regenerate them with
`python3 tools/publish_modules.py --key <secret key file>` after changing a module, and raise its `version`.
See MODULE_SYSTEM_PLAN.md.
