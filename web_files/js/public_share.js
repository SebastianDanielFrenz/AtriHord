// Standalone public share viewer - no login, no cookies needed. Everything
// here goes through /api/public/share/<token>/* which is gated only by the
// token in the URL itself (see server.py: find_share / path_allowed_by_share).
const token = window.location.pathname.split('/').filter(Boolean).pop();

let shareInfo = null;
let currentPath = '';
let currentRootPath = '';
let currentRootLabel = '';
let activePreviewList = [];
let currentPreviewIndex = -1;
let currentView = 'pool';
// Only set once /api/me confirms an authenticated admin session (e.g. an
// admin who's logged into the dashboard in this same browser, viewing
// their own share link) - lets them remove pool items right from this
// page instead of only via the Shares card in the dashboard.
let isAdminViewer = false;


function mediaTypeIcon(type) {
  return { image: '\u{1F5BC}️', video: '\u{1F3AC}', audio: '\u{1F3B5}' }[type] || '\u{1F4C4}';
}

function showError(msg) {
  const el = document.getElementById('shareError');
  el.textContent = msg;
  el.style.display = 'block';
  document.getElementById('shareBrowserCard').style.display = 'none';
}

function addRemoveButton(tile, path) {
  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'tile-remove-btn';
  removeBtn.textContent = '×';
  removeBtn.title = 'Remove from pool';
  removeBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    await removePoolItem(path);
  });
  tile.appendChild(removeBtn);
}

function buildFolderTile(name, onClick, removablePath) {
  const tile = document.createElement('div');
  tile.className = 'media-tile media-folder';
  tile.innerHTML = '<div class="media-tile-icon">\u{1F4C1}</div><div class="media-tile-name"></div>';
  tile.querySelector('.media-tile-name').textContent = name;
  tile.addEventListener('click', onClick);
  if (removablePath) addRemoveButton(tile, removablePath);
  return tile;
}

function buildFileTile(f, removable) {
  const tile = document.createElement('div');
  tile.className = 'media-tile media-file';
  setTileAspect(tile, f.width, f.height);     // js/gallery.js

  if (f.type === 'image') {
    const icon = document.createElement('div');
    icon.className = 'media-tile-icon';
    icon.textContent = mediaTypeIcon(f.type);

    const thumbWrap = document.createElement('div');
    thumbWrap.className = 'media-tile-thumb';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = f.name;
    img.addEventListener('error', () => {
      thumbWrap.replaceWith(icon);
      tile.classList.add('no-thumb');
    }, { once: true });
    img.addEventListener('load', () => fitTileToThumb(tile, img), { once: true });
    // The downscaled rendition, not f.url: a tile is ~140px, and pulling
    // the originals meant megabytes per tile and a full-resolution decode
    // each - which is what made a pool of 30 images crawl.
    img.src = f.thumb_url;
    thumbWrap.appendChild(img);
    tile.appendChild(thumbWrap);
  } else if (f.type === 'audio' || f.type === 'video') {
    const icon = document.createElement('div');
    icon.className = 'media-tile-icon';
    icon.textContent = mediaTypeIcon(f.type);

    const thumbWrap = document.createElement('div');
    thumbWrap.className = 'media-tile-thumb';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = f.name;
    img.addEventListener('error', () => {
      thumbWrap.replaceWith(icon);
      tile.classList.add('no-thumb');
    }, { once: true });
    img.addEventListener('load', () => fitTileToThumb(tile, img), { once: true });
    img.src = f.thumb_url;
    thumbWrap.appendChild(img);
    tile.appendChild(thumbWrap);
  } else {
    const icon = document.createElement('div');
    icon.className = 'media-tile-icon';
    icon.textContent = mediaTypeIcon(f.type);
    tile.appendChild(icon);
    tile.classList.add('no-thumb');
  }

  const badge = tileBadge(f);
  if (badge) tile.appendChild(badge);

  const nameEl = document.createElement('div');
  nameEl.className = 'media-tile-name';
  nameEl.textContent = f.name;
  tile.appendChild(nameEl);

  const sizeEl = document.createElement('div');
  sizeEl.className = 'media-tile-size';
  sizeEl.textContent = formatFileSize(f.size);
  tile.appendChild(sizeEl);

  // With names hidden this is where the name is: the tooltip.
  tile.title = `${f.name}\n${formatFileSize(f.size)}`;
  tile.addEventListener('click', () => openPreview(f, activePreviewList));
  if (removable) addRemoveButton(tile, f.path);
  return tile;
}

