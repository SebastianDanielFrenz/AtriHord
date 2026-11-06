/* Pure logic of the VR app (js/vr/*): no DOM, no three.js, no WebXR. Everything here is
 * unit-tested in a plain browser page (tests/ui/stereo3d_harness.js):
 *   - the start context (what the flat page was showing when VR was entered) and its URL form,
 *   - what an item shows (3D side-by-side file or the 2D original on the same screen),
 *   - frame-time statistics for the perf HUD,
 *   - the layout and hit test of the dock and the HUD text lines,
 *   - the texture window of a 2D (mono) picture,
 *   - the state kept in sessionStorage across leaving and re-entering VR.
 * The controller mapping and the screen geometry stay in js/vr-logic.js (P7). */
import { clamp, clampZoom, clampPan } from "../vr-logic.js";

// ------------------------------------------------------------------ context

export const VIEWS = ["item", "wall", "home"];
export const SOURCE_KINDS = ["home", "folder", "recent", "favorites", "search", "3d", "people", "characters"];

const VIDEO_EXTS = [".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"];
const PICTURE_EXTS = [".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif"];

const str = (v) => (typeof v === "string" ? v : (typeof v === "number" && Number.isFinite(v) ? String(v) : ""));

// "video" | "picture" | null, from a file name (the same sets as js/stereo3d.js).
export function mediaKindOf(path) {
  const name = str(path);
  const dot = name.lastIndexOf(".");
  const ext = dot < 0 ? "" : name.slice(dot).toLowerCase();
  if (VIDEO_EXTS.includes(ext)) return "video";
  if (PICTURE_EXTS.includes(ext)) return "picture";
  return null;
}

export const baseName = (path) => str(path).split("/").filter(Boolean).pop() || "";

// The ordinary (2D) media URL of a library file.
export function fileUrl(root, path) {
  return `/api/media/file?root=${encodeURIComponent(str(root))}&path=${encodeURIComponent(str(path))}`;
}

function normalizeStatus(st) {
  if (!st || typeof st !== "object") return undefined;
  const out = { state: str(st.state) || "none" };
  if (out.state === "done" && str(st.sbs_url)) {
    out.key = str(st.key);
    out.sbs_url = str(st.sbs_url);
    out.kind = st.kind === "picture" ? "picture" : (st.kind === "video" ? "video" : "");
    out.eye_w = Number(st.eye_w) > 0 ? Number(st.eye_w) : 0;
    out.eye_h = Number(st.eye_h) > 0 ? Number(st.eye_h) : 0;
    // The server's nominal frame rate of the converted video (V2b HUD): fps = what the file plays at;
    // source_fps/interpolated are shown when the status carries them.
    if (Number(st.fps) > 0) out.fps = Number(st.fps);
    if (Number(st.source_fps) > 0) out.source_fps = Number(st.source_fps);
    if (st.interpolated === true) out.interpolated = true;
  } else if (out.state === "done") {
    out.state = "none";
  }
  return out;
}

function normalizeItem(raw) {
  if (!raw || typeof raw !== "object" || !str(raw.path)) return null;
  const path = str(raw.path);
  const kind = raw.kind === "picture" || raw.kind === "video" ? raw.kind : mediaKindOf(path);
  if (!kind) return null;
  const item = { root: str(raw.root), path, name: str(raw.name) || baseName(path), kind, key: str(raw.key) };
  if (typeof raw.favorite === "boolean") item.favorite = raw.favorite;
  if (str(raw.url)) item.url = str(raw.url);
  if (Number(raw.fps) > 0) item.fps = Number(raw.fps);       // a 2D file's frame rate, when the library already knows it
  const cn = str(raw.codec) || str(raw.vcodec);              // a 2D file's video codec, when the library already knows it (V2c)
  if (cn && codecInfo(cn)) item.codec = cn;
  const st = normalizeStatus(raw.status);
  if (st) {
    item.status = st;
    if (st.state === "done") item.key = st.key;
  }
  return item;
}

