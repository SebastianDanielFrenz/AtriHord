/* Search and filters of the VR wall (P8 V2a): the pure logic (filter strings, chips, edits of a wall's spec, the
 * on-screen keyboard's layout and typing) plus the 2D drawing of the keyboard and the chip strip. The wall's source
 * spec is the single truth: a wall's filters live in spec.filters, a search wall's words in spec.query, the
 * search type in spec.mode; the chips, the filter panel and the keyboard are views of it.
 *
 * Server parameters mirror the flat page exactly (web_files/js/media.js):
 *   filters   media.js FILTER_KEYS (line ~1524) and filterQuery() (~1546): kind, orientation, min_res, min_hres,
 *             min_vres, min_fps, max_fps, content - appended to /api/media/browse and /api/media/search
 *   search    media.js loadMoreSearchResults() (~2196): /api/media/search?q=&mode=[&tag=][&person=][&favorites=1]
 *             [&narrow=1]&offset=&limit=  (sources.js builds it); "*" as q lists every file ("filters only")
 *   options   GET /api/media/filters (media.js initFilters ~1757): kinds, orientations, standards, contents */
import { discoverySpec } from "./discovery.js";
import { roundRect, fitText, FONT } from "./ui.js";

// media.js FILTER_KEYS, same order (filterQuery() joins them in this order).
export const FILTER_KEYS = ["kind", "orientation", "min_res", "min_hres", "min_vres", "min_fps", "max_fps", "content", "stereo"];
// media.js SEARCH_MODE_LABELS (~1770)
export const SEARCH_MODES = [
  { key: "filename", label: "Filename" }, { key: "context", label: "Context" }, { key: "ocr", label: "Text in image" },
  { key: "people", label: "Faces" }, { key: "character", label: "Character" }, { key: "all", label: "Everything" },
];
// What the panel offers until (or unless) /api/media/filters answers; VR shows pictures and videos only.
export const DEFAULT_OPTIONS = {
  kinds: [{ key: "video", label: "Videos" }, { key: "image", label: "Images" }],
  orientations: ["horizontal", "vertical", "square"],
  standards: [{ key: "720p", label: "720p" }, { key: "fhd", label: "FHD" }, { key: "wqhd", label: "WQHD" }, { key: "4k", label: "4K" }, { key: "8k", label: "8K" }],
  contents: [],
  frame_rates: { min: [], max: [] },
};

export function normalizeOptions(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  const list = (v, fb) => (Array.isArray(v) && v.length ? v : fb);
  const kinds = list(r.kinds, DEFAULT_OPTIONS.kinds).filter((k) => k && k.key && k.key !== "music");
  const rates = (v) => (Array.isArray(v) ? v : []).filter((x) => x && x.key != null && Number(x.fps) > 0)
    .map((x) => ({ key: String(x.key), label: `${Number(x.fps)} fps`, fps: Number(x.fps) }));
  const frameRates = r.frame_rates && typeof r.frame_rates === "object" ? r.frame_rates : {};
  return {
    kinds: kinds.length ? kinds : DEFAULT_OPTIONS.kinds,
    orientations: list(r.orientations, DEFAULT_OPTIONS.orientations).filter((o) => typeof o === "string"),
    standards: list(r.standards, DEFAULT_OPTIONS.standards).filter((s) => s && s.key),
    contents: Array.isArray(r.contents) ? r.contents.filter((c) => c && c.key) : [],
    frame_rates: { min: rates(frameRates.min), max: rates(frameRates.max) },
  };
}

// ------------------------------------------------------------------ filter strings

export function parseFilters(str) {
  const out = {};
  if (!str) return out;
  let p;
  try { p = new URLSearchParams(String(str)); } catch (e) { return out; }
  for (const k of FILTER_KEYS) { const v = p.get(k); if (v) out[k] = v; }
  return out;
}

export function buildFilters(f) {
  return FILTER_KEYS.filter((k) => f && f[k]).map((k) => `${k}=${encodeURIComponent(f[k])}`).join("&");
}

export function setFilterKey(f, key, value) {
  const out = Object.assign({}, f);
  if (!value || out[key] === value) delete out[key]; else out[key] = value;     // click the active one again = clear (media.js setFilter)
  return out;
}

