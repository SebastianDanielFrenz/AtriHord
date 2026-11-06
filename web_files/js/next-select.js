"use strict";

/* Selection for media-next.html. A fork of media-select.js (M2 decision in MEDIA_UI_REDESIGN_PLAN.md):
 * it replaces that script on this page only, so the old page and its Select mode stay as they are
 * until the old page goes.
 *
 * What it does that the old script did not:
 *  - no mode to switch on: a check circle at a tile's top-left on hover (always shown once anything
 *    is selected), clicking it selects without opening the viewer; while a selection exists a click
 *    on a tile toggles it; Shift-click selects the range in the on-screen order of that grid;
 *    Ctrl/Cmd-click toggles one; on touch a long press selects and a tap then toggles.
 *  - the selection bar (#nxSelBar) replaces the top bar while something is selected: favorite, add
 *    to pool, download, convert to 3D, delete. Everything is available to every account except what
 *    the server keeps for admins (3D, delete), which is hidden for guests (body.nx-admin, shell).
 *  - the same delete dialog for the picture open in the viewer (Del).
 *  - the old rubber band stays, behind the old Select button (relabelled in the HTML): it needs
 *    mousedown to be cancelled, which a plain page of tiles must not do, so it is a mode (or it
 *    starts in the empty space around the tiles).
 *
 * Like the old script it keeps off media.js's state: everything it needs is on the tiles
 * (data-path, data-root, data-kind, data-label), so it survives any re-render. The few media.js
 * globals it reads or calls are named where it does, and each is guarded - a cached older
 * media.js must not break the page.
 *
 * A selection belongs to the place it was made in (folder, search, view, destination): leaving
 * that place clears it, because the selection bar replaces the search box and hidden selected
 * files must never be deleted by accident. A re-render of the same place (sort, delete, another
 * page of results) keeps it. */