function normalizeSource(raw) {
  if (!raw || typeof raw !== "object" || !SOURCE_KINDS.includes(raw.kind)) return null;
  const sort = raw.sort && typeof raw.sort === "object" ? raw.sort : {};
  const off = Math.floor(Number(raw.offset));
  const src = {
    kind: raw.kind,
    root: str(raw.root),
    path: str(raw.path),
    query: str(raw.query),
    filters: str(raw.filters),
    sort: { key: str(sort.key), dir: sort.dir === "desc" ? "desc" : (sort.dir === "asc" ? "asc" : "") },
    offset: Number.isFinite(off) && off > 0 ? off : 0,
  };
  // Search and catalogue selectors: keep canonical pools and people filters in Back/resume state.
  for (const k of ["mode", "tag", "person", "label", "poolTag", "variant", "series", "show"]) if (str(raw[k])) src[k] = str(raw[k]);
  if (raw.kind === "people") {
    if (!src.show) src.show = "real";
    src.named = raw.named === true || raw.named === "1" || raw.named === "true";
  }
  if (Array.isArray(raw.variants)) src.variants = raw.variants.filter((v) => v && str(v.tag)).slice(0, 40).map((v) => ({
    tag: str(v.tag), label: str(v.label) || str(v.tag), count: Number(v.count) > 0 ? Number(v.count) : 0,
    ...(v.cover && typeof v.cover === "object" ? { cover: { root: str(v.cover.root), path: str(v.cover.path), mtime: Number(v.cover.mtime) || 0 } } : {}),
  }));
  return src;
}

/* context = { view: "item" | "wall" | "home", item?, source? }
 *   item   = { root, path, name, kind: "video"|"picture", key (3D key when converted, else ""), url?, status? }
 *   source = { kind, root, path, query, filters, sort: {key, dir}, offset, mode?, tag?, person? }
 * Anything unusable degrades instead of throwing: an item view without a usable item becomes the wall
 * of its source, a wall without a source becomes home. */
export function normalizeContext(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  const item = normalizeItem(r.item);
  const source = normalizeSource(r.source);
  let view = VIEWS.includes(r.view) ? r.view : (item ? "item" : (source ? "wall" : "home"));
  if (view === "item" && !item) view = source ? "wall" : "home";
  if (view === "wall" && !source) view = "home";
  const ctx = { view };
  if (item) ctx.item = item;
  if (source) ctx.source = source;
  return ctx;
}

// A flat URL query form (vr.html?view=item&ir=..&ip=..), so a bookmark or a new tab can start the same place.
export function contextToQuery(context) {
  const c = normalizeContext(context);
  const p = new URLSearchParams();
  p.set("view", c.view);
  if (c.item) {
    p.set("ir", c.item.root);
    p.set("ip", c.item.path);
  }
  const s = c.source;
  if (s) {
    p.set("sk", s.kind);
    const put = (k, v) => { if (v) p.set(k, v); };
    put("sr", s.root); put("sp", s.path); put("q", s.query); put("f", s.filters);
    put("so", s.sort.key); put("sd", s.sort.dir); put("sm", s.mode); put("st", s.tag); put("spn", s.person); put("lbl", s.label);
    put("pool", s.poolTag); put("variant", s.variant); put("sh", s.show); if (s.named) p.set("sn", "1");
    if (s.offset) p.set("off", String(s.offset));
  }
  return p.toString();
}

// Reverse of contextToQuery; "" or an unrelated query gives home.
export function contextFromQuery(search) {
  const p = new URLSearchParams(str(search).replace(/^\?/, ""));
  const raw = { view: p.get("view") || "" };
  if (p.get("ip")) raw.item = { root: p.get("ir") || "", path: p.get("ip") };
  if (p.get("sk")) {
    raw.source = {
      kind: p.get("sk"), root: p.get("sr") || "", path: p.get("sp") || "", query: p.get("q") || "", filters: p.get("f") || "",
      sort: { key: p.get("so") || "", dir: p.get("sd") || "" }, offset: p.get("off") || 0,
      mode: p.get("sm") || "", tag: p.get("st") || "", person: p.get("spn") || "", label: p.get("lbl") || "",
      poolTag: p.get("pool") || "", variant: p.get("variant") || "",
    };
    if (raw.source.kind === "people") {
      raw.source.show = p.get("sh") || "real";
      raw.source.named = p.get("sn") === "1";
    }
  }
  return normalizeContext(raw);
}

