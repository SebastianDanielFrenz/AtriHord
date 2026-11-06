/* The VR comfort settings (P8 V1): wall span, radius and text size, kept in localStorage - pure logic, so the
 * defaults, clamping, persistence and the settings panel's layout can be unit-tested. The panel itself is drawn by
 * ui.js; every option is a button whose id is "<row>:<value>". */
import { WALL, clamp } from "./layout.js";
import { clampDistance, DEFAULT_DISTANCE } from "../vr-logic.js";

export const SETTINGS_KEY = "mediadl.vr.settings";
export const SPANS = WALL.SPANS;                    // degrees
export const RADII = [1.5, 2, 2.5, 3];              // metres
export const TEXT_SIZES = [{ id: "S", scale: 1 }, { id: "M", scale: 1.2 }, { id: "L", scale: 1.4 }];

// hiRate: ask the XR runtime for its highest frame rate at session start (V2c); videoEveryFrame: flag the video texture
// for upload every XR frame instead of only when the browser reports a new video frame (V2c, default off).
// screenScale (V2d): the media screen's size in percent of today's size (100), range 50..150 (owner), steps of 5.
export const SCREEN_SCALE = { min: 50, max: 150, def: 100, step: 5, button: 10, presets: [50, 75, 100, 125, 150] };
export const clampScale = (p) => {
  const v = typeof p === "number" && Number.isFinite(p) ? p : SCREEN_SCALE.def;
  return Math.min(SCREEN_SCALE.max, Math.max(SCREEN_SCALE.min, Math.round(v / SCREEN_SCALE.step) * SCREEN_SCALE.step));
};
export const DEFAULTS = { span: 120, radius: 2, text: "S", hiRate: true, videoEveryFrame: false, screenScale: SCREEN_SCALE.def };

const nearest = (list, v, fallback) => {
  if (typeof v !== "number" || !Number.isFinite(v)) return fallback;
  return list.reduce((best, x) => (Math.abs(x - v) < Math.abs(best - v) ? x : best), list[0]);
};

export function normalizeSettings(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  const text = TEXT_SIZES.some((t) => t.id === r.text) ? r.text : DEFAULTS.text;
  return {
    span: nearest(SPANS, Number(r.span), DEFAULTS.span), radius: nearest(RADII, Number(r.radius), DEFAULTS.radius), text,
    hiRate: typeof r.hiRate === "boolean" ? r.hiRate : DEFAULTS.hiRate,
    videoEveryFrame: typeof r.videoEveryFrame === "boolean" ? r.videoEveryFrame : DEFAULTS.videoEveryFrame,
    screenScale: clampScale(r.screenScale),
  };
}

export const textScale = (s) => (TEXT_SIZES.find((t) => t.id === s.text) || TEXT_SIZES[0]).scale;

export function loadSettings(storage) {
  try { return normalizeSettings(JSON.parse(storage.getItem(SETTINGS_KEY) || "null")); } catch (e) { return normalizeSettings(null); }
}

export function saveSettings(storage, s) {
  try { storage.setItem(SETTINGS_KEY, JSON.stringify(normalizeSettings(s))); return true; } catch (e) { return false; }
}

// The rows of the panel: label plus the options (value ids are strings).
export function settingsRows(s) {
  return [
    { id: "span", label: "Wall span", options: SPANS.map((v) => ({ id: String(v), label: `${v}°`, on: s.span === v })) },
    { id: "radius", label: "Wall distance", options: RADII.map((v) => ({ id: String(v), label: `${v} m`, on: s.radius === v })) },
    { id: "text", label: "Text size", options: TEXT_SIZES.map((t) => ({ id: t.id, label: t.id, on: s.text === t.id })) },
    { id: "screen", label: "Screen size", options: SCREEN_SCALE.presets.map((v) => ({ id: String(v), label: `${v}%`, on: s.screenScale === v })) },
    { id: "hirate", label: "Highest refresh rate", options: [{ id: "on", label: "On", on: !!s.hiRate }, { id: "off", label: "Off", on: !s.hiRate }] },
    { id: "vframe", label: "Video every XR frame", options: [{ id: "on", label: "On", on: !!s.videoEveryFrame }, { id: "off", label: "Off", on: !s.videoEveryFrame }] },
  ];
}

/* Layout of the panel in a width x height pixel canvas: a title strip, one line per row (label on the left, the
 * option buttons to the right) and a Close button at the bottom. -> [{ id, label, x, y, w, h, on }] where id is
 * "span:120" or "close". */