// A multi-value filter (content): a comma list in the options' own order (media.js toggleFilterValue).
export function toggleContent(f, value, order) {
  const chosen = new Set(String(f.content || "").split(",").filter(Boolean));
  if (chosen.has(value)) chosen.delete(value); else chosen.add(value);
  const next = order.filter((v) => chosen.has(v));
  const out = Object.assign({}, f);
  if (next.length) out.content = next.join(","); else delete out.content;
  return out;
}

// ------------------------------------------------------------------ chips

const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const labelOf = (list, key) => { const o = list.find((x) => x.key === key); return o ? o.label : key; };

export function filterChipLabel(key, value, opts) {
  switch (key) {
    case "kind": return labelOf(opts.kinds, value);
    case "orientation": return cap(value);
    case "min_res": return `Size ≥ ${labelOf(opts.standards, value)}`;
    case "min_hres": return `Width ≥ ${labelOf(opts.standards, value)}`;
    case "min_vres": return `Height ≥ ${labelOf(opts.standards, value)}`;
    case "min_fps": return `≥ ${String(value).replace(/\D/g, "") || value} fps`;
    case "max_fps": return `≤ ${String(value).replace(/\D/g, "") || value} fps`;
    case "content": return String(value).split(",").filter(Boolean).map((v) => labelOf(opts.contents, v)).join(" + ");
    case "stereo": return "3D only";
    default: return `${key}: ${value}`;
  }
}

/* The active constraints of a wall as chips: [{ id, label, all? }] - the words, "Favourites", a non-default search
 * type, every filter; "Clear all" last when there is any. Ids: "q", "fav", "mode", "f:<key>", "all". */
export function chipsFor(spec, options) {
  const opts = options || DEFAULT_OPTIONS;
  const out = [];
  if (!spec) return out;
  if (spec.kind === "people") {
    if (spec.show && spec.show !== "real") out.push({ id: "show", label: spec.show === "hidden" ? "Hidden people" : "Everyone" });
    if (spec.named) out.push({ id: "named", label: "Named only" });
    if (spec.query) out.push({ id: "q", label: `“${spec.query}”` });
    if (out.length) out.push({ id: "all", label: "Clear all", all: true });
    return out;
  }
  const searchy = spec.kind === "search" || spec.kind === "favorites";
  if (spec.person || spec.tag) out.push({ id: "subject", label: spec.label || (spec.person ? "Selected face" : "Selected character") });
  if (searchy && spec.query && spec.query !== "*") out.push({ id: "q", label: `“${spec.query}”` });
  if (spec.kind === "favorites") out.push({ id: "fav", label: "Favourites" });
  if (searchy && spec.mode && spec.mode !== "filename") out.push({ id: "mode", label: `In: ${labelOf(SEARCH_MODES, spec.mode)}` });
  const f = parseFilters(spec.filters);
  for (const k of FILTER_KEYS) if (f[k]) out.push({ id: `f:${k}`, label: filterChipLabel(k, f[k], opts) });
  if (out.length) out.push({ id: "all", label: "Clear all", all: true });
  return out;
}

export function chipEdit(id) {
  if (id === "subject") return { type: "clearSubject" };
  if (id === "q") return { type: "clearQuery" };
  if (id === "fav") return { type: "fav", on: false };
  if (id === "mode") return { type: "mode", value: "filename" };
  if (id === "show") return { type: "show", value: "real" };
  if (id === "named") return { type: "named", value: false };
  if (id === "all") return { type: "clearAll" };
  if (typeof id === "string" && id.startsWith("f:")) return { type: "clearKey", key: id.slice(2) };
  return null;
}

// ------------------------------------------------------------------ editing a wall's spec

/* planEdit(spec, edit, { depth, rootListing }) -> { op, spec?, note? }
 *   op "replace"  the current wall becomes spec (same place in the stack)
 *   op "push"     spec opens as a new wall (Back returns)
 *   op "mutate"   a root listing: only its filters change (folders opened from it inherit them)
 *   op "origin"   nothing left to ask for on a search wall: go back to where it was opened
 *   op "none"     nothing to do (note says why)
 * edit: { type: "filter", key, value } | { type: "content", value, order } | { type: "clearKey", key } | { type: "clearAll" }
 *       | { type: "clearQuery" } | { type: "fav", on } | { type: "mode", value } | { type: "sort", key?, dir? } */
