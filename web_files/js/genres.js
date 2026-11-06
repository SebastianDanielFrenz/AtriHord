// The genre map (genres.html): every analysed track as a point, placed so
// that tracks the genre model hears alike sit together. genre_map.py lays the
// map out on the server (t-SNE over the Discogs-EffNet embeddings, KSF-042);
// this page draws it, 2D or 3D, on one canvas.
//
// Drag pans (2D) or turns (3D), the wheel or a pinch zooms, WASD moves - in 3D
// the first press switches to first person, flying among the tracks until
// Reset view - a click plays a track in the bar at the bottom, and "Keep
// playing nearby" carries on with the closest track not played yet when one
// ends: a walk through genre space.
'use strict';

const $ = (id) => document.getElementById(id);
const canvas = $('genresCanvas');
const ctx = canvas.getContext('2d');
const wrap = $('genresCanvasWrap');
const audio = $('genresAudio');
const enc = encodeURIComponent;

const MAP_ROUTE = '/api/media/genre-map';
const CAM2 = { x: 0, y: 0, k: 1 };
// The 3D camera looks at, and turns about, the point t (map coordinates) from
// dist away; x/y shift the picture on screen (Shift-drag).
const CAM3 = { yaw: 0.7, pitch: 0.35, dist: 3.4, x: 0, y: 0, t: [0, 0, 0] };
const freshCam3 = () => ({ ...CAM3, t: [...CAM3.t] });
const MORPH_MS = 900;          // a rebuilt map glides into place
const SPIN = 0.00012;          // rad per ms while the 3D view turns by itself
const NEAR = 0.05;             // points closer to the camera than this are not drawn
const ZOOM3 = [0.2, 12];       // camera distance: close enough to fly among the tracks
// First person: the camera turns about itself and looks at a point FP_DIST
// ahead. Point size, speed and steps follow sizeRef(): the camera's distance
// from the middle of the map, never under FP_DIST.
const FP_DIST = 1.0;
const MOVE_FP = 0.35;          // sizeRef()s per second

const S = {
  tracks: [],                  // {id, root, path, size, name, folder, g, s, p2, p3, hay}
  parents: [],
  styles: [],
  order: [],                   // parent indices, biggest first
  counts: [],                  // tracks per parent
  groupColour: [],
  hidden: new Set(),           // parents switched off in the legend
  solo: -1,                    // a parent shown alone: its points coloured by style
  styleColour: new Map(),
  matches: null,               // track indices the search found, or null
  hover: -1,
  current: -1,
  played: new Set(),
  flash: null,                 // {i, until}: a ring that draws the eye to a track
  view: '2d',
  cam2: { ...CAM2 },
  cam3: freshCam3(),
  spinning: true,
  fp: false,                   // first person (since the first WASD in 3D), else orbiting the target
  built: null,
  morph: null,                 // {start, from: Map id -> {p2, p3}}
  screen: new Float32Array(0), // x, y, radius, drawn (1/0) per track, as last drawn
  labels: { '2d': [], '3d': [] },
  w: 0,
  h: 0,
  dpr: 1,
  colours: null,
  light: false,
  dirty: true,
  last: 0,
  poll: null,
};

// ---------------------------------------------------------------- colours --

