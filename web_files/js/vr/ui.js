/* Text and panels for the VR scene: a panel is a plane showing a 2D canvas, so labels come out of
 * the system's own fonts (Windows' Segoe UI, with its automatic fallback to Yu Gothic / Meiryo /
 * Malgun for Japanese and Korean names) with nothing to vendor. Redraw only on change. */
import * as THREE from "../vendor/three.module.min.js";

export const FONT = "'Segoe UI', 'Yu Gothic UI', Meiryo, 'Malgun Gothic', system-ui, sans-serif";
export const MONO = "Consolas, ui-monospace, 'Cascadia Mono', monospace";

export function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

// text cut to maxW pixels with an ellipsis (measured, so wide CJK glyphs fit as well as Latin ones)
export function fitText(ctx, text, maxW) {
  const s = String(text == null ? "" : text);
  if (ctx.measureText(s).width <= maxW) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (ctx.measureText(s.slice(0, mid) + "…").width <= maxW) lo = mid; else hi = mid - 1;
  }
  return s.slice(0, lo) + "…";
}

/* createPanel({ widthM, heightM, pxPerM, opacity }) -> { mesh, canvas, ctx, w, h, texture, redraw(fn), dispose() }
 * fn(ctx, w, h) draws the whole panel; the texture is re-uploaded after it. */
export function createPanel({ widthM, heightM, pxPerM = 1600, opacity = 1 }) {
  const w = Math.round(widthM * pxPerM), h = Math.round(heightM * pxPerM);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  texture.generateMipmaps = true;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  const geometry = new THREE.PlaneGeometry(widthM, heightM);
  const material = new THREE.MeshBasicMaterial({ map: texture, transparent: true, opacity, toneMapped: false, depthWrite: false });
  const mesh = new THREE.Mesh(geometry, material);
  return {
    mesh, canvas, ctx, w, h, texture, material,
    redraw(fn) {
      ctx.clearRect(0, 0, w, h);
      fn(ctx, w, h);
      texture.needsUpdate = true;
    },
    dispose() {
      texture.dispose();
      geometry.dispose();
      material.dispose();
      if (mesh.parent) mesh.parent.remove(mesh);
    },
  };
}

// The dock: source title and item on top, the buttons below. hover = id of the highlighted button.
export function drawDock(ctx, w, h, { layout, title, subtitle, hover, message }) {
  ctx.fillStyle = "rgba(18,20,28,0.92)";
  roundRect(ctx, 0, 0, w, h, 36);
  ctx.fill();
  ctx.strokeStyle = "rgba(120,130,160,0.55)";
  ctx.lineWidth = 3;
  roundRect(ctx, 2, 2, w - 4, h - 4, 34);
  ctx.stroke();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  const pad = 36;
  ctx.fillStyle = "#e8eaf2";
  ctx.font = `600 46px ${FONT}`;
  ctx.fillText(fitText(ctx, title, w - 2 * pad), pad, h * 0.17);
  ctx.fillStyle = message ? "#ffcf7a" : "#9aa3bd";
  ctx.font = `400 34px ${FONT}`;
  ctx.fillText(fitText(ctx, message || subtitle || "", w - 2 * pad), pad, h * 0.38);
  ctx.textAlign = "center";
  for (const b of layout) {
    const on = hover === b.id;
    ctx.fillStyle = on ? "rgba(90,140,255,0.95)" : "rgba(52,58,78,0.95)";
    roundRect(ctx, b.x, b.y, b.w, b.h, 22);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.font = `600 44px ${FONT}`;
    ctx.fillText(fitText(ctx, b.label, b.w - 24), b.x + b.w / 2, b.y + b.h / 2 + 2);
  }
}

export function drawLines(ctx, w, h, lines, { title = "", font = `400 32px ${MONO}`, lineHeight = 52 } = {}) {
  ctx.fillStyle = "rgba(10,12,18,0.88)";
  roundRect(ctx, 0, 0, w, h, 28);
  ctx.fill();
  ctx.strokeStyle = "rgba(120,130,160,0.45)";
  ctx.lineWidth = 3;
  roundRect(ctx, 2, 2, w - 4, h - 4, 26);
  ctx.stroke();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  let y = 46;
  if (title) {
    ctx.fillStyle = "#e8eaf2";
    ctx.font = `600 38px ${FONT}`;
    ctx.fillText(title, 32, y);
    y += lineHeight;
  }
  ctx.fillStyle = "#c5cbe0";
  ctx.font = font;
  for (const l of lines) {
    ctx.fillText(fitText(ctx, l, w - 64), 32, y);
    y += lineHeight * 0.8;
  }
}

/* ---------------------------------------------------------------- V1: analytic ray tests, settings, wall labels */

