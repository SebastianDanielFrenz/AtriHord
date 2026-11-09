"use strict";
/* Browser side of tests/ui/stereo3d_ui_check.py. Loads the REAL media and
 * settings pages in iframes (same origin, served by the stub server, which
 * answers /api/* from a scripted configuration and logs every request) and
 * drives them like a person would: right-click, click, select mode. Each
 * check is reported to the stub server (/__stub/result). */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function stub(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return res.json();
}
const configure = (cfg) => stub("/__stub/config", cfg);
const requests = async (prefix) => (await stub("/__stub/log")).filter((r) => !prefix || r.path.startsWith(prefix));
const clearLog = () => stub("/__stub/clear", {});

let checks = 0;
async function check(name, ok, detail) {
  checks += 1;
  await stub("/__stub/result", { name, ok: !!ok, detail: ok ? "" : String(detail === undefined ? "" : detail) });
}

async function waitFor(fn, ms = 6000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try { const v = fn(); if (v) return v; } catch (e) { /* not there yet */ }
    await sleep(50);
  }
  return null;
}

let frame = null;
async function openPage(url) {
  if (frame) frame.remove();
  frame = document.createElement("iframe");
  frame.style.cssText = "width:1200px;height:800px;border:0";
  document.body.appendChild(frame);
  await new Promise((resolve) => { frame.onload = resolve; frame.src = url; });
  return frame.contentWindow;
}

// Top-level let/const of a classic script are not window properties.
const peek = (w, name) => w.eval(name);

const tileOf = (w, path) => w.document.querySelector(`#mediaGrid .media-tile[data-path="${path}"]`);

async function menuFor(w, tile) {
  tile.dispatchEvent(new w.MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 120, clientY: 120 }));
  const menu = w.document.getElementById("mediaContextMenu");
  await sleep(250);          // the status answer turns the row into the item's status
  return { menu, rows: () => Array.from(menu.querySelectorAll(".context-menu-item")) };
}
const labels = (m) => m.rows().map((r) => r.textContent);
const find3d = (m) => m.rows().find((r) => /3D/.test(r.textContent) && !/^Open/.test(r.textContent));

const FILES = [
  { name: "a.mp4", type: "video" }, { name: "b.jpg", type: "image" }, { name: "c.mp3", type: "audio" },
  { name: "d.gif", type: "image" }, { name: "e.png", type: "image" },
].map((f, i) => ({ ...f, size: 1000 + i, mtime: 1.7e9 + i, added: 1.7e9 + i, root: "downloads", path: f.name,
  url: `/api/media/file?root=downloads&path=${f.name}`, favorite: false }));

const KEY = "0123456789abcdef0123456789abcdef";
const DONE = (path, kind) => ({ state: "done", key: KEY, kind, path, sbs_url: `/api/media/3d/file?key=${KEY}&name=${kind === "video" ? "sbs.mp4" : "sbs.jpg"}`,
  depth_url: `/api/media/3d/file?key=${KEY}&name=${kind === "video" ? "depth.mp4" : "depth.png"}` });

async function errorsOf(w) { return (w.__errors || []).slice(); }

async function mediaPageAdmin() {
  await configure({ role: "admin", files: FILES, status: {} });
  await clearLog();
  const w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4") && peek(w, "viewerIsAdmin"));
  await check("media: grid and admin role loaded", tileOf(w, "a.mp4") && peek(w, "viewerIsAdmin") === true, "no grid / not admin");

  // ---- context menu
  let m = await menuFor(w, tileOf(w, "a.mp4"));
  await check("menu: video offers Convert to 3D", find3d(m) && find3d(m).textContent === "Convert to 3D", labels(m));
  await clearLog();
  find3d(m).click();
  await sleep(300);
  let posts = (await requests("/api/media/3d/convert"));
  await check("menu: click POSTs one convert for that video",
    posts.length === 1 && posts[0].method === "POST"
    && JSON.stringify(posts[0].body.items) === JSON.stringify([{ root: "downloads", path: "a.mp4" }]), JSON.stringify(posts));
  const toast = w.document.getElementById("selectToast");
  await check("menu: the answer is shown as a message", /1 queued/.test(toast.textContent) && toast.style.display === "block", toast.textContent);

  m = await menuFor(w, tileOf(w, "b.jpg"));
  await check("menu: picture offers Convert to 3D", !!find3d(m), labels(m));
  m = await menuFor(w, tileOf(w, "c.mp3"));
  await check("menu: audio does not", !find3d(m), labels(m));
  m = await menuFor(w, tileOf(w, "d.gif"));
  await check("menu: an unsupported picture type (gif) does not", !find3d(m), labels(m));
  m = await menuFor(w, w.document.querySelector("#mediaGrid .media-tile[data-kind='folder']"));
  await check("menu: a folder does not", !find3d(m), labels(m));

  // ---- status in the menu
  await configure({ status: {
    "a.mp4": { state: "queued", position: 2, lane: "manual" },
    "b.jpg": { state: "converting", phase: "converting", progress: { frames_done: 420, frames_total: 1000, eta_s: 600 } },
    "e.png": { state: "failed", error: "worker out of disk" },
  } });
  m = await menuFor(w, tileOf(w, "a.mp4"));
  await check("menu: queued shows its position", labels(m).includes("Queued for 3D (no. 2)"), labels(m));
  await clearLog();
  find3d(m).click(); await sleep(200);
  await check("menu: a queued item's row does not send another request", (await requests("/api/media/3d/convert")).length === 0, "sent");
  m = await menuFor(w, tileOf(w, "b.jpg"));
  await check("menu: converting shows phase, percent and eta", labels(m).some((l) => /converting 42 %, about 10 min left/.test(l)), labels(m));
  m = await menuFor(w, tileOf(w, "e.png"));
  await check("menu: failed says why and offers retry", labels(m).some((l) => /worker out of disk.* - retry/.test(l)), labels(m));
  await clearLog();
  find3d(m).click(); await sleep(300);
  await check("menu: retry sends a convert for that item", (await requests("/api/media/3d/convert")).length === 1, "none");

  await configure({ status: { "a.mp4": DONE("a.mp4", "video") } });
  m = await menuFor(w, tileOf(w, "a.mp4"));
  await check("menu: done shows ready plus both links", labels(m).includes("3D ready") && labels(m).includes("Open 3D (side by side)")
    && labels(m).includes("Open depth map"), labels(m));

  // ---- preview button
  await configure({ status: {} });
  tileOf(w, "b.jpg").click();
  await waitFor(() => w.document.getElementById("mediaPreviewModal").style.display === "flex");
  const btn = w.document.getElementById("mediaConvert3dBtn");
  await sleep(300);
  await check("preview: picture shows the 3D button", !btn.hidden && !btn.disabled, "hidden=" + btn.hidden);
  await clearLog();
  await configure({ status: { "b.jpg": { state: "queued", position: 1, lane: "manual" } } });
  btn.click();
  await sleep(600);
  posts = await requests("/api/media/3d/convert");
  await check("preview: button POSTs the open item", posts.length === 1
    && JSON.stringify(posts[0].body.items) === JSON.stringify([{ root: "downloads", path: "b.jpg" }]), JSON.stringify(posts));
  const out = w.document.getElementById("mediaConvert3dStatus");
  await check("preview: it then shows the queue status and disables the button",
    !out.hidden && /Queued for 3D \(no\. 1\)/.test(out.textContent) && btn.disabled, out.textContent + " disabled=" + btn.disabled);
  await configure({ status: { "b.jpg": DONE("b.jpg", "picture") } });
  w.eval("paint3dButton(activePreviewList[currentPreviewIndex])");
  await sleep(400);
  await check("preview: done shows no '3D ready' text and no side by side / depth links, the 3D button is disabled",
    out.hidden && out.textContent === "" && out.querySelectorAll("a").length === 0 && btn.disabled,
    `hidden=${out.hidden} text=${out.textContent}`);
  theme(w);
  await sleep(700);                                  // colour transitions
  await check("preview: a finished 3D version makes the 3D button the accent blue of the AI-upscale chip",
    btn.classList.contains("convert3d-ready") && colorOf(w, btn, "color") === cssColor(w, "var(--accent)")
    && colorOf(w, btn, "borderTopColor") === cssColor(w, "var(--accent)"), `${colorOf(w, btn, "color")} vs ${cssColor(w, "var(--accent)")} cls=${btn.className} style=${btn.getAttribute("style")}`);
  await configure({ status: {} });
  w.eval("paint3dButton(activePreviewList[currentPreviewIndex])");
  await sleep(400);
  await sleep(700);
  await check("preview: not converted keeps the neutral 3D button (no ready class, not the accent colour)",
    !btn.classList.contains("convert3d-ready") && colorOf(w, btn, "color") !== cssColor(w, "var(--accent)"), colorOf(w, btn, "color"));
  w.closeMediaPreview();
  await sleep(300);

  // preview of audio: hidden
  tileOf(w, "c.mp3").click();
  await waitFor(() => w.document.getElementById("mediaPreviewModal").style.display === "flex");
  await sleep(300);
  await check("preview: audio has no 3D button", btn.hidden, "visible");
  w.closeMediaPreview();
  await sleep(300);

  // ---- select mode
  await configure({ status: {} });
  w.document.getElementById("selectModeBtn").click();
  const conv = w.document.getElementById("selectConvert3dBtn");
  await check("select: Convert to 3D starts disabled", conv && conv.disabled, "enabled");
  const folder = w.document.querySelector("#mediaGrid .media-tile[data-kind='folder']");
  for (const t of [tileOf(w, "e.png"), tileOf(w, "c.mp3"), folder, tileOf(w, "a.mp4")]) t.click();
  await sleep(100);
  await check("select: enabled once something is selected", !conv.disabled, "disabled");
  await clearLog();
  conv.click();
  await sleep(500);
  posts = await requests("/api/media/3d/convert");
  await check("select: one POST, selection order kept, audio and folder left out",
    posts.length === 1 && JSON.stringify(posts[0].body.items.map((i) => i.path)) === JSON.stringify(["e.png", "a.mp4"]),
    JSON.stringify(posts));
  await check("select: the message names what was left out", /2 queued/.test(toast.textContent) && /2 not a video or picture/.test(toast.textContent), toast.textContent);
  await check("select: select mode is left", !w.document.body.classList.contains("select-mode"), "still on");

  w.document.getElementById("selectModeBtn").click();
  tileOf(w, "c.mp3").click();
  await clearLog();
  w.document.getElementById("selectConvert3dBtn").click();
  await sleep(300);
  await check("select: only audio selected sends nothing and says so", (await requests("/api/media/3d/convert")).length === 0
    && /Nothing to convert/.test(toast.textContent), toast.textContent);

  const errs = await errorsOf(w);
  await check("media (admin): no console errors", errs.length === 0, errs.join(" | "));
  await check("media (admin): no 'null'/'undefined' text in the menu", !/\bnull\b|undefined/.test(w.document.getElementById("mediaContextMenu").textContent), "found");
}

async function mediaPageGuest() {
  for (const role of ["guest", "user"]) {
    await configure({ role, files: FILES, status: {} });
    await clearLog();
    const w = await openPage("/media.html");
    await waitFor(() => tileOf(w, "a.mp4"));
    await sleep(400);
    const m = await menuFor(w, tileOf(w, "a.mp4"));
    await check(`${role}: no Convert to 3D in the menu`, !find3d(m), labels(m));
    tileOf(w, "b.jpg").click();
    await waitFor(() => w.document.getElementById("mediaPreviewModal").style.display === "flex");
    await sleep(300);
    await check(`${role}: no 3D button in the preview`, w.document.getElementById("mediaConvert3dBtn").hidden, "visible");
    await check(`${role}: no select mode button`, w.document.getElementById("selectModeBtn").style.display === "none", "visible");
    const calls = (await requests("/api/media/3d")).filter((r) => !/^\/api\/media\/3d\/(status|file|list)/.test(r.path));
    await check(`${role}: no 3D conversion/queue request was made (status reads are allowed)`, calls.length === 0, JSON.stringify(calls));
    const errs = await errorsOf(w);
    await check(`${role}: no console errors`, errs.length === 0, errs.join(" | "));
  }
}

const OVERVIEW = {
  enabled: true,
  items: [
    { key: "k1", root: "downloads", path: "movies/big film.mp4", kind: "video", lane: "manual", state: "submitted",
      attempts: 1, error: null, worker_state: "converting", progress: { phase: "converting", frames_done: 250, frames_total: 1000, eta_s: 1800 }, not_before: 0, at: 1 },
    { key: "k2", root: "downloads", path: "pics/one.jpg", kind: "picture", lane: "favorite", state: "queued",
      attempts: 0, error: null, worker_state: null, progress: null, not_before: 0, at: 2 },
    { key: "k3", root: "downloads", path: "pics/two.png", kind: "picture", lane: "manual", state: "failed",
      attempts: 2, error: "NVENC open failed", worker_state: null, progress: null, not_before: 0, at: 3 },
  ],
  cache: { dir: "/mnt/media/ksf-web-dl-3d-cache", problem: null, max_gb: 150, entries: 7, gb: 12.34 },
  worker: { error: null, status: { available: true, missing: null, disk_free_gb: 210.4, paused: false, counts: { converting: 1, waiting_gpu: 2 } }, retry_in_s: 0 },
  last_step: "polled the worker",
};
const EMPTY_OVERVIEW = { enabled: true, items: [], cache: { dir: null, problem: null, max_gb: null, entries: 0, gb: null },
  worker: { error: null, status: null, retry_in_s: 0 }, last_step: null };

async function settingsPage() {
  await configure({ role: "admin", overview: OVERVIEW });
  await clearLog();
  let w = await openPage("/settings.html#stereo3d");
  await waitFor(() => w.document.querySelector("#settingsMain .settings-table"));
  const main = () => w.document.getElementById("settingsMain");
  const text = () => main().textContent;
  await check("settings: 2D to 3D is in the navigation", Array.from(w.document.querySelectorAll("#settingsNav a")).some((a) => a.textContent === "2D to 3D"), "missing");
  await check("settings: the schema form is there", !!main().querySelector(".settings-form"), "no form");
  await check("settings: queue rows rendered", main().querySelectorAll(".settings-table tbody tr").length === 3, text());
  await check("settings: running row shows phase, percent and eta", /converting 25 %, about 30 min left/.test(text()), text());
  await check("settings: queued and failed rows", /Queued/.test(text()) && /Failed: NVENC open failed/.test(text()), text());
  await check("settings: cache size against the cap", /12\.34 GB/.test(text()) && /150 GB/.test(text()), text());
  await check("settings: worker answer", /ready/.test(text()) && /converting 1, waiting_gpu 2/.test(text()), text());
  await check("settings: no 'null'/'undefined' text", !/\bnull\b|undefined/.test(text()), text());

  const btn = (label, scope) => Array.from((scope || main()).querySelectorAll("button")).find((b) => b.textContent === label);
  const row = (name) => Array.from(main().querySelectorAll("tbody tr")).find((r) => r.textContent.includes(name));
  const confirms = [];
  let answer = true;
  w.confirm = (m) => { confirms.push(m); return answer; };

  await clearLog();
  btn("Retry", row("two.png")).click();
  await sleep(400);
  let calls = await requests("/api/media/3d/retry");
  await check("settings: Retry POSTs that key", calls.length === 1 && calls[0].body.key === "k3", JSON.stringify(calls));
  await check("settings: Retry asks no confirmation", confirms.length === 0, confirms.join());

  await clearLog();
  btn("Retry 1 failed").click();
  await sleep(400);
  calls = await requests("/api/media/3d/retry");
  await check("settings: Retry all failed POSTs all:true", calls.length === 1 && calls[0].body.all === true, JSON.stringify(calls));

  answer = false;
  await clearLog();
  btn("Remove", row("one.jpg")).click();
  await sleep(300);
  await check("settings: Remove asks first, and a No sends nothing", confirms.length === 1 && (await requests("/api/media/3d/queue")).length === 0, confirms.join());
  answer = true;
  btn("Remove", row("one.jpg")).click();
  await sleep(400);
  calls = await requests("/api/media/3d/queue");
  await check("settings: Remove DELETEs that key", calls.length === 1 && calls[0].method === "DELETE" && /key=k2$/.test(calls[0].path), JSON.stringify(calls));
  confirms.length = 0;
  btn("Remove", row("big film")).click();
  await sleep(300);
  await check("settings: running removal is confirmed with a warning", confirms.length === 1 && /Stop the running conversion/.test(confirms[0]), confirms.join());

  confirms.length = 0;
  await clearLog();
  btn("Clear queue").click();
  await sleep(400);
  calls = await requests("/api/media/3d/queue");
  await check("settings: Clear queue confirms then DELETEs all=1", confirms.length === 1 && calls.some((c) => /all=1/.test(c.path)), JSON.stringify([confirms, calls]));

  confirms.length = 0;
  answer = false;
  await clearLog();
  btn("Clear cache").click();
  await sleep(300);
  await check("settings: Clear cache declined sends nothing", confirms.length === 1 && /7 converted/.test(confirms[0]) && (await requests("/api/settings/3d/cache")).length === 0, confirms.join());
  answer = true;
  btn("Clear cache").click();
  await sleep(400);
  calls = await requests("/api/settings/3d/cache");
  await check("settings: Clear cache DELETEs the cache", calls.length === 1 && calls[0].method === "DELETE", JSON.stringify(calls));
  const errs = await errorsOf(w);
  await check("settings: no console errors", errs.length === 0, errs.join(" | "));

  // empty / sparse answer: nothing may print "null" or "undefined"
  await configure({ overview: EMPTY_OVERVIEW });
  w = await openPage("/settings.html#stereo3d");
  await waitFor(() => w.document.querySelector("#settingsMain .settings-stats"));
  await sleep(200);
  const t2 = w.document.getElementById("settingsMain").textContent;
  await check("settings (empty queue): says nothing is waiting", /Nothing is waiting/.test(t2), t2);
  await check("settings (empty queue): no 'null'/'undefined' text", !/\bnull\b|undefined|NaN/.test(t2), t2);
  const e2 = await errorsOf(w);
  await check("settings (empty queue): no console errors", e2.length === 0, e2.join(" | "));

  // problems are shown
  await configure({ overview: { ...EMPTY_OVERVIEW, cache: { ...EMPTY_OVERVIEW.cache, problem: "cache folder missing: run sudo install" },
    worker: { error: "connection refused", status: null, retry_in_s: 30 } } });
  w = await openPage("/settings.html#stereo3d");
  await waitFor(() => w.document.querySelector("#settingsMain .settings-stats"));
  const t3 = w.document.getElementById("settingsMain").textContent;
  await check("settings: cache problem and worker error are shown", /cache folder missing/.test(t3) && /connection refused/.test(t3) && /30 s/.test(t3), t3);
}


// ---------------------------------------------------------------- View in VR

// A fake navigator.xr for the page in the iframe (the stub server's injected hook reads parent.__fakeXr).
const xrState = { mode: "supported", calls: [], sessions: [] };
xrState.fakes = [];
window.__fakeXr = (win) => {
  if (xrState.mode === "missing") return undefined;
  if (win.__xrFake) return win.__xrFake;           // one stable object per page (it takes listeners)
  const listeners = [];
  const fake = win.__xrFake = {
    addEventListener: (t, f) => { if (t === "devicechange") listeners.push(f); },
    removeEventListener: (t, f) => { const i = listeners.indexOf(f); if (i >= 0) listeners.splice(i, 1); },
    fire: () => listeners.slice().forEach((f) => f()),
    probes: 0,
    isSessionSupported: async () => { fake.probes++; return xrState.mode !== "unsupported"; },
    requestSession: async () => {
      xrState.calls.push(Date.now());
      if (xrState.mode === "reject" || (xrState.mode === "reject-once" && xrState.calls.length === 1)) {
        throw new DOMException("no headset", "NotSupportedError");
      }
      if (xrState.mode === "invalid") throw new DOMException("There is already an existing immersive session", "InvalidStateError");
      if (xrState.mode === "denied") throw new DOMException("denied", "SecurityError");
      // A session object three.js cannot drive: the viewer must clean up and report, not leak.
      const sess = { ended: false, end: async () => { sess.ended = true; }, addEventListener() {}, removeEventListener() {} };
      xrState.sessions.push(sess);
      return sess;
    },
  };
  return fake;
};

// Computed colours: cssColor resolves any CSS colour (also var(--x)) the way the page would paint it.
function cssColor(w, v) { const d = w.document.createElement("span"); d.style.color = v; w.document.body.appendChild(d); const c = w.getComputedStyle(d).color; d.remove(); return c; }
// The theme variables come from the remote Stylehub (ksf.css), which the harness does not load: define the three used.
function theme(w) { const r = w.document.documentElement.style; r.setProperty("--accent", "#3987e5"); r.setProperty("--muted", "#898781"); r.setProperty("--line", "#2c2c2a"); r.setProperty("--accent-wash", "rgba(57, 135, 229, 0.13)"); }
function colorOf(w, el, prop) { return w.getComputedStyle(el)[prop]; }
const vrBtnOf = (w) => w.document.getElementById("mediaVrBtn");
const vrOff = (b) => b.getAttribute("aria-disabled") === "true";
const toastOf = (w) => w.document.getElementById("selectToast").textContent;
async function openPreview(w, path) {
  tileOf(w, path).click();
  await waitFor(() => w.document.getElementById("mediaPreviewModal").style.display === "flex");
  await sleep(500);
}
async function closePreview(w) { w.closeMediaPreview(); await sleep(300); }

async function vrButton() {
  const VIDEO_DONE = { ...DONE("a.mp4", "video"), eye_w: 1080, eye_h: 1440 };
  const PIC_DONE = { ...DONE("b.jpg", "picture"), eye_w: 1080, eye_h: 1440 };

  for (const role of ["admin", "user", "guest"]) {
    xrState.mode = "supported";
    await configure({ role, files: FILES, status: { "a.mp4": VIDEO_DONE, "b.jpg": PIC_DONE } });
    await clearLog();
    const w = await openPage("/media.html");
    await waitFor(() => tileOf(w, "a.mp4"));
    await sleep(300);
    await openPreview(w, "a.mp4");
    let b = vrBtnOf(w);
    await check(`vr (${role}): converted video with XR shows an enabled VR button`, !b.hidden && !b.disabled && !vrOff(b), `hidden=${b.hidden} disabled=${b.disabled}`);
    await check(`vr (${role}): the tooltip documents the controls`, /Trigger/.test(b.title) && /B\/Y/.test(b.title) && /A\/X/.test(b.title), b.title);
    await check(`vr (${role}): no separate hint element, button not aria-disabled`, !w.document.getElementById("mediaVrHint") && !vrOff(b), "present");
    await closePreview(w);
    await openPreview(w, "b.jpg");
    b = vrBtnOf(w);
    await check(`vr (${role}): converted picture shows an enabled VR button`, !b.hidden && !b.disabled, `hidden=${b.hidden} disabled=${b.disabled}`);
    await closePreview(w);
    const errs = await errorsOf(w);
    await check(`vr (${role}): no console errors`, errs.length === 0, errs.join(" | "));
  }

  // P8 V0: not converted (none / queued / failed) items get the button too (they open in 2D); audio does not
  xrState.mode = "supported";
  await configure({ role: "user", files: FILES, status: { "b.jpg": { state: "queued", position: 1 }, "e.png": { state: "failed", error: "x" } } });
  let w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  for (const [path, why] of [["a.mp4", "unconverted"], ["b.jpg", "queued"], ["e.png", "failed"]]) {
    await openPreview(w, path);
    const nb = vrBtnOf(w);
    await check(`vr: ${why} item has an enabled VR button (opens in 2D)`, !nb.hidden && !nb.disabled && !vrOff(nb), `hidden=${nb.hidden} disabled=${nb.disabled} aria=${nb.getAttribute("aria-disabled")}`);
    await closePreview(w);
  }
  await openPreview(w, "c.mp3");
  await check("vr: an audio item has no VR button", vrBtnOf(w).hidden, "visible");
  await closePreview(w);

  // XR missing: no VR button at all; XR unsupported: visible, aria-disabled, with the hint
  xrState.mode = "missing";
  await configure({ role: "user", files: FILES, status: { "a.mp4": VIDEO_DONE } });
  w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  await openPreview(w, "a.mp4");
  await check("vr (XR missing): neither the preview's nor the top bar's VR button is shown", vrBtnOf(w).hidden && w.document.getElementById("mediaVrTopBtn").hidden,
    `preview hidden=${vrBtnOf(w).hidden} top hidden=${w.document.getElementById("mediaVrTopBtn").hidden}`);
  const errsMissing = await errorsOf(w);
  await check("vr (XR missing): no console errors", errsMissing.length === 0, errsMissing.join(" | "));
  await closePreview(w);

  for (const mode of ["unsupported"]) {
    xrState.mode = mode;
    await configure({ role: "user", files: FILES, status: { "a.mp4": VIDEO_DONE } });
    w = await openPage("/media.html");
    await waitFor(() => tileOf(w, "a.mp4"));
    await openPreview(w, "a.mp4");
    const b = vrBtnOf(w);
    await check(`vr (XR ${mode}): button is shown but disabled`, !b.hidden && vrOff(b) && !b.disabled, `hidden=${b.hidden} aria=${b.getAttribute("aria-disabled")}`);
    await check(`vr (XR ${mode}): hint names SteamVR and the OpenXR flag`, /SteamVR/.test(b.title) && /webxr-runtime/.test(b.title) && /OpenXR/.test(b.title), b.title);
    const hdr = w.document.querySelector("#mediaPreviewModal .modal-header").textContent;
    await check(`vr (XR ${mode}): no visible 'VR not available' text, only the button`, !/not available/i.test(hdr) && !/3D ready/.test(hdr), hdr);
    const errs = await errorsOf(w);
    await check(`vr (XR ${mode}): no console errors, no null/undefined/NaN text`, errs.length === 0
      && !/null|undefined|NaN/.test(w.document.querySelector("#mediaPreviewModal .modal-header").textContent), errs.join(" | "));
    await closePreview(w);
  }

  // clicking: the right file is requested, requestSession is retried once, the error is clean, nothing leaks
  for (const [path, kind, name] of [["a.mp4", "video", "sbs.mp4"], ["b.jpg", "picture", "sbs.jpg"]]) {
    xrState.mode = "reject"; xrState.calls = []; xrState.sessions = [];
    await configure({ role: "user", files: FILES, status: { [path]: DONE(path, kind) } });
    w = await openPage("/media.html");
    await waitFor(() => tileOf(w, path));
    await openPreview(w, path);
    await clearLog();
    const t0 = Date.now();
    vrBtnOf(w).click();
    await waitFor(() => /SteamVR/.test(toastOf(w)), 8000);
    await check(`vr click (${kind}): requestSession tried twice, ~1.5 s apart`, xrState.calls.length === 2
      && xrState.calls[1] - xrState.calls[0] >= 1300, JSON.stringify(xrState.calls.map((c) => c - t0)));
    await check(`vr click (${kind}): clean error message`, /VR could not be started/.test(toastOf(w)) && !/null|undefined|NaN|Error:/.test(toastOf(w)), toastOf(w));
    const files = (await requests("/api/media/3d/file")).map((r) => r.path);
    await check(`vr click (${kind}): fetches ${name} of the item's key, not the depth map`,
      files.some((f) => f.includes(`key=${KEY}`) && f.includes(`name=${name}`)) && !files.some((f) => /name=depth/.test(f)), JSON.stringify(files));
    await check(`vr click (${kind}): canvas removed, button usable again`, !w.document.getElementById("vrViewerCanvas") && !vrBtnOf(w).disabled,
      `canvas=${!!w.document.getElementById("vrViewerCanvas")} disabled=${vrBtnOf(w).disabled}`);
    const errs = await errorsOf(w);
    await check(`vr click (${kind}): no console errors`, errs.length === 0, errs.join(" | "));
    await closePreview(w);
  }

  // permission denied: its own message, no retry
  xrState.mode = "denied"; xrState.calls = [];
  await configure({ role: "user", files: FILES, status: { "a.mp4": VIDEO_DONE } });
  w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  await openPreview(w, "a.mp4");
  vrBtnOf(w).click();
  await waitFor(() => /blocked/.test(toastOf(w)), 5000);
  await check("vr click: a denied permission says so and is not retried", xrState.calls.length === 1 && /blocked/.test(toastOf(w)), toastOf(w));
  await closePreview(w);

  // an old immersive session is still alive: InvalidStateError gets its own clear text, no retry
  xrState.mode = "invalid"; xrState.calls = [];
  await configure({ role: "user", files: FILES, status: { "a.mp4": VIDEO_DONE } });
  w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  await openPreview(w, "a.mp4");
  vrBtnOf(w).click();
  await waitFor(() => /already running/.test(toastOf(w)), 5000);
  await check("vr click: an already-existing session says so on the page and is not retried", xrState.calls.length === 1
    && /another tab or window/.test(toastOf(w)) && !vrBtnOf(w).disabled, toastOf(w));
  await closePreview(w);

  // retry succeeds, but the session cannot be driven (fake object): ended, reported, nothing left behind
  xrState.mode = "reject-once"; xrState.calls = []; xrState.sessions = [];
  await configure({ role: "user", files: FILES, status: { "a.mp4": VIDEO_DONE } });
  w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  await openPreview(w, "a.mp4");
  vrBtnOf(w).click();
  await waitFor(() => /Could not start VR/.test(toastOf(w)), 8000);
  await check("vr click: retry succeeded, a broken session is ended and reported", xrState.calls.length === 2
    && xrState.sessions.length === 1 && xrState.sessions[0].ended && /Could not start VR/.test(toastOf(w)),
    JSON.stringify([xrState.calls.length, xrState.sessions.map((x) => x.ended), toastOf(w)]));
  await check("vr click: nothing left behind (canvas, button)", !w.document.getElementById("vrViewerCanvas") && !vrBtnOf(w).disabled, "leak");
  await closePreview(w);
  xrState.mode = "supported";
  await vrLiveAvailability(VIDEO_DONE);
  await vrCurrentItem();
  await vrToggle(VIDEO_DONE);
}

