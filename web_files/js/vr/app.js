/* The VR app of media-dl: startVr(context) from a click on the flat page, or from /vr.html.
 *
 * It builds, on top of a host (a real immersive-vr session, or a desktop preview canvas):
 *   - a dark room (no floor grid - the owner asked for none),
 *   - the tile wall (V1): a curved wall of thumbnails in justified rows, scrolled by thumbstick or grip-drag,
 *     fed by Recent / Library (roots -> folders -> files) / favourites / search sources (sources.js),
 *   - the dock (breadcrumb, count, [Back] [Top] [Settings] [HUD] [Recenter] [Exit VR]; in the viewer
 *     [Back] [Prev] [Next] [Switch to 3D]...), controller rays, the perf HUD, the settings panel,
 *   - the viewer: opening a tile puts it on the stereo screen (the converted side-by-side file when there is one,
 *     else the 2D original on the same screen); the wall dims to 30 %; Prev/Next walk the wall's order;
 *     an unconverted picture or video asks the server to convert it (autoconvert.js) and swaps to 3D when done.
 * Where it starts is the context the flat page passes in (nav.js planStart): the open item -> the viewer on it with
 * the item's own list behind it; the main view -> that wall; nothing -> the Library. Nothing is synced back to the
 * flat page; what it shows when you leave is kept in sessionStorage for the next entry.
 *
 * Input (either hand): trigger = select on release; grip + move = drag the wall; thumbstick Y = scroll;
 * stick press = jump to top (viewer: next); A = recenter; B tap = Back, hold 3 s = Exit VR (bare viewer: B only exits, also after 3 s).
 * Left hand alone (Steam Frame d-pad): down (X) is B; up, left and right (all Y) = Forward (viewer: next). Prev and
 * Recenter are on the dock. Without a dock (bare) X and Y are plain A and B again.
 * With the "media-dl d-pad" SteamVR binding (vr-bindings/) the d-pad is four buttons: left = previous, right = next
 * (viewer only), up = Forward, down = Back; once left or right has been seen, up no longer steps in the viewer. */
import * as THREE from "../vendor/three.module.min.js";
import { DEFAULT_DISTANCE, clampDistance, recenterPose, createController } from "../vr-logic.js";
import { createXrHost, createPreviewHost, optionalFeaturesFor, requestImmersiveSession, sessionErrorText, D } from "./host.js";
import { createStereoScreen, MEDIA_TIMEOUT_MS } from "./stereo-screen.js";
import { createPanel, drawDock, drawLines, drawSettings, createPanelHit, drawScaleSlider, fitText, FONT, roundRect } from "./ui.js";
import {
  normalizeContext, itemDisplay, sourceTitle, dockLayout, hitButton, dockPose, DOCK, hudLines,
  loadVrState, saveVrState, baseName, DecoderReport, requestHighestRate,
} from "./logic.js";
import { createWall } from "./wall.js";
import { createBackHold } from "./scroll.js";
import { planStart, createNav, dockButtons, neighbour, countText } from "./nav.js";
import { createSource, makeGetJson, folderSpec, favouritesSpec } from "./sources.js";
import { characterSource, loadCharacterSuggestions, loadCharacterPool } from "./discovery.js";
import { createAutoConvert, AUTO } from "./autoconvert.js";
import {
  loadSettings, saveSettings, settingsRows, settingsLayout, applyChoice, normalizeSettings, loadView, saveView,
  clampScale, scaleSliderLayout, scaleFromFraction, fractionFromScale, stepScale, scaleLabel,
} from "./settings.js";
import {
  DEFAULT_OPTIONS, normalizeOptions, chipsFor, chipEdit, planEdit, searchSpec, filterRows, filterLayout, filterEdit, keyboardLayout,
  typeKey, pasteText, keyAction, applyKeyAction, drawKeyboard, chipsLayout, drawChips,
} from "./search.js";

export { requestImmersiveSession, sessionErrorText };

const HUD_PX = 1600;
const EXPIRED = "Session expired - take off the headset, reload media-dl in Chrome and sign in.";
const SETTINGS_W = 1.3, SETTINGS_H = 0.8, SETTINGS_PX = 1000;
const KB = { w: 1.5, h: 0.8, px: 1000, y: -0.05, z: -1.25 }, FP = { w: 1.9, h: 1.15, px: 1200, y: -0.1, z: -1.55 }, CP = { w: 1.4, h: 0.1, px: 1600 };
const FILTER_PAGE_ROWS = 6;
const SS = { w: 1.4, h: 0.14, px: 1600 };            // V2d: the screen-size slider, one row under the dock (viewer only)

async function fetchStatus(item) {
  try {
    const q = new URLSearchParams({ root: item.root || "", path: item.path });
    const res = await fetch(`/api/media/3d/status?${q}`);
    if (!res.ok) return { state: "none" };
    const st = await res.json();
    return st && typeof st === "object" ? st : { state: "none" };
  } catch (e) {
    return { state: "none" };
  }
}