export function planEdit(spec, edit, { rootListing = false } = {}) {
  if (edit.type === "mode" && edit.value === "people") return { op: "push", spec: discoverySpec("people", "", spec) };
  const kind = spec.kind;
  if (kind === "people") {
    const next = { ...spec, offset: 0 };
    if (edit.type === "show") next.show = ["real", "all", "hidden"].includes(edit.value) ? edit.value : "real";
    else if (edit.type === "named") next.named = !!edit.value;
    else if (edit.type === "clearQuery") next.query = "";
    else if (edit.type === "clearAll") { next.query = ""; next.show = "real"; next.named = false; }
    else return { op: "none" };
    return { op: "replace", spec: next };
  }
  const searchy = kind === "search" || kind === "favorites";
  let subject = {};
  for (const key of ["person", "tag", "label", "poolTag", "variant", "series", "count", "cover", "variants"]) {
    if (spec[key] !== undefined && spec[key] !== "") subject[key] = spec[key];
  }
  let f = parseFilters(spec.filters);
  let q = spec.query || "";
  let fav = kind === "favorites";
  let mode = spec.mode || "";
  let sort = spec.sort ? { key: spec.sort.key || "", dir: spec.sort.dir || "" } : { key: "", dir: "" };
  switch (edit.type) {
    case "filter": f = setFilterKey(f, edit.key, edit.value); break;
    case "content": f = toggleContent(f, edit.value, edit.order || []); break;
    case "clearKey": f = Object.assign({}, f); delete f[edit.key]; break;
    case "clearAll": f = {}; fav = false; q = ""; subject = {}; break;
    case "clearSubject": subject = {}; break;
    case "clearQuery": q = ""; break;
    case "fav": fav = !!edit.on; break;
    case "mode": mode = edit.value || "filename"; break;
    case "sort": if (edit.key !== undefined) sort.key = edit.key; if (edit.dir !== undefined) sort.dir = edit.dir; break;
    default: return { op: "none" };
  }
  const filters = buildFilters(f);
  if (searchy) {
    const narrowing = edit.type !== "mode" && edit.type !== "sort";
    if (narrowing && (!q || q === "*") && !filters && !fav && !subject.person && !subject.tag) return { op: "origin" };
    const next = { ...subject, kind: fav ? "favorites" : "search", root: "", path: "", query: fav ? (q === "*" ? "" : q) : (q || "*"), filters, sort, offset: 0, mode: mode || "filename" };
    return { op: "replace", spec: next };
  }
  if (edit.type === "fav") {
    if (!edit.on) return { op: "none" };
    return { op: "push", spec: { kind: "favorites", root: "", path: "", query: "", filters, sort: { key: "", dir: "" }, offset: 0, mode: mode || "filename" } };
  }
  if (edit.type === "clearQuery" || edit.type === "mode") return { op: "none" };
  // 3D only from Home or Recent: every 3D file in the library, as Favourites opens from anywhere.
  if (edit.type === "filter" && edit.key === "stereo" && kind !== "folder" && f.stereo) {
    return { op: "push", spec: { kind: "search", root: "", path: "", query: "*", filters, sort: { key: "", dir: "" }, offset: 0, mode: mode || "filename" } };
  }
  if (kind === "folder") {
    const next = Object.assign({}, spec, { filters, sort, offset: 0 });
    return { op: rootListing ? "mutate" : "replace", spec: next };
  }
  return { op: "none", note: "Filters apply to folders, search and favourites." };
}

/* searchSpec(base, text, mode) -> the spec Enter runs, or null when there is nothing to ask for. The filters of the
 * wall the search was opened on carry over; opened on the favourites, it searches the favourites. */