// The VR buttons are toggles: gray when idle, highlighted + "Exit VR" + aria-pressed while a session runs.
// The session is a controllable fake handle behind Stereo3d.startVr (the page logic is what is tested).
async function vrToggle(VIDEO_DONE) {
  xrState.mode = "supported";
  await configure({ role: "user", files: FILES, status: { "a.mp4": VIDEO_DONE } });
  const w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  await sleep(400);
  theme(w);
  const top = w.document.getElementById("mediaVrTopBtn");
  await openPreview(w, "a.mp4");
  await waitFor(() => !vrOff(vrBtnOf(w)) && !vrOff(top), 4000).catch(() => {});
  const btns = () => [["preview", vrBtnOf(w)], ["top bar", top]];
  const gray = (b) => colorOf(w, b, "color") === cssColor(w, "var(--muted)");
  const lit = (b) => colorOf(w, b, "color") === cssColor(w, "var(--accent)") && colorOf(w, b, "borderTopColor") === cssColor(w, "var(--accent)");
  const idle = (b) => b.getAttribute("aria-pressed") === "false" && b.textContent.trim() === "VR" && gray(b) && !lit(b);
  const on = (b) => b.getAttribute("aria-pressed") === "true" && b.textContent.trim() === "Exit VR" && b.title === "Exit VR" && lit(b) && !gray(b);
  await check("vr toggle: both buttons start gray, aria-pressed false, labelled VR", btns().every(([, b]) => idle(b)),
    btns().map(([n, b]) => `${n}:${b.getAttribute("aria-pressed")}/${b.textContent}/${colorOf(w, b, "color")}`).join(" "));

  // fake session: starts after `delay`, ends when end() is called or finish() is
  const sessions = [];
  let startCalls = 0, delay = 0;
  w.Stereo3d.startVr = (ctx, opts) => {
    startCalls++;
    return new Promise((res) => setTimeout(() => {
      let done; const ended = new Promise((r) => { done = r; });
      const hd = { ended, ends: 0, end() { hd.ends++; done({}); return Promise.resolve(); }, finish(r) { done(r || {}); } };
      sessions.push(hd); res(hd);
    }, delay));
  };
  for (const [name, b] of btns()) {
    startCalls = 0; sessions.length = 0; delay = 0;
    b.click();
    const syncCalls = startCalls;
    await waitFor(() => sessions.length === 1, 3000).catch(() => {});
    await sleep(700);
    await check(`vr toggle (${name}): startVr is called synchronously from the click`, syncCalls === 1, String(syncCalls));
    await check(`vr toggle (${name}): while the session runs both buttons are pressed, "Exit VR" and highlighted`, btns().every(([, x]) => on(x)),
      btns().map(([n, x]) => `${n}:${x.getAttribute("aria-pressed")}/${x.textContent}/${x.title}/${colorOf(w, x, "color")}/${colorOf(w, x, "borderTopColor")}/${x.getAttribute("style")}`).join(" "));
    b.click();
    await sleep(700);
    await check(`vr toggle (${name}): clicking while active ends the session once and the buttons reset`, sessions[0].ends === 1 && startCalls === 1
      && btns().every(([, x]) => idle(x)), `ends=${sessions[0].ends} calls=${startCalls}`);
  }
  // ended from inside (headset / error): buttons reset, error shown
  startCalls = 0; sessions.length = 0;
  top.click();
  await waitFor(() => sessions.length === 1, 3000).catch(() => {});
  await sleep(100);
  sessions[0].finish({ error: "Headset removed." });
  await sleep(700);
  await check("vr toggle: a session ending by itself resets both buttons and shows its error", btns().every(([, x]) => idle(x)) && /Headset removed/.test(toastOf(w)), toastOf(w));
  // rapid double click: one session, nothing stuck
  startCalls = 0; sessions.length = 0; delay = 300;
  top.click(); top.click(); vrBtnOf(w).click();
  await sleep(900);
  await check("vr toggle: rapid clicks while starting start exactly one session", startCalls === 1 && sessions.length === 1 && sessions[0].ends === 0, `calls=${startCalls} ends=${sessions[0] && sessions[0].ends}`);
  await check("vr toggle: it is then active", btns().every(([, x]) => on(x)), "");
  top.click(); await sleep(700);
  await check("vr toggle: and one click exits it", sessions[0].ends === 1 && btns().every(([, x]) => idle(x)), "");
  // a failing start leaves the buttons usable
  w.Stereo3d.startVr = () => Promise.reject(new Error("VR could not be started."));
  top.click(); await sleep(300);
  await check("vr toggle: a failed start leaves both buttons idle and usable", btns().every(([, x]) => idle(x) && !x.disabled), "");
  const errs = await errorsOf(w);
  await check("vr toggle: no console errors", errs.length === 0, errs.join(" | "));
  await closePreview(w);

  // unavailable: unchanged semantics, gray, aria-disabled
  xrState.mode = "unsupported";
  const w2 = await openPage("/media.html");
  await waitFor(() => tileOf(w2, "a.mp4"));
  await sleep(500);
  theme(w2); await sleep(700);
  const t2 = w2.document.getElementById("mediaVrTopBtn");
  await check("vr toggle: XR unsupported stays aria-disabled, not pressed, gray", vrOff(t2) && t2.getAttribute("aria-pressed") === "false"
    && colorOf(w2, t2, "color") === cssColor(w2, "var(--muted)"), `${t2.getAttribute("aria-disabled")}/${t2.getAttribute("aria-pressed")}/${colorOf(w2, t2, "color")}/${t2.getAttribute("style")}`);
  xrState.mode = "supported";
}

// VR always shows the item currently in the preview, whatever was shown before.
async function vrCurrentItem() {
  const K = (c) => c.repeat(32);
  const D = (path, kind, k) => ({ ...DONE(path, kind), key: K(k), sbs_url: `/api/media/3d/file?key=${K(k)}&name=${kind === "video" ? "sbs.mp4" : "sbs.jpg"}`, eye_w: 1080, eye_h: 1440 });
  xrState.mode = "supported";
  await configure({ role: "user", files: FILES, status: {
    "a.mp4": D("a.mp4", "video", "a"), "b.jpg": D("b.jpg", "picture", "b"),
    "d.gif": D("d.gif", "picture", "d"), "e.png": D("e.png", "picture", "e") } });
  const w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  const press = async (key, name, what) => {
    xrState.calls = []; xrState.sessions = [];
    await sleep(400);
    await waitFor(() => vrBtnOf(w)._vrKnown, 4000).catch(() => {});
    await clearLog();
    vrBtnOf(w).click();
    await waitFor(() => /Could not start VR/.test(toastOf(w)) || xrState.calls.length > 0, 6000).catch(() => {});
    await sleep(800);
    const files = (await requests("/api/media/3d/file")).map((r) => r.path).filter((f) => /name=sbs/.test(f));
    await check(`vr current item: ${what} requests only ${key[0]}'s ${name}`,
      files.length > 0 && files.every((f) => f.includes(`key=${key}`) && f.includes(`name=${name}`)), JSON.stringify(files));
    await waitFor(() => !vrBtnOf(w).disabled, 5000).catch(() => {});
    w.document.getElementById("selectToast").textContent = "";
  };
  await openPreview(w, "a.mp4");
  await press(K("a"), "sbs.mp4", "video A");
  w.navigatePreview(1); await sleep(600);          // b.jpg
  w.navigatePreview(2); await sleep(600);          // d.gif (audio skipped by index: c.mp3 is 2 away)
  w.navigatePreview(1); await sleep(700);          // e.png
  await check("vr current item: button is bound to the current item", vrBtnOf(w)._vrStatus && vrBtnOf(w)._vrStatus.key === K("e"), JSON.stringify(vrBtnOf(w)._vrStatus));
  await press(K("e"), "sbs.jpg", "picture E after A, B, D");
  w.navigatePreview(-4); await sleep(700);         // back to a.mp4: picture -> video
  await press(K("a"), "sbs.mp4", "video A after picture E");
  w.navigatePreview(1); await sleep(700);          // b.jpg: video -> picture
  await press(K("b"), "sbs.jpg", "picture B after video A");
  // pressed while the new item's status is still loading: no stale item may start
  xrState.calls = []; await clearLog();
  w.navigatePreview(1);                            // c.mp3
  w.navigatePreview(1);                            // d.gif, status not answered yet
  vrBtnOf(w).click();
  await sleep(600);
  const early = (await requests("/api/media/3d/file")).map((r) => r.path).filter((f) => /name=sbs/.test(f));
  await check("vr current item: a press before the new item's status arrived never loads the previous item",
    early.every((f) => f.includes(`key=${K("d")}`)), JSON.stringify(early));
  const errs = await errorsOf(w);
  await check("vr current item: no console errors", errs.length === 0, errs.join(" | "));
  await closePreview(w);
  xrState.mode = "supported";
}

// Availability is live: devicechange, focus, visibility and a click on the disabled-looking button re-check it.
async function vrLiveAvailability(VIDEO_DONE) {
  const fresh = async () => {
    xrState.mode = "unsupported"; xrState.calls = []; xrState.sessions = [];
    await configure({ role: "user", files: FILES, status: { "a.mp4": VIDEO_DONE } });
    const w = await openPage("/media.html");
    await waitFor(() => tileOf(w, "a.mp4"));
    await openPreview(w, "a.mp4");
    return w;
  };
  let w = await fresh();
  await check("vr live: starts aria-disabled (XR unsupported)", vrOff(vrBtnOf(w)), "enabled");
  xrState.mode = "supported";
  await sleep(1100);                                  // past the forced re-check coalescing window
  w.navigator.xr.fire();
  await waitFor(() => !vrOff(vrBtnOf(w)), 3000).catch(() => {});
  await check("vr live: devicechange enables the button", !vrOff(vrBtnOf(w)) && /Trigger/.test(vrBtnOf(w).title), vrBtnOf(w).title);
  xrState.mode = "unsupported";
  await sleep(1100);
  w.navigator.xr.fire();
  await waitFor(() => vrOff(vrBtnOf(w)), 3000).catch(() => {});
  await check("vr live: devicechange also disables it again (not cached forever)", vrOff(vrBtnOf(w)), "still enabled");
  xrState.mode = "supported";
  await sleep(1100);
  w.dispatchEvent(new w.Event("focus"));
  await waitFor(() => !vrOff(vrBtnOf(w)), 3000).catch(() => {});
  await check("vr live: window focus enables the button", !vrOff(vrBtnOf(w)), "still disabled");
  xrState.mode = "unsupported";
  await sleep(1100);
  w.dispatchEvent(new w.Event("focus"));
  await waitFor(() => vrOff(vrBtnOf(w)), 3000).catch(() => {});
  xrState.mode = "supported";
  await sleep(1100);
  Object.defineProperty(w.document, "visibilityState", { get: () => "visible", configurable: true });
  w.document.dispatchEvent(new w.Event("visibilitychange"));
  await waitFor(() => !vrOff(vrBtnOf(w)), 3000).catch(() => {});
  await check("vr live: becoming visible enables the button", !vrOff(vrBtnOf(w)), "still disabled");
  let errs = await errorsOf(w);
  await check("vr live: no console errors (events)", errs.length === 0, errs.join(" | "));
  await closePreview(w);

  // hover re-checks
  w = await fresh();
  xrState.mode = "supported";
  await sleep(1100);
  vrBtnOf(w).dispatchEvent(new w.MouseEvent("mouseenter"));
  await waitFor(() => !vrOff(vrBtnOf(w)), 3000).catch(() => {});
  await check("vr live: hovering the disabled-looking button enables it", !vrOff(vrBtnOf(w)), "still disabled");
  await closePreview(w);

  // click while still unsupported: toast with the hint, no session, button stays clickable
  w = await fresh();
  await sleep(1100);
  vrBtnOf(w).click();
  await waitFor(() => /SteamVR/.test(toastOf(w)), 3000).catch(() => {});
  await check("vr live: click while unsupported toasts the hint (SteamVR, OpenXR flag), no session",
    /SteamVR/.test(toastOf(w)) && /webxr-runtime/.test(toastOf(w)) && /OpenXR/.test(toastOf(w)) && xrState.calls.length === 0 && !vrBtnOf(w).disabled && vrOff(vrBtnOf(w)),
    toastOf(w));
  // click once it has become supported (no event fired): re-checks and enters VR directly
  xrState.mode = "supported";
  await sleep(1100);
  vrBtnOf(w).click();
  await waitFor(() => xrState.calls.length >= 1, 4000).catch(() => {});
  await check("vr live: click on the disabled-looking button re-checks and enters VR directly", xrState.calls.length >= 1, `calls=${xrState.calls.length}`);
  await sleep(1500);
  errs = await errorsOf(w);
  await check("vr live: no console errors (click)", errs.length === 0, errs.join(" | "));
  await closePreview(w);
  xrState.mode = "supported";
}

// ---------------------------------------------------------------- pure VR logic (js/vr-logic.js)

async function vrLogic() {
  const L = await import("/js/vr-logic.js");
  // Regression: videos showed the whole SBS frame in each eye because only pictures got a UV window.
  const V = await import("/js/vr-viewer.js");
  const fakeMeshes = () => [0, 1].map(() => { const m = { material: { map: { repeat: { x: 1, y: 1, set(x, y) { this.x = x; this.y = y; } }, offset: { x: 0, y: 0, set(x, y) { this.x = x; this.y = y; } } } } }; return m; });
  for (const kind of ["video", "picture"]) {
    const ms = fakeMeshes();
    V.applyEyeWindows(ms, kind, 1, { x: 0, y: 0 });
    const [l, r] = ms.map((m) => m.material.map);
    await check(`viewer: ${kind} eye meshes show one half of the SBS frame at zoom 1`, l.repeat.x === 0.5 && r.repeat.x === 0.5 && l.repeat.y === 1
      && l.offset.x === 0 && r.offset.x === 0.5 && l.offset.y === 0 && r.offset.y === 0, JSON.stringify([l.repeat, l.offset, r.repeat, r.offset]));
  }
  const vm = fakeMeshes();
  V.applyEyeWindows(vm, "video", 3, { x: 0.2, y: 0.2 });
  await check("viewer: a video ignores zoom and pan", vm[0].material.map.repeat.x === 0.5 && vm[1].material.map.offset.x === 0.5, "zoomed");
  const pm = fakeMeshes();
  V.applyEyeWindows(pm, "picture", 2, { x: 0, y: 0 });
  await check("viewer: a picture at zoom 2 shows a quarter-width window", pm[0].material.map.repeat.x === 0.25, String(pm[0].material.map.repeat.x));
  const near = (a, b, e = 1e-9) => Math.abs(a - b) <= e;

  await check("logic: clamps", L.clampDistance(0.1) === L.MIN_DISTANCE && L.clampDistance(99) === L.MAX_DISTANCE && L.clampDistance(NaN) === L.DEFAULT_DISTANCE
    && L.clampZoom(0.2) === 1 && L.clampZoom(100) === L.MAX_ZOOM, "clamp");
  await check("logic: eye aspect from meta, else media, else 16:9", near(L.eyeAspect(1080, 1440), 0.75) && near(L.eyeAspect(0, 0, 2160, 1440), 0.75)
    && near(L.eyeAspect(null, null, null, null), 16 / 9), "aspect");
  const s = L.screenSize(0.75, 1.4);
  await check("logic: default screen is 1.5 m tall", near(s.h, 1.5) && near(s.w, 1.125), JSON.stringify(s));
  const far = L.screenSize(0.75, 2.8);
  await check("logic: distance keeps the angular size", near(far.h / 2.8, s.h / 1.4) && near(far.w / 2.8, s.w / 1.4), JSON.stringify(far));
  await check("logic: distance stick: dead zone, direction, clamps", L.stepDistance(1.4, 0.2, 1) === 1.4 && L.stepDistance(1.4, 1, 0.1) > 1.4
    && L.stepDistance(1.4, -1, 0.1) < 1.4 && L.stepDistance(1.4, 1, 100) === L.MAX_DISTANCE && L.stepDistance(1.4, -1, 100) === L.MIN_DISTANCE, "dist");
  await check("logic: zoom stick: multiplicative, clamped", L.stepZoom(1, 1, 0.5) > 1 && L.stepZoom(2, -1, 0.5) < 2 && L.stepZoom(1, -1, 10) === 1
    && L.stepZoom(1, 1, 100) === L.MAX_ZOOM && L.stepZoom(3, 0.1, 1) === 3, "zoom");

  const u1 = L.uvWindow(0, 1, { x: 0, y: 0 }), u2 = L.uvWindow(1, 1, { x: 0, y: 0 });
  await check("logic: uv window at zoom 1 is each eye's half", near(u1.repeatX, 0.5) && near(u1.repeatY, 1) && near(u1.offsetX, 0) && near(u1.offsetY, 0)
    && near(u2.offsetX, 0.5), JSON.stringify([u1, u2]));
  const z = L.uvWindow(0, 2, { x: 0, y: 0 });
  await check("logic: zoom 2 centres a half-size window in the eye's half", near(z.repeatX, 0.25) && near(z.repeatY, 0.5) && near(z.offsetX, 0.125) && near(z.offsetY, 0.25), JSON.stringify(z));
  const edge = L.uvWindow(0, 2, { x: 99, y: -99 });
  await check("logic: pan cannot leave the image (left eye stays inside u 0..0.5, v 0..1)", near(edge.offsetX + edge.repeatX, 0.5) && near(edge.offsetY, 0), JSON.stringify(edge));
  const redge = L.uvWindow(1, 4, { x: -99, y: 99 });
  await check("logic: right eye window stays inside u 0.5..1", near(redge.offsetX, 0.5) && near(redge.offsetY + redge.repeatY, 1), JSON.stringify(redge));
  const pd = L.panDelta(0.1, 0, 1, 1, 2);
  await check("logic: dragging right moves the window left, scaled by zoom", near(pd.x, -0.05) && pd.y === 0 && L.panDelta(1, 1, 0, 0, 2).x === 0, JSON.stringify(pd));

  const r0 = L.recenterPose({ x: 1, y: 1.6, z: 2 }, { x: 0, y: 0, z: -1 }, 1.4);
  await check("logic: recenter straight ahead keeps yaw 0 at the head position", near(r0.yaw, 0) && r0.x === 1 && r0.y === 1.6 && r0.z === 2, JSON.stringify(r0));
  const r1 = L.recenterPose({ x: 0, y: 0, z: 0 }, { x: 1, y: 0.5, z: 0 }, 1.4);
  const sx = -Math.sin(r1.yaw) * 1.4, sz = -Math.cos(r1.yaw) * 1.4;
  await check("logic: recenter puts the screen in front of a turned head", near(sx, 1.4) && near(sz, 0), JSON.stringify([r1, sx, sz]));
  const r2 = L.recenterPose({ x: 0, y: 0, z: 0 }, { x: 0, y: -1, z: 0 }, 1.4, 0.7);
  await check("logic: looking straight down keeps the previous yaw", r2.yaw === 0.7, JSON.stringify(r2));

  const hand = (over) => ({ id: "left1", axes: [0, 0, 0, 0], buttons: [], ...over });
  let c = L.createController();
  let a = c.update({ dt: 0.01, kind: "video", hands: [hand({ buttons: [true] })] });
  await check("controller: trigger press toggles once", a.length === 1 && a[0].type === "toggle", JSON.stringify(a));
  a = c.update({ dt: 0.01, kind: "video", hands: [hand({ buttons: [true] })] });
  await check("controller: a held trigger does not repeat", a.length === 0, JSON.stringify(a));
  c.update({ dt: 0.5, kind: "video", hands: [hand({ buttons: [false] })] });
  a = c.update({ dt: 0.01, kind: "video", hands: [hand({ buttons: [true] })] });
  await check("controller: released and pressed again toggles again", a.length === 1, JSON.stringify(a));
  c = L.createController();
  c.update({ dt: 0.01, kind: "video", hands: [hand({ buttons: [true] })] });
  c.update({ dt: 0.01, kind: "video", hands: [hand({ buttons: [false] })] });
  a = c.update({ dt: 0.01, kind: "video", hands: [hand({ buttons: [true] })] });
  await check("controller: contact-bounce presses inside the refractory time are ignored", a.length === 0, JSON.stringify(a));
  c = L.createController();
  a = c.update({ dt: 0.01, kind: "picture", hands: [hand({ buttons: [true] })] });
  await check("controller: trigger does nothing on a picture", a.length === 0, JSON.stringify(a));
  a = c.update({ dt: 0.5, kind: "video", hands: [{ id: "right2", axes: [0, 0, 0, 0], buttons: [false, false, false, false, true, false] }] });
  await check("controller: A/X recenters (either hand)", a.length === 1 && a[0].type === "recenter", JSON.stringify(a));
  a = c.update({ dt: 0.5, kind: "video", hands: [{ id: "right2", axes: [0, 0, 0, 0], buttons: [false, false, false, false, false, true] }] });
  await check("controller: B/Y exits", a.length === 1 && a[0].type === "exit", JSON.stringify(a));
  a = c.update({ dt: 0.5, kind: "video", hands: [{ id: "right2", axes: [0, 0, 0, 0], buttons: [false, false, false, true] }] });
  await check("controller: stick press is next", a.length === 1 && a[0].type === "next", JSON.stringify(a));

  c = L.createController();
  const flick = (id, x, dt = 0.016) => c.update({ dt, kind: "video", hands: [hand({ id, axes: [0, 0, x, 0] })] });
  a = flick("left1", 0.5);
  await check("controller: a half push does not seek", a.length === 0, JSON.stringify(a));
  a = flick("left1", 0.9);
  await check("controller: a flick right seeks +10 s", a.length === 1 && a[0].type === "seek" && a[0].seconds === 10, JSON.stringify(a));
  a = flick("left1", 1).concat(flick("left1", 1), flick("left1", 0.8));
  await check("controller: holding the stick over does not repeat", a.length === 0, JSON.stringify(a));
  flick("left1", 0, 0.5);
  a = flick("left1", -0.9);
  await check("controller: back to centre re-arms; a flick left seeks -10 s", a.length === 1 && a[0].seconds === -10, JSON.stringify(a));
  {
    const c2 = L.createController();
    const pic = (h, x, dt = 0.016) => c2.update({ dt, kind: "picture", hands: [{ axes: [0, 0, x, 0], buttons: [false, false, false, false, false, false], ...h }] });
    const left = { id: "left0", left: true }, right = { id: "right1" };
    const z0 = c2.zoom, lz = pic(left, 1, 0.1), z1 = c2.zoom, rz = pic(right, 1, 0.1);
    await check("controller: on a picture either stick zooms and none steps the image", lz.length === 1 && lz[0].type === "zoom" && z1 > z0 && rz.length === 1 && rz[0].type === "zoom" && c2.zoom > z1, JSON.stringify([lz, rz, z0, z1, c2.zoom]));
    const lb = c2.update({ dt: 0.5, kind: "picture", hands: [{ ...left, axes: [0, 0, 0, 0], buttons: [false, false, false, false, true, true] }] });
    await check("controller: left X and Y neither recenter nor exit (the d-pad's Back and Forward)", lb.length === 0, JSON.stringify(lb));
  }
  flick("left1", 0, 0.01);
  a = flick("left1", 0.9, 0.01);
  await check("controller: a flick right after a fast return is debounced", a.length === 0, JSON.stringify(a));
  a = c.update({ dt: 0.5, kind: "video", hands: [hand({ id: "left1", axes: [0, 0, 0, 1] }), hand({ id: "right2", axes: [0, 0, 0.9, 0] })] });
  await check("controller: both hands work at once (distance from one, seek from the other)",
    a.some((x) => x.type === "distance" && x.dist > 1.4) && a.some((x) => x.type === "seek"), JSON.stringify(a));
  c = L.createController();
  a = c.update({ dt: 0.1, kind: "video", hands: [hand({ axes: [0, 0, 0, 0] })] });
  await check("controller: an idle stick produces nothing", a.length === 0, JSON.stringify(a));
  a = c.update({ dt: 0.1, kind: "picture", hands: [hand({ axes: [0, 0, 1, 0] })] });
  await check("controller: picture stick X zooms in", a.length === 1 && a[0].type === "zoom" && a[0].zoom > 1, JSON.stringify(a));
  a = c.update({ dt: 0.1, kind: "picture", hands: [hand({ buttons: [false, true] })] });
  const rel = c.update({ dt: 0.1, kind: "picture", hands: [hand({ buttons: [false, false] })] });
  await check("controller: grip press/release reported on a picture", a.some((x) => x.type === "grip" && x.down) && rel.some((x) => x.type === "grip" && !x.down), JSON.stringify([a, rel]));
  c = L.createController();
  a = c.update({ dt: 0.5, kind: "video", hands: [{ id: "x", axes: [0, 1], buttons: [] }] });
  await check("controller: a 2-axis gamepad uses axes 0/1", a.some((x) => x.type === "distance"), JSON.stringify(a));

  await check("logic: seek target clamps into the media", L.seekTarget(5, -10, 100) === 0 && near(L.seekTarget(95, 10, 100), 99.9) && L.seekTarget(5, 10, NaN) === 15, "seek");
  const items = [{ key: "a", sbs_url: "x" }, { key: "b", sbs_url: "y" }, { key: "c" }];
  await check("logic: next item wraps, skips unusable ones, null when alone", L.nextItem(items, "a").key === "b" && L.nextItem(items, "b").key === "a"
    && L.nextItem([items[0]], "a") === null && L.nextItem(null, "a") === null, "next");
}


// server.py serves non-admins only GUEST_ALLOWED_FILES: replay the redirect for the new scripts.
async function vrGuestAllowlist() {
  xrState.mode = "supported";
  const VIDEO_DONE = { ...DONE("a.mp4", "video"), eye_w: 1080, eye_h: 1440 };
  for (const role of ["user", "guest"]) {
    await configure({ role, allowlist: "current", files: FILES, status: { "a.mp4": VIDEO_DONE } });
    let w = await openPage("/media.html");
    await waitFor(() => tileOf(w, "a.mp4"));
    await sleep(300);
    await openPreview(w, "a.mp4");
    const own = (w.__errors || []).filter((e) => !/Unexpected token|SyntaxError|Failed to load|MIME/i.test(e));
    await check(`allowlist (${role}, scripts redirected): page works, no VR button, no errors of the page's own`, vrBtnOf(w).hidden && own.length === 0,
      `hidden=${vrBtnOf(w).hidden} errors=${JSON.stringify(w.__errors)}`);
    await closePreview(w);
    await configure({ allowlist: "p8v0" });
    w = await openPage("/media.html");
    await waitFor(() => tileOf(w, "a.mp4"));
    await sleep(300);
    await openPreview(w, "a.mp4");
    await check(`allowlist (${role}, P8 server rule: every plain js/**/*.js, js/vr/* and js/vendor/ included): VR button works, no errors`,
      !vrBtnOf(w).hidden && !vrBtnOf(w).disabled && (w.__errors || []).length === 0, `hidden=${vrBtnOf(w).hidden} errors=${JSON.stringify(w.__errors)}`);
    await closePreview(w);
  }
  await configure({ allowlist: "" });
}

// ======================================================================= P8 V0: the VR app shell

