/* Shared previews use the site's WebXR stereo renderer and only share-scoped cached media. */
(() => {
  const btn = document.getElementById('mediaVrBtn');
  const message = document.getElementById('shareVrStatus');
  const token = location.pathname.split('/').filter(Boolean).pop();
  const base = `/api/public/share/${encodeURIComponent(token)}`;
  const moduleUrl = '/api/public/assets/js/vr/app.js';
  let item = null, status = { state: 'none' }, generation = 0;
  let prepared = false, statusKnown = false, starting = false, handle = null;
  const note = (text) => { message.textContent = text || ''; };
  function paint() {
    btn.hidden = !item || !navigator.xr;
    btn.disabled = starting || !prepared || !statusKnown;
    btn.textContent = handle ? 'Exit VR' : 'VR';
    btn.classList.toggle('vr-active', !!handle);
    btn.setAttribute('aria-disabled', btn.disabled ? 'true' : 'false');
    btn.title = handle ? 'Exit VR' : 'View in VR';
  }
  function end() {
    generation++;
    item = null; prepared = false; statusKnown = false; starting = false;
    if (handle) { const old = handle; handle = null; old.end(); }
    note(''); paint();
  }
  // A single 3D video may have been re-encoded (H.264), so ask the file itself: a track the browser cannot decode
  // is dropped, which leaves a width of 0.
  function hasPicture(url) {
    return new Promise((resolve) => {
      const v = document.createElement('video');
      const done = (ok) => { clearTimeout(timer); v.removeAttribute('src'); v.load(); resolve(ok); };
      const timer = setTimeout(() => done(false), 5000);
      v.preload = 'metadata';
      v.muted = true;
      v.addEventListener('loadedmetadata', () => done(v.videoWidth > 0), { once: true });
      v.addEventListener('error', () => done(false), { once: true });
      v.src = url;
    });
  }
  // Most 3D videos are HEVC, the 2D preview plays the original, so a working preview says nothing about them. A
  // browser that cannot decode HEVC would play the sound over a black screen: use the original in VR instead.
  async function playable(st) {
    if (!st || st.state !== 'done' || st.kind !== 'video') return st;
    try {
      const info = await navigator.mediaCapabilities.decodingInfo({ type: 'file', video: {
        contentType: 'video/mp4; codecs="hev1.1.6.L153.B0"', width: 3840, height: 2160, bitrate: 40000000, framerate: 60 } });
      if (info.supported || await hasPicture(st.sbs_url)) return st;
    } catch (e) {
      return st;
    }
    note('This browser cannot play the 3D version (HEVC), so VR shows this video in 2D.');
    return { state: 'none' };
  }
  function preview(file) {
    end();
    if (!['image', 'video'].includes(file.type)) return;
    item = file; status = { state: 'none' };
    const seq = generation;
    paint();
    // Resolve the rendition before enabling the button so requestSession stays in the click gesture.
    Promise.all([
      Stereo3d.loadVrApp(moduleUrl),
      fetch(`${base}/3d/status?${new URLSearchParams({ root: file.root || '', path: file.path })}`)
        .then(r => r.ok ? r.json() : { state: 'none' }).then(playable).catch(() => ({ state: 'none' })),
    ]).then(([, st]) => {
      if (seq !== generation || item !== file) return;
      prepared = true; statusKnown = true; status = st || { state: 'none' }; paint();
    }).catch(() => {
      if (seq !== generation) return;
      note('Could not load the VR viewer. Reload the page and try again.');
    });
  }
  btn.addEventListener('click', () => {
    if (handle) { handle.end(); return; }
    if (!item || starting) return;
    if (!prepared || !statusKnown) return;
    const file = item, seq = generation;
    const video = document.querySelector('#mediaPreviewBody video');
    const resume = video && !video.paused;
    if (video) video.pause();
    const context = { view: 'item', item: { root: file.root || '', path: file.path,
      name: file.name, kind: file.type === 'image' ? 'picture' : 'video', url: file.url, status } };
    starting = true; paint(); note('');
    // Same immersive-vr session and per-eye SBS renderer as the normal preview.
    Stereo3d.startVr(context, { moduleUrl, bare: true, autoConvert: false,
      listUrl: `${base}/3d/list`, storage: null, onStatus: text => { if (seq === generation) note(text); } }).then(session => {
      if (seq !== generation || item !== file) { session.end(); return; }
      handle = session; starting = false; paint();
      session.ended.then(() => {
        if (handle !== session) return;
        handle = null; paint();
        if (resume && video.isConnected && item === file) startPlayback(video);
      });
    }).catch(error => {
      if (seq !== generation) return;
      starting = false; paint(); note(error.message || 'Could not start VR.');
      if (resume && video.isConnected && item === file) startPlayback(video);
    });
  });
  window.ShareVr = { preview, end };
  paint();
})();