// A ray against a panel's plane, without three's Raycaster (which allocates per call): the panel is a w x h metre
// rectangle in its mesh's local XY plane (mesh.matrixWorld must be current; scale 1). Returns the distance along the
// ray or -1; out gets {u, v} = position on the panel in 0..1 from the left / from the top.
export function createPanelHit() {
  const pos = new THREE.Vector3(), right = new THREE.Vector3(), up = new THREE.Vector3(), nrm = new THREE.Vector3();
  return function hit(mesh, widthM, heightM, o, d, out, unbounded = false) {
    const e = mesh.matrixWorld.elements;
    pos.set(e[12], e[13], e[14]);
    right.set(e[0], e[1], e[2]).normalize();
    up.set(e[4], e[5], e[6]).normalize();
    nrm.set(e[8], e[9], e[10]).normalize();
    const denom = d.x * nrm.x + d.y * nrm.y + d.z * nrm.z;
    if (Math.abs(denom) < 1e-6) return -1;
    const t = ((pos.x - o.x) * nrm.x + (pos.y - o.y) * nrm.y + (pos.z - o.z) * nrm.z) / denom;
    if (!(t > 0)) return -1;
    const hx = o.x + t * d.x - pos.x, hy = o.y + t * d.y - pos.y, hz = o.z + t * d.z - pos.z;
    const lx = hx * right.x + hy * right.y + hz * right.z;
    const ly = hx * up.x + hy * up.y + hz * up.z;
    if (!unbounded && (Math.abs(lx) > widthM / 2 || Math.abs(ly) > heightM / 2)) return -1;
    out.u = lx / widthM + 0.5;
    out.v = 0.5 - ly / heightM;
    return t;
  };
}

// The settings panel: title, a line per row with its option buttons, Close. layout = settingsLayout(...).
export function drawSettings(ctx, w, h, { layout, rows, hover, title = "Settings", drawButton = null }) {
  ctx.fillStyle = "rgba(18,20,28,0.95)";
  roundRect(ctx, 0, 0, w, h, 36);
  ctx.fill();
  ctx.strokeStyle = "rgba(120,130,160,0.55)";
  ctx.lineWidth = 3;
  roundRect(ctx, 2, 2, w - 4, h - 4, 34);
  ctx.stroke();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = "#e8eaf2";
  ctx.font = `600 54px ${FONT}`;
  ctx.fillText(title, 40, h * 0.1);
  ctx.font = `400 44px ${FONT}`;
  ctx.fillStyle = "#c5cbe0";
  rows.forEach((row) => {
    const first = layout.find((b) => b.id.startsWith(`${row.id}:`));
    if (first) ctx.fillText(row.label, 40, first.y + first.h / 2);
  });
  ctx.textAlign = "center";
  for (const b of layout) {
    if (typeof drawButton === "function" && drawButton(ctx, b)) continue;
    ctx.fillStyle = hover === b.id ? "rgba(120,165,255,0.98)" : (b.on ? "rgba(70,120,230,0.95)" : "rgba(52,58,78,0.95)");
    roundRect(ctx, b.x, b.y, b.w, b.h, 20);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.font = `600 42px ${FONT}`;
    ctx.fillText(fitText(ctx, b.label, b.w - 20), b.x + b.w / 2, b.y + b.h / 2 + 2);
  }
}

// The caption under a hovered tile: name, then a small line of facts. Size scales with the text-size setting.
export function drawCaption(ctx, w, h, { name, meta }) {
  ctx.fillStyle = "rgba(10,12,18,0.9)";
  roundRect(ctx, 0, 0, w, h, h * 0.28);
  ctx.fill();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  const pad = h * 0.22;
  ctx.fillStyle = "#f2f4fa";
  ctx.font = `600 ${Math.round(h * 0.38)}px ${FONT}`;
  ctx.fillText(fitText(ctx, name, w - 2 * pad), pad, h * (meta ? 0.34 : 0.5));
  if (meta) {
    ctx.fillStyle = "#9aa3bd";
    ctx.font = `400 ${Math.round(h * 0.28)}px ${FONT}`;
    ctx.fillText(fitText(ctx, meta, w - 2 * pad), pad, h * 0.72);
  }
}

