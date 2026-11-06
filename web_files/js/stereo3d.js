"use strict";

/* The 2D -> 3D conversion, as the browser sees it (backend: stereo3d.py,
 * routes /api/media/3d/*). Shared by the media page (context menu, preview
 * button, select mode) and the settings page (the 3D card), so the wording of
 * a status and the rule for what can be converted live in one place.
 *
 * Converting is admin-only on the server; the pages only decide whether to
 * SHOW the controls, the server decides whether a request is allowed. */

(function () {
  // The same extension sets as stereo3d.py (VIDEO_EXTS / PICTURE_EXTS): audio,
  // animated GIFs and folders are not offered at all rather than refused later.
  const VIDEO_EXTS = [".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"];
  const PICTURE_EXTS = [".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp"];

  function kindOf(path) {
    const name = String(path || "");
    const dot = name.lastIndexOf(".");
    const ext = dot < 0 ? "" : name.slice(dot).toLowerCase();
    if (VIDEO_EXTS.includes(ext)) return "video";
    if (PICTURE_EXTS.includes(ext)) return "picture";
    return null;
  }

  // A file entry from a grid / search / preview ({path, type, isFolder}).
  function canConvert(item) {
    return !!item && !item.isFolder && kindOf(item.path) !== null;
  }

  // What the worker (or this app, around it) is doing, in words.
  const PHASES = {
    uploading: "sending to the worker",
    submitted: "waiting for the GPU",
    uploaded: "waiting for the GPU",
    waiting_gpu: "waiting for the GPU",
    probing: "reading the source",
    interpolating: "smoothing motion",
    converting: "converting",
    joining: "finishing",
    holding: "paused for a manual request",
    held: "paused for a manual request",
    downloading: "fetching the result",
  };

  function phaseText(phase) {
    return PHASES[phase] || (phase ? String(phase) : "working");
  }

  function etaText(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s) || s <= 0) return "";
    if (s < 90) return "under a minute left";
    if (s < 5400) return `about ${Math.round(s / 60)} min left`;
    return `about ${(s / 3600).toFixed(1)} h left`;
  }

  // 0-100, or null when the job has not reported frame counts (pictures, or
  // a video still being read).
  function percentOf(progress) {
    const p = progress || {};
    const total = Number(p.frames_total);
    const done = Number(p.frames_done);
    if (!(total > 0) || !Number.isFinite(done)) return null;
    return Math.max(0, Math.min(100, Math.floor((100 * done) / total)));
  }

  // "converting 42 %, about 5 min left" - for a running item (a status or a
  // queue row: both carry worker_state / state and progress).
  function progressText(item) {
    const phase = phaseText(item.phase || item.worker_state || item.state);
    const pct = percentOf(item.progress);
    const eta = etaText(item.eta_s != null ? item.eta_s : (item.progress || {}).eta_s);
    return [phase + (pct === null ? "" : ` ${pct} %`), eta].filter(Boolean).join(", ");
  }

  // One line for GET /api/media/3d/status.
  function statusText(st) {
    if (!st) return "";
    switch (st.state) {
      case "queued": {
        const pos = st.position ? ` (no. ${st.position})` : "";
        const retry = st.retry_in_s ? `, retrying in ${st.retry_in_s} s` : "";
        return `Queued for 3D${pos}${retry}`;
      }
      case "converting":
        return `3D: ${progressText(st)}`;
      case "held":
        return "3D: paused for a manual request";
      case "failed":
        return `3D failed${st.error ? `: ${st.error}` : ""}`;
      case "done":
        return "3D ready";
      default:
        return "";
    }
  }

  const isActive = (st) => !!st && ["queued", "converting", "held"].includes(st.state);

  async function readJson(res) {
    try { return await res.json(); } catch (e) { return null; }
  }

  async function fetchStatus(root, path) {
    const q = new URLSearchParams({ root: root || "", path });
    const res = await fetch(`/api/media/3d/status?${q}`);
    if (!res.ok) throw new Error(((await readJson(res)) || {}).error || `HTTP ${res.status}`);
    return res.json();
  }

  // items: [{root, path}] in the order they should run. One POST.
  async function requestConversion(items) {
    const res = await fetch("/api/media/3d/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: items.map((i) => ({ root: i.root || "", path: i.path })) }),
    });
    const data = await readJson(res);
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data || { results: [], counts: {} };
  }

  // "3 queued, 1 already waiting, 2 skipped (file not found)".
  function summaryText(data) {
    const c = (data && data.counts) || {};
    const parts = [];
    const add = (n, text) => { if (n) parts.push(`${n} ${text}`); };
    add(c.queued, "queued");
    add(c.promoted, "moved to the front");
    add(c.retry, "retried");
    add(c.duplicate, "already waiting");
    add(c.cached, "already converted");
    if (c.skipped) {
      const why = ((data.results || []).find((r) => r.status === "skipped") || {}).reason;
      parts.push(`${c.skipped} skipped${why ? ` (${why})` : ""}`);
    }
    return parts.length ? parts.join(", ") : "Nothing to convert";
  }


  // ---- View in VR (js/vr/app.js, WebXR) --------------------------------------
  // Any signed-in user may use it: the file routes check root visibility, not the role.
  // The VR app (js/vr/*, and the vendored three.js behind it) is only fetched when a
  // VR-capable browser shows an item or the main view, so the flat pages never pay for it.
  const VR_HELP = "View in VR. Trigger: play/pause. Stick left/right: seek 10 s (picture: zoom). "
    + "Stick up/down: screen distance. Grip + move (picture): pan. A/X: recenter. Stick press: next converted item. B/Y: exit VR.";
  const VR_HINT = "VR needs Chrome on the PC with SteamVR running and chrome://flags #webxr-runtime set to OpenXR.";

  // Availability changes while the page is open (SteamVR started late, headset asleep,
  // runtime reconnect), so the answer is cached only briefly and re-asked on events.
  const VR_CACHE_MS = 10000;
  const VR_FORCE_MIN_MS = 1000;      // forced re-checks (hover spam) are coalesced to one per second
  let vrSupportPromise = null;
  let vrSupportAt = 0;
  async function vrProbe() {
    try { return !!(navigator.xr && await navigator.xr.isSessionSupported("immersive-vr")); } catch (e) { return false; }
  }
  function vrSupported(force) {
    const now = Date.now();
    const age = now - vrSupportAt;
    if (!vrSupportPromise || age > VR_CACHE_MS || (force && age > VR_FORCE_MIN_MS)) {
      vrSupportAt = now;
      vrSupportPromise = vrProbe();
    }
    return vrSupportPromise;
  }
  function vrInvalidate() { vrSupportAt = 0; }

  // cb(supported) after every re-check triggered by navigator.xr "devicechange", window
  // focus or the tab becoming visible. Returns an unsubscribe function.
  function watchVr(cb) {
    const run = () => { vrInvalidate(); vrSupported(true).then(cb).catch(() => {}); };
    const onVis = () => { if (document.visibilityState === "visible") run(); };
    let xr = null;
    try { xr = navigator.xr; if (xr && xr.addEventListener) xr.addEventListener("devicechange", run); } catch (e) { xr = null; }
    window.addEventListener("focus", run);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      try { if (xr && xr.removeEventListener) xr.removeEventListener("devicechange", run); } catch (e) { /* gone */ }
      window.removeEventListener("focus", run);
      document.removeEventListener("visibilitychange", onVis);
    };
  }

  let vrModule = null, vrModuleUrl = null;
  function loadVrApp(moduleUrl = "/js/vr/app.js") {
    if (!vrModule || vrModuleUrl !== moduleUrl) {
      vrModuleUrl = moduleUrl;
      vrModule = import(moduleUrl).catch((e) => { if (vrModuleUrl === moduleUrl) vrModule = null; throw e; });
    }
    return vrModule;
  }

  // st = a /api/media/3d/status answer with state "done".
  function vrItemOf(st) {
    return { url: st.sbs_url, kind: st.kind, eyeW: st.eye_w, eyeH: st.eye_h, key: st.key };
  }

  // The context the VR app starts from (js/vr/logic.js normalizeContext): what the flat page shows now.
  //   { view: "item"|"wall"|"home", item?: {root, path, name, kind, url, key, status?},
  //     source?: {kind, root, path, query, filters, sort: {key, dir}, offset, ...} }
  // lastVrContext is the one passed to the latest startVr (what the harness reads).
  let lastVrContext = null;

  // Resolves with the app's handle once the headset session runs; rejects with a readable Error.
  // Call it from the click handler. opts.onStatus(text) gets short messages while it runs.
  async function startVr(context, opts) {
    lastVrContext = context;
    const mod = await loadVrApp(opts && opts.moduleUrl);
    return mod.startVr(context, opts || {});
  }

  window.Stereo3d = {
    kindOf, canConvert, phaseText, etaText, percentOf, progressText, statusText, isActive,
    fetchStatus, requestConversion, summaryText,
    VR_HELP, VR_HINT, vrSupported, vrInvalidate, watchVr, loadVrApp, vrItemOf, startVr,
    get lastVrContext() { return lastVrContext; },
  };
})();
