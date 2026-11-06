/* Time listened, per song, for the music charts (server: /api/music/listened,
   music_listened.py, vault KSF-052). Loaded by the music page and media-dl's
   library page; the share pages have no account to count for and do not load it.

   What counts is what PLAYED: every timeupdate adds how far the position moved
   since the one before - only while playing and not seeking, and never more
   than the wall clock allows, so a seek between two updates adds a fraction of
   a second at most and a pause adds nothing. Sent about once a minute, and
   when the page is hidden or left (a beacon, which survives the unload).

   ListenTime.track(el, what, position):
     el        a media element
     what()    what it is playing now: {id, item} - id keys the running
               total, item is what the server is sent ({ref} or {root, path})
               - or null for nothing to count
     position(el)  the file's own time (a Smooth motion stream has its own clock)
*/
const ListenTime = (() => {
  const ENDPOINT = '/api/music/listened';
  const FLUSH_MS = 60 * 1000;
  const pending = new Map();         // id -> {item, s}
  const refused = new Set();         // ids the server said are not songs
  let lastFlush = Date.now();
  let flushing = false;

  function take() {
    const sent = [...pending.entries()];
    pending.clear();
    return sent;
  }

  function bodyOf(sent) {
    return JSON.stringify({ items: sent.map(([, p]) => ({ ...p.item, s: Math.round(p.s * 10) / 10 })) });
  }

  // Back into the running totals, for the next report.
  function putBack(sent) {
    for (const [id, p] of sent) {
      const have = pending.get(id);
      if (have) have.s += p.s;
      else pending.set(id, p);
    }
  }

  async function flush() {
    if (flushing || !pending.size) return;
    flushing = true;
    lastFlush = Date.now();
    const sent = take();
    try {
      const res = await fetch(ENDPOINT, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: bodyOf(sent),
      });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        (data.counted || []).forEach((c, i) => { if (c === false && sent[i]) refused.add(sent[i][0]); });
      } else if (res.status >= 500) {
        putBack(sent);                 // the server was busy or away: next time
      }
      // 4xx: a report the server will never take (signed out, malformed) is dropped.
    } catch (e) {
      putBack(sent);                   // offline: next time
    } finally {
      flushing = false;
    }
  }

  // Leaving or hiding the page: fetch may not finish, a beacon does.
  function beacon() {
    if (!pending.size || !navigator.sendBeacon) return;
    const sent = take();
    const ok = navigator.sendBeacon(ENDPOINT, new Blob([bodyOf(sent)], { type: 'application/json' }));
    if (!ok) putBack(sent);
    lastFlush = Date.now();
  }
  window.addEventListener('pagehide', beacon);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') beacon();
  });

  function add(id, item, seconds) {
    const have = pending.get(id);
    if (have) have.s += seconds;
    else pending.set(id, { item, s: seconds });
    if (Date.now() - lastFlush >= FLUSH_MS) flush();
  }

  function track(el, what, position) {
    let last = null;                   // {id, pos, wall} at the update before
    el.addEventListener('timeupdate', () => {
      const now = what();
      const pos = position(el);
      const wall = performance.now();
      if (now && !refused.has(now.id) && last && last.id === now.id && !el.paused && !el.seeking) {
        const moved = pos - last.pos;
        const elapsed = (wall - last.wall) / 1000;
        if (moved > 0 && elapsed > 0) add(now.id, now.item, Math.min(moved, elapsed * (el.playbackRate || 1) + 0.5));
      }
      last = now ? { id: now.id, pos, wall } : null;
    });
  }

  return { track, flush };
})();