function luminance(colour) {
  ctx.fillStyle = '#000';
  ctx.fillStyle = colour;
  const hex = ctx.fillStyle;   // the canvas normalises any CSS colour to #rrggbb
  if (!/^#[0-9a-f]{6}$/i.test(hex)) return 0;
  const n = parseInt(hex.slice(1), 16);
  return (0.2126 * (n >> 16) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255;
}

function readColours() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (cs.getPropertyValue(name) || '').trim() || fallback;
  S.colours = {
    plane: v('--surface', '#1a1a19'),
    ink: v('--ink', '#e6e4db'),
    muted: v('--muted', '#898781'),
    faint: v('--faint', '#605f59'),
    line: v('--line', '#2c2c2a'),
    accent: v('--accent', '#3987e5'),
    // atrihord.css's own values as the fallback: without them every genre would
    // fall back to the same grey.
    series: ['#3987e5', '#199e70', '#c98500', '#e66767', '#9085e9'].map((hex, i) => v(`--series-${i + 1}`, hex)),
    font: getComputedStyle(document.body).fontFamily || 'sans-serif',
  };
  S.light = luminance(S.colours.plane) > 0.5;
}

// The design language's five series colours first, for the biggest groups
// (atrihord.css: "use --series-1 first"). Fifteen genres need fifteen colours, so
// past five come evenly spaced hues at about the series colours' lightness.
function categoryColours(n) {
  const out = S.colours.series.slice(0, n);
  const extra = n - out.length;
  for (let i = 0; i < extra; i++) {
    const hue = Math.round((30 + (i * 360) / extra) % 360);
    out.push(S.light ? `hsl(${hue}, 48%, 44%)` : `hsl(${hue}, 45%, 62%)`);
  }
  return out;
}

function assignColours() {
  const palette = categoryColours(S.order.length);
  S.groupColour = [];
  S.order.forEach((g, rank) => { S.groupColour[g] = palette[rank]; });
  S.styleColour = new Map();
  if (S.solo >= 0) {
    const counts = styleCounts(S.solo);
    const shown = counts.slice(0, 10);
    const pal = categoryColours(shown.length);
    shown.forEach(([si], rank) => S.styleColour.set(si, pal[rank]));
  }
  sprites.clear();
}

// A filled circle per colour, drawn once and stamped: thousands of arcs a
// frame are what makes a canvas scatter stutter.
const sprites = new Map();
const SPRITE_R = 12;
function sprite(colour) {
  let c = sprites.get(colour);
  if (!c) {
    c = document.createElement('canvas');
    c.width = c.height = SPRITE_R * 2 + 2;
    const g = c.getContext('2d');
    g.fillStyle = colour;
    g.beginPath();
    g.arc(SPRITE_R + 1, SPRITE_R + 1, SPRITE_R, 0, Math.PI * 2);
    g.fill();
    sprites.set(colour, c);
  }
  return c;
}

function colourOf(i) {
  const t = S.tracks[i];
  if (S.solo >= 0) {
    const top = t.s.length ? t.s[0][0] : -1;
    return S.styleColour.get(top) || S.colours.faint;
  }
  return S.groupColour[t.g] || S.colours.faint;
}

// ------------------------------------------------------------------- data --

const styleName = (si) => { const s = S.styles[si] || ''; return s.includes('---') ? s.split('---')[1] : s; };
const styleText = (t) => t.s.map(([si, p]) => `${styleName(si)} ${Math.round(p * 100)}%`).join(' · ');
const fileUrl = (t) => `/api/media/file?root=${enc(t.root)}&path=${enc(t.path)}`;
const thumbUrl = (t) => `/api/media/thumbnail?path=${enc(t.path)}&root=${enc(t.root)}&v=${t.size || 0}`;
function libraryUrl(t) {
  const cut = t.path.lastIndexOf('/');
  const dir = cut >= 0 ? t.path.slice(0, cut) : '';
  return `/media.html?path=${enc(dir)}&root=${enc(t.root)}&file=${enc(t.path)}&fileRoot=${enc(t.root)}`;
}

function styleCounts(g) {
  const n = new Map();
  for (const t of S.tracks) {
    if (t.g === g && t.s.length) n.set(t.s[0][0], (n.get(t.s[0][0]) || 0) + 1);
  }
  return [...n.entries()].sort((a, b) => b[1] - a[1]);
}

function ingest(data) {
  const before = new Map(S.tracks.map((t) => [t.id, { p2: t.p2, p3: t.p3 }]));
  const currentId = S.current >= 0 ? S.tracks[S.current].id : null;
  const hiddenNames = new Set([...S.hidden].map((g) => S.parents[g]));
  const soloName = S.solo >= 0 ? S.parents[S.solo] : null;
  const playedIds = new Set([...S.played].map((i) => S.tracks[i].id));

  S.parents = data.parents || [];
  S.styles = data.styles || [];
  const roots = data.roots || [];
  S.tracks = (data.tracks || []).map(([id, ri, path, size, g, s, p2, p3]) => {
    const cut = path.lastIndexOf('/');
    const file = cut >= 0 ? path.slice(cut + 1) : path;
    const name = file.replace(/\.[^.]+$/, '');
    const folder = cut >= 0 ? path.slice(0, cut) : '';
    const t = { id, root: (roots[ri] || {}).id || '', path, size, name, folder, g, s, p2, p3 };
    t.hay = [name, folder, S.parents[g] || '', ...s.map(([si]) => S.styles[si] || '')]
      .join(' ').toLowerCase().replace(/---/g, ' ');
    return t;
  });
  S.counts = S.parents.map(() => 0);
  for (const t of S.tracks) S.counts[t.g] += 1;
  S.order = S.parents.map((_, g) => g).filter((g) => S.counts[g]).sort((a, b) => S.counts[b] - S.counts[a]);
  S.hidden = new Set(S.parents.map((p, g) => (hiddenNames.has(p) ? g : -1)).filter((g) => g >= 0));
  S.solo = soloName ? S.parents.indexOf(soloName) : -1;
  S.played = new Set(S.tracks.map((t, i) => (playedIds.has(t.id) ? i : -1)).filter((i) => i >= 0));
  S.current = currentId === null ? -1 : S.tracks.findIndex((t) => t.id === currentId);
  S.hover = -1;
  S.screen = new Float32Array(S.tracks.length * 4);
  if (before.size && S.built !== null) S.morph = { start: performance.now(), from: before };
  S.built = data.built;
  assignColours();
  computeLabels();
  applySearch($('genresSearch').value);
  renderLegend();
  S.dirty = true;
}

function summary(data) {
  const n = (data.tracks || []).length;
  const fmt = (x) => Number(x || 0).toLocaleString();
  const parts = [];
  if (n) parts.push(`${fmt(n)} tracks, placed by how they sound: neighbours sound alike`);
  if (data.music) parts.push(`${fmt(data.analysed)} of ${fmt(data.music)} analysed`);
  if (data.building) parts.push(n ? 'updating the map…' : 'laying out the map…');
  $('genresSummary').textContent = parts.join(' · ') || 'No music analysed yet';
  $('genresSummary').title = data.method ? `2D: ${data.method['2d']}\n3D: ${data.method['3d']}` : '';

  const empty = $('genresEmpty');
  if (n) {
    empty.hidden = true;
  } else {
    empty.hidden = false;
    if (data.error) empty.textContent = `The map could not be built: ${data.error}`;
    else if (data.building || data.stale) empty.textContent = `Laying out the map for ${fmt(data.analysed)} tracks… this takes a minute or two.`;
    else if (!data.analysed) empty.textContent = data.music
      ? `No music analysed yet. The genre pass is working through ${fmt(data.music)} tracks; the map fills in as it goes.`
      : 'There is no music in the libraries you can see.';
    else empty.textContent = 'The map is empty.';
  }
}

async function load() {
  clearTimeout(S.poll);
  let data;
  try {
    const res = await fetch(MAP_ROUTE, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    if (!S.tracks.length) {
      $('genresEmpty').hidden = false;
      $('genresEmpty').textContent = `Could not load the genre map (${e.message}).`;
    }
    S.poll = setTimeout(load, 30000);
    return;
  }
  const first = S.built === null;
  if (data.built !== S.built || first) ingest(data);
  summary(data);
  if (first) focusFromUrl();
  if (data.building || data.stale) S.poll = setTimeout(load, S.tracks.length ? 20000 : 5000);
}

// ----------------------------------------------------------------- labels --

// Where a group is densest: the fullest cell of a coarse grid, and the mean
// position inside it. A genre in two islands is labelled on the bigger one,
// which its median would miss by landing in the sea between them.
function densest(points, dims) {
  const cells = dims === 2 ? 18 : 8;
  const bins = new Map();
  let best = null;
  for (const p of points) {
    let key = 0;
    for (let d = 0; d < dims; d++) {
      key = key * cells + Math.max(0, Math.min(cells - 1, Math.floor(((p[d] + 1.25) / 2.5) * cells)));
    }
    let b = bins.get(key);
    if (!b) { b = { n: 0, sum: new Array(dims).fill(0) }; bins.set(key, b); }
    b.n += 1;
    for (let d = 0; d < dims; d++) b.sum[d] += p[d];
    if (!best || b.n > best.n) best = b;
  }
  return best ? best.sum.map((x) => x / best.n) : null;
}

function computeLabels() {
  for (const view of ['2d', '3d']) {
    const key = view === '2d' ? 'p2' : 'p3';
    const dims = view === '2d' ? 2 : 3;
    const out = [];
    const minGroup = Math.max(4, S.tracks.length * 0.01);
    for (const g of S.order) {
      if (S.counts[g] < minGroup) continue;
      const pos = densest(S.tracks.filter((t) => t.g === g).map((t) => t[key]), dims);
      if (pos) out.push({ text: S.parents[g], pos, g, size: S.counts[g], kind: 'genre' });
    }
    // Styles, shown once zoomed in: the sub-genres inside a genre's island.
    const byStyle = new Map();
    for (const t of S.tracks) {
      if (!t.s.length) continue;
      const si = t.s[0][0];
      if (!byStyle.has(si)) byStyle.set(si, []);
      byStyle.get(si).push(t);
    }
    for (const [si, list] of byStyle) {
      if (list.length < 6) continue;
      const pos = densest(list.map((t) => t[key]), dims);
      if (pos) out.push({ text: styleName(si), pos, g: list[0].g, si, size: list.length, kind: 'style' });
    }
    out.sort((a, b) => b.size - a.size);
    S.labels[view] = out;
  }
}

// --------------------------------------------------------------- geometry --

function position(i, key) {
  const t = S.tracks[i];
  if (!S.morph) return t[key];
  const from = S.morph.from.get(t.id);
  const a = Math.min(1, (performance.now() - S.morph.start) / MORPH_MS);
  if (!from || a >= 1) return t[key];
  const e = a * a * (3 - 2 * a);
  return t[key].map((v, d) => from[key][d] + (v - from[key][d]) * e);
}

function scale2() { return Math.min(S.w, S.h) * 0.46 * S.cam2.k; }

function project2(p) {
  const s = scale2();
  return [S.w / 2 + (p[0] - S.cam2.x) * s, S.h / 2 - (p[1] - S.cam2.y) * s, 1];
}

function focal() { return Math.min(S.w, S.h) * 0.9; }

// Relative to the target, turn by yaw (about the vertical axis), then pitch
// (about the horizontal one); the camera sits on +z at cam3.dist, looking at
// the target.
function project3(p) {
  const { yaw, pitch, dist, t } = S.cam3;
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  const px = p[0] - t[0];
  const py = p[1] - t[1];
  const pz = p[2] - t[2];
  const x = px * cy + pz * sy;
  const z0 = -px * sy + pz * cy;
  const y = py * cp - z0 * sp;
  const z = py * sp + z0 * cp;
  const d = dist - z;
  if (d < NEAR) return null;
  const persp = focal() / d;
  return [S.w / 2 + S.cam3.x + x * persp, S.h / 2 + S.cam3.y - y * persp, 1 / d, z];
}

const visible = (i) => !S.hidden.has(S.tracks[i].g);

// ------------------------------------------------------------------ frame --

function resize() {
  const r = wrap.getBoundingClientRect();
  S.dpr = window.devicePixelRatio || 1;
  S.w = Math.max(1, r.width);
  S.h = Math.max(1, r.height);
  canvas.width = Math.round(S.w * S.dpr);
  canvas.height = Math.round(S.h * S.dpr);
  S.dirty = true;
}

function drawCube() {
  const c = [-1.1, 1.1];
  const corners = [];
  for (const x of c) for (const y of c) for (const z of c) corners.push(project3([x, y, z]));
  ctx.strokeStyle = S.colours.line;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let a = 0; a < 8; a++) {
    for (let b = a + 1; b < 8; b++) {
      const diff = (a ^ b);
      if (diff !== 1 && diff !== 2 && diff !== 4) continue;   // edges differ in one coordinate
      if (!corners[a] || !corners[b]) continue;
      ctx.moveTo(corners[a][0], corners[a][1]);
      ctx.lineTo(corners[b][0], corners[b][1]);
    }
  }
  ctx.stroke();
}

function draw(now) {
  const { colours } = S;
  ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  ctx.clearRect(0, 0, S.w, S.h);
  const n = S.tracks.length;
  if (!n) return;
  const is3 = S.view === '3d';
  if (is3) drawCube();

  const key = is3 ? 'p3' : 'p2';
  const base = is3 ? 2.6 : Math.max(2, Math.min(5.5, 2.4 + 0.9 * Math.log2(S.cam2.k)));
  const ref = is3 ? sizeRef() : 1;     // the depth at which a point has its base size
  const order = [];
  const depth = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    S.screen[i * 4 + 3] = 0;
    if (!visible(i)) continue;
    const q = is3 ? project3(position(i, key)) : project2(position(i, key));
    if (!q) continue;
    const r = is3 ? Math.max(1.2, Math.min(7, base * ref * q[2])) : base;
    if (q[0] < -r || q[1] < -r || q[0] > S.w + r || q[1] > S.h + r) continue;
    S.screen[i * 4] = q[0];
    S.screen[i * 4 + 1] = q[1];
    S.screen[i * 4 + 2] = r;
    S.screen[i * 4 + 3] = 1;
    depth[i] = is3 ? q[3] : 0;
    order.push(i);
  }
  if (is3) order.sort((a, b) => depth[a] - depth[b]);   // far first

  const searching = !!S.matches;
  const stamp = (i, alpha) => {
    const x = S.screen[i * 4];
    const y = S.screen[i * 4 + 1];
    const r = S.screen[i * 4 + 2];
    ctx.globalAlpha = alpha;
    ctx.drawImage(sprite(colourOf(i)), x - r, y - r, r * 2, r * 2);
  };
  // What the search did not find goes first and faint; what it found on top.
  for (const i of order) {
    if (searching && !S.matches.has(i)) stamp(i, 0.12);
  }
  for (const i of order) {
    if (!searching || S.matches.has(i)) stamp(i, is3 ? 0.75 + 0.25 * Math.max(-1, Math.min(1, depth[i])) : 0.88);
  }
  ctx.globalAlpha = 1;

  drawLabels(is3);

  const ring = (i, colour, extra, width) => {
    if (i < 0 || !S.screen[i * 4 + 3]) return;
    ctx.strokeStyle = colour;
    ctx.lineWidth = width;
    ctx.beginPath();
    ctx.arc(S.screen[i * 4], S.screen[i * 4 + 1], S.screen[i * 4 + 2] + extra, 0, Math.PI * 2);
    ctx.stroke();
  };
  if (S.flash && now < S.flash.until) {
    const phase = ((S.flash.until - now) % 900) / 900;
    ring(S.flash.i, colours.accent, 4 + 10 * phase, 2);
  }
  ring(S.current, colours.ink, 3, 2);
  ring(S.hover, colours.accent, 2.5, 2);
}