export const hasQueryContext = (search) => new URLSearchParams(str(search).replace(/^\?/, "")).has("view");

// The title the dock shows for the place VR was started from.
export function sourceTitle(source, item) {
  const s = normalizeSource(source);
  if (!s) return item ? (baseName(item.path) || "Media library") : "Home";
  switch (s.kind) {
    case "folder": {
      const parts = [s.root || "Library", ...s.path.split("/").filter(Boolean)];
      return parts.join(" / ");
    }
    case "home": return "Discover your library";
    case "recent": return "Recent downloads";
    case "favorites": return s.query ? `Favorites: ${s.query}` : "Favorites";
    case "search": if (s.label) return s.query && s.query !== "*" ? `${s.label}: ${s.query}` : s.label; return s.query === "*" ? "Search: all files" : (s.query ? `Search: ${s.query}` : (s.tag ? `Search: ${s.tag}` : "Search"));
    case "3d": return "3D ready";
    case "people": {
      const who = s.show === "hidden" ? "Hidden faces" : s.show === "all" ? "Everyone" : "Faces";
      return `${s.query ? `${who}: ${s.query}` : who}${s.named ? " · Named" : ""}`;
    }
    case "characters": return s.query ? `Characters: ${s.query}` : "Characters";
    default: return "Media library";
  }
}

// What an item puts on the screen: the converted side-by-side file when there is one, else the original (2D).
// -> { mode: "sbs" | "mono", url, kind, eyeW, eyeH, key, name }
export function itemDisplay(item) {
  const it = normalizeItem(item);
  if (!it) return null;
  const st = it.status;
  if (st && st.state === "done" && st.sbs_url) {
    return { mode: "sbs", url: st.sbs_url, kind: st.kind || it.kind, eyeW: st.eye_w, eyeH: st.eye_h, key: st.key, name: it.name, fps: st.fps || 0, sourceFps: st.source_fps || 0, interpolated: !!st.interpolated, codec: "hevc" };
  }
  return { mode: "mono", url: it.url || fileUrl(it.root, it.path), kind: it.kind, eyeW: 0, eyeH: 0, key: it.key || `${it.root}|${it.path}`, name: it.name, fps: it.fps || 0, sourceFps: 0, interpolated: false, codec: it.codec || "" };
}

// ------------------------------------------------------------------ 2D (mono) picture window

// Aspect of a 2D picture or video frame (not an SBS half): width / height, else 16:9.
export function monoAspect(w, h) {
  return Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0 ? w / h : 16 / 9;
}

// The window of the whole texture a mono screen shows (pan in image fractions, 0 = centred).
export function monoUvWindow(zoom, pan) {
  const z = clampZoom(zoom);
  const p = clampPan(pan, z);
  return { repeatX: 1 / z, repeatY: 1 / z, offsetX: 0.5 + p.x - 0.5 / z, offsetY: 0.5 + p.y - 0.5 / z };
}

// ------------------------------------------------------------------ frame statistics

export class FrameStats {
  constructor(capacity = 240) {
    this.cap = Math.max(2, capacity | 0);
    this.buf = new Float64Array(this.cap);
    this.n = 0;
    this.head = 0;
  }

  push(ms) {
    if (!Number.isFinite(ms) || ms <= 0) return;
    this.buf[this.head] = ms;
    this.head = (this.head + 1) % this.cap;
    if (this.n < this.cap) this.n += 1;
  }

  // { n, p50, p99, mean, fps, over, overPct } - over = frames longer than budgetMs. All finite (0 when empty).
  summary(budgetMs = 1000 / 120 + 0.2) {
    const n = this.n;
    if (!n) return { n: 0, p50: 0, p99: 0, mean: 0, fps: 0, over: 0, overPct: 0 };
    const a = Array.from(this.buf.subarray(0, n)).sort((x, y) => x - y);
    const at = (q) => a[Math.min(n - 1, Math.max(0, Math.ceil(q * n) - 1))];
    const mean = a.reduce((s, v) => s + v, 0) / n;
    const over = a.filter((v) => v > budgetMs).length;
    return { n, p50: at(0.5), p99: at(0.99), mean, fps: 1000 / mean, over, overPct: (100 * over) / n };
  }

  reset() { this.n = 0; this.head = 0; }
}

