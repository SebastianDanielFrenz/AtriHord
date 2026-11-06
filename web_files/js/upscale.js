// Live upscaling in the preview, shared by the library (media.js) and the share
// page (public_share.js). An image looked at for view_seconds - a music track's
// cover at once - is upscaled on the GPU host until a zoom of zoom_factor on THIS
// screen stays sharp ("Live upscaling" in server.py), then swapped in: decoded
// off-screen first, then put into the same element, so its size, zoom and pan
// stay where they are and nothing flashes. Something upscaled on an earlier
// visit is swapped in on opening.
//
// Needs player.js (setPreviewEnhancement, pinUpscaledSize, formatDateTime) and,
// from the page, `currentPreviewKey`: what the preview shows now, so an answer
// for a picture already left behind is dropped. The page calls
// resetLiveUpscale() whenever the preview moves to another item.

// Where to ask. The share page points these at its token-gated routes and stays
// quiet while upscaling is off: a visitor has no Settings to turn it on in.
// setModel is null where nobody may change it: a share page is somebody else's
// link, and its visitor has no Settings and no say over the library.
const upscaleRoutes = {
  config: '/api/media/upscale/config',
  ask: '/api/media/upscale',
  setModel: '/api/media/upscale/model',
  quietWhenOff: false,
};

// Where the chip that flips between the original and the upscale goes. The
// library and the share page put it in the preview's header; the music page's
// now-playing screen names its own place. Same kind of seam as upscaleRoutes.
const upscaleHost = {
  chip: () => document.querySelector('#mediaPreviewModal .modal-header-actions'),
};

let upscaleConfig = null;
let previewUpscale = null;   // {key, img, url, factor, ext, cover} of the upscaled copy on screen
let upscaleConfigAt = 0;

// Re-read at most every 30 s, so a change in Settings reaches a library page
// that is already open with the next picture, not only after a reload.
function loadUpscaleConfig() {
  if (upscaleConfig && Date.now() - upscaleConfigAt < 30000) return Promise.resolve(upscaleConfig);
  return fetch(upscaleRoutes.config)
    .then((r) => (r.ok ? r.json() : upscaleConfig))
    .then((c) => { upscaleConfig = c; upscaleConfigAt = Date.now(); return c; })
    .catch(() => upscaleConfig);             // no upscaling rather than a broken preview
}
const UPSCALE_FINAL = ['not_needed', 'unsupported', 'too_large', 'disabled', 'missing', 'failed', 'error'];

// The frame after the next one - the first frame that has certainly been put on
// screen. A tab nobody is looking at never paints at all, so the wait is
// capped: the swap has to finish either way.
function painted() {
  return new Promise((resolve) => {
    let done = false;
    const go = () => { if (!done) { done = true; resolve(); } };
    requestAnimationFrame(() => requestAnimationFrame(go));
    setTimeout(go, 300);
  });
}