// A folder tile's face: a card with the folder glyph and its name (always visible), 2 lines at most.
export function drawFolderCard(ctx, w, h, { name, scale = 1, special = "" }) {
  const fav = special === "favorites";            // the Favourites tile: a plum card with a star
  ctx.fillStyle = fav ? "#4a2b45" : "#2b3350";
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = fav ? "#6b3a62" : "#3a4570";
  roundRect(ctx, w * 0.04, h * 0.06, w * 0.92, h * 0.88, h * 0.08);
  ctx.fill();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = "#f0c76a";
  const fs = Math.round(h * 0.2 * scale);
  ctx.font = `400 ${Math.round(h * 0.3)}px ${FONT}`;
  ctx.fillText(fav ? "\u2605" : "\u{1F4C1}", w * 0.08, h * 0.3);
  ctx.fillStyle = "#f2f4fa";
  ctx.font = `600 ${fs}px ${FONT}`;
  // wrap into two lines on the measured width
  const maxW = w * 0.84;
  const words = String(name).split(/(?<=[\s_\-\/])/);
  let l1 = "", rest = "";
  for (let i = 0; i < words.length; i += 1) {
    const t = l1 + words[i];
    if (ctx.measureText(t).width > maxW && l1) { rest = words.slice(i).join(""); break; }
    l1 = t;
  }
  ctx.fillText(fitText(ctx, l1.trim() || String(name), maxW), w * 0.08, h * 0.62);
  if (rest) ctx.fillText(fitText(ctx, rest.trim(), maxW), w * 0.08, h * 0.62 + fs * 1.15);
}

// A small flat placeholder face for a tile still without its thumbnail (one shared texture per kind).
export function drawPlaceholder(ctx, w, h, { color, glyph }) {
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = "rgba(255,255,255,0.25)";
  ctx.textBaseline = "middle";
  ctx.textAlign = "center";
  ctx.font = `400 ${Math.round(h * 0.4)}px ${FONT}`;
  ctx.fillText(glyph, w / 2, h / 2);
}

// A badge ("3D", a play mark): rounded chip with text, drawn once into a shared texture.
export function drawBadge(ctx, w, h, { text, bg = "rgba(70,120,230,0.95)" }) {
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = bg;
  roundRect(ctx, 0, 0, w, h, h * 0.3);
  ctx.fill();
  ctx.fillStyle = "#ffffff";
  ctx.textBaseline = "middle";
  ctx.textAlign = "center";
  ctx.font = `700 ${Math.round(h * 0.6)}px ${FONT}`;
  ctx.fillText(text, w / 2, h / 2 + 1);
}

// The hover outline: a rounded frame, transparent inside.
export function drawOutline(ctx, w, h) {
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = Math.max(4, w * 0.02);
  roundRect(ctx, ctx.lineWidth, ctx.lineWidth, w - 2 * ctx.lineWidth, h - 2 * ctx.lineWidth, Math.min(w, h) * 0.06);
  ctx.stroke();
}

// The screen-size slider row (V2d): label, [-], track with a knob, [+]. lay = scaleSliderLayout(); frac = knob position 0..1.
export function drawScaleSlider(ctx, w, h, { lay, label, frac, hover, drag }) {
  ctx.fillStyle = "rgba(18,20,28,0.92)";
  roundRect(ctx, 0, 0, w, h, h * 0.3);
  ctx.fill();
  ctx.strokeStyle = "rgba(120,130,160,0.55)";
  ctx.lineWidth = 3;
  roundRect(ctx, 2, 2, w - 4, h - 4, h * 0.3 - 2);
  ctx.stroke();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.fillStyle = "#e8eaf2";
  ctx.font = `600 ${Math.round(h * 0.34)}px ${FONT}`;
  ctx.fillText(fitText(ctx, label, lay.label.w), lay.label.x, h / 2 + 1);
  ctx.textAlign = "center";
  for (const b of [lay.minus, lay.plus]) {
    ctx.fillStyle = hover === b.id ? "rgba(90,140,255,0.95)" : "rgba(52,58,78,0.95)";
    roundRect(ctx, b.x, b.y, b.w, b.h, 16);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.font = `700 ${Math.round(h * 0.5)}px ${FONT}`;
    ctx.fillText(b.id === "minus" ? "-" : "+", b.x + b.w / 2, b.y + b.h / 2 + 2);
  }
  const t = lay.track, cy = h / 2, tx0 = t.x + h * 0.2, tx1 = t.x + t.w - h * 0.2;
  ctx.fillStyle = "rgba(90,98,126,0.95)";
  roundRect(ctx, tx0, cy - h * 0.06, tx1 - tx0, h * 0.12, h * 0.06);
  ctx.fill();
  const kx = tx0 + (tx1 - tx0) * Math.min(1, Math.max(0, frac));
  ctx.fillStyle = "rgba(70,120,230,0.95)";
  roundRect(ctx, tx0, cy - h * 0.06, kx - tx0, h * 0.12, h * 0.06);
  ctx.fill();
  ctx.fillStyle = drag || hover === "track" ? "#ffffff" : "#dfe6ff";
  ctx.beginPath();
  ctx.arc(kx, cy, h * 0.2, 0, Math.PI * 2);
  ctx.fill();
}
