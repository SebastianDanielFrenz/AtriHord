/* The curved tile wall (P8 V1): the three.js side. Layout, scrolling and the loader are the pure modules next to
 * it (layout.js, scroll.js, loader.js); this file turns them into meshes.
 *
 *   - virtualised: layout covers every tile (aspect numbers only), but meshes exist only for the rows in the
 *     scroll window plus a margin, from a recycled pool; a tile that scrolls away gives its mesh back
 *   - one opaque mesh per tile on a shared unit plane (position/rotation/scale set per tile), so no sorting;
 *     placeholders (flat colour + glyph) until the thumbnail arrives, then a brightness fade-in
 *   - thumbnails through the loader (nearest first, 6 at a time, <= 3 uploads per frame, texture LRU)
 *   - folder tiles draw their name onto a card (always visible); a file's caption shows under the hovered tile
 *   - the hover test is analytic (ray against the cylinder, then layout.hitTile) - no Raycaster, no allocation
 *   - nothing here allocates per frame in its hot path (preallocated vectors, arrays reused)
 * The wall is a child of the app's rig, centred on the origin (the head position at the last recenter). */
import * as THREE from "../vendor/three.module.min.js";
import {
  WALL, layoutRows, visibleRows, tilePlacement, hitTile, rayCylinder, maxOffset, offsetForTile, firstVisibleTile,
  bandDeg, rowElevation, tileAspect,
} from "./layout.js";
import { createScroll } from "./scroll.js";
import { createThumbLoader } from "./loader.js";
import { createPanel, drawFolderCard, drawPlaceholder, drawBadge, drawCaption } from "./ui.js";
import { textScale } from "./settings.js";

const MARGIN_ROWS = 3;
const CARD_W = 512, CARD_H = 320;
const FOLDER_CACHE = 150;
const FADE_S = 0.12;
const HOVER_LIFT = 1.08;
const CAPTION_DELAY_MS = 300;
const CAP_W_M = 0.9, CAP_H_M = 0.12, CAP_PX = 1400;

const clamp01 = (v) => (v < 0 ? 0 : (v > 1 ? 1 : v));

function fmtDuration(s) {
  if (!(s > 0)) return "";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
}

function realLoaderDeps(renderer) {
  const aniso = Math.min(4, renderer.capabilities.getMaxAnisotropy ? renderer.capabilities.getMaxAnisotropy() : 1);
  return {
    async fetchThumb(url, signal) {
      const res = await fetch(url, { signal, credentials: "same-origin" });
      if (!res.ok) { const e = new Error(`HTTP ${res.status}`); e.status = res.status; throw e; }
      return res.blob();
    },
    decode: (blob) => createImageBitmap(blob, { imageOrientation: "flipY", premultiplyAlpha: "none" }),
    makeTexture(bmp) {
      const t = new THREE.Texture(bmp);
      t.flipY = false;                                   // the bitmap was flipped at decode (an ImageBitmap cannot be flipped on upload)
      t.colorSpace = THREE.SRGBColorSpace;
      t.generateMipmaps = true;
      t.minFilter = THREE.LinearMipmapLinearFilter;
      t.magFilter = THREE.LinearFilter;
      t.anisotropy = aniso;
      t.needsUpdate = true;
      return t;
    },
    uploadTexture: (t) => renderer.initTexture(t),
    disposeTexture: (t) => t.dispose(),
  };
}

/* createWall({ renderer, parent, settings, loaderDeps?, cap? }) -> wall
 *   wall.setItems(items, { scrollIndex }) / wall.append(items)
 *   wall.update(dt, nowMs, stickY)         once per frame
 *   wall.pick(ox, oy, oz, dx, dy, dz)      ray in the rig's frame -> tile index or -1 (wall.hitDist = metres)
 *   wall.setHover(index)                   the hovered tile (-1 none)
 *   wall.setDim(f)                         brightness 0..1 of the whole wall (the viewer dims it to 0.3)
 *   wall.setSettings(settings)             span / radius / text size
 *   wall.scrollToTile(i) / wall.firstVisible() / wall.scroll (the model: top(), dragStart/drag/dragEnd, offset)
 *   wall.stats() / wall.dispose() */
