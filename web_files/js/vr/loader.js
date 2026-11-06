/* The thumbnail loader of the wall (P8 V1), pure apart from what is injected (fetch, decode, texture factory,
 * clock), so the harness drives it with fakes. Plan 3.2:
 *   - a priority queue: the wall hands over the wanted tiles with a priority (angular distance from the gaze or
 *     the ray; lower = sooner), the loader always starts the nearest one next
 *   - at most `concurrency` requests at a time (6: gentle on the server's thumbnail generation), requests of
 *     tiles that stopped being wanted are aborted
 *   - decoded images wait for the frame loop, which turns at most `uploadsPerFrame` of them into textures per
 *     frame (nearest first) and uploads them at once, timed - so a frame stays inside its budget
 *   - the textures are an LRU capped at `cap` (1500 = about 1.3 GB of VRAM); the tiles currently wanted are never
 *     evicted; leaving a wall keeps its textures until the cap pushes them out, so Back is instant
 *   - a 404 (no thumbnail) is final; another failure is retried once after a pause */

export const LOADER = { concurrency: 6, uploadsPerFrame: 3, cap: 1500, retryMs: 1500 };

export function createThumbLoader(inj) {
  const c = Object.assign({}, LOADER, inj);
  const now = c.now || (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
  const entries = new Map();             // key -> entry
  const stats = { uploads: 0, lastUploadMs: 0, maxUploadMs: 0, uploadMsTotal: 0, evicted: 0, failed: 0, aborted: 0, started: 0 };
  let stamp = 0, inflight = 0, textures = 0, disposed = false, nDecoded = 0;
  const pinned = new Set();
  const order = [];                      // scratch for sorting decoded entries

  const closeBitmap = (e) => { if (e.bmp && typeof e.bmp.close === "function") { try { e.bmp.close(); } catch (x) { /* gone */ } } e.bmp = null; };

  function drop(e, count = true) {
    if (e.state === "loading") { inflight -= 1; if (count) stats.aborted += 1; }
    if (e.state === "decoded") nDecoded -= 1;
    if (e.ctrl) { try { e.ctrl.abort(); } catch (x) { /* ignore */ } e.ctrl = null; }
    closeBitmap(e);
    if (e.tex) { textures -= 1; try { c.disposeTexture(e.tex); } catch (x) { /* ignore */ } e.tex = null; }
    e.state = "gone";
    entries.delete(e.key);
  }

  function start(e) {
    e.state = "loading";
    inflight += 1;
    stats.started += 1;
    const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    e.ctrl = ctrl;
    const ticket = ++stamp;
    e.ticket = ticket;
    Promise.resolve()
      .then(() => c.fetchThumb(e.url, ctrl ? ctrl.signal : undefined))
      .then((blob) => c.decode(blob))
      .then((bmp) => {
        if (disposed || e.ticket !== ticket || e.state !== "loading") { if (bmp && bmp.close) try { bmp.close(); } catch (x) { /* ignore */ } return; }
        inflight -= 1; e.ctrl = null; e.bmp = bmp; e.state = "decoded"; nDecoded += 1;
        pump();
      })
      .catch((err) => {
        if (disposed || e.ticket !== ticket || e.state !== "loading") return;
        inflight -= 1; e.ctrl = null;
        const status = err && err.status;
        e.attempts += 1;
        if (status === 404 || status === 403 || status === 401 || e.attempts >= 2) {
          e.state = "failed"; stats.failed += 1;
          if (typeof c.onFail === "function") c.onFail(e.key, status || 0);
        } else {
          e.state = "queued"; e.notBefore = now() + c.retryMs;
        }
        pump();
      });
  }

  function pump() {
    if (disposed) return;
    while (inflight < c.concurrency) {
      let best = null;
      const t = now();
      for (const e of entries.values()) {
        if (e.state !== "queued" || e.notBefore > t) continue;
        if (!best || e.priority < best.priority) best = e;
      }
      if (!best) return;
      start(best);
    }
  }

  const loader = {
    /* list[0..n): { key, url, priority } - the tiles that should have a thumbnail now. */
    setWanted(list, n) {
      if (disposed) return;
      pinned.clear();
      for (let i = 0; i < n; i += 1) {
        const w = list[i];
        pinned.add(w.key);
        let e = entries.get(w.key);
        if (!e) {
          e = { key: w.key, url: w.url, priority: w.priority, state: "queued", attempts: 0, notBefore: 0, tex: null, bmp: null, ctrl: null, last: ++stamp, w: 0, h: 0, ticket: 0 };
          entries.set(w.key, e);
        } else {
          e.priority = w.priority;
        }
      }
      for (const e of Array.from(entries.values())) {
        if (pinned.has(e.key)) continue;
        if (e.state === "queued" || e.state === "loading" || e.state === "decoded") drop(e);
      }
      pump();
    },
    get(key) {
      const e = entries.get(key);
      if (!e || !e.tex) return null;
      e.last = ++stamp;
      return e.tex;
    },
    info(key) { const e = entries.get(key); return e ? { state: e.state, w: e.w, h: e.h } : null; },
    /* Once per frame: upload up to uploadsPerFrame decoded images, nearest first. Returns the number uploaded. */
    frame() {
      if (disposed) return 0;
      let done = 0;
      if (nDecoded > 0) {
        order.length = 0;
        for (const e of entries.values()) if (e.state === "decoded") order.push(e);
        order.sort((a, b) => a.priority - b.priority);
        for (let i = 0; i < order.length && done < c.uploadsPerFrame; i += 1) {
          const e = order[i];
          const bmp = e.bmp;
          const w = bmp.width || 0, h = bmp.height || 0;
          const tex = c.makeTexture(bmp, w, h);
          const t0 = now();
          c.uploadTexture(tex);
          const ms = now() - t0;
          stats.uploads += 1; stats.lastUploadMs = ms; stats.uploadMsTotal += ms;
          if (ms > stats.maxUploadMs) stats.maxUploadMs = ms;
          e.tex = tex; e.w = w; e.h = h; e.bmp = null; e.state = "ready"; e.last = ++stamp; nDecoded -= 1;
          textures += 1;
          done += 1;
          if (typeof c.onTexture === "function") c.onTexture(e.key, tex, w, h);
        }
        order.length = 0;
      }
      // LRU: over the cap, drop the least recently used texture that is not wanted right now
      while (textures > c.cap) {
        let victim = null;
        for (const e of entries.values()) {
          if (!e.tex || pinned.has(e.key)) continue;
          if (!victim || e.last < victim.last) victim = e;
        }
        if (!victim) break;
        const key = victim.key;
        drop(victim);
        stats.evicted += 1;
        if (typeof c.onEvict === "function") c.onEvict(key);
      }
      if (done && inflight < c.concurrency) pump();
      return done;
    },
    pump,
    get textureCount() { return textures; },
    get queued() { let n = 0; for (const e of entries.values()) if (e.state === "queued") n += 1; return n; },
    get inflight() { return inflight; },
    get decoded() { return nDecoded; },
    get stats() { return stats; },
    has(key) { return entries.has(key); },
    keys() { return Array.from(entries.keys()); },
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const e of Array.from(entries.values())) drop(e, false);
      entries.clear();
      pinned.clear();
    },
  };
  return loader;
}