// Put another picture into an element that is already showing one, without the
// box going empty in between (user, 2026-09-23: "I saw a flash on the music
// page when the upscaled variant arrived").
//
// decode() is not enough on its own: it decodes the picture at its own size,
// while what an element paints is that picture scaled into its box - a raster
// the browser makes when it first paints it there. For a copy of tens of
// megapixels that is not within the frame in which `src` changes, and an
// element with decoding="async" (the music page's cover) has told the browser
// it may present that frame with nothing in the box.
//
// So the picture is painted in a copy of the element first: the same size and
// the same object-fit, in the corner of the viewport at one per cent opacity -
// invisible, but rasterised, and rasterised at the size it is about to be shown
// at. Then the element takes it with decoding="sync", which asks for it to be
// presented WITH that frame rather than a frame later.
//
// true when it was swapped, false when the browser cannot open the picture at
// all, null when whatever was waiting for it has moved on. `before` runs in the
// same breath as the swap, for anything that has to change with it.
async function swapPicture(img, url, stillWanted, before) {
  const box = img.getBoundingClientRect();
  const ghost = document.createElement('img');
  ghost.src = url;
  ghost.decoding = 'sync';
  ghost.setAttribute('aria-hidden', 'true');
  ghost.style.cssText = 'position:fixed;left:0;top:0;opacity:0.01;pointer-events:none;'
    + 'z-index:2147483647;'
    + `width:${Math.max(1, Math.round(box.width))}px;height:${Math.max(1, Math.round(box.height))}px;`
    + `object-fit:${getComputedStyle(img).objectFit || 'contain'}`;
  // Inside whatever is being drawn. Only a fullscreen element and what is in
  // it is painted, so a copy put in the page BELOW one would never be
  // rasterised - and would never finish decoding either (player.js says the
  // same thing about the preview's own chrome). media-dl's preview goes
  // fullscreen on itself; the music page goes fullscreen on the document, and
  // then the page is what is drawn.
  const fs = document.fullscreenElement;
  (fs && !fs.contains(document.body) ? fs : document.body).appendChild(ghost);
  // A decode that never comes back - a window that is not being drawn does not
  // finish one - must not cost the upscale itself: the warm-up is given up on,
  // the copy taken out of the page, and the picture put in the old way.
  const ready = await Promise.race([
    ghost.decode().then(() => 'ready', () => 'bad'),
    new Promise((resolve) => setTimeout(() => resolve('slow'), 15000)),
  ]);
  if (ready === 'bad') {
    ghost.remove();
    return false;
  }
  if (ready === 'slow') ghost.remove();
  else await painted();
  if (stillWanted && !stillWanted()) {
    ghost.remove();
    return null;
  }
  if (before) before();
  const had = img.getAttribute('decoding');
  img.decoding = 'sync';           // presented WITH this frame, not a frame later
  img.src = url;
  painted().then(() => {
    if (had === null) img.removeAttribute('decoding');
    else img.setAttribute('decoding', had);
    ghost.remove();
  });
  return true;
}

// The details panel's line for every answer that is not a picture to swap in,
// so "why was this not upscaled?" always has an answer there.
function upscaleNote(data, need, cfg, whole) {
  const zoom = Number(cfg.zoom_factor) || 2;
  switch (data.status) {
    case 'not_needed':
      // A cover is never zoomed, it is filled to the screen, so telling its
      // reader about a zoom factor explains the wrong number to them.
      return `Upscaling: not needed - ${data.width} × ${data.height}`
        + (data.soft ? ` (taken as that: ${data.soft.auto ? 'measured as a soft cover' : 'a soft cover'})` : '')
        + ' already '
        + (whole ? 'fills this screen' : `covers a ${zoom}× zoom here`)
        + ` (${need.w} × ${need.h} needed)`;
    case 'too_large':
      if (data.limit_by === 'cover_size') {
        const [w, h] = data.limit_size || [];
        return `Upscaling: skipped - the cover is already ${data.width} × ${data.height}, `
          + `about the Largest song cover in Settings (${w} × ${h})`;
      }
      return `Upscaling: skipped - the file is already ${data.original_mp} MP, next to `
        + `${data.limit_by === 'worker' ? "the vision worker's limit" : 'Largest result in Settings'} of ${data.limit_mp} MP`;
    case 'undecodable':
      return `Upscaling: this browser could not open the ${Math.round((data.width * data.height) / 1e6)} MP result`
        + ' - a lower Largest result in Settings avoids that';
    case 'unsupported':
      return 'Upscaling: not available for this file type';
    case 'disabled':
      return 'Upscaling is off in Settings';
    case 'failed':
      return `Upscaling failed: ${data.error || 'unknown error'}. Tried again after 10 minutes.`;
    case 'queued':
    case 'working':
      return 'Upscaling: running on the GPU…';
    case 'waiting':
      return `Upscaling: starts after ${Number(cfg.view_seconds) || 5} s of viewing`;
    default:
      return null;
  }
}