export function createWall({ renderer, parent, settings, loaderDeps, cap }) {
  const group = new THREE.Group();
  group.name = "wall";
  parent.add(group);
  let cfg = { span: settings.span, radius: settings.radius, scale: textScale(settings) };
  let items = [];
  let layout = layoutRows([], cfg.span);
  let keyIndex = new Map();
  let tileSlot = new Int32Array(0);
  const scroll = createScroll();
  let layoutVersion = 0, appliedVersion = -1, appliedOffset = NaN, t0 = 0, t1 = 0;
  let hover = -1, hoverSince = 0, hoverShown = -1, lift = -1;
  let dimTarget = 1, dimCur = 1, dimApplied = -1;
  let aspectDirtyAt = 0, wantedAt = -1e9, wantedStamp = "";
  let disposed = false;
  const timing = new Float64Array(240); let timingN = 0, timingHead = 0;

  // ---- shared GPU objects
  const geo = new THREE.PlaneGeometry(1, 1);
  const circleGeo = new THREE.CircleGeometry(0.37, 48);
  circleGeo.translate(0, 0.10, 0);
  const textures = [];                               // every texture this wall owns besides the loader's
  const mk = (draw, w, h) => {
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    draw(canvas.getContext("2d"), w, h);
    const t = new THREE.CanvasTexture(canvas);
    t.colorSpace = THREE.SRGBColorSpace;
    t.generateMipmaps = true;
    t.minFilter = THREE.LinearMipmapLinearFilter;
    textures.push(t);
    return t;
  };
  const phPicture = mk((c, w, h) => drawPlaceholder(c, w, h, { color: "#26323f", glyph: "\u{1F5BC}" }), 64, 64);
  const phVideo = mk((c, w, h) => drawPlaceholder(c, w, h, { color: "#33283f", glyph: "▶" }), 64, 64);
  const phFolder = mk((c, w, h) => drawPlaceholder(c, w, h, { color: "#2b3350", glyph: "\u{1F4C1}" }), 64, 64);
  const tex3d = mk((c, w, h) => drawBadge(c, w, h, { text: "3D" }), 128, 64);
  const texPlay = mk((c, w, h) => drawBadge(c, w, h, { text: "▶", bg: "rgba(20,24,34,0.8)" }), 128, 64);
  const badgeMat3d = new THREE.MeshBasicMaterial({ map: tex3d, transparent: true, toneMapped: false, depthWrite: false });
  const badgeMatPlay = new THREE.MeshBasicMaterial({ map: texPlay, transparent: true, toneMapped: false, depthWrite: false });
  const frameMat = new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false });
  const frame = new THREE.Mesh(geo, frameMat);
  frame.rotation.order = "YXZ";
  frame.visible = false;
  group.add(frame);
  let caption = null;

  // ---- folder card textures: one shared canvas, each card uploaded right after drawing (budgeted), LRU
  const cardCanvas = document.createElement("canvas");
  cardCanvas.width = CARD_W; cardCanvas.height = CARD_H;
  const cardCtx = cardCanvas.getContext("2d");
  const cards = new Map();                           // item id -> { tex, last }
  let cardStamp = 0;
  const pendingCards = [];                           // tile indices waiting for their card (processed 3 per frame)

  function cardFor(i) {
    const it = items[i];
    let c = cards.get(it.id);
    if (c) { c.last = ++cardStamp; return c.tex; }
    cardCanvas.height = it.type === "subject" ? 114 : CARD_H;
    cardCtx.clearRect(0, 0, CARD_W, cardCanvas.height);
    if (it.type === "subject") drawCaption(cardCtx, CARD_W, cardCanvas.height, { name: it.name, meta: `${it.count.toLocaleString()} files` });
    else drawFolderCard(cardCtx, CARD_W, CARD_H, { name: it.name, scale: cfg.scale, special: it.special });
    const tex = new THREE.CanvasTexture(cardCanvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.generateMipmaps = true;
    tex.minFilter = THREE.LinearMipmapLinearFilter;
    renderer.initTexture(tex);                       // uploaded now, so the shared canvas can be reused for the next card
    tex.needsUpdate = false;
    c = { tex, last: ++cardStamp };
    cards.set(it.id, c);
    if (cards.size > FOLDER_CACHE) evictCards();
    return tex;
  }
  function evictCards() {
    const inUse = new Set();
    for (const s of pool) if (s.tile >= 0 && items[s.tile] && ["folder", "subject"].includes(items[s.tile].type)) inUse.add(items[s.tile].id);
    while (cards.size > FOLDER_CACHE) {
      let victim = null, vk = null;
      for (const [k, v] of cards) if (!inUse.has(k) && (!victim || v.last < victim.last)) { victim = v; vk = k; }
      if (!victim) break;
      victim.tex.dispose();
      cards.delete(vk);
    }
  }
  function clearCards() {
    for (const c of cards.values()) c.tex.dispose();
    cards.clear();
    pendingCards.length = 0;
  }

  // ---- the loader
  const loader = createThumbLoader(Object.assign({}, loaderDeps || realLoaderDeps(renderer), cap ? { cap } : {}, {
    onTexture: (key, tex, w, h) => onTexture(key, tex, w, h),
  }));

  // ---- the mesh pool
  const pool = [];
  const free = [];
  const tmp = { x: 0, y: 0, z: 0, yaw: 0, tilt: 0, width: 0, height: 0 };
  const hit = { az: 0, elev: 0, x: 0, y: 0, z: 0 };
  let hitDist = 0;

  function makeSlot() {
    const mat = new THREE.MeshBasicMaterial({ map: phPicture, toneMapped: false });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.order = "YXZ";
    mesh.visible = false;
    const b3 = new THREE.Mesh(geo, badgeMat3d);
    const bp = new THREE.Mesh(geo, badgeMatPlay);
    b3.visible = bp.visible = false;
    const label = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ transparent: true, toneMapped: false, depthWrite: false }));
    label.position.set(0, -0.40, 0.003); label.scale.set(0.95, 0.21, 1); label.visible = false;
    mesh.add(b3, bp, label);
    group.add(mesh);
    const slot = { idx: pool.length, mesh, mat, b3, bp, label, tile: -1, fade: 1, textured: false, crop: -1 };
    pool.push(slot);
    return slot;
  }

  function applyTexture(slot, tex, w, h) {
    slot.mat.map = tex;
    slot.textured = true;
    // cover-crop the image into the tile's (clamped) aspect
    const t = slot.tile;
    const ta = layout.w[t] / layout.h[t];
    const ia = w > 0 && h > 0 ? w / h : ta;
    if (ia > ta) { const rx = ta / ia; tex.repeat.set(rx, 1); tex.offset.set((1 - rx) / 2, 0); }
    else { const ry = ia / ta; tex.repeat.set(1, ry); tex.offset.set(0, (1 - ry) / 2); }
    slot.crop = layoutVersion;
  }

  function placeholderFor(it) { return it.type === "folder" ? phFolder : (it.type === "video" ? phVideo : phPicture); }

  function acquire(t) {
    const slot = free.length ? pool[free.pop()] : makeSlot();
    const it = items[t];
    slot.tile = t;
    tileSlot[t] = slot.idx;
    slot.mesh.visible = true;
    slot.mesh.geometry = it.type === "subject" ? circleGeo : geo;
    slot.label.visible = it.type === "subject";
    if (it.type === "subject") { slot.label.material.map = null; pendingCards.push(t); }
    slot.textured = false;
    slot.fade = 1;
    slot.mat.map = placeholderFor(it);
    slot.b3.visible = !!(it.status && it.status.state === "done");
    slot.bp.visible = it.type === "video";
    slot.mat.color.setScalar(dimCur);
    if (it.type === "folder") {
      pendingCards.push(t);
    } else {
      const tex = loader.get(it.id);
      if (tex) { applyTexture(slot, tex, loader.info(it.id).w, loader.info(it.id).h); }
    }
    return slot;
  }

  function release(slot) {
    if (slot.tile >= 0) tileSlot[slot.tile] = -1;
    slot.tile = -1;
    slot.mesh.visible = false;
    slot.mat.map = phPicture;
    slot.textured = false;
    free.push(slot.idx);
  }

  function onTexture(key, tex, w, h) {
    const i = keyIndex.get(key);
    if (i === undefined) return;
    const it = items[i];
    if (it && it.type !== "subject" && !it.aspectKnown && w > 0 && h > 0) {
      it.aspect = w / h; it.aspectKnown = true;
      aspectDirtyAt = performance.now();
    }
    const s = tileSlot[i];
    if (s >= 0) { applyTexture(pool[s], tex, w, h); pool[s].fade = 0.3; }
  }

  // ---- layout
  function relayout(anchor = true) {
    let anchorTile = -1, delta = 0;
    if (anchor && layout.n && scroll.offset > 0) {
      anchorTile = firstVisibleTile(layout, scroll.offset);
      delta = scroll.offset - layout.rowTop[layout.row[anchorTile]];
    }
    const aspects = new Float32Array(items.length);
    for (let i = 0; i < items.length; i += 1) aspects[i] = items[i].aspect;
    layout = layoutRows(aspects, cfg.span);
    scroll.setMax(maxOffset(layout));
    if (anchorTile >= 0 && anchorTile < layout.n) scroll.jump(layout.rowTop[layout.row[anchorTile]] + delta);
    layoutVersion += 1;
    aspectDirtyAt = 0;
  }

  function reindex() {
    keyIndex = new Map();
    for (let i = 0; i < items.length; i += 1) if (items[i].type !== "folder") keyIndex.set(items[i].id, i);
  }

  function growSlots() {
    if (tileSlot.length >= items.length) return;
    const next = new Int32Array(items.length).fill(-1);
    next.set(tileSlot);
    tileSlot = next;
  }

  function releaseAll() {
    for (const s of pool) if (s.tile >= 0) release(s);
    tileSlot.fill(-1);
    t0 = t1 = 0;
    appliedVersion = -1;
  }

  // ---- window, placement, loader wish list
  const wanted = [];
  function syncWindow(force) {
    const { r0, r1 } = visibleRows(layout, scroll.offset, MARGIN_ROWS);
    const a = r1 < r0 ? 0 : layout.rowStart[r0];
    const b = r1 < r0 ? 0 : layout.rowStart[r1 + 1];
    if (!force && a === t0 && b === t1) return false;
    for (let i = 0; i < pool.length; i += 1) {
      const s = pool[i];
      if (s.tile >= 0 && (s.tile < a || s.tile >= b)) release(s);
    }
    for (let t = a; t < b; t += 1) if (tileSlot[t] < 0) acquire(t);
    t0 = a; t1 = b;
    return true;
  }

  function placeAll() {
    const R = cfg.radius;
    const off = scroll.offset;
    for (let i = 0; i < pool.length; i += 1) {
      const s = pool[i];
      if (s.tile < 0) continue;
      placeSlot(s, off, R);
    }
    appliedOffset = off;
    appliedVersion = layoutVersion;
  }

  function placeSlot(s, off, R) {
    const t = s.tile;
    tilePlacement(layout, t, off, R, tmp);
    const m = s.mesh;
    let sc = 1, push = 1;
    if (t === hover) { sc = HOVER_LIFT; push = (R - 0.05) / R; }
    m.position.set(tmp.x * push, tmp.y, tmp.z * push);
    m.rotation.set(tmp.tilt, tmp.yaw, 0);
    const w = tmp.width * sc, h = tmp.height * sc;
    m.scale.set(w, h, 1);
    if (s.b3.visible || s.bp.visible) {
      const bw = 0.07, bh = 0.035, mg = 0.012;
      const sx = bw / w, sy = bh / h;
      if (s.b3.visible) { s.b3.scale.set(sx, sy, 1); s.b3.position.set(0.5 - (bw / 2 + mg) / w, 0.5 - (bh / 2 + mg) / h, 0.002 / 1); }
      if (s.bp.visible) { s.bp.scale.set(sx, sy, 1); s.bp.position.set(-0.5 + (bw / 2 + mg) / w, -0.5 + (bh / 2 + mg) / h, 0.002); }
    }
    if (s.crop !== layoutVersion && s.textured) {
      const info = items[t] && items[t].type !== "folder" ? loader.info(items[t].id) : null;
      const tex = s.mat.map;
      if (info && info.w > 0) applyTexture(s, tex, info.w, info.h);
    }
  }

  function buildWanted() {
    let n = 0;
    for (let t = t0; t < t1; t += 1) {
      const it = items[t];
      if (it.type === "folder" || !it.thumb) continue;
      const az = layout.az[t];
      const el = rowElevation(layout, layout.row[t], scroll.offset);
      const inBand = el < WALL.bandTopDeg + 4 && el > WALL.bandBottomDeg - 4;
      let w = wanted[n];
      if (!w) { w = { key: "", url: "", priority: 0 }; wanted[n] = w; }
      w.key = it.id; w.url = it.thumb;
      w.priority = Math.hypot(az, el) + (inBand ? 0 : 200);
      n += 1;
    }
    loader.setWanted(wanted, n);
    return n;
  }

  function colorOf(s) {
    const f = (s.textured || s.tile >= 0 && items[s.tile] && items[s.tile].type === "folder" ? s.fade : 1) * dimCur;
    s.mat.color.setScalar(f);
    s.label.material.color.setScalar(f);
  }

  function ensureCaption() {
    if (caption) return caption;
    caption = createPanel({ widthM: CAP_W_M, heightM: CAP_H_M, pxPerM: CAP_PX, opacity: 1 });
    caption.mesh.rotation.order = "YXZ";
    caption.mesh.visible = false;
    group.add(caption.mesh);
    return caption;
  }

  function showCaption(i, nowMs) {
    const c = ensureCaption();
    const it = items[i];
    if (!it || it.type === "folder" || nowMs - hoverSince < CAPTION_DELAY_MS) { c.mesh.visible = false; hoverShown = -1; return; }
    const R = cfg.radius;
    tilePlacement(layout, i, scroll.offset, R, tmp);
    const sc = Math.max(0.8, cfg.scale);                       // text size setting: the caption panel grows with it
    c.mesh.scale.set(sc, sc, 1);
    c.mesh.position.set(tmp.x * 0.97, tmp.y - tmp.height / 2 - (CAP_H_M * sc) / 2 - 0.04, tmp.z * 0.97);
    c.mesh.rotation.set(tmp.tilt, tmp.yaw, 0);
    c.mesh.visible = true;
    if (hoverShown !== i) {
      hoverShown = i;
      const meta = [it.type === "video" ? fmtDuration(it.duration) : "", it.status && it.status.state === "done" ? "3D" : ""].filter(Boolean).join("  ·  ");
      c.redraw((x, w, h) => drawCaption(x, w, h, { name: it.name, meta }));
    }
  }

  const wall = {
    group, scroll, loader,
    get items() { return items; },
    get layout() { return layout; },
    get settings() { return cfg; },
    get hover() { return hover; },
    get hitDist() { return hitDist; },
    get hitAz() { return hit.az; },
    get hitElev() { return hit.elev; },
    get window() { return { t0, t1 }; },
    get meshCount() { let n = 0; for (const s of pool) if (s.tile >= 0) n += 1; return n; },
    get poolSize() { return pool.length; },
    get dim() { return dimTarget; },

    setItems(list, { scrollIndex = 0 } = {}) {
      items = list.slice();
      reindex();
      releaseAll();
      tileSlot = new Int32Array(items.length).fill(-1);
      scroll.jump(0);
      relayout(false);
      if (scrollIndex > 0 && scrollIndex < items.length) scroll.jump(offsetForTile(layout, scrollIndex));
      wantedAt = -1e9;
    },

    append(list) {
      if (!list.length) return;
      for (const it of list) items.push(it);
      reindex();
      growSlots();
      relayout(true);
    },

    // Items changed in place (a status, a favourite): refresh badges.
    refreshItem(i) {
      const s = tileSlot[i];
      if (s < 0) return;
      const it = items[i];
      pool[s].b3.visible = !!(it.status && it.status.state === "done");
      appliedVersion = -1;
    },

    setSettings(next) {
      const nc = { span: next.span, radius: next.radius, scale: textScale(next) };
      const spanChanged = nc.span !== cfg.span;
      const textChanged = nc.scale !== cfg.scale;
      cfg = nc;
      if (spanChanged) relayout(true);
      if (textChanged) { clearCards(); for (const s of pool) if (s.tile >= 0 && ["folder", "subject"].includes(items[s.tile].type)) pendingCards.push(s.tile); }
      layoutVersion += 1;
      appliedVersion = -1;
      hoverShown = -1;
    },

    setHover(i) {
      if (i === hover) return;
      hover = i;
      hoverSince = performance.now();
      hoverShown = -1;
      appliedVersion = -1;
    },

    setDim(f) { dimTarget = clamp01(f); },

    // align "center" puts the tile's row mid-band, "top" puts its row at the top of the band.
    scrollToTile(i, align = "center") {
      if (i < 0 || i >= layout.n) return;
      if (align === "top") scroll.jump(Math.min(layout.rowTop[layout.row[i]], maxOffset(layout)));
      else scroll.jump(offsetForTile(layout, i));
    },
    // The mesh currently showing tile i (null when it is outside the window).
    meshOf(i) { const s = i >= 0 && i < tileSlot.length ? tileSlot[i] : -1; return s >= 0 ? pool[s].mesh : null; },
    firstVisible() { return layout.n ? firstVisibleTile(layout, scroll.offset) : 0; },
    nearEnd() { return scroll.max - scroll.offset < bandDeg() * 2; },

    /* One frame. stickY = the thumbstick Y axis driving scrolling (0 when unused). */
    update(dt, nowMs, stickY = 0) {
      if (disposed) return;
      const tStart = performance.now();
      scroll.step(dt, stickY);
      const windowChanged = syncWindow(false);
      if (scroll.offset !== appliedOffset || appliedVersion !== layoutVersion) placeAll();
      // hover frame and caption
      if (hover >= 0 && hover < layout.n && tileSlot[hover] >= 0) {
        const s = pool[tileSlot[hover]];
        frame.visible = true;
        frame.geometry = items[s.tile].type === "subject" ? circleGeo : geo;
        frame.position.copy(s.mesh.position);
        frame.position.x *= 1.004; frame.position.z *= 1.004;
        frame.rotation.copy(s.mesh.rotation);
        frame.scale.set(s.mesh.scale.x + 0.018, s.mesh.scale.y + 0.018, 1);
        showCaption(hover, nowMs);
      } else {
        frame.visible = false;
        if (caption) caption.mesh.visible = false;
      }
      // loader wish list: on window change, and about every 250 ms (the focus moves with the scroll)
      if (windowChanged || nowMs - wantedAt > 250) { wantedAt = nowMs; buildWanted(); }
      loader.frame();
      // folder cards, 3 per frame
      for (let k = 0; k < 3 && pendingCards.length; k += 1) {
        const t = pendingCards.shift();
        const s = tileSlot[t];
        if (s < 0 || !items[t] || !["folder", "subject"].includes(items[t].type)) continue;
        const tex = cardFor(t);
        if (items[t].type === "subject") { pool[s].label.material.map = tex; pool[s].label.material.needsUpdate = true; continue; }
        pool[s].mat.map = tex;
        pool[s].textured = true;
        pool[s].fade = 0.3;
        tex.repeat.set(1, 1); tex.offset.set(0, 0);
      }
      // aspects learned from thumbnails: re-lay out at most every ~0.4 s, anchored on the first visible tile
      if (aspectDirtyAt && performance.now() - aspectDirtyAt > 400) relayout(true);
      // dim and fades
      if (dimCur !== dimTarget) {
        const step = dt / 0.2;
        dimCur = dimCur < dimTarget ? Math.min(dimTarget, dimCur + step) : Math.max(dimTarget, dimCur - step);
      }
      const dimChanged = dimCur !== dimApplied;
      for (let i = 0; i < pool.length; i += 1) {
        const s = pool[i];
        if (s.tile < 0) continue;
        if (s.fade < 1) { s.fade = Math.min(1, s.fade + dt / FADE_S); colorOf(s); }
        else if (dimChanged) colorOf(s);
      }
      dimApplied = dimCur;
      timing[timingHead] = performance.now() - tStart;
      timingHead = (timingHead + 1) % timing.length;
      if (timingN < timing.length) timingN += 1;
    },

    /* The ray (rig frame) -> tile index (-1 none). */
    pick(ox, oy, oz, dx, dy, dz) {
      const t = rayCylinder(ox, oy, oz, dx, dy, dz, cfg.radius, hit);
      if (t < 0) { hitDist = 0; return -1; }
      hitDist = t;
      return hitTile(layout, hit.az, hit.elev, scroll.offset);
    },

    stats() {
      const st = loader.stats;
      const a = Array.from(timing.subarray(0, timingN)).sort((x, y) => x - y);
      const p = (q) => (a.length ? a[Math.min(a.length - 1, Math.ceil(q * a.length) - 1)] : 0);
      return {
        tiles: items.length, rows: layout.rows, meshes: wall.meshCount, pool: pool.length, offset: scroll.offset, max: scroll.max,
        textures: loader.textureCount, queued: loader.queued, inflight: loader.inflight, decoded: loader.decoded,
        uploads: st.uploads, lastUploadMs: st.lastUploadMs, maxUploadMs: st.maxUploadMs, evicted: st.evicted, failed: st.failed, aborted: st.aborted,
        updateP50: p(0.5), updateP99: p(0.99), updateMax: a.length ? a[a.length - 1] : 0, cards: cards.size,
      };
    },

    dispose() {
      if (disposed) return;
      disposed = true;
      loader.dispose();
      clearCards();
      for (const s of pool) { s.mat.dispose(); s.label.material.dispose(); s.mesh.removeFromParent(); }
      pool.length = 0; free.length = 0;
      for (const t of textures) t.dispose();
      badgeMat3d.dispose(); badgeMatPlay.dispose(); frameMat.dispose(); geo.dispose(); circleGeo.dispose();
      if (caption) caption.dispose();
      group.removeFromParent();
    },
  };
  return wall;
}