function drawLabels(is3) {
  const labels = S.labels[S.view];
  if (!labels.length) return;
  const zoomedIn = is3 ? S.cam3.dist < 2.4 : S.cam2.k >= 2.2;
  const taken = [];
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.lineJoin = 'round';
  for (const l of labels) {
    if (S.hidden.has(l.g)) continue;
    if (l.kind === 'style' && !zoomedIn && !(S.solo >= 0 && l.g === S.solo)) continue;
    if (l.kind === 'genre' && S.solo >= 0) continue;   // one genre: its styles say more
    const q = is3 ? project3(l.pos) : project2(l.pos);
    if (!q || q[0] < 0 || q[1] < 0 || q[0] > S.w || q[1] > S.h) continue;
    const size = l.kind === 'genre' ? 14 : 11.5;
    ctx.font = `${l.kind === 'genre' ? 600 : 500} ${size}px ${S.colours.font}`;
    const w = ctx.measureText(l.text).width + 8;
    const box = [q[0] - w / 2, q[1] - size / 2 - 3, q[0] + w / 2, q[1] + size / 2 + 3];
    if (taken.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
    taken.push(box);
    ctx.lineWidth = 4;
    ctx.strokeStyle = S.colours.plane;
    ctx.globalAlpha = 0.85;
    ctx.strokeText(l.text, q[0], q[1]);
    ctx.globalAlpha = 1;
    ctx.fillStyle = l.kind === 'genre' ? S.colours.ink : S.colours.muted;
    ctx.fillText(l.text, q[0], q[1]);
  }
}

function tick(now) {
  const dt = S.last ? Math.min(100, now - S.last) : 16;
  S.last = now;
  if (held.size) fly(dt);
  if (S.view === '3d' && S.spinning && !S.fp && S.tracks.length) {
    S.cam3.yaw += SPIN * dt;
    S.dirty = true;
  }
  if (S.morph) {
    S.dirty = true;
    if (now - S.morph.start > MORPH_MS) S.morph = null;
  }
  if (S.flash) {
    S.dirty = true;
    if (now > S.flash.until) S.flash = null;
  }
  if (S.dirty) {
    S.dirty = false;
    draw(now);
  }
  requestAnimationFrame(tick);
}

// ------------------------------------------------------------ interaction --

function nearestOnScreen(x, y) {
  let best = -1;
  let bestD = Infinity;
  for (let i = 0; i < S.tracks.length; i++) {
    if (!S.screen[i * 4 + 3]) continue;
    if (S.matches && !S.matches.has(i)) continue;
    const dx = S.screen[i * 4] - x;
    const dy = S.screen[i * 4 + 1] - y;
    const reach = Math.max(7, S.screen[i * 4 + 2] + 3);
    const d = dx * dx + dy * dy;
    if (d <= reach * reach && d < bestD) { bestD = d; best = i; }
  }
  return best;
}

function showTip(i, x, y) {
  const tip = $('genresTip');
  if (i < 0) { tip.hidden = true; return; }
  const t = S.tracks[i];
  tip.innerHTML = '';
  const img = document.createElement('img');
  img.alt = '';
  img.src = thumbUrl(t);
  img.addEventListener('error', () => img.remove());
  const text = document.createElement('div');
  const title = document.createElement('div');
  title.className = 'genres-tip-title';
  title.textContent = t.name;
  const folder = document.createElement('div');
  folder.className = 'genres-tip-line';
  folder.textContent = t.folder;
  const styles = document.createElement('div');
  styles.className = 'genres-tip-line';
  styles.textContent = [S.parents[t.g], styleText(t)].filter(Boolean).join(': ');
  text.append(title, folder, styles);
  tip.append(img, text);
  tip.hidden = false;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  tip.style.left = `${Math.max(4, Math.min(S.w - tw - 4, x + 14))}px`;
  tip.style.top = `${y + 14 + th > S.h ? Math.max(4, y - th - 10) : y + 14}px`;
}

function setHover(i, x, y) {
  if (i !== S.hover) {
    S.hover = i;
    S.dirty = true;
    canvas.classList.toggle('pointing', i >= 0);
  }
  showTip(i, x, y);
}

const pointers = new Map();
let drag = null;   // {x, y, moved, mode, pinch}

function localXY(e) {
  const r = canvas.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

function zoomAt(x, y, factor) {
  if (S.view === '2d') {
    const s0 = scale2();
    const mx = S.cam2.x + (x - S.w / 2) / s0;
    const my = S.cam2.y - (y - S.h / 2) / s0;
    S.cam2.k = Math.max(0.5, Math.min(80, S.cam2.k * factor));
    const s1 = scale2();
    S.cam2.x = mx - (x - S.w / 2) / s1;
    S.cam2.y = my + (y - S.h / 2) / s1;
  } else if (S.fp) {
    const { ahead } = axes3();
    const step = Math.log(factor) * sizeRef();
    for (let d = 0; d < 3; d++) S.cam3.t[d] += ahead[d] * step;
  } else {
    S.cam3.dist = Math.max(ZOOM3[0], Math.min(ZOOM3[1], S.cam3.dist / factor));
  }
  S.dirty = true;
}

// The camera's own axes in map coordinates: project3's turn, undone.
function axes3() {
  const cy = Math.cos(S.cam3.yaw);
  const sy = Math.sin(S.cam3.yaw);
  const cp = Math.cos(S.cam3.pitch);
  const sp = Math.sin(S.cam3.pitch);
  return { ahead: [cp * sy, -sp, -cp * cy], right: [cy, 0, sy], up: [sp * sy, cp, -sp * cy] };
}

// Where the camera is: dist back from what it looks at.
function cameraAt() {
  const { ahead } = axes3();
  return S.cam3.t.map((v, d) => v - ahead[d] * S.cam3.dist);
}

// Orbiting: the distance to what the camera turns about. First person: the
// distance to the middle of the map, never under FP_DIST - the same number at
// the moment of the switch (orbiting the middle), and smaller as the camera
// flies in, so steps get finer among the tracks.
function sizeRef() {
  if (!S.fp) return S.cam3.dist;
  return Math.max(FP_DIST, Math.min(ZOOM3[1], Math.hypot(...cameraAt())));
}

// From orbiting to first person. The camera stays where it is and keeps its
// direction, so nothing on screen moves; only what it turns about changes.
function firstPerson() {
  if (S.fp) return;
  const at = cameraAt();
  const { ahead } = axes3();
  S.cam3.dist = FP_DIST;
  S.cam3.t = at.map((v, d) => v + ahead[d] * FP_DIST);
  S.fp = true;
  S.spinning = false;
  showHint();
}

canvas.addEventListener('pointerdown', (e) => {
  try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* a pointer already gone */ }
  const [x, y] = localXY(e);
  pointers.set(e.pointerId, [x, y]);
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    drag = { pinch: Math.hypot(a[0] - b[0], a[1] - b[1]), mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], moved: true };
  } else {
    drag = { x, y, moved: false, pan: e.shiftKey || e.button === 2 };
  }
});