// img is the picture on screen: the image itself, or a track's cover.
// opts.immediate skips the view_seconds wait: it is set when the ask comes from
// a click (setUpscaleModel), which IS the "looked at it long enough" the wait
// stands in for.
async function startLiveUpscale(img, f, opts) {
  const immediate = !!(opts && opts.immediate);
  if (f.type !== 'image' && f.type !== 'audio') return;
  const key = currentPreviewKey;
  const cfg = await loadUpscaleConfig();
  const showing = () => currentPreviewKey === key && img.isConnected && !img.dataset.upscaled;
  if (!cfg || !showing()) return;
  // The layout size, not the zoomed box - offsetWidth ignores the transform.
  const need = () => {
    const dpr = window.devicePixelRatio || 1;
    if (f.type === 'audio') {
      // A song's cover is shown whole, never zoomed, at most in fullscreen:
      // it needs only enough pixels to fill the SCREEN it is on, one way at
      // least - not the zoom factor pictures get (user, 2026-09-18).
      //
      // The screen has to be tried BOTH ways up. window.screen does not turn
      // with the phone everywhere - in the Android app it keeps reporting the
      // device's natural orientation however the phone is held - so a landscape
      // cover held in landscape was measured against the screen's SHORT side
      // and came out at a third of the pixels it is really drawn on (user,
      // 2026-09-23: "Incorrect height readout. I have over 1080 in height").
      // The window is a third candidate because it is always honest about NOW,
      // and on a desktop the screen is the larger of the two anyway - a window
      // can still be enlarged, which is why it was the screen to begin with.
      const nw = img.naturalWidth || img.offsetWidth;
      const nh = img.naturalHeight || img.offsetHeight;
      const sw = ((window.screen && window.screen.width) || 0) * dpr;
      const sh = ((window.screen && window.screen.height) || 0) * dpr;
      const boxes = [[sw, sh], [sh, sw], [window.innerWidth * dpr, window.innerHeight * dpr]];
      let fit = 0;
      for (const [bw, bh] of boxes) {
        if (bw > 0 && bh > 0 && nw > 0 && nh > 0) fit = Math.max(fit, Math.min(bw / nw, bh / nh));
      }
      if (!(fit > 0) || !Number.isFinite(fit)) fit = 1;
      return { w: Math.ceil(nw * fit), h: Math.ceil(nh * fit) };
    }
    const zoom = Number(cfg.zoom_factor) || 2;
    return { w: Math.ceil(img.offsetWidth * dpr * zoom), h: Math.ceil(img.offsetHeight * dpr * zoom) };
  };
  const note = (data) => {
    if (!showing()) return;
    const text = upscaleNote(data, need(), cfg, f.type === 'audio');
    setPreviewEnhancement('upscale', text ? { note: text } : null);   // player.js: the details panel
  };
  if (!cfg.enabled) {
    if (!upscaleRoutes.quietWhenOff) note({ status: 'disabled' });
    return;
  }
  const ask = async (extra) => {
    const size = need();
    const res = await fetch(upscaleRoutes.ask, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        root: f.root || (typeof currentMediaRoot === 'string' ? currentMediaRoot : ''),
        path: f.path,
        need_w: size.w,
        need_h: size.h,
        ...extra,
      }),
    });
    return res.ok ? res.json() : { status: 'error' };
  };
  const swapIn = async (data) => {
    // What the chip flips back to: the file for an image, the cover for a track.
    const originalUrl = img.src;
    const swapped = await swapPicture(img, data.url, showing, () => {
      img.dataset.upscaled = String(data.factor);
      pinUpscaledSize(img);          // player.js: the original's size on screen
    });
    if (swapped === false) {
      note({ status: 'undecodable', width: data.width, height: data.height });
      return;
    }
    if (!swapped) return;            // left behind while it was being made ready
    // Download upscaled, and what the chip flips between.
    // f comes along so the panel's "wrong model for this file" buttons know
    // which file they are about after the ask that produced this is long gone.
    previewUpscale = { key, img, url: data.url, originalUrl, factor: data.factor, scale: data.scale || data.factor,
      ext: data.ext, data, f, showing: 'upscaled', cover: !!data.cover };
    showUpscaleChip(data);
    setPreviewEnhancement('upscale', upscaleDetails(data, f));   // player.js: the details panel
  };
  const poll = async () => {
    // A 4x pass on a big picture takes seconds, a queue of them longer.
    for (let i = 0; i < 300 && showing(); i++) {
      let data;
      try { data = await ask({}); } catch (e) { return; }
      if (data.status === 'ready') { await swapIn(data); return; }
      note(data);
      if (UPSCALE_FINAL.includes(data.status)) return;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  };
  // Only time actually spent looking counts: a hidden tab starts over once
  // it is visible again. A song's cover is asked for at once (user,
  // 2026-09-19: "within the first second") - it is on screen for the whole
  // song, not paged past like pictures, which wait view_seconds so that
  // leafing through a folder upscales nothing. Visible still counts for it:
  // a song playing in a tab nobody looks at needs no sharper cover.
  const waitMs = (f.type === 'audio' || immediate) ? 0 : (Number(cfg.view_seconds) || 5) * 1000;
  const afterViewing = () => {
    setTimeout(() => {
      if (!showing()) return;
      if (document.visibilityState !== 'visible') {
        document.addEventListener('visibilitychange', afterViewing, { once: true });
        return;
      }
      poll();
    }, waitMs);
  };
  ask({ check_only: true }).then((data) => {
    if (!showing()) return;
    if (data.status === 'ready') {
      swapIn(data);
    } else if (data.status === 'queued' || data.status === 'working') {
      note(data);
      poll();
    } else if (UPSCALE_FINAL.includes(data.status)) {
      note(data);
    } else {
      if (waitMs) note({ status: 'waiting' });
      afterViewing();
    }
  }).catch(() => {});
}