export function searchSpec(base, text, mode) {
  const q = String(text || "").trim();
  const filters = base && base.filters ? base.filters : "";
  if (mode === "people" || base && base.kind === "people") return discoverySpec("people", q, base || {});
  if (base && base.kind === "characters") return discoverySpec("characters", q, base);
  const subject = {};
  for (const key of ["person", "tag", "label", "poolTag", "variant", "series", "count", "cover", "variants"]) {
    if (base && base[key] !== undefined && base[key] !== "") subject[key] = base[key];
  }
  const fav = !!base && base.kind === "favorites";
  if (!q && !filters && !fav && !subject.person && !subject.tag) return null;
  return {
    ...subject, kind: fav ? "favorites" : "search", root: "", path: "", query: q || (fav ? "" : "*"), filters,
    sort: { key: "", dir: "" }, offset: 0, mode: mode || "filename",
  };
}

// ------------------------------------------------------------------ the filter panel (rows of option buttons)

/* filterRows(spec, options, { mode, sortable }) -> [{ id, label, options: [{ id, label, on }] }]; a button's id is
 * "<row>:<value>" (value "" = the "Any" button). */
export function filterRows(spec, options, { mode = "filename", sortable = false } = {}) {
  const opts = options || DEFAULT_OPTIONS;
  if (spec && spec.kind === "people") return [
    { id: "show", label: "People", options: [
      { id: "real", label: "Real people", on: !spec.show || spec.show === "real" },
      { id: "all", label: "Everyone", on: spec.show === "all" },
      { id: "hidden", label: "Hidden", on: spec.show === "hidden" },
    ] },
    { id: "named", label: "Names", options: [
      { id: "0", label: "All", on: !spec.named },
      { id: "1", label: "Named only", on: !!spec.named },
    ] },
  ];
  const f = parseFilters(spec && spec.filters);
  const one = (key, any, list) => [{ id: "", label: any, on: !f[key] }, ...list.map((o) => ({ id: o.key, label: o.label, on: f[key] === o.key }))];
  const rows = [
    { id: "kind", label: "Type", options: one("kind", "All", opts.kinds) },
    { id: "fav", label: "Favourites", options: [{ id: "0", label: "All", on: !(spec && spec.kind === "favorites") }, { id: "1", label: "Only", on: !!(spec && spec.kind === "favorites") }] },
    { id: "stereo", label: "3D", options: one("stereo", "All", [{ key: "1", label: "3D only" }]) },
    { id: "orientation", label: "Shape", options: one("orientation", "Any", opts.orientations.map((o) => ({ key: o, label: cap(o) }))) },
    { id: "min_res", label: "Min size", options: one("min_res", "Any", opts.standards) },
    { id: "min_hres", label: "Min width", options: one("min_hres", "Any", opts.standards) },
    { id: "min_vres", label: "Min height", options: one("min_vres", "Any", opts.standards) },
  ];
  const frameRates = opts.frame_rates || {};
  if (frameRates.min && frameRates.min.length) rows.push({ id: "min_fps", label: "At least", options: one("min_fps", "Any", frameRates.min) });
  if (frameRates.max && frameRates.max.length) rows.push({ id: "max_fps", label: "At most", options: one("max_fps", "Any", frameRates.max) });
  if (opts.contents.length) {
    const chosen = new Set(String(f.content || "").split(",").filter(Boolean));
    rows.push({ id: "content", label: "Content", options: opts.contents.map((c) => ({ id: c.key, label: c.label, on: chosen.has(c.key) })) });
  }
  rows.push({ id: "mode", label: "Search in", options: SEARCH_MODES.map((m) => ({ id: m.key, label: m.label, on: mode === m.key })) });
  if (sortable) {
    const key = spec.sort && spec.sort.key ? spec.sort.key : "name";
    const dir = spec.sort && spec.sort.dir ? spec.sort.dir : (["size", "latest"].includes(key) ? "desc" : "asc");
    rows.push({ id: "sort", label: "Sort", options: [
      { id: "name", label: "Name", on: key === "name" }, { id: "artist", label: "Artist", on: key === "artist" },
      { id: "id", label: "ID", on: key === "id" }, { id: "latest", label: "Newest", on: key === "latest" },
      { id: "size", label: "Size", on: key === "size" },
    ] });
    rows.push({ id: "dir", label: "Order", options: [{ id: "asc", label: "Ascending", on: dir === "asc" }, { id: "desc", label: "Descending", on: dir === "desc" }] });
  }
  return rows;
}

