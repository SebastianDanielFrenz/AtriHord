/* Auto-convert on view (P8 V1, OWNER DECISION 2026-10-06): opening a picture or video that has no finished 3D
 * version asks the server to convert it - POST /api/media/3d/view {root, path} - and the VR app shows the 2D
 * original at once. While the item stays open its /api/media/3d/status is polled; when it is done the app swaps
 * (picture) or offers the switch (video). Leaving the item before it is done sends {leave: true} (best effort; the
 * server demotes the request to the manual lane, it is still converted). The 2D site never does this.
 *
 * Pure logic with injected IO and timers (the harness scripts them):
 *   post(body) -> Promise<{ status, json }>      POST /api/media/3d/view
 *   getStatus(item) -> Promise<json | null>      GET /api/media/3d/status
 *   setTimer(fn, ms) / clearTimer(id)
 *   onState({ phase, item, status?, message? })  phases: "requested" | "converting" | "done" | "failed" | "backoff" | "off"
 * 429 -> back off for retry_after_s, then ask again while the item is still open. 403/404/409 (not allowed, or the
 * feature is off) -> stay 2D, silently, for the rest of the session (no request storm). */

export const AUTO = { pollMs: 3000, defaultBackoffS: 30, maxBackoffS: 300, transientRetryMs: 8000, maxTransient: 3 };

export function createAutoConvert(io) {
  const c = Object.assign({}, AUTO, io);
  let cur = null;              // { item, ticket, viewed, doneReached, timer, transient }
  let ticketSeq = 0;
  let off = false;
  const emit = (st) => { if (typeof c.onState === "function") c.onState(st); };
  const clear = (t) => { if (t && t.timer !== null && t.timer !== undefined) { c.clearTimer(t.timer); t.timer = null; } };
  const alive = (t) => cur === t;

  function apply(t, st) {
    if (!alive(t) || !st || typeof st !== "object") return;
    if (st.state === "done" && st.sbs_url) {
      t.doneReached = true;
      clear(t);
      emit({ phase: "done", item: t.item, status: st });
      return;
    }
    if (st.state === "unsupported") { clear(t); return; }       // stereo by nature: nothing to convert or wait for
    if (st.state === "failed") {
      clear(t);
      emit({ phase: "failed", item: t.item, status: st, message: st.error ? String(st.error) : "" });
      return;
    }
    emit({ phase: st.state === "queued" ? "requested" : "converting", item: t.item, status: st });
    clear(t);
    t.timer = c.setTimer(() => poll(t), c.pollMs);
  }

  async function poll(t) {
    t.timer = null;
    if (!alive(t)) return;
    let st = null;
    try { st = await c.getStatus(t.item); } catch (e) { st = null; }
    if (!alive(t)) return;
    if (st) apply(t, st);
    else t.timer = c.setTimer(() => poll(t), c.pollMs);
  }

  async function request(t) {
    t.timer = null;
    if (!alive(t) || off) return;
    let r;
    try {
      r = await c.post({ root: t.item.root || "", path: t.item.path });
    } catch (e) {
      r = null;
    }
    if (!alive(t)) return;
    if (!r) {
      t.transient += 1;
      if (t.transient <= c.maxTransient) t.timer = c.setTimer(() => request(t), c.transientRetryMs);
      return;
    }
    t.viewed = true;
    if (r.status === 429) {
      const s = Math.min(c.maxBackoffS, Math.max(1, Number(r.json && r.json.retry_after_s) || c.defaultBackoffS));
      emit({ phase: "backoff", item: t.item, message: `retry in ${Math.round(s)} s` });
      t.viewed = false;
      t.timer = c.setTimer(() => request(t), s * 1000);
      return;
    }
    if (r.status === 401 || r.status === 403 || r.status === 404 || r.status === 409) {
      off = true;
      t.viewed = false;
      emit({ phase: "off", item: t.item });
      return;
    }
    if (r.status >= 200 && r.status < 300 && r.json) { apply(t, r.json); return; }
    t.viewed = false;
    t.transient += 1;
    if (t.transient <= c.maxTransient) t.timer = c.setTimer(() => request(t), c.transientRetryMs);
  }

  const auto = {
    get off() { return off; },
    get current() { return cur ? cur.item : null; },
    // An item was opened in the viewer. Returns the phase it starts in.
    open(item) {
      auto.leave();
      if (!item) return "idle";
      const t = { item, ticket: ++ticketSeq, viewed: false, doneReached: false, timer: null, transient: 0 };
      cur = t;
      if (item.status && item.status.state === "done") { t.doneReached = true; return "done"; }
      if (off) return "off";
      request(t);
      return "requested";
    },
    // The item was left (Back, next, exit): a best-effort leave unless it already finished.
    leave() {
      const t = cur;
      cur = null;
      if (!t) return;
      clear(t);
      if (t.viewed && !t.doneReached && !off) {
        try { Promise.resolve(c.post({ root: t.item.root || "", path: t.item.path, leave: true })).catch(() => {}); } catch (e) { /* best effort */ }
      }
    },
    // The viewer swapped to the 3D version itself: nothing left to do for this item.
    markDone() { if (cur) { cur.doneReached = true; clear(cur); } },
    dispose() { auto.leave(); },
  };
  return auto;
}
