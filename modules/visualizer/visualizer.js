// The full-screen visualizer view of the music page (module "visualizer", see module.js).
//   AtriVisualizer.open(host)   host = { ctx: AudioContext, input: AudioNode (the stereo bus tap),
//                                        nowPlaying(): { title, artist, cover } | null, onClose?() }
//   AtriVisualizer.songChanged()   the page calls it when another song starts (caption, colour scheme)
//   AtriVisualizer.close()
// Butterchurn renders; the conductor (conductor.js) plays the scenes one after another and changes them on phrases,
// drops or the "Next scene" trigger. The picture listens through a delay of its own, for speakers that play late.
(function () {
  'use strict';
  const KEY = 'atriVisualizer';
  const DEFAULTS = { collection: 'mixed', changes: 'auto', phrases: 2, scheme: 'auto', delay: 0, quality: 1 };
  let cfg = Object.assign({}, DEFAULTS);
  try { Object.assign(cfg, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) { /* private window */ }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(cfg)); } catch (e) { /* private window */ } };

  let host = null, root = null, canvas = null, vis = null, delay = null, feat = null, conductor = null;
  let raf = 0, open = false, lastT = 0, idleTimer = 0, coverScheme = 'spectrum', songKey = '';
  const presets = {};          // name -> preset object (our scenes are rebuilt when the scheme changes)
  let own = [], curated = [];
  const perf = { acc: 0, n: 0, low: 0 };

  const CSS = `
.avis{position:fixed;inset:0;z-index:2147483000;background:#05060c;color:#e9ecf5;font:14px/1.4 system-ui,sans-serif;cursor:default}
.avis canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.avis.idle{cursor:none}
.avis-bar{position:absolute;top:16px;right:16px;display:flex;gap:8px;transition:opacity .4s}
.avis-bar button{width:44px;height:44px;border-radius:50%;border:0;background:rgba(10,12,24,.55);color:#e9ecf5;display:grid;place-items:center;cursor:pointer;backdrop-filter:blur(8px)}
.avis-bar button:hover,.avis-bar button[aria-pressed=true]{background:rgba(57,135,229,.55)}
.avis-bar svg{width:22px;height:22px}
.avis-cap{position:absolute;left:24px;bottom:24px;display:flex;gap:14px;align-items:center;max-width:min(70vw,640px);transition:opacity .6s}
.avis-cap img{width:56px;height:56px;border-radius:8px;object-fit:cover;box-shadow:0 4px 18px rgba(0,0,0,.5)}
.avis-cap b{display:block;font-size:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-shadow:0 1px 8px rgba(0,0,0,.7)}
.avis-cap span{display:block;opacity:.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-shadow:0 1px 8px rgba(0,0,0,.7)}
.avis-scene{position:absolute;top:24px;left:24px;opacity:0;font-size:13px;letter-spacing:.04em;text-shadow:0 1px 8px rgba(0,0,0,.8);transition:opacity .6s}
.avis-scene.show{opacity:.8}
.avis-panel{position:absolute;top:72px;right:16px;width:280px;padding:12px 14px;border-radius:12px;background:rgba(10,12,24,.82);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.08)}
.avis-panel label{display:grid;grid-template-columns:96px 1fr;gap:8px;align-items:center;margin:8px 0}
.avis-panel select,.avis-panel input{width:100%;box-sizing:border-box;background:#1b1e2c;color:#e9ecf5;border:1px solid #333a52;border-radius:6px;padding:4px 6px;font:inherit}
.avis-panel input[type=range]{padding:0}
.avis-panel small{display:block;opacity:.6;margin-top:6px}
.avis.idle .avis-bar,.avis.idle .avis-panel{opacity:0;pointer-events:none}
.avis.idle .avis-cap{opacity:.55}
`;
  const ICON = {
    next: 'M6 18l8.5-6L6 6v12zm10-12v12h2V6h-2z',
    gear: 'M19.4 13a7.8 7.8 0 0 0 0-2l2.1-1.6a.5.5 0 0 0 .1-.6l-2-3.5a.5.5 0 0 0-.6-.2l-2.5 1a7.3 7.3 0 0 0-1.7-1l-.4-2.6a.5.5 0 0 0-.5-.4h-4a.5.5 0 0 0-.5.4l-.4 2.6a7.3 7.3 0 0 0-1.7 1l-2.5-1a.5.5 0 0 0-.6.2l-2 3.5a.5.5 0 0 0 .1.6L4.6 11a7.8 7.8 0 0 0 0 2l-2.1 1.6a.5.5 0 0 0-.1.6l2 3.5c.1.2.4.3.6.2l2.5-1c.5.4 1.1.8 1.7 1l.4 2.6c0 .3.2.4.5.4h4c.3 0 .5-.1.5-.4l.4-2.6c.6-.2 1.2-.6 1.7-1l2.5 1c.2.1.5 0 .6-.2l2-3.5a.5.5 0 0 0-.1-.6L19.4 13zM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z',
    full: 'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
    close: 'M19 6.4L17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z',
  };
  const svg = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><path d="${d}"/></svg>`;
  const $ = (sel) => root.querySelector(sel);
  const opt = (v, t, cur) => `<option value="${v}"${v === String(cur) ? ' selected' : ''}>${t}</option>`;

  function build() {
    const st = document.createElement('style');
    st.textContent = CSS;
    document.head.append(st);
    root = document.createElement('div');
    root.className = 'avis';
    root.hidden = true;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-label', 'Visualizer');
    const schemes = Object.keys(VisDesign.schemes);
    root.innerHTML = `
