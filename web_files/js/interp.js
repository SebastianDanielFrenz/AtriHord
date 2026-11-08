// Smooth motion (frame interpolation) in the preview player, and the player's
// settings menu where it and the bandwidth cap are chosen.
//
// With Smooth motion on, a video is not played from its file: the vision worker
// generates in-between frames with Lossless Scaling frame generation, encodes
// them on the GPU and keeps them as 2-second segments on disk (server.py, "Frame
// interpolation in the video player"). The player plays those segments through
// Media Source Extensions, so anything already made - the loop, a seek back,
// the next visit - plays at once. Where that cannot work (no MSE, codec not
// supported) the older live stream from /api/media/interp/stream is the
// fallback. The output rate is this screen's own rate: a 25 fps video on a
// 120 Hz screen plays at 120 fps (4.8x), so every frame is on screen equally
// long - a whole multiple (4x = 100 fps there) showed every fifth frame twice,
// a judder, and a variable-refresh screen does not come down to meet it:
// browsers composite at the screen's full rate (user, 2026-09-25; KSF-029).
// A screen no faster than the video gets the original file.
//
// Both settings are stored per device (localStorage), not per account: whether
// a screen runs at 144 Hz and how much a connection carries are properties of
// where someone is watching, not of who they are.
//
// Segments carry the file's own timeline, so seeking and looping are native.
// The live stream starts at the position asked for and has no length of its
// own, so there the video carries el._timeline for the control bar (player.js
// playbackPosition) and a seek asks for a new stream from there.
//
// Needs player.js (startPlayback, setPreviewEnhancement, playbackPosition,
// infoRequestUrl).

const interpRoutes = { config: '/api/media/interp/config', stream: '/api/media/interp/stream',
  seg: '/api/media/interp/seg' };
const SMOOTH_MOTION_KEY = 'playerSmoothMotion';     // '1' = on
const BANDWIDTH_KEY = 'playerBandwidthMbps';        // a number; 0 = Max (no cap)

let interpConfig = null;
let interpConfigAt = 0;

function loadInterpConfig() {
  if (interpConfig && Date.now() - interpConfigAt < 30000) return Promise.resolve(interpConfig);
  return fetch(interpRoutes.config)
    .then((r) => (r.ok ? r.json() : interpConfig))
    .then((c) => { interpConfig = c; interpConfigAt = Date.now(); return c; })
    .catch(() => interpConfig);
}

// localStorage throws in some private modes; the defaults are fine there.
function storedSetting(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function storeSetting(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* not remembered, still applied */ }
}

function smoothMotionWanted() {
  return storedSetting(SMOOTH_MOTION_KEY) === '1';
}

// The stored cap if the server still offers it, else the server's default.
function bandwidthCap(cfg) {
  const offered = (cfg && cfg.bitrates_mbps) || [0];
  const stored = storedSetting(BANDWIDTH_KEY);
  if (stored !== null && offered.includes(Number(stored))) return Number(stored);
  return cfg && offered.includes(cfg.default_mbps) ? cfg.default_mbps : offered[0];
}

function bandwidthLabel(mbps) {
  return mbps ? `${mbps} Mbit/s` : 'Max (no cap)';
}

function formatFps(fps) {
  return String(Math.round(fps * 100) / 100);
}

// The screen's refresh rate: the spacing of this window's animation frames,
// measured over a rolling window while a video plays, NOT once per page. It is
// the rate the window really renders at, on whichever monitor it sits - a
// window on a 240 Hz screen beside a 120 Hz main screen, or a monitor whose
// rate was just changed, is followed within seconds (user, 2026-09-20: a video
// delivered at 240 fps was shown at 120). Phones with adaptive refresh run the
// browser at 60 Hz while a page is idle and only speed up for motion (a 144 Hz
// phone measured 60, user 2026-09-18) - hence measuring WHILE PLAYING, and the
// viewer can still set the rate per device. A set rate is never overridden,
// only shown beside the measured one.
const DISPLAY_HZ_KEY = 'playerDisplayHz';            // 'auto' or a number
const DISPLAY_HZ_CHOICES = [60, 90, 100, 120, 144, 165, 240];
const HZ_GAPS = 150;                                 // frame gaps kept (about 1 s at 144 Hz)

function displayHzSetting() {
  const v = Number(storedSetting(DISPLAY_HZ_KEY));
  return DISPLAY_HZ_CHOICES.includes(v) ? v : 'auto';
}

const hzGaps = [];
let hzTicking = false;
let hzLast = null;
let hzUntil = 0;

// Measuring costs one callback per frame, so it runs while a video plays -
// which is also when an adaptive screen runs at its full rate - and for a
// moment whenever someone asks (opening the menu, planning a stream). Any
// playing video Smooth motion looks after counts - the library preview's and
// the music page's song video, which the rate was never measured for.
function hzWanted() {
  if (performance.now() < hzUntil) return true;
  if (document.visibilityState !== 'visible') return false;
  for (const v of document.querySelectorAll('video')) {
    if (!v.paused && (v._smooth || v.closest('#mediaPreviewBody'))) return true;
  }
  return false;
}