const memStorage = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, removeItem: (k) => { m.delete(k); }, _m: m }; };
const noBad = (text) => !/undefined|null|NaN|Infinity|\[object/.test(text);

// Pure logic of js/vr/logic.js.
async function vrAppLogic() {
  const L = await import("/js/vr/logic.js");
  const near = (a, b, e = 1e-9) => Math.abs(a - b) <= e;

  // ---- context: normalise / degrade
  const item = { root: "downloads", path: "cats/a.mp4", status: { state: "done", key: "k", sbs_url: "/x?name=sbs.mp4", kind: "video", eye_w: 1080, eye_h: 1440 } };
  const src = { kind: "folder", root: "downloads", path: "cats", query: "", filters: "type=video", sort: { key: "size", dir: "desc" }, offset: 40 };
  let c = L.normalizeContext({ view: "item", item, source: src });
  await check("vr logic: an item context keeps item, source, sort and offset", c.view === "item" && c.item.kind === "video" && c.item.name === "a.mp4" && c.item.key === "k"
    && c.source.sort.key === "size" && c.source.sort.dir === "desc" && c.source.offset === 40 && c.source.filters === "type=video", JSON.stringify(c));
  await check("vr logic: an item view without a usable item becomes the wall of its source", L.normalizeContext({ view: "item", item: { path: "" }, source: src }).view === "wall"
    && L.normalizeContext({ view: "item", item: { root: "r", path: "readme.txt" }, source: src }).view === "wall", "degrade");
  await check("vr logic: a wall without a source, and anything unusable, becomes home", L.normalizeContext({ view: "wall" }).view === "home"
    && L.normalizeContext(null).view === "home" && L.normalizeContext("x").view === "home" && L.normalizeContext({ view: "bogus", source: { kind: "nope" } }).view === "home", "home");
  await check("vr logic: an item without a view is inferred; a done status without a url degrades", L.normalizeContext({ item }).view === "item"
    && L.normalizeContext({ item: { path: "a.jpg", status: { state: "done" } } }).item.status.state === "none"
    && L.normalizeContext({ item: { path: "a.jpg" } }).item.kind === "picture", "infer");
  await check("vr logic: a bad offset and a bad sort direction are cleaned", (() => { const s = L.normalizeContext({ source: { kind: "folder", offset: -5, sort: { dir: "up" } } }).source; return s.offset === 0 && s.sort.dir === ""; })(), "clean");

  // ---- query round trip
  for (const ctx of [
    { view: "item", item, source: src },
    { view: "wall", source: { kind: "search", root: "", path: "", query: "cat & dog", filters: "", sort: { key: "name", dir: "asc" }, offset: 0, mode: "filename", tag: "x y" } },
    { view: "wall", source: { kind: "favorites", root: "", path: "", query: "", filters: "", sort: { key: "", dir: "" }, offset: 0 } },
    { view: "home" },
  ]) {
    const q = L.contextToQuery(ctx);
    const back = L.contextFromQuery("?" + q);
    const a = L.normalizeContext(ctx);
    if (a.item) delete a.item.status;       // the status never travels in a URL
    if (a.item) a.item.key = "";
    await check(`vr logic: query round trip (${a.view}${a.source ? " / " + a.source.kind : ""})`, JSON.stringify(back) === JSON.stringify(a), `${q} -> ${JSON.stringify(back)} vs ${JSON.stringify(a)}`);
  }
  await check("vr logic: hasQueryContext", L.hasQueryContext("?view=home") && !L.hasQueryContext("") && !L.hasQueryContext("?x=1") && L.contextFromQuery("?x=1").view === "home", "has");

  // ---- titles and what an item shows
  await check("vr logic: titles for every source kind", L.sourceTitle({ kind: "folder", root: "downloads", path: "a/b" }) === "downloads / a / b"
    && L.sourceTitle({ kind: "folder", root: "", path: "" }) === "Library" && L.sourceTitle({ kind: "favorites" }) === "Favorites"
    && L.sourceTitle({ kind: "search", query: "cat" }) === "Search: cat" && L.sourceTitle({ kind: "search", tag: "t" }) === "Search: t"
    && L.sourceTitle({ kind: "recent" }) === "Recent downloads" && L.sourceTitle(null, null) === "Home" && L.sourceTitle(null, { path: "x/y.jpg" }) === "y.jpg", "titles");
  const d1 = L.itemDisplay(item);
  await check("vr logic: a converted item shows its side-by-side file", d1.mode === "sbs" && d1.url === "/x?name=sbs.mp4" && d1.eyeW === 1080 && d1.key === "k", JSON.stringify(d1));
  const d2 = L.itemDisplay({ root: "downloads", path: "p q/b#.jpg" });
  await check("vr logic: an unconverted item shows the original (2D), url encoded", d2.mode === "mono" && d2.url === "/api/media/file?root=downloads&path=p%20q%2Fb%23.jpg" && d2.kind === "picture", JSON.stringify(d2));
  await check("vr logic: an item's own url is used when given; an unusable one is null", L.itemDisplay({ root: "r", path: "a.mp4", url: "/api/media/file?root=r&path=a.mp4" }).url === "/api/media/file?root=r&path=a.mp4"
    && L.itemDisplay({ path: "a.txt" }) === null && L.itemDisplay(null) === null, "url");
  await check("vr logic: media kinds", L.mediaKindOf("a.MP4") === "video" && L.mediaKindOf("a.png") === "picture" && L.mediaKindOf("a.mp3") === null && L.mediaKindOf("") === null, "kinds");

  // ---- 2D picture window
  await check("vr logic: mono aspect (and 16:9 fallback)", near(L.monoAspect(1000, 500), 2) && near(L.monoAspect(0, 0), 16 / 9) && near(L.monoAspect(NaN, 5), 16 / 9), "aspect");
  let u = L.monoUvWindow(1, { x: 0, y: 0 });
  await check("vr logic: mono window at zoom 1 is the whole picture", near(u.repeatX, 1) && near(u.offsetX, 0) && near(u.offsetY, 0), JSON.stringify(u));
  u = L.monoUvWindow(2, { x: 0, y: 0 });
  await check("vr logic: mono window at zoom 2 is the centred half", near(u.repeatX, 0.5) && near(u.offsetX, 0.25) && near(u.offsetY, 0.25), JSON.stringify(u));
  u = L.monoUvWindow(2, { x: 9, y: -9 });
  await check("vr logic: mono pan is clamped inside the picture", u.offsetX >= -1e-9 && u.offsetX + u.repeatX <= 1 + 1e-9 && u.offsetY >= -1e-9 && u.offsetY + u.repeatY <= 1 + 1e-9, JSON.stringify(u));

  // ---- frame statistics and HUD
  const fs = new L.FrameStats(100);
  const empty = fs.summary();
  await check("vr logic: empty stats are all finite zeros", Object.values(empty).every((v) => v === 0), JSON.stringify(empty));
  for (let i = 1; i <= 100; i += 1) fs.push(i);
  fs.push(NaN); fs.push(-3); fs.push(0);
  let sm = fs.summary(50.5);
  await check("vr logic: percentiles over 1..100 ms (bad samples ignored)", sm.n === 100 && sm.p50 === 50 && sm.p99 === 99 && near(sm.mean, 50.5) && sm.over === 50 && near(sm.overPct, 50) && near(sm.fps, 1000 / 50.5), JSON.stringify(sm));
  for (let i = 0; i < 100; i += 1) fs.push(8);
  sm = fs.summary();
  await check("vr logic: the ring buffer forgets old frames", sm.n === 100 && sm.p50 === 8 && sm.over === 0, JSON.stringify(sm));
  const lines = [L.hudLines(empty, {}), L.hudLines(sm, { mode: "xr", enabledFeatures: ["local-floor", "layers"], frameRate: 89.9, supportedFrameRates: [72, 90], textures: 3, geometries: 5, calls: 9 }),
    L.hudLines(null, { enabledFeatures: [], frameRate: NaN }), L.hudLines(sm, { mode: "preview" })];
  await check("vr logic: HUD lines never show NaN/undefined/null", lines.every((ls) => ls.length === 5 && noBad(ls.join("\n"))), JSON.stringify(lines.find((ls) => !noBad(ls.join("\n")))));
  await check("vr logic: HUD reports the runtime frame rate, the features and the session kind", /90 Hz/.test(lines[1][3]) && /72\/90/.test(lines[1][3]) && /layers/.test(lines[1][4]) && /immersive-vr/.test(lines[1][4])
    && /not reported/.test(lines[0][3]) && /desktop preview/.test(lines[3][4]), lines.map((l) => l.join(" | ")).join("\n"));

  // ---- dock layout, hit test, pose
  const btns = [{ id: "exit", label: "Exit" }, { id: "recenter", label: "R" }, { id: "hud", label: "HUD" }];
  const lay = L.dockLayout(btns, L.DOCK.px * L.DOCK.width, L.DOCK.px * L.DOCK.height);
  const W = L.DOCK.px * L.DOCK.width, H = L.DOCK.px * L.DOCK.height;
  await check("vr logic: dock layout is three disjoint buttons inside the panel, centred", lay.length === 3
    && lay.every((b) => b.x >= 0 && b.x + b.w <= W && b.y >= 0 && b.y + b.h <= H && b.w > 0 && b.h > 0)
    && lay[0].x + lay[0].w < lay[1].x && lay[1].x + lay[1].w < lay[2].x
    && near(lay[0].x + (lay[2].x + lay[2].w - lay[0].x) / 2, W / 2, 1e-6), JSON.stringify(lay));
  await check("vr logic: hit test finds the button under a point, null elsewhere and for bad input", lay.every((b) => L.hitButton(lay, b.x + b.w / 2, b.y + b.h / 2) === b.id)
    && L.hitButton(lay, 1, 1) === null && L.hitButton(lay, NaN, 5) === null && L.dockLayout([], 100, 100).length === 0, "hit");
  const dp = L.dockPose();
  await check("vr logic: the dock sits below and in front of the head, tilted toward it", dp.y < 0 && dp.z < 0 && near(Math.hypot(dp.y, dp.z), L.DOCK.radius, 1e-9) && dp.tiltX < 0, JSON.stringify(dp));

  // ---- state across leaving VR
  const st = memStorage();
  await check("vr logic: state save and load round trip", L.saveVrState(st, { context: { view: "item", item, source: src }, dist: 2.5, hud: false })
    && (() => { const s = L.loadVrState(st); return s && s.context.view === "item" && s.context.item.path === "cats/a.mp4" && s.dist === 2.5 && s.hud === false; })(), JSON.stringify(L.loadVrState(st)));
  const bad = memStorage();
  let allOk = true;
  for (const raw of ["{", "null", "[]", "\"x\"", "{\"context\":5,\"dist\":\"a\",\"hud\":1}", ""]) {
    bad.setItem(L.STATE_KEY, raw);
    const s = L.loadVrState(bad);
    if (s !== null && !(s.context && s.context.view === "home" && (s.dist === undefined || Number.isFinite(s.dist)))) allOk = false;
  }
  await check("vr logic: corrupt saved state loads as null or home, never throws", allOk && L.loadVrState({ getItem() { throw new Error("blocked"); } }) === null
    && L.saveVrState({ setItem() { throw new Error("full"); } }, {}) === false, "corrupt");
}

// The module graph loads for every role; the click builds the context for what the page shows.
async function vrAppModules() {
  xrState.mode = "supported";
  for (const role of ["admin", "user", "guest"]) {
    await configure({ role, allowlist: role === "admin" ? "" : "p8v0", files: FILES, status: {} });
    const w = await openPage("/media.html");
    await waitFor(() => tileOf(w, "a.mp4"));
    let err = "";
    try { await w.Stereo3d.loadVrApp(); } catch (e) { err = String(e && e.message || e); }
    await sleep(200);
    await check(`vr app (${role}): js/vr/app.js and its imports load, no console errors`, !err && (w.__errors || []).length === 0, `${err} ${JSON.stringify(w.__errors)}`);
  }
  await configure({ allowlist: "" });
}

async function vrContextAtClick() {
  const PIC_DONE = { ...DONE("b.jpg", "picture"), eye_w: 1080, eye_h: 1440 };
  xrState.mode = "denied";                        // the click builds the context, the session request is refused
  await configure({ role: "user", files: FILES, status: { "b.jpg": PIC_DONE, "e.png": { state: "queued", position: 1 } } });
  let w = await openPage("/media.html");
  await waitFor(() => tileOf(w, "a.mp4"));
  await sleep(300);
  const ctxOf = async (clickId) => {
    xrState.calls = [];
    w.Stereo3d.startVr; // (the getter below is read after the click)
    w.document.getElementById(clickId).click();
    await waitFor(() => /blocked|Could not start VR/.test(toastOf(w)) || w.Stereo3d.lastVrContext, 5000).catch(() => {});
    await sleep(500);
    const c = JSON.parse(JSON.stringify(w.Stereo3d.lastVrContext || null));
    w.document.getElementById("selectToast").textContent = "";
    return c;
  };
  w.eval("window.__ctxReset = 1");
  for (const [path, want, label] of [["b.jpg", "done", "a converted picture"], ["a.mp4", "none", "an unconverted video"], ["e.png", "none", "a queued picture (opens in 2D)"]]) {
    await openPreview(w, path);
    const c = await ctxOf("mediaVrBtn");
    await check(`vr context: ${label} -> item view, status ${want}, source = the folder`, c && c.view === "item" && c.item.path === path && c.item.status.state === want
      && c.item.root === "downloads" && c.source.kind === "folder" && c.source.root === "downloads", JSON.stringify(c));
    if (want === "done") await check("vr context: a converted item carries its side-by-side url and eye size", c.item.status.sbs_url.includes("name=sbs.jpg") && c.item.status.eye_w === 1080, JSON.stringify(c.item));
    await closePreview(w);
  }
  // main view: the top bar's button, folder + sort + filters + subfolder
  w.eval("currentMediaRoot = 'downloads'; currentMediaPath = 'cats/small'; mediaSortKey = 'size'; mediaSortDir = 'desc';");
  const filters = w.eval("filterQuery()");
  let c = await ctxOf("mediaVrTopBtn");
  await check("vr context: the top bar button starts the wall of the current folder with its sort, filters and subfolder",
    c && c.view === "wall" && !c.item && c.source.kind === "folder" && c.source.root === "downloads" && c.source.path === "cats/small"
    && c.source.sort.key === "size" && c.source.sort.dir === "desc" && c.source.filters === filters, JSON.stringify(c));
  w.eval("searchModeActive = true; searchQuery = 'sunset'; searchMode = 'filename'; searchOffset = 30;");
  c = await ctxOf("mediaVrTopBtn");
  await check("vr context: during a search the source is that search (query, mode, offset)", c && c.view === "wall" && c.source.kind === "search" && c.source.query === "sunset"
    && c.source.mode === "filename" && c.source.offset === 30, JSON.stringify(c));
  w.eval("searchQuery = ''; activeFavorites = true; searchOffset = 0;");
  c = await ctxOf("mediaVrTopBtn");
  await check("vr context: browsing the favourites is a favourites source", c && c.source.kind === "favorites", JSON.stringify(c));
  const errs = await errorsOf(w);
  await check("vr context: no page errors", errs.length === 0, errs.join(" | "));
  xrState.mode = "supported";
}

// The whole scene in the desktop preview, driven like a person with a mouse.
async function vrAppPreview() {
  const A = await import("/js/vr/app.js");
  const L = await import("/js/vr/logic.js");
  const PIC = (key) => ({ state: "done", key, sbs_url: `/api/media/3d/file?key=${key}&name=sbs.jpg`, kind: "picture", eye_w: 1080, eye_h: 1440 });
  const src = { kind: "folder", root: "downloads", path: "cats", query: "", filters: "", sort: { key: "name", dir: "asc" }, offset: 0 };
  const K = "1".repeat(32);
  const canvas = () => document.getElementById("vrPreviewCanvas");
  const move = (x, y, extra) => canvas().dispatchEvent(new PointerEvent("pointermove", { clientX: x, clientY: y, bubbles: true, ...(extra || {}) }));
  const press = (button) => canvas().dispatchEvent(new PointerEvent("pointerdown", { button, bubbles: true }));
  const release = (button) => window.dispatchEvent(new PointerEvent("pointerup", { button, bubbles: true }));
  const key = async (k) => { window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); await sleep(200); window.dispatchEvent(new KeyboardEvent("keyup", { key: k, bubbles: true })); await sleep(100); };
  const frames = async (n) => { const f0 = h.snapshot().frames; await waitFor(() => h.snapshot().frames >= f0 + n, 5000); };
  const clickButton = async (id) => {
    const p = h.projectButton(id);
    move(p.clientX, p.clientY); await frames(2);
    press(0); await frames(2); release(0); await frames(2);
  };

  {
    const hw = await A.startVr({ view: "wall", source: src }, { preview: true, storage: memStorage() });
    await waitFor(() => hw.snapshot().frames > 5, 8000);
    const e = hw.host.renderer.getContext().getError();
    await check("vr preview: the room alone (grid, dock, HUD, wall, rays) renders with no WebGL error", e === 0, `glError=${e}`);
    await hw.end();
  }
  await configure({ role: "admin", files: FILES, status: { "e.png": PIC(K) } });
  await clearLog();
  const mem = memStorage();
  let h = await A.startVr({ view: "item", item: { root: "downloads", path: "e.png", name: "e.png", kind: "picture", status: PIC(K) }, source: src },
    { preview: true, storage: mem, mediaTimeoutMs: 8000 });
  await waitFor(() => h.screen && h.screen.mounted && h.snapshot().frames > 5, 8000);
  let s = h.snapshot();
  await check("vr preview: room (no floor grid), dock, HUD, wall and two controller rays are built (V1: an item start shows the viewer buttons)", s.mode === "preview" && !("grid" in s.parts) && s.parts.dock && s.parts.hud && s.parts.wall && s.parts.rays === 2
    && s.buttons.join() === "back,prev,next,hud,recenter,exit" && s.view === "item", JSON.stringify(s.parts));
  await check("vr preview: the dock names the source and the item", /Downloads \/ cats/.test(s.title) && /e\.png/.test(s.subtitle) && /3D/.test(s.subtitle), `${s.title} | ${s.subtitle}`);
  await check("vr preview: a converted item opens in sbs on the screen", s.screen && s.screen.mode === "sbs" && s.screen.kind === "picture" && /name=sbs\.jpg/.test(s.screen.url), JSON.stringify(s.screen));
  const reqs = (await requests("/api/media/3d/file")).map((r) => r.path);
  await check("vr preview: only the sbs file is requested (no depth map)", reqs.length >= 1 && reqs.every((p) => /name=sbs/.test(p)) && !reqs.some((p) => /depth/.test(p)), JSON.stringify(reqs));
  const gl = h.host.renderer.getContext();
  const glErr = gl.getError();
  await check("vr preview: frames render and WebGL reports no error", s.frames > 5 && glErr === 0, `frames=${s.frames} glError=${glErr}`);
  await check("vr preview: the HUD lines are clean and say it is the desktop preview", s.hudLines.length === 12 && noBad(s.hudLines.join("\n")) && /desktop preview/.test(s.hudLines[4]) && /Wall/.test(s.hudLines[5]), s.hudLines.join(" | "));

  // dock: hover, trigger release over a button
  const p = h.projectButton("hud");
  move(p.clientX, p.clientY); await frames(3);
  await check("vr preview: the ray over a dock button highlights it", h.snapshot().hover === "hud", String(h.snapshot().hover));
  move(5, 5); await frames(3);
  await check("vr preview: the ray off the dock clears the highlight", h.snapshot().hover === null, String(h.snapshot().hover));
  const visible0 = h.snapshot().hudVisible;
  await clickButton("hud");
  await check("vr preview: the HUD button toggles the HUD panel", h.snapshot().hudVisible === !visible0, `${visible0} -> ${h.snapshot().hudVisible}`);
  await clickButton("hud");
  await check("vr preview: and back", h.snapshot().hudVisible === visible0, String(h.snapshot().hudVisible));
  // press on a button, release elsewhere: no action
  const pr = h.projectButton("hud");
  move(pr.clientX, pr.clientY); await frames(2); press(0); await frames(2);
  move(5, 5); await frames(2); release(0); await frames(2);
  await check("vr preview: a press that is released off the button does nothing", h.snapshot().hudVisible === visible0, String(h.snapshot().hudVisible));

  // recenter after turning the head (right-button drag)
  press(2); move(400, 300, { movementX: 160, movementY: 0 }); release(2); await frames(3);
  const before = JSON.stringify(h.snapshot().recenters);
  await clickButton("recenter");
  const rc = h.snapshot().recenters;
  await check("vr preview: recenter after a head turn moves the furniture to face the head again", JSON.stringify(rc) !== before && Math.abs(rc.yaw) > 0.3, `${before} -> ${JSON.stringify(rc)}`);
  await frames(2);
  await key("r");
  await check("vr preview: the A/X key recenters too (no crash, still rendering)", h.snapshot().frames > 0 && h.host.cleaned === false, "cleaned");
  await check("vr preview: still no WebGL error after interaction", gl.getError() === 0, String(gl.getError()));

  // Exit through the dock
  await clickButton("exit");
  let r = await Promise.race([h.ended, sleep(4000).then(() => null)]);
  await check("vr preview: the Exit button ends the session with 0 textures and 0 geometries left", r && r.textures === 0 && r.geometries === 0 && !r.error, JSON.stringify(r));
  await check("vr preview: the canvas and the overlay are gone", !document.getElementById("vrPreviewCanvas") && !document.getElementById("vrPreviewRoot"), "left over");
  const saved = L.loadVrState(mem);
  await check("vr preview: the place is kept in sessionStorage for the next start", saved && saved.context.view === "item" && saved.context.item.path === "e.png" && saved.context.source.path === "cats", JSON.stringify(saved));

  // Second start from the saved state: the dock distance comes back; Escape/B exits
  h = await A.startVr(saved.context, { preview: true, storage: mem, mediaTimeoutMs: 8000 });
  await waitFor(() => h.screen && h.screen.mounted, 8000);
  await key("Escape");
  await check("vr preview: B/Y with an item open is Back (V1): the viewer closes, the session stays", h.snapshot().view === "wall" && !h.host.cleaned, JSON.stringify(h.snapshot().view));
  await key("Escape");
  r = await Promise.race([h.ended, sleep(4000).then(() => null)]);
  await check("vr preview: B/Y at the wall's top level exits", r && r.textures === 0 && r.geometries === 0, JSON.stringify(r));

  // A 2D item: the original file, mono
  await configure({ role: "admin", files: FILES, status: {} });
  await clearLog();
  h = await A.startVr({ view: "item", item: { root: "downloads", path: "b.jpg", name: "b.jpg", kind: "picture" }, source: src }, { preview: true, storage: memStorage(), mediaTimeoutMs: 8000 });
  await waitFor(() => h.screen && h.screen.mounted && h.snapshot().frames > 4, 8000);
  s = h.snapshot();
  const fileReqs = (await requests("/api/media/file")).map((q) => q.path);
  const dReqs = (await requests("/api/media/3d/file")).map((q) => q.path);
  await check("vr preview: an unconverted item opens in 2D from the normal media url", s.screen && s.screen.mode === "mono" && fileReqs.some((q) => /path=b\.jpg/.test(q)) && dReqs.length === 0, `${JSON.stringify(s.screen)} ${JSON.stringify(fileReqs)} ${JSON.stringify(dReqs)}`);
  let glErr2 = -1;
  await check("vr preview: its dock subtitle says 2D, and WebGL is clean", /2D/.test(s.subtitle) && (glErr2 = h.host.renderer.getContext().getError()) === 0, `${s.subtitle} glError=${glErr2}`);
  await h.end();

  // The wall view: the real wall, no item, no screen
  h = await A.startVr({ view: "wall", source: src }, { preview: true, storage: memStorage() });
  await waitFor(() => h.snapshot().frames > 4 && h.snapshot().wall && h.snapshot().wall.tiles > 0, 8000);
  s = h.snapshot();
  await check("vr preview: the wall view builds the room with the wall and no screen", s.view === "wall" && s.screen === null && s.parts.wall && s.parts.dock && s.wall.tiles >= 4 && /of/.test(s.subtitle), JSON.stringify(s));
  r = await h.end().then(() => h.ended);
  await check("vr preview: the wall view cleans up too", r.textures === 0 && r.geometries === 0, JSON.stringify(r));
  h = await A.startVr({}, { preview: true, storage: memStorage() });
  await waitFor(() => h.snapshot().frames > 3 && h.snapshot().wall && h.snapshot().wall.tiles > 0, 8000);
  await check("vr preview: an empty context starts at the Library (V1: home is not built yet)", h.snapshot().view === "wall" && h.snapshot().source.kind === "folder" && h.snapshot().source.path === "", JSON.stringify(h.snapshot().source));
  await h.end();

  // A media file that fails: the dock says so (not bare), the session stays up
  await configure({ role: "admin", files: FILES, status: {} });
  h = await A.startVr({ view: "item", item: { root: "downloads", path: "a.mp4", name: "a.mp4", kind: "video" }, source: src }, { preview: true, storage: memStorage(), mediaTimeoutMs: 6000 });
  await waitFor(() => h.snapshot().message || h.snapshot().frames > 30, 9000);
  await sleep(500);
  s = h.snapshot();
  await check("vr preview: a video that cannot play is reported in the dock and the room stays", (s.message || "").length > 0 && !h.host.cleaned && noBad(s.message), JSON.stringify(s.message));
  await h.end();
}

// 20 enter/exit cycles leave nothing behind.
async function vrCycles() {
  const A = await import("/js/vr/app.js");
  const K = "2".repeat(32);
  const st = { state: "done", key: K, sbs_url: `/api/media/3d/file?key=${K}&name=sbs.jpg`, kind: "picture", eye_w: 1080, eye_h: 1440 };
  await configure({ role: "admin", files: FILES, status: { "e.png": st } });
  const mem = memStorage();
  const bad = [];
  for (let i = 0; i < 20; i += 1) {
    const h = await A.startVr({ view: "item", item: { root: "downloads", path: "e.png", kind: "picture", status: st }, source: { kind: "folder", root: "downloads", path: "", sort: {} } }, { preview: true, storage: mem, mediaTimeoutMs: 8000 });
    if (i % 3 !== 2) await waitFor(() => h.screen && h.screen.mounted, 8000);     // some exits come while still loading
    if (i % 5 === 4) { for (let k = 0; k < 2; k += 1) { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); await sleep(250); window.dispatchEvent(new KeyboardEvent("keyup", { key: "Escape" })); await sleep(150); } }
    else await h.end();
    const r = await Promise.race([h.ended, sleep(5000).then(() => ({ timeout: true }))]);
    if (!r || r.timeout || r.textures !== 0 || r.geometries !== 0 || document.getElementById("vrPreviewCanvas") || document.getElementById("vrPreviewRoot")) bad.push(`${i}:${JSON.stringify(r)}`);
  }
  await check("vr cycles: 20 enter/exit cycles leave 0 textures, 0 geometries and no canvas or overlay", bad.length === 0, bad.join(" ; "));
  await check("vr cycles: the state is saved in sessionStorage after exit", !!mem.getItem("mediadl.vr.state"), "no state");
}