// How the details panel (player.js renderInfoPanel) describes the upscaled copy
// on screen. The file itself is never changed.
// The scale actually served: a whole factor, or a fractional one where a
// megapixel limit bound (plan_upscale). One decimal on screen.
function upscaleScaleLabel(data) {
  const s = Number(data.scale || data.factor);
  return Number.isInteger(s) ? String(s) : s.toFixed(1);
}

// Why the factor is lower than the zoom needed, if it is: the largest factor
// setting, or a megapixel limit - the setting or the vision worker's own.
function upscaleLimitText(data) {
  const limited = data.limited;
  if (!limited) return null;
  if (limited.by === 'max_factor') return `${upscaleScaleLabel(data)}× by Largest upscale factor (${limited.needed}× needed)`;
  if (limited.by === 'cover_size') {
    const [w, h] = limited.size || [];
    return `${upscaleScaleLabel(data)}× - shrunk to fit the Largest song cover in Settings (${w} × ${h})`;
  }
  const which = limited.by === 'worker' ? "the vision worker's limit" : 'Largest result';
  return `${upscaleScaleLabel(data)}× - the most ${which} of ${limited.limit_mp} MP allows (${limited.needed}× needed)`;
}

// A model's name as people know it. The server's list comes with the upscale
// config (UPSCALE_MODEL_LABELS in server.py); the key itself if it is not there.
function upscaleModelLabel(model) {
  const labels = (upscaleConfig && upscaleConfig.model_labels) || {};
  return labels[model] || model;
}