function hzTick(now) {
  if (hzLast !== null) {
    const gap = now - hzLast;
    if (gap > 0.4 && gap < 250) {                    // a tab coming back skips a long gap
      hzGaps.push(gap);
      if (hzGaps.length > HZ_GAPS) hzGaps.shift();
    }
  }
  hzLast = now;
  if (!hzWanted()) { hzTicking = false; hzLast = null; return; }
  requestAnimationFrame(hzTick);
}

function measureFor(seconds) {
  hzUntil = Math.max(hzUntil, performance.now() + seconds * 1000);
  if (hzTicking) return;
  hzTicking = true;
  hzLast = null;
  requestAnimationFrame(hzTick);
}

// Playback starting, or the tab coming back while something plays, is when
// measuring should run again: the ticker stops itself whenever nothing plays.
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') measureFor(0); });
document.addEventListener('play', () => measureFor(0), true);

// Rates screens run at. The output rate IS the measured rate, so it snaps to
// the nearest of these only when that is within 5% (59.94 -> 60, 101.3 -> 100)
// and is otherwise taken as it is: a 100 Hz TV read as 90 or 120 would judder.
const KNOWN_HZ = [48, 50, 60, 72, 75, 85, 90, 100, 110, 120, 144, 155, 160, 165, 170, 175, 180,
  200, 240, 280, 300, 360];
const snapHz = (hz) => {
  const near = KNOWN_HZ.reduce((best, c) => (Math.abs(c - hz) < Math.abs(best - hz) ? c : best), 60);
  return Math.abs(near - hz) <= hz * 0.05 ? near : Math.round(hz);
};

// The fastest typical spacing (the 20th percentile, so a few quick frames
// count but a single odd one does not), snapped as above. null until enough
// frames have been seen to tell.
function measuredHz(least = 40) {
  if (hzGaps.length < least) return null;
  const gaps = [...hzGaps].sort((a, b) => a - b);
  return snapHz(1000 / gaps[Math.floor(gaps.length * 0.2)]);
}

const forgetHz = () => { hzGaps.length = 0; };

// What is measured now, or a fresh measurement if nothing is (the first video
// of a page). A hidden tab gets no animation frames: 60 after the timeout.
function measureDisplayHz(least = 40) {
  const have = measuredHz(least);
  if (have) return Promise.resolve(have);
  measureFor(2);
  return new Promise((resolve) => {
    const started = performance.now();
    const wait = () => {
      const hz = measuredHz(least);
      if (hz) { resolve(hz); return; }
      if (performance.now() - started > 2500) { resolve(measuredHz(8) || 60); return; }
      setTimeout(wait, 100);
    };
    wait();
  });
}

// {hz: what to plan with, measured: whether that is the measured rate,
//  now: what this window renders at right now, null until it is known}.
async function displayHz() {
  const set = displayHzSetting();
  if (set !== 'auto') return { hz: set, measured: false, now: measuredHz() };
  const hz = measuredHz() || await measureDisplayHz();
  return { hz, measured: true, now: measuredHz() || hz };
}

// What Smooth motion would do for this video on this screen: {out} (frames a
// second) or {reason}. The output is the screen's rate, or an even fraction of
// it when the server's ceiling (or 16x the video) is lower - a 360 Hz screen
// gets 180 fps, each frame shown twice - so every frame stays equally long.
const SMOOTH_MIN_RATIO = 1.1;     // server.py INTERP_MIN_RATIO: less barely moves
const SMOOTH_MAX_RATIO = 16;      // server.py INTERP_MAX_RATIO

const formatRatio = (r) => `${Math.round(r * 100) / 100}×`;

async function planSmoothMotion(video) {
  const st = video._smooth;
  const cfg = await loadInterpConfig();
  if (!cfg) return { reason: 'the server did not answer' };
  if (!cfg.available) return { reason: cfg.reason || 'not available' };
  if (!st.info) {
    try {
      const r = await fetch(infoRequestUrl(st.f));
      st.info = r.ok ? await r.json() : {};
    } catch (e) {
      st.info = {};
    }
  }
  const fps = Number(st.info.fps) || 0;
  if (!fps) return { reason: 'the frame rate of this video is unknown' };
  const screen = await displayHz();
  st.hz = screen.hz;
  const k = Math.max(1, Math.ceil(st.hz / (cfg.max_output_fps || 240) - 1e-9),
    Math.ceil(st.hz / (fps * SMOOTH_MAX_RATIO) - 1e-9));
  const out = Math.round((st.hz / k) * 1000) / 1000;
  if (out < fps * SMOOTH_MIN_RATIO) {
    return { reason: `this screen ${screen.measured ? 'measured' : 'is set to'} ${st.hz} Hz, `
      + `${out > fps ? 'barely' : 'not'} faster than the video's ${formatFps(fps)} fps`
      + (screen.measured ? ' - if it can do more, set Screen refresh in the ⚙ menu' : '') };
  }
  return { out };
}

// The screen can change under a running stream: the window moved to another
// monitor, or that monitor's rate changed. While the rate is measured (Auto),
// a new rate that holds for two checks re-plans the stream from the same
// place; a rate the viewer set by hand is left alone.
const HZ_WATCH_MS = 2000;
let hzWatch = null;