// `removable` only applies to items at the top level of a pool share (its
// own registered items) - not to plain subfolder contents while browsing
// inside one, since those aren't individually removable pool entries.
function renderGrid(dirNames, files, folderItems, removable) {
  const grid = document.getElementById('shareGrid');
  const empty = document.getElementById('shareEmpty');
  grid.innerHTML = '';
  activePreviewList = files || [];

  const allEmpty = !(dirNames && dirNames.length) && !(files && files.length) && !(folderItems && folderItems.length);
  empty.style.display = allEmpty ? 'block' : 'none';

  for (const d of dirNames || []) {
    const path = currentPath ? `${currentPath}/${d}` : d;
    grid.appendChild(buildFolderTile(d, () => browseTo(path)));
  }
  for (const item of folderItems || []) {
    grid.appendChild(buildFolderTile(item.name, () => enterFolderRoot(item.path, item.name), removable ? item.path : null));
  }
  // Folders on their own line(s) above the files, as in the library.
  if (grid.children.length && files && files.length) grid.appendChild(galleryBreak());
  for (const f of files || []) {
    grid.appendChild(buildFileTile(f, removable));
  }
  scheduleGalleryLayout(grid, true);
}

function renderBreadcrumb(path) {
  const el = document.getElementById('shareBreadcrumb');
  el.style.display = '';
  el.innerHTML = '';

  if (shareInfo.kind === 'pool') {
    const poolCrumb = document.createElement('span');
    poolCrumb.className = 'crumb';
    poolCrumb.textContent = shareInfo.label;
    // Wrapped, not passed directly: the handler would receive the click
    // Event as its options argument, and destructuring `push` off that
    // yields undefined - silently turning the push into a replace.
    poolCrumb.addEventListener('click', () => showPoolRoot());
    el.appendChild(poolCrumb);
    const sep = document.createElement('span');
    sep.className = 'crumb-sep';
    sep.textContent = ' / ';
    el.appendChild(sep);
  }

  const rootCrumb = document.createElement('span');
  rootCrumb.className = 'crumb';
  rootCrumb.textContent = currentRootLabel;
  rootCrumb.addEventListener('click', () => browseTo(currentRootPath));
  el.appendChild(rootCrumb);

  const rest = path === currentRootPath ? '' : path.slice(currentRootPath.length + 1);
  let accum = currentRootPath;
  if (rest) {
    for (const part of rest.split('/')) {
      accum = `${accum}/${part}`;
      const sep = document.createElement('span');
      sep.className = 'crumb-sep';
      sep.textContent = ' / ';
      el.appendChild(sep);

      const crumb = document.createElement('span');
      crumb.className = 'crumb';
      crumb.textContent = part;
      const target = accum;
      crumb.addEventListener('click', () => browseTo(target));
      el.appendChild(crumb);
    }
  }
}

// -------------------------------------------------------------------
// History / Back-button handling. Without this, opening a preview or
// entering a folder pushes nothing, so the first Back press leaves the
// site entirely instead of just backing out of the preview/folder.
//
// The token stays the whole address; where you are *inside* the share is
// carried in the query string, so a folder or a single item deep in a
// pool can be linked to directly and survives a reload. Only paths the
// token already permits are reachable - the server re-checks every one
// (path_allowed_by_share), so an edited URL grants nothing.
// -------------------------------------------------------------------