canvas.addEventListener('pointermove', (e) => {
  const [x, y] = localXY(e);
  if (!pointers.has(e.pointerId)) {
    if (e.pointerType === 'mouse') setHover(nearestOnScreen(x, y), x, y);
    return;
  }
  pointers.set(e.pointerId, [x, y]);
  if (!drag) return;
  if (pointers.size >= 2 && drag.pinch) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
    const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    zoomAt(mid[0], mid[1], d / drag.pinch);
    panBy(mid[0] - drag.mid[0], mid[1] - drag.mid[1], true);
    drag.pinch = d;
    drag.mid = mid;
    return;
  }
  const dx = x - drag.x;
  const dy = y - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 5) return;
  if (!drag.moved) {
    drag.moved = true;
    canvas.classList.add('dragging');
    setHover(-1, 0, 0);
  }
  // Turning or moving the view by hand ends its turning by itself; zooming
  // (wheel, pinch) and clicking a track do not.
  S.spinning = false;
  panBy(dx, dy, drag.pan);
  drag.x = x;
  drag.y = y;
});

function panBy(dx, dy, pan) {
  if (S.view === '2d') {
    const s = scale2();
    S.cam2.x -= dx / s;
    S.cam2.y += dy / s;
  } else if (S.fp) {
    // First person, and the world is grabbed as in 2D: what is under the
    // pointer follows it. A drag looks around (the camera turns about
    // itself); Shift or the right button slides the camera instead.
    const k = 1 / focal();
    if (pan) {
      const { right, up } = axes3();
      const s = sizeRef() * k;
      for (let d = 0; d < 3; d++) S.cam3.t[d] += (-right[d] * dx + up[d] * dy) * s;
    } else {
      const at = cameraAt();
      // Yaw turns about the map's vertical; looking up or down, that moves the
      // picture sideways by only cos(pitch) of the angle. Divided by it (at
      // most twice, for steep views), the grabbed point stays under the pointer.
      S.cam3.yaw -= (dx * k) / Math.max(0.5, Math.cos(S.cam3.pitch));
      S.cam3.pitch = Math.max(-1.45, Math.min(1.45, S.cam3.pitch - dy * k));
      const { ahead } = axes3();
      S.cam3.t = at.map((v, d) => v + ahead[d] * S.cam3.dist);
    }
  } else if (pan) {
    S.cam3.x += dx;
    S.cam3.y += dy;
  } else {
    S.cam3.yaw += dx * 0.006;
    S.cam3.pitch = Math.max(-1.45, Math.min(1.45, S.cam3.pitch + dy * 0.006));
  }
  S.dirty = true;
}