const f1 = (v) => (Number.isFinite(v) ? v.toFixed(1) : "-");

// info = { enabledFeatures, frameRate, supportedFrameRates, mode, textures, geometries, calls, status, wall? }
export function hudLines(sum, info = {}) {
  const s = sum || { n: 0, p50: 0, p99: 0, fps: 0, over: 0, overPct: 0 };
  const feat = Array.isArray(info.enabledFeatures) && info.enabledFeatures.length ? info.enabledFeatures.join(", ")
    : (info.enabledFeatures === undefined ? "not reported" : "none");
  const rates = info.supportedFrameRates && info.supportedFrameRates.length ? Array.from(info.supportedFrameRates).map((r) => Math.round(r)).join("/") : "";
  const rate = Number.isFinite(info.frameRate) && info.frameRate > 0 ? `${Math.round(info.frameRate)} Hz` : "not reported";
  return [
    `Frame ${f1(s.p50)} ms p50   ${f1(s.p99)} ms p99   ${Math.round(s.fps) || 0} fps   (${s.n} frames)`,
    `Over budget ${s.over} (${f1(s.overPct)} %)`,
    `Textures ${info.textures | 0}   geometries ${info.geometries | 0}   draw calls ${info.calls | 0}`,
    `Runtime frame rate ${rate}${rates ? `   supported ${rates}` : ""}${info.rateRequest ? `   ${rateRequestText(info.rateRequest)}` : ""}`,
    ...(info.rateRequest && info.rateRequest.state === "failed" ? [`Refresh rate request failed: ${String(info.rateRequest.error || "").slice(0, 40)}`] : []),
    `Session ${info.mode === "preview" ? "desktop preview" : "immersive-vr"}   features: ${feat}`,
    ...wallLines(info.wall),
    ...videoLines(info.video),
    ...(info.decoders ? info.decoders.lines() : []),
  ];
}

// ------------------------------------------------------------------ highest refresh rate request (V2c)

// -> { state: "off" | "n/a" | "pending" | "ok" | "failed", rate, error }
export function rateRequestText(r) {
  if (!r) return "";
  switch (r.state) {
    case "off": return "target off";
    case "pending": return `target ${Math.round(r.rate)} ...`;
    case "ok": return `target ${Math.round(r.rate)} ok`;
    case "failed": return `target ${Math.round(r.rate)} FAILED`;
    default: return "target n/a";
  }
}

export function highestRate(rates) {
  let best = 0;
  if (rates && typeof rates.length === "number") for (let i = 0; i < rates.length; i += 1) if (Number(rates[i]) > best) best = Number(rates[i]);
  return best;
}

/* Ask the XR session for its highest supported frame rate (session.updateTargetFrameRate). Never rejects.
 * -> Promise<{ state, rate, error }>; "n/a" when the session reports no supportedFrameRates or lacks the method. */
export function requestHighestRate(session) {
  const best = highestRate(session && session.supportedFrameRates);
  if (!(best > 0) || typeof session.updateTargetFrameRate !== "function") return Promise.resolve({ state: "n/a", rate: 0, error: "" });
  try {
    return Promise.resolve(session.updateTargetFrameRate(best)).then(
      () => ({ state: "ok", rate: best, error: "" }),
      (e) => ({ state: "failed", rate: best, error: (e && (e.message || e.name)) || String(e) }));
  } catch (e) {
    return Promise.resolve({ state: "failed", rate: best, error: (e && (e.message || e.name)) || String(e) });
  }
}

// ------------------------------------------------------------------ video statistics (V2b)

const fmtFps = (v) => String(Math.round(v * 100) / 100);

/* What the HUD knows about the video on the screen. Counting is allocation-free: the per-frame
 * callback (HTMLVideoElement.requestVideoFrameCallback) writes one timestamp into a ring and re-arms
 * itself; everything else happens when the HUD redraws (sample/read, a few times a second, only while
 * the HUD is visible).
 *   rendered  = totalVideoFrames - droppedVideoFrames of getVideoPlaybackQuality() when the browser has it,
 *               else the rVFC presentedFrames counter, else unknown (null)
 *   dropped   = droppedVideoFrames, else unknown (null)
 *   measured fps = presented frames per second over the last ~1 s (rVFC callback times); without rVFC the
 *               rendered-count delta over the sample window
 *   dropped/s = droppedVideoFrames delta over the sample window (<= 1.2 s)
 * Totals count since this video element started (a new item = a new element = reset to 0); seeking and
 * looping do not reset them. nominal = { fps, sourceFps, interpolated } from the item. */