<canvas></canvas>
<div class="avis-scene"></div>
<div class="avis-cap"><img alt="" hidden><div><b></b><span></span></div></div>
<div class="avis-bar">
  <button type="button" data-a="next" title="Next scene (A)" aria-label="Next scene">${svg(ICON.next)}</button>
  <button type="button" data-a="settings" title="Settings" aria-label="Settings" aria-pressed="false">${svg(ICON.gear)}</button>
  <button type="button" data-a="full" title="Fullscreen (F)" aria-label="Fullscreen">${svg(ICON.full)}</button>
  <button type="button" data-a="close" title="Close (Esc)" aria-label="Close">${svg(ICON.close)}</button>
</div>
<div class="avis-panel" hidden>
  <label>Scenes <select data-k="collection">${opt('mixed', 'AtriHord + curated', cfg.collection)}${opt('atrihord', 'AtriHord only', cfg.collection)}${opt('curated', 'Curated only', cfg.collection)}</select></label>
  <label>Changes <select data-k="changes">${opt('auto', 'On phrases and drops', cfg.changes)}${opt('drops', 'On drops only', cfg.changes)}${opt('manual', 'Only by hand (A)', cfg.changes)}</select></label>
  <label>Phrases <input type="range" data-k="phrases" min="1" max="8" step="1" value="${cfg.phrases}"></label>
  <label>Colours <select data-k="scheme">${opt('auto', 'From the cover', cfg.scheme)}${schemes.map((s) => opt(s, s[0].toUpperCase() + s.slice(1), cfg.scheme)).join('')}</select></label>
  <label>Delay <input type="range" data-k="delay" min="0" max="3000" step="10" value="${cfg.delay}"></label>
  <label>Quality <select data-k="quality">${opt('1', 'Full', cfg.quality)}${opt('0.75', 'Balanced', cfg.quality)}${opt('0.5', 'Light', cfg.quality)}</select></label>
  <small class="avis-info"></small>