function endPointer(e) {
  if (!pointers.has(e.pointerId)) return;
  const [x, y] = pointers.get(e.pointerId);
  pointers.delete(e.pointerId);
  canvas.classList.remove('dragging');
  if (drag && !drag.moved && e.type === 'pointerup') {
    const i = nearestOnScreen(x, y);
    if (i >= 0) play(i);
  }
  if (pointers.size === 1) {
    const [p] = pointers.values();
    drag = { x: p[0], y: p[1], moved: true, pan: false };
  } else if (!pointers.size) {
    drag = null;
  }
}
canvas.addEventListener('pointerup', endPointer);
canvas.addEventListener('pointercancel', endPointer);
canvas.addEventListener('pointerleave', (e) => { if (!pointers.size) setHover(-1, 0, 0); });
canvas.addEventListener('contextmenu', (e) => e.preventDefault());
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const [x, y] = localXY(e);
  const lines = e.deltaMode === 1 ? 40 : 1;
  zoomAt(x, y, Math.exp((-e.deltaY * lines) * 0.0015));
}, { passive: false });

// --------------------------------------------------------------- keyboard --

// WASD flies the 3D camera, first person from the first press on (user,
// 2026-09-18): W/S forward and back along the view, A/D to the sides, Q/E
// down and up, Shift three times as fast. In 2D the same keys move the map,
// scaled by the zoom. e.code is the key's place on the keyboard, whatever the
// layout.
const MOVE_KEYS = { KeyW: [0, 0, 1], KeyS: [0, 0, -1], KeyA: [-1, 0, 0], KeyD: [1, 0, 0], KeyQ: [0, -1, 0], KeyE: [0, 1, 0] };
const MOVE_2D = 0.8;           // map units per second at zoom 1
const held = new Set();
let fast = false;