// /vr.html standalone, and the server's allowlist for it.
async function vrPage() {
  const K = "3".repeat(32);
  const st = { state: "done", key: K, sbs_url: `/api/media/3d/file?key=${K}&name=sbs.jpg`, kind: "picture", eye_w: 1080, eye_h: 1440 };
  const E = (w) => w.document.getElementById.bind(w.document);
  for (const [mode, ok] of [["supported", true], ["unsupported", false], ["missing", false]]) {
    xrState.mode = mode;
    await configure({ role: "user", allowlist: "p8v0", files: FILES, status: { "e.png": st } });
    const w = await openPage("/vr.html?view=item&ir=downloads&ip=e.png&sk=folder&sr=downloads");
    await waitFor(() => /VR|WebXR/.test(E(w)("vrStatus").textContent) && E(w)("vrStatus").textContent.length > 5);
    await sleep(300);
    const e = E(w);
    const off = e("vrEnter").getAttribute("aria-disabled") === "true";
    await check(`vr page (XR ${mode}): Enter VR is ${ok ? "enabled" : "disabled-looking"}, status text says why`, off === !ok && noBad(e("vrStatus").textContent)
      && (ok ? /ready/i.test(e("vrStatus").textContent) : (mode === "missing" ? /no WebXR/.test(e("vrStatus").textContent) : /not available/.test(e("vrStatus").textContent))), `${off} ${e("vrStatus").textContent}`);
    await check(`vr page (XR ${mode}): the hint names SteamVR and OpenXR when VR is unavailable, and the page offers the desktop preview`,
      (ok ? e("vrHint").textContent === "" : /SteamVR/.test(e("vrHint").textContent) && /OpenXR/.test(e("vrHint").textContent)) && !e("vrPreview").disabled, e("vrHint").textContent);
    await check(`vr page (XR ${mode}): the start place comes from the URL query`, /e\.png/.test(e("vrStart").textContent), e("vrStart").textContent);
    const errs = await errorsOf(w);
    await check(`vr page (XR ${mode}): no console errors`, errs.length === 0, errs.join(" | "));
  }
  xrState.mode = "supported";
  await configure({ role: "user", allowlist: "p8v0", files: FILES, status: { "e.png": st } });
  await clearLog();
  let w = await openPage("/vr.html?view=item&ir=downloads&ip=e.png&sk=folder&sr=downloads");
  await waitFor(() => /ready/i.test(w.document.getElementById("vrStatus").textContent));
  w.document.getElementById("vrPreview").click();
  const up = await waitFor(() => w.document.getElementById("vrPreviewCanvas"), 8000);
  await sleep(1200);
  const files = (await requests("/api/media/3d/file")).map((r) => r.path);
  await check("vr page: Desktop preview shows the scene in the window and requests the item's sbs file", !!up && files.some((f) => /name=sbs\.jpg/.test(f)) && !files.some((f) => /depth/.test(f)), JSON.stringify(files));
  for (let k = 0; k < 2; k += 1) { w.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape" })); await sleep(250); w.dispatchEvent(new w.KeyboardEvent("keyup", { key: "Escape" })); await sleep(150); }
  const gone = await waitFor(() => !w.document.getElementById("vrPreviewCanvas") && !w.document.getElementById("vrPreviewRoot"), 5000);
  await check("vr page: Escape leaves the preview and the page is usable again", !!gone && !w.document.getElementById("vrEnter").disabled && !w.document.getElementById("vrPreview").disabled, "stuck");
  const saved = w.sessionStorage.getItem("mediadl.vr.state");
  await check("vr page: the place is saved in sessionStorage (V1: Back out of the viewer first, so it is the wall of the item's folder)", !!saved && /"view":"wall"/.test(saved) && /"path":""/.test(saved) && /downloads/.test(saved), String(saved));
  w = await openPage("/vr.html");
  await sleep(500);
  await check("vr page: without a query the page resumes at the saved place", /downloads/.test(w.document.getElementById("vrStart").textContent), w.document.getElementById("vrStart").textContent);
  w.sessionStorage.clear();
  w = await openPage("/vr.html");
  await sleep(300);
  await check("vr page: without a query or saved place it starts at home", /home/i.test(w.document.getElementById("vrStart").textContent), w.document.getElementById("vrStart").textContent);
  const errs2 = await errorsOf(w);
  await check("vr page: no console errors", errs2.length === 0, errs2.join(" | "));

  // refused session: a readable message on the page, nothing left over
  xrState.mode = "denied";
  w = await openPage("/vr.html?view=home");
  await waitFor(() => /ready/i.test(w.document.getElementById("vrStatus").textContent));
  w.document.getElementById("vrEnter").click();
  await waitFor(() => w.document.getElementById("vrMessage").textContent.length > 3, 5000);
  const msg = w.document.getElementById("vrMessage").textContent;
  await check("vr page: a refused session shows a readable message and re-enables the buttons", /blocked/i.test(msg) && noBad(msg) && !w.document.getElementById("vrEnter").disabled
    && !w.document.getElementById("vrViewerCanvas"), msg);
  xrState.mode = "supported";

  // the server: vr.html for a signed-in guest/user only with the V0 package; admin pages stay closed
  for (const role of ["user", "guest"]) {
    await configure({ role, allowlist: "p8srv" });
    let res = await fetch("/vr.html");
    await check(`vr server (${role}, before the V0 server package): vr.html is redirected to /media.html`, /\/media\.html$/.test(res.url), res.url);
    await configure({ role, allowlist: "p8v0" });
    res = await fetch("/vr.html");
    const txt = await res.text();
    await check(`vr server (${role}, with the V0 server package): vr.html is served`, /\/vr\.html$/.test(res.url) && /vrEnter/.test(txt), res.url);
    for (const other of ["/settings.html", "/index.html", "/admin.html"]) {
      res = await fetch(other);
      await check(`vr server (${role}): ${other} is still redirected`, /\/media\.html$/.test(res.url), res.url);
    }
  }
  await configure({ role: "admin", allowlist: "" });
}

// The P7 entry point still works as a shim.
async function vrShim() {
  const V = await import("/js/vr-viewer.js");
  let err = null;
  try { await V.enterVr({ url: "/api/media/3d/file?key=" + "4".repeat(32) + "&name=sbs.jpg", kind: "picture", key: "4".repeat(32), retryDelayMs: 10 }); } catch (e) { err = e; }
  await check("vr shim: enterVr without a headset session rejects with a readable message", err && err.message && noBad(err.message) && err.message.length > 10, String(err && err.message));
  await sleep(300);
  await check("vr shim: and leaves no canvas behind", !document.getElementById("vrViewerCanvas") && !document.getElementById("vrPreviewCanvas"), "canvas");
}


// ======================= P8 V1: the tile wall =======================
const mkFile = (name, type, extra) => ({ name, type, size: 1000, mtime: 1.7e9, added: 1.7e9, root: "downloads", path: name,
  url: `/api/media/file?root=downloads&path=${name}`, favorite: false, width: 1600, height: 1200, ...(extra || {}) });

// Pure modules: layout, scroll, settings, nav, auto-convert, loader, sources.
async function vrWallUnits() {
  const LA = await import("/js/vr/layout.js");
  const SC = await import("/js/vr/scroll.js");
  const ST = await import("/js/vr/settings.js");
  const NV = await import("/js/vr/nav.js");
  const AC = await import("/js/vr/autoconvert.js");
  const TL = await import("/js/vr/loader.js");
  const SO = await import("/js/vr/sources.js");
  const LG = await import("/js/vr/logic.js");
  const near = (a, b, e = 1e-6) => Math.abs(a - b) <= e;
  const note = (text) => stub("/__stub/note", { text });

  // ---- layout
  let seed = 12345;
  const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const asp = Array.from({ length: 500 }, () => 0.3 + rnd() * 3);
  const problems = [];
  for (const span of LA.WALL.SPANS) {
    const l = LA.layoutRows(asp, span);
    let badFill = 0, badAspect = 0, badOverlap = 0, badHit = 0;
    for (let r = 0; r < l.rows - 1; r += 1) {
      const a = l.rowStart[r], b = l.rowStart[r + 1] - 1;
      if (Math.abs(l.az[a] - l.w[a] / 2 + span / 2) > 0.01 || Math.abs(l.az[b] + l.w[b] / 2 - span / 2) > 0.01) badFill += 1;
    }
    for (let i = 0; i < l.n; i += 1) {
      const ar = l.w[i] / l.h[i];
      if (ar < 0.5 - 1e-3 || ar > 2.2 + 1e-3) badAspect += 1;
      if (i > 0 && l.row[i] === l.row[i - 1] && l.az[i] - l.w[i] / 2 < l.az[i - 1] + l.w[i - 1] / 2 - 1e-3) badOverlap += 1;
    }
    for (let i = 0; i < l.n; i += 7) {
      const r = l.row[i];
      const elev = LA.WALL.bandTopDeg - (l.rowTop[r] + l.rowH[r] / 2);
      if (LA.hitTile(l, l.az[i], elev, 0) !== i) badHit += 1;
    }
    if (badFill || badAspect || badOverlap || badHit) problems.push(`${span}: fill ${badFill} aspect ${badAspect} overlap ${badOverlap} hit ${badHit}`);
  }
  await check("wall layout: every row fills the span (90/120/150/180), aspects clamp to 0.5-2.2, no overlap, every tile's centre hits itself", problems.length === 0, problems.join(" ; "));
  const l120 = LA.layoutRows(asp, 120);
  const maxOff = LA.maxOffset(l120);
  await check("wall layout: max offset = total height minus the band; offsetForTile stays inside [0, max]",
    near(maxOff, Math.max(0, l120.totalDeg - LA.bandDeg()), 1e-4) && [0, 100, 250, 499].every((i) => {
      const o = LA.offsetForTile(l120, i);
      return o >= 0 && o <= maxOff + 1e-6;
    }), JSON.stringify({ maxOff, total: l120.totalDeg }));
  const hit = {};
  const t1 = LA.rayCylinder(0, 0, 0, 0, 0, -1, 2, hit);
  const az0 = hit.az;
  const t2 = LA.rayCylinder(0, 0, 0, Math.sin(Math.PI / 6), 0, -Math.cos(Math.PI / 6), 2, hit);
  await check("wall layout: a ray from the middle hits the cylinder at the radius, azimuth comes back right, a ray straight up misses",
    near(t1, 2) && near(az0, 0, 1e-6) && near(t2, 2, 1e-6) && near(hit.az, 30, 1e-6) && LA.rayCylinder(0, 0, 0, 0, 1, 0, 2, hit) === -1, `${t1} ${t2} ${hit.az}`);
  const big = Array.from({ length: 2000 }, (_, i) => [1.78, 0.56, 1, 1.33, 0.75][i % 5]);
  const tL0 = performance.now();
  let lb = null;
  for (let k = 0; k < 20; k += 1) lb = LA.layoutRows(big, 120);
  const layoutMs = (performance.now() - tL0) / 20;
  await note(`layout of 2000 tiles: ${layoutMs.toFixed(2)} ms, ${lb.rows} rows, ${lb.totalDeg.toFixed(0)} deg, max offset ${LA.maxOffset(lb).toFixed(0)} deg`);
  await check("wall layout: 2000 tiles lay out in well under a frame", layoutMs < 8 && lb.rows > 100, `${layoutMs} ms`);

  // ---- scroll
  const s1 = SC.createScroll();
  s1.setMax(100);
  for (let i = 0; i < 60; i += 1) s1.step(1 / 60, -1);
  const afterPush = s1.offset;
  const s1b = SC.createScroll(); s1b.setMax(100);
  for (let i = 0; i < 60; i += 1) s1b.step(1 / 60, 0.2);
  await check("wall scroll: the stick pushed forward moves the wall on (capped at 60 deg/s), inside the dead zone it does not",
    afterPush > 25 && afterPush <= 60 + 1e-6 && s1b.offset === 0, `${afterPush} ${s1b.offset}`);
  for (let i = 0; i < 600; i += 1) s1.step(1 / 60, -1);
  const atEnd = s1.offset;
  s1.jump(-5);
  const clampedLow = s1.offset;
  s1.jump(50); s1.top();
  await check("wall scroll: it stops at the ends without bouncing, jump clamps, top goes to 0", atEnd === 100 && clampedLow === 0 && s1.offset === 0 && s1.velocity === 0, `${atEnd} ${clampedLow}`);
  const s2 = SC.createScroll(); s2.setMax(200); s2.jump(50);
  s2.dragStart(0); s2.drag(10, 0.016); s2.drag(20, 0.016);
  const dragged = s2.offset;
  s2.dragEnd();
  const v0 = s2.velocity;
  let steps = 0;
  while (s2.velocity !== 0 && steps < 1000) { s2.step(1 / 60, 0); steps += 1; }
  await check("wall scroll: grip-drag follows the hand 1:1, inertia on release decays to a stop", near(dragged, 70, 1e-6) && v0 !== 0 && s2.velocity === 0 && steps > 10 && steps < 600 && s2.offset > dragged, `${dragged} v0 ${v0} steps ${steps}`);
  const bh = SC.createBackHold(1);
  const tap = [bh.update(true, 0.1), bh.update(false, 0.1)];
  const hold = [bh.update(true, 0.6), bh.update(true, 0.5), bh.update(true, 0.5), bh.update(false, 0.1)];
  await check("wall scroll: B/Y - a tap is Back on release, holding a second is Exit once (and no Back after it)", tap[0] === null && tap[1] === "back"
    && hold[0] === null && hold[1] === "exit" && hold[2] === null && hold[3] === null, JSON.stringify([tap, hold]));

  // ---- settings
  const d0 = ST.normalizeSettings(null);
  const mem = memStorage();
  ST.saveSettings(mem, { span: 150, radius: 2.5, text: "L" });
  const back = ST.loadSettings(mem);
  mem.setItem("mediadl.vr.settings", "{nope");
  const rows = ST.settingsRows(back);
  const lay = ST.settingsLayout(rows, 1000, 615);
  const inside = lay.every((b) => b.x >= 0 && b.y >= 0 && b.x + b.w <= 1000 && b.y + b.h <= 615);
  await check("wall settings: defaults 120 / 2 m / S, clamped to the offered values, saved and loaded, a broken store gives the defaults",
    d0.span === 120 && d0.radius === 2 && d0.text === "S" && ST.normalizeSettings({ span: 100, radius: 9, text: "Q" }).span === 90
    && ST.normalizeSettings({ span: 100, radius: 9, text: "Q" }).radius === 3 && ST.normalizeSettings({ text: "Q" }).text === "S"
    && back.span === 150 && back.radius === 2.5 && back.text === "L" && ST.loadSettings(mem).span === 120, JSON.stringify([d0, back]));
  await check("wall settings: every option is a button inside the panel, ids resolve (span:150, radius:3, text:M), unknown ids change nothing",
    inside && lay.some((b) => b.id === "close") && ST.applyChoice(d0, "span:150").span === 150 && ST.applyChoice(d0, "radius:3").radius === 3
    && ST.applyChoice(d0, "text:M").text === "M" && JSON.stringify(ST.applyChoice(d0, "bogus:1")) === JSON.stringify(d0), JSON.stringify(lay.map((b) => b.id)));

  // ---- nav
  const ctxItem = LG.normalizeContext({ view: "item", item: { root: "downloads", path: "big/f0007.jpg", name: "f0007.jpg", kind: "picture" }, source: { kind: "folder", root: "downloads", path: "big", sort: { key: "name", dir: "asc" } } });
  const p1 = NV.planStart(ctxItem);
  const p2 = NV.planStart(LG.normalizeContext({ view: "wall", source: { kind: "recent", offset: 40 } }));
  const p3 = NV.planStart(LG.normalizeContext({}));
  await check("wall nav: an item context plans the viewer on it with its own folder behind it; a wall context its source at its offset; nothing = the Library",
    p1.view === "item" && p1.item.path === "big/f0007.jpg" && p1.spec.kind === "folder" && p1.spec.path === "big"
    && p2.view === "wall" && p2.spec.kind === "recent" && p2.scrollIndex === 40 && p3.view === "wall" && p3.spec.kind === "folder" && p3.spec.path === "" && p3.spec.root === "", JSON.stringify([p1, p2, p3]));
  const nav = NV.createNav(p3);
  nav.go(SO.folderSpec("downloads", "a"), 14);
  nav.go(SO.folderSpec("downloads", "a/b"), 3);
  const v = nav.openViewer({ root: "downloads", path: "a/b/x.jpg" });
  {
    const n2 = NV.createNav({ spec: { kind: "root" }, scrollIndex: 0 });
    const sub = { kind: "folder", path: "sub" }, it = { root: "r", path: "sub/a.jpg" };
    n2.go(sub, 7); n2.openViewer(it);
    n2.back(); n2.back();
    const f1 = n2.forward(9), d1 = n2.depth, i1 = n2.walls[0].index, f2 = n2.forward(0), f3 = n2.forward(0);
    await check("vr nav: Forward undoes Back, the folder first, then the item, then nothing; it keeps the wall's scroll", f1.type === "wall" && f1.spec === sub && d1 === 2 && i1 === 9 && f2.type === "viewer" && f2.item === it && f3 === null, [f1, d1, i1, f2, f3]);
    n2.back(); n2.go({ kind: "folder", path: "other" }, 0);
    await check("vr nav: going somewhere new clears Forward", !n2.canForward && n2.forward(0) === null, n2.canForward);
  }
  const bk = [nav.back(), nav.back(), nav.back(), nav.back()];
  await check("wall nav: Back closes the viewer first, then pops walls with the index each was left at, and at the top means exit",
    v.path === "a/b/x.jpg" && bk[0].type === "viewer" && bk[1].type === "wall" && bk[1].spec.path === "a" && bk[1].index === 3 && bk[2].type === "wall" && bk[2].spec.path === "" && bk[2].index === 14
    && bk[3].type === "exit" && nav.depth === 1, JSON.stringify(bk));
  const favIts = [{ type: "folder", special: "favorites" }, { type: "picture" }, { type: "video" }];
  await check("wall nav: next/prev never land on the Favourites tile", NV.neighbour(favIts, 1, -1) === -1 && NV.neighbour(favIts, 2, -1) === 1 && NV.neighbour(favIts, 1, 1) === 2, "fav tile");
  const its = [{ type: "folder" }, { type: "folder" }, { type: "picture" }, { type: "video" }, { type: "folder" }, { type: "picture" }];
  await check("wall nav: next/prev skip folder tiles and stop at the ends; the count text and the dock buttons are right",
    NV.neighbour(its, 2, 1) === 3 && NV.neighbour(its, 3, 1) === 5 && NV.neighbour(its, 5, 1) === -1 && NV.neighbour(its, 3, -1) === 2 && NV.neighbour(its, 2, -1) === -1
    && NV.countText(0, 5, 2000) === "1 of 2,000" && NV.countText(0, 5, null) === "1 of 5" && NV.countText(0, 0, null) === "empty"
    && NV.dockButtons("wall").map((b) => b.id).join() === "back,top,search,filters,settings,hud,recenter,exit"
    && NV.dockButtons("viewer").map((b) => b.id).join() === "back,prev,next,hud,recenter,exit"
    && NV.dockButtons("viewer", { ready3d: true }).map((b) => b.id).includes("switch3d"), "nav helpers");

  // ---- auto convert (scripted IO)
  const mkAuto = (over) => {
    const posts = [], timers = [], states = [];
    const io = {
      pollMs: 1000,
      post: async (b) => { posts.push(b); return (over.reply || (() => ({ status: 200, json: { state: "queued" } })))(b, posts.length); },
      getStatus: async () => (over.status || (() => ({ state: "converting" })))(),
      setTimer: (fn, ms) => { const t = { id: timers.length + 1, fn, ms, live: true }; timers.push(t); return t.id; },
      clearTimer: (id) => { const t = timers.find((x) => x.id === id); if (t) t.live = false; },
      onState: (s) => states.push(s),
    };
    const fire = async () => { const t = timers.filter((x) => x.live).pop(); if (!t) return false; t.live = false; await t.fn(); await sleep(0); return true; };
    return { auto: AC.createAutoConvert(io), posts, timers, states, fire };
  };
  const itemU = { root: "downloads", path: "v/one.mp4", status: { state: "none" } };
  let m = mkAuto({});
  m.auto.open(itemU); await sleep(10);
  const phase1 = m.states.map((s) => s.phase).join();
  await check("wall auto: opening an unconverted item POSTs {root, path} once and starts polling", m.posts.length === 1 && JSON.stringify(m.posts[0]) === JSON.stringify({ root: "downloads", path: "v/one.mp4" })
    && phase1 === "requested" && m.timers.some((t) => t.live && t.ms === 1000), JSON.stringify([m.posts, phase1]));
  let st = { state: "converting" };
  m = mkAuto({ status: () => st });
  m.auto.open(itemU); await sleep(10);
  await m.fire();
  st = { state: "done", key: "k", sbs_url: "/x?name=sbs.mp4", kind: "video" };
  await m.fire();
  const last = m.states[m.states.length - 1];
  m.auto.leave(); await sleep(10);
  await check("wall auto: polling reports converting, then done with the status; leaving after done sends no leave", last.phase === "done" && last.status.sbs_url === "/x?name=sbs.mp4"
    && m.states.some((s) => s.phase === "converting") && m.posts.length === 1, JSON.stringify([m.states.map((s) => s.phase), m.posts]));
  m = mkAuto({});
  m.auto.open(itemU); await sleep(10);
  m.auto.leave(); await sleep(10);
  await check("wall auto: leaving before it is done sends the same call with leave:true, once", m.posts.length === 2 && m.posts[1].leave === true && m.posts[1].path === "v/one.mp4", JSON.stringify(m.posts));
  m = mkAuto({ reply: (b, n) => (n === 1 ? { status: 429, json: { retry_after_s: 7 } } : { status: 200, json: { state: "queued" } }) });
  m.auto.open(itemU); await sleep(10);
  const bo = m.states.find((s) => s.phase === "backoff");
  const waitMs = m.timers.filter((t) => t.live).map((t) => t.ms);
  await m.fire(); await sleep(10);
  await check("wall auto: 429 backs off for retry_after_s, then asks again while the item is open", !!bo && waitMs.join() === "7000" && m.posts.length === 2 && m.posts[1].leave === undefined, JSON.stringify([waitMs, m.posts]));
  for (const code of [403, 409]) {
    m = mkAuto({ reply: () => ({ status: code, json: { error: "off" } }) });
    m.auto.open(itemU); await sleep(10);
    m.auto.open({ root: "downloads", path: "v/two.mp4", status: { state: "none" } }); await sleep(10);
    await check(`wall auto: ${code} turns it off for the session: one request, silent (no storm on the next item)`, m.posts.length === 1 && m.auto.off && m.states.some((s) => s.phase === "off") && !m.timers.some((t) => t.live), JSON.stringify(m.posts));
  }
  m = mkAuto({});
  const ph = m.auto.open({ root: "downloads", path: "v/d.mp4", status: { state: "done", key: "k", sbs_url: "/y" } }); await sleep(10);
  await check("wall auto: an item that is already 3D sends nothing", ph === "done" && m.posts.length === 0, JSON.stringify(m.posts));

  // ---- thumbnail loader (fake IO)
  const mkLoader = (over) => {
    const pending = [], started = [], uploaded = [], disposedTex = [], clock = { t: 0 };
    let texId = 0, fetches = 0, closed = 0;
    const loader = TL.createThumbLoader(Object.assign({
      concurrency: 6, uploadsPerFrame: 3, cap: 1500, retryMs: 1500, now: () => clock.t,
      fetchThumb: (url) => { fetches += 1; started.push(url); return new Promise((res, rej) => pending.push({ url, res, rej })); },
      decode: async () => ({ width: 64, height: 32, close() { closed += 1; } }),
      makeTexture: (bmp, w, h) => ({ id: ++texId, w, h, disposed: false }),
      uploadTexture: () => {},
      disposeTexture: (t) => { t.disposed = true; disposedTex.push(t.id); },
      onTexture: (key) => uploaded.push(key),
    }, over || {}));
    return { loader, pending, started, uploaded, disposedTex, clock, get fetches() { return fetches; }, get closed() { return closed; } };
  };
  const want = (n, from = 0) => Array.from({ length: n }, (_, i) => ({ key: `k${from + i}`, url: `u${from + i}`, priority: from + i }));
  let L = mkLoader();
  L.loader.setWanted(want(20), 20);
  await sleep(5);
  const firstSix = L.started.join();
  await check("wall loader: at most 6 requests at a time, nearest tiles first", L.loader.inflight === 6 && firstSix === "u0,u1,u2,u3,u4,u5" && L.loader.queued === 14, `${L.loader.inflight} ${firstSix}`);
  L.pending.splice(0).forEach((p) => p.res({}));
  await sleep(10);
  const f1 = L.loader.frame();
  const f2 = L.loader.frame();
  const order = L.uploaded.join();
  await check("wall loader: at most 3 uploads per frame, nearest first; finished downloads make room for the next ones", f1 === 3 && f2 === 3 && order === "k0,k1,k2,k3,k4,k5" && L.loader.textureCount === 6 && L.loader.inflight === 6, `${f1} ${f2} ${order} inflight ${L.loader.inflight}`);
  L.loader.setWanted(want(3, 100), 3);
  await sleep(5);
  await check("wall loader: tiles that are no longer wanted are dropped (in-flight ones aborted and counted), their textures stay until the LRU needs the room",
    L.loader.stats.aborted >= 6 && L.loader.textureCount === 6 && L.loader.inflight <= 3, JSON.stringify(L.loader.stats));
  L.loader.dispose();
  await check("wall loader: dispose leaves 0 textures, every texture disposed", L.loader.textureCount === 0 && L.disposedTex.length === 6, `${L.loader.textureCount} ${L.disposedTex.length}`);
  L = mkLoader({ cap: 5 });
  L.loader.setWanted(want(8), 8);
  for (let round = 0; round < 6; round += 1) { await sleep(5); L.pending.splice(0).forEach((p) => p.res({})); await sleep(5); L.loader.frame(); L.loader.frame(); L.loader.frame(); }
  const have8 = L.loader.textureCount;
  L.loader.setWanted(want(3, 50), 3);
  for (let round = 0; round < 4; round += 1) { await sleep(5); L.pending.splice(0).forEach((p) => p.res({})); await sleep(5); L.loader.frame(); L.loader.frame(); }
  await check("wall loader: the texture LRU is capped (8 loaded then 3 wanted, cap 5): the oldest unwanted go first, the wanted ones stay",
    have8 === 8 && L.loader.textureCount === 5 && L.disposedTex.join() === "1,2,3,4,5,6" && L.loader.stats.evicted === 6 && L.loader.get("k50") !== null, `${have8} ${L.loader.textureCount} ${L.disposedTex.join()}`);
  L.loader.dispose();
  L = mkLoader();
  L.loader.setWanted(want(1), 1);
  await sleep(5);
  L.pending.splice(0).forEach((p) => p.rej(Object.assign(new Error("HTTP 404"), { status: 404 })));
  await sleep(10);
  L.clock.t += 10000; L.loader.pump(); await sleep(5);
  await check("wall loader: a 404 (no thumbnail) is final - one request, the placeholder stays", L.fetches === 1 && L.loader.info("k0").state === "failed" && L.loader.stats.failed === 1, `${L.fetches} ${JSON.stringify(L.loader.info("k0"))}`);
  L.loader.dispose();
  L = mkLoader();
  L.loader.setWanted(want(1), 1);
  await sleep(5);
  L.pending.splice(0).forEach((p) => p.rej(Object.assign(new Error("HTTP 500"), { status: 500 })));
  await sleep(10);
  const waiting = L.loader.info("k0").state;
  L.loader.pump(); await sleep(5);
  const noEarly = L.fetches;
  L.clock.t += 2000; L.loader.pump(); await sleep(5);
  const retried = L.fetches;
  L.pending.splice(0).forEach((p) => p.rej(Object.assign(new Error("HTTP 500"), { status: 500 })));
  await sleep(10);
  await check("wall loader: another failure is retried once after a pause, then it is final", waiting === "queued" && noEarly === 1 && retried === 2 && L.loader.info("k0").state === "failed", `${waiting} ${noEarly} ${retried} ${L.loader.info("k0").state}`);
  L.loader.dispose();

  // ---- sources (scripted getJson)
  const urls = [];
  const mkGet = (answers) => async (url) => {
    urls.push(url);
    const a = answers(url);
    if (a instanceof Error) throw a;
    return a;
  };
  const recentAll = Array.from({ length: 250 }, (_, i) => mkFile(`r${String(i).padStart(3, "0")}.jpg`, "image"));
  const recentGet = mkGet((u) => { const o = +/offset=(\d+)/.exec(u)[1], lim = +/limit=(\d+)/.exec(u)[1]; return { items: recentAll.slice(o, o + lim), total: 250, has_more: o + lim < 250 }; });
  let src = SO.createSource({ kind: "recent" }, { getJson: recentGet });
  await src.loadMore();
  const c1 = src.items.length, more1 = src.hasMore;
  await src.ensure(240);
  await check("wall sources: Recent pages by offset/limit 100, hasMore until the end, total known, ensure() pages until an index exists",
    c1 === 100 && more1 && src.items.length === 250 && !src.hasMore && src.total === 250 && /offset=0&limit=100/.test(urls[0]) && /offset=100&limit=100/.test(urls[1]) && /offset=200&limit=100/.test(urls[2]) && urls.length === 3
    && src.indexOf("downloads", "r123.jpg") === 123, `${c1} ${src.items.length} ${urls.join(" ")}`);
  const browseGet = mkGet(() => ({ root: "downloads", root_name: "Downloads", path: "x/y", is_root_listing: false, multi_root: false, dirs: ["zdir", "adir"],
    files: [mkFile("b10.jpg", "image", { path: "x/y/b10.jpg" }), mkFile("b2.jpg", "image", { path: "x/y/b2.jpg" }), mkFile("song.mp3", "audio", { path: "x/y/song.mp3" }), mkFile("anim.gif", "image", { path: "x/y/anim.gif" }), mkFile("c.mp4", "video", { path: "x/y/c.mp4" })] }));
  src = SO.createSource(SO.folderSpec("downloads", "x/y"), { getJson: browseGet });
  await src.loadMore();
  await check("wall sources: a folder lists its folder tiles first (as the server sent them), then viewable files (audio and gif dropped) in natural name order; the breadcrumb is root / parts",
    src.items.map((i) => i.type).join() === "folder,folder,picture,picture,video" && src.items[0].name === "zdir" && src.items[0].path === "x/y/zdir" && src.items[2].name === "b2.jpg" && src.items[3].name === "b10.jpg"
    && src.crumbs.map((c) => c.label).join(" / ") === "Downloads / x / y" && src.title === "Downloads / x / y" && !src.hasMore && src.total === 5, JSON.stringify(src.items.map((i) => i.name)));
  const rootGet = mkGet(() => ({ is_root_listing: true, multi_root: true, roots: [{ id: "downloads", name: "Downloads" }, { id: "movies", name: "Movies" }] }));
  src = SO.createSource(SO.folderSpec("", ""), { getJson: rootGet });
  await src.loadMore();
  await check("wall sources: the root listing is the Favourites tile first, then one folder tile per library, opening them is root + empty path", src.items.length === 3 && src.items.every((i) => i.type === "folder" && i.path === "") && src.items[0].special === "favorites" && src.items[0].name === "Favourites" && src.items[2].root === "movies" && src.title === "Libraries", JSON.stringify(src.items));
  const subGet = mkGet(() => ({ root: "downloads", root_name: "Downloads", path: "x", is_root_listing: false, multi_root: true, dirs: ["d"], files: [] }));
  const subSrc = SO.createSource(SO.folderSpec("downloads", "x"), { getJson: subGet });
  await subSrc.loadMore();
  const favSrc = SO.createSource(SO.favouritesSpec({}), { getJson: mkGet(() => ({ items: [], total: 0, has_more: false })) });
  await check("wall sources: the Favourites tile exists only in the root listing; it opens the favourites source titled Libraries / Favourites",
    subSrc.items.every((i) => i.special !== "favorites") && favSrc.spec.kind === "favorites" && favSrc.title === "Libraries / Favourites" && favSrc.crumbs.map((c) => c.label).join() === "Libraries,Favourites", JSON.stringify([subSrc.items, favSrc.title]));
  urls.length = 0;
  src = SO.createSource({ kind: "favorites", query: "", filters: "" }, { getJson: mkGet(() => ({ items: [], total: 0, has_more: false })) });
  await src.loadMore();
  const favUrl = urls[0];
  urls.length = 0;
  // A page the server shortened (gone or empty files left out) must not make the next request overlap it.
  const shortUrls = [];
  src = SO.createSource({ kind: "search", query: "x", mode: "filename", filters: "" }, { getJson: async (u) => {
    shortUrls.push(u);
    const off = +/offset=(\d+)/.exec(u)[1];
    return { items: off === 100 ? [] : [{ root: "r", path: `f${off}.jpg`, name: `f${off}.jpg` }], offset: off, limit: 100, total: 250, has_more: off + 100 < 250 };
  } });
  await src.loadMore(); await src.loadMore(); await src.loadMore();
  await check("wall sources: paging steps by the server's page, so a shortened or emptied page neither repeats tiles nor ends the list",
    shortUrls.map((u) => +/offset=(\d+)/.exec(u)[1]).join() === "0,100,200" && src.items.length === 2 && !src.hasMore, shortUrls.join(" "));
  src = SO.createSource({ kind: "search", query: "sun set", mode: "filename", filters: "" }, { getJson: mkGet(() => ({ items: [], total: 0, has_more: false })) });
  await src.loadMore();
  await check("wall sources: favourites and search use the search route with their parameters", /\/api\/media\/search\?favorites=1&offset=0&limit=100/.test(favUrl) && /q=sun%20set&mode=filename/.test(urls[0]), `${favUrl} ${urls[0]}`);
  src = SO.createSource({ kind: "recent" }, { getJson: mkGet(() => new SO.ApiError(401, "no")) });
  await src.loadMore();
  const src2 = SO.createSource({ kind: "recent" }, { getJson: mkGet(() => new SO.ApiError(-1, "network", { network: true })) });
  await src2.loadMore();
  await check("wall sources: a lapsed session (401) is flagged expired, a network error is flagged, neither retries", src.error && src.error.expired && !src.hasMore && src2.error && src2.error.network && !src2.error.expired, JSON.stringify([src.error, src2.error]));
}

// The whole wall in the desktop preview with the stub library (one browser, real WebGL via SwiftShader).
async function vrWallApp() {
  const A = await import("/js/vr/app.js");
  const info = await stub("/__stub/info");
  const note = (text) => stub("/__stub/note", { text });
  const errs = [];
  const ce = console.error;
  console.error = (...a) => { errs.push(a.map(String).join(" ")); ce.apply(console, a); };
  const onerr = (e) => errs.push("error: " + (e && e.message));
  const onrej = (e) => errs.push("rejection: " + (e && e.reason && e.reason.message || e.reason));
  window.addEventListener("error", onerr);
  window.addEventListener("unhandledrejection", onrej);

  let h = null;
  const canvas = () => document.getElementById("vrPreviewCanvas");
  const move = (x, y, extra) => canvas().dispatchEvent(new PointerEvent("pointermove", { clientX: x, clientY: y, bubbles: true, ...(extra || {}) }));
  const press = (button) => canvas().dispatchEvent(new PointerEvent("pointerdown", { button, bubbles: true }));
  const release = (button) => window.dispatchEvent(new PointerEvent("pointerup", { button, bubbles: true }));
  const keyDown = (k) => window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true }));
  const keyUp = (k) => window.dispatchEvent(new KeyboardEvent("keyup", { key: k, bubbles: true }));
  const frames = async (n) => { const f0 = h.snapshot().frames; await waitFor(() => h.snapshot().frames >= f0 + n, 8000); };
  const start = async (ctx, opts) => {
    h = await A.startVr(ctx, Object.assign({ preview: true, storage: memStorage(), settingsStorage: memStorage(), autoPollMs: 150, mediaTimeoutMs: 8000 }, opts || {}));
    return h;
  };
  const stop = async () => { await h.end(); return Promise.race([h.ended, sleep(5000).then(() => ({ timeout: true }))]); };
  const posts = async () => (await requests("/api/media/3d/view")).map((r) => r.body);
  const PK = "p".repeat(32), K1 = "5".repeat(32), K2 = "6".repeat(32);
  const sbsOf = (key, kind, name) => ({ state: "done", key, kind, sbs_url: `/api/media/3d/file?key=${key}&name=${name || (kind === "video" ? "sbs.mp4" : "sbs.jpg")}`, eye_w: kind === "video" ? 64 : 1080, eye_h: kind === "video" ? 64 : 1440 });
  const LIB = [mkFile("a.mp4", "video"), mkFile("b.jpg", "image"), mkFile("c.mp3", "audio"), mkFile("d.gif", "image"), mkFile("e.png", "image")];
  const SUB = [mkFile("s1.jpg", "image", { path: "sub/s1.jpg" }), mkFile("s2.mp4", "video", { path: "sub/s2.mp4" })];
  const base = { role: "admin", files: LIB, with_big: true, tree: { sub: { files: SUB } }, status: {}, list: [], recent: [], view_mode: "ok", multiroot: false };
  const wallReady = (n) => waitFor(() => h.wall && h.wall.items.length >= n && h.snapshot().frames > 3, 12000);

  // ---- Library start, folders first, open a folder, breadcrumb, Back
  await configure(base);
  await clearLog();
  await start({});
  await wallReady(5);
  let s = h.snapshot();
  await check("wall app: with no context it starts in the Library: folder tiles first, then viewable files (audio and gif dropped)",
    s.view === "wall" && h.wall.items.map((i) => i.type).join() === "folder,folder,video,picture,picture" && h.wall.items[0].name === "sub" && h.wall.items[1].name === "big"
    && s.buttons.join() === "back,top,search,filters,settings,hud,recenter,exit" && s.title === "Downloads" && /1 of 5/.test(s.subtitle), JSON.stringify([s.title, s.subtitle, h.wall.items.map((i) => i.name)]));
  await check("wall app: the HUD reports the wall (tiles, meshes, thumbs, queue, upload) cleanly", s.hudLines.length === 12 && /Wall 5 tiles/.test(s.hudLines[5]) && noBad(s.hudLines.join("\n")), s.hudLines.join(" | "));
  await waitFor(() => h.wall.stats().textures >= 3, 8000);
  const thumbReqs = (await requests("/api/media/thumbnail")).length;
  await check("wall app: thumbnails for the 3 files are fetched and uploaded as textures (folder tiles draw their own card)", h.wall.stats().textures >= 3 && thumbReqs >= 3 && thumbReqs <= 5, `${JSON.stringify(h.wall.stats())} reqs ${thumbReqs}`);
  h.select(0);
  await waitFor(() => h.snapshot().source && h.snapshot().source.path === "sub" && h.wall.items.length === 2, 6000);
  s = h.snapshot();
  await check("wall app: opening a folder tile loads that folder; the dock shows the breadcrumb Downloads / sub, the stack is 2 deep",
    s.depth === 2 && s.title === "Downloads / sub" && h.wall.items.length === 2 && h.wall.items[0].name === "s1.jpg" && s.buttons.includes("back"), JSON.stringify([s.depth, s.title, s.source]));
  h.select(0);
  await waitFor(() => h.snapshot().viewer, 4000);
  h.back();
  await waitFor(() => !h.snapshot().viewer, 4000);
  await check("wall app: Back from the viewer closes it first (the folder wall stays)", h.snapshot().depth === 2 && h.snapshot().view === "wall" && h.snapshot().title === "Downloads / sub", JSON.stringify([h.snapshot().depth, h.snapshot().view]));
  h.back();
  await waitFor(() => h.snapshot().depth === 1, 4000);
  await frames(2);
  s = h.snapshot();
  await check("wall app: Back from a folder returns to the Library wall at once (the stack keeps it)", s.depth === 1 && s.title === "Downloads" && h.wall.items.length === 5 && s.source.path === "", JSON.stringify([s.depth, s.title, h.wall.items.length]));
  h.select(1);
  await waitFor(() => h.snapshot().source && h.snapshot().source.path === "big" && h.wall.items.length === 2000, 12000);
  s = h.snapshot();
  await check("wall app: the 2000-file folder opens with its count in the dock", s.title === "Downloads / big" && /of 2,000/.test(s.subtitle) && h.wall.items.length === 2000, JSON.stringify([s.title, s.subtitle]));
  h.back();
  await waitFor(() => h.snapshot().depth === 1, 4000);
  h.back();                                                   // at the top of the stack: Exit VR
  let r = await Promise.race([h.ended, sleep(4000).then(() => null)]);
  await check("wall app: Back at the Library top level exits with 0 textures and 0 geometries left", r && r.textures === 0 && r.geometries === 0 && !r.error, JSON.stringify(r));

  // ---- multi-root: Libraries tiles
  await configure({ ...base, multiroot: true });
  await start({});
  await wallReady(2);
  s = h.snapshot();
  await check("wall app: with several libraries the start is the root listing (Favourites tile first, then one folder tile each, title Libraries)", s.title === "Libraries" && h.wall.items.length === 3 && h.wall.items.every((i) => i.type === "folder")
    && h.wall.items[0].special === "favorites" && h.wall.items[0].name === "Favourites" && h.wall.items[1].name === "Downloads", JSON.stringify([s.title, h.wall.items.map((i) => i.name)]));
  h.select(2);
  await waitFor(() => h.snapshot().source && h.snapshot().source.root === "movies", 5000);
  await check("wall app: a library tile opens that root and Back returns to the listing", h.snapshot().depth === 2 && /movies/i.test(h.snapshot().title), h.snapshot().title);
  h.back();
  await waitFor(() => h.snapshot().depth === 1, 4000);
  // the Favourites tile: opens the stub's favourites, breadcrumb, viewer skips nothing odd, Back returns to the roots wall
  await configure({ ...base, multiroot: true, search_items: [mkFile("fav1.jpg", "image", { favorite: true }), mkFile("fav2.jpg", "image", { favorite: true })] });
  await clearLog();
  h.select(0);
  await waitFor(() => h.snapshot().source && h.snapshot().source.kind === "favorites" && h.wall.items.length === 2, 6000);
  s = h.snapshot();
  const favReq = (await requests("/api/media/search")).map((q) => q.path).join(" ");
  await check("wall app: the Favourites tile opens the favourites source (search?favorites=1), lists the stub's favourites, breadcrumb Libraries / Favourites, stack 2 deep",
    s.depth === 2 && s.title === "Libraries / Favourites" && h.wall.items.map((i) => i.name).join() === "fav1.jpg,fav2.jpg" && /favorites=1/.test(favReq) && s.buttons.includes("back"), JSON.stringify([s.depth, s.title, favReq]));
  h.select(0);
  await waitFor(() => h.snapshot().viewer, 4000);
  await h.next();
  await waitFor(() => h.snapshot().viewer && h.snapshot().viewer.path === "fav2.jpg", 4000);
  await h.prev();
  await h.prev();
  await sleep(200);
  await check("wall app: viewer Prev/Next walk the favourites files and never land on a folder-like tile", h.snapshot().viewer && h.snapshot().viewer.path === "fav1.jpg" && h.wall.items.every((i) => i.type !== "folder"), JSON.stringify(h.snapshot().viewer));
  h.back();
  await waitFor(() => !h.snapshot().viewer, 4000);
  h.back();
  await waitFor(() => h.snapshot().depth === 1, 4000);
  await frames(2);
  s = h.snapshot();
  await check("wall app: Back from Favourites returns to the roots wall (Favourites tile first, 3 tiles, title Libraries)", s.depth === 1 && s.title === "Libraries" && h.wall.items.length === 3 && h.wall.items[0].special === "favorites" && h.wall.firstVisible() === 0, JSON.stringify([s.depth, s.title, h.wall.items.length]));
  await stop();
  await configure(base);

  // ---- 3D badges from /3d/list, 2D viewer + auto convert, picture swap in place, leave
  await configure({ ...base, status: {}, list: [{ ...sbsOf(K2, "picture"), root: "downloads", path: "e.png", title: "e" }] });
  await clearLog();
  await start({});
  await wallReady(5);
  await waitFor(() => h.wall.items.find((i) => i.path === "e.png").status, 4000);
  const ei = h.wall.items.findIndex((i) => i.path === "e.png");
  await check("wall app: items with a finished conversion in /api/media/3d/list get the 3D badge state", h.wall.items[ei].status && h.wall.items[ei].status.state === "done" && !h.wall.items.find((i) => i.path === "b.jpg").status, JSON.stringify(h.wall.items.map((i) => i.status)));
  const bi = h.wall.items.findIndex((i) => i.path === "b.jpg");
  await clearLog();
  h.select(bi);
  await waitFor(() => h.screen && h.screen.mounted, 8000);
  await sleep(300);
  s = h.snapshot();
  let ps = await posts();
  await check("wall app: opening an unconverted picture shows it in 2D at once and POSTs /3d/view once with {root, path}", s.view === "item" && s.screen.mode === "mono" && s.screen.kind === "picture"
    && ps.length === 1 && JSON.stringify(ps[0]) === JSON.stringify({ root: "downloads", path: "b.jpg" }), JSON.stringify([s.screen, ps]));
  await check("wall app: the wall dims to 30 % behind the viewer, the dock shows the viewer buttons and the converting note", h.wall.dim === 0.3 && s.buttons.join() === "back,prev,next,hud,recenter,exit" && /converting to 3D/.test(s.subtitle) && /2D/.test(s.subtitle), JSON.stringify([h.wall.dim, s.buttons, s.subtitle]));
  const polls = (await requests("/api/media/3d/status")).filter((q) => /path=b\.jpg/.test(q.path)).length;
  const scr0 = h.screen;
  await configure({ status: { "b.jpg": sbsOf(K1, "picture") } });
  await waitFor(() => h.snapshot().screen && h.snapshot().screen.mode === "sbs", 6000);
  s = h.snapshot();
  await check("wall app: the status is polled while the item is open; when it is done the picture swaps to stereo in place (new screen, same item, no click)",
    polls >= 1 && s.screen.mode === "sbs" && /name=sbs\.jpg/.test(s.screen.url) && h.screen !== scr0 && s.view === "item" && s.viewer.path === "b.jpg" && /3D/.test(s.subtitle) && !/converting/.test(s.subtitle), JSON.stringify([polls, s.screen, s.subtitle]));
  await check("wall app: the item's tile gets its 3D badge state too", h.wall.items[bi].status && h.wall.items[bi].status.state === "done", JSON.stringify(h.wall.items[bi].status));
  await clearLog();
  h.back();
  await sleep(500);
  ps = await posts();
  await check("wall app: leaving an item that finished sends no leave", ps.length === 0, JSON.stringify(ps));
  await check("wall app: Back from the viewer lands on the wall with the item's tile in the band and the wall undimmed", h.snapshot().view === "wall" && h.wall.dim === 1 && !h.snapshot().viewer
    && (() => { const l = h.wall.layout; const rr = l.row[bi]; const o = h.wall.scroll.offset; return l.rowTop[rr] >= o - 1 && l.rowTop[rr] <= o + 55; })(), JSON.stringify([h.snapshot().view, h.wall.dim]));
  // leave before done
  await clearLog();
  h.select(h.wall.items.findIndex((i) => i.path === "a.mp4"));
  await sleep(400);
  h.back();
  await sleep(600);
  ps = await posts();
  await check("wall app: leaving before it is done sends the same call with leave:true", ps.length === 2 && ps[0].leave === undefined && ps[1].leave === true && ps[1].path === "a.mp4", JSON.stringify(ps));
  // mouse: hover then click a tile through the real picking path
  await frames(3);
  const pt = h.projectTile(bi);
  move(pt.clientX, pt.clientY);
  await frames(4);
  await check("wall app: the ray over a tile highlights it (hover index)", !pt.behind && h.wall.hover === bi, `hover ${h.wall.hover} want ${bi} ${JSON.stringify(pt)}`);
  press(0); await frames(2); release(0);
  await waitFor(() => h.snapshot().viewer, 5000);
  await check("wall app: trigger release over a tile opens it in the viewer (analytic picking)", h.snapshot().viewer && h.snapshot().viewer.path === "b.jpg", JSON.stringify(h.snapshot().viewer));
  // next / prev walk the wall's order and skip folders
  await h.prev();
  await waitFor(() => h.snapshot().viewer && h.snapshot().viewer.path === "a.mp4", 3000);
  const at1 = h.snapshot().viewer.path;
  await h.prev();
  await sleep(300);
  await check("wall app: Prev walks the wall's order, skips the folder tiles and stops at the first file", at1 === "a.mp4" && h.snapshot().viewer.path === "a.mp4", at1);
  await h.next(); await h.next();
  await waitFor(() => h.snapshot().viewer && h.snapshot().viewer.path === "e.png", 3000);
  await check("wall app: Next walks on (a.mp4 -> b.jpg -> e.png)", h.snapshot().viewer.path === "e.png", JSON.stringify(h.snapshot().viewer));
  await sleep(300);
  await check("wall app: an item that is already 3D opens in stereo with no /3d/view call for it", h.snapshot().screen && h.snapshot().screen.mode === "sbs" && !(await posts()).some((p) => p.path === "e.png" && !p.leave), JSON.stringify([h.snapshot().screen, await posts()]));
  r = await stop();
  await check("wall app: exit with the viewer open cleans up", r.textures === 0 && r.geometries === 0, JSON.stringify(r));

  // ---- 429, 403
  await configure({ ...base, view_mode: "429", retry_after_s: 1, status: {} });
  await clearLog();
  await start({ view: "item", item: { root: "downloads", path: "b.jpg", name: "b.jpg", kind: "picture" }, source: { kind: "folder", root: "downloads", path: "", sort: {} } });
  await waitFor(() => h.screen && h.screen.mounted, 8000);
  await sleep(500);
  const n1 = (await posts()).length;
  const mode1 = h.snapshot().screen && h.snapshot().screen.mode;
  await configure({ view_mode: "ok" });
  await sleep(1400);
  const n2 = (await posts()).length;
  await check("wall app: a 429 backs off (one request, 2D stays, no storm), then asks again after retry_after_s", n1 === 1 && mode1 === "mono" && n2 >= 2 && n2 <= 3, `n1 ${n1} n2 ${n2} ${mode1}`);
  await stop();
  await configure({ ...base, view_mode: "403", status: {} });
  await clearLog();
  await start({ view: "wall", source: { kind: "folder", root: "downloads", path: "", sort: {} } });
  await wallReady(5);
  h.select(h.wall.items.findIndex((i) => i.path === "b.jpg"));
  await waitFor(() => h.screen && h.screen.mounted, 8000);
  await sleep(300);
  await h.next();
  await sleep(500);
  ps = await posts();
  await check("wall app: a 403 (off for this user) leaves it in 2D silently: one request for the session, no leave, no error text", ps.length === 1 && h.snapshot().screen.mode === "mono" && !/failed|error|403/i.test(h.snapshot().subtitle), JSON.stringify([ps, h.snapshot().subtitle]));
  await stop();

  // ---- video: 2D first, 3D prompt, switch keeps the position
  if (!info.has_video) {
    await note("video checks skipped: the stub server could not make a test video (no ffmpeg)");
  } else {
    await configure({ ...base, files: [...LIB, mkFile("play.mp4", "video", { duration: 20 })], status: {}, view_mode: "ok" });
    await clearLog();
    await start({ view: "item", item: { root: "downloads", path: "play.mp4", name: "play.mp4", kind: "video" }, source: { kind: "folder", root: "downloads", path: "", sort: {} } });
    await waitFor(() => h.screen && h.screen.mounted && h.screen.video && h.screen.video.readyState >= 2, 12000);
    const vid0 = h.screen.video;
    vid0.currentTime = 3.2;
    await waitFor(() => Math.abs(vid0.currentTime - 3.2) < 0.4 && !vid0.seeking, 6000);
    const t0 = vid0.currentTime;
    s = h.snapshot();
    await check("wall app: an unconverted video opens as a 2D video on the screen and asks for 3D", s.screen.mode === "mono" && s.screen.kind === "video" && (await posts()).length === 1, JSON.stringify([s.screen, await posts()]));
    await configure({ status: { "play.mp4": sbsOf(PK, "video") } });
    await waitFor(() => h.snapshot().ready3d, 6000);
    s = h.snapshot();
    await check("wall app: when the video's 3D is done the app only OFFERS it: a switch button and a message, the playing 2D video is untouched (same element, position kept)",
      s.ready3d && s.buttons.includes("switch3d") && /switch/i.test(s.subtitle) && s.screen.mode === "mono" && h.screen.video === vid0 && vid0.currentTime >= t0 - 0.1, JSON.stringify([s.buttons, s.subtitle, t0, vid0.currentTime]));
    await h.switch3d();
    await waitFor(() => h.snapshot().screen && h.snapshot().screen.mode === "sbs" && h.screen.mounted && h.screen.video && h.screen.video.readyState >= 2, 12000);
    const t1 = h.screen.video.currentTime;
    s = h.snapshot();
    await check("wall app: switching to 3D shows the stereo video at the same playback position", s.screen.mode === "sbs" && /name=sbs\.mp4/.test(s.screen.url) && t1 >= t0 - 0.5 && t1 < 19, `t0 ${t0} t1 ${t1}`);
    await check("wall app: the switch button is gone after switching", !h.snapshot().buttons.includes("switch3d") && !h.snapshot().ready3d, JSON.stringify(h.snapshot().buttons));
    await stop();
  }

  // ---- start context
  await configure({ ...base, status: {}, view_mode: "ok" });
  await clearLog();
  const bigSrc = { kind: "folder", root: "downloads", path: "big", query: "", filters: "", sort: { key: "name", dir: "asc" }, offset: 0 };
  await start({ view: "item", item: { root: "downloads", path: "big/f1501.jpg", name: "f1501.jpg", kind: "picture" }, source: bigSrc });
  await waitFor(() => h.wall && h.wall.items.length === 2000 && h.snapshot().viewer && h.screen && h.screen.mounted, 15000);
  await sleep(300);
  const wi = h.wall.items.findIndex((i) => i.path === "big/f1501.jpg");
  const lay = h.wall.layout;
  const inBand = () => { const o = h.wall.scroll.offset; const rr = lay.row[wi]; return lay.rowTop[rr] >= o - 1 && lay.rowTop[rr] + lay.rowH[rr] <= o + 56; };
  ps = await posts();
  await check("wall app: opened from an item: the viewer is on that item (2D, its own folder as the wall behind, dimmed), scrolled so the item's row is in view",
    h.snapshot().view === "item" && wi === 1501 && h.snapshot().viewer.path === "big/f1501.jpg" && h.wall.dim === 0.3 && h.wall.scroll.offset > 100 && inBand() && h.snapshot().screen.mode === "mono", JSON.stringify([wi, h.wall.scroll.offset, h.wall.dim]));
  await check("wall app: ... and it asked for 3D for exactly that item", ps.length === 1 && ps[0].path === "big/f1501.jpg" && ps[0].leave === undefined, JSON.stringify(ps));
  h.back();
  await sleep(400);
  const o2 = h.wall.scroll.offset, mid = o2 + 55 / 2, rr2 = lay.row[wi];
  await check("wall app: Back from the viewer shows the wall scrolled to that item (centred), undimmed", h.snapshot().view === "wall" && h.wall.dim === 1 && Math.abs(lay.rowTop[rr2] + lay.rowH[rr2] / 2 - mid) < 12, JSON.stringify([o2, lay.rowTop[rr2]]));
  await stop();
  await clearLog();
  await start({ view: "wall", source: { ...bigSrc, offset: 600 } });
  await waitFor(() => h.wall && h.wall.items.length === 2000 && h.snapshot().frames > 4, 15000);
  await sleep(300);
  const fv = h.wall.firstVisible();
  await check("wall app: opened from the main view: that wall at the current position (first visible near tile 600), no viewer, no screen, no conversion request",
    h.snapshot().view === "wall" && fv >= 575 && fv <= 600 && h.snapshot().screen === null && !h.snapshot().viewer && (await posts()).length === 0, JSON.stringify([fv, h.snapshot().view]));
  // the stick, jump to top, grip drag
  const canvasRect = canvas().getBoundingClientRect();
  const off0 = h.wall.scroll.offset;
  for (let i = 0; i < 14; i += 1) { canvas().dispatchEvent(new WheelEvent("wheel", { deltaY: 100, bubbles: true, cancelable: true })); await sleep(100); }
  const off1 = h.wall.scroll.offset;
  await check("wall app: the stick (wheel in the preview) scrolls the wall on, the wall moves and the viewer position does not", off1 > off0 + 5, `${off0} -> ${off1}`);
  h.press("top");
  await frames(2);
  await check("wall app: the Top button jumps to the top", h.wall.scroll.offset === 0, String(h.wall.scroll.offset));
  h.wall.scroll.jump(300);
  await frames(2);
  move(canvasRect.left + canvasRect.width * 0.5, canvasRect.top + canvasRect.height * 0.4);
  await frames(3);
  keyDown("g"); await frames(3);
  const g0 = h.wall.scroll.offset;
  move(canvasRect.left + canvasRect.width * 0.5, canvasRect.top + canvasRect.height * 0.55); await frames(3);
  const g1 = h.wall.scroll.offset;
  keyUp("g"); await frames(2);
  const g2 = h.wall.scroll.offset;
  await sleep(600);
  const g3 = h.wall.scroll.offset;
  await check("wall app: grip + moving the ray drags the wall; on release it carries on a little with inertia", Math.abs(g1 - g0) > 1 && g3 !== g1 && Math.abs(g3 - g2) >= 0, `${g0} ${g1} ${g2} ${g3}`);
  await stop();

  // ---- recent: paged sources against the real route
  const recent = Array.from({ length: 250 }, (_, i) => mkFile(`r${String(i).padStart(3, "0")}.jpg`, "image", { added: 1.7e9 - i }));
  await configure({ ...base, recent });
  await clearLog();
  await start({ view: "wall", source: { kind: "recent" } });
  await wallReady(100);
  const first = h.wall.items.length;
  for (let k = 0; k < 40 && h.wall.items.length < 250; k += 1) { h.wall.scroll.jump(h.wall.scroll.max); await frames(2); await sleep(60); }
  const offsets = (await requests("/api/media/recent")).map((q) => +/offset=(\d+)/.exec(q.path)[1]);
  await check("wall app: Recent loads 100 at a time, the next page when the scroll nears the end, in order, no duplicates",
    first >= 100 && h.wall.items.length === 250 && offsets.join() === "0,100,200" && new Set(h.wall.items.map((i) => i.id)).size === 250 && h.snapshot().title === "Recent downloads", `${first} ${h.wall.items.length} ${offsets}`);
  await stop();

  // ---- settings panel
  const mem = memStorage();
  await configure(base);
  await start({}, { settingsStorage: mem });
  await wallReady(5);
  h.press("settings");
  await frames(2);
  const open1 = h.snapshot().settingsOpen;
  h.choose("span:150");
  await frames(2);
  h.choose("radius:3");
  h.choose("text:L");
  await frames(2);
  const saved = JSON.parse(mem.getItem("mediadl.vr.settings") || "null");
  await check("wall app: the settings panel opens on the dock button; span 150 / distance 3 m / text L re-lay the wall and are stored", open1 && h.wall.layout.spanDeg === 150 && h.wall.settings.radius === 3 && h.wall.settings.scale > 1.3
    && saved && saved.span === 150 && saved.radius === 3 && saved.text === "L", JSON.stringify([open1, h.wall.layout.spanDeg, saved]));
  h.choose("close");
  await frames(1);
  const closed = !h.snapshot().settingsOpen;
  await stop();
  await start({}, { settingsStorage: mem });
  await wallReady(5);
  await check("wall app: the close button closes it; the next start uses the stored settings", closed && h.settings.span === 150 && h.wall.layout.spanDeg === 150 && h.wall.settings.radius === 3, JSON.stringify(h.settings));
  await stop();
  await start({}, { settingsStorage: memStorage() });
  await wallReady(5);
  await check("wall app: the default span is 120 degrees", h.wall.layout.spanDeg === 120 && h.settings.span === 120, String(h.wall.layout.spanDeg));
  await stop();

  // ---- the 2000-item folder scrolled end to end (HUD stats logged)
  await configure(base);
  await clearLog();
  await start({ view: "wall", source: bigSrc });
  await waitFor(() => h.wall && h.wall.items.length === 2000 && h.snapshot().frames > 4, 15000);
  const gl = h.host.renderer.getContext();
  const agg = { meshes: 0, pool: 0, textures: 0, queued: 0, inflight: 0, steps: 0, maxUploadMs: 0, updateMax: 0, evicted: 0 };
  const maxOff2 = h.wall.scroll.max;
  const tScroll0 = performance.now();
  for (let off = 0; off <= maxOff2 + 30; off += 30) {
    h.wall.scroll.jump(Math.min(off, maxOff2));
    await frames(1);
    const w = h.wall.stats();
    agg.steps += 1;
    agg.meshes = Math.max(agg.meshes, w.meshes); agg.pool = Math.max(agg.pool, w.pool); agg.textures = Math.max(agg.textures, w.textures);
    agg.queued = Math.max(agg.queued, w.queued); agg.inflight = Math.max(agg.inflight, w.inflight);
    agg.maxUploadMs = Math.max(agg.maxUploadMs, w.maxUploadMs); agg.updateMax = Math.max(agg.updateMax, w.updateMax); agg.evicted = w.evicted;
  }
  await sleep(1500);
  const wEnd = h.wall.stats();
  const glErr = gl.getError();
  await note(`2000-item folder, ${agg.steps} scroll steps (${maxOff2.toFixed(0)} deg) in ${((performance.now() - tScroll0) / 1000).toFixed(1)} s: max meshes ${agg.meshes} (pool ${agg.pool}), max textures ${agg.textures}, max queued ${agg.queued}, max in-flight ${agg.inflight}, `
    + `upload max ${agg.maxUploadMs.toFixed(1)} ms, update max ${agg.updateMax.toFixed(1)} ms / p99 ${wEnd.updateP99.toFixed(2)} ms, evicted ${agg.evicted}, thumbnails fetched ${(await requests("/api/media/thumbnail")).length}`);
  await note("HUD at the end: " + h.snapshot().hudLines.join(" | "));
  await check("wall scroll end to end: 2000 tiles in the desktop preview - meshes stay bounded (virtualised), loader concurrency <= 6, offset reaches the end, no WebGL error",
    agg.meshes > 20 && agg.meshes < 260 && agg.pool < 300 && agg.inflight <= 6 && h.wall.scroll.offset >= maxOff2 - 0.01 && glErr === 0 && wEnd.textures > 0 && wEnd.textures <= 1500, JSON.stringify([agg, wEnd, glErr]));
  await check("wall scroll end to end: thumbnails of the last rows arrive (placeholders replaced) and the texture count stays within the cap", wEnd.uploads > 50 && wEnd.failed === 0, JSON.stringify(wEnd));
  r = await stop();
  await check("wall scroll end to end: exit afterwards leaves 0 textures and 0 geometries", r.textures === 0 && r.geometries === 0, JSON.stringify(r));

  // ---- 20 enter/exit cycles with the big folder: 0 textures every time
  const bad = [];
  for (let i = 0; i < 20; i += 1) {
    await start({ view: "wall", source: bigSrc }, { storage: memStorage() });
    await waitFor(() => h.wall && h.wall.items.length === 2000 && h.wall.stats().textures > 0, 12000);
    if (i % 2 === 1) { h.select(5 + i); await sleep(150); }
    if (i % 4 === 3) h.wall.scroll.jump(h.wall.scroll.max * 0.5);
    await frames(2);
    h.end();
    const rr = await Promise.race([h.ended, sleep(6000).then(() => ({ timeout: true }))]);
    if (!rr || rr.timeout || rr.textures !== 0 || rr.geometries !== 0 || document.getElementById("vrPreviewCanvas") || document.getElementById("vrPreviewRoot")) bad.push(`${i}:${JSON.stringify(rr)}`);
  }
  await check("wall cycles: 20 enter/exit cycles with the 2000-file folder (some with a viewer open, some scrolled) end at 0 textures, 0 geometries, no canvas", bad.length === 0, bad.join(" ; "));

  // ---- no console errors, vr.html page
  await sleep(300);
  await check("wall app: no console errors or unhandled rejections during all of the above", errs.length === 0, errs.slice(0, 5).join(" | "));
  console.error = ce;
  window.removeEventListener("error", onerr);
  window.removeEventListener("unhandledrejection", onrej);
}