export class VideoStats {
  constructor(video, nominal = {}, nav = null) {
    this.v = video;
    this.nav = nav;
    this.dec = null;                // V2c: { label, w, h, fps, state: "checking"|"done"|"na", info }
    this.nominal = nominal || {};
    this.ts = new Float64Array(512);
    this.head = 0;
    this.n = 0;
    this.presented = 0;
    this.handle = 0;
    this.alive = true;
    this.hasRvfc = !!video && typeof video.requestVideoFrameCallback === "function";
    this.sT = new Float64Array(8);
    this.sR = new Float64Array(8);
    this.sD = new Float64Array(8);
    this.sC = new Float64Array(8);   // V2c: video.currentTime at each sample (playback speed)
    this.sU = new Float64Array(8);   // V2c: texture uploads counted at each sample
    this.texCount = 0;
    this.texN = 0;                   // how many textures the counter covers (sbs: one per eye)
    this.onTex = () => { this.texCount += 1; };   // Texture.onUpdate: called by three.js right after each upload
    this.sHead = 0;
    this.sN = 0;
    this.cb = function videoStatsFrame(now, meta) {
      if (!this.alive) return;
      this.ts[this.head] = now;
      this.head = (this.head + 1) & 511;
      if (this.n < 512) this.n += 1;
      if (meta && Number.isFinite(meta.presentedFrames)) this.presented = meta.presentedFrames;
      else this.presented += 1;
      this.handle = this.v.requestVideoFrameCallback(this.cb);
    }.bind(this);
    if (this.hasRvfc) this.handle = video.requestVideoFrameCallback(this.cb);
  }

  quality() {
    const v = this.v;
    if (!v || typeof v.getVideoPlaybackQuality !== "function") return null;
    try {
      const q = v.getVideoPlaybackQuality();
      if (q && Number.isFinite(q.totalVideoFrames) && Number.isFinite(q.droppedVideoFrames)) return q;
    } catch (e) { /* element not ready */ }
    return null;
  }

  // Called by the HUD redraw: remember (time, rendered, dropped) for the per-second rates.
  sample(now) {
    const q = this.quality();
    const i = this.sHead;
    this.sT[i] = now;
    this.sR[i] = q ? q.totalVideoFrames - q.droppedVideoFrames : -1;
    this.sD[i] = q ? q.droppedVideoFrames : -1;
    this.sC[i] = this.v && Number.isFinite(this.v.currentTime) ? this.v.currentTime : -1;
    this.sU[i] = this.texCount;
    this.sHead = (i + 1) & 7;
    if (this.sN < 8) this.sN += 1;
  }

  // V2c: ask mediaCapabilities once per item about THIS video's codec/size/fps (needs the decoded size, so it
  // starts at the first read with videoWidth > 0). Async; the answer lands in this.dec.
  maybeProbe() {
    if (this.dec || !this.nominal.codec) return;
    const v = this.v, ci = codecInfo(this.nominal.codec);
    if (!v || !ci || !(v.videoWidth > 0 && v.videoHeight > 0)) return;
    const w = v.videoWidth | 0, h = v.videoHeight | 0;
    const fps = this.nominal.fps > 0 ? this.nominal.fps : 30;
    const dec = { label: ci.label, w, h, fps, state: "checking", info: null };
    this.dec = dec;
    const mime = /\.(webm|mkv)(\?|$)/i.test(String(v.currentSrc || v.src || "")) ? "video/webm" : "video/mp4";
    const bitrate = Math.min(120e6, Math.max(2e6, Math.round(w * h * fps * 0.05)));
    probeDecode(this.nav, decodingConfig("file", mime, ci.codec, w, h, fps, bitrate)).then((r) => {
      dec.info = r; dec.state = r ? "done" : "na";
    });
  }