</div>`;
    canvas = $('canvas');
    document.body.append(root);
    root.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-a]');
      if (!b) return;
      const a = b.dataset.a;
      if (a === 'next') nextScene();
      else if (a === 'settings') togglePanel();
      else if (a === 'full') toggleFullscreen();
      else if (a === 'close') close();
    });
    root.querySelector('.avis-panel').addEventListener('input', (e) => {
      const k = e.target.dataset.k;
      if (!k) return;
      cfg[k] = e.target.type === 'range' || k === 'quality' ? +e.target.value : e.target.value;
      save();
      apply(k);
    });
    for (const ev of ['pointermove', 'pointerdown', 'wheel']) root.addEventListener(ev, wake, { passive: true });
    new ResizeObserver(size).observe(canvas);
  }

  function info() {
    const el = root.querySelector('.avis-info');
    if (el) el.textContent = 'Delay ' + cfg.delay + ' ms · ' + (cfg.scheme === 'auto' ? 'colours from the cover: ' + coverScheme : 'colours: ' + cfg.scheme);
  }
  function apply(k) {
    if (k === 'delay' && delay) delay.delayTime.setTargetAtTime(cfg.delay / 1000, delay.context.currentTime, 0.05);
    if (k === 'quality') size();
    if (k === 'changes' && conductor) conductor.setMode(cfg.changes);
    if (k === 'phrases' && conductor) conductor.setPhrases(cfg.phrases);
    if (k === 'collection' && conductor) { conductor.setScenes(sceneList()); conductor.start(0); }
    if (k === 'scheme') useScheme(cfg.scheme === 'auto' ? coverScheme : cfg.scheme);
    info();
  }
  function togglePanel(on) {
    const p = root.querySelector('.avis-panel');
    p.hidden = on == null ? !p.hidden : !on;
    root.querySelector('[data-a=settings]').setAttribute('aria-pressed', String(!p.hidden));
    info();
  }
  function wake() {
    root.classList.remove('idle');
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (root.querySelector('.avis-panel').hidden) root.classList.add('idle'); }, 2500);
  }
  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else root.requestFullscreen().catch(() => {});
  }

  // ---- presets and scenes ---------------------------------------------------------------------------------------
  function buildOwn() {
    own = [];
    for (const s of VisPresets.set(VisDesign)) { presets[s.name] = s.preset; own.push(s.name); }
  }
  function sceneList() {
    if (cfg.collection === 'atrihord') return own.slice();
    const cur = curated.slice();
    for (let i = cur.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [cur[i], cur[j]] = [cur[j], cur[i]]; }
    if (cfg.collection === 'curated') return cur;
    // mixed: our scenes in order, a curated preset between each two
    const out = [];
    const n = Math.max(own.length, cur.length);
    for (let i = 0; i < n; i++) { if (own.length) out.push(own[i % own.length]); if (cur[i]) out.push(cur[i]); }
    return out;
  }
  let current = '', sceneTimer = 0;
  function load(name, blend) {
    if (!vis || !presets[name]) return;
    current = name;
    try { vis.loadPreset(presets[name], blend); } catch (e) { console.warn('visualizer preset', name, e); }
    const el = $('.avis-scene');
    el.textContent = name.replace(/^AtriHord - /, '');
    el.classList.add('show');
    clearTimeout(sceneTimer);
    sceneTimer = setTimeout(() => el.classList.remove('show'), 2500);
  }
  function nextScene() { if (conductor) conductor.next('trigger'); }
  function useScheme(name) {
    if (VisDesign.scheme === name) return;
    VisDesign.use(name);
    buildOwn();
    if (own.includes(current)) load(current, 1.5);
  }

  // ---- the song: caption and (in "auto") the colour scheme from the cover ---------------------------------------
  function songChanged() {
    if (!root || !host) return;
    const np = host.nowPlaying && host.nowPlaying();
    const img = $('.avis-cap img');
    $('.avis-cap b').textContent = np ? np.title || '' : '';
    $('.avis-cap span').textContent = np ? np.artist || '' : '';
    const key = np ? (np.cover || '') + '|' + np.title : '';
    if (key === songKey) return;
    songKey = key;
    if (!np || !np.cover) { img.hidden = true; img.removeAttribute('src'); coverScheme = 'spectrum'; if (cfg.scheme === 'auto') useScheme('spectrum'); info(); return; }
    img.hidden = false;
    img.src = np.cover;
    const probe = new Image();
    probe.onload = () => {
      try { coverScheme = VisDesign.schemeForCover(MusicVisualizer.paletteFromImage(probe, 5)); } catch (e) { coverScheme = 'spectrum'; }
      if (cfg.scheme === 'auto') useScheme(coverScheme);
      info();
    };
    probe.src = np.cover;
  }

  // ---- render loop ----------------------------------------------------------------------------------------------
  function size() {
    if (!canvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2) * cfg.quality * (perf.low > 0 ? 0.75 : 1);
    const w = Math.max(1, Math.round(canvas.clientWidth * dpr)), h = Math.max(1, Math.round(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; if (vis) vis.setRendererSize(w, h); }
  }
  function frame(t) {
    raf = requestAnimationFrame(frame);
    const dt = lastT ? Math.min(0.1, (t - lastT) / 1000) : 0.016;
    lastT = t;
    if (feat && conductor) conductor.update(t / 1000, feat(t / 1000, dt));
    try { vis.render(); } catch (e) { console.warn('visualizer render', e); }
    // a slow machine: after 3 s under 40 fps, render at three quarters of the size once
    perf.acc += dt; perf.n++;
    if (perf.acc >= 3) { if (perf.n / perf.acc < 40 && !perf.low) { perf.low = 1; size(); } perf.acc = 0; perf.n = 0; }
  }

  // ---- open / close ---------------------------------------------------------------------------------------------
  function openView(h) {
    host = h;
    if (!root) build();
    const ctx = host.ctx;
    if (!vis || vis.__ctx !== ctx) {
      const BC = (window.butterchurn && window.butterchurn.default) || window.butterchurn;
      curated = Object.keys(window.VisCurated || {});
      Object.assign(presets, window.VisCurated || {});
      root.hidden = false;      // the canvas needs its size before the renderer is made
      size();
      vis = BC.createVisualizer(ctx, canvas, { width: canvas.width, height: canvas.height, pixelRatio: 1, textureRatio: 1 });
      vis.__ctx = ctx;
      delay = ctx.createDelay(5);
      delay.delayTime.value = cfg.delay / 1000;
      vis.connectAudio(delay);
      const an = ctx.createAnalyser();
      delay.connect(an);
      feat = MusicVisualizer.audioFeatures(an);
      VisDesign.use(cfg.scheme === 'auto' ? 'spectrum' : cfg.scheme);
      buildOwn();
      conductor = VisConductor.create({ scenes: sceneList(), mode: cfg.changes, phrases: cfg.phrases, load });
    }
    try { host.input.connect(delay); } catch (e) { /* already */ }
    root.hidden = false;
    open = true;
    lastT = 0;
    songKey = '';
    songChanged();
    if (!conductor.active) conductor.start(0);
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(frame);
    document.addEventListener('keydown', onKey, true);
    wake();
    root.requestFullscreen().catch(() => { /* not allowed now: the window view is fine, F asks again */ });
  }
  function close() {
    if (!open) return;
    open = false;
    cancelAnimationFrame(raf);
    raf = 0;
    try { host.input.disconnect(delay); } catch (e) { /* not connected */ }
    document.removeEventListener('keydown', onKey, true);
    if (document.fullscreenElement === root) document.exitFullscreen().catch(() => {});
    root.hidden = true;
    togglePanel(false);
    if (host && host.onClose) host.onClose();
  }
  function onKey(e) {
    if (!open) return;
    const t = e.target;
    if (t && (t.tagName === 'SELECT' || (t.tagName === 'INPUT' && t.type !== 'range'))) return;
    const k = e.key.toLowerCase();
    if (e.key === 'Escape') {
      e.stopPropagation();
      if (!root.querySelector('.avis-panel').hidden) togglePanel(false);
      else close();
    } else if ((k === 'a' || k === 'n') && !e.ctrlKey && !e.metaKey && !e.altKey) { e.stopPropagation(); e.preventDefault(); nextScene(); wake(); }
    else if (k === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) { e.stopPropagation(); e.preventDefault(); toggleFullscreen(); }
    // everything else (Space, J/K/L, media keys) goes on to the page: playback keeps working
  }
  document.addEventListener('visibilitychange', () => {
    if (!open) return;
    if (document.hidden) { cancelAnimationFrame(raf); raf = 0; } else if (!raf) { lastT = 0; raf = requestAnimationFrame(frame); }
  });

  window.AtriVisualizer = {
    open: openView, close, songChanged,
    get isOpen() { return open; },
    get scene() { return current; },
  };
})();