// ---------------------------------------------------------------- V2a: search and filters in the VR app
async function vrSearchUnits() {
  const S = await import("/js/vr/search.js");
  const NV = await import("/js/vr/nav.js");
  const L = await import("/js/vr/logic.js");
  const o = S.normalizeOptions({ kinds: [{ key: "video", label: "Videos" }, { key: "image", label: "Images" }, { key: "music", label: "Music" }], contents: [{ key: "safe", label: "Safe" }, { key: "suggestive", label: "Suggestive" }] });
  await check("vr search: filter strings use media.js's FILTER_KEYS order and parse back", S.FILTER_KEYS.join() === "kind,orientation,min_res,min_hres,min_vres,min_fps,max_fps,content"
    && S.buildFilters({ min_res: "4k", kind: "video" }) === "kind=video&min_res=4k" && JSON.stringify(S.parseFilters("kind=video&min_res=4k&bogus=1")) === JSON.stringify({ kind: "video", min_res: "4k" }) && S.buildFilters(S.parseFilters("")) === "", "filters");
  await check("vr search: setFilterKey clears on the same value (single select); content is a comma list in the options' order",
    JSON.stringify(S.setFilterKey({ kind: "video" }, "kind", "video")) === "{}" && S.setFilterKey({ kind: "video" }, "kind", "image").kind === "image"
    && S.toggleContent({}, "suggestive", ["safe", "suggestive"]).content === "suggestive" && S.toggleContent({ content: "suggestive" }, "safe", ["safe", "suggestive"]).content === "safe,suggestive"
    && S.toggleContent({ content: "safe" }, "safe", ["safe", "suggestive"]).content === undefined, "toggles");
  await check("vr search: the panel offers videos and images (no music) from /api/media/filters, defaults without it", o.kinds.map((k) => k.key).join() === "video,image" && S.normalizeOptions(null).kinds.length === 2 && S.normalizeOptions({}).standards.length === 5, JSON.stringify(o.kinds));
  const sp = { kind: "search", query: "dog", filters: "kind=video&min_res=4k&content=safe", mode: "context", sort: { key: "", dir: "" } };
  const chips = S.chipsFor(sp, o);
  await check("vr search: chips = words, search type, every filter, then Clear all", chips.map((c) => c.label).join("|") === "\u201Cdog\u201D|In: Context|Videos|Size \u2265 4K|Safe|Clear all" && chips.map((c) => c.id).join() === "q,mode,f:kind,f:min_res,f:content,all", JSON.stringify(chips));
  await check("vr search: a wall without constraints has no chips; the Favourites wall has one", S.chipsFor({ kind: "folder", filters: "" }, o).length === 0 && S.chipsFor({ kind: "favorites", query: "", filters: "" }, o).map((c) => c.id).join() === "fav,all", "chips");
  let r = S.planEdit(sp, { type: "clearKey", key: "kind" });
  await check("vr search: clearing one filter replaces the search wall with the others kept", r.op === "replace" && r.spec.filters === "min_res=4k&content=safe" && r.spec.query === "dog", JSON.stringify(r));
  r = S.planEdit({ kind: "search", query: "dog", filters: "", mode: "filename" }, { type: "clearQuery" });
  await check("vr search: clearing the words of a search with nothing else goes back to where it was opened", r.op === "origin", JSON.stringify(r));
  r = S.planEdit({ kind: "search", query: "dog", filters: "kind=video", mode: "filename" }, { type: "clearQuery" });
  await check("vr search: clearing the words while filters remain becomes a filters-only search (q=*)", r.op === "replace" && r.spec.query === "*" && r.spec.filters === "kind=video", JSON.stringify(r));
  r = S.planEdit({ kind: "search", query: "dog", filters: "", mode: "filename" }, { type: "fav", on: true });
  await check("vr search: Favourites only on a search wall makes it a favourites search with the words kept", r.op === "replace" && r.spec.kind === "favorites" && r.spec.query === "dog", JSON.stringify(r));
  r = S.planEdit({ kind: "folder", root: "downloads", path: "sub", filters: "", sort: { key: "", dir: "" } }, { type: "filter", key: "kind", value: "image" });
  await check("vr search: a filter on a folder wall replaces it with the filtered listing; on the root listing it only mutates", r.op === "replace" && r.spec.filters === "kind=image" && r.spec.path === "sub"
    && S.planEdit({ kind: "folder", root: "", path: "", filters: "" }, { type: "filter", key: "kind", value: "image" }, { rootListing: true }).op === "mutate", JSON.stringify(r));
  await check("vr search: a filter on Recent is refused with a note; Favourites only from a folder pushes a favourites wall",
    S.planEdit({ kind: "recent", filters: "" }, { type: "filter", key: "kind", value: "image" }).op === "none" && S.planEdit({ kind: "recent", filters: "" }, { type: "filter", key: "kind", value: "image" }).note.length > 0
    && S.planEdit({ kind: "folder", root: "d", path: "", filters: "kind=image" }, { type: "fav", on: true }).op === "push", "none/push");
  const st3 = S.planEdit({ kind: "home", filters: "" }, S.filterEdit("stereo:1", S.DEFAULT_OPTIONS));
  const stRow = S.filterRows({ kind: "search", query: "x", filters: "stereo=1" }, S.DEFAULT_OPTIONS).find((r) => r.id === "stereo");
  const stOff = S.planEdit({ kind: "folder", root: "d", path: "a", filters: "stereo=1" }, S.filterEdit("stereo:", S.DEFAULT_OPTIONS));
  await check("vr search: 3D only is a filter row and a chip, reaches the server as stereo=1, from Home it opens a search of everything, All clears it",
    st3.op === "push" && st3.spec.kind === "search" && st3.spec.query === "*" && st3.spec.filters === "stereo=1"
    && stRow.options.map((o) => `${o.label}:${o.on}`).join() === "All:false,3D only:true"
    && S.chipsFor({ kind: "search", query: "*", filters: "stereo=1" }, S.DEFAULT_OPTIONS)[0].label === "3D only" && stOff.spec.filters === "", JSON.stringify([st3, stRow, stOff]));
  await check("vr search: Enter with nothing typed and no filter does nothing; with a filter it is a q=* search; on Favourites it searches them",
    S.searchSpec({ kind: "folder", filters: "" }, "  ", "filename") === null && S.searchSpec({ kind: "folder", filters: "kind=image" }, "", "filename").query === "*"
    && S.searchSpec({ kind: "favorites", filters: "" }, "cat", "ocr").kind === "favorites" && S.searchSpec({ kind: "favorites", filters: "" }, "cat", "ocr").mode === "ocr" && S.searchSpec({ kind: "folder", filters: "" }, " cat ", "filename").query === "cat", "searchSpec");
  const lay = S.keyboardLayout("abc", 1500, 800);
  const ids = lay.keys.map((k) => k.id);
  const inside = lay.keys.every((k) => k.x >= 0 && k.y >= 0 && k.x + k.w <= 1500.5 && k.y + k.h <= 800.5 && k.w > 40 && k.h > 40);
  const overlap = lay.keys.some((a, i) => lay.keys.some((b, j) => j > i && a.x < b.x + b.w - 0.5 && b.x < a.x + a.w - 0.5 && a.y < b.y + b.h - 0.5 && b.y < a.y + a.h - 0.5));
  await check("vr search: the keyboard has letters, digits, space, backspace, clear, enter, a symbols switch and close; all inside the panel and not overlapping",
    "abcdefghijklmnopqrstuvwxyz0123456789".split("").every((c) => ids.includes(`k:${c}`)) && ["space", "bksp", "clear", "enter", "layer", "close"].every((i) => ids.includes(i)) && inside && !overlap, JSON.stringify([ids.length, inside, overlap]));
  const sym = S.keyboardLayout("sym", 1500, 800).keys.map((k) => k.id);
  await check("vr search: the symbols layer has punctuation and the * wildcard", ["k:*", "k:-", "k:_", "k:.", "k:@", "k:\\", "k:\""].every((i) => sym.includes(i)), sym.join(" "));
  await check("vr search: typing keys, backspace removes one code point, space, clear, 120 character cap",
    S.typeKey("ab", "k:c") === "abc" && S.typeKey("a\u{1F600}", "bksp") === "a" && S.typeKey("a", "space") === "a " && S.typeKey("abc", "clear") === "" && S.typeKey("x".repeat(120), "k:y").length === 120
    && S.pasteText("a", "b\nc\td") === "ab c d" && S.pasteText("", "z".repeat(300)).length === 120, "typeKey");
  await check("vr search: keyboard events map to chars (also CJK), Backspace, Enter, Escape; Ctrl combinations are left alone",
    S.keyAction({ key: "a" }).op === "char" && S.keyAction({ key: "\u732B" }).ch === "\u732B" && S.keyAction({ key: "Backspace" }).op === "bksp" && S.keyAction({ key: "Enter" }).op === "enter"
    && S.keyAction({ key: "Escape" }).op === "close" && S.keyAction({ key: "v", ctrlKey: true }).op === null && S.keyAction({ key: "Shift" }).op === null && S.keyAction({ key: "F5" }).op === null, "keyAction");
  const rows = S.filterRows({ kind: "folder", filters: "kind=video", sort: { key: "", dir: "" } }, o, { mode: "filename", sortable: true });
  await check("vr search: the filter panel rows (type, favourites, shape, min size, content, search in, sort, order) with the active option marked",
    rows.map((x) => x.id).join() === "kind,fav,orientation,min_res,content,mode,sort,dir" && rows[0].options.find((x) => x.on).id === "video" && rows.find((x) => x.id === "sort").options.find((x) => x.on).id === "name"
    && S.filterRows({ kind: "search", filters: "" }, o, {}).every((x) => x.id !== "sort"), rows.map((x) => x.id).join());
  const fl = S.filterLayout(rows, 1600, 1100);
  await check("vr search: the filter panel layout fits its canvas and ends with Clear all / Close", fl.every((b) => b.x >= 0 && b.y >= 0 && b.x + b.w <= 1600.5 && b.y + b.h <= 1100.5) && fl[fl.length - 1].id === "close" && fl[fl.length - 2].id === "clearall:", "layout");
  await check("vr search: the dock's wall buttons include Search and Filters, the viewer's do not; a q=* search is titled",
    NV.dockButtons("wall").map((b) => b.id).includes("search") && NV.dockButtons("wall").map((b) => b.id).includes("filters") && !NV.dockButtons("viewer").map((b) => b.id).includes("search")
    && L.sourceTitle({ kind: "search", query: "*" }) === "Search: all files", "dock");
}