const typing = (el) => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

document.addEventListener('keydown', (e) => {
  fast = e.shiftKey;
  if (!(e.code in MOVE_KEYS) || e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || !S.tracks.length) return;
  held.add(e.code);
  e.preventDefault();
});
document.addEventListener('keyup', (e) => {
  fast = e.shiftKey;
  held.delete(e.code);
});
// A key let go while the window had no focus never sends its keyup.
window.addEventListener('blur', () => held.clear());
document.addEventListener('visibilitychange', () => { if (document.hidden) held.clear(); });

function fly(dt) {
  let r = 0;
  let u = 0;
  let f = 0;
  for (const code of held) {
    const v = MOVE_KEYS[code];
    r += v[0];
    u += v[1];
    f += v[2];
  }
  const len = Math.hypot(r, u, f);
  if (!len) return;                        // W and S together: nowhere to go
  const per = ((fast ? 3 : 1) * dt) / 1000 / len;
  if (S.view === '3d') {
    firstPerson();
    const step = MOVE_FP * sizeRef() * per;
    const { ahead, right, up } = axes3();
    for (let d = 0; d < 3; d++) S.cam3.t[d] += (ahead[d] * f + right[d] * r + up[d] * u) * step;
  } else {
    const step = (MOVE_2D / S.cam2.k) * per;
    S.cam2.x += r * step;
    S.cam2.y += f * step;
  }
  S.dirty = true;
}