// The model is picked from what the vision models saw, and on some files there
// is nothing there to read differently - a dark cosplay photograph captioned
// "the image is animated" and tagged only "1girl" has three reasons for the
// anime model and none for the right one. These buttons say so for this one
// file; the server keeps the answer and it wins over every rule (server.py,
// "THE MODEL FOR ONE FILE, SET BY HAND").
function upscaleModelActions(data, f) {
  if (!upscaleRoutes.setModel || !f || !f.path) return [];
  const isAnime = data.model === 'anime';
  const set = (kind) => setUpscaleModel(f, kind);
  const actions = [
    { label: 'Photograph', active: !isAnime, disabled: !isAnime && data.override === 'photo',
      title: `Use the photo model (${upscaleModelLabel((upscaleConfig && upscaleConfig.photo_model) || '')}) for this file`,
      onClick: () => set('photo') },
    { label: 'Artwork', active: isAnime, disabled: isAnime && data.override === 'anime',
      title: 'Use the anime model for this file', onClick: () => set('anime') },
  ];
  if (data.override) {
    actions.push({ label: 'Automatic', title: 'Go back to deciding this file by the rules',
      onClick: () => set(null) });
  }
  return actions;
}

// The film grain row: what the vision worker measured on the original and
// whether it laid grain back over the upscale (artwork only; null otherwise).
// Strength is the grain's standard deviation on 0-255, at the original's scale.
function upscaleGrainText(g) {
  if (!g) return null;
  if (g.added) {
    const kind = g.mono >= 0.8 ? 'monochrome' : g.mono <= 0.4 ? 'coloured' : 'lightly coloured';
    const left = typeof g.kept === 'number' ? `; the model had left ${g.kept.toFixed(1)}` : '';
    // How much finer than the original's pixels it was drawn (vision/grain.py FINER).
    const fine = typeof g.fine === 'number' && g.fine > 1.05 ? `, drawn ${+g.fine.toFixed(1)}× finer` : '';
    return `put back - strength ${Number(g.sigma).toFixed(1)}, ${kind}${fine}${left}`;
  }
  const why = String(g.why || '');
  if (/^flat areas are clean/.test(why)) return 'none in the original';
  if (/^a regular pattern/.test(why)) return 'none - a regular pattern (screentone), not grain';
  if (/^only \d+ % of flat areas/.test(why)) return 'none - only some areas are textured';
  if (/^the upscale kept the grain/.test(why)) return 'kept by the model, nothing to add';
  return why ? `not added - ${why}` : 'not added';
}

// A soft cover - from a folder in Settings > Soft covers, or found by measuring
// its edges - is shrunk before the GPU pass (server.py, "Soft covers"), so the
// factor in the title is from that size. Which of the two it was is always
// said (user, 2026-09-28: "The info panel must list when this happened").
function upscaleSoftText(soft) {
  if (!soft || !soft.size || !soft.file) return null;
  const file = `(the file holds ${soft.file[0]} × ${soft.file[1]})`;
  if (soft.auto) {
    return `${soft.size[0]} × ${soft.size[1]} - found soft by measuring: edge steepness `
      + `${Number(soft.auto.edge).toFixed(2)}, at most ${Number(soft.auto.limit).toFixed(2)} counts as soft ${file}`;
  }
  return `${soft.size[0]} × ${soft.size[1]} - ${soft.folder || 'its folder'} is a soft-cover folder ${file}`;
}

function upscaleDetails(data, f) {
  return {
    title: `${data.cover ? 'Cover shown' : 'Shown'} upscaled ${upscaleScaleLabel(data)}×`,
    rows: [
      ['Shown at', data.width && data.height ? `${data.width} × ${data.height}` : null],
      ['Taken as', upscaleSoftText(data.soft)],
      ['Model', upscaleModelLabel(data.model)],
      ['Why this model', data.reason],
      ['Film grain', upscaleGrainText(data.grain)],
      ['Limited to', upscaleLimitText(data)],
      ['GPU time', data.seconds ? `${Number(data.seconds).toFixed(1)} s` : null],
      ['Upscaled', data.created ? formatDateTime(data.created) : null],
      ['File on disk', 'unchanged - a cached copy is shown'],
    ],
    actions: upscaleModelActions(data, f),
  };
}