  read(now, xrHz = 0) {
    const v = this.v;
    this.maybeProbe();
    const q = this.quality();
    let fps = 0;
    if (this.n >= 2) {
      const newest = this.ts[(this.head - 1) & 511];
      if (now - newest <= 1000) {
        let cnt = 0, oldest = newest;
        for (let k = 1; k <= this.n; k += 1) {
          const t = this.ts[(this.head - k) & 511];
          if (newest - t > 1000) break;
          cnt += 1; oldest = t;
        }
        if (cnt >= 2 && newest > oldest) fps = ((cnt - 1) * 1000) / (newest - oldest);
      }
    }
    let dps = 0, rps = 0, speed = null, ups = null;
    if (this.sN >= 2) {
      const last = (this.sHead - 1) & 7;
      let first = -1;
      for (let k = this.sN; k >= 2; k -= 1) {
        const j = (this.sHead - k) & 7;
        if (now - this.sT[j] <= 1200 && this.sT[last] > this.sT[j]) { first = j; break; }
      }
      if (first >= 0) {
        const span = (this.sT[last] - this.sT[first]) / 1000;
        if (this.sD[last] >= 0 && this.sD[first] >= 0) dps = Math.max(0, (this.sD[last] - this.sD[first]) / span);
        if (this.sR[last] >= 0 && this.sR[first] >= 0) rps = Math.max(0, (this.sR[last] - this.sR[first]) / span);
        if (this.sC[last] >= 0 && this.sC[first] >= 0) speed = Math.max(0, (this.sC[last] - this.sC[first]) / span);
        ups = Math.max(0, (this.sU[last] - this.sU[first]) / span);
      }
    }
    if (!this.hasRvfc) fps = rps;
    const n = this.nominal;
    return {
      width: v ? v.videoWidth | 0 : 0, height: v ? v.videoHeight | 0 : 0,
      nominalFps: n.fps > 0 ? n.fps : 0, sourceFps: n.sourceFps > 0 ? n.sourceFps : 0, interpolated: !!n.interpolated,
      measuredFps: fps,
      rendered: q ? q.totalVideoFrames - q.droppedVideoFrames : (this.hasRvfc ? this.presented : null),
      renderedFrom: q ? "quality" : (this.hasRvfc ? "presented" : "none"),
      dropped: q ? q.droppedVideoFrames : null,
      droppedPerS: dps,
      decode: this.dec,
      speed, texPerS: ups, texN: this.texN, xrHz: xrHz > 0 ? xrHz : 0,
    };
  }

  dispose() {
    this.alive = false;
    if (this.hasRvfc && this.handle && typeof this.v.cancelVideoFrameCallback === "function") {
      try { this.v.cancelVideoFrameCallback(this.handle); } catch (e) { /* gone */ }
    }
    this.handle = 0;
  }
}

// The two HUD lines for the playing video; none when nothing plays.
export function videoLines(v) {
  if (!v) return [];
  const res = v.width > 0 && v.height > 0 ? `${v.width}x${v.height}` : "-";
  let nominal = "unknown";
  if (v.nominalFps > 0) {
    nominal = v.interpolated && v.sourceFps > 0 ? `${fmtFps(v.nominalFps)} fps (interpolated from ${fmtFps(v.sourceFps)})` : `${fmtFps(v.nominalFps)} fps`;
  }
  const num = (x) => (x === null || x === undefined || !Number.isFinite(x) ? "n/a" : String(Math.round(x)));
  return [
    `Video ${res}   nominal ${nominal}   measured ${f1(v.measuredFps)} fps`,
    `Frames rendered ${num(v.rendered)} (${f1(v.measuredFps)}/s)   dropped ${num(v.dropped)} (${f1(v.droppedPerS)}/s)`,
    `Speed ${v.speed === null || v.speed === undefined ? "-" : v.speed.toFixed(2) + "x"}   XR ${v.xrHz > 0 ? Math.round(v.xrHz) + " Hz" : "n/a"}   texture updates ${v.texPerS === null || v.texPerS === undefined ? "-" : f1(v.texN > 1 ? v.texPerS / v.texN : v.texPerS) + "/s" + (v.texN > 1 ? ` x${v.texN}` : "")}`,
    ...(v.decode ? [decodeLine(v.decode)] : []),
  ];
}

// ------------------------------------------------------------------ decoder capabilities (V2c)