// An item whose 3D state the flat page did not know is asked now (any signed-in user may read it).
async function resolveContext(ctx) {
  if (ctx.item && ctx.item.status === undefined) {
    const st = await fetchStatus(ctx.item);
    return normalizeContext({ ...ctx, item: { ...ctx.item, status: st } });
  }
  return ctx;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const doneStatus = (e) => ({ state: "done", key: e.key, sbs_url: e.sbs_url, kind: e.kind, eye_w: e.eye_w, eye_h: e.eye_h });

/* startVr(context, opts)
 *   opts.preview       render in a desktop canvas instead of a headset session
 *   opts.bare          no room/dock/HUD/wall (the standalone item viewer of P7: B/Y or a load failure ends the session)
 *   opts.onStatus(text) short messages (a toast on the flat page)
 *   opts.onItem(item)  the item on the screen changed (stick-press "next", bare mode)
 *   opts.retryDelayMs, opts.optionalFeatures, opts.mediaTimeoutMs, opts.storage (default sessionStorage), opts.listUrl
 *   opts.settingsStorage (default localStorage), opts.getJson (the API access, tests), opts.autoConvert (false = off),
 *   opts.autoPollMs, opts.loaderDeps (the thumbnail loader's IO, tests)
 * Call it from the click handler. Resolves once the session runs, with
 *   { ended: Promise<{error?, textures, geometries}>, end(), show(item), key, zoom, context, wall, snapshot(), ... };
 * rejects with an Error whose message is fit to show when no session could be started. */
export async function startVr(context, opts = {}) {
  const status = typeof opts.onStatus === "function" ? opts.onStatus : () => {};
  const listUrl = opts.listUrl || "/api/media/3d/list";
  let storage = null;
  try { storage = opts.storage === undefined ? window.sessionStorage : opts.storage; } catch (e) { storage = null; }
  let settingsStorage = null;
  try { settingsStorage = opts.settingsStorage === undefined ? window.localStorage : opts.settingsStorage; } catch (e) { settingsStorage = null; }
  const saved = storage ? loadVrState(storage) : null;
  const ctx0 = normalizeContext(context);
  const view0 = settingsStorage ? loadView(settingsStorage) : { dist: DEFAULT_DISTANCE };       // V2d: the distance is remembered across sessions
  const startDist = clampDistance(saved && saved.dist !== undefined ? saved.dist : view0.dist);
  const hudOn0 = saved ? saved.hud : ctx0.view !== "home";
  const bare = !!opts.bare;

  // The media request starts as soon as there is a renderer, in parallel with the session request
  // (which must stay in this click's first synchronous stretch).
  let gotRenderer;
  const rendererP = new Promise((r) => { gotRenderer = r; });
  const resolving = resolveContext(ctx0);
  const makeScreen = async (ctx, renderer, dist) => {
    if (ctx.view !== "item") return null;
    const display = itemDisplay(ctx.item);
    return display ? { screen: createStereoScreen({ renderer, item: display, onStatus: status, dist }), display } : null;
  };
  const earlyP = Promise.all([resolving, rendererP]).then(([c, r]) => makeScreen(c, r, startDist)).catch(() => null);

  let host;
  try {
    host = opts.preview ? createPreviewHost() : await createXrHost({
      retryDelayMs: opts.retryDelayMs,
      optionalFeatures: opts.optionalFeatures || optionalFeaturesFor(typeof location !== "undefined" ? location.search : ""),
      onRenderer: gotRenderer,
    });
  } catch (e) {
    earlyP.then((x) => x && x.screen.dispose());
    throw e;
  }
  gotRenderer(host.renderer);
  const ctx = await resolving;
  const early = await earlyP;

  const { scene, renderer } = host;
  let context1 = ctx;
  let screen = null, display = null, message = "", hudOn = hudOn0;
  let yaw = 0, rigPose = { x: 0, y: 0, z: 0, yaw: 0 };
  let ended = false;
  host.ended.then(() => { ended = true; });

  // ---- the room (static)
  let rays = [], dock = null, hud = null, layout = [], wall = null, settingsPanel = null, scalePanel = null;
  let settings = settingsStorage ? loadSettings(settingsStorage) : normalizeSettings(null);
  // ---- navigation state (V1)
  const plan = planStart(ctx);
  const nav = createNav(plan);
  const sources = [];                          // one source per wall in the nav stack (Back is instant)
  const threeD = new Map();                    // "root|path" -> done status (from /3d/list and from conversions seen here)
  const favoriteState = new Map();              // keeps local toggles visible while paged walls finish loading
  const getJson = opts.getJson || makeGetJson();
  let sourceError = "";
  let ready3d = null;                          // { item, status } while a video's conversion finished and the switch is offered
  let autoNote = "";                           // "converting to 3D..." etc. for the dock
  let viewerItem = null;                       // the item context shown in the viewer
  let settingsOpen = false;
  let titleCache = "", subCache = "";
  let favoriteBusy = false;
  // search and filters (V2a): the keyboard and filter panels exist only while open; the chip strip while a wall has constraints
  let searchPanel = null, filterPanel = null, chipsPanel = null;
  let variantPool = null, variantPage = 0, filterPage = 0;
  const variantThumbs = new Map();
  let variantGeneration = 0;
  let kbText = "", kbLayer = "abc", kbLayout = null, searchHover = null, filterHover = null, chipsHover = null;
  let chipList = [], chipLay = [], chipKey = "";
  let filterOptions = DEFAULT_OPTIONS, optionsAsked = false, searchMode = "filename", flashTimer = 0;
  let characterSuggestions = [], suggestionSeq = 0, suggestionTimer = 0;
  host.addDisposer(() => { variantGeneration += 1; variantThumbs.clear(); });

  const currentSource = () => sources[sources.length - 1];
  const titleText = () => {
    if (bare) return sourceTitle(ctx.source, ctx.item);
    const s = currentSource();
    return s ? s.title : sourceTitle(plan.spec, plan.item);
  };
  const viewerOpen = () => !!nav.viewer;
  const subtitleText = () => {
    if (message) return message;
    if (viewerOpen() && viewerItem) {
      const mode = display ? (display.mode === "sbs" ? "3D" : "2D") : "...";
      const extra = ready3d ? "  -  3D ready, switch?" : (autoNote ? `  -  ${autoNote}` : "");
      return `${viewerItem.name}  -  ${mode}${extra}`;
    }
    if (bare) return ctx.item ? `${ctx.item.name}  -  ${display ? (display.mode === "sbs" ? "3D" : "2D") : "..."}` : "";
    if (sourceError) return sourceError;
    const s = currentSource();
    if (!s || !wall) return "";
    if (!wall.items.length) return s.loading || !s.ready ? "Loading..." : "Empty";
    const more = s.loading ? "  (loading more)" : "";
    return countText(wall.firstVisible(), wall.items.length, s.total) + more;
  };
  const buttons = () => dockButtons(viewerOpen() ? "viewer" : "wall", {
    ready3d: !!ready3d, favorite: !!(viewerItem && viewerItem.favorite), favoriteBusy,
  });

  const rig = new THREE.Group();               // moves with recenter: dock, HUD, wall, settings
  let dockBtns = buttons();
  if (!bare) {
    scene.add(rig);
    dock = createPanel({ widthM: DOCK.width, heightM: DOCK.height, pxPerM: DOCK.px });
    const dp = dockPose();
    dock.mesh.position.set(dp.x, dp.y, dp.z);
    dock.mesh.rotation.x = dp.tiltX;
    rig.add(dock.mesh);
    layout = dockLayout(dockBtns, dock.w, dock.h);

    hud = createPanel({ widthM: 0.9, heightM: 0.62, pxPerM: HUD_PX, opacity: 0.95 });
    hud.mesh.position.set(-0.95, 0.05, -1.25);
    hud.mesh.rotation.y = Math.atan2(0.95, 1.25);
    hud.mesh.visible = hudOn;
    rig.add(hud.mesh);

    settingsPanel = createPanel({ widthM: SETTINGS_W, heightM: SETTINGS_H, pxPerM: SETTINGS_PX, opacity: 1 });
    settingsPanel.mesh.position.set(0, 0.05, -1.5);
    settingsPanel.mesh.visible = false;
    rig.add(settingsPanel.mesh);

    wall = createWall({ renderer, parent: rig, settings, loaderDeps: opts.loaderDeps });

    // V2d: the screen-size slider sits directly below the dock (the dock does not follow the screen's size or distance,
    // so the slider stays reachable at every size); it is shown in the viewer only.
    scalePanel = createPanel({ widthM: SS.w, heightM: SS.h, pxPerM: SS.px, opacity: 1 });
    {
      const dp2 = dockPose();
      const a2 = (DOCK.dropDeg * Math.PI) / 180, off2 = DOCK.height / 2 + SS.h / 2 + 0.02;
      scalePanel.mesh.position.set(dp2.x, dp2.y - off2 * Math.cos(a2), dp2.z + off2 * Math.sin(a2));
      scalePanel.mesh.rotation.x = dp2.tiltX;
    }
    scalePanel.mesh.visible = false;
    scalePanel.layoutButtons = scaleSliderLayout(scalePanel.w, scalePanel.h).buttons;
    rig.add(scalePanel.mesh);

    for (let i = 0; i < 2; i += 1) {
      const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]);
      const m = new THREE.LineBasicMaterial({ color: 0x9db8ff });
      const line = new THREE.Line(g, m);
      line.frustumCulled = false;
      line.visible = false;
      scene.add(line);
      rays.push(line);
    }
    host.addDisposer(() => {
      for (const r of rays) { r.geometry.dispose(); r.material.dispose(); scene.remove(r); }
      dock.dispose(); hud.dispose(); settingsPanel.dispose(); scalePanel.dispose();
      closeSearch(); closeFilters();
      if (chipsPanel) { chipsPanel.dispose(); chipsPanel = null; }
      if (flashTimer) { clearTimeout(flashTimer); flashTimer = 0; }
      wall.dispose();
      scene.remove(rig);
    });
  }

  let dockHover = null;
  const drawDockNow = (hover) => {
    if (!dock) return;
    titleCache = titleText();
    subCache = subtitleText();
    dock.redraw((c, w, h) => drawDock(c, w, h, { layout, title: titleCache, subtitle: subCache, hover, message: "" }));
  };
  const refreshDockButtons = () => {
    dockBtns = buttons();
    if (dock) layout = dockLayout(dockBtns, dock.w, dock.h);
    drawDockNow(dockHover);
    syncOverlays();
    syncViewerParts();
  };
  // V2d: while an item is shown the wall (tiles, caption, hover frame) is hidden - not drawn, and aim() skips it - and the
  // slider row is shown; Back reverses both. The wall keeps its scroll and its items.
  function syncViewerParts() {
    const v = viewerOpen();
    if (wall) wall.group.visible = !v;
    if (scalePanel) { scalePanel.mesh.visible = v; if (!v) { scaleHover = null; scaleDrag = false; persistScale(); } }
  }
  drawDockNow(null);

  let settingsHover = null;
  const drawSettingsNow = (hover) => {
    if (!settingsPanel) return;
    settingsHover = hover;
    const rows = settingsRows(settings);
    const lay = settingsLayout(rows, settingsPanel.w, settingsPanel.h);
    settingsPanel.layoutButtons = lay;
    settingsPanel.redraw((c, w, h) => drawSettings(c, w, h, { layout: lay, rows, hover }));
  };
  drawSettingsNow(null);

  // ---- V2d: screen size. Effective size = slider percent (settings.screenScale) x the live distance (thumbstick Y); the
  // picture zoom/pan (a texture window, thumbstick X / grip) is independent of both, so a slider change never loses a zoom,
  // and a move/zoom never changes the slider. Slider -> settings key (on change end); distance -> versioned view key
  // (written once still for 600 ms, and on exit), both in localStorage (settingsStorage). The zoom is NOT persisted.
  let scaleHover = null, scaleDrag = false, scaleDirty = false;
  const scaleLay = scalePanel ? scaleSliderLayout(scalePanel.w, scalePanel.h) : null;
  const drawScaleNow = () => {
    if (!scalePanel) return;
    scalePanel.redraw((c, w, h) => drawScaleSlider(c, w, h, { lay: scaleLay, label: scaleLabel(settings.screenScale), frac: fractionFromScale(settings.screenScale), hover: scaleHover, drag: scaleDrag }));
  };
  function setScreenScale(pct) {
    const v = clampScale(pct);
    if (v === settings.screenScale) return false;
    settings = Object.assign({}, settings, { screenScale: v });
    if (screen) screen.setScale(v / 100);
    scaleDirty = true;
    drawScaleNow();
    if (settingsOpen) drawSettingsNow(settingsHover);
    return true;
  }
  function persistScale() {
    if (scaleDirty && settingsStorage) saveSettings(settingsStorage, settings);
    scaleDirty = false;
  }
  drawScaleNow();
  let liveDist = startDist, savedDist = startDist, viewChangedAt = -1;
  function persistView() {
    viewChangedAt = -1;
    if (liveDist === savedDist) return;
    savedDist = liveDist;
    if (settingsStorage) saveView(settingsStorage, { dist: liveDist });
  }
  host.addDisposer(() => { persistScale(); persistView(); });

  // ---- the item on the stereo screen
  const attachScreen = (s, d) => {
    screen = s; display = d;
    if (s.setEveryFrame) s.setEveryFrame(settings.videoEveryFrame);
    if (s.setScale) s.setScale(settings.screenScale / 100);
    if (yaw !== 0 || rigPose.x || rigPose.y || rigPose.z) s.setPose(rigPose);
    scene.add(s.group);
  };
  host.addDisposer(() => { if (screen) { screen.dispose(); screen = null; } });
  if (early) attachScreen(early.screen, early.display);

  const loadScreen = async (s, isFirst) => {
    const timeout = sleep(opts.mediaTimeoutMs === undefined ? MEDIA_TIMEOUT_MS : opts.mediaTimeoutMs).then(() => "timeout");
    const got = await Promise.race([s.ready.then(() => "ready"), timeout, host.ended.then(() => "ended")]);
    if (got === "ended" || ended) return false;
    if (got === "timeout" || s.failed) {
      const msg = got === "timeout" ? "The media did not load within 20 s." : s.failed;
      if (bare) {
        host.endError = got === "timeout" ? "The 3D media did not load within 20 s, so VR was closed." : s.failed;
        await host.end();
        return false;
      }
      message = msg;
      status(msg);
      drawDockNow(dockHover);
      if (screen === s) { s.dispose(); screen = null; } else s.dispose();
      return false;
    }
    if (isFirst || screen === s) s.mount();
    return true;
  };
  if (screen) loadScreen(screen, true);

  // Swaps the screen to another item; only the newest request wins. resume = { t, playing } keeps a video's place.
  let swapSeq = 0;
  async function showDisplay(d, itemCtx, notify, resume) {
    const my = ++swapSeq;
    const fresh = createStereoScreen({ renderer, item: d, onStatus: status, dist: screen ? screen.dist : liveDist });
    await fresh.ready;
    if (ended || my !== swapSeq) { fresh.dispose(); return false; }
    if (fresh.failed) { message = fresh.failed; status(fresh.failed); fresh.dispose(); drawDockNow(dockHover); return false; }
    const old = screen;
    message = "";
    if (resume && fresh.video && resume.t > 0) { try { fresh.video.currentTime = resume.t; } catch (e) { /* not seekable yet */ } }
    attachScreen(fresh, d);
    if (old) old.dispose();
    fresh.mount();
    if (resume && fresh.video && !resume.playing) fresh.video.pause();
    if (itemCtx && bare) { context1 = normalizeContext({ ...context1, view: "item", item: itemCtx }); ctx.item = context1.item; ctx.view = "item"; }
    drawDockNow(dockHover);
    if (notify && typeof opts.onItem === "function") opts.onItem(itemCtx || d);
    return true;
  }

  // ---- 3D badges and conversions seen
  const statusFor = (it) => it.status || threeD.get(it.id) || null;
  function applyBadges() {
    if (!wall || !threeD.size) return;
    const items = wall.items;
    for (let i = 0; i < items.length; i += 1) {
      const it = items[i];
      if (it.type === "folder" || it.type === "subject" || it.status) continue;
      const st = threeD.get(it.id);
      if (st) { it.status = st; wall.refreshItem(i); }
    }
  }
  if (!bare) {
    getJson(listUrl).then((data) => {
      if (ended) return;
      for (const e of (data && data.items) || []) if (e && e.sbs_url && e.path !== undefined) threeD.set(`${e.root || ""}|${e.path}`, doneStatus(e));
      applyBadges();
    }).catch((e) => { if (e && e.expired) { sourceError = EXPIRED; drawDockNow(dockHover); } });
  }

  // ---- sources and the wall
  const openSeq = { n: 0 };
  function syncWall(src, scrollIndex, align) {
    if (!wall || ended) return;
    if (src.items.length > wall.items.length && wall.items.length > 0 && wall.items[0] === src.items[0]) wall.append(src.items.slice(wall.items.length));
    else if (src.items.length !== wall.items.length || wall.items[0] !== src.items[0]) wall.setItems(src.items);
    for (const item of src.items) if (favoriteState.has(item.id)) item.favorite = favoriteState.get(item.id);
    applyBadges();
    if (scrollIndex !== undefined && scrollIndex >= 0) wall.scrollToTile(scrollIndex, align || "top");
  }
  async function pumpSource(src, my, until) {
    await src.loadMore();
    if (my !== openSeq.n || ended) return;
    sourceError = src.error ? (src.error.expired ? EXPIRED : (src.error.network ? "Could not reach the server." : "Could not load this list.")) : "";
    syncWall(src);
    if (until) {
      let idx = until();
      for (let guard = 0; idx < 0 && src.hasMore && !src.error && guard < 60; guard += 1) {
        await src.loadMore();
        if (my !== openSeq.n || ended) return;
        syncWall(src);
        idx = until();
      }
      if (idx >= 0) syncWall(src, idx, until.align || "center");
    }
    drawDockNow(dockHover);
  }
  function openSource(spec, opts2 = {}) {
    const my = ++openSeq.n;
    const src = createSource(spec, { getJson, pageSize: () => Math.max(1,Math.floor((wall?.settings.span || 120)/10.5)) * (Math.ceil(55/10.5)+1) });
    src.onChange = () => { if (my === openSeq.n && !ended) syncWall(src); };
    sources.push(src);
    sourceError = "";
    if (wall) wall.setItems([]);
    let until = null;
    if (opts2.landItem) {
      until = () => src.indexOf(opts2.landItem.root, opts2.landItem.path);
      until.align = "center";
    } else if (opts2.index > 0) {
      until = () => (src.items.length > opts2.index ? opts2.index : -1);
      until.align = "top";
    }
    pumpSource(src, my, until);
    syncOverlays();
    return src;
  }
  // Back to a wall that is still in memory (the stack keeps each wall's source): instant, scrolled where it was.
  function showSource(src, index) {
    sourceError = src.error ? (src.error.expired ? EXPIRED : "Could not load this list.") : "";
    for (const item of src.items) if (favoriteState.has(item.id)) item.favorite = favoriteState.get(item.id);
    wall.setItems(src.items);
    applyBadges();
    if (index > 0) wall.scrollToTile(index, "top");
    const my = ++openSeq.n;
    if (src.hasMore && !src.error) pumpMore(src, my);
  }
  async function pumpMore(src, my) {
    if (src.loading) return;
    await src.loadMore();
    if (my !== openSeq.n || ended || src !== currentSource()) return;
    syncWall(src);
    drawDockNow(dockHover);
  }

  // ---- auto convert on view
  const auto = bare || opts.autoConvert === false ? null : createAutoConvert({
    pollMs: opts.autoPollMs || AUTO.pollMs,
    post: async (body) => {
      const res = await fetch("/api/media/3d/view", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify(body), keepalive: body.leave === true,
      });
      let json = null;
      try { json = await res.json(); } catch (e) { json = null; }
      return { status: res.status, json };
    },
    getStatus: fetchStatus,
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (id) => clearTimeout(id),
    onState: (st) => onAutoState(st),
  });
  if (auto) host.addDisposer(() => auto.dispose());

  function onAutoState(st) {
    if (ended || !viewerItem || !st.item || st.item.path !== viewerItem.path || st.item.root !== viewerItem.root) return;
    if (st.phase === "done" && st.status) {
      const done = doneStatus(st.status);
      threeD.set(`${viewerItem.root}|${viewerItem.path}`, done);
      viewerItem = normalizeContext({ item: { ...viewerItem, status: done } }).item;
      const idx = wall ? wall.items.findIndex((x) => x.id === `${viewerItem.root}|${viewerItem.path}`) : -1;
      if (idx >= 0) { wall.items[idx].status = done; wall.refreshItem(idx); }
      autoNote = "";
      if (display && display.mode === "sbs") { refreshDockButtons(); return; }
      if (viewerItem.kind === "picture") {
        // a picture swaps in place
        const d = itemDisplay(viewerItem);
        if (d) showDisplay(d, viewerItem, false).then((ok) => { if (ok) refreshDockButtons(); });
      } else {
        ready3d = { item: viewerItem, status: done };
        status("3D ready - switch?");
        refreshDockButtons();
      }
    } else if (st.phase === "requested" || st.phase === "converting") {
      autoNote = "converting to 3D...";
      drawDockNow(dockHover);
    } else if (st.phase === "failed") {
      autoNote = "3D conversion failed";
      drawDockNow(dockHover);
    } else if (st.phase === "backoff" || st.phase === "off") {
      autoNote = "";
      drawDockNow(dockHover);
    }
  }

  async function switchTo3d() {
    if (!ready3d || !screen) return;
    const r = ready3d;
    const resume = screen.video ? { t: screen.video.currentTime || 0, playing: !screen.video.paused } : null;
    const d = itemDisplay(r.item);
    ready3d = null;
    if (d && d.mode === "sbs") await showDisplay(d, r.item, false, resume);
    refreshDockButtons();
  }

  // ---- viewer
  const itemCtxOf = (it) => {
    const st = statusFor(it);
    const item = normalizeContext({ item: { root: it.root, path: it.path, name: it.name, kind: it.type, url: it.url,
      favorite: !!it.favorite, status: st && st.state === "done" ? st : { state: "none" } } }).item;
    if (item) item.favorite = !!it.favorite;
    return item;
  };
  let openedScroll = -1, openedId = "";          // the wall's scroll when the viewer opened (Back restores it unless Prev/Next moved on)
  function openViewerItem(itemCtx) {
    if (!viewerItem && wall) { openedScroll = wall.scroll.offset; openedId = `${itemCtx.root}|${itemCtx.path}`; }
    viewerItem = itemCtx;
    ready3d = null;
    autoNote = "";
    nav.openViewer(itemCtx);
    if (wall) wall.setDim(0.3);
    settingsOpen = false;
    if (settingsPanel) settingsPanel.mesh.visible = false;
    closeSearch(); closeFilters();
    const d = itemDisplay(itemCtx);
    refreshDockButtons();
    if (d) showDisplay(d, itemCtx, false);
    if (auto) auto.open(itemCtx);
  }
  function closeViewer() {
    if (auto) auto.leave();
    nav.closeViewer();
    const was = viewerItem;
    viewerItem = null;
    ready3d = null;
    autoNote = "";
    message = "";
    swapSeq += 1;
    if (screen) { screen.dispose(); screen = null; display = null; }
    if (wall) {
      wall.setDim(1);
      if (was && openedScroll >= 0 && openedId === `${was.root}|${was.path}`) wall.scroll.jump(openedScroll);     // the same scroll as before the viewer
      else if (was) {
        const idx = wall.items.findIndex((x) => x.id === `${was.root}|${was.path}`);
        if (idx >= 0) wall.scrollToTile(idx, "center");
      }
      openedScroll = -1;
    }
    refreshDockButtons();
  }
  let busyNav = false;
  async function toggleFavorite() {
    if (!viewerItem || favoriteBusy) return;
    const item = viewerItem, id = `${item.root}|${item.path}`, favorite = !item.favorite;
    favoriteBusy = true; refreshDockButtons();
    try {
      const res = await fetch("/api/media/favorite", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
        body: JSON.stringify({ root: item.root || "", path: item.path, favorite }),
      });
      let data = null; try { data = await res.json(); } catch (_) { /* report the HTTP status below */ }
      if (!res.ok) throw new Error(data && data.error ? String(data.error) : (res.status === 401 ? "Sign in again to change favourites." : "Could not update this favourite."));
      const row = wall && wall.items.find((x) => x.id === id);
      favoriteState.set(id, favorite);
      if (row) { row.favorite = favorite; wall.refreshItem(wall.items.indexOf(row)); }
      if (viewerItem && `${viewerItem.root}|${viewerItem.path}` === id) viewerItem = { ...viewerItem, favorite };
      flash(favorite ? "Added to favourites." : "Removed from favourites.");
    } catch (e) {
      flash(e && e.message ? e.message : "Could not update this favourite.");
    } finally {
      favoriteBusy = false; refreshDockButtons();
    }
  }
  // Bare mode (P7 viewer): walks the list of finished conversions.
  async function goNext() {
    if (busyNav) return;
    busyNav = true;
    try {
      const res = await fetch(listUrl);
      const data = res.ok ? await res.json() : null;
      const items = ((data && data.items) || []).filter((i) => i && i.sbs_url);
      const cur = screen ? screen.key : null;
      if (items.length < 2) { status("No other converted item."); return; }
      const at = items.findIndex((i) => i.key === cur);
      const nxt = items[(at + 1) % items.length];
      const itemCtx = { root: nxt.root || "", path: nxt.path || "", name: nxt.title || baseName(nxt.path || ""), kind: nxt.kind === "picture" ? "picture" : "video",
        key: nxt.key, status: doneStatus(nxt) };
      await showDisplay({ mode: "sbs", url: nxt.sbs_url, kind: nxt.kind, eyeW: nxt.eye_w, eyeH: nxt.eye_h, key: nxt.key, name: itemCtx.name }, itemCtx, true);
    } catch (e) {
      status("Could not load the next item.");
    } finally {
      busyNav = false;
    }
  }
  async function step(dir) {
    if (bare) { if (dir > 0) await goNext(); return; }
    if (busyNav || !viewerItem || !wall) return;
    busyNav = true;
    try {
      const id = `${viewerItem.root}|${viewerItem.path}`;
      let cur = wall.items.findIndex((x) => x.id === id);
      let k = neighbour(wall.items, cur < 0 ? 0 : cur, dir);
      const src = currentSource();
      for (let guard = 0; k < 0 && dir > 0 && src && src.hasMore && !src.error && guard < 20; guard += 1) {
        await src.loadMore();
        syncWall(src);
        cur = wall.items.findIndex((x) => x.id === id);
        k = neighbour(wall.items, cur < 0 ? 0 : cur, dir);
      }
      if (k < 0) { status(dir > 0 ? "That was the last item." : "That was the first item."); return; }
      openViewerItem(itemCtxOf(wall.items[k]));
    } finally {
      busyNav = false;
    }
  }

  // ---- search, filters, chips (V2a). The current wall's spec is the truth (search.js): spec.filters / spec.query / spec.mode.
  function flash(text) {
    message = text;
    drawDockNow(dockHover);
    if (flashTimer) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flashTimer = 0; if (message === text) { message = ""; drawDockNow(dockHover); } }, 2500);
  }
  const mkPanel = (d, y, z) => {
    const p = createPanel({ widthM: d.w, heightM: d.h, pxPerM: d.px, opacity: 1 });
    p.mesh.position.set(0, y, z);
    rig.add(p.mesh);
    return p;
  };
  function closeSettings() { settingsOpen = false; if (settingsPanel) settingsPanel.mesh.visible = false; }

  function drawSearchNow(hover) {
    if (!searchPanel) return;
    searchHover = hover;
    kbLayout = keyboardLayout(kbLayer, searchPanel.w, searchPanel.h, { suggestions: characterSuggestions.length });
    searchPanel.layoutButtons = [...kbLayout.keys, ...kbLayout.suggestions.map((b, i) => ({ ...b, label: characterSuggestions[i]?.label || characterSuggestions[i]?.tag || "" }))];
    searchPanel.redraw((c, w, h) => drawKeyboard(c, w, h, { layout: kbLayout, text: kbText, hover,
      suggestions: characterSuggestions, suggestionHover: hover && hover.startsWith("suggest:") ? hover : null }));
  }
  function queueCharacterSuggestions() {
    if (suggestionTimer) { clearTimeout(suggestionTimer); suggestionTimer = 0; }
    const seq = ++suggestionSeq, query = kbText.trim();
    if (searchMode !== "character" || query.length < 2) {
      characterSuggestions = [];
      if (searchPanel) drawSearchNow(searchHover);
      return;
    }
    suggestionTimer = setTimeout(async () => {
      suggestionTimer = 0;
      try {
        const rows = await loadCharacterSuggestions(getJson, query, 8);
        if (ended || !searchPanel || seq !== suggestionSeq || query !== kbText.trim()) return;
        characterSuggestions = rows;
        drawSearchNow(searchHover);
      } catch (_) {
        if (seq === suggestionSeq) { characterSuggestions = []; if (searchPanel) drawSearchNow(searchHover); }
      }
    }, 220);
  }
  function chooseCharacterSuggestion(index) {
    const pool = characterSuggestions[index];
    if (!pool) return;
    const base = currentSource()?.spec || {};
    const selected = characterSource(pool, "", base);
    const spec = searchSpec(selected, kbText, "character");
    if (!spec) return;
    closeSearch(); nav.go(spec, wall ? wall.firstVisible() : 0); openSource(spec); refreshDockButtons();
  }
  // A desktop or Bluetooth keyboard while the panel is open: the host's own keys (G, R, B, Escape...) must not fire.
  const onSearchKey = (e) => {
    if (!searchPanel) return;
    e.stopImmediatePropagation();
    if (e.ctrlKey || e.metaKey || e.altKey) return;                   // Ctrl+V arrives as a paste event
    const a = keyAction(e);
    if (!a.op) return;
    e.preventDefault();
    if (a.op === "enter") runSearchNow();
    else if (a.op === "close") closeSearch();
    else { kbText = applyKeyAction(kbText, a); drawSearchNow(searchHover); queueCharacterSuggestions(); }
  };
  const onSearchPaste = (e) => {
    if (!searchPanel) return;
    const t = e.clipboardData && e.clipboardData.getData ? e.clipboardData.getData("text") : "";
    if (t) { e.preventDefault(); kbText = pasteText(kbText, t); drawSearchNow(searchHover); queueCharacterSuggestions(); }
  };
  function openSearch() {
    if (bare || !wall || viewerOpen() || searchPanel) return;
    closeFilters(); closeSettings();
    const sp = currentSource() ? currentSource().spec : null;
    if (sp && sp.kind === "people") searchMode = "people";
    else if (sp && sp.kind === "characters") searchMode = "character";
    kbText = sp && (["search", "favorites", "people", "characters"].includes(sp.kind)) && sp.query && sp.query !== "*" ? sp.query : "";
    kbLayer = "abc";
    searchPanel = mkPanel(KB, KB.y, KB.z);
    if (searchMode === "character") queueCharacterSuggestions();
    host.captureKeys = true;
    window.addEventListener("keydown", onSearchKey, true);
    window.addEventListener("paste", onSearchPaste, true);
    drawSearchNow(null);
  }
  function closeSearch() {
    if (!searchPanel) return;
    window.removeEventListener("keydown", onSearchKey, true);
    window.removeEventListener("paste", onSearchPaste, true);
    if (suggestionTimer) { clearTimeout(suggestionTimer); suggestionTimer = 0; }
    suggestionSeq += 1;
    characterSuggestions = [];
    searchPanel.dispose();
    host.captureKeys = false;
    searchPanel = null; kbLayout = null; searchHover = null;
  }
  function runSearchNow() {
    const src = currentSource();
    if (!src || !wall) return;
    const spec = searchSpec(src.spec, kbText, searchMode);
    if (!spec) { flash("Type something to search for, or set a filter."); return; }
    closeSearch();
    nav.go(spec, wall.firstVisible());
    openSource(spec);
    refreshDockButtons();
  }
  function doSearchKey(id) {
    if (!searchPanel) return;
    if (id.startsWith("suggest:")) chooseCharacterSuggestion(Number(id.slice(8)));
    else if (id === "close") closeSearch();
    else if (id === "enter") runSearchNow();
    else if (id === "layer") { kbLayer = kbLayer === "abc" ? "sym" : "abc"; drawSearchNow(searchHover); }
    else { kbText = typeKey(kbText, id); drawSearchNow(searchHover); queueCharacterSuggestions(); }
  }

  function loadVariantThumb(cover) {
    if (!cover || !cover.path) return null;
    const query = new URLSearchParams({ root: cover.root || "", path: cover.path, v: String(Number(cover.mtime) || 0) });
    const url = `/api/media/thumbnail?${query}`;
    let entry = variantThumbs.get(url);
    if (entry) return entry;
    if (typeof Image === "undefined") return null;
    const image = new Image();
    entry = { state: "loading", image: null };
    variantThumbs.set(url, entry);
    if (variantThumbs.size > 120) variantThumbs.delete(variantThumbs.keys().next().value);
    const generation = variantGeneration, pool = variantPool, page = variantPage, panel = filterPanel;
    image.onload = () => {
      entry.state = image.naturalWidth > 0 ? "ready" : "missing";
      if (entry.state === "ready") entry.image = image;
      if (!ended && generation === variantGeneration && variantPool === pool && variantPage === page && filterPanel === panel) drawFilterNow(filterHover);
    };
    image.onerror = () => {
      entry.state = "missing";
      if (!ended && generation === variantGeneration && variantPool === pool && variantPage === page && filterPanel === panel) drawFilterNow(filterHover);
    };
    image.src = url;
    return entry;
  }

  function drawVariantChoice(c, button, option, cover) {
    c.fillStyle = filterHover === button.id ? "rgba(120,165,255,0.98)" : (button.on ? "rgba(70,120,230,0.95)" : "rgba(52,58,78,0.95)");
    roundRect(c, button.x, button.y, button.w, button.h, 18); c.fill();
    const pad = Math.min(10, button.w * 0.06), previewW = Math.max(1, button.w - pad * 2);
    const previewH = Math.min(button.h * 0.70, Math.max(180, previewW * 2.0));
    const px = button.x + pad, py = button.y + pad;
    c.save(); roundRect(c, px, py, previewW, previewH, 12); c.clip();
    c.fillStyle = "rgba(10,12,18,0.88)"; c.fillRect(px, py, previewW, previewH);
    const entry = loadVariantThumb(cover);
    if (entry && entry.state === "ready" && entry.image) {
      const image = entry.image, scale = Math.max(previewW / image.naturalWidth, previewH / image.naturalHeight);
      const sw = previewW / scale, sh = previewH / scale;
      c.drawImage(image, (image.naturalWidth - sw) / 2, (image.naturalHeight - sh) / 2, sw, sh, px, py, previewW, previewH);
    } else {
      c.textAlign = "center"; c.textBaseline = "middle";
      c.fillStyle = "#9aa3bd"; c.font = `500 ${Math.max(20, Math.min(30, previewW * 0.16))}px ${FONT}`;
      c.fillText(entry && entry.state === "loading" ? "Loading preview…" : "No preview", px + previewW / 2, py + previewH / 2);
    }
    c.restore();

    const count = Number(option.count) || 0;
    const label = String(option.shortLabel || option.label || "Variant");
    const labelY = py + previewH + 12, availableH = Math.max(30, button.y + button.h - labelY - pad);
    const fontSize = Math.max(20, Math.min(34, button.w * 0.17));
    c.fillStyle = "#fff"; c.textAlign = "center"; c.textBaseline = "middle"; c.font = `600 ${fontSize}px ${FONT}`;
    const words = label.split(/\s+/), lines = []; let line = "";
    for (const word of words) {
      const next = line ? `${line} ${word}` : word;
      if (line && c.measureText(next).width > button.w - pad * 2) { lines.push(line); line = word; }
      else line = next;
    }
    if (line) lines.push(line);
    const maxLines = Math.min(2, Math.max(1, Math.floor((availableH * 0.66) / (fontSize * 1.18))));
    if (lines.length > maxLines) { lines.length = maxLines; lines[maxLines - 1] = fitText(c, `${lines[maxLines - 1]}…`, button.w - pad * 2); }
    const lineH = fontSize * 1.12, labelBlockH = lines.length * lineH;
    let textY = labelY + Math.max(0, (availableH - labelBlockH - fontSize * 0.85) / 2) + lineH / 2;
    for (const item of lines) { c.fillText(fitText(c, item, button.w - pad * 2), button.x + button.w / 2, textY); textY += lineH; }
    c.fillStyle = "#c5cbe0"; c.font = `500 ${Math.max(18, fontSize * 0.72)}px ${FONT}`;
    c.fillText(`${count.toLocaleString()} files`, button.x + button.w / 2, Math.min(button.y + button.h - pad - 12, textY + 2));
    return true;
  }

  function drawFilterNow(hover) {
    if (!filterPanel) return;
    filterHover = hover;
    if (variantPool) {
      const variants = Array.isArray(variantPool.variants) ? variantPool.variants : [];
      const pageSize = 10, pageCount = Math.max(1, Math.ceil(variants.length / pageSize));
      variantPage = Math.min(variantPage, pageCount - 1);
      const options = [{ id: "all", label: `All versions (${Number(variantPool.count || 0).toLocaleString()})`, shortLabel: "All versions", count: variantPool.count, cover: variantPool.cover, on: !variantPool.variant }];
      const visible = variants.slice(variantPage * pageSize, (variantPage + 1) * pageSize);
      for (const v of visible) options.push({ id: `v:${v.tag}`, label: `${v.label} (${Number(v.count || 0).toLocaleString()})`, shortLabel: v.label, count: v.count, cover: v.cover, on: variantPool.variant === v.tag });
      const optionById = new Map(options.map((option) => [`variant:${option.id}`, option]));
      const rows = [{ id: "variant", label: variantPool.label || "Character", options }];
      if (pageCount > 1) rows.push({ id: "variantPage", label: `${variantPage + 1} / ${pageCount}`, options: [
        { id: "prev", label: "Previous", on: false }, { id: "next", label: "Next", on: false },
      ] });
      const lay = filterLayout(rows, filterPanel.w, filterPanel.h);
      filterPanel.layoutButtons = lay;
      filterPanel.redraw((c, w, h) => drawSettings(c, w, h, { layout: lay, rows, hover, title: "Choose a character version",
        drawButton: (c2, button) => {
          if (!button.id.startsWith("variant:")) return false;
          const option = optionById.get(button.id);
          if (!option) return false;
          return drawVariantChoice(c2, button, option, option.cover);
        },
      }));
      return;
    }
    const src = currentSource();
    const spec = src ? src.spec : { kind: "folder", filters: "" };
    const rows = filterRows(spec, filterOptions, { mode: searchMode, sortable: !!src && spec.kind === "folder" && !src.rootListing });
    const pageCount = Math.max(1, Math.ceil(rows.length / FILTER_PAGE_ROWS));
    filterPage = Math.min(filterPage, pageCount - 1);
    const visibleRows = rows.slice(filterPage * FILTER_PAGE_ROWS, (filterPage + 1) * FILTER_PAGE_ROWS);
    const lay = filterLayout(visibleRows, filterPanel.w, filterPanel.h);
    const navButtons = [];
    if (filterPage > 0) navButtons.push({ id: "page:prev", label: `Prev ${filterPage}/${pageCount}`, x: filterPanel.w - 480, y: 22, w: 210, h: 120 });
    if (filterPage < pageCount - 1) navButtons.push({ id: "page:next", label: `Next ${filterPage + 2}/${pageCount}`, x: filterPanel.w - 250, y: 22, w: 220, h: 120 });
    filterPanel.layoutButtons = [...lay, ...navButtons];
    filterPanel.redraw((c, w, h) => {
      drawSettings(c, w, h, { layout: lay, rows: visibleRows, hover, title: pageCount > 1 ? `Filters · page ${filterPage + 1} of ${pageCount}` : "Filters" });
      for (const b of navButtons) {
        c.fillStyle = hover === b.id ? "rgba(120,165,255,0.98)" : "rgba(52,58,78,0.95)";
        roundRect(c, b.x, b.y, b.w, b.h, 18); c.fill();
        c.fillStyle = "#fff"; c.textAlign = "center"; c.textBaseline = "middle"; c.font = `600 42px ${FONT}`;
        c.fillText(fitText(c, b.label, b.w - 20), b.x + b.w / 2, b.y + b.h / 2);
      }
    });
  }
  function openFilters() {
    if (bare || !wall || viewerOpen() || filterPanel) return;
    closeSearch(); closeSettings(); filterPage = 0;
    filterPanel = mkPanel(FP, FP.y, FP.z);
    drawFilterNow(null);
    if (!optionsAsked) {
      optionsAsked = true;
      getJson("/api/media/filters").then((d) => { if (ended) return; filterOptions = normalizeOptions(d); syncOverlays(); }).catch(() => {});
    }
  }
  function closeFilters() {
    if (!filterPanel) return;
    variantGeneration += 1;
    filterPanel.dispose();
    filterPanel = null; filterHover = null; variantPool = null; variantPage = 0;
  }

  function openVariantPicker(item) {
    const pool = item && item.source;
    if (!pool || !pool.poolTag || !Array.isArray(pool.variants) || pool.variants.length < 2) return false;
    closeSearch(); closeSettings(); closeFilters();
    variantGeneration += 1;
    variantPool = pool; variantPage = 0;
    filterPanel = mkPanel(FP, FP.y, FP.z);
    drawFilterNow(null);
    return true;
  }

  function drawChipsNow(hover) {
    if (!chipsPanel) return;
    chipsHover = hover;
    chipLay = chipsLayout(chipList, chipsPanel.ctx, chipsPanel.w, chipsPanel.h);
    chipsPanel.layoutButtons = chipLay;
    chipsPanel.redraw((c, w, h) => drawChips(c, w, h, { layout: chipLay, hover }));
  }
  // Keeps the chip strip and the filter panel in step with the current wall's spec.
  function syncOverlays() {
    if (bare || ended) return;
    const src = currentSource();
    if (src && (src.spec.kind === "search" || src.spec.kind === "favorites") && src.spec.mode) searchMode = src.spec.mode;
    const chips = src && !viewerOpen() ? chipsFor(src.spec, filterOptions) : [];
    if (!chips.length) {
      if (chipsPanel) { chipsPanel.dispose(); chipsPanel = null; chipLay = []; chipKey = ""; chipsHover = null; }
    } else {
      if (!chipsPanel) {
        chipsPanel = createPanel({ widthM: CP.w, heightM: CP.h, pxPerM: CP.px, opacity: 1 });
        const dp = dockPose();
        const a = (DOCK.dropDeg * Math.PI) / 180, off = DOCK.height / 2 + CP.h / 2 + 0.02;
        chipsPanel.mesh.position.set(dp.x, dp.y + off * Math.cos(a), dp.z - off * Math.sin(a));
        chipsPanel.mesh.rotation.x = dp.tiltX;
        rig.add(chipsPanel.mesh);
        chipKey = "";
      }
      const key = chips.map((c) => `${c.id}=${c.label}`).join("|");
      if (key !== chipKey) { chipKey = key; chipList = chips; drawChipsNow(chipsHover); }
    }
    if (filterPanel) drawFilterNow(filterHover);
  }
  function replaceTop(spec) {
    const old = sources.pop();
    if (old) old.dispose();
    nav.top.spec = spec;
    openSource(spec);
  }
  // Every change of a constraint goes through here: the wall's spec is planned anew (search.js planEdit).
  function applyEdit(edit) {
    const src = currentSource();
    if (!src || !wall || viewerOpen()) return;
    if (edit.type === "mode") searchMode = edit.value;
    const r = planEdit(src.spec, edit, { rootListing: src.rootListing });
    if (r.op === "replace") replaceTop(r.spec);
    else if (r.op === "push") { nav.go(r.spec, wall.firstVisible()); openSource(r.spec); }
    else if (r.op === "mutate") { src.spec.filters = r.spec.filters; syncOverlays(); }
    else if (r.op === "origin") { if (nav.depth > 1) goBackNow(); else replaceTop(folderSpec("", "")); }
    else { if (r.note) flash(r.note); syncOverlays(); }
    refreshDockButtons();
  }
  function doFilter(id) {
    if (!filterPanel) return;
    if (id === "close") { closeFilters(); return; }
    if (id === "page:prev") { filterPage = Math.max(0, filterPage - 1); drawFilterNow(null); return; }
    if (id === "page:next") { filterPage += 1; drawFilterNow(null); return; }
    if (variantPool) {
      if (id === "variantPage:prev") { variantPage = Math.max(0, variantPage - 1); drawFilterNow(null); return; }
      if (id === "variantPage:next") { variantPage = Math.min(Math.ceil(variantPool.variants.length / 10) - 1, variantPage + 1); drawFilterNow(null); return; }
      if (id === "clearall:") id = "variant:all";
      if (id.startsWith("variant:")) {
        const key = id.slice("variant:".length);
        const variantTag = key === "all" ? "" : key.startsWith("v:") ? key.slice(2) : "";
        const spec = characterSource(variantPool, variantTag, currentSource()?.spec || {});
        if (spec) { closeFilters(); nav.go(spec, wall ? wall.firstVisible() : 0); openSource(spec); refreshDockButtons(); }
        return;
      }
      return;
    }
    const e = filterEdit(id, filterOptions);
    if (e) applyEdit(e);
  }
  function doChip(id) {
    const e = chipEdit(id);
    if (e) applyEdit(e);
  }

  // ---- selecting
  function selectTile(i) {
    if (!wall || i < 0 || i >= wall.items.length) return;
    const it = wall.items[i];
    closeSearch(); closeFilters();
    if (it.type === "folder" || it.type === "subject") {
      if (it.type === "subject" && it.subjectKind === "characters" && openVariantPicker(it)) return;
      const base = currentSource() ? currentSource().spec : {};
      const s = it.source ? { ...it.source, filters: base.filters || it.source.filters || "" } : it.special === "favorites" ? favouritesSpec(base) : folderSpec(it.root, it.path, base);
      nav.go(s, wall.firstVisible());
      openSource(s);
      refreshDockButtons();
    } else {
      openViewerItem(itemCtxOf(it));
    }
  }
  function goBack() {
    if (searchPanel || filterPanel) { closeSearch(); closeFilters(); return; }       // Back closes an open panel first
    goBackNow();
  }
  function goBackNow() {
    const r = nav.back();
    if (r.type === "viewer") { closeViewer(); return; }
    if (r.type === "wall") {
      const dead = sources.pop();
      if (dead) dead.dispose();
      showSource(currentSource(), r.index);
      refreshDockButtons();
      return;
    }
    host.end();
  }
  // The left d-pad's up: undoes the last Back. A viewer is the deepest place, so nothing lies ahead of one.
  function goForward() {
    if (searchPanel || filterPanel || viewerOpen()) return;
    const f = nav.forward(wall ? wall.firstVisible() : 0);
    if (!f) { flash("Nothing to go forward to."); return; }
    if (f.type === "viewer") { openViewerItem(f.item); return; }
    openSource(f.spec, { index: f.index });
    refreshDockButtons();
  }
  function doButton(id) {
    switch (id) {
      case "exit": host.end(); break;
      case "recenter": doRecenter(); break;
      case "hud": hudOn = !hudOn; hud.mesh.visible = hudOn; break;
      case "back": goBack(); break;
      case "home": {
        closeSearch(); closeFilters();
        const s = { ...folderSpec("", ""), kind: "home" };
        nav.go(s, wall ? wall.firstVisible() : 0); openSource(s); refreshDockButtons(); break;
      }
      case "top": if (wall) wall.scroll.top(); break;
      case "search": if (searchPanel) closeSearch(); else openSearch(); break;
      case "filters": if (filterPanel) closeFilters(); else openFilters(); break;
      case "settings":
        closeSearch(); closeFilters();
        settingsOpen = !settingsOpen;
        settingsPanel.mesh.visible = settingsOpen;
        drawSettingsNow(null);
        break;
      case "prev": step(-1); break;
      case "next": step(1); break;
      case "favorite": toggleFavorite(); break;
      case "switch3d": switchTo3d(); break;
      default: break;
    }
  }
  function doScale(id) {
    if (id === "minus" || id === "plus") { setScreenScale(stepScale(settings.screenScale, id === "minus" ? -1 : 1)); persistScale(); }
  }
  function doSetting(id) {
    if (id === "close") { settingsOpen = false; settingsPanel.mesh.visible = false; return; }
    const next = applyChoice(settings, id);
    if (JSON.stringify(next) === JSON.stringify(settings)) return;
    const prev = settings;
    settings = next;
    if (screen && screen.setEveryFrame) screen.setEveryFrame(settings.videoEveryFrame);
    if (settings.hiRate && !prev.hiRate) askRate();
    else if (!settings.hiRate) rateReq = { state: "off", rate: 0, error: "" };
    if (settingsStorage) saveSettings(settingsStorage, settings);
    wall.setSettings(settings);
    if (screen && screen.setScale) screen.setScale(settings.screenScale / 100);
    scaleDirty = false;
    drawScaleNow();
    drawSettingsNow(settingsHover);
  }

  // ---- recenter (A/X): the furniture, the wall and the screen go in front of the head again
  const doRecenter = () => {
    const h = host.head();
    const pose = screen ? screen.recenter(h.pos, h.fwd) : recenterPose(h.pos, h.fwd, DEFAULT_DISTANCE, yaw);
    yaw = pose.yaw;
    rigPose = pose;
    rig.position.set(pose.x, pose.y, pose.z);
    rig.rotation.set(0, pose.yaw, 0);
    rig.updateMatrixWorld(true);
  };
  rig.updateMatrixWorld(true);

  // ---- the first view: where the context said we start
  if (!bare) {
    const savedSpec = plan.spec;
    if (savedSpec && savedSpec.poolTag && (!Array.isArray(savedSpec.variants) || !savedSpec.variants.length)) {
      const pool = await loadCharacterPool(getJson, savedSpec.poolTag);
      if (pool && !ended) {
        const hydrated = characterSource(pool, savedSpec.variant || "", savedSpec);
        if (hydrated) { plan.spec = hydrated; nav.top.spec = hydrated; }
      }
    }
    const landItem = plan.view === "item" ? plan.item : null;
    openSource(plan.spec, landItem ? { landItem } : { index: plan.scrollIndex });
    if (landItem) {
      viewerItem = landItem;
      nav.openViewer(landItem);
      wall.setDim(0.3);
      refreshDockButtons();
      if (auto) auto.open(landItem);
    }
  }

  // ---- input
  const backHold = createBackHold(3);
  const bareExit = createBackHold(3);   // bare viewer: no level to go back to, B/Y exits only after a 3 s hold
  const trig = new Map();            // hand id -> { down, type, id, tile } trigger state for select-on-release
  const edges = new Map();           // hand id -> { a, stick } previous button states
  const dpadEdge = new Map();        // hand id -> previous d-pad left/right (-1, 0, +1)
  let dpadBound = false;             // left/right seen: the d-pad binding is installed, up is only Forward
  const noScreenInput = bare ? createNoScreenInput() : null;
  const invRig = new THREE.Matrix4();
  const ro = new THREE.Vector3(), rd = new THREE.Vector3();
  const panelHit = createPanelHit();
  const uv = { u: 0, v: 0 };
  const targets = [{ type: "", id: "", tile: -1, dist: 0, elev: 0 }, { type: "", id: "", tile: -1, dist: 0, elev: 0 }];
  // the slider value under a ray, plane-intersected without the panel bounds so a drag keeps working off the bar (no allocation)
  function scaleAt(hand) {
    const t = panelHit(scalePanel.mesh, SS.w, SS.h, hand.origin, hand.dir, uv, true);
    if (!(t > 0)) return -1;
    const tr = scaleLay.track, pad = scalePanel.h * 0.2;
    return scaleFromFraction((uv.u * scalePanel.w - tr.x - pad) / (tr.w - 2 * pad));
  }
  let hudAt = -1e9, lastDist = startDist, subAt = -1e9;
  let gripHand = null;

  // A ray against one of the overlay panels (keyboard, filters, chips): the nearer hit wins; out gets the button under it.
  function hitPanel(p, wM, hM, type, hand, best, out) {
    if (!p.layoutButtons) return best;
    const t = panelHit(p.mesh, wM, hM, hand.origin, hand.dir, uv);
    if (!(t > 0 && t < best)) return best;
    out.dist = t; out.type = ""; out.id = "";
    const id = hitButton(p.layoutButtons, uv.u * p.w, uv.v * p.h);
    if (id) { out.type = type; out.id = id; }
    return t;
  }

  function aim(hand, out) {
    out.type = ""; out.id = ""; out.tile = -1; out.dist = 0; out.elev = 0;
    if (!hand.hasRay) return out;
    let best = 1e9;
    if (dock) {
      const t = panelHit(dock.mesh, DOCK.width, DOCK.height, hand.origin, hand.dir, uv);
      if (t > 0) {
        best = t; out.dist = t;
        const id = hitButton(layout, uv.u * dock.w, uv.v * dock.h);
        if (id) { out.type = "dock"; out.id = id; }
      }
    }
    if (settingsOpen && settingsPanel && settingsPanel.layoutButtons) {
      const t = panelHit(settingsPanel.mesh, SETTINGS_W, SETTINGS_H, hand.origin, hand.dir, uv);
      if (t > 0 && t < best) {
        best = t; out.dist = t; out.type = ""; out.id = "";
        const id = hitButton(settingsPanel.layoutButtons, uv.u * settingsPanel.w, uv.v * settingsPanel.h);
        if (id) { out.type = "settings"; out.id = id; }
      }
    }
    if (searchPanel) best = hitPanel(searchPanel, KB.w, KB.h, "search", hand, best, out);
    if (filterPanel) best = hitPanel(filterPanel, FP.w, FP.h, "filters", hand, best, out);
    if (chipsPanel) best = hitPanel(chipsPanel, CP.w, CP.h, "chip", hand, best, out);
    if (scalePanel && scalePanel.mesh.visible) best = hitPanel(scalePanel, SS.w, SS.h, "scale", hand, best, out);
    if (wall) {
      ro.copy(hand.origin).applyMatrix4(invRig);
      rd.copy(hand.dir).transformDirection(invRig);
      out.elev = (Math.atan2(rd.y, Math.hypot(rd.x, rd.z)) * 180) / Math.PI;
      if (!viewerOpen()) {
        const i = wall.pick(ro.x, ro.y, ro.z, rd.x, rd.y, rd.z);
        if (wall.hitDist > 0 && wall.hitDist < best) {
          out.dist = wall.hitDist;
          if (i >= 0) { out.type = "tile"; out.tile = i; out.id = ""; }
        }
      }
    }
    return out;
  }

  const hit = new THREE.Vector3();
  // SteamVR's local origin can be metres from the headset at session start.
  // Wait for a tracked pose, then put the room and any initial screen in front of it.
  let initialRecenter = host.mode === "xr";
  host.run((dt, t) => {
    if (initialRecenter && host.hasViewerPose()) {
      doRecenter();
      initialRecenter = false;
    }
    const hands = host.hands();
    if (bare) for (let i = 0; i < hands.length; i += 1) hands[i].left = false;      // no dock: X and Y must recenter and exit
    for (let i = 0; i < hands.length; i += 1) {
      const d = hands[i].dpad || 0;
      if (d && d !== (dpadEdge.get(hands[i].id) || 0)) { dpadBound = true; if (bare || viewerOpen()) step(d); }
      dpadEdge.set(hands[i].id, d);
    }
    let hovering = false;
    let newHover = null, newScaleHover = null, newSettingsHover = null, hoverTile = -1, newSearchHover = null, newFilterHover = null, newChipHover = null;
    if (!bare) { rig.updateMatrixWorld(); invRig.copy(rig.matrixWorld).invert(); }
    let stickY = 0;
    for (let i = 0; i < hands.length; i += 1) {
      const hnd = hands[i];
      const hv = bare ? targets[0] : aim(hnd, targets[i < 2 ? i : 1]);
      if (hv.type === "dock") { hovering = true; newHover = hv.id; }
      else if (hv.type === "settings") { hovering = true; newSettingsHover = hv.id; }
      else if (hv.type === "search") { hovering = true; newSearchHover = hv.id; }
      else if (hv.type === "filters") { hovering = true; newFilterHover = hv.id; }
      else if (hv.type === "chip") { hovering = true; newChipHover = hv.id; }
      else if (hv.type === "scale") { hovering = true; newScaleHover = hv.id; }
      else if (hv.type === "tile") hoverTile = hv.tile;
      const line = rays[i];
      if (line) {
        line.visible = !!hnd.hasRay && hnd.dir.lengthSq() > 0;
        if (line.visible) {
          const len = hv.dist > 0 ? hv.dist : 3;
          const pos = line.geometry.attributes.position;
          pos.setXYZ(0, hnd.origin.x, hnd.origin.y, hnd.origin.z);
          hit.copy(hnd.origin).addScaledVector(hnd.dir, len);
          pos.setXYZ(1, hit.x, hit.y, hit.z);
          pos.needsUpdate = true;
          line.material.color.setHex(hv.type ? 0xffffff : 0x9db8ff);
        }
      }
      // trigger: select on release over the thing the press started on
      let st = trig.get(hnd.id);
      if (!st) { st = { down: false, type: "", id: "", tile: -1, drag: false }; trig.set(hnd.id, st); }
      const down = !!hnd.buttons[0];
      if (down && !st.down) {
        st.type = hv.type; st.id = hv.id; st.tile = hv.tile;
        if (hv.type === "scale" && hv.id === "track" && scalePanel) { st.drag = true; scaleDrag = true; const p = scaleAt(hnd); if (p > 0) setScreenScale(p); }
      }
      if (st.drag) {
        hovering = true;                                   // a slider drag never toggles play/pause
        if (down) { const p = scaleAt(hnd); if (p > 0) setScreenScale(p); }
        else { st.drag = false; scaleDrag = false; persistScale(); drawScaleNow(); }
      }
      if (!down && st.down && st.type && st.type === hv.type && st.id === hv.id && st.tile === hv.tile) {
        if (st.type === "dock") doButton(st.id);
        else if (st.type === "settings") doSetting(st.id);
        else if (st.type === "search") doSearchKey(st.id);
        else if (st.type === "filters") doFilter(st.id);
        else if (st.type === "chip") doChip(st.id);
        else if (st.type === "scale") doScale(st.id);
        else if (st.type === "tile") selectTile(st.tile);
      }
      st.down = down;
      if (!down) { st.type = ""; st.drag = false; }

      if (!bare) {
        let ed = edges.get(hnd.id);
        if (!ed) { ed = { a: false, stick: false, fwd: false }; edges.set(hnd.id, ed); }
        const a = !!hnd.buttons[4], sp = !!hnd.buttons[3];
        // Left hand on the Steam Frame: SteamVR sends d-pad up, left and right all as Y and down as X (Back below).
        // Y is Forward on a wall and Next in the viewer. The three directions cannot be told apart without a binding.
        const fwd = !!(hnd.left && hnd.buttons[5]);
        if (a && !ed.a && !hnd.left && !screen) doRecenter();
        if (fwd && !ed.fwd) { if (!viewerOpen()) goForward(); else if (!dpadBound) step(1); }
        if (sp && !ed.stick && !viewerOpen() && wall) wall.scroll.top();
        ed.a = a; ed.stick = sp; ed.fwd = fwd;
        // grip drags the wall (not while the viewer owns the grips)
        const grip = !!hnd.buttons[1];
        if (wall && !viewerOpen()) {
          if (grip && !gripHand && hv.type !== "dock") { gripHand = hnd.id; wall.scroll.dragStart(hv.elev); }
          else if (grip && gripHand === hnd.id) wall.scroll.drag(hv.elev, dt);
        }
        if (!grip && gripHand === hnd.id) { gripHand = null; wall.scroll.dragEnd(); }
        const ax = hnd.axes;
        const sy = ax.length >= 4 ? ax[3] : (ax[1] || 0);
        if (Math.abs(sy) > Math.abs(stickY)) stickY = sy;
      }
    }
    if (hands.length === 0 && gripHand && wall) { gripHand = null; wall.scroll.dragEnd(); }
    if (newHover !== dockHover) { dockHover = newHover; drawDockNow(dockHover); }
    if (settingsOpen && newSettingsHover !== settingsHover) drawSettingsNow(newSettingsHover);
    if (searchPanel && newSearchHover !== searchHover) drawSearchNow(newSearchHover);
    if (filterPanel && newFilterHover !== filterHover) drawFilterNow(newFilterHover);
    if (chipsPanel && newChipHover !== chipsHover) drawChipsNow(newChipHover);
    if (scalePanel && scalePanel.mesh.visible && !scaleDrag && newScaleHover !== scaleHover) { scaleHover = newScaleHover; drawScaleNow(); }

    if (!bare) {
      wall.setHover(viewerOpen() ? -1 : hoverTile);
      wall.update(dt, t, viewerOpen() || gripHand ? 0 : (host.mode === "preview" ? -stickY : stickY));   // a wheel notch down = further down the list
      if (!viewerOpen() && wall.nearEnd()) {
        const s = currentSource();
        if (s && s.hasMore && !s.loading && !s.error) pumpMore(s, openSeq.n);
      }
      // the dock's count follows the scroll: redraw only when the text changed
      if (t - subAt > 250) {
        subAt = t;
        if (titleText() !== titleCache || subtitleText() !== subCache) drawDockNow(dockHover);
      }
      // B/Y: a tap is Back, a hold of three seconds is Exit (owner: at least 3 s)
      let bDown = false;
      for (let i = 0; i < hands.length; i += 1) if (hands[i].buttons[hands[i].left ? 4 : 5]) bDown = true;
      const bAct = backHold.update(bDown, dt);
      if (bAct === "exit") host.end();
      else if (bAct === "back") goBack();
    }

    if (bare) {
      let bDown = false;
      for (let i = 0; i < hands.length; i += 1) if (hands[i].buttons[5]) bDown = true;
      if (bareExit.update(bDown, dt) === "exit") host.end();
    }
    const pass = screen && screen.mounted
      ? screen.update({ dt, hands, suppressToggle: hovering })
      : (noScreenInput ? noScreenInput.update({ dt, hands }) : []);
    if (screen) {
      lastDist = screen.dist;
      // the distance: remembered once still for 600 ms - never a write per frame
      liveDist = screen.dist;
      if (liveDist !== savedDist) { if (viewChangedAt < 0) viewChangedAt = t; else if (t - viewChangedAt > 600) persistView(); }
      else viewChangedAt = -1;
    }
    for (const a of pass) {
      if (a.type === "recenter") doRecenter();
      else if (a.type === "next") step(1);
    }
    if (hud && hudOn && t - hudAt > 250) {
      hudAt = t;
      const budget = (() => { const fr = host.xrInfo().frameRate; return 1000 / (fr > 0 ? fr : 120) + 0.2; })();
      const info = Object.assign({}, host.xrInfo(), host.info(), wall ? { wall: wall.stats() } : {}, { video: videoInfo(), decoders, rateRequest: rateReq });
      hud.redraw((c, w, h) => drawLines(c, w, h, hudLines(host.stats.summary(budget), info), { title: "Performance" }));
    }
  });

  // V2c: the highest-refresh-rate request (XR only; the preview has no session). State shown on the HUD's frame-rate line.
  let rateReq = host.session ? { state: settings.hiRate ? "pending" : "off", rate: 0, error: "" } : null;
  const askRate = () => {
    if (!host.session) return;
    rateReq = { state: "pending", rate: 0, error: "" };
    requestHighestRate(host.session).then((r) => { if (settings.hiRate) rateReq = r; });
  };
  if (host.session && settings.hiRate) askRate();

  // V2c: the decoder table, probed once per session (async; the HUD shows "..." until the answers land).
  const decoders = new DecoderReport(typeof navigator !== "undefined" ? navigator : null, (t) => document.createElement("video").canPlayType(t));

  // V2b: the playing video's numbers for the HUD (null when no video is on the screen).
  const videoInfo = () => {
    const vs = screen && screen.videoStats;
    if (!vs) return null;
    const now = performance.now();
    vs.sample(now);
    return vs.read(now, host.xrInfo().frameRate);
  };

  // What to show next time: the open item (if any) and the wall it sits on, scrolled where it was.
  const currentContext = () => {
    const s = currentSource();
    const base = s ? { ...s.spec, offset: wall ? wall.firstVisible() : 0 } : context1.source;
    if (viewerItem) return normalizeContext({ view: "item", item: viewerItem, source: base });
    return normalizeContext({ view: "wall", source: base });
  };
  host.ended.then((r) => {
    if (opts.noPersist || !storage) return;
    try { context1 = bare ? context1 : currentContext(); } catch (e) { /* keep the last */ }
    saveVrState(storage, { context: context1, dist: lastDist, hud: hudOn });
    D("state saved", r && r.textures);
  });

  if (!early && ctx.view === "item" && ctx.item) {
    // no early screen (the renderer was not known in time): create it now
    const d = itemDisplay(ctx.item);
    if (d) showDisplay(d, null, false);
  }

  const handle = {
    ended: host.ended,
    end: () => host.end(),
    host,
    get context() { return bare ? context1 : currentContext(); },
    get key() { return screen ? screen.key : null; },
    get zoom() { return screen ? screen.zoom : 1; },
    get screen() { return screen; },
    get wall() { return wall; },
    get nav() { return nav; },
    get settings() { return settings; },
    get auto() { return auto; },
    // Accepts a display ({mode, url, kind, eyeW, eyeH, key}) or a library item ({root, path, status...}).
    show(x) {
      if (!x) return Promise.resolve(false);
      const isItem = typeof x.path === "string";
      const d = isItem ? itemDisplay(x) : { mode: x.mode || "sbs", url: x.url, kind: x.kind, eyeW: x.eyeW || 0, eyeH: x.eyeH || 0, key: x.key, name: x.name || "" };
      if (!d || (screen && d.key === screen.key)) return Promise.resolve(false);
      return showDisplay(d, isItem ? normalizeContext({ item: x }).item : null, false).catch(() => { status("Could not load that item."); return false; });
    },
    // Test and console hooks: the same actions the controller triggers.
    select(i) { selectTile(i); },
    back() { goBack(); },
    forward() { goForward(); },
    next() { return step(1); },
    prev() { return step(-1); },
    press(id) { doButton(id); },
    choose(id) { doSetting(id); },
    scaleStep(dir) { doScale(dir < 0 ? "minus" : "plus"); },
    setScale(pct) { setScreenScale(pct); persistScale(); },
    flushView() { persistScale(); persistView(); },
    searchKey(id) { doSearchKey(id); },
    setSearchText(t) { kbText = pasteText("", t); if (searchPanel) { drawSearchNow(searchHover); queueCharacterSuggestions(); } },
    selectSuggestion(i) { chooseCharacterSuggestion(i); },
    filter(id) { doFilter(id); },
    chip(id) { doChip(id); },
    switch3d() { return switchTo3d(); },
    // A read-only look at what was built (the test harness; also handy in the console).
    snapshot() {
      const hudSum = host.stats.summary();
      const s = currentSource();
      return {
        mode: host.mode,
        view: viewerOpen() || bare ? "item" : "wall",
        title: titleText(),
        subtitle: subtitleText(),
        message,
        buttons: dockBtns.map((b) => b.id),
        hover: dockHover,
        hudVisible: !!(hud && hud.mesh.visible),
        hudLines: hudLines(hudSum, Object.assign({}, host.xrInfo(), host.info(), wall ? { wall: wall.stats() } : {}, { video: videoInfo(), decoders, rateRequest: rateReq })),
        parts: { dock: !!dock, hud: !!hud, wall: !!wall, rays: rays.length },
        screen: screen ? { mode: screen.mode, kind: screen.kind, key: screen.key, url: screen.requestedUrl, failed: screen.failed } : null,
        info: host.info(),
        frames: hudSum.n,
        recenters: rigPose,
        source: s ? { kind: s.spec.kind, root: s.spec.root, path: s.spec.path, count: s.items.length, hasMore: s.hasMore, loading: s.loading, error: s.error, total: s.total } : null,
        depth: nav.depth,
        viewer: nav.viewer,
        ready3d: !!ready3d,
        autoNote,
        settings,
        screenScale: settings.screenScale,
        wallVisible: wall ? wall.group.visible : null,
        scaleSlider: scalePanel ? { visible: scalePanel.mesh.visible, label: scaleLabel(settings.screenScale), frac: fractionFromScale(settings.screenScale) } : null,
        settingsOpen,
        search: { open: !!searchPanel, text: kbText, layer: kbLayer, mode: searchMode,
          suggestions: characterSuggestions.map((x) => ({ tag: x.tag, label: x.label })) },
        filters: { open: !!filterPanel, options: filterOptions },
        chips: (() => { const cs = currentSource(); return !bare && cs && !viewerOpen() ? chipsFor(cs.spec, filterOptions).map((c) => c.label) : []; })(),
        chipIds: chipList.map((c) => c.id),
        spec: s ? s.spec : null,
        wall: wall ? wall.stats() : null,
      };
    },
    // Client coordinates of a dock button in the preview canvas (null in a headset session).
    projectButton(id) {
      if (host.mode !== "preview" || !dock) return null;
      const b = layout.find((l) => l.id === id);
      if (!b) return null;
      dock.mesh.updateWorldMatrix(true, false);
      const px = (b.x + b.w / 2) / dock.w - 0.5, py = 0.5 - (b.y + b.h / 2) / dock.h;
      const v = new THREE.Vector3(px * DOCK.width, py * DOCK.height, 0).applyMatrix4(dock.mesh.matrixWorld).project(host.camera);
      const r = host.canvas.getBoundingClientRect();
      return { clientX: r.left + ((v.x + 1) / 2) * r.width, clientY: r.top + ((1 - v.y) / 2) * r.height };
    },
    // Client coordinates of a button on the keyboard / filter / chip panel in the preview canvas.
    projectPanel(name, id) {
      const p = name === "search" ? searchPanel : (name === "filters" ? filterPanel : (name === "chips" ? chipsPanel : null));
      const d = name === "search" ? KB : (name === "filters" ? FP : CP);
      if (host.mode !== "preview" || !p || !p.layoutButtons) return null;
      const b = p.layoutButtons.find((l) => l.id === id);
      if (!b) return null;
      p.mesh.updateWorldMatrix(true, false);
      const v = new THREE.Vector3(((b.x + b.w / 2) / p.w - 0.5) * d.w, (0.5 - (b.y + b.h / 2) / p.h) * d.h, 0).applyMatrix4(p.mesh.matrixWorld).project(host.camera);
      const r = host.canvas.getBoundingClientRect();
      return { clientX: r.left + ((v.x + 1) / 2) * r.width, clientY: r.top + ((1 - v.y) / 2) * r.height };
    },
    // Client coordinates of the slider's "minus" / "plus" / a point on the track (frac 0..1) in the preview canvas.
    projectScale(id, frac = 0.5) {
      if (host.mode !== "preview" || !scalePanel || !scalePanel.mesh.visible) return null;
      const b = scaleLay.buttons.find((l) => l.id === id);
      if (!b) return null;
      const tr = scaleLay.track, pad = scalePanel.h * 0.2;
      const px = id === "track" ? tr.x + pad + frac * (tr.w - 2 * pad) : b.x + b.w / 2;
      scalePanel.mesh.updateWorldMatrix(true, false);
      const v = new THREE.Vector3((px / scalePanel.w - 0.5) * SS.w, 0, 0).applyMatrix4(scalePanel.mesh.matrixWorld).project(host.camera);
      const r = host.canvas.getBoundingClientRect();
      return { clientX: r.left + ((v.x + 1) / 2) * r.width, clientY: r.top + ((1 - v.y) / 2) * r.height };
    },
    // Client coordinates of a wall tile's centre in the preview canvas (null when it has no mesh).
    projectTile(i) {
      if (host.mode !== "preview" || !wall) return null;
      const m = wall.meshOf(i);
      if (!m) return null;
      m.updateWorldMatrix(true, false);
      const v = new THREE.Vector3().setFromMatrixPosition(m.matrixWorld).project(host.camera);
      const r = host.canvas.getBoundingClientRect();
      return { clientX: r.left + ((v.x + 1) / 2) * r.width, clientY: r.top + ((1 - v.y) / 2) * r.height, behind: v.z > 1 };
    },
  };
  return handle;
}

// Controller mapping when no item is on screen in a bare session: only recenter / exit / next are meaningful.
function createNoScreenInput() {
  const input = createController({ dist: DEFAULT_DISTANCE });
  return {
    update({ dt, hands }) {
      return input.update({ dt, kind: "picture", hands }).filter((a) => a.type === "recenter" || a.type === "exit" || a.type === "next");
    },
  };
}