function shareUrlFor(extra = {}) {
  const params = new URLSearchParams();
  // The bare link already means "the folder this share is of", so its own
  // top level doesn't get spelled out again.
  const atFolderShareTop = shareInfo && shareInfo.kind === 'folder' && currentPath === shareInfo.path;
  if (currentView === 'browse' && currentPath && !atFolderShareTop) {
    params.set('path', currentPath);
  }
  if (extra.previewFile) params.set('file', extra.previewFile);
  const qs = params.toString();
  return `/share/${token}${qs ? '?' + qs : ''}`;
}

function shareHistoryState(extra = {}) {
  return {
    share: true,
    view: currentView,
    path: currentPath,
    rootPath: currentRootPath,
    rootLabel: currentRootLabel,
    previewFile: null,
    ...extra
  };
}

function pushShareState(extra = {}) {
  history.pushState(shareHistoryState(extra), '', shareUrlFor(extra));
}

function replaceShareState(extra = {}) {
  history.replaceState(shareHistoryState(extra), '', shareUrlFor(extra));
}

async function browseTo(path, { push = true } = {}) {
  currentPath = path;
  currentView = 'browse';
  let res;
  try {
    res = await fetch(`/api/public/share/${token}/browse?path=${encodeURIComponent(path)}`);
  } catch (e) {
    showError('This folder could not be reached.');
    return;
  }
  if (!res.ok) {
    showError('This folder could not be loaded.');
    return;
  }
  const data = await res.json();
  renderBreadcrumb(data.path);
  renderGrid(data.dirs, data.files, []);
  if (push) pushShareState(); else replaceShareState();
}

function enterFolderRoot(rootPath, label, opts = {}) {
  currentRootPath = rootPath;
  currentRootLabel = label;
  return browseTo(rootPath, opts);
}

// Which of a pool's own folder items a deep-linked path lives under -
// needed to rebuild the breadcrumb back to the pool root after a reload.
function poolRootFor(path) {
  return (shareInfo.items || []).find(
    (i) => i.kind === 'folder' && (path === i.path || path.startsWith(i.path + '/'))
  ) || null;
}

// Restores the view a ?path=/?file= link points at, on top of whichever
// base view init() has already rendered.
async function restoreFromUrl(path, file) {
  if (path && shareInfo.kind !== 'file') {
    if (shareInfo.kind === 'pool') {
      const root = poolRootFor(path);
      // A path belonging to no pooled folder is not part of this share;
      // ignore it and stay on the pool root rather than showing an error.
      if (root) await enterFolderRoot(root.path, root.name, { push: false });
      if (root && path !== root.path) await browseTo(path, { push: false });
    } else {
      await browseTo(path, { push: false });
    }
  }

  if (file) {
    const match = activePreviewList.find((x) => x.path === file);
    if (match) openPreview(match, activePreviewList, { push: false });
  }
}

function showPoolRoot({ push = true } = {}) {
  currentView = 'pool';
  currentPath = '';
  document.getElementById('shareBreadcrumb').style.display = 'none';
  const folderItems = shareInfo.items.filter(i => i.kind === 'folder');
  const fileItems = shareInfo.items.filter(i => i.kind !== 'folder');
  renderGrid([], fileItems, folderItems, isAdminViewer);
  if (push) pushShareState(); else replaceShareState();
}

async function removePoolItem(path) {
  await fetch(`/api/share/${shareInfo.id}/items`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path })
  });

  const res = await fetch(`/api/public/share/${token}`);
  if (!res.ok) return;
  shareInfo = await res.json();
  // Re-render in place - you're already viewing the pool root, so this
  // isn't a navigation and shouldn't add a history entry.
  showPoolRoot({ push: false });
}

// applyMediaSizing(), wrapWithCustomControls(), and enableImageZoomPan()
// come from player.js - same preview experience as the logged-in Media
// Library, just fed from the public /api/public/share/* endpoints instead.

// The share's own info endpoint. It takes the file's path because that is
// how the server identifies it, and returns metadata with the location
// stripped - whoever holds the link gets to see what the file IS, not where
// it is kept. See public_share_info() in server.py.
infoRequestUrl = (f) =>
  `/api/public/share/${token}/info?path=${encodeURIComponent(f.path)}`
  + `&root=${encodeURIComponent(f.root || '')}`;