// Known codec names -> the mediaCapabilities codec string. A full codec string of a known family passes through.
const CODEC_TABLE = [
  { label: "HEVC", codec: "hvc1.1.6.L153.B0", names: ["hevc", "h265", "h.265", "hvc1", "hev1"] },
  { label: "AV1", codec: "av01.0.08M.08", names: ["av1", "av01"] },
  { label: "VP9", codec: "vp09.00.41.08", names: ["vp9", "vp09"] },
  { label: "H.264", codec: "avc1.640033", names: ["h264", "h.264", "avc", "avc1"] },
];
export function codecInfo(name) {
  const n = String(name || "").toLowerCase().trim();
  if (!n) return null;
  for (const c of CODEC_TABLE) {
    for (const k of c.names) {
      if (n === k) return { label: c.label, codec: c.codec };
      if (n.startsWith(k + ".") && /^[a-z0-9]{4}\./.test(n)) return { label: c.label, codec: n };
    }
  }
  return null;
}

export const DECODER_CODECS = [
  { id: "AV1", codec: "av01.0.08M.08", mime: "video/mp4" },
  { id: "HEVC", codec: "hvc1.1.6.L153.B0", mime: "video/mp4" },
  { id: "VP9", codec: "vp09.00.41.08", mime: "video/webm" },
  { id: "H264", codec: "avc1.640033", mime: "video/mp4" },
];
export const DECODER_MODES = [
  { id: "1080p60", w: 1920, h: 1080, fps: 60, bitrate: 12e6 },
  { id: "2160p60", w: 3840, h: 2160, fps: 60, bitrate: 40e6 },
  { id: "120a", w: 4316, h: 1214, fps: 120, bitrate: 36e6 },
  { id: "120b", w: 2160, h: 1920, fps: 120, bitrate: 36e6 },
];

export function decodingConfig(type, mime, codec, width, height, framerate, bitrate) {
  return { type, video: { contentType: `${mime}; codecs="${codec}"`, width, height, bitrate, framerate } };
}

// -> Promise of { supported, smooth, powerEfficient } or null (API missing, rejected, unusable answer). Never rejects.
export function probeDecode(nav, cfg) {
  const mc = nav && nav.mediaCapabilities;
  if (!mc || typeof mc.decodingInfo !== "function") return Promise.resolve(null);
  try {
    return Promise.resolve(mc.decodingInfo(cfg)).then(
      (r) => (r && typeof r === "object" ? { supported: !!r.supported, smooth: !!r.smooth, powerEfficient: !!r.powerEfficient } : null),
      () => null);
  } catch (e) {
    return Promise.resolve(null);
  }
}

const flagText = (r) => (r ? `${r.supported ? "S" : "-"}${r.smooth ? "M" : "-"}${r.powerEfficient ? "H" : "-"}` : "n/a");

function decodeLine(d) {
  const head = `Decode ${d.label} ${d.w}x${d.h}@${fmtFps(d.fps)}:`;
  if (d.state === "checking") return `${head} checking...`;
  if (!d.info) return `${head} n/a`;
  return `${head} ${d.info.supported ? "supported" : "NOT supported"}, ${d.info.smooth ? "smooth" : "not smooth"}, ${d.info.powerEfficient ? "hardware" : "no hw flag"}`;
}

/* The decoder table of the HUD: one probe per session (4 codecs x 4 modes via mediaCapabilities.decodingInfo,
 * type "file", plus canPlayType), async; lines() is cheap and returns the cached text until an answer lands.
 * S = supported, M = smooth, H = powerEfficient (the browser's hardware decoder signal), n/a = no answer. */
export class DecoderReport {
  constructor(nav, canPlay) {
    this.nav = nav;
    this.canPlay = typeof canPlay === "function" ? canPlay : null;
    this.started = false;
    this.rows = [];
    this.text = null;
    this.queries = 0;
  }

  start() {
    if (this.started) return;
    this.started = true;
    for (const c of DECODER_CODECS) {
      let play = "n/a";
      if (this.canPlay) {
        try { play = this.canPlay(`${c.mime}; codecs="${c.codec}"`) || "no"; } catch (e) { play = "n/a"; }
      }
      const row = { id: c.id, play, cells: DECODER_MODES.map(() => undefined) };
      this.rows.push(row);
      DECODER_MODES.forEach((m, j) => {
        this.queries += 1;
        probeDecode(this.nav, decodingConfig("file", c.mime, c.codec, m.w, m.h, m.fps, m.bitrate)).then((r) => {
          row.cells[j] = r; this.text = null;
        });
      });
    }
    this.text = null;
  }