// ------------------------------------------------------------------ views --

function showHint() {
  $('genresHint').textContent = S.view === '2d'
    ? 'Drag or WASD to move · wheel or pinch to zoom · click a point to play it'
    : S.fp
      ? 'First person: drag to look around · WASD to fly, Q/E down and up, Shift faster · wheel to move ahead · Reset view to orbit again'
      : 'Drag to turn · WASD to fly in first person, Q/E down and up, Shift faster · wheel or pinch to zoom · click a point to play it';
}

function setView(view) {
  S.view = view;
  document.querySelectorAll('.genres-seg button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
  showHint();
  setHover(-1, 0, 0);
  S.dirty = true;
}
document.querySelectorAll('.genres-seg button').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));

$('genresReset').addEventListener('click', () => {
  S.cam2 = { ...CAM2 };
  S.cam3 = freshCam3();
  S.fp = false;                  // back to orbiting the middle, turning by itself
  S.spinning = S.view === '3d';
  showHint();
  S.dirty = true;
});

function focusOn(i) {
  if (i < 0) return;
  const t = S.tracks[i];
  if (S.hidden.has(t.g)) {
    S.hidden.delete(t.g);
    if (S.solo >= 0 && S.solo !== t.g) setSolo(-1);
    refresh();
  }
  S.spinning = false;
  S.cam2.x = t.p2[0];
  S.cam2.y = t.p2[1];
  S.cam2.k = Math.max(S.cam2.k, 4);
  // In 3D: look at the track from close by (and, orbiting, turn about it).
  S.cam3.t = [...t.p3];
  S.cam3.x = 0;
  S.cam3.y = 0;
  S.cam3.dist = Math.min(S.cam3.dist, 1.6);
  S.flash = { i, until: performance.now() + 2600 };
  S.dirty = true;
}

// ----------------------------------------------------------------- search --

function applySearch(q) {
  const words = String(q || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) {
    S.matches = null;
  } else {
    S.matches = new Set();
    S.tracks.forEach((t, i) => { if (words.every((w) => t.hay.includes(w))) S.matches.add(i); });
  }
  const n = S.matches ? [...S.matches].filter(visible).length : 0;
  $('genresMatches').textContent = S.matches ? `${n.toLocaleString()} match${n === 1 ? '' : 'es'}` : '';
  S.dirty = true;
}
$('genresSearch').addEventListener('input', (e) => applySearch(e.target.value));
$('genresSearch').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && S.matches) {
    const first = [...S.matches].find(visible);
    if (first !== undefined) {
      focusOn(first);
      play(first);
    }
  } else if (e.key === 'Escape') {
    e.target.value = '';
    applySearch('');
  }
});

// ----------------------------------------------------------------- legend --