async function vrSearchApp() {
  const A = await import("/js/vr/app.js");
  const errs = [];
  const ce = console.error;
  console.error = (...a) => { errs.push(a.map(String).join(" ")); ce.apply(console, a); };
  const onerr = (e) => errs.push("error: " + (e && e.message));
  const onrej = (e) => errs.push("rejection: " + (e && e.reason && e.reason.message || e.reason));
  window.addEventListener("error", onerr);
  window.addEventListener("unhandledrejection", onrej);
  let h = null;
  const canvas = () => document.getElementById("vrPreviewCanvas");
  const move = (x, y) => canvas().dispatchEvent(new PointerEvent("pointermove", { clientX: x, clientY: y, bubbles: true }));
  const press = (button) => canvas().dispatchEvent(new PointerEvent("pointerdown", { button, bubbles: true }));
  const release = (button) => window.dispatchEvent(new PointerEvent("pointerup", { button, bubbles: true }));
  const keyDown = (k, extra) => window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, ...(extra || {}) }));
  const frames = async (n) => { const f0 = h.snapshot().frames; await waitFor(() => h.snapshot().frames >= f0 + n, 8000); };
  const start = async (ctx, opts) => { h = await A.startVr(ctx, Object.assign({ preview: true, storage: memStorage(), settingsStorage: memStorage(), autoPollMs: 150, mediaTimeoutMs: 8000 }, opts || {})); return h; };
  const stop = async () => { await h.end(); return Promise.race([h.ended, sleep(5000).then(() => ({ timeout: true }))]); };
  const clickPanel = async (name, id) => {
    const p = h.projectPanel(name, id);
    if (!p) return false;
    move(p.clientX, p.clientY); await frames(2);
    press(0); await frames(2); release(0); await frames(2);
    return true;
  };
  const searchReqs = async () => (await requests("/api/media/search")).map((q) => q.path);
  const waitSource = (pred, ms = 6000) => waitFor(() => { const s = h.snapshot().source; return s && pred(s) && !s.loading; }, ms);
  const names = () => h.wall.items.map((i) => i.name).join();
  const typeKeys = (text) => { for (const ch of text) h.searchKey(`k:${ch}`); };

  const F = (name, type, extra) => mkFile(name, type, extra);
  const ALL = [
    F("cat1.jpg", "image", { size: 5000, width: 1600, height: 1200 }),
    F("cat2.jpg", "image", { size: 9000, width: 1080, height: 1920 }),
    F("cat3.png", "image", { size: 7000, width: 4000, height: 3000, favorite: true }),
    F("dog.mp4", "video", { size: 3000, width: 1920, height: 1080 }),
    F("dog2.mp4", "video", { size: 8000, width: 3840, height: 2160, favorite: true }),
    F("dog_pic.jpg", "image", { size: 1000, width: 1000, height: 1000 }),
    F("bird.jpg", "image", { size: 2000, width: 1600, height: 1200, favorite: true }),
  ];
  const OPTIONS = { standards: [{ key: "720p", label: "720p", long_edge: 1280 }, { key: "fhd", label: "FHD", long_edge: 1920 }, { key: "wqhd", label: "WQHD", long_edge: 2560 }, { key: "4k", label: "4K", long_edge: 3840 }, { key: "8k", label: "8K", long_edge: 7680 }],
    kinds: [{ key: "video", label: "Videos" }, { key: "image", label: "Images" }, { key: "music", label: "Music" }], orientations: ["horizontal", "vertical", "square"], contents: [{ key: "safe", label: "Safe" }, { key: "suggestive", label: "Suggestive" }] };
  const base = { role: "admin", files: ALL, search_items: ALL, search_filter: true, filter_options: OPTIONS, with_big: true, tree: { sub: { files: [] } }, status: {}, list: [], recent: [], view_mode: "ok", multiroot: true };
  const wallReady = (n) => waitFor(() => h.wall && h.wall.items.length >= n && h.snapshot().frames > 3, 12000);

  // ---- from the roots view: the keyboard, the in-VR keys (and a real pointer click), Enter runs the search
  await configure(base);
  await clearLog();
  await start({});
  await wallReady(3);
  let s = h.snapshot();
  await check("search app: the roots view has the Search and Filters buttons in the dock", s.title === "Libraries" && s.buttons.includes("search") && s.buttons.includes("filters") && !s.search.open && !s.filters.open && s.chips.length === 0, JSON.stringify([s.buttons, s.search]));
  await waitFor(() => h.snapshot().frames > 6, 6000);
  await sleep(500);
  const tex0 = h.host.info().textures;
  h.press("search");
  await frames(3);
  s = h.snapshot();
  await check("search app: Search opens the keyboard panel (one more texture), empty field, abc layer", s.search.open && s.search.text === "" && s.search.layer === "abc" && h.host.info().textures === tex0 + 1, JSON.stringify([s.search, h.host.info().textures, tex0]));
  typeKeys("ca");
  await clickPanel("search", "k:t");
  s = h.snapshot();
  await check("search app: keys by the in-VR keyboard (two by call, one by a real pointer click on the panel) type into the field", s.search.text === "cat", s.search.text);
  h.searchKey("layer"); h.searchKey("k:_"); h.searchKey("layer"); h.searchKey("bksp");
  s = h.snapshot();
  await check("search app: the symbols layer switches and back, backspace removes the last character", s.search.text === "cat" && s.search.layer === "abc", JSON.stringify(s.search));
  await clearLog();
  h.searchKey("enter");
  await waitSource((x) => x.kind === "search" && x.count === 3);
  s = h.snapshot();
  const reqs1 = await searchReqs();
  await check("search app: Enter runs /api/media/search with q, mode=filename and paging exactly like the flat page; the keyboard closes",
    reqs1.length === 1 && /^\/api\/media\/search\?q=cat&mode=filename&offset=0&limit=100$/.test(reqs1[0]) && !s.search.open, JSON.stringify([reqs1, s.search.open]));
  await check("search app: the results are the three cats, breadcrumb Search: cat, stack 2 deep, a chip for the words", names() === "cat1.jpg,cat2.jpg,cat3.png" && s.title === "Search: cat" && s.depth === 2 && s.chips.join("|") === "\u201Ccat\u201D|Clear all", JSON.stringify([names(), s.title, s.depth, s.chips]));
  h.back();
  await waitFor(() => h.snapshot().depth === 1, 4000);
  await frames(2);
  s = h.snapshot();
  await check("search app: Back returns to the roots wall (3 tiles, Favourites first), the chip strip is gone", s.title === "Libraries" && h.wall.items.length === 3 && h.wall.items[0].special === "favorites" && s.chips.length === 0, JSON.stringify([s.title, s.chips]));

  // ---- a desktop keyboard while the panel is open: host keys are swallowed, Escape closes the panel only, paste, CJK
  await sleep(500);
  const texOpen = h.host.info().textures;
  h.press("search");
  await frames(2);
  for (const k of ["d", "o", "g", "b", "r", "g"]) keyDown(k);
  keyDown("Backspace"); keyDown("Backspace"); keyDown("Backspace");
  keyDown("\u732B");
  await frames(2);
  s = h.snapshot();
  await check("search app: keydown types while the panel is open (host keys b/r/g do not exit/recenter/grip; Backspace; a CJK character)", s.search.text === "dog\u732B" && !h.host.cleaned && s.recenters.yaw === 0 && (() => { const b = h.host.hands()[0].buttons; return !b[1] && !b[4] && !b[5]; })(), JSON.stringify([s.search.text, s.recenters, h.host.hands()[0].buttons]));
  keyDown("Backspace");
  const dt = new DataTransfer();
  dt.setData("text", "h\u00E9llo\nw\u00F6rld");
  window.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  await frames(1);
  const pasted = h.snapshot().search.text;
  await check("search app: pasted text (non-ASCII, line break flattened) is appended", pasted === "dogh\u00E9llo w\u00F6rld", pasted);
  keyDown("Escape");
  await frames(2);
  s = h.snapshot();
  await check("search app: Escape closes the keyboard and leaves the session running (texture released)", !s.search.open && !h.host.cleaned && h.host.info().textures === texOpen, JSON.stringify([s.search.open, h.host.info().textures, texOpen]));
  h.press("search");
  await frames(2);
  await check("search app: reopening from a wall that is not a search starts with an empty field", h.snapshot().search.open && h.snapshot().search.text === "", h.snapshot().search.text);
  h.setSearchText("d\u00F6g w\u00F8rld");
  await clearLog();
  keyDown("Enter");
  await waitSource((x) => x.kind === "search");
  const reqsU = await searchReqs();
  await check("search app: a typed/pasted non-ASCII query is sent percent-encoded as q", reqsU.length === 1 && reqsU[0].startsWith(`/api/media/search?q=${encodeURIComponent("d\u00F6g w\u00F8rld")}&mode=filename`), JSON.stringify(reqsU));
  h.back();
  await waitFor(() => h.snapshot().depth === 1, 4000);
  h.press("search");
  await frames(1);
  await clearLog();
  h.searchKey("enter");
  await frames(2);
  s = h.snapshot();
  await check("search app: Enter with nothing typed and no filter sends no request and says so", (await searchReqs()).length === 0 && s.search.open && /Type something/.test(s.message) && s.depth === 1, JSON.stringify([s.message, s.depth, s.search, await searchReqs()]));
  h.searchKey("close");
  await stop();

  // ---- from a folder, scrolled: Back returns to that scroll; Prev/Next walk the results
  await configure({ ...base, multiroot: false, search_items: ALL });
  const bigSrc = { kind: "folder", root: "downloads", path: "big", query: "", filters: "", sort: { key: "name", dir: "asc" }, offset: 0 };
  await start({ view: "wall", source: bigSrc });
  await waitFor(() => h.wall && h.wall.items.length === 2000 && h.snapshot().frames > 4, 15000);
  h.wall.scroll.jump(h.wall.scroll.max * 0.4);
  await frames(3);
  const fv = h.wall.firstVisible();
  h.press("search");
  await frames(1);
  typeKeys("cat");
  h.searchKey("enter");
  await waitSource((x) => x.kind === "search" && x.count === 3);
  await check("search app: opened from the big folder, the results replace the wall (3 files), Search: cat", names() === "cat1.jpg,cat2.jpg,cat3.png" && h.snapshot().depth === 2, names());
  h.select(0);
  await waitFor(() => h.snapshot().viewer && h.snapshot().viewer.path === "cat1.jpg", 4000);
  await h.next();
  await waitFor(() => h.snapshot().viewer.path === "cat2.jpg", 4000);
  await h.next();
  await waitFor(() => h.snapshot().viewer.path === "cat3.png", 4000);
  await h.next();
  await sleep(150);
  const atEnd = h.snapshot().viewer.path;
  await h.prev();
  await waitFor(() => h.snapshot().viewer.path === "cat2.jpg", 4000);
  await check("search app: the viewer's Next/Prev walk the search results in order and stop at the ends", atEnd === "cat3.png" && h.snapshot().viewer.path === "cat2.jpg", atEnd);
  h.back();
  await waitFor(() => !h.snapshot().viewer, 4000);
  h.back();
  await waitFor(() => h.snapshot().depth === 1 && h.wall.items.length === 2000, 6000);
  await frames(3);
  const fv2 = h.wall.firstVisible();
  await check("search app: Back from the results returns to the 2000-file folder at the scroll position the search was opened at", Math.abs(fv2 - fv) <= 2 && fv > 100, `${fv} -> ${fv2}`);
  await stop();

  // ---- filters on a search wall: each changes the request and the results; chips clear one by one and all together
  await configure(base);
  await clearLog();
  await start({});
  await wallReady(3);
  h.press("search");
  await frames(1);
  typeKeys("dog");
  h.searchKey("enter");
  await waitSource((x) => x.kind === "search" && x.count === 3);
  await check("search app: the dog search lists 3 files", names() === "dog.mp4,dog2.mp4,dog_pic.jpg", names());
  await clearLog();
  h.press("filters");
  await frames(2);
  await waitFor(() => h.snapshot().filters.options.contents.length === 2, 4000);
  const optReq = (await requests("/api/media/filters")).length;
  await check("search app: Filters opens the panel and asks /api/media/filters once; its options (no music) come from the server", h.snapshot().filters.open && optReq === 1 && h.snapshot().filters.options.kinds.map((k) => k.key).join() === "video,image", JSON.stringify([optReq, h.snapshot().filters.options.kinds]));
  await clearLog();
  await clickPanel("filters", "kind:video");
  await waitSource((x) => x.kind === "search" && x.count === 2);
  let rq = await searchReqs();
  s = h.snapshot();
  await check("search app: Type = Videos re-runs the search with kind=video; results and chips follow; the title stays", /^\/api\/media\/search\?q=dog&mode=filename&kind=video&offset=0&limit=100$/.test(rq[rq.length - 1]) && names() === "dog.mp4,dog2.mp4"
    && s.chips.join("|") === "\u201Cdog\u201D|Videos|Clear all" && s.title === "Search: dog" && s.depth === 2 && s.filters.open, JSON.stringify([rq, names(), s.chips]));
  await clearLog();
  h.filter("min_res:4k");
  await waitSource((x) => x.kind === "search" && x.count === 1);
  rq = await searchReqs();
  await check("search app: Min size = 4K adds min_res=4k (order kind, min_res as media.js); one result left", /&kind=video&min_res=4k&offset=0&limit=100$/.test(rq[rq.length - 1]) && names() === "dog2.mp4" && h.snapshot().depth === 2, JSON.stringify([rq, names()]));
  await clearLog();
  await clickPanel("chips", "f:kind");
  await waitSource((x) => x.kind === "search" && x.count === 1);
  rq = await searchReqs();
  s = h.snapshot();
  await check("search app: tapping the Videos chip clears just that filter (request keeps min_res=4k only); the chip is gone", /&min_res=4k&offset=0&limit=100$/.test(rq[rq.length - 1]) && !/kind=/.test(rq[rq.length - 1]) && s.chips.join("|") === "\u201Cdog\u201D|Size \u2265 4K|Clear all", JSON.stringify([rq, s.chips]));
  await clearLog();
  h.filter("orientation:vertical");
  await waitSource((x) => x.kind === "search" && x.count === 0);
  rq = await searchReqs();
  await check("search app: Shape = Vertical adds orientation=vertical before min_res; no dog is vertical and 4K - an empty wall, Empty in the dock", /&orientation=vertical&min_res=4k&offset=0&limit=100$/.test(rq[rq.length - 1]) && h.wall.items.length === 0 && /Empty/.test(h.snapshot().subtitle), JSON.stringify([rq, h.snapshot().subtitle]));
  h.filter("orientation:vertical");              // the same option again clears it (media.js setFilter)
  await waitSource((x) => x.kind === "search" && x.count === 1);
  h.filter("fav:1");
  await waitSource((x) => x.kind === "favorites" && x.count === 1);
  rq = await searchReqs();
  s = h.snapshot();
  await check("search app: Favourites only turns the wall into a favourites search with the words (favorites=1 and narrow=1 like the flat page) and a Favourites chip",
    /^\/api\/media\/search\?favorites=1&q=dog&mode=filename&narrow=1&min_res=4k&offset=0&limit=100$/.test(rq[rq.length - 1]) && names() === "dog2.mp4" && s.chips.join("|") === "\u201Cdog\u201D|Favourites|Size \u2265 4K|Clear all" && s.depth === 2, JSON.stringify([rq[rq.length - 1], names(), s.chips]));
  h.filter("mode:context");
  await waitSource((x) => x.kind === "favorites");
  rq = await searchReqs();
  await check("search app: Search in = Context sends mode=context and shows an In: Context chip", /mode=context/.test(rq[rq.length - 1]) && h.snapshot().chips.includes("In: Context"), JSON.stringify([rq[rq.length - 1], h.snapshot().chips]));
  await clearLog();
  await clickPanel("chips", "all");
  await waitFor(() => h.snapshot().depth === 1, 5000);
  h.filter("mode:filename");
  await frames(2);
  s = h.snapshot();
  await check("search app: Clear all drops the words and every filter, so the wall goes back to where the search was opened (roots), chips gone", s.title === "Libraries" && s.chips.length === 0 && (await searchReqs()).length === 0, JSON.stringify([s.title, s.chips]));
  await check("search app: the filter panel stays open over the restored wall and the stack never grew beyond the roots", s.filters.open && s.depth === 1, JSON.stringify(s.filters.open));
  h.filter("close");
  await frames(1);

  // ---- a filter set on the roots view is inherited by the folders opened from it; filters-only search is q=*
  h.press("filters");
  await frames(1);
  await clearLog();
  h.filter("kind:image");
  await frames(2);
  s = h.snapshot();
  const noReload = (await requests("/api/media/")).filter((q) => /search|browse/.test(q.path)).length;
  await check("search app: a filter on the roots view only marks the wall (chip, no request, same 3 tiles)", s.chips.join("|") === "Images|Clear all" && noReload === 0 && h.wall.items.length === 3, JSON.stringify([s.chips, h.wall.items.length, noReload]));
  h.filter("close");
  h.select(1);                                    // the Downloads library
  await waitSource((x) => x.kind === "folder" && x.root === "downloads");
  const br = (await requests("/api/media/browse")).map((q) => q.path);
  await check("search app: the folder opened afterwards is requested with kind=image and lists only pictures", /kind=image/.test(br[br.length - 1]) && h.wall.items.filter((i) => i.type !== "folder").every((i) => i.type === "picture") && h.wall.items.some((i) => i.type === "picture") && h.snapshot().chips.join("|") === "Images|Clear all", JSON.stringify([br, names()]));
  h.press("filters");
  await frames(2);
  h.filter("sort:size");
  await waitSource((x) => x.kind === "folder");
  const sizes = h.wall.items.filter((i) => i.type !== "folder").map((i) => i.size);
  await check("search app: Sort = Size in a folder wall sorts the files by size, largest first (the flat page's descending default)", sizes.length >= 4 && sizes.every((v, i) => i === 0 || sizes[i - 1] >= v) && h.snapshot().depth === 2, sizes.join());
  h.filter("dir:asc");
  await waitSource((x) => x.kind === "folder");
  const sizes2 = h.wall.items.filter((i) => i.type !== "folder").map((i) => i.size);
  await check("search app: Order = Ascending flips it", sizes2.every((v, i) => i === 0 || sizes2[i - 1] <= v), sizes2.join());
  h.filter("close");
  h.choose("close");
  await clearLog();
  h.press("search");
  await frames(1);
  h.searchKey("enter");
  await waitSource((x) => x.kind === "search" && x.count > 0);
  rq = await searchReqs();
  s = h.snapshot();
  await check("search app: Enter with an empty field while a filter is set is a filters-only search: q=* with that filter, titled Search: all files",
    rq.length === 1 && /^\/api\/media\/search\?q=\*&mode=filename&kind=image&offset=0&limit=100$/.test(rq[0]) && s.title === "Search: all files" && h.wall.items.every((i) => i.type === "picture") && h.wall.items.length === 5 && s.depth === 3, JSON.stringify([rq, s.title, h.wall.items.length, s.depth]));
  await check("search app: for q=* there is no words chip, only the filter chip", s.chips.join("|") === "Images|Clear all", JSON.stringify(s.chips));
  h.back();
  h.back();
  await waitFor(() => h.snapshot().depth === 1, 4000);

  // ---- the Favourites tile wall is a favourites wall: its chip clears back to the roots
  await clearLog();
  h.select(0);
  await waitSource((x) => x.kind === "favorites");
  s = h.snapshot();
  await check("search app: the Favourites tile wall shows the Favourites chip (and the roots' Images filter is inherited)", s.chips.join("|") === "Favourites|Images|Clear all" && s.title === "Libraries / Favourites", JSON.stringify([s.chips, s.title]));
  await clickPanel("chips", "fav");
  await waitSource((x) => x.kind === "search");
  await check("search app: clearing the Favourites chip while a filter remains turns the wall into the filters-only search (Images, titled Search: all files)", h.snapshot().title === "Search: all files" && h.snapshot().chips.join("|") === "Images|Clear all" && h.snapshot().depth === 2, JSON.stringify([h.snapshot().title, h.snapshot().chips, h.snapshot().depth]));
  await clickPanel("chips", "all");
  await waitFor(() => h.snapshot().depth === 1, 4000);
  await check("search app: Clear all on that wall leaves it: back to the roots", h.snapshot().title === "Libraries" && h.snapshot().depth === 1, h.snapshot().title);

  // ---- panel open/close cycles: textures and panels
  await frames(2);
  const texBase = h.host.info().textures;
  for (let i = 0; i < 20; i += 1) {
    h.press("search"); await frames(1);
    typeKeys("x");
    h.press("filters"); await frames(1);                     // switching closes the keyboard
    if (i % 2) h.back(); else h.filter("close");             // Back closes an open panel first
    await frames(1);
  }
  s = h.snapshot();
  await check("search app: 20 keyboard/filter open-close cycles leave the texture count where it was and no panel open", h.host.info().textures === texBase && !s.search.open && !s.filters.open && s.depth === 1, JSON.stringify([texBase, h.host.info().textures, s.search.open, s.filters.open, s.depth]));
  h.press("search");
  await frames(2);
  h.press("filters");
  await frames(2);
  await check("search app: opening Filters closes the keyboard (one panel at a time)", !h.snapshot().search.open && h.snapshot().filters.open, JSON.stringify([h.snapshot().search.open, h.snapshot().filters.open]));
  h.press("settings");
  await frames(2);
  await check("search app: opening Settings closes the filter panel", !h.snapshot().filters.open && h.snapshot().settingsOpen, "settings");
  h.choose("close");
  h.press("search");
  await frames(2);
  const rEnd = await stop();
  await check("search app: leaving VR with the keyboard open disposes it: 0 textures, 0 geometries", rEnd.textures === 0 && rEnd.geometries === 0 && !rEnd.error, JSON.stringify(rEnd));

  // ---- 20 sessions that end with panels and chips alive
  const bad = [];
  for (let i = 0; i < 20; i += 1) {
    await start({}, { storage: memStorage() });
    await wallReady(3);
    h.press("filters"); await frames(1);
    h.filter("kind:video"); await frames(1);
    if (i % 2) { h.press("search"); await frames(1); }
    h.end();
    const rr = await Promise.race([h.ended, sleep(6000).then(() => ({ timeout: true }))]);
    if (!rr || rr.timeout || rr.textures !== 0 || rr.geometries !== 0 || document.getElementById("vrPreviewCanvas")) bad.push(`${i}:${JSON.stringify(rr)}`);
  }
  await check("search app: 20 sessions ended with the filter panel, chip strip and keyboard alive all end at 0 textures, 0 geometries", bad.length === 0, bad.join(" ; "));

  await sleep(300);
  await check("search app: no console errors or unhandled rejections during all of the above", errs.length === 0, errs.slice(0, 5).join(" | "));
  console.error = ce;
  window.removeEventListener("error", onerr);
  window.removeEventListener("unhandledrejection", onrej);
  await configure({ ...base, search_filter: false });
}