export function settingsLayout(rows, width, height, { pad = 40 } = {}) {
  const out = [];
  const top = Math.round(height * 0.2);
  const closeH = Math.round(height * 0.14);
  const rowH = Math.round((height - top - closeH - pad * 1.5) / Math.max(1, rows.length));
  const labelW = Math.round(width * 0.3);
  rows.forEach((row, i) => {
    const y = top + i * rowH;
    const n = row.options.length;
    const gap = 18;
    const bw = Math.min(210, (width - 2 * pad - labelW - gap * (n - 1)) / n);
    const bh = Math.round(rowH * 0.7);
    row.options.forEach((o, k) => out.push({ id: `${row.id}:${o.id}`, label: o.label, x: pad + labelW + k * (bw + gap), y: y + (rowH - bh) / 2, w: bw, h: bh, on: o.on }));
  });
  const cw = Math.round(width * 0.3);
  out.push({ id: "close", label: "Close", x: (width - cw) / 2, y: height - pad - closeH, w: cw, h: closeH, on: false });
  return out;
}

// "span:150" -> a new settings object (unchanged for an unknown id).
export function applyChoice(s, id) {
  const [row, value] = String(id).split(":");
  const next = Object.assign({}, s);
  if (row === "span") next.span = nearest(SPANS, Number(value), s.span);
  else if (row === "radius") next.radius = nearest(RADII, Number(value), s.radius);
  else if (row === "text" && TEXT_SIZES.some((t) => t.id === value)) next.text = value;
  else if (row === "screen") next.screenScale = clampScale(Number(value));
  else if (row === "hirate" && (value === "on" || value === "off")) next.hiRate = value === "on";
  else if (row === "vframe" && (value === "on" || value === "off")) next.videoEveryFrame = value === "on";
  return normalizeSettings(next);
}

export { clamp };

/* ---- the screen-size slider (V2d): pure layout and mapping, drawn by ui.js, one row under the dock.
 * scaleSliderLayout(w, h) -> { label, minus, plus, track } rectangles in panel pixels. */
export function scaleSliderLayout(w, h) {
  const pad = Math.round(h * 0.18), bh = h - 2 * pad, bw = Math.round(h * 0.9), labelW = Math.round(w * 0.2), gap = Math.round(h * 0.2);
  const label = { x: pad, y: pad, w: labelW, h: bh };
  const minus = { id: "minus", x: label.x + labelW + gap, y: pad, w: bw, h: bh };
  const plus = { id: "plus", x: w - pad - bw, y: pad, w: bw, h: bh };
  const x0 = minus.x + bw + gap;
  const track = { id: "track", x: x0, y: 0, w: plus.x - gap - x0, h };
  return { label, minus, plus, track, buttons: [minus, plus, track] };
}
// Position along the track (0..1, may be outside while dragging) <-> percent.
export const scaleFromFraction = (f) => clampScale(SCREEN_SCALE.min + Math.min(1, Math.max(0, Number.isFinite(f) ? f : 0)) * (SCREEN_SCALE.max - SCREEN_SCALE.min));
export const fractionFromScale = (p) => (clampScale(p) - SCREEN_SCALE.min) / (SCREEN_SCALE.max - SCREEN_SCALE.min);
export const stepScale = (p, dir) => clampScale(clampScale(p) + (dir < 0 ? -1 : 1) * SCREEN_SCALE.button);
export const scaleLabel = (p) => `Size ${clampScale(p)} %`;

/* ---- the screen's front-back position (V2d): its distance from the viewer, kept in localStorage under a versioned key so the
 * next item - in the next session, after the browser was closed - opens at the same effective size (slider percent x
 * distance size). The picture zoom is deliberately NOT stored (per item / session as before), nor left-right/up-down.
 * Clamped on load; anything corrupt falls back to the default. */
export const VIEW_KEY = "mediadl.vr.view.v1";
export function normalizeView(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : NaN);
  return { dist: clampDistance(Number.isNaN(num(r.dist)) ? DEFAULT_DISTANCE : num(r.dist)) };
}
export function loadView(storage) {
  try { const raw = JSON.parse(storage.getItem(VIEW_KEY) || "null"); return raw && raw.v === 1 ? normalizeView(raw) : normalizeView(null); } catch (e) { return normalizeView(null); }
}
export function saveView(storage, v) {
  try { storage.setItem(VIEW_KEY, JSON.stringify(Object.assign({ v: 1 }, normalizeView(v)))); return true; } catch (e) { return false; }
}