function renderLegend() {
  const el = $('genresLegend');
  el.innerHTML = '';
  if (!S.order.length) return;
  const note = document.createElement('p');
  note.className = 'genres-legend-note';
  note.textContent = 'Click a genre to hide it, double-click to see it alone, coloured by style.';
  el.appendChild(note);
  const all = document.createElement('button');
  all.type = 'button';
  all.className = 'genres-all';
  all.textContent = 'Show all genres';
  all.hidden = !S.hidden.size;
  all.addEventListener('click', () => { S.hidden.clear(); setSolo(-1); refresh(); });
  el.appendChild(all);
  for (const g of S.order) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'genres-group';
    b.dataset.g = String(g);
    b.setAttribute('aria-pressed', String(!S.hidden.has(g)));
    const sw = document.createElement('span');
    sw.className = 'genres-swatch';
    sw.style.background = S.groupColour[g];
    const name = document.createElement('span');
    name.textContent = S.parents[g];
    const count = document.createElement('span');
    count.className = 'genres-count';
    count.textContent = S.counts[g].toLocaleString();
    b.append(sw, name, count);
    // A click changes the legend in place: rebuilt, the second click of a
    // double-click would land on a new button and never make a dblclick.
    b.addEventListener('click', () => {
      if (S.hidden.has(g)) S.hidden.delete(g); else S.hidden.add(g);
      if (S.solo >= 0 && (S.hidden.has(S.solo) || S.hidden.size !== S.order.length - 1)) setSolo(-1);
      refresh();
    });
    b.addEventListener('dblclick', () => {
      S.hidden = new Set(S.order.filter((x) => x !== g));
      setSolo(g);
      refresh();
    });
    el.appendChild(b);
    if (S.solo === g) {
      const list = document.createElement('div');
      list.className = 'genres-styles';
      for (const [si, n] of styleCounts(g)) {
        const row = document.createElement('div');
        row.className = 'genres-style';
        const s = document.createElement('span');
        s.className = 'genres-swatch';
        s.style.background = S.styleColour.get(si) || S.colours.faint;
        const label = document.createElement('span');
        label.textContent = styleName(si);
        const c = document.createElement('span');
        c.className = 'genres-count';
        c.textContent = n.toLocaleString();
        row.append(s, label, c);
        list.appendChild(row);
      }
      el.appendChild(list);
    }
  }
}

// Solo changes the colours and the style list under the genre: a rebuild.
function setSolo(g) {
  if (S.solo === g) return;
  S.solo = g;
  assignColours();
  renderLegend();
}

function refresh() {
  document.querySelectorAll('#genresLegend .genres-group').forEach((b) => {
    b.setAttribute('aria-pressed', String(!S.hidden.has(Number(b.dataset.g))));
  });
  const all = document.querySelector('#genresLegend .genres-all');
  if (all) all.hidden = !S.hidden.size;
  applySearch($('genresSearch').value);
  setHover(-1, 0, 0);
  S.dirty = true;
}

// ----------------------------------------------------------------- player --

function showPlayer(t, note) {
  const bar = $('genresPlayer');
  bar.hidden = false;
  document.body.classList.add('has-player');
  const cover = $('genresPlayerCover');
  cover.hidden = false;
  cover.onerror = () => { cover.hidden = true; };
  cover.src = thumbUrl(t);
  $('genresPlayerTitle').textContent = t.name;
  $('genresPlayerTitle').title = `${t.folder}/${t.name}`;
  $('genresPlayerStyles').textContent = note || [S.parents[t.g], styleText(t)].filter(Boolean).join(': ');
  $('genresOpen').href = libraryUrl(t);
}

function cue(i) {
  S.current = i;
  const t = S.tracks[i];
  audio.src = fileUrl(t);
  showPlayer(t);
  S.dirty = true;
}

function play(i) {
  if (i < 0) return;
  cue(i);
  S.played.add(i);
  audio.play().catch(() => {});
}

// The next track: the closest one in 3D (which keeps more of the model's
// neighbourhoods than 2D) that is shown and not played yet.
function nearestUnplayed(i) {
  const p = S.tracks[i].p3;
  let best = -1;
  let bestD = Infinity;
  S.tracks.forEach((t, j) => {
    if (j === i || S.played.has(j) || !visible(j)) return;
    if (S.matches && !S.matches.has(j)) return;
    const d = (t.p3[0] - p[0]) ** 2 + (t.p3[1] - p[1]) ** 2 + (t.p3[2] - p[2]) ** 2;
    if (d < bestD) { bestD = d; best = j; }
  });
  return best;
}

function playNext() {
  if (S.current < 0) return;
  const j = nearestUnplayed(S.current);
  if (j >= 0) play(j);
}

audio.addEventListener('ended', () => { if ($('genresAutoplay').checked) playNext(); });
audio.addEventListener('error', () => {
  if (S.current >= 0 && audio.getAttribute('src')) showPlayer(S.tracks[S.current], 'Could not play this file.');
});
$('genresNext').addEventListener('click', playNext);

// A link from a track's info panel: ?root=...&path=... puts it in the middle,
// ready to play.
function focusFromUrl() {
  const params = new URLSearchParams(location.search);
  const path = params.get('path');
  if (!path) return;
  const root = params.get('root') || '';
  const i = S.tracks.findIndex((t) => t.path === path && (!root || t.root === root));
  if (i < 0) {
    $('genresMatches').textContent = 'That track is not on the map yet';
    return;
  }
  focusOn(i);
  cue(i);
}

// ------------------------------------------------------------------- init --

readColours();
new ResizeObserver(resize).observe(wrap);
// The theme can change under the page (the user menu's switch, or the OS).
new MutationObserver(() => { readColours(); assignColours(); renderLegend(); S.dirty = true; })
  .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
window.matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
  readColours(); assignColours(); renderLegend(); S.dirty = true;
});
setView('2d');
resize();
requestAnimationFrame(tick);
load();