(function () {
  const GRID_IDS = ["mediaGrid", "recentMediaGrid", "searchResultsGrid"];
  const GRID_SEL = GRID_IDS.map((id) => "#" + id).join(", ");
  const TILE_SEL = GRID_IDS.map((id) => `#${id} .media-tile[data-path]`).join(", ");
  const $ = (id) => document.getElementById(id);

  const DOWNLOAD_CAP = 20;          // no zip API: files go one after another, so there is a ceiling
  const DOWNLOAD_GAP_MS = 450;      // a browser asks before it lets a page start many downloads at once
  const LONG_PRESS_MS = 450;
  const CHECK_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 12.5l3.8 3.8 7.2-7.6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  let isAdmin = false;
  let dragMode = false;              // the old Select button: rubber band, and a click always toggles
  const selected = new Map();        // key -> {root, path, kind, label}
  let anchorKey = null;              // where Shift-click ranges start
  let selectionPlace = null;         // placeParts() when the selection began
  let busy = false;                  // a bulk action is running

  const keyOf = (el) => `${el.dataset.root || ""}::${el.dataset.path || ""}`;
  const tiles = () => Array.from(document.querySelectorAll(TILE_SEL));
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function toast(msg) {
    const el = $("selectToast");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "block";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.style.display = "none"; }, 9000);
  }

  // Runs fn over items with at most `limit` in flight; never throws, reports each outcome.
  async function runLimited(items, limit, fn) {
    const out = new Array(items.length);
    let next = 0;
    async function lane() {
      while (next < items.length) {
        const i = next++;
        try { out[i] = { item: items[i], ok: true, value: await fn(items[i]) }; }
        catch (err) { out[i] = { item: items[i], ok: false, err: (err && err.message) || String(err) }; }
      }
    }
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
    return out;
  }

  // "2 failed (Not found), 1 failed (HTTP 500)" from runLimited results.
  function failureText(results) {
    const bad = results.filter((r) => !r.ok);
    if (!bad.length) return "";
    const why = new Map();
    for (const r of bad) why.set(r.err, (why.get(r.err) || 0) + 1);
    return Array.from(why, ([msg, n]) => `${n} failed (${msg})`).join(", ");
  }

  async function postJson(url, body) {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  }

  /* ---------- the entry objects behind the tiles ---------- */

  // media.js shares ONE object per file between the folder grid, the recent list, the search results
  // and the viewer's list (inventory 5.3): flipping `favorite` on it is what keeps them agreeing.
  // They are reachable by name; each read is guarded.
  function entryLists() {
    const out = [];
    const add = (l) => { if (Array.isArray(l) && !out.includes(l)) out.push(l); };
    try { add(currentFilesList); } catch (e) { /* older media.js */ }
    try { add(recentMediaItems); } catch (e) { /* ditto */ }
    try { add(searchItems); } catch (e) { /* ditto */ }
    try { add(activePreviewList); } catch (e) { /* ditto */ }
    return out;
  }
  function currentRoot() { try { return currentMediaRoot || ""; } catch (e) { return ""; } }

  // key -> [entries], built once per action (a selection can be thousands of files).
  function entryIndex() {
    const idx = new Map();
    const cur = currentRoot();
    for (const list of entryLists()) {
      for (const e of list) {
        if (!e || !e.path) continue;
        const k = `${e.root || cur}::${e.path}`;
        const a = idx.get(k);
        if (a) a.push(e); else idx.set(k, [e]);
      }
    }
    return idx;
  }

  const hasHeartTile = (item) => tiles().some((t) => keyOf(t) === `${item.root}::${item.path}` && t.querySelector(".media-tile-fav"));

  /* ---------- selecting ---------- */

  // Where the user is, as parts. media.js sets the root late (a URL without ?root= loads with root ''
  // and then takes the server's), so an empty root matches any: only a real change of folder, search,
  // view or destination counts as leaving.
  function placeParts() {
    try {
      const search = searchModeActive ? [searchQuery, activeTag, activePerson, activeFavorites ? "fav" : ""].join("|") : "";
      return { root: currentMediaRoot || "", rest: [currentMediaPath, search, currentView || "", document.body.dataset.dest || ""].join("") };
    } catch (e) { return { root: "", rest: "" }; }
  }
  const samePlace = (a, b) => a.rest === b.rest && (a.root === b.root || !a.root || !b.root);

  // A selection that no longer belongs to the place on screen is dropped. While the folder is still
  // loading nothing is judged: the half-built state says nothing about where the user ended up.
  function checkPlace() {
    if (!selected.size || document.querySelector("#mediaGrid > .media-loading")) return;
    if (!samePlace(selectionPlace, placeParts())) clearSelection();
  }

  function addSelected(tile) {
    if (!selected.size) selectionPlace = placeParts();
    selected.set(keyOf(tile), {
      root: tile.dataset.root || "",
      path: tile.dataset.path,
      kind: tile.dataset.kind,
      label: tile.dataset.label || tile.dataset.path,
    });
  }

  function toggle(tile) {
    const key = keyOf(tile);
    if (selected.has(key)) selected.delete(key); else addSelected(tile);
    anchorKey = key;
    afterChange();
  }

  function selectRangeTo(tile) {
    const grid = tile.closest(GRID_SEL);
    const list = grid ? Array.from(grid.querySelectorAll(".media-tile[data-path]")) : [];
    const a = list.findIndex((t) => keyOf(t) === anchorKey);
    const b = list.indexOf(tile);
    if (a < 0 || b < 0) { addSelected(tile); anchorKey = keyOf(tile); afterChange(); return; }
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) addSelected(list[i]);
    afterChange();                      // the anchor stays, so the range can be widened
  }

  function clearSelection() {
    selected.clear();
    anchorKey = null;
    afterChange();
  }

  // The same file can be on screen twice (Recent and its folder): paint by key, not by tile.
  function paintTile(t) {
    const on = selected.has(keyOf(t));
    if (t.classList.contains("tile-selected") !== on) t.classList.toggle("tile-selected", on);
    const c = t.querySelector(":scope > .nx-check");
    if (c) c.setAttribute("aria-checked", String(on));
  }
  const paintAll = () => tiles().forEach(paintTile);

  function decorate(t) {
    if (t.querySelector(":scope > .nx-check")) return;
    const c = document.createElement("span");
    c.className = "nx-check";
    c.setAttribute("role", "checkbox");
    c.setAttribute("aria-checked", "false");
    c.setAttribute("aria-label", "Select");
    c.innerHTML = CHECK_SVG;
    t.appendChild(c);
  }

  /* ---------- the selection bar ---------- */

  function selectedItems() { return Array.from(selected.values()); }

  function isFavorite(item, idx) {
    const es = idx.get(`${item.root || currentRoot()}::${item.path}`);
    if (es && es.length) return es.some((e) => e.favorite);
    return hasHeartTile(item);
  }

  function updateBar() {
    const n = selected.size;
    const bar = $("nxSelBar");
    document.body.classList.toggle("nx-selecting", n > 0);
    if (!bar) return;
    bar.hidden = n === 0;
    if (!n) return;
    $("nxSelCount").textContent = `${n} selected`;
    const items = selectedItems();
    const files = items.filter((i) => i.kind !== "folder");
    const fav = $("nxSelFav");
    const idx = entryIndex();
    const allFav = files.length > 0 && files.every((i) => isFavorite(i, idx));
    fav.classList.toggle("is-on", allFav);
    fav.dataset.tip = allFav ? "Remove from favorites" : "Add to favorites";
    fav.setAttribute("aria-label", fav.dataset.tip);
    fav.disabled = busy || !files.length;
    $("nxSelPool").disabled = busy;
    $("nxSelDownload").disabled = busy || !files.length;
    const convertible = window.Stereo3d ? files.filter((i) => Stereo3d.canConvert(i)).length : 0;
    $("nxSel3d").disabled = busy || !convertible;
    $("nxSelDelete").disabled = busy;
  }

  function afterChange() {
    paintAll();
    updateBar();
  }

  function setBusy(on) { busy = on; updateBar(); }

  /* ---------- favorite ---------- */

  async function bulkFavorite() {
    const files = selectedItems().filter((i) => i.kind !== "folder");
    const folders = selected.size - files.length;
    if (!files.length || busy) return;
    const idx = entryIndex();
    const want = !files.every((i) => isFavorite(i, idx));
    const todo = files.filter((i) => isFavorite(i, idx) !== want);
    setBusy(true);
    const results = await runLimited(todo, 4, (i) =>
      postJson("/api/media/favorite", { root: i.root, path: i.path, favorite: want }));
    // Through the same entry objects the grids and the viewer share, so every heart agrees.
    for (const r of results) {
      if (!r.ok) continue;
      const i = r.item;
      for (const e of idx.get(`${i.root || currentRoot()}::${i.path}`) || []) e.favorite = want;
      for (const t of tiles()) {
        if (keyOf(t) !== `${i.root}::${i.path}`) continue;
        if (typeof setTileFavorite === "function") setTileFavorite(t, want);
      }
    }
    try {
      const shown = activePreviewList[currentPreviewIndex];
      if (shown && typeof paintFavoriteButton === "function") paintFavoriteButton(shown);
    } catch (e) { /* no viewer state */ }
    const done = results.filter((r) => r.ok).length;
    const bad = failureText(results);
    let msg = want ? `Added ${plural(done, "file")} to favorites` : `Removed ${plural(done, "file")} from favorites`;
    if (bad) msg += `, ${bad}`;
    if (folders) msg += `, ${plural(folders, "folder")} skipped (a folder cannot be a favorite)`;
    toast(msg);
    setBusy(false);
  }

  /* ---------- download ---------- */

  function startDownload(item, idx) {
    const e = (idx.get(`${item.root || currentRoot()}::${item.path}`) || [])[0];
    const url = e && e.url ? e.url : `/api/media/file?root=${encodeURIComponent(item.root)}&path=${encodeURIComponent(item.path)}`;
    const name = (e && e.name) || item.label;
    if (typeof downloadFile === "function") { downloadFile(url, name); return; }
    const a = document.createElement("a");
    a.href = url + (url.includes("?") ? "&" : "?") + "download=1";
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function bulkDownload() {
    const files = selectedItems().filter((i) => i.kind !== "folder");
    const folders = selected.size - files.length;
    if (!files.length || busy) return;
    const batch = files.slice(0, DOWNLOAD_CAP);
    let msg;
    if (files.length > DOWNLOAD_CAP) {
      msg = `Downloading the first ${DOWNLOAD_CAP} of ${files.length} files: there is no zip, so at most ${DOWNLOAD_CAP} start at once. ${files.length - DOWNLOAD_CAP} were left out - select them again for the next go.`;
    } else {
      msg = `Downloading ${plural(batch.length, "file")} one after another`;
      if (batch.length > 1) msg += " (your browser may ask to allow multiple downloads)";
    }
    if (folders) msg += ` ${plural(folders, "folder")} skipped: a folder cannot be downloaded.`;
    toast(msg);
    setBusy(true);
    const idx = entryIndex();
    for (let i = 0; i < batch.length; i++) {
      startDownload(batch[i], idx);
      if (i < batch.length - 1) await sleep(DOWNLOAD_GAP_MS);
    }
    setBusy(false);
  }

  /* ---------- add to pool ---------- */

  // The pool picker is media.js's (rows, create form, thumbnails) and takes one path. It calls the
  // global addCurrentItemToPool(poolId) when a row or "Create & Add" is clicked, so for a selection
  // that one function is replaced: `bulk` holds the selection while the picker is open and every
  // other way into the picker (the context menu) finds it null and runs media.js's own code.
  let bulk = null;
  const originalAdd = window.addCurrentItemToPool;
  const originalClose = window.closePoolPicker;

  async function addBulkToPool(poolId) {
    const items = bulk.slice();
    bulk = null;
    const results = await runLimited(items, 1, (i) => postJson(`/api/share/${encodeURIComponent(poolId)}/items`, { path: i.path, root: i.root }));
    // The server answers with the pool, so the toast can name it.
    const pool = (results.find((r) => r.ok) || {}).value || {};
    const done = results.filter((r) => r.ok).length;
    const bad = failureText(results);
    if (typeof originalClose === "function") originalClose();
    if (typeof loadShares === "function") loadShares();
    toast(`Added ${plural(done, "item")} to ${pool.label ? `"${pool.label}"` : "the pool"}${bad ? `, ${bad}` : ""}`);
    if (!bad) clearSelection();
    setBusy(false);
  }

  if (typeof originalAdd === "function") {
    window.addCurrentItemToPool = function (poolId) {
      return bulk ? addBulkToPool(poolId) : originalAdd.apply(this, arguments);
    };
  }
  if (typeof originalClose === "function") {
    window.closePoolPicker = function () {
      bulk = null;
      setBusy(false);
      return originalClose.apply(this, arguments);
    };
  }

  function bulkToPool() {
    const items = selectedItems();
    if (!items.length || busy || typeof addToPoolFlow !== "function") return;
    bulk = items;
    setBusy(true);
    addToPoolFlow(items[0].path, items[0].root);
  }

  /* ---------- convert to 3D ---------- */

  // One POST for the whole selection. Folders, audio and anything the 3D backend cannot take are
  // left out here, so the server never has to refuse half a request.
  async function bulkConvert3d() {
    if (!isAdmin || busy || !window.Stereo3d) return;
    const items = selectedItems().filter((i) => i.kind !== "folder" && Stereo3d.canConvert(i)).map((i) => ({ root: i.root, path: i.path }));
    const skipped = selected.size - items.length;
    if (!items.length) { toast("Nothing to convert: select videos or pictures"); return; }
    setBusy(true);
    try {
      const out = await Stereo3d.requestConversion(items);
      let msg = Stereo3d.summaryText(out);
      if (skipped) msg += `, ${skipped} not a video or picture`;
      toast(msg);
      clearSelection();
    } catch (err) {
      toast(`Nothing was queued: ${err.message}`);
    }
    setBusy(false);
  }

  /* ---------- the delete dialog ---------- */

  let pending = null;               // {items, viewer: {list, index} | null}

  function openDeleteDialog(items, viewer) {
    if (!isAdmin || !items.length) return;
    const modal = $("deleteModal");
    const list = $("deleteList");
    pending = { items, viewer: viewer || null };

    const folders = items.filter((i) => i.kind === "folder").length;
    const files = items.length - folders;
    const parts = [];
    if (files) parts.push(plural(files, "file"));
    if (folders) parts.push(plural(folders, "folder"));
    $("deleteSummary").textContent = parts.join(" and ");
    $("deleteFolderWarning").style.display = folders ? "block" : "none";

    list.innerHTML = "";
    for (const i of items.slice(0, 200)) {
      const row = document.createElement("div");
      row.className = "delete-list-row";
      row.textContent = (i.kind === "folder" ? "\u{1F4C1} " : "") + i.label;
      row.title = i.path;
      list.appendChild(row);
    }
    if (items.length > 200) {
      const more = document.createElement("div");
      more.className = "delete-list-more";
      more.textContent = `…and ${items.length - 200} more`;
      list.appendChild(more);
    }
    setDeleteStatus("");
    modal.style.display = "flex";
  }

  // overlay-dismiss.js gives this dialog a history entry and pops it when it closes. With the viewer
  // open that pop must be over BEFORE the viewer moves on, or the pop lands on the entry of the item
  // just deleted and media.js reopens it.
  function closeDeleteDialog() {
    const modal = $("deleteModal");
    if (modal.style.display === "none") return Promise.resolve();
    const viaHistory = !!(history.state && history.state.ahOverlay);
    modal.style.display = "none";
    if (!viaHistory) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { clearTimeout(t); window.removeEventListener("popstate", done); resolve(); };
      const t = setTimeout(done, 600);
      window.addEventListener("popstate", done);
    });
  }

  function setDeleteStatus(msg, kind) {
    const el = $("deleteStatus");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "delete-status" + (kind ? " " + kind : "");
  }

  const setDeleteButtons = (disabled) => {
    for (const b of document.querySelectorAll("#deleteModal button")) b.disabled = disabled;
  };

  async function runDelete(mode) {
    if (!pending) return;
    const ctx = pending;
    setDeleteStatus("Deleting…");
    setDeleteButtons(true);

    let data = {};
    try {
      const res = await fetch("/api/media/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: ctx.items.map((i) => ({ root: i.root, path: i.path })), mode }),
      });
      data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      setDeleteStatus(`Nothing was deleted: ${err.message}`, "err");
      setDeleteButtons(false);
      return;
    }
    setDeleteButtons(false);
    pending = null;
    await closeDeleteDialog();

    // Which items the server really moved: its `deleted` list when it sends one, else all that
    // it did not list as failed.
    const failedPaths = new Set((data.failed || []).map((f) => (typeof f === "string" ? f : f.path)));
    let gone;
    if (Array.isArray(data.deleted)) {
      const set = new Set(data.deleted.map((d) => (typeof d === "string" ? d : d.path)));
      gone = ctx.items.filter((i) => set.has(i.path));
    } else {
      gone = ctx.items.filter((i) => !failedPaths.has(i.path));
    }
    const notDone = ctx.items.length - gone.length;

    removeFromView(gone, !!ctx.viewer);
    for (const i of gone) selected.delete(`${i.root}::${i.path}`);
    afterChange();

    let msg = `Moved ${plural(gone.length, "item")} to the trash`;
    const notes = (data.records || []).join(", ");
    if (notes) msg += ` (${notes})`;
    if (notDone) msg += `, ${notDone} could not be deleted`;
    toast(msg);

    if (ctx.viewer) advanceViewer(ctx.viewer, gone.length > 0);
  }

  // The deleted files leave every grid and list on screen at once. The folder grid is rebuilt from
  // media.js's own list (its tiles hold a private sorted copy of the list for the arrow keys, which
  // only a rebuild replaces); with the viewer open the open list is trimmed in place instead,
  // because that private copy IS the viewer's list.
  function removeFromView(gone, fromViewer) {
    if (!gone.length) return;
    const cur = currentRoot();
    const hit = (root, path) => gone.some((g) => (g.root || cur) === (root || cur)
      && (g.path === path || (g.kind === "folder" && path.startsWith(g.path + "/"))));

    for (const list of entryLists()) {
      for (let i = list.length - 1; i >= 0; i--) if (list[i] && hit(list[i].root, list[i].path)) list.splice(i, 1);
    }
    let folderGridTouched = false;
    const touched = new Set();
    for (const t of tiles()) {
      if (!hit(t.dataset.root, t.dataset.path)) continue;
      if (t.closest("#mediaGrid")) folderGridTouched = true;
      touched.add(t.parentElement);
      t.remove();
    }
    try {
      for (const g of gone) {
        if (g.kind !== "folder" || (g.root || cur) !== cur) continue;
        const parent = g.path.includes("/") ? g.path.slice(0, g.path.lastIndexOf("/")) : "";
        if (parent !== currentMediaPath) continue;
        const name = g.path.slice(parent ? parent.length + 1 : 0);
        currentDirNames = currentDirNames.filter((d) => d !== name);
        currentDirInfo = currentDirInfo.filter((d) => d.name !== name);
      }
    } catch (e) { /* older media.js */ }

    if (folderGridTouched && !fromViewer && typeof renderMediaGrid === "function") {
      try { renderMediaGrid(currentDirNames, currentFilesList); } catch (e) { /* leave the trimmed grid */ }
    } else if (typeof scheduleGalleryLayout === "function") {
      for (const grid of touched) if (grid && grid.isConnected) scheduleGalleryLayout(grid, true);
    }
    const mg = $("mediaGrid");
    if (mg && !mg.querySelector(".media-tile") && !mg.querySelector(".media-loading, .media-error")) {
      const empty = $("mediaEmpty");
      if (empty) empty.style.display = "block";
    }
  }

  // After the picture in the viewer was deleted: on to the one that took its place, or out at the end.
  function advanceViewer(viewer, deleted) {
    if (!deleted) return;
    const list = viewer.list;
    if (list && viewer.index < list.length) openMediaPreview(list[viewer.index], list, { push: false });
    else closeMediaPreview();
  }

  const viewerOpen = () => $("mediaPreviewModal").style.display === "flex";

  function deleteCurrent() {
    if (!isAdmin || !viewerOpen()) return false;
    let f;
    try { f = activePreviewList[currentPreviewIndex]; } catch (e) { f = null; }
    if (!f) return false;
    openDeleteDialog([{ root: f.root || currentRoot(), path: f.path, kind: "file", label: f.name || f.path }],
      { list: activePreviewList, index: currentPreviewIndex });
    return true;
  }

  /* ---------- rubber band (behind the Select button, or from the empty space around the tiles) ---------- */

  // A press only becomes a drag once the pointer has moved this far. Below it the press is a click.
  const DRAG_THRESHOLD_PX = 5;
  let band = null;
  let origin = null;
  let additive = false;
  let baseline = null;
  let suppressClick = false;

  function bandRect(a, b) {
    return { left: Math.min(a.x, b.x), top: Math.min(a.y, b.y), right: Math.max(a.x, b.x), bottom: Math.max(a.y, b.y) };
  }
  const intersects = (r, t) => !(t.right < r.left || t.left > r.right || t.bottom < r.top || t.top > r.bottom);

  function onMouseDown(ev) {
    if (ev.button !== 0 || !ev.target.closest) return;
    const inGrid = ev.target.closest(GRID_SEL);
    if (!inGrid || ev.target.closest(".nx-check")) return;
    // Shift-click must not start a text selection across the tiles.
    if (ev.shiftKey && ev.target.closest(TILE_SEL)) ev.preventDefault();
    const onTile = !!ev.target.closest(TILE_SEL);
    if (!dragMode && onTile) return;
    if (!dragMode && !(ev.target === inGrid || ev.target.classList.contains("gallery-break"))) return;
    origin = { x: ev.clientX, y: ev.clientY };
    additive = ev.ctrlKey || ev.metaKey || ev.shiftKey;
    suppressClick = false;
    ev.preventDefault();
  }

  function onMouseMove(ev) {
    if (!origin) return;
    if (!band) {
      if (Math.hypot(ev.clientX - origin.x, ev.clientY - origin.y) < DRAG_THRESHOLD_PX) return;
      if (!additive) selected.clear();
      baseline = new Set(selected.keys());
      band = document.createElement("div");
      band.className = "select-band";
      document.body.appendChild(band);
      suppressClick = true;
    }
    const r = bandRect(origin, { x: ev.clientX, y: ev.clientY });
    band.style.left = r.left + "px";
    band.style.top = r.top + "px";
    band.style.width = (r.right - r.left) + "px";
    band.style.height = (r.bottom - r.top) + "px";

    // Decided per file, not per tile: a file on screen twice must not be deselected by its far copy.
    const wanted = new Map();
    for (const t of tiles()) {
      const k = keyOf(t);
      const tr = t.getBoundingClientRect();
      const inBand = tr.width > 0 && intersects(r, tr);     // a tile in a hidden section has an empty rect
      const w = wanted.get(k);
      if (!w) wanted.set(k, { tile: t, on: inBand || baseline.has(k) });
      else if (inBand) w.on = true;
    }
    for (const [k, w] of wanted) {
      if (w.on && !selected.has(k)) addSelected(w.tile);
      else if (!w.on && selected.has(k)) selected.delete(k);
    }
    afterChange();
  }

  function onMouseUp() {
    if (band) band.remove();
    band = null;
    origin = null;
    baseline = null;
    additive = false;
  }

  /* ---------- clicks ---------- */

  function onClickCapture(ev) {
    if (!ev.target.closest) return;
    const tile = ev.target.closest(TILE_SEL);
    if (!tile) return;
    if (suppressClick || (swallowUntil && Date.now() < swallowUntil)) {
      suppressClick = false;
      swallowUntil = 0;
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    const onCircle = !!ev.target.closest(".nx-check");
    const modified = ev.shiftKey || ev.ctrlKey || ev.metaKey;
    // Nothing selected and nothing asked for: the click is the tile's own (open it, descend into it).
    if (!(onCircle || modified || selected.size || dragMode)) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.shiftKey && anchorKey) selectRangeTo(tile); else toggle(tile);
  }

  /* ---------- touch: long press selects, a tap then toggles ---------- */

  let press = null;
  let lastTouchAt = 0;
  let swallowUntil = 0;           // the click that follows a long press must not open the tile

  function firePress() {
    if (!press || press.fired) return;
    press.fired = true;
    if (!selected.has(keyOf(press.tile))) {
      addSelected(press.tile);
      anchorKey = keyOf(press.tile);
      afterChange();
      if (navigator.vibrate && navigator.userActivation && navigator.userActivation.hasBeenActive) { try { navigator.vibrate(12); } catch (e) { /* not allowed */ } }
    }
  }
  function endPress() {
    if (!press) return;
    clearTimeout(press.timer);
    if (press.fired) swallowUntil = Date.now() + 800;
    press = null;
  }

  function onTouchStart(ev) {
    lastTouchAt = Date.now();
    endPress();
    if (ev.touches.length !== 1 || !ev.target.closest) return;
    const tile = ev.target.closest(TILE_SEL);
    if (!tile) return;
    const t = ev.touches[0];
    press = { tile, x: t.clientX, y: t.clientY, fired: false, timer: setTimeout(firePress, LONG_PRESS_MS) };
  }
  function onTouchMove(ev) {
    if (!press || press.fired) return;
    const t = ev.touches[0];
    if (Math.hypot(t.clientX - press.x, t.clientY - press.y) > 10) { clearTimeout(press.timer); press = null; }
  }

  // The browser's own long-press menu (Android Chrome fires contextmenu on a held picture) would open
  // media.js's context menu over the selection that just began. On a touch long press it is ours; a
  // mouse right-click still gets the menu.
  function onContextMenu(ev) {
    if (!ev.target.closest) return;
    const tile = ev.target.closest(TILE_SEL);
    if (!tile || Date.now() - lastTouchAt > 1500) return;
    ev.preventDefault();
    ev.stopPropagation();
    firePress();
  }

  /* ---------- keys ---------- */

  const isTyping = (t) => !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  const shown = (el) => !!el && el.style.display === "flex";
  const overlayOpen = () => shown($("poolPickerModal")) || !$("nxHelp").hidden
    || ($("imageSearchDialog") && $("imageSearchDialog").open)
    || ($("mediaContextMenu") && $("mediaContextMenu").style.display === "block")
    || ($("mediaFilterPanel") && !$("mediaFilterPanel").hidden);

  // Capture on window, so it sees Esc before media.js closes a menu: one press does one thing.
  window.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape" || isTyping(ev.target)) return;
    // The dialog first, and only it: with the viewer behind it the same press must not close the viewer too.
    if (shown($("deleteModal"))) { pending = null; ev.stopPropagation(); ev.preventDefault(); closeDeleteDialog(); return; }
    if (viewerOpen() || overlayOpen()) return;
    if (selected.size) { clearSelection(); ev.stopPropagation(); }
    else if (dragMode) setDragMode(false);
  }, true);

  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Delete" || ev.ctrlKey || ev.metaKey || ev.altKey || isTyping(ev.target) || !isAdmin) return;
    if (shown($("deleteModal")) || overlayOpen()) return;
    if (viewerOpen()) {
      if (deleteCurrent()) ev.preventDefault();
    } else if (selected.size) {
      ev.preventDefault();
      openDeleteDialog(selectedItems(), null);
    }
  });

  /* ---------- wiring ---------- */

  function setDragMode(on) {
    dragMode = on;
    document.body.classList.toggle("select-mode", on);
    const btn = $("selectModeBtn");
    if (btn) {
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", String(on));
    }
  }

  function wire() {
    $("nxSelClose").addEventListener("click", clearSelection);
    $("nxSelFav").addEventListener("click", bulkFavorite);
    $("nxSelPool").addEventListener("click", bulkToPool);
    $("nxSelDownload").addEventListener("click", bulkDownload);
    $("nxSel3d").addEventListener("click", bulkConvert3d);
    $("nxSelDelete").addEventListener("click", () => openDeleteDialog(selectedItems(), null));
    const mode = $("selectModeBtn");
    if (mode) mode.addEventListener("click", () => setDragMode(!dragMode));

    $("deleteCancelBtn").addEventListener("click", () => { pending = null; closeDeleteDialog(); });
    $("deleteReleaseBtn").addEventListener("click", () => runDelete("release"));
    $("deleteBanBtn").addEventListener("click", () => runDelete("ban"));

    document.addEventListener("click", onClickCapture, true);
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchmove", onTouchMove, { passive: true });
    document.addEventListener("touchend", endPress, { passive: true });
    document.addEventListener("touchcancel", endPress, { passive: true });
    document.addEventListener("contextmenu", onContextMenu, true);
    window.addEventListener("popstate", () => setTimeout(checkPlace, 0));

    // media.js rebuilds a grid on every sort, navigation and search and does not know this module
    // exists, so watch for it: new tiles get their circle and their selected look, and a change of
    // place drops the selection.
    for (const id of GRID_IDS) {
      const g = $(id);
      if (!g) continue;
      for (const t of g.querySelectorAll(".media-tile[data-path]")) { decorate(t); paintTile(t); }
      new MutationObserver((records) => {
        for (const r of records) {
          for (const n of r.addedNodes) {
            if (n.nodeType !== 1 || !n.matches(".media-tile[data-path]")) continue;
            decorate(n);
            paintTile(n);
          }
        }
        checkPlace();
      }).observe(g, { childList: true });
    }
    new MutationObserver(checkPlace).observe(document.body, { attributes: true, attributeFilter: ["data-dest"] });
    updateBar();
  }

  fetch("/api/me", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((me) => { isAdmin = !!(me && me.role === "admin"); })
    .catch(() => {});

  window.NextSelect = {
    clear: clearSelection,
    count: () => selected.size,
    deleteCurrent,
    // For the context menu opened from the viewer's three dots.
    canDelete: () => isAdmin,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
