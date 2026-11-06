"use strict";

/* The Trash destination of media-next.html (admin only). A client for three routes that existed
 * without a page: GET /api/media/trash (every batch, newest first, each with the relative paths it
 * holds), POST /api/media/trash/<batch>/restore, DELETE /api/media/trash/<batch>?root=.
 *
 * A batch is what one delete made: a hand delete from the dialog or the review queue, or one run of
 * an automatic job (the picture de-duplication makes hundreds). So the list is filtered by where a
 * batch came from (chips, "Deleted by hand" first) and rendered a page at a time - 1000 batches
 * with their file lists stay a few dozen rows in the DOM.
 *
 * Nothing is erased except by the one button in the confirmation dialog: purge() has exactly one
 * caller, that button's handler. Restoring is not destructive and asks nothing.
 *
 * Classic script; the shell (next-shell.js) calls NextTrash.open() when the destination opens. */

(function () {
  const $ = (id) => document.getElementById(id);
  const PAGE = 40;                    // rows added per step
  const PREVIEWS = 3;                 // thumbnails per hand-made batch

  const SOURCES = [
    ["manual", "Deleted by hand"],
    ["dedup", "Image dedup"],
    ["blacklist", "Character blacklist"],
    ["auto", "Other automatic"],
    ["other", "Other"],
  ];
  const BULK = new Set(["dedup", "blacklist", "auto"]);     // too many and too alike for thumbnails

  let batches = [];
  let view = [];                      // batches after the chip and the search
  let shown = 0;                      // how many of `view` are in the DOM
  let source = null;                  // null until chosen: "Deleted by hand" when there is any, else all
  let query = "";
  let loadGen = 0;

  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

  function sizeText(bytes) {
    const b = Number(bytes) || 0;
    if (b >= 1e9) return `${(b / 1e9).toFixed(1)} GB`;
    if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
    if (b >= 1e3) return `${Math.round(b / 1e3)} KB`;
    return `${b} B`;
  }

  function dateText(b) {
    if (!b.deleted_at) return "Date unknown";
    const d = new Date(b.deleted_at * 1000);
    const days = Math.floor((Date.now() - d.getTime()) / 86400000);
    const ago = days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
    return `${d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} (${ago})`;
  }

  function sourceOf(b) {
    const reason = String(b.reason || "").toLowerCase();
    const by = String(b.deleted_by || "").toLowerCase();
    if (reason.startsWith("image dedup") || by.includes("image dedup")) return "dedup";
    if (reason.startsWith("character blacklist") || by.includes("character blacklist")) return "blacklist";
    if (by.startsWith("auto")) return "auto";
    if (reason) return "other";
    return "manual";
  }

  function labelOf(b) {
    if (b.reason) return b.reason;
    return b.deleted_by ? `Deleted by ${b.deleted_by}` : "Deleted";
  }

  function toast(msg) {
    const el = $("selectToast");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "block";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.style.display = "none"; }, 9000);
  }

  /* ---------- loading ---------- */

  function setStatus(msg) {
    const el = $("nxTrashStatus");
    el.textContent = msg || "";
    el.hidden = !msg;
  }

  async function load() {
    const gen = ++loadGen;
    setStatus("Loading…");
    let data;
    try {
      const res = await fetch("/api/media/trash", { cache: "no-store" });
      if (res.status === 401) { window.location.reload(); return; }
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
      data = Array.isArray(body) ? body : [];
    } catch (err) {
      if (gen !== loadGen) return;
      batches = [];
      render();
      setStatus(`Could not load the trash: ${err.message}`);
      return;
    }
    if (gen !== loadGen) return;
    batches = data;
    for (const b of batches) { b._src = sourceOf(b); b.items = b.items || []; }
    // The newest first, as the server sends them; a batch with no date goes last.
    batches.sort((a, b) => (b.deleted_at || 0) - (a.deleted_at || 0));
    const manual = batches.some((b) => b._src === "manual");
    if (source === null || (source !== "all" && !batches.some((b) => b._src === source))) source = manual ? "manual" : "all";
    setStatus("");
    render();
  }

  /* ---------- the list ---------- */

  function applyFilter() {
    const q = query.trim().toLowerCase();
    view = batches.filter((b) => {
      if (source !== null && source !== "all" && b._src !== source) return false;
      if (!q) return true;
      if (b._hay === undefined) b._hay = b.items.join("\n").toLowerCase();     // built once, on first search
      return b._hay.includes(q);
    });
  }

  function drawSummary() {
    const files = batches.reduce((n, b) => n + (b.file_count || 0), 0);
    const bytes = batches.reduce((n, b) => n + (b.size || 0), 0);
    $("nxTrashSum").textContent = batches.length
      ? `${plural(batches.length, "batch", "batches")} · ${plural(files, "file")} · ${sizeText(bytes)}`
      : "";
  }

  function drawChips() {
    const box = $("nxTrashChips");
    box.replaceChildren();
    if (!batches.length) return;
    const counts = new Map();
    for (const b of batches) counts.set(b._src, (counts.get(b._src) || 0) + 1);
    const chips = [["all", "All", batches.length]];
    for (const [key, label] of SOURCES) if (counts.has(key)) chips.push([key, label, counts.get(key)]);
    for (const [key, label, n] of chips) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "nx-share-chip";
      chip.setAttribute("aria-pressed", String(source === key));
      chip.textContent = `${label} ${n}`;
      chip.addEventListener("click", () => { source = key; render(); });
      box.appendChild(chip);
    }
  }

  function emptyNote() {
    const p = document.createElement("p");
    p.className = "nx-trash-empty";
    if (!batches.length) {
      p.textContent = "The trash is empty. Files you delete wait here until you delete them for good.";
    } else if (query.trim()) {
      p.textContent = "No batch holds a file with that name in this view.";
    } else {
      p.textContent = "Nothing from this source.";
    }
    return p;
  }

  function render() {
    applyFilter();
    drawSummary();
    drawChips();
    const list = $("nxTrashList");
    list.replaceChildren();
    shown = 0;
    if (!view.length) {
      if (!$("nxTrashStatus").textContent.startsWith("Could not")) list.appendChild(emptyNote());
      $("nxTrashMore").hidden = true;
      return;
    }
    more();
  }

  function more() {
    const list = $("nxTrashList");
    const next = view.slice(shown, shown + PAGE);
    const frag = document.createDocumentFragment();
    for (const b of next) frag.appendChild(rowFor(b));
    list.appendChild(frag);
    shown += next.length;
    const left = view.length - shown;
    const bar = $("nxTrashMore");
    bar.hidden = left <= 0;
    $("nxTrashMoreBtn").textContent = `Show ${Math.min(PAGE, left)} more of ${left} left`;
  }

  // Scrolling near the bottom adds the next page by itself; the button is the fallback. A scroll
  // listener rather than an IntersectionObserver: it is a few lines, and a hidden or throttled tab
  // does not deliver observer callbacks or animation frames.
  let scrollTick = false;
  function nearEnd() {
    scrollTick = false;
    const bar = $("nxTrashMore");
    if (bar.hidden || document.body.dataset.dest !== "trash") return;
    if (bar.getBoundingClientRect().top < window.innerHeight + 400) more();
  }
  window.addEventListener("scroll", () => {
    if (scrollTick) return;
    scrollTick = true;
    setTimeout(nearEnd, 80);
  }, { passive: true });

  const thumbUrl = (b, rel) => `/api/media/thumbnail?path=${encodeURIComponent(`.trash/${b.batch}/${rel}`)}&root=${encodeURIComponent(b.root || "")}`;
  const baseName = (p) => String(p).split("/").pop();

  function rowFor(b) {
    const row = document.createElement("article");
    row.className = "nx-trash-row";
    row.dataset.batch = b.batch;
    const n = b.file_count || 0;

    const thumbs = document.createElement("div");
    thumbs.className = "nx-trash-thumbs";
    if (!BULK.has(b._src)) {
      const media = b.items.filter((p) => /\.(jpe?g|png|gif|webp|bmp|avif|mp4|webm|mkv|mov)$/i.test(p)).slice(0, PREVIEWS);
      for (const rel of media) {
        const img = document.createElement("img");
        img.loading = "lazy";
        img.decoding = "async";
        img.alt = "";
        img.title = rel;
        img.src = thumbUrl(b, rel);
        img.addEventListener("error", () => img.remove(), { once: true });     // a preview is a courtesy
        thumbs.appendChild(img);
      }
    }
    row.appendChild(thumbs);

    const body = document.createElement("div");
    body.className = "nx-trash-body";
    const title = document.createElement("div");
    title.className = "nx-trash-title";
    title.textContent = labelOf(b);
    const meta = document.createElement("div");
    meta.className = "nx-trash-meta";
    meta.textContent = [dateText(b), plural(n, "file"), sizeText(b.size),
      b.deleted_by && !b.reason ? "" : (b.deleted_by ? `by ${b.deleted_by}` : "")].filter(Boolean).join(" · ");
    body.append(title, meta);
    if (b.mode === "ban" || b.mode === "release") {
      const badge = document.createElement("span");
      badge.className = "nx-trash-badge" + (b.mode === "ban" ? " is-ban" : "");
      badge.textContent = b.mode === "ban" ? "Names banned" : "Can be downloaded again";
      badge.title = b.mode === "ban" ? "The downloader will not fetch these again" : "The download record was cleared: a later run of the same URL may fetch them again";
      body.appendChild(badge);
    }
    if (b.items.length) {
      const names = document.createElement("div");
      names.className = "nx-trash-names";
      const first = b.items.slice(0, 3).map(baseName);
      names.textContent = first.join(", ") + (b.items.length > 3 ? ` and ${b.items.length - 3} more` : "");
      names.title = b.items.slice(0, 30).join("\n");
      body.appendChild(names);
    }
    row.appendChild(body);

    const actions = document.createElement("div");
    actions.className = "nx-trash-actions";
    const restoreBtn = document.createElement("button");
    restoreBtn.type = "button";
    restoreBtn.className = "btn nx-trash-restore";
    restoreBtn.textContent = "Restore";
    restoreBtn.addEventListener("click", () => restore(b, row));
    const purgeBtn = document.createElement("button");
    purgeBtn.type = "button";
    purgeBtn.className = "btn btn-quiet nx-trash-purge";
    purgeBtn.textContent = "Delete forever…";
    purgeBtn.addEventListener("click", () => askPurge(b, row, purgeBtn));
    actions.append(restoreBtn, purgeBtn);
    row.appendChild(actions);
    return row;
  }

  function setRowBusy(row, on) {
    for (const btn of row.querySelectorAll("button")) btn.disabled = on;
    row.classList.toggle("is-busy", on);
  }

  // A batch left the trash (restored whole, or deleted forever): out of the data and the DOM.
  function dropBatch(b, row) {
    const i = batches.indexOf(b);
    if (i >= 0) batches.splice(i, 1);
    const j = view.indexOf(b);
    if (j >= 0) { view.splice(j, 1); if (j < shown) shown--; }
    row.remove();
    drawSummary();
    drawChips();
    if (!batches.some((x) => x._src === source) && source !== "all") { source = "all"; render(); return; }
    if (!view.length) render();
    else if (shown < PAGE && shown < view.length) more();
    else $("nxTrashMore").hidden = view.length - shown <= 0;
  }

  /* ---------- restore ---------- */

  // The folder grids and Recent were drawn before the files came back; refresh them in the
  // background so they are right when the user goes there.
  function libraryChanged() {
    try { if (typeof loadMedia === "function" && !searchModeActive && !currentView) loadMedia(currentMediaPath, false, currentMediaRoot); } catch (e) { /* older media.js */ }
    try { if (typeof resetRecentMedia === "function") resetRecentMedia(); } catch (e) { /* ditto */ }
  }

  async function restore(b, row) {
    setRowBusy(row, true);
    let data;
    try {
      const res = await fetch(`/api/media/trash/${encodeURIComponent(b.batch)}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root: b.root }),
      });
      data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      setRowBusy(row, false);
      toast(`Nothing was restored: ${err.message}`);
      return;
    }
    const restored = (data.restored || []).length;
    const failed = data.failed || [];
    if (restored) libraryChanged();
    if (!failed.length) {
      dropBatch(b, row);
      toast(`Restored ${plural(restored, "file")} to where ${restored === 1 ? "it was" : "they were"}`);
      return;
    }
    // Part of the batch could not go back: what is left stays here, and the page says so.
    const left = failed.map((f) => f.path).filter(Boolean);
    b.items = left.length ? left : b.items;
    b.file_count = failed.length;
    b._hay = undefined;
    const why = Array.from(new Set(failed.map((f) => f.error).filter(Boolean))).slice(0, 2).join("; ");
    toast(`Restored ${plural(restored, "file")}, ${failed.length} could not be put back${why ? ` (${why})` : ""}. They stay in the trash.`);
    row.replaceWith(rowFor(b));
    drawSummary();
  }

  /* ---------- delete forever ---------- */

  let confirmCtx = null;       // {go: fn, opener}

  function openConfirm(opts) {
    const dlg = $("nxConfirm");
    $("nxConfirmTitle").textContent = opts.title;
    $("nxConfirmText").textContent = opts.text;
    $("nxConfirmStatus").textContent = "";
    $("nxConfirmGo").textContent = opts.go;
    $("nxConfirmGo").disabled = false;
    $("nxConfirmCancel").disabled = false;
    confirmCtx = { go: opts.onGo, opener: opts.opener, busy: false };
    dlg.hidden = false;
    $("nxConfirmCancel").focus();         // Enter does the safe thing
  }

  function closeConfirm() {
    if (confirmCtx && confirmCtx.busy) return;     // a request is in flight: let it end
    $("nxConfirm").hidden = true;
    const opener = confirmCtx && confirmCtx.opener;
    confirmCtx = null;
    if (opener && opener.isConnected) opener.focus();
  }

  function askPurge(b, row, opener) {
    const n = b.file_count || 0;
    openConfirm({
      title: `Delete ${plural(n, "file")} forever?`,
      text: `This permanently deletes ${plural(n, "file")} (${sizeText(b.size)}) from the disk: "${labelOf(b)}", ${dateText(b)}. It cannot be undone and they can no longer be restored.`,
      go: `Delete ${plural(n, "file")} forever`,
      opener,
      onGo: () => purge(b, row),
    });
  }

  // The only request in this file that erases anything, and only the confirm button calls it.
  async function purge(b, row) {
    confirmCtx.busy = true;
    $("nxConfirmGo").disabled = true;
    $("nxConfirmCancel").disabled = true;
    $("nxConfirmStatus").textContent = "Deleting…";
    try {
      const res = await fetch(`/api/media/trash/${encodeURIComponent(b.batch)}?root=${encodeURIComponent(b.root || "")}`, { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      confirmCtx.busy = false;
      $("nxConfirmGo").disabled = false;
      $("nxConfirmCancel").disabled = false;
      $("nxConfirmStatus").textContent = `Nothing was deleted: ${err.message}`;
      return;
    }
    confirmCtx.busy = false;
    closeConfirm();
    dropBatch(b, row);
    toast(`Deleted forever: ${plural(b.file_count || 0, "file")} (${sizeText(b.size)} freed)`);
  }

  /* ---------- wiring ---------- */

  function wire() {
    $("nxTrashRefresh").addEventListener("click", load);
    $("nxTrashMoreBtn").addEventListener("click", more);
    let t = 0;
    $("nxTrashFind").addEventListener("input", (e) => {
      clearTimeout(t);
      t = setTimeout(() => { query = e.target.value; render(); }, 150);
    });
    $("nxConfirmCancel").addEventListener("click", closeConfirm);
    $("nxConfirmGo").addEventListener("click", () => { if (confirmCtx && !confirmCtx.busy) confirmCtx.go(); });
    $("nxConfirm").addEventListener("click", (e) => { if (e.target === $("nxConfirm")) closeConfirm(); });
    window.addEventListener("keydown", (e) => {
      if ($("nxConfirm").hidden) return;
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeConfirm(); }
      else if (e.key === "Tab") {                  // the two buttons are the whole dialog
        const btns = [$("nxConfirmCancel"), $("nxConfirmGo")].filter((b) => !b.disabled);
        if (!btns.length) { e.preventDefault(); return; }
        const i = btns.indexOf(document.activeElement);
        e.preventDefault();
        btns[(i + (e.shiftKey ? -1 : 1) + btns.length) % btns.length].focus();
      }
    }, true);
  }

  window.NextTrash = {
    open() { load(); },
    // For the tests: what is on screen and in memory.
    stats: () => ({ batches: batches.length, view: view.length, shown, rows: $("nxTrashList").children.length }),
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