function watchScreenRate(video) {
  clearInterval(hzWatch);
  let settled = 0;
  hzWatch = setInterval(async () => {
    const st = video._smooth;
    if (!video.isConnected || !st || !st.active) { clearInterval(hzWatch); hzWatch = null; return; }
    if (displayHzSetting() !== 'auto' || video.paused) { settled = 0; return; }
    const now = measuredHz(60);
    if (!now || now === st.hz) { settled = 0; return; }
    if ((settled += 1) < 2) return;
    settled = 0;
    forgetHz();                                      // the old screen's frames are no evidence
    const was = st.out;
    const plan = await planSmoothMotion(video);      // also updates st.hz
    if (!video.isConnected || !st.active) return;
    if (plan.out && plan.out !== was) applySmoothMotion(video, playbackPosition(video));
    else if (!plan.out) {
      st.reason = plan.reason;
      setPreviewEnhancement('interpolation', { note: `Smooth motion: off - ${plan.reason}` });
      playOriginal(video, playbackPosition(video));
    }
  }, HZ_WATCH_MS);
}

function stopSegments(st) {
  if (st.run) {
    st.run.stopped = true;
    st.run.abort.abort();
    st.run = null;
  }
}

function playOriginal(video, at) {
  const st = video._smooth;
  stopSegments(st);
  // Already playing the file (Smooth motion was asked for but declined): keep
  // it running rather than reloading it from the same spot.
  if (!st.active && (video.getAttribute('src') || '').split('#')[0] === st.f.url) return;
  st.active = false;
  st.mode = 'original';
  video._timeline = null;
  video.loop = true;
  // A media fragment, not currentTime at loadedmetadata: that early, Chrome
  // cannot seek a WebM yet and snaps a seek to 60 s back to 0 (measured).
  video.src = at > 0 ? `${st.f.url}#t=${at.toFixed(2)}` : st.f.url;
  startPlayback(video);
}

// ------------------------------------------------------------ segment playback --
// The server keeps interpolated video as 2-second segments on disk (server.py,
// /api/media/interp/seg), so what was made once plays at once: looping, seeking
// back, the next visit. The segments carry the file's own timeline, so the
// element's currentTime is the file's time and seeking and looping are native.
const SEG_AHEAD_S = 8;           // keep this much buffered ahead of the playhead
const SEG_KEEP_BEHIND_S = 10;    // on a full buffer, drop what lies further behind

function segUrl(st, mbps, name, extra = '') {
  const q = new URLSearchParams({ root: st.f.root || '', path: st.f.path, o: String(st.out),
    mbps: String(mbps), name });
  return `${interpRoutes.seg}?${q}${extra}`;
}