// Live upscaling (js/upscale.js), through the share's own routes: the server
// answers only for files this share covers, and only while Settings allow it
// on shared links (public_share_upscale in server.py).
upscaleRoutes.config = `/api/public/share/${token}/upscale/config`;
upscaleRoutes.ask = `/api/public/share/${token}/upscale`;
upscaleRoutes.quietWhenOff = true;
upscaleRoutes.setModel = null;           // a visitor does not re-decide the library's models
loadUpscaleConfig();
interpRoutes.config = `/api/public/share/${token}/interp/config`;
interpRoutes.seg = `/api/public/share/${token}/interp/seg`;
interpRoutes.cacheOnly = true;
// What the preview shows now, for upscale.js.
let currentPreviewKey = '';

function openPreview(f, previewList = activePreviewList, { push = true } = {}) {
  ShareVr.end();
  activePreviewList = previewList;
  currentPreviewIndex = previewList.findIndex(x => x.path === f.path);

  // A preview gets its own history entry so Back closes it (rather than
  // walking off the page); stepping between items replaces that entry so
  // arrow-key browsing doesn't pile up dozens of them.
  if (push) {
    pushShareState({ previewFile: f.path });
  } else {
    replaceShareState({ previewFile: f.path });
  }

  const modal = document.getElementById('mediaPreviewModal');
  const body = document.getElementById('mediaPreviewBody');
  document.getElementById('mediaPreviewTitle').textContent = f.name;
  followPreviewWithInfoPanel();      // player.js, as in the Media Library
  clearPreviewBody();
  currentPreviewEl = null;
  currentPreviewKey = `${f.root || ''} ${f.path}`;
  resetLiveUpscale();
  setPreviewBackground('');
  modal.classList.remove('fullscreen-media');

  if (f.type === 'image') {
    const img = document.createElement('img');
    img.className = 'media-preview-media';
    img.alt = f.name;
    img.style.opacity = '0';
    img.addEventListener('load', () => {
      // The upscaled swap fires load again: the frame keeps the size it has.
      if (img.dataset.upscaled) return;
      applyMediaSizing(img, img.naturalWidth, img.naturalHeight);
      img.style.opacity = '1';
      startLiveUpscale(img, f);
    });
    img.src = f.url;
    enableImageZoomPan(img);
    body.appendChild(img);
  } else if (f.type === 'video') {
    const video = document.createElement('video');
    video.className = 'media-preview-media';
    video.autoplay = true;
    video.loop = true;
    video.style.opacity = '0';
    video.addEventListener('loadedmetadata', () => {
      applyMediaSizing(video, video.videoWidth, video.videoHeight);
      video.style.opacity = '1';
    });
    playPreviewVideo(video, f);
    body.appendChild(wrapWithCustomControls(video, { extras: [playerSettingsButton(video)] }));
  } else if (f.type === 'audio') {
    // Same video-with-poster trick as the Media Library: a track with a
    // cover plays through <video> so it gets the same dark controls bar
    // and stays sized/anchored like a real video instead of the plain
    // native <audio> bar.
    const thumbUrl = f.thumb_url;
    // Started inside the opening gesture - see the same branch in media.js
    // and startPlayback() in player.js for why the cover probe cannot be
    // allowed to own the <audio> element.
    const audio = document.createElement('audio');
    audio.src = f.url;
    audio.autoplay = true;
    audio.loop = true;
    startPlayback(audio);

    const probe = new Image();
    probe.addEventListener('load', () => {
      const cover = document.createElement('img');
      cover.className = 'media-preview-media';
      cover.alt = '';
      cover.src = thumbUrl;
      applyAmbientBackgroundFrom(probe);

      applyMediaSizing(cover, probe.naturalWidth, probe.naturalHeight);
      body.appendChild(wrapWithCustomControls(audio, { coverEl: cover }));
      // Upscaled like a picture, once it has its size on screen (see media.js).
      if (cover.complete && cover.naturalWidth) startLiveUpscale(cover, f);
      else cover.addEventListener('load', () => startLiveUpscale(cover, f), { once: true });
    }, { once: true });
    probe.addEventListener('error', () => {
      body.appendChild(wrapWithCustomControls(audio, { audioOnly: true }));
    }, { once: true });
    probe.src = thumbUrl;
  } else {
    modal.classList.remove('fullscreen-media');
    const fallback = document.createElement('div');
    fallback.className = 'media-preview-fallback';
    fallback.innerHTML = 'No inline preview available for this file type.<br>';
    const link = document.createElement('a');
    link.href = f.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Open / Download';
    fallback.appendChild(link);
    body.appendChild(fallback);
  }

  ShareVr.preview(f);
  modal.style.display = 'flex';
  setPageScrollLocked(true);
}