// V2b: the HUD's video lines (nominal fps, rendered/dropped frames, measured fps, resolution).
async function vrVideoHud() {
  const A = await import("/js/vr/app.js");
  const L = await import("/js/vr/logic.js");
  const info = await stub("/__stub/info");
  const note = (text) => stub("/__stub/note", { text });
  const errs = [];
  const ce = console.error;
  console.error = (...a) => { errs.push(a.map(String).join(" ")); ce.apply(console, a); };
  const onerr = (e) => errs.push("error: " + (e && e.message));
  const onrej = (e) => errs.push("rejection: " + (e && e.reason && e.reason.message || e.reason));
  window.addEventListener("error", onerr);
  window.addEventListener("unhandledrejection", onrej);

  // ---- the pure parts with a stubbed video element
  const fake = (opts = {}) => {
    const v = { videoWidth: 1920, videoHeight: 1080, cbs: new Map(), next: 1, cancelled: [], q: { totalVideoFrames: 0, droppedVideoFrames: 0 } };
    if (opts.rvfc !== false) {
      v.requestVideoFrameCallback = (cb) => { const id = v.next++; v.cbs.set(id, cb); return id; };
      v.cancelVideoFrameCallback = (id) => { v.cancelled.push(id); v.cbs.delete(id); };
    }
    if (opts.quality !== false) v.getVideoPlaybackQuality = () => ({ ...v.q });
    v.fire = (now, presented) => { const cbs = Array.from(v.cbs.entries()); v.cbs.clear(); for (const [, cb] of cbs) cb(now, { presentedFrames: presented }); };
    return v;
  };
  {
    const v = fake();
    const st = new L.VideoStats(v, { fps: 60 });
    let t = 1000;
    for (let i = 1; i <= 90; i += 1) { t += 1000 / 60; v.fire(t, i); if (i % 15 === 0) { v.q.totalVideoFrames = i; v.q.droppedVideoFrames = Math.floor(i / 30) * 3; st.sample(t); } }
    const r = st.read(t);
    await check("video hud: the measured fps is the presented-frame rate of the last second (stubbed requestVideoFrameCallback, 60 fps)", Math.abs(r.measuredFps - 60) < 0.5, JSON.stringify(r));
    await check("video hud: rendered = totalVideoFrames - droppedVideoFrames (getVideoPlaybackQuality), dropped = droppedVideoFrames", r.rendered === 90 - 9 && r.dropped === 9 && r.renderedFrom === "quality", JSON.stringify(r));
    await check("video hud: dropped per second is the dropped delta over the ~1 s sample window (two drops of 3 within the last second = 6/s)", Math.abs(r.droppedPerS - 6) < 0.8, String(r.droppedPerS));
    const lines = L.videoLines(r);
    await check("video hud: the two lines name the resolution, nominal fps, measured fps and both frame counts", lines.length === 3 && /1920x1080/.test(lines[0]) && /nominal 60 fps/.test(lines[0]) && /measured 60\.0 fps/.test(lines[0])
      && /rendered 81 \(60\.0\/s\)/.test(lines[1]) && /dropped 9 \(\d\.\d\/s\)/.test(lines[1]) && !/NaN|undefined|null/.test(lines.join("\n")), lines.join(" | "));
    await check("video hud: a stalled video shows 0.0 fps, not the old rate (callbacks older than 1 s)", st.read(t + 2500).measuredFps === 0, String(st.read(t + 2500).measuredFps));
    const handles = v.cbs.size;
    st.dispose();
    await check("video hud: dispose cancels the pending frame callback and nothing re-arms it", handles === 1 && v.cbs.size === 0 && v.cancelled.length === 1, JSON.stringify([handles, v.cbs.size, v.cancelled]));
    v.fire(t + 20, 91);
    await check("video hud: a callback that still arrives after dispose neither counts nor re-registers", v.cbs.size === 0, String(v.cbs.size));
  }
  {
    const v = fake({ quality: false });
    const st = new L.VideoStats(v, { fps: 120, sourceFps: 30, interpolated: true });
    for (let i = 1; i <= 50; i += 1) v.fire(i * 8.333, 700 + i);
    const r = st.read(50 * 8.333);
    const lines = L.videoLines(r);
    await check("video hud: without getVideoPlaybackQuality the rendered count is rVFC presentedFrames and dropped is n/a", r.rendered === 750 && r.renderedFrom === "presented" && r.dropped === null && /rendered 750 /.test(lines[1]) && /dropped n\/a/.test(lines[1]), lines.join(" | "));
    await check("video hud: an interpolated version says so (120 fps from 30)", /nominal 120 fps \(interpolated from 30\)/.test(lines[0]), lines[0]);
    st.dispose();
  }
  {
    const v = fake({ rvfc: false });
    const st = new L.VideoStats(v, {});
    for (let i = 0; i <= 4; i += 1) { v.q.totalVideoFrames = 6 * i; v.q.droppedVideoFrames = i; st.sample(1000 + i * 250); }
    const r = st.read(2000);
    await check("video hud: without rVFC the fps comes from the rendered-count delta of getVideoPlaybackQuality (5 per 250 ms = 20 fps), dropped 4/s", Math.abs(r.measuredFps - 20) < 0.5 && r.rendered === 20 && Math.abs(r.droppedPerS - 4) < 0.2 && /unknown/.test(L.videoLines(r)[0]), JSON.stringify(r));
    st.dispose();
  }
  await check("video hud: no video, no lines; the HUD keeps its 5 lines", L.videoLines(null).length === 0 && L.hudLines(null, {}).length === 5 && L.hudLines(null, { video: null }).length === 5, "");
  const di = L.itemDisplay({ root: "downloads", path: "a.mp4", kind: "video", status: { state: "done", key: "k".repeat(32), sbs_url: "/x", kind: "video", eye_w: 10, eye_h: 10, fps: 119.88, source_fps: 29.97, interpolated: true } });
  const d2 = L.itemDisplay({ root: "downloads", path: "a.mp4", kind: "video", fps: 25 });
  await check("video hud: the item display carries the server's fps / source_fps / interpolated for 3D and the library's fps for 2D", di.fps === 119.88 && di.sourceFps === 29.97 && di.interpolated === true && d2.fps === 25 && d2.mode === "mono", JSON.stringify([di, d2]));

  if (!info.has_video) {
    await note("video hud app checks skipped: the stub server could not make a test video (no ffmpeg)");
  } else {
    // count the frame callbacks still pending on real elements
    const proto = HTMLVideoElement.prototype;
    const origReq = proto.requestVideoFrameCallback, origCancel = proto.cancelVideoFrameCallback;
    const pending = new Set();
    let maxPending = 0;
    let serial = 0;
    proto.requestVideoFrameCallback = function (cb) {
      if (!/videoStatsFrame/.test(cb.name)) return origReq.call(this, cb);       // only the HUD's own callbacks (three.js registers its own)
      if (!this.__k) this.__k = "v" + (serial += 1);
      const el = this;
      let id = 0;
      id = origReq.call(this, (now, meta) => { pending.delete(el.__k + ":" + id); cb(now, meta); });
      pending.add(this.__k + ":" + id);
      maxPending = Math.max(maxPending, pending.size);
      return id;
    };
    proto.cancelVideoFrameCallback = function (id) { if (this.__k) pending.delete(this.__k + ":" + id); return origCancel.call(this, id); };
    const PK = "p".repeat(32);
    const sbs = (extra) => ({ state: "done", key: PK, kind: "video", sbs_url: `/api/media/3d/file?key=${PK}&name=sbs.mp4`, eye_w: 64, eye_h: 64, ...extra });
    const src = { kind: "folder", root: "downloads", path: "", sort: {} };
    const file = { root: "downloads", path: "play.mp4", name: "play.mp4", kind: "video" };
    const files = [{ name: "play.mp4", type: "video", size: 1000, mtime: 1.7e9, added: 1.7e9, root: "downloads", path: "play.mp4", url: "/api/media/file?root=downloads&path=play.mp4", favorite: false, width: 128, height: 64, duration: 20 }];
    const cfg = (status) => configure({ role: "admin", files, status: status ? { "play.mp4": status } : {}, view_mode: "ok", with_big: false, tree: {}, list: [], recent: [], multiroot: false });
    const ropts = { preview: true, storage: null, settingsStorage: null, autoPollMs: 150, mediaTimeoutMs: 8000 };
    const start = (item) => A.startVr({ view: "item", item, source: src }, { ...ropts, storage: memStorage(), settingsStorage: memStorage() });
    const open = async (item, status) => {
      await cfg(status);
      const h = await start(item);
      await waitFor(() => h.screen && h.screen.mounted && h.screen.video && h.screen.video.readyState >= 2, 12000);
      await waitFor(() => h.snapshot().hudLines.some((l) => /^Frames rendered [1-9]/.test(l)) && h.screen.video.currentTime > 0.3, 8000);
      await sleep(1300);
      return h;
    };
    const vl = (h) => h.snapshot().hudLines.filter((l) => /^(Video|Frames) /.test(l));
    const S1 = sbs({ fps: 120, source_fps: 30, interpolated: true });
    let h = await open({ ...file, status: S1 }, S1);
    let l = vl(h);
    let m = /^Frames rendered (\d+) \(([\d.]+)\/s\)   dropped (\d+) \(([\d.]+)\/s\)$/.exec(l[1] || "");
    await check("video hud app: a 3D interpolated video shows the resolution and 'nominal 120 fps (interpolated from 30)'", l.length === 2 && /^Video 128x64   nominal 120 fps \(interpolated from 30\)   measured [\d.]+ fps$/.test(l[0]), l.join(" | "));
    await check("video hud app: the measured fps is the real presented rate (> 0) and equals the per-second figure on the frames line", m && Number(m[2]) > 1 && Math.abs(Number(m[2]) - Number(/measured ([\d.]+) fps/.exec(l[0])[1])) < 0.11, l.join(" | "));
    const q0 = h.screen.video.getVideoPlaybackQuality();
    await check("video hud app: the rendered count is the browser's totalVideoFrames - droppedVideoFrames, dropped its droppedVideoFrames", m && Number(m[1]) > 0 && Number(m[1]) <= q0.totalVideoFrames - q0.droppedVideoFrames && Number(m[3]) <= q0.droppedVideoFrames, JSON.stringify([m, q0.totalVideoFrames, q0.droppedVideoFrames]));
    const r1 = Number(m[1]);
    await sleep(700);
    m = /rendered (\d+)/.exec(vl(h)[1] || "");
    await check("video hud app: the running rendered total keeps counting while the video plays", m && Number(m[1]) > r1, `${r1} -> ${m && m[1]}`);
    await h.end(); await h.ended;
    const S2 = sbs({ fps: 30 });
    h = await open({ ...file, status: S2 }, S2);
    l = vl(h);
    await check("video hud app: a 3D video at its native rate shows 'nominal 30 fps' without the interpolated note", /nominal 30 fps   measured/.test(l[0] || "") && !/interpolated/.test(l.join("")), l.join(" | "));
    await h.end(); await h.ended;
    h = await open({ ...file, fps: 25 }, null);
    l = vl(h);
    await check("video hud app: a 2D file shows the library's fps (25)", h.snapshot().screen.mode === "mono" && /nominal 25 fps/.test(l[0] || ""), l.join(" | "));
    await h.end(); await h.ended;
    h = await open({ ...file }, null);
    l = vl(h);
    await check("video hud app: a 2D file with no known fps says 'nominal unknown' (and still measures)", /nominal unknown   measured [1-9]/.test(l[0] || ""), l.join(" | "));
    const hudNum = h.snapshot().hudLines.length;
    await h.end(); await h.ended;
    await check("video hud app: the frame callback is unregistered after the viewer closes (none pending)", pending.size === 0, String(pending.size));
    // a picture: no video line
    const K = "1".repeat(32);
    const PIC = { state: "done", key: K, sbs_url: `/api/media/3d/file?key=${K}&name=sbs.jpg`, kind: "picture", eye_w: 1080, eye_h: 1440 };
    await configure({ role: "admin", files: FILES, status: { "e.png": PIC } });
    h = await start({ root: "downloads", path: "e.png", name: "e.png", kind: "picture", status: PIC });
    await waitFor(() => h.screen && h.screen.mounted && h.snapshot().frames > 4, 8000);
    await check("video hud app: with a picture (no video) the HUD has no video line", !h.snapshot().hudLines.some((x) => /^(Video|Frames rendered)/.test(x)) && h.snapshot().hudLines.length === hudNum - 3, h.snapshot().hudLines.join(" | "));
    await h.end(); await h.ended;
    // 20 open/close cycles, some closed before the video is ready
    const bad = [];
    await cfg(S2);
    maxPending = 0;
    for (let i = 0; i < 20; i += 1) {
      const hh = await start({ ...file, status: S2 });
      if (i % 3 !== 2) await waitFor(() => hh.screen && hh.screen.mounted && hh.screen.video && hh.screen.video.readyState >= 2, 8000);
      if (i % 4 === 0) await sleep(200);
      await hh.end();
      const r = await Promise.race([hh.ended, sleep(5000).then(() => ({ timeout: true }))]);
      if (!r || r.timeout || pending.size !== 0) bad.push(`${i}:${pending.size}:${JSON.stringify(r)}`);
    }
    await check("video hud app: 20 open/close cycles leave no frame callback registered (pending 0 after each, at most 2 at any time)", bad.length === 0 && maxPending <= 2, `${bad.join(" ; ")} max=${maxPending}`);
    proto.requestVideoFrameCallback = origReq;
    proto.cancelVideoFrameCallback = origCancel;
  }
  await sleep(300);
  await check("video hud: no console errors or unhandled rejections during all of the above", errs.length === 0, errs.slice(0, 5).join(" | "));
  console.error = ce;
  window.removeEventListener("error", onerr);
  window.removeEventListener("unhandledrejection", onrej);
}

// V2c: decoder capabilities in the HUD (per-item mediaCapabilities line, per-session decoder table), playback speed,
// highest-refresh-rate request, texture-update counter and the "video every XR frame" toggle.
async function vrDecoderHud() {
  const A = await import("/js/vr/app.js");
  const L = await import("/js/vr/logic.js");
  const ST = await import("/js/vr/settings.js");
  const info = await stub("/__stub/info");
  const note = (text) => stub("/__stub/note", { text });
  const errs = [];
  const ce = console.error;
  console.error = (...a) => { errs.push(a.map(String).join(" ")); ce.apply(console, a); };
  const onerr = (e) => errs.push("error: " + (e && e.message));
  const onrej = (e) => errs.push("rejection: " + (e && e.reason && e.reason.message || e.reason));
  window.addEventListener("error", onerr);
  window.addEventListener("unhandledrejection", onrej);

  const own = Object.getOwnPropertyDescriptor(navigator, "mediaCapabilities");
  const setMc = (mc) => Object.defineProperty(navigator, "mediaCapabilities", { value: mc, configurable: true });
  const restoreMc = () => { if (own) Object.defineProperty(navigator, "mediaCapabilities", own); else delete navigator.mediaCapabilities; };
  const stubMc = (variant) => {
    const calls = [];
    return {
      calls,
      decodingInfo(cfg) {
        calls.push(cfg);
        const ct = cfg.video.contentType;
        if (variant === "reject") return Promise.reject(new Error("boom"));
        if (variant === "none") return Promise.resolve({ supported: false, smooth: false, powerEfficient: false });
        if (variant === "noav1" && /av01/.test(ct)) return Promise.resolve({ supported: false, smooth: false, powerEfficient: false });
        if (variant === "sw") return Promise.resolve({ supported: true, smooth: cfg.video.framerate <= 60, powerEfficient: false });
        return Promise.resolve({ supported: true, smooth: true, powerEfficient: true });
      },
    };
  };
  const play = (t) => (/hvc1|avc1/.test(t) ? "probably" : (/vp09/.test(t) ? "maybe" : ""));

  // ---- the decoder table (pure)
  {
    const mc = stubMc("noav1");
    const rep = new L.DecoderReport({ mediaCapabilities: mc }, play);
    const first = rep.lines();
    await check("decoder hud: before the answers land every cell shows '...', canPlayType is read synchronously", first.length === 5 && /^AV1 +1080p60 \.\.\./.test(first[1]) && /play probably/.test(first[2]) && /play maybe/.test(first[3]), first.join(" | "));
    await sleep(50);
    const ls = rep.lines();
    await check("decoder hud: 16 mediaCapabilities queries (4 codecs x 4 modes), type file, codec strings and sizes as specified", mc.calls.length === 16 && mc.calls.every((c) => c.type === "file")
      && mc.calls.some((c) => c.video.contentType === 'video/mp4; codecs="av01.0.08M.08"') && mc.calls.some((c) => c.video.contentType === 'video/mp4; codecs="hvc1.1.6.L153.B0"')
      && mc.calls.some((c) => c.video.contentType === 'video/webm; codecs="vp09.00.41.08"') && mc.calls.some((c) => c.video.contentType === 'video/mp4; codecs="avc1.640033"')
      && mc.calls.some((c) => c.video.width === 4316 && c.video.height === 1214 && c.video.framerate === 120) && mc.calls.some((c) => c.video.width === 2160 && c.video.height === 1920 && c.video.framerate === 120)
      && mc.calls.some((c) => c.video.width === 3840 && c.video.framerate === 60) && mc.calls.every((c) => c.video.bitrate > 0), mc.calls.length + " calls");
    await check("decoder hud: supported / smooth / powerEfficient show as S M H per mode; an unsupported codec shows ---", /^AV1 +1080p60 ---  2160p60 ---  120a ---  120b ---/.test(ls[1]) && /^HEVC +1080p60 SMH  2160p60 SMH  120a SMH  120b SMH/.test(ls[2]), ls.join(" | "));
    rep.lines(); rep.lines();
    await check("decoder hud: the probe runs once (further lines() calls cost no query) and the cached text is reused", mc.calls.length === 16 && rep.lines() === rep.lines(), String(mc.calls.length));
    const sw = new L.DecoderReport({ mediaCapabilities: stubMc("sw") }, play); sw.lines(); await sleep(50);
    await check("decoder hud: software decode shows S without H, and the 120 fps cells not smooth", /1080p60 SM-/.test(sw.lines()[1]) && /120a S--/.test(sw.lines()[1]), sw.lines().join(" | "));
    const rj = new L.DecoderReport({ mediaCapabilities: stubMc("reject") }, play); rj.lines(); await sleep(50);
    await check("decoder hud: a rejecting decodingInfo shows n/a, no unhandled rejection", /n\/a/.test(rj.lines()[1]), rj.lines().join(" | "));
    const no = new L.DecoderReport({}, null); const nl = no.lines(); await sleep(20);
    await check("decoder hud: without mediaCapabilities (and without canPlayType) everything reads n/a, nothing throws", no.lines().length === 5 && no.lines().slice(1).every((x) => /n\/a/.test(x) && /play n\/a/.test(x)) && !/NaN|undefined|null/.test(no.lines().join("\n")) && nl.length === 5, no.lines().join(" | "));
    const nul = new L.DecoderReport(null, () => { throw new Error("x"); }); nul.lines();
    await check("decoder hud: a null navigator and a throwing canPlayType are survived", nul.lines().length === 5, "");
    const hl = L.hudLines(null, { frameRate: 90, supportedFrameRates: [72, 90, 120], decoders: rep });
    await check("decoder hud: hudLines appends the decoder table after the runtime frame rate line (XR 90 Hz, supported 72/90/120 shown)", hl.length === 10 && /Runtime frame rate 90 Hz   supported 72\/90\/120/.test(hl[3]) && /^Decode S=/.test(hl[5]), hl.join(" | "));
    await check("decoder hud: every line fits the HUD (<= 76 characters)", hl.every((x) => x.length <= 76), hl.map((x) => x.length).join(","));
    await check("decoder hud: codecInfo maps names and full codec strings and rejects unknown", L.codecInfo("HEVC").codec === "hvc1.1.6.L153.B0" && L.codecInfo("av1").label === "AV1" && L.codecInfo("vp09.00.50.08").codec === "vp09.00.50.08" && L.codecInfo("h264").label === "H.264" && L.codecInfo("mpeg4") === null && L.codecInfo("") === null, "");
    const d3 = L.itemDisplay({ root: "downloads", path: "a.mp4", kind: "video", status: { state: "done", key: "k".repeat(32), sbs_url: "/x", kind: "video", eye_w: 10, eye_h: 10, fps: 120 } });
    const d2 = L.itemDisplay({ root: "downloads", path: "a.webm", kind: "video", codec: "av1" });
    const d2n = L.itemDisplay({ root: "downloads", path: "a.webm", kind: "video" });
    await check("decoder hud: a 3D display carries the HEVC codec, a 2D one the library's codec when known, else none", d3.codec === "hevc" && d2.codec === "av1" && d2n.codec === "", JSON.stringify([d3.codec, d2.codec, d2n.codec]));
  }
  // ---- per item (pure, stubbed video element)
  {
    const mc = stubMc("hw");
    const v = { videoWidth: 0, videoHeight: 0, currentSrc: "http://x/sbs.mp4", currentTime: 0 };
    const st = new L.VideoStats(v, { fps: 120, codec: "hevc" }, { mediaCapabilities: mc });
    st.read(1000);
    await check("decoder hud item: no query before the decoded size is known", mc.calls.length === 0 && L.videoLines(st.read(1000)).length === 3, String(mc.calls.length));
    v.videoWidth = 4316; v.videoHeight = 1214;
    const r0 = st.read(2000);
    await check("decoder hud item: the first read with a size starts one query for THAT video (HEVC 4316x1214@120), shown as 'checking...'", mc.calls.length === 1 && mc.calls[0].video.width === 4316 && mc.calls[0].video.framerate === 120 && /hvc1/.test(mc.calls[0].video.contentType) && mc.calls[0].type === "file"
      && /^Decode HEVC 4316x1214@120: checking\.\.\.$/.test(L.videoLines(r0)[3]), JSON.stringify(mc.calls));
    await sleep(30);
    st.read(3000); st.read(4000);
    const lines = L.videoLines(st.read(5000));
    await check("decoder hud item: the answer lands once ('supported, smooth, hardware'), further reads cost no query", mc.calls.length === 1 && lines.length === 4 && lines[3] === "Decode HEVC 4316x1214@120: supported, smooth, hardware", lines.join(" | "));
    const mc2 = stubMc("none");
    const st2 = new L.VideoStats({ videoWidth: 1920, videoHeight: 1080, currentSrc: "a.webm" }, { fps: 60, codec: "av1" }, { mediaCapabilities: mc2 });
    st2.read(0); await sleep(30);
    await check("decoder hud item: an unsupported codec reads 'NOT supported, not smooth, no hw flag'; webm uses the webm container", L.videoLines(st2.read(10))[3] === "Decode AV1 1920x1080@60: NOT supported, not smooth, no hw flag" && /^video\/webm/.test(mc2.calls[0].video.contentType), L.videoLines(st2.read(10)).join(" | "));
    const st3 = new L.VideoStats({ videoWidth: 640, videoHeight: 360 }, { fps: 30, codec: "hevc" }, {});
    st3.read(0); await sleep(20);
    await check("decoder hud item: without mediaCapabilities the line says n/a", L.videoLines(st3.read(10))[3] === "Decode HEVC 640x360@30: n/a", L.videoLines(st3.read(10)).join(" | "));
    const st4 = new L.VideoStats({ videoWidth: 640, videoHeight: 360 }, { fps: 30 }, { mediaCapabilities: mc });
    st4.read(0);
    await check("decoder hud item: an unknown codec (2D file without codec metadata) is skipped: no query, no decode line", mc.calls.length === 1 && L.videoLines(st4.read(10)).length === 3, String(mc.calls.length));
  }
  // ---- playback speed, XR rate and texture update counter (pure)
  {
    const v = { videoWidth: 100, videoHeight: 50, currentTime: 0 };
    const st = new L.VideoStats(v, { fps: 120 }, null);
    for (let i = 0; i <= 4; i += 1) { v.currentTime = i * 0.25; for (let k = 0; k < 15; k += 1) st.onTex(); st.sample(1000 + i * 250); }
    const r = st.read(2000, 90);
    const ln = L.videoLines(r)[2];
    await check("speed: currentTime advancing as fast as the wall clock reads 1.00x; XR 90 Hz and texture updates 60.0/s on the same line", Math.abs(r.speed - 1) < 0.01 && Math.abs(r.texPerS - 60) < 1 && ln === "Speed 1.00x   XR 90 Hz   texture updates 60.0/s", ln);
    st.texN = 2;
    await check("speed: with two textures (one per eye) the line shows the per-texture rate and 'x2'", L.videoLines(st.read(2000, 90))[2] === "Speed 1.00x   XR 90 Hz   texture updates 30.0/s x2", L.videoLines(st.read(2000, 90))[2]);
    const v2 = { videoWidth: 100, videoHeight: 50, currentTime: 0 };
    const s2 = new L.VideoStats(v2, {}, null);
    for (let i = 0; i <= 4; i += 1) { v2.currentTime = i * 0.125; s2.sample(1000 + i * 250); }
    await check("speed: a video that advances half as fast as the wall clock reads 0.50x (and 0 texture updates reads 0.0/s)", Math.abs(s2.read(2000).speed - 0.5) < 0.01 && /^Speed 0\.50x   XR n\/a   texture updates 0\.0\/s$/.test(L.videoLines(s2.read(2000))[2]), L.videoLines(s2.read(2000))[2]);
    const v3 = { videoWidth: 100, videoHeight: 50, currentTime: 5 };
    const s3 = new L.VideoStats(v3, {}, null);
    for (let i = 0; i <= 4; i += 1) s3.sample(1000 + i * 250);
    await check("speed: a paused video reads 0.00x; before any window it reads '-'", s3.read(2000).speed === 0 && new L.VideoStats(v3, {}, null).read(0) && /^Speed -   XR n\/a   texture updates -$/.test(L.videoLines(new L.VideoStats(v3, {}, null).read(0))[2]), L.videoLines(s3.read(2000))[2]);
  }
  // ---- highest refresh rate request (pure, stub sessions)
  {
    const calls = [];
    const ok = await L.requestHighestRate({ supportedFrameRates: [72, 90, 120, 80], updateTargetFrameRate: (r) => { calls.push(r); return Promise.resolve(); } });
    const rej = await L.requestHighestRate({ supportedFrameRates: new Float32Array([72, 90]), updateTargetFrameRate: () => Promise.reject(new Error("NotSupportedError: nope")) });
    const thr = await L.requestHighestRate({ supportedFrameRates: [90], updateTargetFrameRate: () => { throw new Error("sync boom"); } });
    const none = await L.requestHighestRate({ updateTargetFrameRate: () => Promise.resolve() });
    const nofn = await L.requestHighestRate({ supportedFrameRates: [90] });
    const nul = await L.requestHighestRate(null);
    await check("refresh rate: the highest supported rate is requested through updateTargetFrameRate (120 of 72/90/120/80), once", ok.state === "ok" && ok.rate === 120 && calls.length === 1 && calls[0] === 120, JSON.stringify([ok, calls]));
    await check("refresh rate: a rejected request is reported as failed with the reason; a synchronous throw likewise; nothing rejects", rej.state === "failed" && rej.rate === 90 && /NotSupportedError/.test(rej.error) && thr.state === "failed" && /sync boom/.test(thr.error), JSON.stringify([rej, thr]));
    await check("refresh rate: no supportedFrameRates, no method or no session reports n/a without a call", none.state === "n/a" && nofn.state === "n/a" && nul.state === "n/a", JSON.stringify([none, nofn, nul]));
    const hl = L.hudLines(null, { frameRate: 120, supportedFrameRates: [72, 90, 120], rateRequest: ok });
    const hf = L.hudLines(null, { frameRate: 90, supportedFrameRates: [72, 90], rateRequest: rej });
    await check("refresh rate: the HUD frame-rate line shows the target and its result (ok / FAILED reason / off / n/a)", /Runtime frame rate 120 Hz   supported 72\/90\/120   target 120 ok$/.test(hl[3]) && /target 90 FAILED$/.test(hf[3]) && /^Refresh rate request failed: NotSupportedError: nope$/.test(hf[4]) && hf.length === 6
      && L.rateRequestText({ state: "off" }) === "target off" && L.rateRequestText(none) === "target n/a" && L.rateRequestText({ state: "pending", rate: 120 }) === "target 120 ..." && hf[3].length <= 76, hl[3] + " | " + hf[3]);
  }
  // ---- settings
  {
    const d = ST.normalizeSettings(null);
    const a = ST.applyChoice(d, "vframe:on"), b = ST.applyChoice(d, "hirate:off");
    const rows = ST.settingsRows(d);
    const lay = ST.settingsLayout(rows, 1000, 615);
    await check("settings: 'Highest refresh rate' defaults on, 'Video every XR frame' defaults off, both toggle by id, garbage is ignored, they survive the store",
      d.hiRate === true && d.videoEveryFrame === false && a.videoEveryFrame === true && a.hiRate === true && b.hiRate === false && ST.applyChoice(d, "vframe:maybe").videoEveryFrame === false
      && ST.normalizeSettings({ hiRate: "yes", videoEveryFrame: 1 }).hiRate === true && rows.find((r) => r.id === "hirate").options[0].on && rows.find((r) => r.id === "vframe").options[1].on
      && lay.every((x) => x.x >= 0 && x.y >= 0 && x.x + x.w <= 1000 && x.y + x.h <= 615) && lay.some((x) => x.id === "vframe:on") && lay.some((x) => x.id === "hirate:off"), JSON.stringify(lay.map((x) => x.id)));
    const mem = memStorage(); ST.saveSettings(mem, a);
    await check("settings: the toggles round-trip through storage", ST.loadSettings(mem).videoEveryFrame === true && ST.loadSettings(mem).hiRate === true, "");
  }

  // ---- in the app (preview host, real <video>)
  if (!info.has_video) {
    await note("decoder hud app checks skipped: the stub server could not make a test video (no ffmpeg)");
  } else {
    const PK = "p".repeat(32);
    const S1 = { state: "done", key: PK, kind: "video", sbs_url: `/api/media/3d/file?key=${PK}&name=sbs.mp4`, eye_w: 64, eye_h: 64, fps: 120, source_fps: 30, interpolated: true };
    const src = { kind: "folder", root: "downloads", path: "", sort: {} };
    const file = { root: "downloads", path: "play.mp4", name: "play.mp4", kind: "video" };
    const files = [{ name: "play.mp4", type: "video", size: 1000, mtime: 1.7e9, added: 1.7e9, root: "downloads", path: "play.mp4", url: "/api/media/file?root=downloads&path=play.mp4", favorite: false, width: 128, height: 64, duration: 20 }];
    const cfg = (status) => configure({ role: "admin", files, status: status ? { "play.mp4": status } : {}, view_mode: "ok", with_big: false, tree: {}, list: [], recent: [], multiroot: false });
    const ropts = { preview: true, autoPollMs: 150, mediaTimeoutMs: 8000 };
    const start = (item) => A.startVr({ view: "item", item, source: src }, { ...ropts, storage: memStorage(), settingsStorage: memStorage() });
    const ready = async (h) => { await waitFor(() => h.screen && h.screen.mounted && h.screen.video && h.screen.video.readyState >= 2, 12000); await waitFor(() => h.snapshot().hudLines.some((l) => /^Decode HEVC 128x64@120: (supp|NOT|n)/.test(l)), 8000); await waitFor(() => h.snapshot().hudLines.some((l) => /^Speed [0-9]/.test(l)), 6000); };

    const mc = stubMc("hw");
    setMc(mc);
    await cfg(S1);
    let h = await start({ ...file, status: S1 });
    await ready(h);
    await sleep(1300);
    let hl = h.snapshot().hudLines;
    const n1 = mc.calls.length;
    h.snapshot(); h.snapshot();
    await check("decoder hud app: a 3D item shows the decoder table (header + 4 codecs) and its own 'Decode HEVC 128x64@120' line",
      hl.some((x) => /^Decode S=supported/.test(x)) && ["AV1", "HEVC", "VP9", "H264"].every((c) => hl.some((x) => x.startsWith(c + " ") && /1080p60 SMH/.test(x))) && hl.some((x) => x === "Decode HEVC 128x64@120: supported, smooth, hardware"), hl.join(" | "));
    await check("decoder hud app: 16 session queries + exactly 1 for the item, and no more while it plays and the HUD redraws", n1 === 17 && mc.calls.length === 17 && mc.calls.filter((c) => c.video.width === 128).length === 1, String(mc.calls.length));
    const spd = hl.find((x) => /^Speed /.test(x)) || "";
    const sm = /^Speed ([\d.]+)x   XR n\/a   texture updates ([\d.]+)\/s x2$/.exec(spd);
    await check("video speed app: the real video plays at 1.0x in real time (0.8 - 1.25) and the texture counter counts real uploads (> 0/s); XR n/a in the preview", sm && Number(sm[1]) > 0.8 && Number(sm[1]) < 1.25 && Number(sm[2]) > 0, spd);
    const wideHud = hl;
    await check("decoder hud app: the HUD lines are clean, fit and say the runtime frame rate is not reported in the preview (no refresh-rate request without a session)", wideHud.every((x) => x.length <= 76) && !/NaN|undefined|null/.test(wideHud.join("\n")) && wideHud.some((x) => /^Runtime frame rate not reported$/.test(x)), wideHud.join(" | "));
    // the toggle: default off vs on (count uploads over the same window)
    const vs = h.screen.videoStats;
    const c0 = vs.texCount; await sleep(1500); const offRate = (vs.texCount - c0) / 1.5;
    h.choose("vframe:on");
    await check("video every XR frame app: choosing it sets the setting and the screen flag", h.settings.videoEveryFrame === true, JSON.stringify(h.settings));
    const c1 = vs.texCount; await sleep(1500); const onRate = (vs.texCount - c1) / 1.5;
    await note(`texture updates/s: default ${offRate.toFixed(1)}, every XR frame ${onRate.toFixed(1)}`);
    await check("video every XR frame app: uploads happen at least as often with the toggle on (frame loop rate) as by default, and never fewer than the video's own frames", onRate >= offRate * 0.9 && onRate > 5, `${offRate.toFixed(1)} -> ${onRate.toFixed(1)}`);
    h.screen.video.pause(); await sleep(400);
    const c2 = vs.texCount; await sleep(800);
    await check("video every XR frame app: a paused video is not re-uploaded", vs.texCount - c2 <= 1, String(vs.texCount - c2));
    h.choose("vframe:off");
    h.choose("hirate:off");
    await check("highest refresh rate app: the toggle is settable and stored", h.settings.hiRate === false && h.settings.videoEveryFrame === false, JSON.stringify(h.settings));
    await h.end(); await h.ended;
    h = await start({ ...file, status: S1 });
    await ready(h);
    await sleep(300);
    await check("decoder hud app: a second session probes again (16) plus its item (1) - once per session and once per item", mc.calls.length === 34, String(mc.calls.length));
    await h.end(); await h.ended;

    setMc(undefined);
    await cfg(S1);
    h = await start({ ...file, status: S1 });
    await ready(h);
    hl = h.snapshot().hudLines;
    await check("decoder hud app: without mediaCapabilities the table and the item line read n/a and the HUD still works", hl.some((x) => x === "Decode HEVC 128x64@120: n/a") && hl.filter((x) => /^(AV1|HEVC|VP9|H264) /.test(x)).length === 4 && hl.filter((x) => /^(AV1|HEVC|VP9|H264) /.test(x)).every((x) => /n\/a/.test(x)), hl.join(" | "));
    await h.end(); await h.ended;
    const mcn = stubMc("none");
    setMc(mcn);
    h = await start({ ...file, status: S1 });
    await ready(h);
    hl = h.snapshot().hudLines;
    await check("decoder hud app: unsupported everywhere reads '---' and 'NOT supported'", hl.some((x) => /^HEVC +1080p60 ---/.test(x)) && hl.some((x) => /^Decode HEVC 128x64@120: NOT supported/.test(x)), hl.join(" | "));
    await h.end(); await h.ended;

    const mc20 = stubMc("hw");
    setMc(mc20);
    const heap0 = (performance.memory && performance.memory.usedJSHeapSize) || 0;
    const bad = [];
    for (let i = 0; i < 20; i += 1) {
      const hh = await start({ ...file, status: S1 });
      if (i % 3 !== 2) await waitFor(() => hh.screen && hh.screen.mounted && hh.screen.video && hh.screen.video.readyState >= 2, 8000);
      if (i % 4 === 0) await sleep(200);
      await hh.end();
      const r = await Promise.race([hh.ended, sleep(5000).then(() => ({ timeout: true }))]);
      if (!r || r.timeout) bad.push(`${i}:timeout`);
    }
    await sleep(300);
    await check("decoder hud app: 20 open/close cycles: at most 16 session + 1 item queries per open (a session closed before its HUD drew probes nothing), none repeated within an open", bad.length === 0 && mc20.calls.length <= 20 * 17 && mc20.calls.length >= 12 * 16, `${bad.join(";")} calls=${mc20.calls.length}`);
    await check("decoder hud app: the heap does not grow by more than 30 MB over those 20 cycles (when measurable)", !heap0 || (performance.memory.usedJSHeapSize - heap0) < 30e6, String(heap0 ? performance.memory.usedJSHeapSize - heap0 : "n/a"));
    restoreMc();
  }
  restoreMc();
  await sleep(300);
  await check("decoder hud: no console errors or unhandled rejections during all of the above", errs.length === 0, errs.slice(0, 5).join(" | "));
  console.error = ce;
  window.removeEventListener("error", onerr);
  window.removeEventListener("unhandledrejection", onrej);
}