// Layout in a width x height canvas: a title strip, one line per row (label left, options right), Clear all + Close.
export function filterLayout(rows, width, height, { pad = 40 } = {}) {
  const out = [];
  const top = Math.round(height * 0.14);
  const footH = Math.round(height * 0.1);
  const rowH = Math.round((height - top - footH - pad * 1.6) / Math.max(1, rows.length));
  const labelW = Math.round(width * 0.24);
  rows.forEach((row, i) => {
    const y = top + i * rowH;
    const n = row.options.length;
    const gap = 14;
    const bw = Math.min(230, (width - 2 * pad - labelW - gap * (n - 1)) / n);
    const bh = Math.round(rowH * 0.74);
    row.options.forEach((o, k) => out.push({ id: `${row.id}:${o.id}`, label: o.label, x: pad + labelW + k * (bw + gap), y: y + (rowH - bh) / 2, w: bw, h: bh, on: o.on }));
  });
  const fw = Math.round(width * 0.26);
  out.push({ id: "clearall:", label: "Clear all", x: width / 2 - fw - 12, y: height - pad * 0.8 - footH, w: fw, h: footH, on: false });
  out.push({ id: "close", label: "Close", x: width / 2 + 12, y: height - pad * 0.8 - footH, w: fw, h: footH, on: false });
  return out;
}

// A pressed button id -> an edit (null for close).
export function filterEdit(id, options) {
  const i = String(id).indexOf(":");
  if (i < 0) return null;
  const row = id.slice(0, i), value = id.slice(i + 1);
  switch (row) {
    case "kind": case "stereo": case "orientation": case "min_res": case "min_hres": case "min_vres": case "min_fps": case "max_fps":
      return { type: "filter", key: row, value };
    case "fav": return { type: "fav", on: value === "1" };
    case "show": return { type: "show", value };
    case "named": return { type: "named", value: value === "1" };
    case "content": return { type: "content", value, order: ((options && options.contents) || []).map((c) => c.key) };
    case "mode": return { type: "mode", value };
    case "sort": return { type: "sort", key: value, dir: value === "name" ? "asc" : "desc" };
    case "dir": return { type: "sort", dir: value };
    case "clearall": return { type: "clearAll" };
    default: return null;
  }
}

// ------------------------------------------------------------------ the keyboard

const DIGITS = "1234567890";
const ROWS = {
  abc: [DIGITS, "qwertyuiop", "asdfghjkl'", "zxcvbnm,.-"],
  sym: [DIGITS, "!@#$%^&*()", "-_=+[]{};:", "\"<>/\\|?~.,"],
};
export const MAX_QUERY = 120;

// Rows of keys: [{ id, label, wu }] - wu is the key's width in units, a row is justified to the full width.
function keyRows(layer) {
  const L = layer === "sym" ? "sym" : "abc";
  const chars = ROWS[L].map((s) => Array.from(s).map((ch) => ({ id: `k:${ch}`, label: ch, wu: 1 })));
  chars[0].push({ id: "bksp", label: "⌫", wu: 1.8 });
  const bottom = [
    { id: "layer", label: L === "abc" ? "?123" : "abc", wu: 1.6 },
    { id: "space", label: "space", wu: 4.4 },
    { id: "clear", label: "Clear", wu: 1.6 },
    { id: "enter", label: "Search", wu: 2 },
    { id: "close", label: "Close", wu: 1.6 },
  ];
  return [...chars, bottom];
}