function closeMediaPreview(fromPopstate = false) {
  ShareVr.end();
  const modal = document.getElementById('mediaPreviewModal');
  const wasOpen = modal.style.display === 'flex';
  // Closing while the media is in real browser fullscreen (e.g. via the X
  // or a Back press) would otherwise leave the page stuck in fullscreen
  // with the preview already gone.
  if (wasOpen && document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
  modal.style.display = 'none';
  modal.classList.remove('fullscreen-media');
  clearPreviewBody();
  setPreviewBackground('');
  currentPreviewIndex = -1;
  currentPreviewEl = null;
  currentPreviewKey = '';
  hideInfoPanel();
  setPageScrollLocked(false);

  // Closing by X / backdrop click / Escape should consume the preview's
  // own history entry, so a later Back press doesn't have to step through
  // a stale "preview open" state first.
  if (wasOpen && !fromPopstate && history.state && history.state.previewFile) {
    history.back();
  }
}

function navigatePreview(delta) {
  if (currentPreviewIndex === -1) return;
  const nextIndex = currentPreviewIndex + delta;
  if (nextIndex < 0 || nextIndex >= activePreviewList.length) return;
  openPreview(activePreviewList[nextIndex], activePreviewList, { push: false });
}

window.addEventListener('popstate', async (e) => {
  const state = e.state;

  // No share state at all means this entry predates the viewer (i.e. the
  // user really is navigating away) - let the browser handle it.
  if (!state || !state.share) return;

  closeMediaPreview(true);

  if (state.view === 'pool') {
    showPoolRoot({ push: false });
  } else if (state.view === 'browse') {
    currentRootPath = state.rootPath || '';
    currentRootLabel = state.rootLabel || '';
    // Awaited: browseTo fetches, and only then populates
    // activePreviewList - which the preview restore below needs.
    await browseTo(state.path || currentRootPath, { push: false });
  }

  if (state.previewFile) {
    const match = activePreviewList.find(x => x.path === state.previewFile);
    if (match) openPreview(match, activePreviewList, { push: false });
  }
});

document.getElementById('mediaPreviewModal').addEventListener('click', (e) => {
  if (e.target.id === 'mediaPreviewModal') closeMediaPreview();
});
document.addEventListener('keydown', (e) => {
  const modal = document.getElementById('mediaPreviewModal');
  if (modal.style.display !== 'flex') return;

  if (e.key === 'Escape') {
    // A fullscreen <video> already owns Escape (browser exits fullscreen
    // on its own) - don't also close the modal underneath it.
    if (document.fullscreenElement) return;
    closeMediaPreview();
  } else if (e.key === 'ArrowLeft') {
    navigatePreview(-1);
  } else if (e.key === 'ArrowRight') {
    navigatePreview(1);
  }
});

/* A live share - a character or a person with fixed filters - can match a
   thousand files, so it arrives in pages from /items and is appended as it is
   scrolled to. The next page also loads while the page is not tall enough to
   scroll at all: a scroll event that never happens cannot ask for it. */
let queryOffset = 0;
let queryHasMore = false;
let queryLoading = false;

async function loadQueryPage() {
  if (queryLoading || !queryHasMore) return;
  queryLoading = true;
  try {
    const res = await fetch(`/api/public/share/${token}/items?offset=${queryOffset}&limit=60`);
    if (!res.ok) { queryHasMore = false; return; }
    const data = await res.json();
    const items = data.items || [];
    const grid = document.getElementById('shareGrid');
    for (const f of items) {
      activePreviewList.push(f);
      grid.appendChild(buildFileTile(f, false));
    }
    scheduleGalleryLayout(grid);
    queryOffset += items.length;
    queryHasMore = !!data.has_more && items.length > 0;
    document.getElementById('shareEmpty').style.display = activePreviewList.length ? 'none' : 'block';
  } catch (e) {
    queryHasMore = false;
  } finally {
    queryLoading = false;
  }
  setTimeout(fillQueryPage, 0);
}

function fillQueryPage() {
  if (!queryHasMore || queryLoading) return;
  const doc = document.documentElement;
  if (window.scrollY + window.innerHeight >= doc.scrollHeight - 400) loadQueryPage();
}

async function showQueryShare() {
  currentView = 'query';
  document.getElementById('shareBreadcrumb').style.display = 'none';
  renderGrid([], [], []);
  const n = Number(shareInfo.total || 0);
  document.getElementById('shareTitle').textContent =
    `${shareInfo.label} · ${n.toLocaleString('en-GB')} file${n === 1 ? '' : 's'}`;
  queryOffset = 0;
  queryHasMore = true;
  replaceShareState();
  await loadQueryPage();
}

window.addEventListener('scroll', fillQueryPage, { passive: true });
window.addEventListener('resize', fillQueryPage);

async function init() {
  if (!token) {
    showError('No share link given.');
    return;
  }

  // Read before anything renders: setting up the base view replaces the
  // history entry, which rewrites the URL and would erase these first.
  const params = new URLSearchParams(window.location.search);
  const linkedPath = params.get('path') || '';
  const linkedFile = params.get('file') || '';

  let res;
  try {
    res = await fetch(`/api/public/share/${token}`);
  } catch (e) {
    showError('This link could not be reached.');
    return;
  }
  if (!res.ok) {
    showError(res.status === 410
      ? 'This share link has expired.'
      : 'This share link is invalid or has been revoked.');
    return;
  }

  shareInfo = await res.json();
  document.getElementById('shareTitle').textContent = shareInfo.label;
  document.getElementById('shareBrowserCard').style.display = 'block';

  try {
    const meRes = await fetch('/api/me');
    if (meRes.ok) {
      const me = await meRes.json();
      isAdminViewer = me.role === 'admin';
    }
  } catch (e) {
    // not logged in / can't reach it - stays a normal anonymous viewer
  }

  const coverEl = document.getElementById('shareCover');
  coverEl.addEventListener('error', () => { coverEl.style.display = 'none'; }, { once: true });
  coverEl.addEventListener('load', () => { coverEl.style.display = ''; }, { once: true });
  coverEl.src = shareInfo.cover_url;

  // push: false throughout - the initial view replaces the entry the
  // browser already created for this page load rather than stacking a
  // second one on top of it (which would need two Backs to leave).
  if (shareInfo.kind === 'file') {
    currentView = 'file';
    renderGrid([], [shareInfo.file], []);
    replaceShareState();
  } else if (shareInfo.kind === 'folder') {
    currentRootLabel = shareInfo.label;
    await enterFolderRoot(shareInfo.path, shareInfo.label, { push: false });
  } else if (shareInfo.kind === 'pool') {
    showPoolRoot({ push: false });
  } else if (shareInfo.kind === 'query') {
    await showQueryShare();
  }

  // A link into a folder or item within the share reopens exactly that,
  // without adding an entry - Back from here still leaves the page, the
  // same as landing on the bare link would.
  await restoreFromUrl(linkedPath, linkedFile);
}

init();