  lines() {
    this.start();
    if (this.text) return this.text;
    const out = ["Decode S=supported M=smooth H=hardware; 120a 4316x1214, 120b 2160x1920"];
    for (const r of this.rows) {
      const cells = r.cells.map((x, j) => `${DECODER_MODES[j].id} ${x === undefined ? "..." : flagText(x)}`).join("  ");
      out.push(`${r.id.padEnd(4)} ${cells}  play ${r.play}`);
    }
    this.text = out;
    return out;
  }
}

// The wall's own numbers (V1): tiles, meshes, thumbnail textures, the loader queue, upload cost, update cost.
function wallLines(w) {
  if (!w) return [];
  return [
    `Wall ${w.tiles | 0} tiles   ${w.meshes | 0} meshes   ${w.textures | 0} thumbs   queue ${w.queued | 0}+${w.inflight | 0}+${w.decoded | 0}`,
    `Upload ${f1(w.lastUploadMs)} ms (max ${f1(w.maxUploadMs)})   update p99 ${f1(w.updateP99)} ms   evicted ${w.evicted | 0}`,
  ];
}

// ------------------------------------------------------------------ dock layout

// Buttons in one row at the bottom of a width x height pixel panel; the top part holds the title lines.
export function dockLayout(buttons, width, height, { pad = 28, gap = 24, maxButtonWidth = 420 } = {}) {
  const n = buttons.length;
  if (!n) return [];
  const bh = Math.round(height * 0.38);
  const y = height - pad - bh;
  const bw = Math.min(maxButtonWidth, (width - 2 * pad - gap * (n - 1)) / n);
  const total = bw * n + gap * (n - 1);
  const x0 = (width - total) / 2;
  return buttons.map((b, i) => ({ id: b.id, label: b.label, x: x0 + i * (bw + gap), y, w: bw, h: bh }));
}

export function hitButton(layout, px, py) {
  if (!Number.isFinite(px) || !Number.isFinite(py)) return null;
  const b = layout.find((r) => px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h);
  return b ? b.id : null;
}

// Where the furniture sits relative to the recentered frame (the head at yaw): metres, radians.
// The dock is a desk edge below the line of sight, tilted towards the user.
// dropDeg 48 (was 40; user, 2026-10-18: "The VR nav bar must sit lower so to not sit in front of a big screen."): the
// largest screen (screen size 150 %, SCREEN_HEIGHT 1.5 m at 1.4 m, constant angular size at any distance) reaches
// atan(1.125 / 1.4) = 38.8 deg below the line of sight, and the dock's top edge must clear it: at 48 deg the top edge
// is at 48 - atan(0.16 / 1.2) = 40.4 deg. The slider row under it and the chip strip above it follow dockPose.
export const DOCK = { dropDeg: 48, radius: 1.2, width: 1.4, height: 0.32, px: 1600 };
export function dockPose(d = DOCK) {
  const a = (d.dropDeg * Math.PI) / 180;
  return { x: 0, y: -d.radius * Math.sin(a), z: -d.radius * Math.cos(a), tiltX: -a };
}

// ------------------------------------------------------------------ state across leaving VR

export const STATE_KEY = "mediadl.vr.state";

export function saveVrState(storage, state) {
  try {
    storage.setItem(STATE_KEY, JSON.stringify({ context: normalizeContext(state && state.context), dist: state && state.dist, hud: !!(state && state.hud), at: Date.now() }));
    return true;
  } catch (e) { return false; }
}

export function loadVrState(storage) {
  try {
    const raw = JSON.parse(storage.getItem(STATE_KEY) || "null");
    if (!raw || typeof raw !== "object") return null;
    return { context: normalizeContext(raw.context), dist: Number.isFinite(raw.dist) ? raw.dist : undefined, hud: raw.hud !== false, at: Number(raw.at) || 0 };
  } catch (e) { return null; }
}

export { clamp };