/* keyboardLayout(layer, width, height) -> { field: {x,y,w,h}, keys: [{ id, label, x, y, w, h }] } */
export function keyboardLayout(layer, width, height, { pad = 30, gap = 10, suggestions = 0 } = {}) {
  const rows = keyRows(layer);
  const fieldH = Math.round(height * (suggestions ? 0.27 : 0.14));
  const field = { x: pad, y: pad * 0.8, w: width - 2 * pad, h: fieldH };
  const top = field.y + fieldH + 22;
  const rowH = Math.floor((height - top - pad * 0.6 - gap * (rows.length - 1)) / rows.length);
  const keys = [];
  rows.forEach((row, r) => {
    const units = row.reduce((a, k) => a + k.wu, 0);
    const unit = (width - 2 * pad - gap * (row.length - 1)) / units;
    let x = pad;
    for (const k of row) {
      const w = unit * k.wu;
      keys.push({ id: k.id, label: k.label, x, y: top + r * (rowH + gap), w, h: rowH });
      x += w + gap;
    }
  });
  const suggestionButtons = [];
  const count = Math.min(8, Math.max(0, suggestions | 0));
  if (count) {
    const cols = Math.min(4, count), nrows = Math.ceil(count / cols), sgap = 8;
    const left = field.x + 18, totalW = field.w - 36, buttonH = (field.h * 0.48 - sgap * (nrows - 1)) / nrows;
    const buttonW = (totalW - sgap * (cols - 1)) / cols;
    for (let i = 0; i < count; i += 1) suggestionButtons.push({ id: `suggest:${i}`, x: left + (i % cols) * (buttonW + sgap),
      y: field.y + field.h * 0.5 + Math.floor(i / cols) * (buttonH + sgap), w: buttonW, h: buttonH });
  }
  return { field, keys, suggestions: suggestionButtons };
}

const flat = (s) => String(s).replace(/[\r\n\t]+/g, " ");

// One key id -> the new text (bksp removes a whole code point, so a pasted emoji or CJK character goes in one press).
export function typeKey(text, id) {
  const t = String(text || "");
  if (id === "bksp") return Array.from(t).slice(0, -1).join("");
  if (id === "clear") return "";
  if (id === "space") return t.length < MAX_QUERY ? `${t} ` : t;
  if (typeof id === "string" && id.startsWith("k:")) return t.length < MAX_QUERY ? t + id.slice(2) : t;
  return t;
}

export function pasteText(text, pasted) {
  return (String(text || "") + flat(pasted)).slice(0, MAX_QUERY);
}

/* A desktop / Bluetooth keyboard event -> { op: "char"|"bksp"|"enter"|"close"|"clear"|null, ch? }.
 * Ctrl/Meta/Alt combinations are left alone (Ctrl+V arrives as a paste event). */
export function keyAction(e) {
  if (!e) return { op: null };
  const key = e.key;
  if (e.ctrlKey || e.metaKey || e.altKey) return { op: null };
  if (key === "Backspace") return { op: "bksp" };
  if (key === "Enter") return { op: "enter" };
  if (key === "Escape") return { op: "close" };
  if (key === "Delete") return { op: "clear" };
  if (typeof key === "string" && Array.from(key).length === 1) return { op: "char", ch: key };
  return { op: null };
}

export function applyKeyAction(text, a) {
  if (a.op === "char") return text.length < MAX_QUERY ? text + a.ch : text;
  if (a.op === "bksp") return typeKey(text, "bksp");
  if (a.op === "clear") return "";
  return text;
}

// ------------------------------------------------------------------ drawing