// A file of the segment cache. 503 means "still being made": ask again.
async function fetchSegmentFile(url, signal, asJson = false) {
  for (let attempt = 0; attempt < 8; attempt++) {
    const r = await fetch(url, { signal });
    if (r.ok) return asJson ? r.json() : r.arrayBuffer();
    if (r.status !== 503) {
      const err = new Error(`HTTP ${r.status}`);
      err.status = r.status;
      throw err;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error('the server is still making this part - try again in a moment');
}

// appendBuffer / remove as promises.
function bufferOp(sb, op) {
  return new Promise((resolve, reject) => {
    const done = () => { cleanup(); resolve(); };
    const fail = () => { cleanup(); reject(new Error('the browser rejected the video data')); };
    const cleanup = () => {
      sb.removeEventListener('updateend', done);
      sb.removeEventListener('error', fail);
    };
    sb.addEventListener('updateend', done);
    sb.addEventListener('error', fail);
    try { op(); } catch (e) { cleanup(); reject(e); }
  });
}

// Whether segment [start, start + dur) is in the buffer - judged by its first
// half (dur is that segment's real length).
function segmentBuffered(sb, start, dur) {
  const b = sb.buffered;
  for (let i = 0; i < b.length; i++) {
    if (b.start(i) <= start + 0.05 && b.end(i) >= start + dur * 0.5) return true;
  }
  return false;
}

async function startSmoothSegments(video, at, run) {
  const st = video._smooth;
  const cfg = interpConfig || {};
  const mbps = bandwidthCap(cfg);
  const fps = Number(st.info.fps);
  // Where the viewer starts, so the server makes that segment first.
  const guess = Math.floor((at * fps) / Math.max(1, Math.round(2 * fps)));
  const meta = await fetchSegmentFile(segUrl(st, mbps, 'meta.json', `&from=${guess}`), run.abort.signal, true);
  const mime = `video/mp4; codecs="${meta.video_codec}${meta.audio_codec ? `, ${meta.audio_codec}` : ''}"`;
  // Every wait here can outlast the video: the viewer moves on while the worker
  // is still making the first segment. A player no longer in the page must not
  // get a source (its metadata then sized the NEXT item - media.js previewBuild),
  // start playing unseen, or put its rows in the next item's details panel.
  const gone = () => run.stopped || !video.isConnected;
  if (gone()) return;
  if (!window.MediaSource || !MediaSource.isTypeSupported(mime)) {
    throw new Error(`this browser cannot play ${mime}`);
  }
  const segDur = (meta.segment_frames * meta.fps[1]) / meta.fps[0];
  // For the stats panel: what is playing, and every segment fetched.
  st.meta = meta;
  st.segDur = segDur;
  // What the server made: the asked-for rate, unless its ceiling cut it down
  // (segments made before exact rates only say mult).
  st.madeFps = meta.out_fps ? meta.out_fps[0] / meta.out_fps[1] : fps * (Number(meta.mult) || st.out / fps);
  st.net = [];
  let total = meta.segments;
  const ms = new MediaSource();
  const objectUrl = URL.createObjectURL(ms);
  st.active = true;
  st.mode = 'segments';
  video._timeline = null;        // the segments carry the file's own timeline
  video.loop = true;             // native: the start is buffered before the end is reached
  video.src = objectUrl;
  await new Promise((resolve) => ms.addEventListener('sourceopen', resolve, { once: true }));
  if (gone()) { URL.revokeObjectURL(objectUrl); return; }
  ms.duration = meta.duration;
  const sb = ms.addSourceBuffer(mime);
  const init = await fetchSegmentFile(segUrl(st, mbps, 'init.mp4'), run.abort.signal);
  await bufferOp(sb, () => sb.appendBuffer(init));
  if (gone()) { URL.revokeObjectURL(objectUrl); return; }
  if (at > 0) video.currentTime = at;
  startPlayback(video);
  setPreviewEnhancement('interpolation', {
    title: 'Smooth motion',
    rows: [
      ['Frame rate', `${formatFps(fps)} → ${formatFps(st.madeFps)} fps (${formatRatio(st.madeFps / fps)}, `
        + `screen ${st.hz} Hz)`],
      ['Bandwidth cap', bandwidthLabel(mbps)],
      ['Engine', cfg.engine || 'Lossless Scaling frame generation'],
      ['Kept', 'on the server in segments - loops, seeks back and later visits play from there'],
    ],
  });

  // A seek makes whatever is being fetched pointless: abort it, fetch for the
  // new position next.
  const onSeeking = () => {
    run.abort.abort();
    run.abort = new AbortController();
  };
  video.addEventListener('seeking', onSeeking);
  const alive = () => !run.stopped && video.isConnected && video.src === objectUrl
    && ms.readyState !== 'closed';
  // Real segment lengths: the last one ends with the file and can be a fraction
  // of the others (0.33 s in a test), which a "half a segment" rule never saw.
  const segLen = (i) => Math.max(0.05, Math.min(segDur, meta.duration - i * segDur));
  // The last segment is judged by having been appended, not by its length:
  // meta.duration is the longer track's, and sb.buffered only reaches as far as
  // BOTH tracks do. A video whose audio runs on past its last frame (12.05 s of
  // picture, 12.17 s of sound) never buffered "half" of its 0.17 s last segment,
  // so the end was never declared, playback stopped before the loop and that
  // segment was fetched again and again.
  const appended = new Set();
  const lastBuffered = () => (appended.has(total - 1) && segmentBuffered(sb, (total - 1) * segDur, 0.001))
    || segmentBuffered(sb, (total - 1) * segDur, segLen(total - 1));
  const isBuffered = (i) => (i === total - 1 ? lastBuffered() : segmentBuffered(sb, i * segDur, segLen(i)));
  const markEnd = () => {
    // The end must be declared for the element to reach it and loop; appending
    // again (the start, for the loop) reopens the source, so this repeats.
    if (lastBuffered() && ms.readyState === 'open' && !sb.updating) {
      try { ms.endOfStream(); } catch (e) { /* not in a state to end - next time */ }
    }
  };
  try {
    while (alive()) {
      const t = video.currentTime;
      const cur = Math.min(total - 1, Math.max(0, Math.floor(t / segDur)));
      const want = [];
      for (let i = cur; i < total && i * segDur < t + SEG_AHEAD_S; i++) want.push(i);
      if ((total - cur) * segDur < SEG_AHEAD_S) {
        for (let i = 0; i < 2 && i < cur; i++) want.push(i);      // the loop's start
      }
      const next = want.find((i) => !isBuffered(i));
      if (next === undefined) {
        markEnd();
        await new Promise((resolve) => setTimeout(resolve, 250));
        continue;
      }
      let data;
      try {
        const t0 = performance.now();
        data = await fetchSegmentFile(segUrl(st, mbps, `seg_${String(next).padStart(5, '0')}.m4s`),
          run.abort.signal);
        const now = performance.now();
        st.net.push({ t: now, bytes: data.byteLength, ms: now - t0, index: next });
        if (st.net.length > 200) st.net.shift();
      } catch (e) {
        if (e.name === 'AbortError') continue;                    // a seek; go again
        if (e.status === 404 && next === total - 1 && next > 0) { // the estimate ran one over
          total = next;
          continue;
        }
        throw e;
      }
      if (!alive()) break;
      try {
        await bufferOp(sb, () => sb.appendBuffer(data));
      } catch (e) {
        if (e.name !== 'QuotaExceededError') throw e;
        // The browser's buffer is full: drop what is well behind (but not the
        // loop's start) and what is far ahead, then try once more.
        const now = video.currentTime;
        const behind = now - SEG_KEEP_BEHIND_S;
        if (behind > 2 * segDur) await bufferOp(sb, () => sb.remove(2 * segDur, behind));
        await bufferOp(sb, () => sb.remove(now + SEG_AHEAD_S * 2, Infinity));
        appended.clear();                          // the removals may have taken any of them
        await bufferOp(sb, () => sb.appendBuffer(data));
      }
      appended.add(next);
      markEnd();
    }
  } finally {
    video.removeEventListener('seeking', onSeeking);
    URL.revokeObjectURL(objectUrl);
  }
}

// Segments where the browser can play them, else the live stream. A run that
// was stopped on purpose (switched off, another cap, the next file) falls back
// to nothing.
function startSmooth(video, at) {
  const st = video._smooth;
  stopSegments(st);
  const run = { stopped: false, abort: new AbortController() };
  st.run = run;
  startSmoothSegments(video, at, run).catch((err) => {
    if (run.stopped || !video.isConnected) return;    // the viewer moved on: nothing to fall back to
    const pos = st.mode === 'segments' ? video.currentTime : at;
    stopSegments(st);
    if (interpRoutes.cacheOnly) {
      st.reason = 'this rendition is not cached';
      playOriginal(video, pos);
      return;
    }
    console.warn('Smooth motion segments failed, using the live stream:', err);
    startSmoothStream(video, pos);
  });
}

function startSmoothStream(video, at) {
  if (!video.isConnected) return;                    // see startSmoothSegments' gone()
  const st = video._smooth;
  stopSegments(st);
  st.mode = 'stream';
  st.madeFps = st.out;
  const cfg = interpConfig || {};
  const mbps = bandwidthCap(cfg);
  const duration = Number(st.info && st.info.duration) || NaN;
  if (isFinite(duration)) at = Math.max(0, Math.min(at, duration - 0.5));
  st.active = true;
  st.failedAt = null;
  // The server loops the stream itself (loop=1): starting a new one at the end
  // cost over a second of black while the encoder came up again. video.loop
  // stays off - the stream has no end to loop at; 'ended' is only a fallback.
  video.loop = false;
  video._timeline = { offset: at, duration, loop: true, seek: (t) => startSmoothStream(video, t) };
  const q = new URLSearchParams({ root: st.f.root || '', path: st.f.path, t: at.toFixed(2),
    o: String(st.out), mbps: String(mbps), loop: '1' });
  video.src = `${interpRoutes.stream}?${q}`;
  startPlayback(video);
  const fps = Number(st.info.fps);
  setPreviewEnhancement('interpolation', {
    title: 'Smooth motion',
    rows: [
      ['Frame rate', `${formatFps(fps)} → ${formatFps(st.out)} fps (${formatRatio(st.out / fps)}, screen ${st.hz} Hz)`],
      ['Bandwidth cap', bandwidthLabel(mbps)],
      ['Engine', cfg.engine || 'Lossless Scaling frame generation'],
    ],
  });
}

// Decide and start. `generation` drops a decision that finished after the
// viewer already changed the setting again.
async function applySmoothMotion(video, at) {
  const st = video._smooth;
  const generation = ++st.generation;
  setPreviewEnhancement('interpolation', { note: 'Smooth motion: starting…' });
  const plan = await planSmoothMotion(video);
  if (generation !== st.generation || !video.isConnected) return;
  if (!plan.out) {
    st.reason = plan.reason;
    setPreviewEnhancement('interpolation', { note: `Smooth motion: off - ${plan.reason}` });
    playOriginal(video, at);
    return;
  }
  st.out = plan.out;
  st.reason = '';
  startSmooth(video, at);
  watchScreenRate(video);
}

// media.js calls this instead of setting src: plays `f` in `video`, smoothed
// when the viewer has Smooth motion on.
function playPreviewVideo(video, f) {
  video._smooth = { f, info: null, hz: 0, out: 0, active: false, reason: '', generation: 0 };
  video.addEventListener('ended', () => {
    // Only the live stream ends; segments loop natively (video.loop).
    if (video._smooth.active && video._smooth.mode === 'stream') startSmoothStream(video, 0);
  });
  video.addEventListener('error', () => {
    const st = video._smooth;
    // Not in the page: the error is the teardown's (clearPreviewBody), not a failure.
    if (!st.active || !video.isConnected) return;
    // The stream failed (worker busy with another viewer, worker down, ...):
    // carry on with the original from the same place rather than a dead player.
    const at = playbackPosition(video);
    st.reason = 'the stream failed to start - the GPU may be busy with another stream';
    setPreviewEnhancement('interpolation', { note: `Smooth motion: off - ${st.reason}` });
    playOriginal(video, at);
  });
  if (smoothMotionWanted()) {
    applySmoothMotion(video, 0);
  } else {
    playOriginal(video, 0);
  }
  // Stats for nerds stay open from one video to the next. The player wrap is
  // added by the caller right after this returns.
  if (videoStatsWanted) setTimeout(() => showVideoStats(video), 0);
}

// ------------------------------------------------------------ stats for nerds --
// A live panel in the player's top left, like YouTube's "Stats for nerds":
// frames shown and dropped, what is playing (the file, Smooth motion segments
// or the live stream), buffer, network. Toggled from the video's right-click
// menu (media.js); once open it stays open for the next video until closed.

let videoStatsWanted = false;
const STATS_HISTORY = 60;              // samples in the graphs (at 2 per second: 30 s)

function formatBytes(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} kB`;
  return `${n} B`;
}

function formatMbps(bitsPerSecond) {
  return bitsPerSecond >= 1e6 ? `${(bitsPerSecond / 1e6).toFixed(1)} Mbit/s`
    : `${(bitsPerSecond / 1e3).toFixed(0)} kbit/s`;
}

function videoStatsOpen(video) {
  return !!(video && video._stats);
}

function toggleVideoStats(video) {
  videoStatsWanted = !videoStatsOpen(video);
  if (videoStatsWanted) showVideoStats(video);
  else hideVideoStats(video);
}

function hideVideoStats(video) {
  const s = video && video._stats;
  if (!s) return;
  s.closed = true;
  clearInterval(s.timer);
  s.panel.remove();
  video._stats = null;
}

// Seconds of video buffered ahead of the playhead.
function bufferedAhead(video) {
  const b = video.buffered;
  const t = video.currentTime;
  for (let i = 0; i < b.length; i++) {
    if (b.start(i) <= t + 0.05 && b.end(i) > t) return b.end(i) - t;
  }
  return 0;
}

function drawSparkline(canvas, values, max, color) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2 || !(max > 0)) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = (i / (STATS_HISTORY - 1)) * w;
    const y = h - 1 - Math.min(1, v / max) * (h - 2);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function showVideoStats(video) {
  if (!video || video._stats || !video.isConnected) return;
  const wrap = video.closest('.media-player-wrap');
  if (!wrap) return;
  const panel = document.createElement('div');
  panel.className = 'video-stats';
  // Its own clicks must not reach the player (play/pause, fullscreen).
  for (const ev of ['click', 'dblclick', 'contextmenu']) panel.addEventListener(ev, (e) => e.stopPropagation());
  const head = document.createElement('div');
  head.className = 'video-stats-head';
  head.textContent = 'Stats for nerds';
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'video-stats-close';
  close.textContent = '×';
  close.title = 'Close';
  close.addEventListener('click', () => { videoStatsWanted = false; hideVideoStats(video); });
  head.appendChild(close);
  const list = document.createElement('dl');
  panel.append(head, list);
  const graph = (label) => {
    const row = document.createElement('div');
    row.className = 'video-stats-graph';
    const text = document.createElement('span');
    text.textContent = label;
    const canvas = document.createElement('canvas');
    canvas.width = 140;
    canvas.height = 22;
    row.append(text, canvas);
    panel.appendChild(row);
    return canvas;
  };
  const netCanvas = graph('Network');
  const bufCanvas = graph('Buffer');
  wrap.appendChild(panel);

  const s = { panel, closed: false, netHistory: [], bufHistory: [], samples: [], presented: 0 };
  video._stats = s;
  // Frames that actually reached the screen, as the compositor counts them.
  if (typeof video.requestVideoFrameCallback === 'function') {
    const onFrame = (now, meta) => {
      if (s.closed) return;
      s.presented = meta.presentedFrames;
      s.frameSize = `${meta.width} × ${meta.height}`;
      video.requestVideoFrameCallback(onFrame);
    };
    video.requestVideoFrameCallback(onFrame);
  }
  const st = video._smooth || {};
  if (!st.info && st.f) {
    fetch(infoRequestUrl(st.f)).then((r) => (r.ok ? r.json() : null)).then((info) => {
      if (info && !st.info) st.info = info;
    }).catch(() => {});
  }

  // The screen this window renders on, as it is right now - it can change
  // while the panel is open (another monitor, another refresh rate).
  const screenNow = () => {
    const set = displayHzSetting();
    const live = measuredHz();
    if (set === 'auto') return live ? ` on a ${live} Hz screen (measured)` : '';
    return ` on a ${set} Hz screen (set)` + (live && live !== set ? `, measuring ${live} Hz now` : '');
  };

  const update = () => {
    if (!video.isConnected) { hideVideoStats(video); return; }
    const now = performance.now();
    const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : {};
    const total = q.totalVideoFrames || 0;
    const dropped = q.droppedVideoFrames || 0;
    s.samples.push({ t: now, total, dropped, presented: s.presented });
    while (s.samples.length > 1 && now - s.samples[0].t > 5000) s.samples.shift();
    const first = s.samples[0];
    const span = (now - first.t) / 1000;
    const shownFps = span > 0.9 ? (s.presented - first.presented) / span : null;
    const droppedRecent = dropped - first.dropped;
    const decodedRecent = total - first.total;

    const info = st.info || {};
    const mode = st.mode === 'segments' && st.active ? 'segments'
      : st.mode === 'stream' && st.active ? 'stream' : 'original';
    const meta = st.meta || {};
    const outFps = mode === 'original' ? Number(info.fps) || null : st.madeFps || st.out || null;
    const ahead = bufferedAhead(video);

    // Network: what the segment loader fetched in the last 3 s. The file and
    // the live stream are fetched by the browser itself, which does not say.
    let netRate = null;
    let last = null;
    if (mode === 'segments' && st.net) {
      const recent = st.net.filter((n) => now - n.t < 3000);
      netRate = (recent.reduce((a, n) => a + n.bytes, 0) * 8) / 3;
      last = st.net[st.net.length - 1] || null;
    }
    s.netHistory.push(netRate || 0);
    s.bufHistory.push(ahead);
    if (s.netHistory.length > STATS_HISTORY) s.netHistory.shift();
    if (s.bufHistory.length > STATS_HISTORY) s.bufHistory.shift();
    const conn = navigator.connection;

    const rows = [
      ['Playing', mode === 'segments' ? 'Smooth motion - segments from the server cache'
        : mode === 'stream' ? 'Smooth motion - live stream' : 'original file'],
      ['Frame rate', outFps ? `${formatFps(outFps)} fps${mode !== 'original' && Number(info.fps)
        ? ` (${formatFps(Number(info.fps))} × ${Math.round((outFps / Number(info.fps)) * 100) / 100})` : ''}` : null],
      ['Shown', shownFps === null ? '…' : `${shownFps.toFixed(1)} fps${screenNow()}`],
      // Which screen this window is on: its position tells it apart from the
      // others in chrome://gpu's display list, where each display's own
      // refresh rate is listed (two screens of the same size are otherwise
      // impossible to tell apart from in here).
      ['Window', `at ${Math.round(window.screenX)}, ${Math.round(window.screenY)} on a `
        + `${window.screen.width} × ${window.screen.height} screen`
        + (window.devicePixelRatio !== 1 ? ` at ${window.devicePixelRatio}×` : '')],
      ['Frames', `${total.toLocaleString()} decoded / ${dropped.toLocaleString()} dropped`
        + (total ? ` (${((dropped / total) * 100).toFixed(2)}%)` : '')],
      ['Last 5 s', `${decodedRecent} decoded, ${droppedRecent} dropped`],
      ['Resolution', `${video.videoWidth} × ${video.videoHeight}`
        + (currentPreviewEl === video && displayedMediaSize() ? ` → ${displayedMediaSize()} on screen` : '')],
      ['Codecs', mode === 'segments' && meta.video_codec
        ? `${meta.video_codec}${meta.audio_codec ? ` + ${meta.audio_codec}` : ''}`
        : [info.video_codec, info.audio_codec].filter(Boolean).join(' + ') || null],
      ['Buffer', `${ahead.toFixed(1)} s ahead`],
      ['Network', netRate === null ? 'fetched by the browser (not measurable here)'
        : `${formatMbps(netRate)} now, ${formatBytes(st.net.reduce((a, n) => a + n.bytes, 0))} `
          + `in ${st.net.length} segment${st.net.length === 1 ? '' : 's'}`],
      ['Last segment', last ? `#${last.index}, ${formatBytes(last.bytes)}, ${Math.round(last.ms)} ms`
        + (last.ms < 200 ? ' (from disk)' : ' (made while waiting)') : null],
      ['Bandwidth cap', mode !== 'original' ? bandwidthLabel(bandwidthCap(interpConfig)) : null],
      ['File bitrate', info.bit_rate ? formatMbps(info.bit_rate) : null],
      ['Connection', conn && conn.downlink ? `~${conn.downlink} Mbit/s (${conn.effectiveType || '?'}), browser estimate` : null],
      ['Position', `${formatPlaybackTime(playbackPosition(video))} / ${formatPlaybackTime(playbackDuration(video))}`],
    ].filter(([, v]) => v !== null && v !== undefined && v !== '');
    list.replaceChildren(...rows.flatMap(([k, v]) => {
      const dt = document.createElement('dt');
      dt.textContent = k;
      const dd = document.createElement('dd');
      dd.textContent = v;
      return [dt, dd];
    }));
    drawSparkline(netCanvas, s.netHistory, Math.max(...s.netHistory, 1e6), '#6ee7b7');
    drawSparkline(bufCanvas, s.bufHistory, Math.max(...s.bufHistory, SEG_AHEAD_S), '#93c5fd');
  };
  update();
  s.timer = setInterval(update, 500);
}

