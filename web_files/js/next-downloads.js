"use strict";

/* The Downloads destination of the media page (admin only): add links, watch and steer the download queue,
 * see per-site pipes, system load and the library indexing. A client for the same routes the old dashboard
 * used - POST /api/download, GET /api/tasks, POST /api/task/<id>/(terminate|prioritize), GET /api/pipes,
 * POST /api/pipe/<site>/purge, GET /api/system-stats, GET /api/media/indexing, GET /api/downloads/cookies.
 *
 * Rows are keyed by task id and updated in place, so a text selection or a scroll position survives the
 * once-a-second refresh; nothing the downloader says goes through innerHTML. Classic script; the shell
 * (next-shell.js) calls NextDownloads.open() when the destination opens. */

(function () {
  const $ = (id) => document.getElementById(id);
  const root = $("nxDownloads");
  if (!root) return;

  const SITES = [
    ["youtube.com", "YouTube"], ["youtu.be", "YouTube"], ["instagram.com", "Instagram"], ["instagr.am", "Instagram"],
    ["twitter.com", "Twitter / X"], ["x.com", "Twitter / X"], ["pixiv.net", "Pixiv"], ["pixiv.", "Pixiv"],
    ["danbooru", "Danbooru"], ["civitai.com", "Civitai"], ["patreon.com", "Patreon"], ["bsky.app", "Bluesky"],
    ["bluesky", "Bluesky"], ["deviantart.com", "DeviantArt"], ["discord.com", "Discord"],
    ["discordapp.com", "Discord"], ["bilibili.com", "Bilibili"], ["b23.tv", "Bilibili"],
  ];
  const SKIP_LABELS = {
    age_restricted: "age-restricted", members_only: "members-only", private: "private",
    geo_blocked: "blocked in this country", not_released: "not released yet", unavailable: "unavailable",
  };
  const TABS = ["active", "done", "failed", "sites", "system"];

  const S = {
    tab: "active",
    tasks: [],
    byId: new Map(),
    pipes: null,
    drawerId: null,
    busy: false,
    indexAt: 0,
    cookieAt: 0,
  };
  try { const t = localStorage.getItem("nxDlTab"); if (TABS.includes(t)) S.tab = t; } catch (e) { /* private mode */ }

  // ---------------------------------------------------------------- small helpers

  const nf = new Intl.NumberFormat("en-GB");
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }
  function siteOf(line) {
    const t = line.trim();
    if (!t) return "";
    if (t.startsWith("@")) return "Twitter / X";
    let host = "";
    try { host = new URL(/^[a-z]+:\/\//i.test(t) ? t : "https://" + t).hostname.toLowerCase(); } catch (e) { return ""; }
    for (const [needle, name] of SITES) if (host === needle || host.endsWith("." + needle) || host.includes(needle)) return name;
    return "";
  }
  function elapsed(epoch) {
    if (!epoch) return "";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
    if (s < 60) return s + " s";
    if (s < 3600) return Math.floor(s / 60) + " min";
    if (s < 86400) return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min";
    return Math.floor(s / 86400) + " d";
  }
  function bytes(n) {
    if (!n) return "0 MB";
    const gb = n / 1024 ** 3;
    return gb >= 1 ? gb.toFixed(2) + " GB" : (n / 1024 ** 2).toFixed(1) + " MB";
  }
  function bitrate(mib) {
    const kbit = mib * 1024 * 8;
    return kbit >= 1000 ? (kbit / 1000).toFixed(2) + " Mbit/s" : kbit.toFixed(1) + " Kbit/s";
  }
  function diskText(used, total) {
    return used >= 1024 || total >= 1024
      ? (used / 1024).toFixed(2) + " / " + (total / 1024).toFixed(2) + " TB"
      : used.toFixed(1) + " / " + total.toFixed(1) + " GB";
  }
  async function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  }
  const kindOf = (t) => (t.status === "Running" || t.status === "Queued") ? "active"
    : t.status === "Completed" ? "done" : "failed";

  // ---------------------------------------------------------------- the add box

  const urlBox = $("nxDlUrl"), siteChip = $("nxDlSite"), formNote = $("nxDlFormNote");
  function grow() {
    urlBox.style.height = "auto";
    urlBox.style.height = Math.min(urlBox.scrollHeight, 170) + "px";
  }
  function paintSiteChip() {
    const lines = urlBox.value.split("\n").map((l) => l.trim()).filter(Boolean);
    let text = "";
    if (lines.length === 1) text = siteOf(lines[0]);
    else if (lines.length > 1) text = lines.length + " links";
    siteChip.hidden = !text;
    siteChip.textContent = text;
  }
  urlBox.addEventListener("input", () => { grow(); paintSiteChip(); formNote.hidden = true; });
  urlBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("nxDlForm").requestSubmit(); }
  });
  $("nxDlForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const lines = urlBox.value.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.length) return;
    const btn = $("nxDlGo");
    btn.disabled = true;
    const failed = [], problems = [];
    for (const url of lines) {
      try {
        const res = await post("/api/download", { url });
        if (!res.ok) {
          let why = "refused";
          try { why = (await res.json()).error || why; } catch (err) { /* no body */ }
          failed.push(url); problems.push(why);
        }
      } catch (err) { failed.push(url); problems.push("no answer from the server"); }
    }
    btn.disabled = false;
    urlBox.value = failed.join("\n");
    grow(); paintSiteChip();
    if (failed.length) {
      formNote.textContent = failed.length === 1 ? problems[0]
        : failed.length + " links were not accepted (" + [...new Set(problems)].join("; ") + ") - they are still in the box.";
      formNote.hidden = false;
    } else formNote.hidden = true;
    if (failed.length < lines.length) { setTab("active"); refresh(true); }
  });

  // ---------------------------------------------------------------- tabs

  function setTab(tab) {
    S.tab = tab;
    try { localStorage.setItem("nxDlTab", tab); } catch (e) { /* ignore */ }
    for (const b of root.querySelectorAll("[data-dltab]")) b.setAttribute("aria-selected", b.dataset.dltab === tab ? "true" : "false");
    $("nxDlList").hidden = !["active", "done", "failed"].includes(tab);
    $("nxDlSites").hidden = tab !== "sites";
    $("nxDlSystem").hidden = tab !== "system";
    paintList();
    refresh(true);
  }
  root.querySelector(".nx-dl-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-dltab]");
    if (b) setTab(b.dataset.dltab);
  });

  // ---------------------------------------------------------------- the task list

  const list = $("nxDlList");
  function actionBtn(act, label, cls) {
    const b = el("button", "nx-dl-btn" + (cls ? " " + cls : ""), label);
    b.type = "button"; b.dataset.act = act;
    return b;
  }
  function paintActions(box, t) {
    box.textContent = "";
    if (t.media_path) box.append(actionBtn("files", "Files"));
    if (t.status === "Failed" || t.status === "Terminated") box.append(actionBtn("rerun", "Run again"));
    if (t.status === "Queued") box.append(actionBtn("next", "Next"));
    if (t.status === "Running" || t.status === "Queued") box.append(actionBtn("stop", "Stop", "is-danger"));
  }
  function skipText(t) {
    const skipped = Object.entries(t.skipped || {});
    if (!skipped.length || String(t.progress || "").includes("skipped:")) return "";
    const signedOut = t.cookies_signed_out && (t.skipped.age_restricted || t.skipped.members_only);
    return "Skipped: " + skipped.sort((a, b) => b[1] - a[1])
      .map(([r, n]) => n + " " + (r === "error" ? (n === 1 ? "other error" : "other errors") : (SKIP_LABELS[r] || r))).join(" · ")
      + (signedOut ? " - YouTube cookies are signed out" : "");
  }
  function makeRow(t) {
    const row = el("div", "nx-dl-row");
    row.dataset.id = t.id;
    row.tabIndex = 0;
    const head = el("div", "nx-dl-rowhead");
    head.append(el("span", "nx-dl-site-tag"), el("span", "nx-dl-state"));
    const url = el("div", "nx-dl-url");
    const prog = el("div", "nx-dl-prog");
    const bar = el("div", "nx-dl-bar"); bar.append(el("div", "nx-dl-barfill"));
    const skip = el("div", "nx-dl-skip"); skip.hidden = true;
    const foot = el("div", "nx-dl-rowfoot");
    foot.append(el("span", "nx-dl-when"), el("span", "nx-dl-actions"));
    row.append(head, url, bar, prog, skip, foot);
    return row;
  }
  function fillRow(row, t) {
    const [tag, state] = row.querySelector(".nx-dl-rowhead").children;
    setText(tag, t.category || "");
    tag.dataset.site = t.category || "";
    setText(state, t.status);
    row.dataset.status = t.status;
    setText(row.querySelector(".nx-dl-url"), t.url);
    setText(row.querySelector(".nx-dl-prog"), t.progress || "");
    const m = /(\d+(?:\.\d+)?)\s*%/.exec(t.progress || "");
    const bar = row.querySelector(".nx-dl-bar");
    bar.hidden = !(t.status === "Running" && m);
    if (m) bar.firstChild.style.width = Math.min(100, parseFloat(m[1])) + "%";
    const sk = row.querySelector(".nx-dl-skip"), st = skipText(t);
    sk.hidden = !st; setText(sk, st);
    const when = t.status === "Running" ? "running for " + elapsed(t.started_at)
      : t.status === "Queued" ? "queued for " + elapsed(t.created_at) : "";
    setText(row.querySelector(".nx-dl-when"), when);
    const actions = row.querySelector(".nx-dl-actions");
    if (actions.dataset.shape !== t.status + (t.media_path ? "f" : "")) {
      paintActions(actions, t);
      actions.dataset.shape = t.status + (t.media_path ? "f" : "");
    }
  }
  function paintList() {
    const counts = { active: 0, done: 0, failed: 0 };
    for (const t of S.tasks) counts[kindOf(t)]++;
    for (const k of Object.keys(counts)) {
      const c = root.querySelector('[data-dltab="' + k + '"] .nx-dl-count');
      if (c) setText(c, counts[k] ? String(counts[k]) : "");
    }
    const running = S.tasks.filter((t) => t.status === "Running").length;
    setText($("nxDlSum"), S.tasks.length ? running + " running · " + (counts.active - running) + " queued" : "");
    if (list.hidden) return;
    const shown = S.tasks.filter((t) => kindOf(t) === S.tab).reverse();
    const keep = new Set(shown.map((t) => String(t.id)));
    for (const row of [...list.querySelectorAll(".nx-dl-row")]) if (!keep.has(row.dataset.id)) row.remove();
    let prev = null;
    for (const t of shown) {
      let row = list.querySelector('.nx-dl-row[data-id="' + CSS.escape(String(t.id)) + '"]');
      if (!row) row = makeRow(t);
      fillRow(row, t);
      const want = prev ? prev.nextElementSibling : list.firstElementChild;
      if (row !== want) list.insertBefore(row, want);
      prev = row;
    }
    let empty = list.querySelector(".nx-dl-empty");
    if (!shown.length) {
      if (!empty) { empty = el("div", "nx-dl-empty"); list.append(empty); }
      setText(empty, S.tab === "active" ? "Nothing is downloading. Paste a link above to start."
        : S.tab === "done" ? "Nothing has finished yet." : "Nothing has failed.");
    } else if (empty) empty.remove();
  }

  async function act(kind, t) {
    if (kind === "files") window.open("/media.html?path=" + encodeURIComponent(t.media_path), "_blank");
    else if (kind === "stop") await post("/api/task/" + t.id + "/terminate");
    else if (kind === "next") await post("/api/task/" + t.id + "/prioritize");
    else if (kind === "rerun") await post("/api/download", { url: t.url });
    if (kind !== "files") refresh(true);
  }
  list.addEventListener("click", (e) => {
    const row = e.target.closest(".nx-dl-row");
    if (!row) return;
    const t = S.byId.get(row.dataset.id);
    if (!t) return;
    const b = e.target.closest("[data-act]");
    if (b) { e.stopPropagation(); b.disabled = true; act(b.dataset.act, t); return; }
    if (window.getSelection && String(window.getSelection())) return;   // selecting text is not a click
    openDrawer(t.id);
  });
  list.addEventListener("keydown", (e) => {
    const row = e.target.closest(".nx-dl-row");
    if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openDrawer(row.dataset.id); }
  });

  // ---------------------------------------------------------------- the log drawer

  const drawer = $("nxDlDrawer"), logBox = $("nxDlLog");
  function paintDrawer() {
    const t = S.byId.get(String(S.drawerId));
    if (!t) { closeDrawer(); return; }
    setText($("nxDlDrawerTitle"), t.category + " · " + t.status);
    setText($("nxDlDrawerUrl"), t.url);
    const text = t.logs && t.logs.length ? t.logs.join("\n") : "No log yet.";
    if (logBox.textContent !== text) {
      const near = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40;
      logBox.textContent = text;
      if (near) logBox.scrollTop = logBox.scrollHeight;
    }
  }
  function openDrawer(id) {
    S.drawerId = String(id);
    drawer.hidden = false;
    root.classList.add("has-drawer");
    paintDrawer();
    logBox.scrollTop = logBox.scrollHeight;
  }
  function closeDrawer() { S.drawerId = null; drawer.hidden = true; root.classList.remove("has-drawer"); }
  $("nxDlDrawerClose").addEventListener("click", closeDrawer);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !drawer.hidden && document.body.dataset.dest === "downloads") {
      e.stopPropagation(); e.preventDefault(); closeDrawer();
    }
  }, true);

  // ---------------------------------------------------------------- sites (pipes)

  function renderSites(pipes) {
    const box = $("nxDlSites");
    const cats = Object.keys(pipes).sort((a, b) => (pipes[b].weekly_usage || 0) - (pipes[a].weekly_usage || 0));
    let prev = null;
    for (const cat of cats) {
      const p = pipes[cat];
      let row = box.querySelector('.nx-dl-site-row[data-cat="' + CSS.escape(cat) + '"]');
      if (!row) {
        row = el("div", "nx-dl-site-row");
        row.dataset.cat = cat;
        const head = el("div", "nx-dl-site-head");
        const tag = el("span", "nx-dl-site-tag", cat);
        tag.dataset.site = cat;
        const meta = el("span", "nx-dl-site-meta");
        const purge = actionBtn("purge", "Clear queue");
        purge.addEventListener("click", async () => { purge.disabled = true; await post("/api/pipe/" + encodeURIComponent(cat) + "/purge"); refresh(true); });
        head.append(tag, meta, purge);
        row.append(head, el("div", "nx-dl-site-now"));
        row.querySelector(".nx-dl-site-now").hidden = true;
      }
      setText(row.querySelector(".nx-dl-site-meta"),
        p.queued + " queued · " + p.rate_mbps + " MiB/s · " + p.items_per_hr + "/h · used " + p.weekly_usage + "× this week");
      const now = row.querySelector(".nx-dl-site-now");
      const cur = p.current_task;
      now.hidden = !cur;
      if (cur) setText(now, (cur.progress || "Running") + " · " + cur.url + (cur.started_at ? " · " + elapsed(cur.started_at) : ""));
      const want = prev ? prev.nextElementSibling : box.firstElementChild;
      if (row !== want) box.insertBefore(row, want);
      prev = row;
    }
    for (const row of [...box.querySelectorAll(".nx-dl-site-row")]) if (!cats.includes(row.dataset.cat)) row.remove();
  }

  // ---------------------------------------------------------------- system and indexing

  function renderStats(s) {
    const used = s.disk_total_gb - s.disk_free_gb;
    const tiles = [
      ["CPU", s.cpu_percent + "%"], ["Memory", s.ram_used_gb + " / " + s.ram_total_gb + " GB"],
      ["Network in", bitrate(s.net_recv_mbps)], ["Network out", bitrate(s.net_sent_mbps)],
      ["Files, last hour", String(s.items_last_60min)], ["Downloaded, 24 h", bytes(s.downloaded_bytes_24h)],
      ["Disk", diskText(used, s.disk_total_gb) + " (" + s.disk_used_percent + "%)"],
    ];
    const box = $("nxDlStats");
    while (box.children.length > tiles.length) box.lastChild.remove();
    tiles.forEach(([label, value], i) => {
      let tile = box.children[i];
      if (!tile) { tile = el("div", "nx-dl-stat"); tile.append(el("div", "nx-dl-stat-l"), el("div", "nx-dl-stat-v")); box.append(tile); }
      setText(tile.children[0], label); setText(tile.children[1], value);
    });
  }

  const PASSES = [["captioned", "caption_done", "Descriptions"], ["ocr", "ocr_done", "Text in images"],
    ["tagged", "tags_done", "Tags and characters"], ["faces_files", "faces_done", "Faces"]];
  function renderIndexing(d) {
    const box = $("nxDlIndex");
    box.textContent = "";
    const total = d.indexable || d.library_files || 0;
    const state = d.state || (d.running ? "indexing" : "stopped");
    const current = { "caption,ocr": "captioned", caption: "captioned", ocr: "ocr", tags: "tagged", faces: "faces_files" }[d.current_task];
    for (const [key, doneKey, label] of PASSES) {
      const done = (typeof d[doneKey] === "number" ? d[doneKey] : d[key]) || 0;
      const row = el("div", "nx-dl-pass" + (state === "indexing" && key === current ? " is-active" : ""));
      const head = el("div", "nx-dl-pass-head");
      head.append(el("span", null, label), el("span", "nx-dl-pass-n", nf.format(done) + " / " + nf.format(total)));
      const bar = el("div", "nx-dl-bar"); const fill = el("div", "nx-dl-barfill");
      fill.style.width = (total ? Math.min(100, done * 100 / total) : 0).toFixed(2) + "%";
      bar.append(fill); row.append(head, bar); box.append(row);
    }
    const w = d.worker || {};
    const bits = [state === "indexing" ? "indexing " + (d.current_task || "?")
      : state === "idle" ? "idle, checking for new files every few minutes"
      : state === "waiting" ? "waiting for the worker's processing window" : "indexer stopped"];
    if (!w.reachable) bits.push("worker unreachable");
    else if (w.loaded && w.loaded.length) bits.push("model: " + w.loaded.join(", "));
    else bits.push("models unloaded (idle)");
    const sw = d.sweep || {};
    const span = (sw.at || 0) - (sw.started || 0);
    const rate = state === "indexing" && span > 60 ? ((sw.done || 0) + (sw.failed || 0)) / span : (w.seconds_per_image ? 1 / w.seconds_per_image : 0);
    if (rate) {
      bits.push(rate.toFixed(1) + " files/s");
      const left = state === "indexing" ? (d.left || 0) : 0;
      if (left) { const h = left / rate / 3600; bits.push(h >= 1 ? "~" + h.toFixed(1) + " h left in this pass" : "~" + Math.round(h * 60) + " min left in this pass"); }
    }
    if (w.in_window === false) bits.push("outside the window (" + (w.windows || "") + ")");
    box.append(el("div", "nx-dl-note", bits.join(" · ")));
    const found = [];
    if (d.characters) found.push(nf.format(d.characters) + " characters");
    if (d.people) found.push(nf.format(d.people) + " people");
    if (d.faces) found.push(nf.format(d.faces) + " faces");
    if (found.length) box.append(el("div", "nx-dl-note", found.join(" · ")));
  }

  // ---------------------------------------------------------------- cookies hint

  async function checkCookies() {
    S.cookieAt = Date.now();
    const box = $("nxDlCookie");
    try {
      const res = await fetch("/api/downloads/cookies");
      if (!res.ok) return;
      const yt = (await res.json()).youtube || {};
      box.hidden = !(yt.present && !yt.signed_in);
      if (box.hidden) return;
      const a = el("a", null, "How to fix");
      a.href = "/settings.html#downloads";
      box.replaceChildren("YouTube cookies are signed out - age-restricted and members-only videos will be skipped. ", a);
    } catch (e) { /* next check */ }
  }

  // ---------------------------------------------------------------- refresh loop

  async function refresh(force) {
    if (S.busy && !force) return;
    if (document.body.dataset.dest !== "downloads" || document.hidden) return;
    if (!force && window.getSelection && String(window.getSelection())) return;   // never repaint under a selection
    S.busy = true;
    try {
      const res = await fetch("/api/tasks");
      if (res.status === 401) { location.reload(); return; }
      S.tasks = await res.json();
      S.byId = new Map(S.tasks.map((t) => [String(t.id), t]));
      paintList();
      if (S.drawerId) paintDrawer();
      if (S.tab === "sites") {
        const r = await fetch("/api/pipes");
        if (r.ok) { S.pipes = await r.json(); renderSites(S.pipes); }
      } else if (S.tab === "system") {
        const r = await fetch("/api/system-stats");
        if (r.ok) renderStats(await r.json());
        if (Date.now() - S.indexAt > 5000) {
          S.indexAt = Date.now();
          const i = await fetch("/api/media/indexing");
          if (i.ok) renderIndexing(await i.json());
          else $("nxDlIndex").textContent = "Indexing status unavailable.";
        }
      }
      if (Date.now() - S.cookieAt > 60000) checkCookies();
    } catch (e) { /* the next tick tries again */ } finally { S.busy = false; }
  }
  setInterval(() => refresh(false), 1500);

  window.NextDownloads = {
    open() {
      // The shell sets body[data-dest] right after calling this; refresh() wants it.
      setTimeout(() => { setTab(S.tab); checkCookies(); }, 0);
    },
  };
})();