export function drawKeyboard(ctx, w, h, { layout, text, hover, suggestions = [], suggestionHover = null }) {
  ctx.fillStyle = "rgba(18,20,28,0.96)";
  roundRect(ctx, 0, 0, w, h, 36);
  ctx.fill();
  ctx.strokeStyle = "rgba(120,130,160,0.55)";
  ctx.lineWidth = 3;
  roundRect(ctx, 2, 2, w - 4, h - 4, 34);
  ctx.stroke();
  const f = layout.field;
  ctx.fillStyle = "rgba(8,10,16,0.95)";
  roundRect(ctx, f.x, f.y, f.w, f.h, 20);
  ctx.fill();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  const fs = Math.round(f.h * (suggestions.length ? 0.32 : 0.5));
  ctx.font = `500 ${fs}px ${FONT}`;
  const maxW = f.w - 60;
  let shown = String(text || "");
  if (ctx.measureText(shown).width > maxW) {
    const chars = Array.from(shown);
    while (chars.length > 1 && ctx.measureText(`…${chars.join("")}`).width > maxW) chars.shift();
    shown = `…${chars.join("")}`;
  }
  const inputY = f.y + (suggestions.length ? f.h * 0.27 : f.h / 2);
  if (text) { ctx.fillStyle = "#f2f4fa"; ctx.fillText(shown, f.x + 28, inputY + 2); }
  else { ctx.fillStyle = "#6c748f"; ctx.fillText("Search the library...", f.x + 28, inputY + 2); }
  for (let i = 0; i < layout.suggestions.length; i += 1) {
    const b = layout.suggestions[i], pool = suggestions[i];
    if (!pool) continue;
    ctx.fillStyle = suggestionHover === b.id ? "rgba(120,165,255,0.98)" : "rgba(52,58,78,0.95)";
    roundRect(ctx, b.x, b.y, b.w, b.h, 12); ctx.fill();
    ctx.textAlign = "center"; ctx.fillStyle = "#ffffff"; ctx.font = `600 ${Math.max(20, Math.round(b.h * 0.44))}px ${FONT}`;
    ctx.fillText(fitText(ctx, pool.label || pool.tag, b.w - 16), b.x + b.w / 2, b.y + b.h / 2);
  }
  ctx.textAlign = "center";
  for (const k of layout.keys) {
    const on = hover === k.id;
    const accent = k.id === "enter";
    ctx.fillStyle = on ? "rgba(120,165,255,0.98)" : (accent ? "rgba(70,120,230,0.95)" : (k.id.startsWith("k:") ? "rgba(52,58,78,0.95)" : "rgba(40,45,62,0.95)"));
    roundRect(ctx, k.x, k.y, k.w, k.h, 18);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.font = `${k.id.startsWith("k:") ? 600 : 500} ${Math.round(k.h * (k.id.startsWith("k:") ? 0.5 : 0.36))}px ${FONT}`;
    ctx.fillText(fitText(ctx, k.label, k.w - 12), k.x + k.w / 2, k.y + k.h / 2 + 2);
  }
}

export const CHIP_FONT_PX = 38;

/* chipsLayout(chips, ctx, width, height) -> [{ id, label, all, x, y, w, h }]; the font must be the draw font
 * (chipsFont()). "Clear all" is anchored at the right end so it can never be pushed out. */
export const chipsFont = () => `600 ${CHIP_FONT_PX}px ${FONT}`;
export function chipsLayout(chips, ctx, width, height, { pad = 24, gap = 16, maxLabel = 420 } = {}) {
  ctx.font = chipsFont();
  const bh = Math.round(height * 0.74), y = (height - bh) / 2;
  const out = [];
  const widthOf = (c) => Math.min(ctx.measureText(c.label).width, maxLabel) + 2 * 28 + (c.all ? 0 : 46);
  const all = chips.find((c) => c.all);
  let right = width - pad;
  if (all) { const bw = widthOf(all); out.push({ id: all.id, label: all.label, all: true, x: right - bw, y, w: bw, h: bh }); right -= bw + gap * 2; }
  let x = pad;
  for (const c of chips) {
    if (c.all) continue;
    const bw = widthOf(c);
    if (x + bw > right) break;
    out.push({ id: c.id, label: c.label, all: false, x, y, w: bw, h: bh });
    x += bw + gap;
  }
  return out;
}

export function drawChips(ctx, w, h, { layout, hover }) {
  ctx.fillStyle = "rgba(18,20,28,0.9)";
  roundRect(ctx, 0, 0, w, h, h * 0.4);
  ctx.fill();
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  ctx.font = chipsFont();
  for (const c of layout) {
    const on = hover === c.id;
    ctx.fillStyle = on ? "rgba(120,165,255,0.98)" : (c.all ? "rgba(120,60,70,0.95)" : "rgba(52,70,120,0.95)");
    roundRect(ctx, c.x, c.y, c.w, c.h, c.h / 2);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.fillText(fitText(ctx, c.label, c.w - 56 - (c.all ? 0 : 36)), c.x + 28, c.y + c.h / 2 + 2);
    if (!c.all) { ctx.textAlign = "right"; ctx.fillText("×", c.x + c.w - 22, c.y + c.h / 2 + 2); ctx.textAlign = "left"; }
  }
}