// ------------------------------------------------------------ settings menu --

function segmentedChoice(options, current, onPick) {
  const group = document.createElement('div');
  group.className = 'player-settings-choice';
  for (const [label, value] of options) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.setAttribute('aria-pressed', String(value === current));
    if (value === current) b.classList.add('selected');
    b.addEventListener('click', () => onPick(value));
    group.appendChild(b);
  }
  return group;
}

async function renderPlayerSettings(menu, video) {
  const cfg = (await loadInterpConfig()) || { bitrates_mbps: [0], default_mbps: 0, available: false,
    reason: 'the server did not answer' };
  const st = video._smooth;
  menu.innerHTML = '';

  const heading = (text) => {
    const h = document.createElement('div');
    h.className = 'player-settings-label';
    h.textContent = text;
    menu.appendChild(h);
  };
  const note = (text) => {
    const p = document.createElement('div');
    p.className = 'player-settings-note';
    p.textContent = text;
    menu.appendChild(p);
  };
  const rerender = () => renderPlayerSettings(menu, video);

  // How far a video filmed sideways is turned (js/rotate.js). First, because
  // the turn chip in the preview header opens this menu to change it.
  if (typeof renderTurnSettings === 'function') renderTurnSettings(menu, video, heading, note, rerender);

  heading('Smooth motion');
  menu.appendChild(segmentedChoice([['Off', false], ['On', true]], smoothMotionWanted(), (on) => {
    storeSetting(SMOOTH_MOTION_KEY, on ? '1' : '0');
    const at = playbackPosition(video);
    if (on) {
      applySmoothMotion(video, at).then(rerender);
    } else {
      st.generation++;
      setPreviewEnhancement('interpolation', null);
      if (st.active) playOriginal(video, at);
    }
    rerender();
  }));
  if (!cfg.available) {
    note(`Not available: ${cfg.reason}`);
  } else if (st.active) {
    const fps = Number(st.info.fps);
    const made = st.madeFps || st.out;
    note(`${formatFps(fps)} → ${formatFps(made)} fps (${formatRatio(made / fps)}) on this ${st.hz} Hz screen`);
  } else if (smoothMotionWanted() && st.reason) {
    note(`Playing the original: ${st.reason}`);
  } else {
    note(interpRoutes.cacheOnly ? 'Plays cached Smooth motion when available; otherwise plays the original.'
      : 'Generates in-between frames on the server so motion is as smooth as this screen allows.');
  }

  heading('Screen refresh');
  const hzNow = displayHzSetting();
  menu.appendChild(segmentedChoice([['Auto', 'auto'], ...DISPLAY_HZ_CHOICES.map((h) => [String(h), h])], hzNow,
    (h) => {
      storeSetting(DISPLAY_HZ_KEY, String(h));
      // A new rate means a new output rate (or none): decide again from here.
      if (smoothMotionWanted()) applySmoothMotion(video, playbackPosition(video)).then(rerender);
      rerender();
    }));
  // What this window renders at right now - the screen it is on, not the main
  // one - so a rate set by hand can be seen to disagree with it.
  measureDisplayHz().then((hz) => {
    const n = menu.querySelector('.player-settings-hz-note');
    if (!n) return;
    if (hzNow === 'auto') {
      n.textContent = `This window renders at ${hz} Hz now, and Smooth motion follows it - `
        + 'the screen it is on, not the main one. A phone that idles at 60 Hz can measure low '
        + 'while nothing moves; then pick the real rate.';
    } else if (hz !== hzNow) {
      n.textContent = `Set to ${hzNow} Hz, but this window renders at ${hz} Hz now, so only ${hz} `
        + 'frames a second can reach the screen. If the screen really is faster, it is the browser '
        + 'painting this window at another rate: it reads the refresh rate when it starts, so restart '
        + 'it after changing one, keep the window on this screen, and make this screen the main one.';
    } else {
      n.textContent = `Set to ${hzNow} Hz, which is what this window renders at now.`;
    }
  });
  note('Measuring…');
  menu.lastChild.classList.add('player-settings-hz-note');

  heading('Bandwidth cap (Mbit/s)');
  const offered = cfg.bitrates_mbps || [0];
  menu.appendChild(segmentedChoice(offered.map((m) => [m ? String(m) : 'Max', m]), bandwidthCap(cfg), (m) => {
    storeSetting(BANDWIDTH_KEY, String(m));
    if (st.active) startSmooth(video, playbackPosition(video));   // new cap from here on
    rerender();
  }));
  note('Limits Smooth motion streams. Without Smooth motion the file is sent as it is stored.');
}

// The gear in the control bar (wrapWithCustomControls extras).
function playerSettingsButton(video) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'custom-controls-btn player-settings-btn';
  btn.title = 'Video settings';
  btn.setAttribute('aria-label', 'Video settings');
  btn.textContent = '⚙';
  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const wrap = btn.closest('.media-player-wrap');
    const open = wrap.querySelector('.player-settings-menu');
    if (open) { open.remove(); return; }
    const menu = document.createElement('div');
    menu.className = 'player-settings-menu';
    menu.addEventListener('click', (e) => e.stopPropagation());
    menu.addEventListener('dblclick', (e) => e.stopPropagation());
    wrap.appendChild(menu);
    renderPlayerSettings(menu, video);
    const close = (e) => {
      if (!menu.isConnected) { document.removeEventListener('pointerdown', close, true); return; }
      if (menu.contains(e.target) || btn.contains(e.target)) return;
      menu.remove();
      document.removeEventListener('pointerdown', close, true);
    };
    document.addEventListener('pointerdown', close, true);
  });
  return btn;
}
