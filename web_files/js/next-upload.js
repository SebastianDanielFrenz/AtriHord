"use strict";

/* The Upload destination of media-next.html (admin only): put files or whole folders into a chosen
 * folder of a library by hand. A client for POST /api/media/upload and GET /api/media/upload/limits
 * (server_patch_upload.py), plus the existing /api/roots and /api/media/browse for the folder picker.
 *
 * Nothing is sent when files are dropped or chosen: they join a list, the page says exactly where
 * each one will go ("42 files -> Downloads/Holiday/Trip/Day 1"), and only the Upload button starts
 * the transfer. Files go in batches (a request holds up to BATCH_FILES files or BATCH_BYTES bytes, a
 * bigger file travels alone), three requests at a time. Every file carries an id that stays the same
 * across retries, so a batch that broke off after three files and is sent again does not save those
 * three twice (the server remembers the id).
 *
 * Classic script; the shell (next-shell.js) calls NextUpload.open() when the destination opens, and
 * the folder view's "Upload here" calls NextUpload.go({root, path}). */

(function () {
  const $ = (id) => document.getElementById(id);
  const PARALLEL = 3;                         // requests at a time
  const BATCH_FILES = 100;
  const BATCH_BYTES = 200 * 1024 * 1024;
  const PAGE = 100;                           // queue rows added per step
  const BODY_OVERHEAD = 700;                  // multipart framing per file, for the progress estimate

  const S = {
    limits: null,                // /api/media/upload/limits, for the chosen destination
    roots: [],                   // /api/roots
    root: "",                    // chosen library id
    path: "",                    // chosen folder (may not exist yet)
    browseGen: 0,
    browseExists: true,
    dirs: [],
    items: [],                   // the queue
    shown: 0,                    // rows in the DOM
    nextId: 1,
    running: false,              // a run is in progress (Upload was pressed and armed files remain)
    active: new Set(),           // batches in flight
    skipped: new Map(),          // reason -> count, files that were not added
    summary: null,               // the last finished run, for the box under the buttons
    run: null,                   // {root, path, saved, failed, renamed:[], dups:[], failedList:[], bytes, start}
    preselect: null,
    opened: false,
    limitsGen: 0,
    samples: [],                 // [time, bytes] for the speed
  };
  const runId = Math.random().toString(36).slice(2, 8);

  /* ---------- small helpers ---------- */

  const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : (many || one + "s")}`;

  function sizeText(bytes) {
    const b = Number(bytes) || 0;
    if (b >= 1e12) return `${(b / 1e12).toFixed(1)} TB`;
    if (b >= 1e9) return `${(b / 1e9).toFixed(1)} GB`;
    if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
    if (b >= 1e3) return `${Math.round(b / 1e3)} KB`;
    return `${b} B`;
  }

  // The server's limits are binary (20 GiB, 5 GiB): shown as such so the numbers match what the owner set.
  function binText(bytes) {
    const b = Number(bytes) || 0;
    if (b >= 1024 ** 4) return `${(b / 1024 ** 4).toFixed(1)} TiB`;
    if (b >= 1024 ** 3) return `${(b / 1024 ** 3).toFixed(b >= 100 * 1024 ** 3 ? 0 : 1).replace(/\.0$/, "")} GiB`;
    return sizeText(b);
  }

  function toast(msg) {
    const el = $("selectToast");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "block";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.style.display = "none"; }, 7000);
  }

  const extOf = (name) => {
    const i = String(name).lastIndexOf(".");
    return i > 0 ? String(name).slice(i).toLowerCase() : "";
  };

  /* Mirrors the server's rules for a typed destination (_upload_clean_dir, strict) so a mistake is
     seen at once; the server checks again. Returns {parts} or {error}. */
  const BAD_CHARS = /[\u0000-\u001f\u007f<>:"|?*\\/]/;
  const SYSTEM_DIRS = ["lost+found", "system volume information", "$recycle.bin", "@eadir", "#recycle", "#snapshot"];
  const RESERVED = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;
  function cleanDir(raw) {
    const parts = [];
    for (let seg of String(raw || "").replace(/\\/g, "/").split("/")) {
      seg = seg.trim();
      if (seg === "" || seg === ".") continue;
      if (seg === "..") return { error: "'..' is not allowed in a folder path" };
      if (seg.startsWith(".")) return { error: `hidden folders are not allowed ('${seg}')` };
      if (SYSTEM_DIRS.includes(seg.toLowerCase())) return { error: `'${seg}' is a system folder` };
      if (BAD_CHARS.test(seg) || RESERVED.test(seg) || /[. ]$/.test(seg)) return { error: `'${seg}' is not a valid folder name` };
      parts.push(seg);
    }
    return { parts };
  }

  const rootName = (id) => { const r = S.roots.find((x) => x.id === id); return r ? r.name : id; };
  const destLabel = (root, path, sub) => [rootName(root), ...cleanDirParts(path), ...(sub ? sub.split("/") : [])].join(" / ");
  function cleanDirParts(path) { const c = cleanDir(path); return c.parts || []; }

  /* ---------- limits and roots ---------- */

  async function getJson(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (res.status === 401) { window.location.reload(); throw new Error("signed out"); }
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
    return body;
  }

  async function loadRoots() {
    try {
      const roots = await getJson("/api/roots");
      S.roots = (Array.isArray(roots) ? roots : []).filter((r) => r.exists !== false);
    } catch (e) {
      if (!S.roots.length) S.roots = [{ id: "downloads", name: "Downloads" }];
    }
    const sel = $("nxUpRoot");
    sel.replaceChildren();
    for (const r of S.roots) {
      const o = document.createElement("option");
      o.value = r.id;
      o.textContent = r.name || r.id;
      sel.appendChild(o);
    }
    if (!S.roots.some((r) => r.id === S.root)) S.root = (S.roots[0] && S.roots[0].id) || "downloads";
    sel.value = S.root;
  }

  async function loadLimits() {
    const gen = ++S.limitsGen;
    const cleaned = cleanDir(S.path);
    const qs = `root=${encodeURIComponent(S.root)}&path=${encodeURIComponent((cleaned.parts || []).join("/"))}`;
    try {
      const data = await getJson(`/api/media/upload/limits?${qs}`);
      if (gen !== S.limitsGen) return;
      S.limits = data;
    } catch (e) {
      if (gen !== S.limitsGen) return;
      S.limits = null;
      $("nxUpLimits").textContent = `Could not read the limits: ${e.message}`;
    }
    drawLimits();
    drawPreview();
  }

  function acceptedList() {
    const ex = (S.limits && S.limits.extensions) || {};
    const bits = [];
    for (const [kind, label] of [["image", "pictures"], ["video", "videos"], ["audio", "audio"]]) {
      if (ex[kind] && ex[kind].length) bits.push(`${label} (${ex[kind].map((x) => x.slice(1)).join(", ")})`);
    }
    return bits.join("; ");
  }

  function drawLimits() {
    const L = S.limits;
    const el = $("nxUpLimits");
    if (!L) return;
    const free = L.free_bytes == null ? "unknown" : binText(L.free_bytes);
    el.textContent = `Up to ${binText(L.max_request_bytes)} per file · ${free} free on this library's disk (${binText(L.min_free_bytes)} always stays free)`;
    el.title = `Accepted: ${acceptedList()}. Refused on purpose: ${(L.refused_extensions || []).join(", ") || "none"}`;
    // The downloader folders carry the downloader's own automatic rules.
    const first = cleanDirParts(S.path)[0];
    const note = $("nxUpDlNote");
    if (S.root === "downloads" && first && (L.downloader_folders || []).includes(first)) {
      note.textContent = `"${first}" is a downloader folder. Files saved here are handled by that downloader's automatic rules as well (for example Twitter posts cut into pieces are joined, and the review queue may flag files). Pick another folder to keep that from happening.`;
      note.hidden = false;
    } else {
      note.hidden = true;
    }
  }

  /* ---------- destination: root, typed path, folder browser ---------- */

  function setPath(path, opts) {
    S.path = String(path || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    const c = cleanDir(S.path);
    const err = $("nxUpPathError");
    err.hidden = !c.error;
    err.textContent = c.error || "";
    if (c.parts) S.path = c.parts.join("/");
    if (!(opts && opts.keepInput)) $("nxUpPath").value = S.path;
    drawCrumbs();
    drawPreview();
    if (!c.error) { browse(); loadLimits(); }
  }

  function setRoot(id) {
    S.root = id;
    $("nxUpRoot").value = id;
    setPath("");
  }

  function drawCrumbs() {
    const box = $("nxUpCrumbs");
    box.replaceChildren();
    const parts = cleanDirParts(S.path);
    const add = (label, path) => {
      if (box.childNodes.length) {
        const sep = document.createElement("span");
        sep.className = "nx-up-sep";
        sep.textContent = "/";
        sep.setAttribute("aria-hidden", "true");
        box.appendChild(sep);
      }
      const b = document.createElement("button");
      b.type = "button";
      b.className = "nx-up-crumb";
      b.textContent = label;
      b.addEventListener("click", () => setPath(path));
      box.appendChild(b);
    };
    add(rootName(S.root), "");
    parts.forEach((p, i) => add(p, parts.slice(0, i + 1).join("/")));
  }

  async function browse() {
    const gen = ++S.browseGen;
    const note = $("nxUpBrowserNote");
    const c = cleanDir(S.path);
    if (c.error) return;
    note.textContent = "Loading…";
    let data = null, status = 0;
    try {
      const res = await fetch(`/api/media/browse?root=${encodeURIComponent(S.root)}&path=${encodeURIComponent(c.parts.join("/"))}`, { cache: "no-store" });
      status = res.status;
      if (res.status === 401) { window.location.reload(); return; }
      data = await res.json().catch(() => null);
    } catch (e) { /* offline */ }
    if (gen !== S.browseGen) return;
    if (data && !data.error && Array.isArray(data.dirs)) {
      S.browseExists = true;
      S.dirs = data.dirs.slice();
      note.textContent = S.dirs.length ? plural(S.dirs.length, "folder") + " here" : "No folders here.";
    } else if (status === 404) {
      S.browseExists = false;
      S.dirs = [];
      note.textContent = "This folder does not exist yet. It is created when you upload.";
    } else {
      S.browseExists = true;
      S.dirs = [];
      note.textContent = `Could not list the folders${data && data.error ? ": " + data.error : ""}.`;
    }
    drawDirs();
  }

  function drawDirs() {
    const box = $("nxUpDirs");
    box.replaceChildren();
    const parts = cleanDirParts(S.path);
    if (parts.length) {
      const up = document.createElement("button");
      up.type = "button";
      up.className = "nx-up-dir is-up";
      up.textContent = "‹ Up";
      up.addEventListener("click", () => setPath(parts.slice(0, -1).join("/")));
      box.appendChild(up);
    }
    for (const name of S.dirs) {
      if (name.startsWith(".")) continue;
      const b = document.createElement("button");
      b.type = "button";
      b.className = "nx-up-dir";
      b.textContent = name;
      b.title = `Open ${name}`;
      b.addEventListener("click", () => setPath([...parts, name].join("/")));
      box.appendChild(b);
    }
  }

  /* ---------- adding files ---------- */

  function skip(reason) { S.skipped.set(reason, (S.skipped.get(reason) || 0) + 1); }

  function skipText() {
    if (!S.skipped.size) return "";
    const parts = [];
    let total = 0;
    for (const [reason, n] of S.skipped) { parts.push(`${n.toLocaleString()} × ${reason}`); total += n; }
    return `Not added (${plural(total, "file")}): ${parts.join(", ")}. The library only lists ${acceptedList() || "pictures, videos and audio"}.`;
  }

  function addFile(file, rel) {
    const name = file.name;
    if (name.startsWith(".")) { skip("hidden files"); return; }
    const ext = extOf(name);
    const L = S.limits;
    if (L) {
      const ok = Object.values(L.extensions || {}).some((list) => list.includes(ext));
      if (!ok) {
        skip(ext ? (L.refused_extensions || []).includes(ext) ? `${ext} (refused: can carry script)` : `${ext} files` : "files without an extension");
        return;
      }
      if (file.size > L.max_request_bytes) { skip(`files over ${binText(L.max_request_bytes)}`); return; }
    }
    if (file.size === 0) { skip("empty files"); return; }
    S.items.push({
      id: S.nextId++, cid: `${runId}-${S.nextId}-${file.size}-${file.lastModified}`,
      file, name, rel: rel || "", size: file.size, state: "queued", armed: false, sent: 0,
      error: "", notes: [], result: null, row: null,
    });
  }

  function addFiles(list, source) {
    for (const f of list) {
      let rel = "";
      if (source === "folder" && f.webkitRelativePath) {
        const bits = f.webkitRelativePath.split("/");
        bits.pop();
        rel = bits.join("/");
      }
      addFile(f, rel);
    }
    afterAdd();
  }

  function afterAdd() {
    redrawQueue();
    drawPreview();
    drawSkipped();
  }

  function drawSkipped() {
    const el = $("nxUpSkipped");
    const t = skipText();
    el.textContent = t;
    el.hidden = !t;
  }

  // A dropped folder: webkitGetAsEntry must be called for every item before anything is awaited.
  async function readEntry(entry, rel, out) {
    if (entry.isFile) {
      await new Promise((resolve) => entry.file((f) => { out.push([f, rel]); resolve(); }, () => { skip("unreadable files"); resolve(); }));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const sub = rel ? `${rel}/${entry.name}` : entry.name;
      for (;;) {
        const batch = await new Promise((resolve) => reader.readEntries(resolve, () => resolve([])));
        if (!batch.length) break;
        for (const child of batch) await readEntry(child, sub, out);
      }
    }
  }

  async function onDrop(e) {
    e.preventDefault();
    $("nxUpDrop").classList.remove("is-over");
    const dt = e.dataTransfer;
    if (!dt) return;
    const entries = [];
    if (dt.items && dt.items.length && dt.items[0].webkitGetAsEntry) {
      for (const it of dt.items) {
        if (it.kind !== "file") continue;
        const en = it.webkitGetAsEntry();
        if (en) entries.push(en);
      }
    }
    if (!entries.length) { addFiles(Array.from(dt.files || []), "files"); return; }
    $("nxUpBrowserNote").dataset.busy = "1";
    const out = [];
    for (const en of entries) await readEntry(en, "", out);
    delete $("nxUpBrowserNote").dataset.busy;
    for (const [f, rel] of out) addFile(f, rel);
    afterAdd();
  }

  /* ---------- preview ("N files -> root/path/sub") ---------- */

  function queued() { return S.items.filter((i) => i.state === "queued" && !i.armed); }

  function drawPreview() {
    const box = $("nxUpPreview");
    const go = $("nxUpGo");
    const room = $("nxUpRoom");
    const c = cleanDir(S.path);
    const waiting = queued();
    const total = waiting.reduce((n, i) => n + i.size, 0);
    box.replaceChildren();
    room.hidden = true;
    if (!S.items.length) {
      box.textContent = "Nothing chosen yet.";
    } else if (!waiting.length) {
      box.textContent = S.running ? "Uploading…" : "Nothing waiting to be uploaded.";
    } else {
      const head = document.createElement("div");
      head.className = "nx-up-preview-head";
      head.textContent = c.error ? "Fix the folder in step 1 first." : `${plural(waiting.length, "file")} (${sizeText(total)}) will be saved in:`;
      box.appendChild(head);
      if (!c.error) {
        const groups = new Map();
        for (const i of waiting) {
          let g = groups.get(i.rel);
          if (!g) groups.set(i.rel, g = { n: 0, bytes: 0 });
          g.n++;
          g.bytes += i.size;
        }
        const rows = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
        for (const [rel, g] of rows.slice(0, 8)) {
          const line = document.createElement("div");
          line.className = "nx-up-preview-line";
          const to = document.createElement("span");
          to.className = "nx-up-to";
          to.textContent = destLabel(S.root, S.path, rel);
          const n = document.createElement("span");
          n.className = "nx-up-muted";
          n.textContent = `${plural(g.n, "file")}, ${sizeText(g.bytes)}`;
          line.append(to, n);
          box.appendChild(line);
        }
        if (rows.length > 8) {
          const more = document.createElement("div");
          more.className = "nx-up-muted";
          more.textContent = `…and ${plural(rows.length - 8, "more folder")}`;
          box.appendChild(more);
        }
      }
    }
    // Room on the disk: the server refuses too; this says it before sending.
    const L = S.limits;
    let blocked = !!c.error || !waiting.length || S.running;
    if (L && L.free_bytes != null && total && L.free_bytes - total < L.min_free_bytes) {
      room.textContent = `Not enough room: ${sizeText(total)} to upload, ${binText(L.free_bytes)} free, and ${binText(L.min_free_bytes)} must stay free. Remove some files from the list.`;
      room.hidden = false;
      blocked = true;
    }
    if (L && L.writable === false) {
      room.textContent = "The server cannot write to that library folder (read-only).";
      room.hidden = false;
      blocked = true;
    }
    go.disabled = blocked;
    $("nxUpClear").disabled = S.running || !S.items.length;
    $("nxUpCancelAll").disabled = !S.running && !S.items.some((i) => i.armed && i.state === "queued");
    const failed = S.items.filter((i) => i.state === "error" || i.state === "cancelled").length;
    $("nxUpRetry").hidden = !failed || S.running;
    $("nxUpRetry").textContent = `Retry ${plural(failed, "failed file")}`;
  }

  /* ---------- the queue list ---------- */

  const STATE_TEXT = { queued: "Waiting", uploading: "Uploading", done: "Saved", error: "Failed", cancelled: "Cancelled" };

  function rowFor(it) {
    const row = document.createElement("div");
    row.className = "nx-up-row";
    const main = document.createElement("div");
    main.className = "nx-up-row-main";
    const name = document.createElement("div");
    name.className = "nx-up-row-name";
    name.textContent = (it.rel ? it.rel + "/" : "") + it.name;
    name.title = name.textContent;
    const meta = document.createElement("div");
    meta.className = "nx-up-row-meta";
    main.append(name, meta);
    const bar = document.createElement("div");
    bar.className = "nx-up-bar nx-up-row-bar";
    const fill = document.createElement("div");
    bar.appendChild(fill);
    const act = document.createElement("div");
    act.className = "nx-up-row-actions";
    row.append(main, bar, act);
    it.row = { el: row, meta, fill, act, bar };
    paintRow(it);
    return row;
  }

  function paintRow(it) {
    const r = it.row;
    if (!r) return;
    r.el.className = `nx-up-row is-${it.state}`;
    const bits = [sizeText(it.size)];
    if (it.state === "uploading") bits.push(`${Math.round((it.sent / it.size) * 100)}%`);
    else if (it.state === "queued") bits.push(it.armed ? "Waiting to start" : "Not started");
    else if (it.state === "done") {
      bits.push("Saved");
      if (it.result && it.result.saved_as && it.result.saved_as !== it.name) bits.push(`saved as ${it.result.saved_as}`);
    } else if (it.error) bits.push(it.error);
    else bits.push(STATE_TEXT[it.state]);
    r.meta.textContent = bits.join(" · ");
    r.meta.title = [bits.join(" · "), ...it.notes].join("\n");
    r.fill.style.width = `${it.state === "done" ? 100 : it.size ? Math.min(100, (it.sent / it.size) * 100) : 0}%`;
    r.bar.hidden = !(it.state === "uploading" || it.state === "done");
    r.act.replaceChildren();
    const btn = (label, title, fn) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "nx-up-icon";
      b.textContent = label;
      b.title = title;
      b.setAttribute("aria-label", `${title}: ${it.name}`);
      b.addEventListener("click", fn);
      r.act.appendChild(b);
    };
    if (it.state === "queued" || it.state === "uploading") btn("✕", it.state === "queued" && !it.armed ? "Remove from the list" : "Cancel", () => cancelItem(it));
    else if (it.state === "error" || it.state === "cancelled") {
      btn("↻", "Retry", () => retryItems([it]));
      btn("✕", "Remove from the list", () => removeItem(it));
    } else if (it.state === "done" && it.result && it.result.duplicate_of) {
      const tag = document.createElement("span");
      tag.className = "nx-up-tag";
      tag.textContent = "exact copy";
      tag.title = "Identical to " + it.result.duplicate_of.join(", ");
      r.act.appendChild(tag);
    }
  }

  function redrawQueue() {
    const list = $("nxUpQueue");
    list.replaceChildren();
    S.shown = 0;
    for (const it of S.items) it.row = null;
    moreRows();
    $("nxUpClear").disabled = S.running || !S.items.length;
  }

  function moreRows() {
    const list = $("nxUpQueue");
    const next = S.items.slice(S.shown, S.shown + PAGE);
    const frag = document.createDocumentFragment();
    for (const it of next) frag.appendChild(rowFor(it));
    list.appendChild(frag);
    S.shown += next.length;
    const left = S.items.length - S.shown;
    $("nxUpMore").hidden = left <= 0;
    $("nxUpMoreBtn").textContent = `Show ${Math.min(PAGE, left)} more of ${left} left`;
  }

  /* ---------- running ---------- */

  let paintTick = false;
  function schedulePaint() {
    if (paintTick) return;
    paintTick = true;
    requestAnimationFrame(() => { paintTick = false; paintAll(); });
  }

  function paintAll() {
    for (const b of S.active) for (const it of b.items) paintRow(it);
    drawTotal();
  }

  function drawTotal() {
    const box = $("nxUpTotal");
    if (!S.run) { box.hidden = true; return; }
    const mine = S.items.filter((i) => i.runStamp === S.run.start);
    const total = mine.reduce((n, i) => n + i.size, 0);
    const sent = mine.reduce((n, i) => n + (i.state === "done" ? i.size : i.sent), 0);
    const done = mine.filter((i) => i.state === "done").length;
    const bad = mine.filter((i) => i.state === "error" || i.state === "cancelled").length;
    box.hidden = false;
    const pct = total ? Math.min(100, (sent / total) * 100) : 0;
    $("nxUpTotalFill").style.width = `${pct}%`;
    $("nxUpTotalBar").setAttribute("aria-valuenow", String(Math.round(pct)));
    const now = performance.now();
    S.samples.push([now, sent]);
    while (S.samples.length > 2 && now - S.samples[0][0] > 4000) S.samples.shift();
    let speed = "";
    if (S.running && S.samples.length > 1) {
      const [t0, b0] = S.samples[0];
      const dt = (now - t0) / 1000;
      if (dt > 0.5 && sent >= b0) speed = ` · ${sizeText((sent - b0) / dt)}/s`;
    }
    $("nxUpTotalText").textContent = `${plural(done, "file")} of ${mine.length} saved${bad ? `, ${bad} failed or cancelled` : ""} · ${sizeText(sent)} of ${sizeText(total)}${speed}`;
  }

  function nextBatch() {
    const L = S.limits || {};
    const maxFiles = Math.min(BATCH_FILES, L.max_files_per_request || BATCH_FILES);
    const items = [];
    let bytes = 0;
    for (const it of S.items) {
      if (it.state !== "queued" || !it.armed) continue;
      if (items.length && (items.length >= maxFiles || bytes + it.size > BATCH_BYTES)) break;
      items.push(it);
      bytes += it.size;
      if (bytes >= BATCH_BYTES) break;
    }
    return items.length ? { items, bytes } : null;
  }

  function pump() {
    while (S.active.size < PARALLEL) {
      const b = nextBatch();
      if (!b) break;
      sendBatch(b);
    }
    if (!S.active.size && !nextBatch()) finishRun();
    drawPreview();
    schedulePaint();
  }

  function startRun() {
    const c = cleanDir(S.path);
    if (c.error || S.running) return;
    const waiting = queued();
    if (!waiting.length) return;
    const stamp = Date.now();
    S.run = { start: stamp, root: S.root, path: c.parts.join("/"), saved: 0, failed: 0, bytes: 0, renamed: [], dups: [], failedList: [], pipeline: null };
    S.samples = [];
    S.summary = null;
    $("nxUpSummary").hidden = true;
    for (const it of waiting) { it.armed = true; it.runStamp = stamp; it.dest = { root: S.run.root, path: S.run.path }; it.sent = 0; }
    S.running = true;
    setLocked(true);
    pump();
  }

  function setLocked(on) {
    for (const id of ["nxUpRoot", "nxUpPath", "nxUpNewFolderBtn"]) $(id).disabled = on;
    $("nxUpBrowser").classList.toggle("is-locked", on);
    $("nxUpCrumbs").classList.toggle("is-locked", on);
  }

  function sendBatch(batch) {
    const items = batch.items;
    const dest = items[0].dest;
    // One request holds one destination; items armed under another one (a later run) wait.
    const same = items.filter((i) => i.dest.root === dest.root && i.dest.path === dest.path);
    batch.items = same;
    const fd = new FormData();
    fd.append("root", dest.root);
    fd.append("path", dest.path);
    for (const it of same) {
      fd.append("cid", it.cid);
      if (it.rel) fd.append("rel", it.rel);
      fd.append("mtime", String(it.file.lastModified || 0));
      fd.append("size", String(it.size));
      fd.append("files", it.file, it.name);
      it.state = "uploading";
      it.sent = 0;
      it.error = "";
    }
    const xhr = new XMLHttpRequest();
    batch.xhr = xhr;
    S.active.add(batch);
    xhr.open("POST", "/api/media/upload");
    xhr.responseType = "text";
    xhr.upload.onprogress = (e) => {
      let before = 0;
      for (const it of same) {
        it.sent = Math.max(0, Math.min(it.size, e.loaded - before));
        before += it.size + BODY_OVERHEAD;
      }
      schedulePaint();
    };
    xhr.onload = () => {
      S.active.delete(batch);
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
      if (xhr.status === 401) { window.location.reload(); return; }
      if (xhr.status >= 200 && xhr.status < 300 && data && Array.isArray(data.results)) {
        applyResults(batch, data);
      } else {
        const why = (data && data.error) || `The server answered ${xhr.status}`;
        for (const it of same) failItem(it, why);
      }
      pump();
    };
    xhr.onerror = () => {
      S.active.delete(batch);
      for (const it of same) failItem(it, "Connection lost. If part of it arrived it is not saved twice: retrying is safe.");
      pump();
    };
    xhr.onabort = () => {
      S.active.delete(batch);
      for (const it of same) {
        if (it.state !== "uploading") continue;
        if (it.cancelRequested) { it.state = "cancelled"; it.armed = false; it.sent = 0; it.error = "Cancelled"; }
        else if (batch.cancelAll) { it.state = "cancelled"; it.armed = false; it.sent = 0; it.error = "Cancelled"; }
        else { it.state = "queued"; it.sent = 0; }       // only its neighbour was cancelled: goes again
        paintRow(it);
      }
      pump();
    };
    xhr.send(fd);
  }

  function applyResults(batch, data) {
    const run = S.run;
    batch.items.forEach((it, i) => {
      const r = data.results[i];
      if (!r) { failItem(it, "The server sent no answer for this file"); return; }
      it.result = r;
      it.notes = r.notes || [];
      if (r.ok) {
        it.state = "done";
        it.sent = it.size;
        it.armed = false;
        if (run) {
          run.saved++;
          run.bytes += r.size || it.size;
          if (r.saved_as && r.saved_as !== it.name) run.renamed.push({ from: (it.rel ? it.rel + "/" : "") + it.name, to: r.saved_as, note: (r.notes || []).join("; ") });
          if (r.duplicate_of && r.duplicate_of.length) run.dups.push({ name: r.path || it.name, of: r.duplicate_of });
        }
      } else {
        failItem(it, r.error || "The server refused this file");
      }
      paintRow(it);
    });
    if (run && data.pipeline) run.pipeline = data.pipeline;
    if (data.interrupted) toast("The upload was cut short on the server: the files not marked Saved were not kept. Retry them.");
  }

  function failItem(it, why) {
    it.state = "error";
    it.armed = false;
    it.sent = 0;
    it.error = why;
    if (S.run && it.runStamp === S.run.start) {
      S.run.failed++;
      S.run.failedList.push({ name: (it.rel ? it.rel + "/" : "") + it.name, error: why });
    }
    paintRow(it);
  }

  function cancelItem(it) {
    if (it.state === "queued") {
      if (!it.armed) { removeItem(it); return; }
      it.state = "cancelled";
      it.armed = false;
      it.error = "Cancelled";
      paintRow(it);
      drawPreview();
      return;
    }
    if (it.state === "uploading") {
      it.cancelRequested = true;
      for (const b of S.active) if (b.items.includes(it)) { b.xhr.abort(); return; }
    }
  }

  function cancelAll() {
    for (const it of S.items) {
      if (it.state === "queued" && it.armed) { it.state = "cancelled"; it.armed = false; it.error = "Cancelled"; paintRow(it); }
    }
    for (const b of [...S.active]) { b.cancelAll = true; b.xhr.abort(); }
    if (!S.active.size) pump();
  }

  function removeItem(it) {
    const i = S.items.indexOf(it);
    if (i < 0) return;
    S.items.splice(i, 1);
    if (it.row) { it.row.el.remove(); S.shown = Math.max(0, S.shown - 1); }
    drawPreview();
    if (!S.items.length) $("nxUpMore").hidden = true;
  }

  function retryItems(list) {
    if (S.running) return;
    for (const it of list) {
      it.state = "queued";
      it.armed = false;
      it.error = "";
      it.cancelRequested = false;
      it.sent = 0;
      paintRow(it);
    }
    drawPreview();
    toast(`${plural(list.length, "file")} back in the list. Press Upload to send ${list.length === 1 ? "it" : "them"} again.`);
  }

  /* ---------- the end of a run ---------- */

  function finishRun() {
    if (!S.running) return;
    S.running = false;
    setLocked(false);
    const run = S.run;
    S.summary = run;
    drawSummary(run);
    if (run && run.saved) libraryChanged();
  }

  function libraryChanged() {
    try { if (typeof resetRecentMedia === "function") resetRecentMedia(); } catch (e) { /* older media.js */ }
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function drawSummary(run) {
    const box = $("nxUpSummary");
    box.replaceChildren();
    if (!run) { box.hidden = true; return; }
    const to = destLabel(run.root, run.path, "");
    const cancelled = S.items.filter((i) => i.runStamp === run.start && i.state === "cancelled").length;
    const head = el("div", "nx-up-summary-head");
    head.appendChild(el("strong", "", `${plural(run.saved, "file")} saved (${sizeText(run.bytes)}) in ${to}`));
    if (run.failed) head.appendChild(el("span", "nx-up-bad", ` · ${run.failed} failed`));
    if (cancelled) head.appendChild(el("span", "nx-up-muted", ` · ${cancelled} cancelled`));
    box.appendChild(head);
    if (run.saved) {
      const open = el("button", "btn nx-up-open", "Open this folder");
      open.type = "button";
      open.addEventListener("click", () => openFolder(run.root, run.path));
      box.appendChild(open);
    }
    const list = (title, rows) => {
      if (!rows.length) return;
      const d = el("details", "nx-up-detail");
      d.open = rows.length <= 5;
      d.appendChild(el("summary", "", title));
      const ul = el("ul");
      for (const t of rows.slice(0, 30)) ul.appendChild(el("li", "", t));
      if (rows.length > 30) ul.appendChild(el("li", "nx-up-muted", `…and ${rows.length - 30} more`));
      d.appendChild(ul);
      box.appendChild(d);
    };
    list(`${plural(run.renamed.length, "file")} saved under another name (the name was taken or had to be made safe)`,
      run.renamed.map((r) => `${r.from} → saved as ${r.to}`));
    list(`${plural(run.dups.length, "picture")} identical to ones already in the library (kept, nothing removed)`,
      run.dups.map((d) => `${d.name} = ${d.of.slice(0, 2).join(", ")}${d.of.length > 2 ? " …" : ""}`));
    list(`${plural(run.failedList.length, "file")} not saved`, run.failedList.map((f) => `${f.name}: ${f.error}`));
    if (run.saved) {
      const p = run.pipeline || {};
      const bad = Object.entries(p).filter(([, v]) => String(v).startsWith("failed"));
      box.appendChild(el("div", "nx-up-muted nx-up-after",
        "They are in Recent and the search now, like a finished download. The tagger, face and character passes pick them up on their next sweep."
        + (bad.length ? ` A follow-up step failed on the server: ${bad.map(([k, v]) => `${k} (${v})`).join("; ")}.` : "")));
    }
    box.hidden = false;
  }

  async function openFolder(root, path) {
    const folders = $("navFolders");
    if (folders) folders.click();
    await new Promise((r) => setTimeout(r, 40));
    try { if (typeof loadMedia === "function") loadMedia(path, true, root); } catch (e) { /* ignore */ }
    window.scrollTo(0, 0);
  }

  /* ---------- opening ---------- */

  async function open() {
    const pre = S.preselect;
    S.preselect = null;
    if (!S.opened || !S.roots.length) {
      await loadRoots();
      S.opened = true;
    }
    if (pre) {
      S.root = S.roots.some((r) => r.id === pre.root) ? pre.root : S.root || "downloads";
      $("nxUpRoot").value = S.root;
      S.path = "";
      setPath(pre.path || "");
    } else if (!S.path && !S.root) {
      S.root = (S.roots[0] && S.roots[0].id) || "downloads";
      $("nxUpRoot").value = S.root;
      setPath("");
    } else {
      $("nxUpRoot").value = S.root;
      setPath(S.path);
    }
    drawSkipped();
    redrawQueue();
    if (S.summary && !S.running) drawSummary(S.summary);
    drawTotal();
  }

  // The folder view's "Upload here": remember the folder, then go to the destination (or refresh it
  // when it is already open).
  function go(opts) {
    if (S.running) { toast("An upload is running: its destination cannot change until it ends."); }
    else S.preselect = { root: (opts && opts.root) || "", path: (opts && opts.path) || "" };
    if (document.body.dataset.dest === "upload") open();
    else $("navUpload").click();
  }

  /* ---------- wiring ---------- */

  function wire() {
    const drop = $("nxUpDrop");
    const fileInput = $("nxUpFileInput");
    const folderInput = $("nxUpFolderInput");
    drop.addEventListener("click", () => fileInput.click());
    drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
    drop.addEventListener("dragenter", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
    drop.addEventListener("dragleave", (e) => { if (!drop.contains(e.relatedTarget)) drop.classList.remove("is-over"); });
    drop.addEventListener("drop", onDrop);
    // A file dropped beside the zone must not make the browser open it.
    window.addEventListener("dragover", (e) => { if (document.body.dataset.dest === "upload") e.preventDefault(); });
    window.addEventListener("drop", (e) => { if (document.body.dataset.dest === "upload" && !drop.contains(e.target)) e.preventDefault(); });
    $("nxUpPickFiles").addEventListener("click", () => fileInput.click());
    $("nxUpPickFolder").addEventListener("click", () => folderInput.click());
    fileInput.addEventListener("change", () => { addFiles(Array.from(fileInput.files || []), "files"); fileInput.value = ""; });
    folderInput.addEventListener("change", () => { addFiles(Array.from(folderInput.files || []), "folder"); folderInput.value = ""; });

    $("nxUpRoot").addEventListener("change", (e) => setRoot(e.target.value));
    let t = 0;
    $("nxUpPath").addEventListener("input", (e) => {
      clearTimeout(t);
      t = setTimeout(() => setPath(e.target.value, { keepInput: true }), 250);
    });
    $("nxUpPath").addEventListener("keydown", (e) => { if (e.key === "Enter") { clearTimeout(t); setPath(e.target.value); } });

    $("nxUpNewFolderBtn").addEventListener("click", () => {
      const f = $("nxUpNewFolder");
      f.hidden = !f.hidden;
      if (!f.hidden) $("nxUpNewFolderName").focus();
    });
    $("nxUpNewFolderCancel").addEventListener("click", () => { $("nxUpNewFolder").hidden = true; });
    $("nxUpNewFolder").addEventListener("submit", (e) => {
      e.preventDefault();
      const name = $("nxUpNewFolderName").value.trim();
      if (!name) return;
      const c = cleanDir([...cleanDirParts(S.path), name].join("/"));
      const err = $("nxUpPathError");
      if (c.error) { err.textContent = c.error; err.hidden = false; return; }
      $("nxUpNewFolderName").value = "";
      $("nxUpNewFolder").hidden = true;
      setPath(c.parts.join("/"));
    });

    $("nxUpGo").addEventListener("click", startRun);
    $("nxUpCancelAll").addEventListener("click", cancelAll);
    $("nxUpRetry").addEventListener("click", () => retryItems(S.items.filter((i) => i.state === "error" || i.state === "cancelled")));
    $("nxUpClear").addEventListener("click", () => {
      if (S.running) return;
      S.items = [];
      S.skipped.clear();
      S.run = null;
      S.summary = null;
      $("nxUpSummary").hidden = true;
      $("nxUpTotal").hidden = true;
      afterAdd();
    });
    $("nxUpMoreBtn").addEventListener("click", moreRows);

    const here = $("nxUploadHere");
    if (here) {
      here.addEventListener("click", () => {
        const roots = (typeof currentMediaRoot === "string" ? currentMediaRoot : "") || "downloads";
        go({ root: roots, path: typeof currentMediaPath === "string" ? currentMediaPath : "" });
      });
    }
    window.addEventListener("beforeunload", (e) => {
      if (!S.running) return;
      e.preventDefault();
      e.returnValue = "";
    });
  }

  window.NextUpload = {
    open() { open(); },
    go,
    // For the tests: what is on screen and in memory.
    stats: () => ({ items: S.items.length, shown: S.shown, running: S.running, active: S.active.size, root: S.root, path: S.path, skipped: [...S.skipped] }),
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