// Set the model for one file and show the result at once. The copy made under
// the old model stays in the cache under its own key, so going back costs one
// pass the first time and nothing after that.
async function setUpscaleModel(f, kind) {
  const up = previewUpscale;
  if (!up || !up.img || currentPreviewKey !== up.key) return;
  let res;
  try {
    res = await fetch(upscaleRoutes.setModel, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        root: f.root || (typeof currentMediaRoot === 'string' ? currentMediaRoot : ''),
        path: f.path,
        model: kind,
      }),
    });
  } catch (e) {
    return;
  }
  if (!res.ok || previewUpscale !== up || currentPreviewKey !== up.key || !up.img.isConnected) return;
  // Back to the original, then ask again as if the picture had just been
  // opened: dataset.upscaled is what stops a second ask, so it has to go.
  const img = up.img;
  if (up.showing === 'upscaled' && up.originalUrl) img.src = up.originalUrl;
  delete img.dataset.upscaled;
  resetLiveUpscale();
  setPreviewEnhancement('upscale', { note: 'Upscaling: running on the GPU…' });
  startLiveUpscale(img, f, { immediate: true });
}

function showUpscaleChip(data) {
  const actions = upscaleHost.chip();
  if (!actions) return;
  let chip = document.getElementById('mediaUpscaleChip');
  if (!chip) {
    chip = document.createElement('button');
    chip.type = 'button';
    chip.id = 'mediaUpscaleChip';
    chip.className = 'upscale-chip';
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleUpscaleView();
    });
    actions.prepend(chip);
  }
  paintUpscaleChip(data);
}

function paintUpscaleChip(data) {
  const chip = document.getElementById('mediaUpscaleChip');
  if (!chip) return;
  const scale = `${upscaleScaleLabel(data)}×`;
  const original = !!previewUpscale && previewUpscale.showing === 'original';
  // The chip may say "AI" - it is the only word there. What does not need
  // it is "upscaled", which implies it (user, 2026-09-23: "the words
  // 'upscaled' or 'interpolated' and 'AI' do not need to occur together"), so
  // no line here says both.
  chip.textContent = original ? 'Original' : `${scale} AI`;
  chip.setAttribute('aria-pressed', String(!original));
  // No tooltip (user, 2026-09-23: "The upscale chip should not have
  // description"): the model, why it was chosen and the size are in the
  // details panel, which is where the preview describes what it does.
}

// The chip flips the preview between the upscaled copy and the original, in the
// same element: decoded first, so the frame never goes blank, and the size stays
// pinned to the original's (pinUpscaledSize), so position and zoom stay put.
// img.dataset.upscaled stays set either way - it means "an upscale is loaded",
// which is what keeps startLiveUpscale from asking again.
async function toggleUpscaleView() {
  const up = previewUpscale;
  const img = up && up.img;
  if (!up || !img || currentPreviewKey !== up.key) return;
  const toOriginal = up.showing === 'upscaled';
  const url = toOriginal ? up.originalUrl : up.url;
  const swapped = await swapPicture(img, url,
    () => previewUpscale === up && currentPreviewKey === up.key && img.isConnected);
  if (!swapped) return;
  up.showing = toOriginal ? 'original' : 'upscaled';
  paintUpscaleChip(up.data);
  setPreviewEnhancement('upscale', toOriginal
    ? { note: `Showing the original ${up.cover ? 'cover' : 'file'} - the ${upscaleScaleLabel(up.data)}× upscale is one click away (the chip)` }
    : upscaleDetails(up.data, up.f));
}

// The preview moved on: no chip, no upscaled copy to download or flip to.
function resetLiveUpscale() {
  const chip = document.getElementById('mediaUpscaleChip');
  if (chip) chip.remove();
  previewUpscale = null;
}