// V2d: the screen-size slider, the hidden wall, persistence of slider + distance (not the zoom).
async function vrScale() {
  const A = await import("/js/vr/app.js");
  const S = await import("/js/vr/settings.js");
  const errs = [];
  const ce = console.error;
  console.error = (...a) => { errs.push(a.map(String).join(" ")); ce.apply(console, a); };
  const onerr = (e) => errs.push("error: " + (e && e.message));
  const onrej = (e) => errs.push("rejection: " + (e && e.reason && e.reason.message || e.reason));
  window.addEventListener("error", onerr);
  window.addEventListener("unhandledrejection", onrej);

  // ---- pure parts
  await check("screen scale: range 50..150, default 100, steps of 5, buttons +-10", S.SCREEN_SCALE.min === 50 && S.SCREEN_SCALE.max === 150 && S.SCREEN_SCALE.def === 100 && S.SCREEN_SCALE.step === 5 && S.SCREEN_SCALE.button === 10, JSON.stringify(S.SCREEN_SCALE));
  await check("screen scale: normalize clamps and ignores garbage (9999 -> 150, -5 -> 50, text/NaN/null/object -> 100, 112 -> 110)",
    [9999, -5, "x", NaN, null, {}, 112, 100].map((v) => S.normalizeSettings({ screenScale: v }).screenScale).join() === "150,50,100,100,100,100,110,100" && S.normalizeSettings(null).screenScale === 100, "");
  await check("screen scale: track fraction <-> percent (0 -> 50, 0.5 -> 100, 1 -> 150, outside clamps, NaN -> 50), step +-10 clamps",
    [0, 0.5, 1, -3, 7, NaN].map(S.scaleFromFraction).join() === "50,100,150,50,150,50" && S.fractionFromScale(100) === 0.5 && S.stepScale(145, 1) === 150 && S.stepScale(55, -1) === 50 && S.stepScale(100, 1) === 110, "");
  {
    const lay = S.scaleSliderLayout(2240, 224);
    const inside = lay.buttons.every((b) => b.x >= 0 && b.x + b.w <= 2240 && b.y >= 0 && b.y + b.h <= 224);
    const ordered = lay.minus.x + lay.minus.w < lay.track.x && lay.track.x + lay.track.w < lay.plus.x && lay.label.x + lay.label.w < lay.minus.x;
    await check("screen scale: slider layout fits the panel, label | minus | track | plus left to right without overlap", inside && ordered, JSON.stringify(lay));
  }
  {
    const m = memStorage();
    S.saveView(m, { dist: 3, zoom: 5 });
    const raw = JSON.parse(m.getItem(S.VIEW_KEY));
    const bad = [null, "garbage", '{"v":2,"dist":3}', '{"v":1,"dist":"x"}', '{"v":1,"dist":1e9}', '{"v":1,"dist":-4}'].map((x) => { const q = memStorage(); if (x !== null) q.setItem(S.VIEW_KEY, x); return S.loadView(q).dist; });
    await check("screen scale: the view key is versioned, holds only the distance (no zoom), and corrupt / unknown-version / out-of-range values give the default or the clamp",
      S.VIEW_KEY.endsWith(".v1") && raw.v === 1 && raw.dist === 3 && !("zoom" in raw) && S.loadView(m).dist === 3 && bad.join() === "1.4,1.4,1.4,1.4,8,0.8", JSON.stringify([raw, bad]));
  }

  // ---- the app
  let h = null;
  const canvas = () => document.getElementById("vrPreviewCanvas");
  const move = (x, y) => canvas().dispatchEvent(new PointerEvent("pointermove", { clientX: x, clientY: y, bubbles: true }));
  const press = (b) => canvas().dispatchEvent(new PointerEvent("pointerdown", { button: b, bubbles: true }));
  const release = (b) => window.dispatchEvent(new PointerEvent("pointerup", { button: b, bubbles: true }));
  const key = async (k, ms) => { window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); await sleep(ms); window.dispatchEvent(new KeyboardEvent("keyup", { key: k, bubbles: true })); await sleep(60); };
  const wheel = async (dy, n) => { for (let i = 0; i < n; i += 1) { canvas().dispatchEvent(new WheelEvent("wheel", { deltaY: dy, bubbles: true, cancelable: true })); await sleep(90); } };
  const frames = async (n) => { const f0 = h.snapshot().frames; await waitFor(() => h.snapshot().frames >= f0 + n, 8000); };
  const K = "7".repeat(32);
  const sbsPic = { state: "done", key: K, kind: "picture", sbs_url: `/api/media/3d/file?key=${K}&name=sbs.jpg`, eye_w: 1080, eye_h: 1440 };
  const LIB = [mkFile("a.jpg", "image"), mkFile("e.png", "image"), mkFile("f.png", "image")];
  const base = { role: "admin", files: LIB, with_big: true, tree: {}, status: { "a.jpg": sbsPic, "e.png": sbsPic, "f.png": sbsPic }, list: [], recent: [], view_mode: "ok", multiroot: false };
  const stores = () => ({ storage: memStorage(), settingsStorage: memStorage() });
  const start = async (ctx, o) => { h = await A.startVr(ctx, Object.assign({ preview: true, autoPollMs: 150, mediaTimeoutMs: 8000 }, o)); return h; };
  const stop = async () => { await h.end(); return Promise.race([h.ended, sleep(5000).then(() => ({ timeout: true }))]); };
  const wallReady = (n) => waitFor(() => h.wall && h.wall.items.length >= n && h.snapshot().frames > 3, 12000);
  const openFirstFile = async () => {
    const i = h.wall.items.findIndex((x) => x.type !== "folder");
    h.select(i);
    await waitFor(() => h.screen && h.screen.mounted, 8000);
    await frames(3);
  };
  const clickAt = async (id, frac) => { const p = h.projectScale(id, frac); move(p.clientX, p.clientY); await frames(2); press(0); await frames(2); release(0); await frames(2); };
  const SH = 1.5;                                    // vr-logic SCREEN_HEIGHT at DEFAULT_DISTANCE 1.4
  const expectH = () => SH * (h.screen.dist / 1.4) * (h.snapshot().screenScale / 100);
  const sizeOk = () => { const z = h.screen.size; return Math.abs(z.h - expectH()) < 1e-6 && Math.abs(z.w / z.h - h.screen.aspect) < 1e-6; };

  await configure(base);
  let st = stores();
  await start({}, st);
  await wallReady(3);
  let s = h.snapshot();
  await check("screen scale app: on the wall the wall is visible and the slider row is not", s.wallVisible === true && s.scaleSlider && s.scaleSlider.visible === false && h.projectScale("plus") === null, JSON.stringify([s.wallVisible, s.scaleSlider]));
  await openFirstFile();
  s = h.snapshot();
  await check("screen scale app: with an item open the wall is hidden (group not visible, no tile hover) and the slider row, the viewer nav bar and the HUD are shown",
    s.view === "item" && s.wallVisible === false && h.wall.group.visible === false && s.scaleSlider.visible === true && s.buttons.join() === "back,prev,next,hud,recenter,exit" && s.hudVisible === true && s.parts.hud && h.wall.hover === -1, JSON.stringify([s.view, s.wallVisible, s.scaleSlider, s.buttons, s.hudVisible]));
  await check("screen scale app: the slider shows 'Size 100 %', the screen is today's size (1.5 m high at 1.4 m), aspect kept", s.scaleSlider.label === "Size 100 %" && h.screen.scale === 1 && Math.abs(h.screen.size.h - 1.5) < 1e-6 && sizeOk(), JSON.stringify([s.scaleSlider, h.screen.size]));
  {
    const pb = h.projectButton("back"), pp = h.projectScale("plus");
    await check("screen scale app: the slider row is directly below the nav bar (its buttons project lower on screen than the dock's)", pb && pp && pp.clientY > pb.clientY, JSON.stringify([pb, pp]));
  }
  await clickAt("plus");
  await check("screen scale app: plus makes the screen 110 % (label, uniform size, aspect kept, centre distance unchanged)", h.snapshot().screenScale === 110 && h.snapshot().scaleSlider.label === "Size 110 %" && sizeOk() && Math.abs(h.screen.content.group.position.z + h.screen.dist) < 1e-9, JSON.stringify([h.snapshot().screenScale, h.screen.size]));
  await clickAt("minus"); await clickAt("minus");
  await check("screen scale app: minus twice gives 90 %", h.snapshot().screenScale === 90 && sizeOk(), String(h.snapshot().screenScale));
  for (let i = 0; i < 6; i += 1) await clickAt("minus");
  await check("screen scale app: minus stops at the 50 % minimum", h.snapshot().screenScale === 50 && sizeOk(), String(h.snapshot().screenScale));
  for (let i = 0; i < 12; i += 1) await clickAt("plus");
  await check("screen scale app: plus stops at the 150 % maximum (1.5x today's size)", h.snapshot().screenScale === 150 && sizeOk() && Math.abs(h.screen.size.h - 2.25) < 1e-6, String(h.snapshot().screenScale));
  await clickAt("track", 0.25);
  await check("screen scale app: a click on the track sets the value there (25 % along = 75 %)", h.snapshot().screenScale === 75 && sizeOk(), String(h.snapshot().screenScale));
  {
    let p = h.projectScale("track", 0.5); move(p.clientX, p.clientY); await frames(2);
    press(0); await frames(2);
    p = h.projectScale("track", 0.9); move(p.clientX, p.clientY); await frames(3);
    const mid = h.snapshot().screenScale;
    p = h.projectScale("track", 1.1); move(p.clientX, p.clientY + 120); await frames(3);       // off the bar vertically: the drag continues
    const off = h.snapshot().screenScale;
    release(0); await frames(2);
    await check("screen scale app: dragging the knob follows the ray (0.9 along = 140 %), keeps working when the ray leaves the bar (150 %), release ends it", mid === 140 && off === 150 && sizeOk(), JSON.stringify([mid, off]));
  }
  const w1 = JSON.parse(st.settingsStorage.getItem("mediadl.vr.settings") || "{}");
  await check("screen scale app: the slider value is stored in localStorage when the change ends", w1.screenScale === 150, JSON.stringify(w1));

  // ---- move and zoom compose with the slider at min / default / max
  for (const pct of [50, 100, 150]) {
    h.setScale(pct);
    await frames(2);
    const d0 = h.screen.dist, z0 = h.screen.zoom;
    await wheel(-100, 3);                                    // stick Y: move the screen
    await frames(2);
    const d1 = h.screen.dist;
    await key("ArrowRight", 350);                            // stick X: zoom (picture)
    await frames(2);
    const z1 = h.screen.zoom;
    await check(`screen scale app: at ${pct} % the screen still moves (distance ${d0.toFixed(2)} -> ${d1.toFixed(2)}) and zooms (${z0.toFixed(2)} -> ${z1.toFixed(2)}), slider value untouched, size = slider x distance`,
      d1 !== d0 && z1 > z0 && h.snapshot().screenScale === pct && sizeOk(), JSON.stringify([pct, d0, d1, z0, z1, h.snapshot().screenScale]));
    const zKeep = h.screen.zoom;
    h.setScale(pct === 150 ? 120 : pct + 20);
    await frames(2);
    await check(`screen scale app: changing the slider from ${pct} % does not lose the zoom or the distance`, h.screen.zoom === zKeep && h.screen.dist === d1 && sizeOk(), JSON.stringify([zKeep, h.screen.zoom, d1, h.screen.dist]));
    await key("ArrowLeft", 900);                             // back to zoom 1
    await wheel(100, 6);
    await frames(2);
  }

  // ---- Back: the wall returns
  h.back();
  await waitFor(() => !h.snapshot().viewer, 4000);
  await frames(2);
  s = h.snapshot();
  await check("screen scale app: Back shows the wall again (visible, items kept) and hides the slider row", s.wallVisible === true && s.view === "wall" && s.scaleSlider.visible === false && h.projectScale("plus") === null && h.wall.items.length >= 3, JSON.stringify([s.wallVisible, s.view]));
  await stop();

  // ---- persistence: a new XR session / a reload with the same localStorage (fresh sessionStorage), zoom NOT restored
  await configure(base);
  let wr = 0;
  {
    const ls = st.settingsStorage, orig = ls.setItem;
    ls.setItem = (k, v) => { wr += 1; return orig(k, v); };
    await start({}, { storage: memStorage(), settingsStorage: ls });
    await wallReady(3);
    await openFirstFile();
    s = h.snapshot();
    await check("screen scale persist: a new session opens the item with the stored slider value (last set: 120 %)", s.screenScale === 120 && s.scaleSlider.label === "Size 120 %" && sizeOk(), JSON.stringify([s.screenScale, h.screen.dist]));
    await check("screen scale persist: the zoom is NOT restored (picture opens at zoom 1)", h.screen.zoom === 1, String(h.screen.zoom));
    wr = 0;
    await sleep(1500);
    await check("screen scale persist: no storage writes while nothing changes (no write per frame)", wr === 0, String(wr));
    await wheel(-100, 8);
    await sleep(1400);
    const w2 = wr;
    const v2 = JSON.parse(ls.getItem("mediadl.vr.view.v1") || "null");
    await check("screen scale persist: moving the screen writes the distance once it has been still, not per frame (<= 2 writes), and the stored distance is the live one", w2 >= 1 && w2 <= 2 && v2 && Math.abs(v2.dist - h.screen.dist) < 1e-9 && !("zoom" in v2), JSON.stringify([w2, v2, h.screen.dist]));
    const dLive = h.screen.dist;
    await key("ArrowRight", 350);
    await sleep(1000);
    const v3 = JSON.parse(ls.getItem("mediadl.vr.view.v1") || "null");
    await check("screen scale persist: zooming changes nothing in the stored view", v3.dist === v2.dist && !("zoom" in v3), JSON.stringify(v3));
    await h.next();
    await waitFor(() => h.screen && h.screen.mounted && h.snapshot().viewer.path !== "a.jpg", 8000);
    await frames(2);
    await check("screen scale persist: the next item (Next) keeps the slider and the distance", h.snapshot().screenScale === 120 && Math.abs(h.screen.dist - dLive) < 1e-9 && sizeOk(), JSON.stringify([h.snapshot().screenScale, h.screen.dist, dLive]));
    await stop();
    ls.setItem = orig;
    await start({}, { storage: memStorage(), settingsStorage: ls });
    await wallReady(3);
    await openFirstFile();
    await check("screen scale persist: after another reload the distance is back and the zoom is 1 again", Math.abs(h.screen.dist - dLive) < 1e-9 && h.screen.zoom === 1 && h.snapshot().screenScale === 120, JSON.stringify([h.screen.dist, dLive, h.screen.zoom]));
    await stop();
  }
  // corrupt / out-of-range stored values
  for (const [sv, vv, want] of [['{"screenScale": 9999}', '{"v":1,"dist":1e9}', [150, 8]], ["not json", "also not json", [100, 1.4]], ['{"screenScale": "big"}', '{"v":1,"dist":"far"}', [100, 1.4]], ['{"screenScale": 3}', '{"v":1,"dist":0.01}', [50, 0.8]]]) {
    const q = stores();
    q.settingsStorage.setItem("mediadl.vr.settings", sv);
    q.settingsStorage.setItem("mediadl.vr.view.v1", vv);
    await start({}, q);
    await wallReady(3);
    await openFirstFile();
    await check(`screen scale persist: stored ${sv} / ${vv} -> slider ${want[0]} %, distance ${want[1]} (clamped or default)`, h.snapshot().screenScale === want[0] && Math.abs(h.screen.dist - want[1]) < 1e-9 && sizeOk(), JSON.stringify([h.snapshot().screenScale, h.screen.dist]));
    await stop();
  }
  // settings-panel mirror
  {
    await start({}, stores());
    await wallReady(3);
    h.press("settings");
    await frames(2);
    h.choose("screen:75");
    await frames(2);
    const mirrored = h.snapshot().screenScale === 75;
    await openFirstFile();
    await check("screen scale app: the settings panel row 'Screen size' sets the same value (75 %) and the next item uses it", mirrored && h.snapshot().screenScale === 75 && sizeOk() && h.snapshot().scaleSlider.label === "Size 75 %", String(h.snapshot().screenScale));
    await stop();
  }

  // ---- wall hidden while viewing on a folder, favourites and search wall; Back at the same scroll
  await configure({ ...base, search_items: [mkFile("fav1.jpg", "image", { favorite: true }), mkFile("fav2.jpg", "image", { favorite: true })] });
  await start({ view: "wall", source: { kind: "folder", root: "downloads", path: "big", sort: {} } }, stores());
  await waitFor(() => h.wall && h.wall.items.length >= 100, 12000);
  h.wall.scroll.jump(300);
  await frames(4);
  const off0 = h.wall.scroll.offset, first0 = h.wall.firstVisible();
  const pick = h.wall.items.findIndex((it, i) => i >= first0 + 2 && it.type !== "folder");
  h.select(pick);
  await waitFor(() => h.screen && h.screen.mounted, 8000);
  await frames(3);
  await check("screen scale app: the folder wall (2000 files) is hidden while the viewer is open", h.snapshot().wallVisible === false && h.snapshot().view === "item", "");
  h.back();
  await waitFor(() => !h.snapshot().viewer, 4000);
  await frames(3);
  await check("screen scale app: Back from a folder wall shows it again at the same scroll offset (not re-centred)", h.snapshot().wallVisible === true && Math.abs(h.wall.scroll.offset - off0) < 1.5 && h.wall.firstVisible() === first0, JSON.stringify([off0, h.wall.scroll.offset, first0, h.wall.firstVisible()]));
  await stop();
  for (const [name, src] of [["favourites", { kind: "favorites", root: "", path: "", sort: {} }], ["search results", { kind: "search", query: "fav", mode: "filename", root: "", path: "", sort: {} }]]) {
    await start({ view: "wall", source: src }, stores());
    await waitFor(() => h.wall && h.wall.items.length >= 2, 12000).catch(() => {});
    if (!h.wall.items.length) { await check(`screen scale app: ${name} wall loaded for the hide test`, false, JSON.stringify(h.snapshot().source)); await stop(); continue; }
    await frames(3);
    const o = h.wall.scroll.offset;
    h.select(h.wall.items.findIndex((it) => it.type !== "folder"));
    await waitFor(() => h.snapshot().viewer, 4000);
    await frames(3);
    const hidden = h.snapshot().wallVisible === false && h.snapshot().scaleSlider.visible;
    h.back();
    await waitFor(() => !h.snapshot().viewer, 4000);
    await frames(2);
    await check(`screen scale app: ${name} wall is hidden while viewing and back at the same scroll afterwards`, hidden && h.snapshot().wallVisible === true && h.wall.scroll.offset === o, JSON.stringify([hidden, h.snapshot().wallVisible, o, h.wall.scroll.offset]));
    await stop();
  }

  // ---- cycles
  {
    const bad = [];
    const q = stores();
    for (let i = 0; i < 20; i += 1) {
      await start({ view: "item", item: { root: "downloads", path: "e.png", kind: "picture", status: sbsPic }, source: { kind: "folder", root: "downloads", path: "", sort: {} } }, q);
      await waitFor(() => h.screen && h.screen.mounted, 8000).catch(() => {});
      if (i % 3 === 0) h.scaleStep(1);
      if (i % 4 === 1) h.back();
      const r = await stop();
      if (!r || r.timeout || r.textures !== 0 || r.geometries !== 0) bad.push(`${i}:${JSON.stringify(r)}`);
    }
    await check("screen scale app: 20 open/close cycles with the slider and the hidden wall leave 0 textures and 0 geometries", bad.length === 0, bad.join(" ; "));
  }
  await sleep(300);
  await check("screen scale: no console errors or unhandled rejections during all of the above", errs.length === 0, errs.slice(0, 5).join(" | "));
  console.error = ce;
  window.removeEventListener("error", onerr);
  window.removeEventListener("unhandledrejection", onrej);
}

const ONLY = new URLSearchParams(location.search).get("only");
(async () => {
  try {
    if (ONLY === "wall") { await vrWallUnits(); await vrWallApp(); await stub("/__stub/done", {}); return; }
    if (ONLY === "scale") { await vrScale(); await stub("/__stub/done", {}); return; }
    if (ONLY === "decoder") { await vrDecoderHud(); await stub("/__stub/done", {}); return; }
    if (ONLY === "video") { await vrVideoHud(); await stub("/__stub/done", {}); return; }
    if (ONLY === "search") { await vrSearchUnits(); await vrSearchApp(); await stub("/__stub/done", {}); return; }
    await mediaPageAdmin();
    await mediaPageGuest();
    await settingsPage();
    await vrLogic();
    await vrButton();
    await vrGuestAllowlist();
    await vrAppLogic();
    await vrAppModules();
    await vrContextAtClick();
    await vrAppPreview();
    await vrCycles();
    await vrPage();
    await vrShim();
    await vrWallUnits();
    await vrWallApp();
    await vrSearchUnits();
    await vrSearchApp();
    await vrVideoHud();
    await vrDecoderHud();
    await vrScale();
  } catch (e) {
    await check("harness ran to the end", false, e && e.stack || e);
  }
  await stub("/__stub/done", {});
})();
