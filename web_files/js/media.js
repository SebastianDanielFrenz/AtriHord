const DOWNLOADS_ROOT_LABEL = 'Downloads';
let currentMediaPath = '';
// Which media root is being browsed ('' = the root listing itself, shown
// only when more than one root is configured).
let currentMediaRoot = '';
let currentRootName = '';
let isMultiRoot = false;
let currentFilesList = [];
let activePreviewList = [];
let currentPreviewIndex = -1;
// currentPreviewEl/W/H, applyMediaSizing(), wrapWithCustomControls(), and
// enableImageZoomPan() all live in player.js (shared with public_share.js).

function mediaUrlFor(path, root = currentMediaRoot, query = '', view = '', extra = {}) {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  if (root) params.set('root', root);
  // A browse view is a place, so it is in the URL and the link reopens it.
  if (view) params.set('view', view);
  // The folder stays in the URL alongside the query: leaving the search
  // returns to where it was started from, and the link reopens both.
  if (query) params.set('q', query);
  // A tag search is not reproducible from its query text - the text is only
  // the readable label - so the tags themselves ride in the URL. Without them
  // Back out of a costume landed on a plain word search for the character's
  // name, which returns something quite different.
  for (const [k, v] of Object.entries(extra)) if (v) params.set(k, v);
  const qs = params.toString();
  return qs ? `/media.html?${qs}` : '/media.html';
}

// Keeps the folder browser's own root+path (whatever they currently are,
// regardless of whether this file actually came from that folder - e.g.
// opened from the recent strip or search results) and adds the file, so
// the URL bar always has a link that reopens this exact item once shared.
function previewUrlFor(f) {
  const params = new URLSearchParams();
  if (currentMediaPath) params.set('path', currentMediaPath);
  if (currentMediaRoot) params.set('root', currentMediaRoot);
  // Opened from search results, the query belongs in the URL too, so
  // closing the preview goes back to those results rather than dumping
  // the viewer in the folder browser.
  if (searchModeActive && searchQuery) params.set('q', searchQuery);
  if (searchModeActive) {
    for (const [k, v] of Object.entries(subjectParams())) if (v) params.set(k, v);
  }
  params.set('file', f.path);
  if (f.root) params.set('fileRoot', f.root);
  return `/media.html?${params.toString()}`;
}

function absoluteFileUrl(f) {
  return window.location.origin + f.url;
}

// Chromium's clipboard only accepts image/png for image writes - handing
// it the file's own image/jpeg blob is rejected outright ("Type
// image/jpeg not supported on write"), which is why most of this library
// couldn't be copied. Anything that isn't already PNG gets re-encoded
// through a canvas. (An animated GIF necessarily becomes its first frame;
// the clipboard has no animated format anyway.)
async function imageAsPngBlob(url) {
  const res = await fetch(url);
  const blob = await res.blob();
  if (blob.type === 'image/png') return blob;

  const bitmap = await createImageBitmap(blob);
  try {
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext('2d').drawImage(bitmap, 0, 0);
    return await new Promise((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob failed'))), 'image/png');
    });
  } finally {
    if (bitmap.close) bitmap.close();
  }
}

// -------------------------------------------------------------------
// Right-click context menu (Copy Link / Download) - grid tiles and the
// open preview both use this, via attachContextMenu().
// -------------------------------------------------------------------

function hideContextMenu() {
  document.getElementById('mediaContextMenu').style.display = 'none';
}

function addContextMenuItem(menu, label, onClick) {
  const item = document.createElement('div');
  item.className = 'context-menu-item';
  item.textContent = label;
  item.addEventListener('click', onClick);
  menu.appendChild(item);
  return item;
}

// download=1 makes the SERVER send Content-Disposition: attachment. The
// `download` attribute is only a request to the browser, and a managed browser
// may honour the response's `inline` instead - which is what silently did
// nothing on a locked-down laptop. Both are set: the header decides, the
// attribute names the file. The menu is closed first, so the dismissal handler
// above has nothing to dismiss when the link's click passes it.
function downloadFile(url, name) {
  const sep = url.includes('?') ? '&' : '?';
  const a = document.createElement('a');
  a.href = url + sep + 'download=1';
  a.download = name;
  hideContextMenu();
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// "photo.jpg" upscaled 3x into a WebP -> "photo.upscaled-3x.webp", 4.78x at a
// limit -> "photo.upscaled-4.78x.webp": the name the server gives the attachment
// too (media_upscaled_file, two decimals at most).
function upscaledFileName(name, upscaled) {
  const dot = name.lastIndexOf('.');
  const scale = String(Number(Number(upscaled.scale || upscaled.factor).toFixed(2)));
  return `${dot > 0 ? name.slice(0, dot) : name}${upscaled.cover ? '.cover' : ''}.upscaled-${scale}x${upscaled.ext || '.webp'}`;
}

// "video.mp4" playing Smooth motion at 119.88 fps -> "video.smooth-119.88fps.mp4":
// the name /api/media/interp/download gives the attachment too.
function smoothMotionFileName(name, fps) {
  const dot = name.lastIndexOf('.');
  return `${dot > 0 ? name.slice(0, dot) : name}.smooth-${formatFps(fps)}fps.mp4`;
}

// Same o (output fps) and mbps (bandwidth cap) the preview is playing with
// (js/interp.js's segUrl), so the download reuses whatever is already cached
// on the vision worker instead of making a different variant.
function smoothMotionDownloadUrl(item, st) {
  const q = new URLSearchParams({ root: item.root || '', path: item.path,
    o: String(st.out), mbps: String(bandwidthCap(interpConfig)) });
  return `/api/media/interp/download?${q}`;
}

// `item` is either a file entry (has url/type/name/path) from the folder
// grid, recent strip, search results, or open preview - or a plain
// {path, name, isFolder: true} for a folder tile, which only gets the
// sharing actions (Copy Link/Download don't make sense for a folder here).
function showContextMenu(x, y, item) {
  const menu = document.getElementById('mediaContextMenu');
  menu.innerHTML = '';

  if (!item.isFolder) {
    if (item.type === 'image') addContextMenuItem(menu, 'Find similar images', () => { hideContextMenu(); ImageSearch.open(item); });
    addContextMenuItem(menu, 'Copy Link', async () => {
      try {
        await navigator.clipboard.writeText(absoluteFileUrl(item));
      } catch (e) {
        // ignore - nothing sensible to show without reintroducing the
        // "leave the menu open with a status message" pattern this replaces
      }
      hideContextMenu();
    });

    // The video playing in the open preview, if any (also used below for
    // Smooth motion download and stats-for-nerds - js/interp.js).
    const previewVideo = typeof toggleVideoStats === 'function' && item.type === 'video'
      && currentPreviewKey === previewKeyFor(item)
      ? document.querySelector('#mediaPreviewBody video') : null;
    // While it plays Smooth motion, both versions - same idea as the upscaled
    // picture below, one flight up: the picture is enhanced by upscale.js, the
    // video by interp.js, and a file is never both.
    const smooth = previewVideo && previewVideo._smooth && previewVideo._smooth.active
      ? previewVideo._smooth : null;
    // While the open preview shows this file's upscaled copy, both versions.
    const upscaled = previewUpscale && previewUpscale.key === previewKeyFor(item) ? previewUpscale : null;
    if (upscaled) {
      addContextMenuItem(menu, upscaled.cover ? 'Download' : 'Download original', () => downloadFile(item.url, item.name));
      addContextMenuItem(menu, `Download upscaled${upscaled.cover ? ' cover' : ''} (${upscaleScaleLabel(upscaled)}×)`,
        () => downloadFile(upscaled.url, upscaledFileName(item.name, upscaled)));
    } else if (smooth) {
      const fps = smooth.madeFps || smooth.out;
      addContextMenuItem(menu, 'Download original', () => downloadFile(item.url, item.name));
      addContextMenuItem(menu, `Download smooth motion (${formatFps(fps)} fps)`,
        () => downloadFile(smoothMotionDownloadUrl(item, smooth), smoothMotionFileName(item.name, fps)));
    } else {
      addContextMenuItem(menu, 'Download', () => downloadFile(item.url, item.name));
    }

    // Our own context menu replaces the browser's native one - which is
    // what normally offers "Copy image" for an <img> - so it has to be
    // re-added here manually or that ability just disappears for images.
    if (item.type === 'image') {
      addContextMenuItem(menu, 'Copy Image', () => {
        // Handing the ClipboardItem a promise (rather than awaiting the
        // fetch first and then calling write) keeps the write call itself
        // synchronous inside the click handler, so it can't be rejected
        // for having lost the user-activation that permits clipboard use.
        try {
          navigator.clipboard
            .write([new ClipboardItem({ 'image/png': imageAsPngBlob(item.url) })])
            .catch(() => {});
        } catch (e) {
          // Older browsers without ClipboardItem/promise support.
        }
        hideContextMenu();
      });
    }

    addContextMenuItem(menu, item.favorite ? 'Remove from favorites' : 'Add to favorites', () => {
      hideContextMenu();
      toggleFavorite(item);
    });

    // 2D -> 3D (js/stereo3d.js): admins only, videos and pictures only.
    if (viewerIsAdmin && Stereo3d.canConvert(item)) addConvert3dMenuItems(menu, item);

    // YouTube-style stats for the video above (js/interp.js).
    if (previewVideo) {
      addContextMenuItem(menu, videoStatsOpen(previewVideo) ? 'Hide stats for nerds' : 'Stats for nerds', () => {
        hideContextMenu();
        toggleVideoStats(previewVideo);
      });
    }
  }

  addContextMenuItem(menu, 'Share', async () => {
    hideContextMenu();
    await shareItemAndCopyLink(item.path, item.root);
  });

  addContextMenuItem(menu, 'Add to Pool…', () => {
    hideContextMenu();
    addToPoolFlow(item.path, item.root);
  });

  menu.style.left = '0px';
  menu.style.top = '0px';
  menu.style.display = 'block';
  const rect = menu.getBoundingClientRect();
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 8)) + 'px';
  menu.style.top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 8)) + 'px';
}

// getFile is a function rather than a plain value so this also works for
// the open preview, where the "current file" changes as the user arrow-key
// navigates without needing to re-attach the listener each time.
function attachContextMenu(el, getFile) {
  el.addEventListener('contextmenu', (e) => {
    const f = getFile();
    if (!f) return;
    e.preventDefault();
    showContextMenu(e.clientX, e.clientY, f);
  });
}

function contextMenuIsOpen() {
  const menu = document.getElementById('mediaContextMenu');
  return !!menu && menu.style.display === 'block';
}

// Dismissing the context menu must do ONLY that.
//
// Both of these run in the CAPTURE phase so they see the event before anything
// else does - in particular before the preview modal's background-click and
// Escape handlers. Those call closeMediaPreview(), which is a navigation: it
// fires history.back(), and the popstate handler then refetches the folder,
// re-runs the search and rebuilds the player. Dismissing a menu looked like
// the page reloading, and a playing track jumped back to 0:00.
//
// Clicks inside the menu are left alone, or the menu items would never fire.
document.addEventListener('click', (e) => {
  const menu = document.getElementById('mediaContextMenu');
  // Only a person's click dismisses. A menu item's own programmatic click is
  // untrusted: Download clicks a temporary <a download> outside the menu, and
  // cancelling THAT click here is what made Download save nothing.
  if (!e.isTrusted || !contextMenuIsOpen() || menu.contains(e.target)) return;
  hideContextMenu();
  e.stopPropagation();
  e.preventDefault();
}, true);

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || !contextMenuIsOpen()) return;
  hideContextMenu();
  e.stopPropagation();
}, true);
window.addEventListener('scroll', hideContextMenu, true);
window.addEventListener('blur', hideContextMenu);

// -------------------------------------------------------------------
// Public sharing (file/folder/pool links, no login required to view) -
// see /api/share* and /api/public/share/* in server.py.
// -------------------------------------------------------------------

async function shareItemAndCopyLink(path, root = currentMediaRoot) {
  let res;
  try {
    res = await fetch('/api/share', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, root })
    });
  } catch (e) {
    return;
  }
  if (!res.ok) return;
  const share = await res.json();
  try {
    await navigator.clipboard.writeText(window.location.origin + share.url);
  } catch (e) {
    // clipboard write failed (e.g. insecure context) - the share still
    // exists and is listed in the Shares card either way.
  }
  loadShares();
}

/* A live link to what is being browsed - a character pool or a person, with
   the filters that are on. The server stores the subject and the filters,
   not a list of files, so new downloads of that character show up in it (see
   create_query_share in server.py). */
function syncShareQueryButton() {
  const btn = document.getElementById('shareQueryBtn');
  if (btn) btn.hidden = !(activeTag || activePerson);
  syncBlacklistButton();
}

/* "Blacklist this character" (admins), beside the Share link button on a
   character pool's page. It opens the same preview and explicit confirmation as
   Settings > Character blacklist: the number of files that go to the trash, the
   variant tags covered, and a Confirm button. The button is made here rather than
   in media.html so the page stays as it is for everybody else. */
function syncBlacklistButton() {
  const share = document.getElementById('shareQueryBtn');
  if (!share || !share.parentNode) return;
  let btn = document.getElementById('blacklistCharacterBtn');
  if (!btn) {
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'blacklistCharacterBtn';
    btn.className = 'btn btn-quiet';
    btn.textContent = 'Blacklist this character';
    btn.title = 'Hide this character and trash the pictures that show only blacklisted characters. Asks first.';
    btn.addEventListener('click', () => { if (activePool) blacklistCharacterDialog(activePool); });
    share.parentNode.insertBefore(btn, share);
  }
  btn.hidden = !(viewerIsAdmin && activePool && activePool.tag);
}

async function blacklistCharacterDialog(pool) {
  const body = { tag: pool.tag, include_variants: true };
  const post = async (url, payload) => {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const err = new Error(data.error || `HTTP ${res.status}`); err.data = data; throw err; }
    return data;
  };
  let pv;
  try { pv = await post('/api/media/characters/blacklist/preview', body); }
  catch (err) { alert(`No preview: ${err.message}`); return; }
  if (pv.already_listed) { alert(`${pool.label} is already on the blacklist.`); return; }
  const dialog = document.createElement('dialog');
  dialog.style.maxWidth = '34rem';
  const title = document.createElement('h3');
  title.textContent = `Blacklist ${pool.label}?`;
  const what = document.createElement('p');
  what.textContent = (pv.count
    ? `${pv.count.toLocaleString('en-GB')} file${pv.count === 1 ? '' : 's'} will be moved to the trash with a ban, for everybody, `
      + 'and restorable from the trash for 30 days. '
    : 'No file matches right now; new ones will be trashed as the tagger finds them. ')
    + (pv.kept_shared
      ? `${pv.kept_shared.toLocaleString('en-GB')} picture${pv.kept_shared === 1 ? ' that also shows' : 's that also show'} a character not on the list ${pv.kept_shared === 1 ? 'is' : 'are'} kept, only left out of this character's pool. `
      : 'Pictures that also show a character not on the list are kept. ')
    + `Covers ${pv.variants.length} tag${pv.variants.length === 1 ? '' : 's'} at confidence ${pv.min_p} or more: ${pv.variants.join(', ')}.`;
  const actions = document.createElement('p');
  const confirmBtn = document.createElement('button');
  confirmBtn.type = 'button';
  confirmBtn.className = 'btn';
  confirmBtn.textContent = pv.count ? `Trash ${pv.count.toLocaleString('en-GB')} files and blacklist` : 'Blacklist';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'btn btn-quiet';
  cancelBtn.textContent = 'Cancel';
  const close = () => { dialog.close(); dialog.remove(); };
  cancelBtn.addEventListener('click', close);
  confirmBtn.addEventListener('click', async () => {
    confirmBtn.disabled = true;
    try {
      await post('/api/media/characters/blacklist', { ...body, confirm: true, expected_count: pv.count });
      close();
      alert(`${pool.label} is blacklisted; its files are being moved to the trash.`);
    } catch (err) {
      close();
      alert(err.data && err.data.code === 'count_grew'
        ? `${err.data.count} files match now, ${pv.count} were previewed. Open the dialog again.`
        : `Not added: ${err.message}`);
    }
  });
  actions.append(confirmBtn, cancelBtn);
  dialog.append(title, what, actions);
  document.body.appendChild(dialog);
  dialog.showModal();
}

async function shareCurrentQuery(btn) {
  const subject = activePool
    ? (activePool.series ? `${activePool.label} · ${activePool.series}` : activePool.label)
    : activeSubjectLabel;
  // A bare "60" in a link's name says nothing; the rates are spelled out.
  const filterBits = FILTER_KEYS.filter((k) => mediaFilters[k] && !VIEWER_FILTER_KEYS.includes(k)).map((k) => (
    k === 'min_fps' ? `≥${mediaFilters[k]} fps`
      : k === 'max_fps' ? `≤${mediaFilters[k]} fps`
        : k === 'content' ? contentLabel(mediaFilters[k])
          : mediaFilters[k]));
  // Words narrowing the subject go with the link: without them it would show
  // more than the page it was shared from.
  const words = (searchQuery || '').replace(/\*/g, ' ').trim() ? searchQuery.trim() : '';
  const body = {
    label: subject + (words ? ` · "${words}"` : '') + (filterBits.length ? ` (${filterBits.join(', ')})` : ''),
    filters: Object.fromEntries(Object.entries(mediaFilters).filter(([k]) => !VIEWER_FILTER_KEYS.includes(k))),
    text: words,
    mode: searchMode,
  };
  if (activePerson) {
    body.type = 'person';
    body.person = activePerson;
  } else {
    body.type = 'character';
    body.tags = String(activeTag).split(',').filter(Boolean);
  }
  const was = btn.textContent;
  btn.disabled = true;
  try {
    const res = await fetch('/api/share/query', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(out.error || 'The link could not be created.');
      return;
    }
    const url = window.location.origin + out.url;
    let copied = false;
    try { await navigator.clipboard.writeText(url); copied = true; } catch (e) { /* insecure context */ }
    if (copied) {
      btn.textContent = `Link copied · ${Number(out.total || 0).toLocaleString('en-GB')} files`;
      setTimeout(() => { btn.textContent = was; }, 3000);
    } else {
      window.prompt('Share link', url);
    }
    loadShares();
  } finally {
    btn.disabled = false;
  }
}

// -------------------------------------------------------------------
// "Add to Pool" overlay - a scrollable, thumbnailed pool picker (replaces
// the earlier prompt()-based flow) shown over the media content.
// -------------------------------------------------------------------

let poolPickerTargetPath = null;
let poolPickerTargetRoot = '';

async function addToPoolFlow(path, root = currentMediaRoot) {
  poolPickerTargetPath = path;
  poolPickerTargetRoot = root || currentMediaRoot;
  const modal = document.getElementById('poolPickerModal');
  const listEl = document.getElementById('poolPickerList');
  listEl.innerHTML = '<div class="media-loading">Loading...</div>';
  modal.style.display = 'flex';

  let res;
  try {
    res = await fetch('/api/share');
  } catch (e) {
    listEl.innerHTML = '<div class="media-error">Failed to load pools.</div>';
    return;
  }
  if (!res.ok) {
    listEl.innerHTML = '<div class="media-error">Failed to load pools.</div>';
    return;
  }
  const shares = await res.json();
  renderPoolPickerList(shares.filter(s => s.kind === 'pool' && !s.revoked));
}

function renderPoolPickerList(pools) {
  const listEl = document.getElementById('poolPickerList');
  listEl.innerHTML = '';

  if (pools.length === 0) {
    listEl.innerHTML = '<div class="media-empty">No pools yet - create one above.</div>';
    return;
  }

  for (const p of pools) {
    const row = document.createElement('div');
    row.className = 'pool-picker-row';

    const thumb = document.createElement('div');
    thumb.className = 'pool-picker-thumb';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = '';
    img.addEventListener('error', () => {
      thumb.classList.add('pool-picker-thumb-fallback');
      thumb.textContent = '\u{1F4C1}';
    }, { once: true });
    img.src = p.cover_url;
    thumb.appendChild(img);
    row.appendChild(thumb);

    const info = document.createElement('div');
    info.className = 'pool-picker-info';
    const name = document.createElement('div');
    name.className = 'pool-picker-name';
    name.textContent = p.label;
    info.appendChild(name);
    const count = document.createElement('div');
    count.className = 'pool-picker-count';
    const n = (p.items || []).length;
    count.textContent = `${n} item${n === 1 ? '' : 's'}`;
    info.appendChild(count);
    row.appendChild(info);

    row.addEventListener('click', () => addCurrentItemToPool(p.id));
    listEl.appendChild(row);
  }
}

async function addCurrentItemToPool(poolId) {
  if (!poolPickerTargetPath) return;
  await fetch(`/api/share/${poolId}/items`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: poolPickerTargetPath, root: poolPickerTargetRoot })
  });
  closePoolPicker();
  loadShares();
}

function closePoolPicker() {
  document.getElementById('poolPickerModal').style.display = 'none';
  poolPickerTargetPath = null;
  poolPickerTargetRoot = '';
}

document.getElementById('poolPickerModal').addEventListener('click', (e) => {
  if (e.target.id === 'poolPickerModal') closePoolPicker();
});

document.getElementById('poolPickerCreateBtn').addEventListener('click', async () => {
  const input = document.getElementById('poolPickerNewName');
  const label = input.value.trim();
  if (!label || !poolPickerTargetPath) return;

  const res = await fetch('/api/share/pool', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label })
  });
  if (!res.ok) return;
  const pool = await res.json();
  input.value = '';
  await addCurrentItemToPool(pool.id);
});

async function loadShares() {
  const listEl = document.getElementById('sharesList');
  if (!listEl) return;
  let shares;
  try {
    const res = await fetch('/api/share');
    if (!res.ok) return;
    shares = await res.json();
  } catch (e) {
    return;
  }
  // Managing share links moved to Settings > Shares (search, expiry, views,
  // grouping by content); this card says what there is and leads there.
  const active = shares.filter((s) => !s.revoked && !s.expired);
  const pools = active.filter((s) => s.kind === 'pool').length;
  const expired = shares.filter((s) => !s.revoked && s.expired).length;
  const summary = document.createElement('p');
  summary.className = 'shares-summary';
  summary.textContent = active.length
    ? `${active.length} active share link${active.length === 1 ? '' : 's'}`
      + (pools ? `, ${pools} of them pool${pools === 1 ? '' : 's'}` : '')
      + (expired ? ` · ${expired} expired` : '') + '.'
    : 'No active share links - right-click any file or folder to share it.';
  listEl.replaceChildren(summary);
  if (viewerIsAdmin) {
    const link = document.createElement('a');
    link.href = '/settings.html#shares';
    link.className = 'btn btn-quiet shares-manage-link';
    link.textContent = 'Manage share links in Settings';
    listEl.appendChild(link);
  }
}

function initShares() {
  const btn = document.getElementById('createPoolBtn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const input = document.getElementById('newPoolLabel');
    const label = input.value.trim();
    if (!label) return;
    const res = await fetch('/api/share/pool', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label })
    });
    if (res.ok) {
      input.value = '';
      loadShares();
    }
  });
  loadShares();
}

// -------------------------------------------------------------------
// Media roots admin - add folders from anywhere on the server as extra
// libraries, and control which ones guests may see.
//
// Admin-only, and gated on the client as well as the server. Every write
// here (add, guest toggle, remove) is @require_admin server-side, so a
// guest could never actually change anything - but rendering the panel to
// them anyway offered an Add form and Remove buttons that answer 403, and
// listed libraries with a blank path, because the server withholds the
// on-disk location from non-admins. Hiding the card is the honest version
// of the same rule.
// -------------------------------------------------------------------

let canManageRoots = false;

async function loadRootsAdmin() {
  const listEl = document.getElementById('rootsList');
  if (!listEl) return;

  let res;
  try {
    res = await fetch('/api/roots');
  } catch (e) {
    return;
  }
  if (!res.ok) return;
  const roots = await res.json();

  listEl.innerHTML = '';
  for (const r of roots) {
    const row = document.createElement('div');
    row.className = 'root-row';

    const info = document.createElement('div');
    info.className = 'root-info';

    const name = document.createElement('div');
    name.className = 'root-name';
    name.textContent = r.name + (r.builtin ? ' (built-in)' : '');
    info.appendChild(name);

    const pathEl = document.createElement('div');
    pathEl.className = 'root-path';
    pathEl.textContent = r.exists ? (r.path || '') : `${r.path || ''}  -  MISSING ON DISK`;
    if (!r.exists) pathEl.classList.add('root-path-missing');
    info.appendChild(pathEl);
    row.appendChild(info);

    const actions = document.createElement('div');
    actions.className = 'root-actions';

    const guestLabel = document.createElement('label');
    guestLabel.className = 'root-guest-toggle';
    const guestBox = document.createElement('input');
    guestBox.type = 'checkbox';
    guestBox.checked = !!r.guest_visible;
    guestBox.addEventListener('change', async () => {
      await fetch(`/api/roots/${encodeURIComponent(r.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ guest_visible: guestBox.checked })
      });
      loadRootsAdmin();
    });
    guestLabel.appendChild(guestBox);
    guestLabel.appendChild(document.createTextNode(' Guests'));
    actions.appendChild(guestLabel);

    if (!r.builtin) {
      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'share-action-btn share-revoke-btn';
      removeBtn.textContent = 'Remove';
      removeBtn.title = 'Removes the library from this list - nothing on disk is deleted';
      removeBtn.addEventListener('click', async () => {
        await fetch(`/api/roots/${encodeURIComponent(r.id)}`, { method: 'DELETE' });
        loadRootsAdmin();
        loadMedia('', true, '');
      });
      actions.appendChild(removeBtn);
    }

    row.appendChild(actions);
    listEl.appendChild(row);
  }
}

async function initRootsAdmin() {
  const btn = document.getElementById('addRootBtn');
  if (!btn) return;

  try {
    const res = await fetch('/api/me', { cache: 'no-store' });
    canManageRoots = res.ok && (await res.json()).role === 'admin';
  } catch (e) {
    canManageRoots = false;
  }
  // The first visibility pass already ran (loadMedia, during init) while
  // the role was still unknown and the card defaulted to hidden. Re-run it
  // now that the answer is in, so an admin gets the card back.
  updateTopLevelSectionsVisibility();
  if (!canManageRoots) return;
  btn.addEventListener('click', async () => {
    const nameInput = document.getElementById('newRootName');
    const pathInput = document.getElementById('newRootPath');
    const errEl = document.getElementById('rootsError');
    errEl.style.display = 'none';

    const name = nameInput.value.trim();
    const path = pathInput.value.trim();
    if (!name || !path) return;

    const res = await fetch('/api/roots', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, path })
    });
    const data = await res.json();
    if (res.ok) {
      nameInput.value = '';
      pathInput.value = '';
      loadRootsAdmin();
      loadMedia('', true, '');
    } else {
      errEl.textContent = data.error || 'Failed to add library';
      errEl.style.display = 'block';
    }
  });
  loadRootsAdmin();
}

// The "Recently Downloaded", "Shares" and "Libraries" panels are
// top-level overviews, not properties of whatever folder you happen to be
// in - so they only show at the very top of the library.
function updateTopLevelSectionsVisibility() {
  // "Top" means the root listing itself, or the top of any one library -
  // not once you've descended into a subfolder. (With a single library
  // configured the server descends straight into it, so currentMediaRoot
  // is set even at the top; the empty path is what actually marks it.)
  const atTop = !currentMediaPath;
  for (const id of ['recentMediaCard', 'sharesCard', 'rootsCard']) {
    const el = document.getElementById(id);
    if (!el) continue;
    // Libraries is admin-only: this pass decides WHERE panels show, not
    // who may see them, so it must not hand the card back to a guest.
    const allowed = id !== 'rootsCard' || canManageRoots;
    el.style.display = atTop && allowed ? '' : 'none';
  }
}

async function loadMedia(path, updateHistory = true, root = currentMediaRoot) {
  currentMediaPath = path || '';
  currentMediaRoot = root || '';

  if (updateHistory) {
    const target = mediaUrlFor(currentMediaPath, currentMediaRoot);
    if (target !== window.location.pathname + window.location.search) {
      history.pushState({ mediaPath: currentMediaPath, mediaRoot: currentMediaRoot }, '', target);
    }
  }

  updateTopLevelSectionsVisibility();

  const grid = document.getElementById('mediaGrid');
  grid.innerHTML = '<div class="media-loading">Loading...</div>';
  document.getElementById('mediaEmpty').style.display = 'none';

  let res;
  try {
    const f = filterQuery();
    const q = `path=${encodeURIComponent(currentMediaPath)}&root=${encodeURIComponent(currentMediaRoot)}` + (f ? '&' + f : '');
    res = await fetch(`/api/media/browse?${q}`);
  } catch (e) {
    grid.innerHTML = '<div class="media-error">Failed to reach server.</div>';
    return;
  }

  if (res.status === 401) {
    window.location.reload();
    return;
  }
  if (!res.ok) {
    grid.innerHTML = '<div class="media-error">Failed to load this folder.</div>';
    return;
  }

  const data = await res.json();

  // Top level with several roots configured: the grid lists the roots
  // themselves rather than any one folder's contents.
  if (data.is_root_listing) {
    currentFilesList = [];
    currentMediaRoot = '';
    currentRootName = '';
    isMultiRoot = true;
    updateTopLevelSectionsVisibility();
    renderBreadcrumb('');
    renderRootListing(data.roots);
    return;
  }

  currentMediaRoot = data.root || '';
  currentRootName = data.root_name || '';
  isMultiRoot = !!data.multi_root;
  currentFilesList = data.files;
  currentDirNames = data.dirs;
  currentDirInfo = data.dir_info || data.dirs.map((d) => ({ name: d, artist: d, id: null }));
  if (folderStatsKey !== currentFolderKey()) currentFolderStats = {};
  updateTopLevelSectionsVisibility();
  renderBreadcrumb(data.path);
  applySort();
}

function renderRootListing(roots) {
  const grid = document.getElementById('mediaGrid');
  const empty = document.getElementById('mediaEmpty');
  grid.innerHTML = '';
  empty.style.display = roots.length ? 'none' : 'block';

  for (const r of roots) {
    const tile = document.createElement('div');
    tile.className = 'media-tile media-folder';
    tile.innerHTML = `<div class="media-tile-icon">\u{1F5C4}\u{FE0F}</div><div class="media-tile-name"></div>`;
    tile.querySelector('.media-tile-name').textContent = r.name;
    tile.addEventListener('click', () => loadMedia('', true, r.id));
    grid.appendChild(tile);
  }
}

function renderBreadcrumb(path) {
  const parts = path ? path.split('/') : [];
  const el = document.getElementById('mediaBreadcrumb');
  el.innerHTML = '';

  // No root selected while several exist means the grid is showing the
  // list of libraries itself - so "Libraries" IS the location, and there
  // is no library below it to name. Appending one anyway fell back to the
  // built-in label, and the list of libraries read "Libraries / Downloads"
  // while standing in neither.
  const atLibraryList = isMultiRoot && !currentMediaRoot;

  // With more than one root, the first crumb steps back out to the root
  // list; with only one there's nothing above it, so it's just a label.
  if (isMultiRoot) {
    const allCrumb = document.createElement('span');
    allCrumb.className = 'crumb';
    allCrumb.textContent = 'Libraries';
    allCrumb.addEventListener('click', () => loadMedia('', true, ''));
    el.appendChild(allCrumb);

    if (!atLibraryList) {
      const sep = document.createElement('span');
      sep.className = 'crumb-sep';
      sep.textContent = ' / ';
      el.appendChild(sep);
    }
  }

  if (!atLibraryList) {
    const rootCrumb = document.createElement('span');
    rootCrumb.className = 'crumb';
    rootCrumb.textContent = currentRootName || DOWNLOADS_ROOT_LABEL;
    rootCrumb.addEventListener('click', () => loadMedia('', true, currentMediaRoot));
    el.appendChild(rootCrumb);
  }

  let accum = '';
  for (const part of parts) {
    accum = accum ? `${accum}/${part}` : part;
    const sep = document.createElement('span');
    sep.className = 'crumb-sep';
    sep.textContent = ' / ';
    el.appendChild(sep);

    const crumb = document.createElement('span');
    crumb.className = 'crumb';
    crumb.textContent = part;
    const target = accum;
    crumb.addEventListener('click', () => loadMedia(target));
    // Every folder on the way here is share-able too, not just the ones
    // that happen to have a tile in the grid below.
    attachContextMenu(crumb, () => ({
      path: target, name: part, root: currentMediaRoot, isFolder: true
    }));
    el.appendChild(crumb);
  }

  updateFolderActions();
}

// The folder being browsed has no tile of its own to right-click, so its
// share actions live in the header instead. The root listing and a root's
// own top level are excluded: the server refuses an empty path, and
// "share everything" is not an action worth one click anyway.
function updateFolderActions() {
  const bar = document.getElementById('folderActions');
  if (!bar) return;
  bar.style.display = currentMediaPath ? 'flex' : 'none';
}

function initFolderActions() {
  document.getElementById('shareFolderBtn').addEventListener('click', async () => {
    if (!currentMediaPath) return;
    await shareItemAndCopyLink(currentMediaPath, currentMediaRoot);
  });

  document.getElementById('folderToPoolBtn').addEventListener('click', () => {
    if (!currentMediaPath) return;
    addToPoolFlow(currentMediaPath, currentMediaRoot);
  });
}

function mediaTypeIcon(type) {
  return { image: '\u{1F5BC}️', video: '\u{1F3AC}', audio: '\u{1F3B5}' }[type] || '\u{1F4C4}';
}

// The gallery helpers - setTileAspect, fitTileToThumb, tileBadge, galleryBreak,
// layoutGallery and the Names toggle - live in js/gallery.js, shared with the
// share viewer. media.html loads it before this file.

function thumbnailUrlFor(f) {
  const root = f.root || currentMediaRoot || '';
  // Thumbnails are cached hard (a day) because the grid asks for hundreds of
  // them - but the URL has to change when the file does, or a regenerated
  // thumbnail stays invisible until the cache expires and the only fix is
  // clearing it by hand. The source's mtime is exactly that key.
  const version = Math.floor(f.mtime || 0);
  return `/api/media/thumbnail?path=${encodeURIComponent(f.path)}&root=${encodeURIComponent(root)}&v=${version}`;
}

// Audio files may or may not have an embedded cover (youtube_music_extractor.py
// embeds one where it can); video files get a generated frame grab. Try the
// thumbnail endpoint and fall back to the generic icon on load failure -
// cheaper than asking the server up front for every file in a folder just
// to know which ones have art.
function attachThumbOrIcon(tile, f) {
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
  img.src = thumbnailUrlFor(f);
  thumbWrap.appendChild(img);
  tile.appendChild(thumbWrap);
}







// The library knows exactly where a file lives, so its info request says
// so. The share viewer's equivalent (public_share.js) deliberately cannot -
// see infoRequestUrl in player.js.
infoRequestUrl = (f) =>
  `/api/media/info?path=${encodeURIComponent(f.path)}`
  + `&root=${encodeURIComponent(f.root || currentMediaRoot || '')}`;

// Shared by the folder grid and the recent-downloads strip.
/* A folder in the search results.
 *
 * Deliberately not a branch of createFileTile: a folder has no thumbnail, no
 * preview and no place in the arrow-key run - it is somewhere to go. Clicking
 * one leaves the search and opens it, which is the point of surfacing folders
 * at all.
 */
function createSearchFolderTile(f) {
  const tile = document.createElement('div');
  tile.className = 'media-tile media-folder';
  tile.innerHTML = `<div class="media-tile-icon">\u{1F4C1}</div><div class="media-tile-name"></div>`;
  tile.querySelector('.media-tile-name').textContent = f.name;

  // Where it lives: two artists can both have a "covers" folder, and the name
  // on its own does not say which one this is.
  const parent = f.path.includes('/') ? f.path.slice(0, f.path.lastIndexOf('/')) : '';
  if (parent) {
    const meta = document.createElement('div');
    meta.className = 'media-tile-folder-meta';
    meta.textContent = parent;
    meta.title = parent;
    tile.appendChild(meta);
  }

  tile.dataset.path = f.path;
  tile.dataset.root = f.root || '';
  tile.dataset.kind = 'folder';
  tile.dataset.label = f.name;

  tile.addEventListener('click', () => {
    // keepEntry: this handler does its own navigating, and letting
    // exitSearchMode pop history would race loadMedia and win.
    exitSearchMode({ keepEntry: true });
    loadMedia(f.path, true, f.root || currentMediaRoot);
  });
  attachContextMenu(tile, () => ({ path: f.path, name: f.name,
                                   root: f.root || currentMediaRoot, isFolder: true }));
  return tile;
}

// -------------------------------------------------------------------
// Convert to 3D (backend stereo3d.py, /api/media/3d/*, wording in
// js/stereo3d.js). Admin-only: the server refuses anyone else, and the
// controls below are simply not shown to a guest or a non-admin.
// -------------------------------------------------------------------

// The select bar's own toast element, shared (media-select.js writes it too).
function showMediaToast(msg) {
  const el = document.getElementById('selectToast');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(showMediaToast._t);
  showMediaToast._t = setTimeout(() => { el.style.display = 'none'; }, 6000);
}

async function convertItemTo3d(item) {
  try {
    const out = await Stereo3d.requestConversion([{ root: item.root || currentMediaRoot || '', path: item.path }]);
    showMediaToast(Stereo3d.summaryText(out));
  } catch (e) {
    showMediaToast(`Could not start the 3D conversion: ${e.message}`);
  }
}

function add3dResultLinks(menu, after, st) {
  let anchor = after;
  for (const [label, url] of [['Open 3D (side by side)', st.sbs_url], ['Open depth map', st.depth_url]]) {
    if (!url) continue;
    const row = document.createElement('div');
    row.className = 'context-menu-item';
    row.textContent = label;
    row.addEventListener('click', () => { hideContextMenu(); window.open(url, '_blank', 'noopener'); });
    anchor.after(row);
    anchor = row;
  }
}

// The row says "Convert to 3D" at once and turns into the item's 3D status as
// soon as the server has answered; a finished conversion gets its two links.
function addConvert3dMenuItems(menu, item) {
  const row = addContextMenuItem(menu, 'Convert to 3D', () => {
    if (row.dataset.busy) { hideContextMenu(); return; }
    hideContextMenu();
    convertItemTo3d(item);
  });
  Stereo3d.fetchStatus(item.root || currentMediaRoot || '', item.path).then((st) => {
    if (!row.isConnected || menu.style.display !== 'block') return;
    if (st.state === 'done') {
      row.textContent = '3D ready';
      row.dataset.busy = '1';
      add3dResultLinks(menu, row, st);
    } else if (Stereo3d.isActive(st)) {
      row.textContent = Stereo3d.statusText(st);
      row.dataset.busy = '1';
    } else if (st.state === 'failed') {
      row.textContent = `${Stereo3d.statusText(st)} - retry`;
    }
    const r = menu.getBoundingClientRect();
    if (r.bottom > window.innerHeight - 8) menu.style.top = Math.max(4, window.innerHeight - r.height - 8) + 'px';
  }).catch(() => {});
}

// The preview header: a "3D" button plus the item's status next to it.
let preview3dSeq = 0;
let preview3dTimer = null;

function paint3dStatus(btn, out, st) {
  const kids = [];
  btn.disabled = false;
  btn.title = 'Convert to 3D';
  // A finished 3D version: the blue of the AI-upscale chip (.upscale-chip); otherwise the neutral look.
  const ready = !!(st && st.state === 'done');
  btn.classList.toggle('convert3d-ready', ready);
  btn.style.borderColor = ready ? 'var(--accent)' : '';
  btn.style.color = ready ? 'var(--accent)' : '';
  btn.style.backgroundColor = ready ? 'var(--accent-wash, rgba(90, 150, 255, 0.12))' : '';
  btn.style.opacity = ready ? '1' : '';
  if (st && st.state === 'done') {
    btn.disabled = true;
    btn.title = 'Already converted';
    // Done: no status text or links here; the VR button is the way in.
  } else if (st && Stereo3d.isActive(st)) {
    btn.disabled = true;
    btn.title = 'Already in the 3D queue';
    kids.push(document.createTextNode(Stereo3d.statusText(st)));
  } else if (st && st.state === 'failed') {
    btn.title = 'Retry the 3D conversion';
    kids.push(document.createTextNode(Stereo3d.statusText(st)));
  }
  out.replaceChildren(...kids);
  out.hidden = kids.length === 0;
}

// The VR buttons (the preview's and the top bar's) never use the disabled attribute for "XR
// unavailable": aria-disabled plus styling keeps clicks arriving, so a click can re-check and enter
// VR at once. Both are hidden when the browser has no WebXR at all.
function setVrAvailable(vrBtn, ok) {
  if (vrSession) ok = true;      // a running session is exited with this button, whatever a re-check says
  vrBtn.setAttribute('aria-disabled', ok ? 'false' : 'true');
  vrBtn.classList.toggle('vr-unavailable', !ok);
  // Same look as :disabled (main.css), without the attribute.
  vrBtn.style.opacity = ok ? '' : '0.45';
  vrBtn.style.cursor = ok ? '' : 'default';
  vrBtn.title = vrSession ? 'Exit VR' : ok ? (vrBtn.dataset.help || Stereo3d.VR_HELP) : Stereo3d.VR_HINT;
}

// Both VR buttons are toggles: while a session started from this page runs (vrSession = its handle)
// they read "Exit VR" with aria-pressed="true" and end it; vrStarting guards the time between the click
// and the running session (a second click then does nothing).
let vrSession = null;
let vrStarting = false;
function paintVrActive() {
  const on = !!vrSession;
  for (const id of ['mediaVrBtn', 'mediaVrTopBtn']) {
    const b = document.getElementById(id);
    if (!b) continue;
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.classList.toggle('vr-active', on);
    b.textContent = on ? 'Exit VR' : 'VR';
    // Gray while no session runs, the blue of the AI-upscale chip (.upscale-chip) while one does.
    b.style.borderColor = on ? 'var(--accent)' : 'var(--line)';
    b.style.color = on ? 'var(--accent)' : 'var(--muted)';
    b.style.backgroundColor = on ? 'var(--accent-wash, rgba(90, 150, 255, 0.12))' : '';
    if (on) {
      b.setAttribute('aria-disabled', 'false');
      b.classList.remove('vr-unavailable');
      b.style.opacity = '';
      b.style.cursor = '';
      b.title = 'Exit VR';
    } else if (typeof Stereo3d !== 'undefined') {
      b.title = b.getAttribute('aria-disabled') === 'true' ? Stereo3d.VR_HINT : (b.dataset.help || Stereo3d.VR_HELP);
    }
  }
}

// The preview's VR button: any picture or video, converted or not (an unconverted one opens in 2D on
// the same screen, a converted one in 3D). Where VR opens is decided at the click, from the state then.
function paintVrButton(f) {
  const vrBtn = document.getElementById('mediaVrBtn');
  if (!vrBtn) return;
  const ok = !!(f && typeof Stereo3d !== 'undefined' && navigator.xr && Stereo3d.canConvert(f));
  vrBtn._vrStatus = null;        // the item's 3D status once known; a done one is shown in 3D
  vrBtn._vrKnown = false;        // false until the status answer arrived
  vrBtn._vrFor = ok ? f : null;
  vrBtn.hidden = !ok;
  if (!ok) return;
  setVrAvailable(vrBtn, false);
  paintVrActive();
  Stereo3d.vrSupported().then((sup) => {
    if (vrBtn._vrFor !== f) return;          // the preview moved on meanwhile
    setVrAvailable(vrBtn, sup);
    // Fetch the VR app now, not at the click; if it cannot be loaded, offer no VR at all.
    if (sup) Stereo3d.loadVrApp().catch(() => { if (vrBtn._vrFor === f) { vrBtn.hidden = true; vrBtn._vrFor = null; } });
  });
}

// The top bar's VR button: the main view (the current folder / search / favourites) in VR.
function paintVrTopButton() {
  const b = document.getElementById('mediaVrTopBtn');
  if (!b) return;
  if (typeof Stereo3d === 'undefined' || !navigator.xr) { b.hidden = true; return; }
  b.hidden = false;
  setVrAvailable(b, false);
  paintVrActive();
  Stereo3d.vrSupported().then((sup) => {
    setVrAvailable(b, sup);
    if (sup) Stereo3d.loadVrApp().catch(() => { b.hidden = true; });
  });
}

// Live availability: devicechange, window focus and the tab becoming visible re-ask the browser.
let vrWatchStop = null;
function startVrWatch() {
  if (vrWatchStop || typeof Stereo3d === 'undefined') return;
  vrWatchStop = Stereo3d.watchVr((ok) => {
    for (const id of ['mediaVrBtn', 'mediaVrTopBtn']) {
      const b = document.getElementById(id);
      if (b && !b.hidden && (id === 'mediaVrTopBtn' || b._vrFor)) setVrAvailable(b, ok);
    }
  });
}

// What the flat page shows as its list right now, as the VR app's source (js/vr/logic.js normalizeContext).
function vrSourceNow() {
  const sort = { key: mediaSortKey, dir: mediaSortDir };
  const filters = filterQuery();
  if (searchModeActive) {
    const onlyFavorites = activeFavorites && !searchQuery && !activeTag && !activePerson;
    const src = { kind: onlyFavorites ? 'favorites' : 'search', root: '', path: '', query: searchQuery, filters, sort,
      offset: searchOffset, mode: searchMode };
    if (activeTag) src.tag = activeTag;
    if (activePerson) src.person = activePerson;
    if (activePool) {
      src.poolTag = activePool.tag || '';
      src.variant = activeVariant || '';
      src.series = activePool.series || '';
      src.count = Number(activePool.count) || 0;
      src.cover = activePool.cover || null;
      src.variants = Array.isArray(activePool.variants) ? activePool.variants : [];
      const selected = activeVariant && activePool.variants.find((v) => v.tag === activeVariant);
      src.label = selected && selected.label && selected.label !== 'Default'
        ? `${activePool.label} (${selected.label})` : (activePool.label || activeSubjectLabel || 'Character');
    }
    return src;
  }
  return { kind: 'folder', root: currentMediaRoot, path: currentMediaPath, query: '', filters, sort, offset: 0 };
}

// f = the open preview item or null (main view); st = its 3D status answer (null = not converted / unknown).
function buildVrContext(f, st) {
  const source = vrSourceNow();
  if (!f) return { view: 'wall', source };
  const item = { root: f.root || currentMediaRoot || '', path: f.path, name: f.name || '', kind: f.type === 'video' ? 'video' : 'picture', url: f.url, favorite: !!f.favorite };
  item.status = st && st.state === 'done' && st.sbs_url
    ? { state: 'done', key: st.key, sbs_url: st.sbs_url, kind: st.kind, eye_w: st.eye_w, eye_h: st.eye_h }
    : { state: (st && st.state) || 'none' };
  return { view: 'item', item, source };
}

// Starts the VR app from a click. The flat player is paused while the headset session runs and
// resumes afterwards if it was playing; the flat page is not driven while in VR.
async function startVrFromClick(btn, context) {
  if (vrStarting || vrSession) return;
  vrStarting = true;
  const flat = document.querySelector('#mediaPreviewBody video');
  const wasPlaying = !!(flat && !flat.paused);
  if (flat) flat.pause();
  const resume = () => { if (flat && wasPlaying && flat.isConnected) flat.play().catch(() => {}); };
  try {
    const handle = await Stereo3d.startVr(context, { onStatus: (t) => { if (t) showMediaToast(t); } });
    vrStarting = false;
    vrSession = handle;
    paintVrActive();
    handle.ended.then((r) => {
      if (vrSession === handle) vrSession = null;
      paintVrActive();
      resume();
      if (r && r.error) showMediaToast(r.error);
    });
  } catch (err) {
    vrStarting = false;
    resume();
    showMediaToast(err && err.message ? err.message : 'Could not start VR.');
  }
}

// A click on a VR button while the session runs ends it (the buttons reset when `ended` resolves).
function endVrFromClick() {
  const s = vrSession;
  if (!s) return false;
  try { const r = s.end(); if (r && r.catch) r.catch(() => {}); } catch (e) { /* already ended */ }
  return true;
}

function paint3dButton(f) {
  const btn = document.getElementById('mediaConvert3dBtn');
  const out = document.getElementById('mediaConvert3dStatus');
  if (!btn || !out) return;
  // js/stereo3d.js failed to load (the server serves some scripts only to some roles): no 3D controls at all.
  if (typeof Stereo3d === 'undefined') return;
  clearTimeout(preview3dTimer);
  const seq = ++preview3dSeq;
  const convertible = !!(f && Stereo3d.canConvert(f));
  const show = convertible && viewerIsAdmin;
  btn.hidden = !show;
  btn.classList.remove('convert3d-ready');
  btn.style.borderColor = btn.style.color = btn.style.backgroundColor = btn.style.opacity = '';
  out.replaceChildren();
  out.hidden = true;
  startVrWatch();
  paintVrButton(f);
  if (!convertible) return;
  btn.disabled = false;
  const refresh = async () => {
    let st = null;
    try { st = await Stereo3d.fetchStatus(f.root || currentMediaRoot || '', f.path); } catch (e) { st = null; }
    if (seq !== preview3dSeq) return;
    if (show) paint3dStatus(btn, out, st);
    // The VR button only learns what to open: a finished item opens in 3D, anything else in 2D.
    const vrBtn = document.getElementById('mediaVrBtn');
    if (vrBtn && vrBtn._vrFor === f) {
      vrBtn._vrStatus = st && st.state === 'done' && st.sbs_url ? st : null;
      vrBtn._vrKnown = true;
    }
    // A queued or running item changes by itself: follow it while the preview stays on it.
    if (st && Stereo3d.isActive(st)) preview3dTimer = setTimeout(refresh, 5000);
  };
  refresh();
}

// Favourites are per account (server.py media_favorite). An entry object is
// shared by the grid, the search results and the preview's list, so flipping it
// here updates all of them; the tiles on screen are found by path.
async function toggleFavorite(f) {
  // Optimistic: the heart flips at once; the server's answer only matters if it refuses.
  const want = !f.favorite;
  const root = f.root || currentMediaRoot || '';
  const apply = (on) => {
    f.favorite = on;
    for (const tile of document.querySelectorAll('.media-tile[data-path]')) {
      if (tile.dataset.path === f.path && tile.dataset.root === root) setTileFavorite(tile, on);
    }
    const shown = activePreviewList[currentPreviewIndex];
    if (shown && shown.path === f.path && (shown.root || currentMediaRoot || '') === root) paintFavoriteButton(shown);
  };
  apply(want);
  let ok = false;
  try {
    const res = await fetch('/api/media/favorite', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root, path: f.path, favorite: want }),
    });
    ok = res.ok;
  } catch (e) {
    ok = false;
  }
  if (!ok && f.favorite === want) apply(!want);
}

function setTileFavorite(tile, on) {
  let mark = tile.querySelector('.media-tile-fav');
  if (on && !mark) {
    mark = document.createElement('span');
    mark.className = 'media-tile-fav';
    mark.textContent = '♥';
    mark.title = 'Favorite';
    tile.appendChild(mark);
  } else if (!on && mark) {
    mark.remove();
  }
}

function paintFavoriteButton(f) {
  const btn = document.getElementById('mediaFavoriteBtn');
  if (!btn) return;
  const on = !!(f && f.favorite);
  btn.textContent = on ? '♥' : '♡';
  btn.setAttribute('aria-pressed', String(on));
  btn.title = on ? 'Remove from favorites' : 'Add to favorites';
}

function createFileTile(f, previewList) {
  const tile = document.createElement('div');
  tile.className = 'media-tile media-file';
  setTileAspect(tile, f.width, f.height);

  if (f.type === 'image') {
    const thumbWrap = document.createElement('div');
    thumbWrap.className = 'media-tile-thumb';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = f.name;
    // The downscaled thumbnail, not the original: these tiles are ~140px,
    // and loading full-size images here meant tens of MB per page plus a
    // multi-megapixel decode each. Falls back to the original if the
    // thumbnailer couldn't handle this file.
    img.addEventListener('error', () => { img.src = f.url; }, { once: true });
    img.addEventListener('load', () => fitTileToThumb(tile, img));
    img.src = thumbnailUrlFor(f);
    thumbWrap.appendChild(img);
    tile.appendChild(thumbWrap);
  } else if (f.type === 'audio' || f.type === 'video') {
    attachThumbOrIcon(tile, f);
  } else {
    const icon = document.createElement('div');
    icon.className = 'media-tile-icon';
    icon.textContent = mediaTypeIcon(f.type);
    tile.appendChild(icon);
    tile.classList.add('no-thumb');
  }

  const badge = tileBadge(f);
  if (badge) tile.appendChild(badge);
  if (f.favorite) setTileFavorite(tile, true);

  const nameEl = document.createElement('div');
  nameEl.className = 'media-tile-name';
  nameEl.textContent = f.name;
  tile.appendChild(nameEl);

  const sizeEl = document.createElement('div');
  sizeEl.className = 'media-tile-size';
  // Size and date on one line - the tiles are narrow, and a second line
  // would push the grid rows taller for very little gain.
  sizeEl.textContent = formatFileSize(f.size) + '  ·  ' + formatShortDate(f.mtime);
  sizeEl.title = `Created ${formatDateTime(f.mtime)}\nDownloaded ${formatDateTime(f.added)}`;
  tile.appendChild(sizeEl);

  tile.dataset.path = f.path;
  tile.dataset.root = f.root || currentMediaRoot || '';
  tile.dataset.kind = 'file';
  tile.dataset.label = f.name;
  // With names hidden this is where the name is: the tooltip.
  tile.title = `${f.name}\n${formatFileSize(f.size)} · ${formatShortDate(f.mtime)}`;
  tile.addEventListener('click', () => openMediaPreview(f, previewList));
  attachContextMenu(tile, () => f);
  return tile;
}

// -------------------------------------------------------------------
// Sorting the folder browser
// -------------------------------------------------------------------
// Name/artist/ID come with the listing (the server parses the per-source
// folder conventions - see parse_folder_identity). Size and last-updated
// are subtree totals, so they're fetched only when actually sorted by.

const SORT_KEYS = ['name', 'artist', 'id', 'latest', 'size'];
// Nobody wants the smallest folder or the stalest date first.
const DESCENDING_BY_DEFAULT = ['latest', 'size'];

let mediaSortKey = localStorage.getItem('mediaSortKey') || 'name';
let mediaSortDir = localStorage.getItem('mediaSortDir') || 'asc';
if (!SORT_KEYS.includes(mediaSortKey)) mediaSortKey = 'name';
if (mediaSortDir !== 'desc') mediaSortDir = 'asc';

let currentDirInfo = [];
let currentFolderStats = {};
// Which folder the stats in hand describe, so switching folders doesn't
// sort by the previous one's numbers.
let folderStatsKey = '';
let folderStatsPending = false;

function sortNeedsFolderStats() {
  return mediaSortKey === 'latest' || mediaSortKey === 'size';
}

function currentFolderKey() {
  return `${currentMediaRoot}|${currentMediaPath}`;
}

function compareText(a, b) {
  // numeric:true so "Chapter 9" precedes "Chapter 10".
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

// A folder with no artist (twitter stores only "@handle") or no id
// (youtube records no channel handle on disk) has nothing to compare -
// those sort to the end either way rather than clumping at whichever end
// an empty string would land on.
function compareWithMissingLast(aVal, bVal, compare) {
  const aMissing = aVal === null || aVal === undefined || aVal === '';
  const bMissing = bVal === null || bVal === undefined || bVal === '';
  if (aMissing && bMissing) return 0;
  if (aMissing) return Number.POSITIVE_INFINITY;
  if (bMissing) return Number.NEGATIVE_INFINITY;
  return compare(aVal, bVal);
}

function sortValueForDir(info) {
  const stats = currentFolderStats[info.name];
  switch (mediaSortKey) {
    case 'artist': return info.artist || info.name;
    case 'id': return info.id;
    case 'latest': return stats ? stats.latest : null;
    case 'size': return stats ? stats.size : null;
    default: return info.name;
  }
}

function sortValueForFile(f) {
  switch (mediaSortKey) {
    case 'latest': return f.added || f.mtime;
    case 'size': return f.size;
    // Files carry no artist/ID of their own; falling back to the name
    // keeps them in a stable, readable order instead of shuffling.
    default: return f.name;
  }
}

function sortedByCurrentMode(items, valueOf) {
  const numeric = sortNeedsFolderStats();
  const compare = numeric ? (a, b) => a - b : compareText;
  const factor = mediaSortDir === 'desc' ? -1 : 1;

  return items.slice().sort((a, b) => {
    const raw = compareWithMissingLast(valueOf(a), valueOf(b), compare);
    // The missing-last sentinels must survive the direction flip, so they
    // are applied after it rather than being multiplied by -1.
    if (!Number.isFinite(raw)) return raw;
    return raw * factor;
  });
}

function renderMediaGrid(dirs, files) {
  const grid = document.getElementById('mediaGrid');
  const empty = document.getElementById('mediaEmpty');
  grid.innerHTML = '';

  if (!dirs.length && !files.length) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  const infos = dirs.map((d) => currentDirInfo.find((i) => i.name === d) || { name: d, artist: d, id: null });

  for (const info of sortedByCurrentMode(infos, sortValueForDir)) {
    const d = info.name;
    const tile = document.createElement('div');
    tile.className = 'media-tile media-folder';
    tile.innerHTML = `<div class="media-tile-icon">\u{1F4C1}</div><div class="media-tile-name"></div>`;
    tile.querySelector('.media-tile-name').textContent = d;

    const stats = currentFolderStats[d];
    if (stats) {
      const meta = document.createElement('div');
      meta.className = 'media-tile-folder-meta';
      meta.textContent = `${formatFileSize(stats.size)} · ${formatShortDate(stats.latest)}`;
      meta.title = `${stats.files} file${stats.files === 1 ? '' : 's'}`;
      tile.appendChild(meta);
    }

    const folderPath = currentMediaPath ? `${currentMediaPath}/${d}` : d;
    // Identity on the tile itself, so select-mode (media-select.js) can
    // work off the DOM without a second copy of the listing.
    tile.dataset.path = folderPath;
    tile.dataset.root = currentMediaRoot || '';
    tile.dataset.kind = 'folder';
    tile.dataset.label = d;
    tile.addEventListener('click', () => loadMedia(folderPath));
    attachContextMenu(tile, () => ({ path: folderPath, name: d, root: currentMediaRoot, isFolder: true }));
    grid.appendChild(tile);
  }

  // The tiles are laid out in the CHOSEN sort order, so the arrow keys must
  // walk that same order. Handing each tile the unsorted currentFilesList
  // meant "next" followed the server's order instead, and the run always
  // ended on whichever file happened to be last in THAT order - the same file
  // no matter how the folder was sorted.
  const ordered = sortedByCurrentMode(files, sortValueForFile);
  if (infos.length && ordered.length) grid.appendChild(galleryBreak());
  for (const f of ordered) {
    grid.appendChild(createFileTile(f, ordered));
  }
  scheduleGalleryLayout(grid, true);
}

function setSortStatus(text) {
  const el = document.getElementById('mediaSortStatus');
  if (el) el.textContent = text || '';
}

async function ensureFolderStats() {
  const key = currentFolderKey();
  if (folderStatsKey === key || folderStatsPending) return;
  if (!currentDirInfo.length) {
    folderStatsKey = key;
    return;
  }

  folderStatsPending = true;
  setSortStatus('measuring…');
  const params = new URLSearchParams();
  if (currentMediaPath) params.set('path', currentMediaPath);
  if (currentMediaRoot) params.set('root', currentMediaRoot);

  try {
    const res = await fetch(`/api/media/folder-stats?${params.toString()}`);
    if (res.ok) {
      const data = await res.json();
      // The user may have navigated away while this was in flight.
      if (currentFolderKey() === key) {
        currentFolderStats = data.folders || {};
        folderStatsKey = key;
      }
    }
  } catch (e) {
    // Leave the stats empty: those folders just sort to the end.
  } finally {
    folderStatsPending = false;
    setSortStatus('');
  }
}


// ---------------------------------------------------------------------------
// Media filters — orientation and three resolution families.
//
// The three families share one set of 16:9 numbers and differ only in the edge
// they measure: horizontal looks at the width, vertical at the height, and
// "either" at the longest side, which is exactly the union of the other two.
// Vertical is the one worth having on a phone: a portrait video that fills the
// screen top to bottom is judged on its height, because the sides are where
// the black bars go.
//
// The server does the filtering. It has to: filtering a page of results after
// paging would hand back short pages and a total counting files never shown.
// ---------------------------------------------------------------------------

const FILTER_KEYS = ['kind', 'orientation', 'min_res', 'min_hres', 'min_vres', 'min_fps', 'max_fps', 'content', 'fav'];
// Per viewer: never part of a live share, which has no viewer to have favourites.
const VIEWER_FILTER_KEYS = ['fav'];
let mediaFilters = {};
let filterOptions = null;

function loadStoredFilters() {
  try {
    const raw = JSON.parse(localStorage.getItem('mediaFilters') || '{}');
    for (const k of FILTER_KEYS) if (raw[k]) mediaFilters[k] = raw[k];
  } catch (e) { /* nothing stored, or unreadable: no filters */ }
}

function storeFilters() {
  try { localStorage.setItem('mediaFilters', JSON.stringify(mediaFilters)); }
  catch (e) { /* private mode: filters just will not be remembered */ }
}

function activeFilterCount() {
  return FILTER_KEYS.filter((k) => mediaFilters[k]).length;
}

// Appended to every browse and search request, so a filter survives paging,
// navigation and a reload of a shared URL.
function filterQuery() {
  return FILTER_KEYS
    .filter((k) => mediaFilters[k])
    .map((k) => `${k}=${encodeURIComponent(mediaFilters[k])}`)
    .join('&');
}

function setFilter(key, value) {
  if (mediaFilters[key] === value) delete mediaFilters[key];   // click again to clear
  else mediaFilters[key] = value;
  storeFilters();
  renderFilterPanel();
  syncFilterButton();
  reloadForFilters();
}

function clearFilters() {
  mediaFilters = {};
  storeFilters();
  renderFilterPanel();
  syncFilterButton();
  reloadForFilters();
}

/* A subject with no words yet - Favorites, a person, a character - is a search
   too. Testing searchQuery alone sent a filter change there to the folder
   hidden underneath, so the favourites never filtered (user, 2026-09-24:
   "Favorites should be filterable"). */
function reloadForFilters() {
  resetRecentMedia();
  if (searchModeActive && (searchQuery || hasSubject())) runSearch(searchQuery);
  else loadMedia(currentMediaPath, false, currentMediaRoot);
}

function syncFilterButton() {
  const n = activeFilterCount();
  const badge = document.getElementById('mediaFilterCount');
  badge.textContent = String(n);
  badge.hidden = n === 0;
}

function optionButton(label, key, value, title) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'mf-opt';
  b.textContent = label;
  if (title) b.title = title;
  b.setAttribute('aria-pressed', String(mediaFilters[key] === value));
  b.addEventListener('click', () => setFilter(key, value));
  return b;
}

// A filter that takes several values at once, kept as a comma-separated list
// in the options' own order - so Safe and Suggestive make one URL however
// they were clicked together.
function toggleFilterValue(key, value, order) {
  const chosen = new Set(String(mediaFilters[key] || '').split(',').filter(Boolean));
  if (chosen.has(value)) chosen.delete(value);
  else chosen.add(value);
  const next = order.filter((v) => chosen.has(v));
  if (next.length) mediaFilters[key] = next.join(',');
  else delete mediaFilters[key];
  storeFilters();
  renderFilterPanel();
  syncFilterButton();
  reloadForFilters();
}

function multiOptionButton(label, key, value, order, title) {
  const chosen = String(mediaFilters[key] || '').split(',');
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'mf-opt';
  b.textContent = label;
  if (title) b.title = title;
  b.setAttribute('aria-pressed', String(chosen.includes(value)));
  b.addEventListener('click', () => toggleFilterValue(key, value, order));
  return b;
}

// "safe,suggestive" -> "Safe + Suggestive", for a live share's name.
function contentLabel(value) {
  const opts = (filterOptions && filterOptions.contents) || [];
  return String(value).split(',').filter(Boolean)
    .map((v) => (opts.find((o) => o.key === v) || { label: v }).label).join(' + ');
}

function applyAiCoverage(opts) {
  const ai = (opts && opts.ai) || null;
  if (!ai) return;
  // The denominator is what a pass CAN process - files still in the library
  // that are pictures or videos (ai.indexable). library_files counts music,
  // ban tombstones and sidecars too, and the old result counts included
  // trashed files, so the note read "509,487 of 533,966 (95%)" with tagging
  // finished. The fallbacks are for a server that predates those fields.
  aiCoverage = { ...ai, library: ai.indexable || opts.library_files || ai.files || 0 };
  searchNote();
}

function renderFilterPanel() {
  if (!filterOptions) return;
  const std = filterOptions.standards || [];

  const kind = document.getElementById('mfKind');
  kind.innerHTML = '';
  // Single-select: "show only videos" is the question, so picking one clears
  // the last, and clicking the active one again clears the filter entirely.
  for (const k of (filterOptions.kinds || [])) {
    kind.appendChild(optionButton(k.label, 'kind', k.key, k.hint));
  }

  // Several at once: Safe and Suggestive together are "safe for work".
  const content = document.getElementById('mfContent');
  if (content) {
    content.innerHTML = '';
    const opts = filterOptions.contents || [];
    const order = opts.map((o) => o.key);
    const counts = filterOptions.content_counts || {};
    for (const o of opts) {
      const n = counts[o.key];
      content.appendChild(multiOptionButton(o.label, 'content', o.key, order,
        n == null ? o.hint : `${o.hint} - ${Number(n).toLocaleString('en-GB')} files`));
    }
  }

  const fav = document.getElementById('mfFavorites');
  if (fav) {
    fav.innerHTML = '';
    fav.appendChild(optionButton('In favorites', 'fav', '1', 'Only files you have favourited'));
  }

  const orient = document.getElementById('mfOrientation');
  orient.innerHTML = '';
  for (const o of filterOptions.orientations || []) {
    orient.appendChild(optionButton(
      o.charAt(0).toUpperCase() + o.slice(1), 'orientation', o));
  }

  const fill = (id, key, edge) => {
    const box = document.getElementById(id);
    box.innerHTML = '';
    for (const st of std) {
      box.appendChild(optionButton(
        st.label, key, st.key, `${edge} at least ${st.long_edge}px`));
    }
  };
  fill('mfResAny', 'min_res', 'Longest side');
  fill('mfResH', 'min_hres', 'Width');
  fill('mfResV', 'min_vres', 'Height');

  // A floor and a ceiling; both set make a band (at least 30, at most 30).
  const rates = filterOptions.frame_rates || {};
  const fillFps = (id, key, list, word) => {
    const box = document.getElementById(id);
    if (!box) return;
    box.innerHTML = '';
    for (const r of list || []) {
      box.appendChild(optionButton(`${r.fps} fps`, key, r.key, `${word} ${r.fps} frames a second`));
    }
  };
  fillFps('mfFpsMin', 'min_fps', rates.min, 'At least');
  fillFps('mfFpsMax', 'max_fps', rates.max, 'At most');

  const cov = document.getElementById('mediaFilterCoverage');
  // Said plainly, because a half-finished index looks exactly like a filter
  // that is losing results at random.
  const fmt = (n) => Number(n || 0).toLocaleString('en-GB');
  // The buttons come with the page; the figures follow from /api/media/filters.
  if (filterOptions.known === undefined) {
    cov.textContent = 'Counting what the library can answer…';
    return;
  }
  const bits = [filterOptions.known
    ? `${fmt(filterOptions.known)} files measured`
    : 'nothing measured yet — size filters will match nothing'];
  if (filterOptions.videos_known && filterOptions.fps_known < filterOptions.videos_known) {
    bits.push(`frame rate known for ${fmt(filterOptions.fps_known)} of ${fmt(filterOptions.videos_known)} videos`);
  }
  if (filterOptions.content_counts) {
    const rated = Object.values(filterOptions.content_counts).reduce((a, b) => a + b, 0);
    bits.push(`content rated for ${fmt(rated)} files`);
  }
  cov.textContent = bits.join(' · ');
}

async function initFilters() {
  loadStoredFilters();
  syncFilterButton();

  const btn = document.getElementById('mediaFilterBtn');
  const panel = document.getElementById('mediaFilterPanel');

  const close = () => {
    panel.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  };
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
  });
  // Capture phase and stopPropagation, like the context menu: a bubble-phase
  // handler here would let the same click also reach the preview modal.
  document.addEventListener('click', (e) => {
    if (panel.hidden) return;
    if (panel.contains(e.target) || e.target === btn) return;
    close();
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !panel.hidden) { e.stopPropagation(); close(); }
  }, true);
  document.getElementById('mediaFilterClear').addEventListener('click', clearFilters);
  // Before the fetch, not after it: the search-type chip used to answer taps
  // only once /api/media/filters had returned - seconds on a freshly started
  // server, so on a phone it "did not expand" - and never if rendering the
  // panel threw (2026-09-16).
  initSearchModeChip();

  // The buttons are in the page itself (the server fills #filterOptionsStatic into
  // media.html), so the panel is whole before any request. Only the coverage
  // figures - seconds to count on a big library - wait for /api/media/filters.
  let staticOptions = null;
  try {
    const el = document.getElementById('filterOptionsStatic');
    if (el) staticOptions = JSON.parse(el.textContent);
  } catch (e) { /* no inline options: the request below brings everything */ }
  if (staticOptions) {
    filterOptions = staticOptions;
    renderFilterPanel();
  }

  try {
    const res = await fetch('/api/media/filters');
    if (res.ok) {
      filterOptions = { ...(staticOptions || {}), ...(await res.json()) };
      applyAiCoverage(filterOptions);
    }
  } catch (e) { /* the buttons stay; only the figures are missing */ }
  renderFilterPanel();
  // The coverage note reads the counts that just arrived.
  searchNote();
}

/* The mode chip: what the bar is currently searching, and the menu to change
   it. It replaced a <select> because the mode is state worth reading, not a
   control worth staring at - and because a native select cannot be made to sit
   inside a pill without fighting the platform for its arrow. */

const SEARCH_MODE_LABELS = {
  filename: 'Filename', context: 'Context', ocr: 'Text in image',
  character: 'Character', all: 'Everything',
};

function syncSearchModeChip() {
  const chip = document.getElementById('mediaSearchModeChip');
  if (!chip) return;
  chip.textContent = SEARCH_MODE_LABELS[searchMode] || searchMode;
  // Filename is the resting state; anything else is a choice in force and is
  // marked as such, the same way an active filter is.
  chip.classList.toggle('is-set', searchMode !== 'filename');
  document.querySelectorAll('.search-chip-opt').forEach((opt) => {
    opt.setAttribute('aria-current', String(opt.dataset.mode === searchMode));
  });
}

function initSearchModeChip() {
  const chip = document.getElementById('mediaSearchModeChip');
  const menu = document.getElementById('mediaSearchModeMenu');
  if (!chip || !menu) return;

  try {
    let saved = localStorage.getItem('mediaSearchMode');
    // One-time repair: clicking a character pool used to SAVE 'character' as
    // the search type, so a stored 'character' is almost always that leftover
    // rather than a choice - and it made every later search blind to music.
    // Dropped once; a character mode picked from the chip afterwards is kept.
    if (saved === 'character' && !localStorage.getItem('mediaSearchModeRepaired')) {
      saved = 'filename';
      localStorage.setItem('mediaSearchMode', saved);
    }
    localStorage.setItem('mediaSearchModeRepaired', '1');
    // Not over a type a subject brought with it - a reloaded or copied link to
    // a character narrowed in Context.
    if (saved && SEARCH_MODE_LABELS[saved] && !searchModeFromPool) searchMode = saved;
  } catch (e) { /* private mode: filename it is */ }
  syncSearchModeChip();
  searchNote();

  const close = () => {
    menu.hidden = true;
    chip.setAttribute('aria-expanded', 'false');
  };
  chip.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    chip.setAttribute('aria-expanded', String(open));
  });
  menu.addEventListener('click', (e) => {
    const opt = e.target.closest('.search-chip-opt');
    if (!opt) return;
    e.stopPropagation();
    close();
    if (opt.dataset.browse === 'people') {
      document.getElementById('peopleSearch').value = document.getElementById('mediaSearchInput').value.trim().replace(/^\*$/, '');
      setBrowseView('people');
      document.getElementById('peopleSearch').focus();
    } else setSearchType(opt.dataset.mode);
  });
  // Capture phase and stopPropagation, like the filter panel: a bubble-phase
  // handler here would let the same click also reach the preview modal.
  document.addEventListener('click', (e) => {
    if (menu.hidden) return;
    if (menu.contains(e.target) || e.target === chip) return;
    close();
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) { e.stopPropagation(); close(); }
  }, true);
}

let currentDirNames = [];

function syncSortControls() {
  document.getElementById('mediaSortSelect').value = mediaSortKey;
  document.getElementById('mediaSortDirBtn').textContent = mediaSortDir === 'desc' ? '↑' : '↓';
}

// Renders with whatever is already known, then again once the subtree
// totals arrive - measuring a folder like twitter/ takes a moment and the
// grid should not sit empty waiting for it.
async function applySort() {
  syncSortControls();
  renderMediaGrid(currentDirNames, currentFilesList);
  if (sortNeedsFolderStats() && folderStatsKey !== currentFolderKey()) {
    await ensureFolderStats();
    renderMediaGrid(currentDirNames, currentFilesList);
  }
}

function initSorting() {
  const select = document.getElementById('mediaSortSelect');
  const dirBtn = document.getElementById('mediaSortDirBtn');

  select.addEventListener('change', () => {
    mediaSortKey = select.value;
    // Picking a new field resets the direction to whatever is sensible for
    // it; flipping afterwards is one click.
    mediaSortDir = DESCENDING_BY_DEFAULT.includes(mediaSortKey) ? 'desc' : 'asc';
    localStorage.setItem('mediaSortKey', mediaSortKey);
    localStorage.setItem('mediaSortDir', mediaSortDir);
    applySort();
  });

  dirBtn.addEventListener('click', () => {
    mediaSortDir = mediaSortDir === 'desc' ? 'asc' : 'desc';
    localStorage.setItem('mediaSortDir', mediaSortDir);
    applySort();
  });
}

// -------------------------------------------------------------------
// Recently Downloaded (below the folder browser) - lazy-loaded strip
// -------------------------------------------------------------------

let recentMediaItems = [];
let recentMediaOffset = 0;
let recentMediaLoading = false;
let recentMediaHasMore = true;
let recentMediaGeneration = 0;

function resetRecentMedia() {
  recentMediaGeneration++;
  recentMediaItems = [];
  recentMediaOffset = 0;
  recentMediaLoading = false;
  recentMediaHasMore = true;
  document.getElementById('recentMediaGrid').replaceChildren();
  document.getElementById('recentMediaEmpty').style.display = 'none';
  document.getElementById('recentMediaEnd').style.display = 'none';
  loadMoreRecentMedia();
}

async function loadMoreRecentMedia() {
  if (recentMediaLoading || !recentMediaHasMore) return;
  recentMediaLoading = true;
  const generation = recentMediaGeneration;
  const loadingEl = document.getElementById('recentMediaLoading');
  const endEl = document.getElementById('recentMediaEnd');
  const emptyEl = document.getElementById('recentMediaEmpty');
  loadingEl.style.display = 'block';
  endEl.style.display = 'none';

  try {
    const query = filterQuery();
    const res = await fetch(`/api/media/recent?offset=${recentMediaOffset}&limit=40${query ? '&' + query : ''}`);
    if (generation !== recentMediaGeneration) return;
    if (res.status === 401) {
      window.location.reload();
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    if (generation !== recentMediaGeneration) return;
    const grid = document.getElementById('recentMediaGrid');
    for (const f of data.items) {
      recentMediaItems.push(f);
      grid.appendChild(createFileTile(f, recentMediaItems));
    }
    scheduleGalleryLayout(grid);
    recentMediaOffset += data.items.length;
    recentMediaHasMore = !!data.has_more;
    if (recentMediaItems.length === 0) {
      emptyEl.textContent = query ? 'No recent media matches these filters.' : 'Nothing downloaded yet.';
      emptyEl.style.display = 'block';
    } else if (!recentMediaHasMore) {
      endEl.style.display = 'block';
    }
  } catch (e) {
    // Leave the page retryable on the next scroll after a failed request.
  } finally {
    // An older request must not change the new filter's loading state.
    if (generation === recentMediaGeneration) {
      recentMediaLoading = false;
      loadingEl.style.display = 'none';
    }
  }
}

function initRecentMedia() {
  const scrollEl = document.getElementById('recentMediaScroll');
  scrollEl.addEventListener('scroll', () => {
    const nearBottom = scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 150;
    if (nearBottom) loadMoreRecentMedia();
  });
  loadMoreRecentMedia();
}

// -------------------------------------------------------------------
// Search - filename/folder substring match, lazy-loaded like the recent
// strip. Replaces the folder browser + recent strip while a query is
// active; clearing the box goes back to normal browsing.
// -------------------------------------------------------------------

let searchQuery = '';
// Filename is the default because it needs no index and is what someone
// typing an artist name means. The others read what the vision models saw and
// are only as complete as the backfill - which is why searchNote() says so out
// loud rather than letting a thin result look like a broken search.
let searchMode = 'filename';
// Set when a pool was clicked. Cleared by any typing, because at that point
// the words in the box are the question again, not the tag.
let activeTag = '';
// True while the search type was set by clicking a pool rather than chosen.
let searchModeFromPool = false;
// The character pool being browsed, if any: {tag, label, series, count,
// variants[]}. Held because the strip of variant circles has to be rebuilt on
// every render, including one restored by the Back button.
let activePool = null;
let activeVariant = null;   // the single variant tag chosen, or null for all
let activePerson = null;   // a face cluster being browsed
let activeFavorites = false;   // the viewer's favourites being browsed (favorites=1)
// What the subject chip says: the pool or tag label, the person's name, or
// "Favorites". Kept apart from the search box, whose words now narrow it.
let activeSubjectLabel = '';
let aiCoverage = null;
let characterPools = null;      // cached list, so reopening is instant
let suggestTimer = null;
let searchItems = [];
// Folders are search results too, but they are not preview items, so the
// "no results" test cannot be searchItems.length any more.
let searchResultCount = 0;
let searchOffset = 0;
let searchLoading = false;
// Counts searches. runSearch() empties the grid and starts over without waiting
// for a page still in flight, so that page's answer has to be recognised as
// belonging to the search before and dropped - it used to be appended to the
// new results (user, 2026-09-30: pictures under the Videos filter, the
// unfiltered page arriving after the filter was set).
let searchGeneration = 0;
let searchHasMore = true;
let searchDebounceTimer = null;

let searchModeActive = false;

function setSearchMode(active) {
  searchModeActive = active;
  document.getElementById('browseSections').style.display = active ? 'none' : '';
  document.getElementById('searchResultsCard').style.display = active ? '' : 'none';
}

/* A character pool, a person or the favourites being searched within. Words
   typed beside it narrow it (narrow_to_words in server.py). Until 2026-09-14
   typing replaced the subject, so a character and "at the beach" could not be
   asked together. */
function hasSubject() {
  return !!(activeTag || activePerson || activeFavorites);
}

function syncSubjectChip() {
  const chip = document.getElementById('mediaSubjectChip');
  const input = document.getElementById('mediaSearchInput');
  const on = hasSubject();
  if (chip) {
    chip.hidden = !on;
    chip.querySelector('.search-subject-label').textContent = on ? activeSubjectLabel : '';
  }
  if (input) {
    if (!input.dataset.idlePlaceholder) input.dataset.idlePlaceholder = input.placeholder;
    input.placeholder = on ? 'Add words to narrow it down: beach, sword, night...' : input.dataset.idlePlaceholder;
  }
}

/* What a search's history entry and URL carry beside the words, so Back, a
   reload or a copied link come back to the same subject in the same type. */
function subjectParams() {
  if (!hasSubject()) return { tag: '', pool: '', person: '', fav: '', label: '', mode: '' };
  return {
    tag: activeTag || '', pool: activePool ? activePool.tag : '',
    person: activePerson ? String(activePerson) : '', fav: activeFavorites ? '1' : '',
    label: activeSubjectLabel, mode: searchMode,
  };
}

/* Entries and links from before words could narrow a subject carried its label
   as the query ("?q=Hatsune Miku&tag=hatsune_miku"): that is read as the label.
   Returns the subject with the real query beside it. */
function legacySubject(subject, query) {
  if (subject.tag && !subject.label && query) return { ...subject, label: query, query: '' };
  return { ...subject, query };
}

/* A subject from a history entry or a URL. The pool itself is fetched by
   restorePool. */
function restoreSubject({ tag = '', person = '', fav = '', label = '', mode = '' } = {}) {
  activeTag = tag || '';
  activePerson = !activeTag && person ? person : null;
  activeFavorites = !activeTag && !activePerson && fav === '1';
  if (!activeTag) { activePool = null; activeVariant = null; }
  if (hasSubject()) {
    activeSubjectLabel = label
      || (activeFavorites ? 'Favorites' : activeTag ? activeTag.split(',')[0].replace(/_/g, ' ') : 'this person');
    if (SEARCH_MODE_LABELS[mode]) {
      searchMode = mode;
      searchModeFromPool = true;
      syncSearchModeChip();
    }
  } else {
    activeSubjectLabel = '';
  }
  syncSubjectChip();
}

/* The common end of a pool, person or favourites click. The subject goes in its
   chip and the box is left for words that narrow it, read as what the picture
   shows - captions and tags ("context") - for this browse only: saving that type
   once made every later search a tag search, which never finds music (vault
   KSF-014), and the chip's x brings the viewer's own type back.
   keepWords: a variant circle moves within the pool on screen, so the words
   stay, in the type already in force. */
function enterSubject(label, { keepWords = false } = {}) {
  activeSubjectLabel = label;
  const input = document.getElementById('mediaSearchInput');
  if (!keepWords) {
    input.value = '';
    searchMode = 'context';
    searchModeFromPool = true;
    syncSearchModeChip();
  }
  syncSubjectChip();
  hideSuggest();
  searchNote();
  return enterSearchMode(input.value.trim());
}

/* The chip's x, or Backspace in an empty box: the subject goes and the words
   stay, as an ordinary search in the type the viewer last chose. With no words
   left there is nothing to search. */
function clearSubject() {
  activeTag = '';
  activePool = null;
  activeVariant = null;
  activePerson = null;
  activeFavorites = false;
  activeSubjectLabel = '';
  if (searchModeFromPool) {
    searchModeFromPool = false;
    try { searchMode = localStorage.getItem('mediaSearchMode') || 'filename'; } catch (e) { searchMode = 'filename'; }
    if (!SEARCH_MODE_LABELS[searchMode]) searchMode = 'filename';
    syncSearchModeChip();
  }
  syncSubjectChip();
  hideSuggest();
  searchNote();
  const q = document.getElementById('mediaSearchInput').value.trim();
  if (q) return enterSearchMode(q);
  exitSearchMode();
  return Promise.resolve();
}

// Searching is a place you can be, so it gets a history entry: Back leaves
// the search and returns to the folder underneath, instead of leaving the
// library altogether. Typing more only rewrites that one entry - a push
// per keystroke would bury the folder under a dozen Back presses.
function enterSearchMode(query) {
  const subject = subjectParams();
  const state = {
    mediaPath: currentMediaPath, mediaRoot: currentMediaRoot, search: query,
    // A subject with no words yet is a search too, and `search` is '' then.
    searching: true, ...subject,
  };
  const url = mediaUrlFor(currentMediaPath, currentMediaRoot, query, '', subject);
  // Normally re-entering the search replaces its entry, so typing does not
  // fill the history with a stack of half-typed words. Picking a variant is
  // different: it is a move to a new place within the same pool, and Back has
  // to come back out of it to the whole character.
  const wasPool = history.state && history.state.pool;
  const movedWithinPool = wasPool && state.pool === wasPool
      && state.tag !== (history.state.tag || '');
  if (!movedWithinPool
      && (searchModeActive || (history.state && (history.state.searching || history.state.search)))) {
    history.replaceState(state, '', url);
  } else {
    history.pushState(state, '', url);
  }
  setSearchMode(true);
  return runSearch(query);
}

// Emptying the box is the same gesture as pressing Back, so it pops that
// entry rather than pushing a second one (cf. closeMediaPreview).
/* keepEntry: leave the search's history entry alone because the caller is
   about to navigate somewhere itself.

   Popping it here fires popstate, and that handler re-runs loadMedia() for
   whatever folder the PREVIOUS entry names - which lands after the caller's
   own navigation and overwrites currentMediaPath with the folder you were in
   before searching. Everything gated on currentMediaPath then breaks: the
   folder actions bar hides itself and "Add folder to pool" refuses to open.
   Keeping the entry also makes Back go from the folder to the search results
   it was opened from, which is where it should go anyway. */
function exitSearchMode({ fromPopstate = false, keepEntry = false } = {}) {
  const hadEntry = history.state && (history.state.searching || history.state.search);
  setSearchMode(false);
  searchQuery = '';
  activeTag = '';
  activePerson = null;
  activeFavorites = false;
  activePool = null;
  activeVariant = null;
  activeSubjectLabel = '';
  syncSubjectChip();
  renderVariantStrip();
  searchGeneration++;          // a page still in flight belongs to nobody now
  searchLoading = false;
  searchItems = [];
  searchResultCount = 0;
  searchTotal = null;
  searchNote();          // a "*" note goes with the search it described
  if (!fromPopstate && !keepEntry && hadEntry) history.back();
}

/* The next page used to load ONLY on a scroll event inside the results box -
   and a page that fits without a scrollbar never produces one, so the results
   stopped at the first 40 however many matched (975 for one character pool).
   A wider, taller box made that likely. Keep loading until the box can
   scroll or there is nothing more; never while the box is hidden, where it
   would quietly pull every page. */
function fillSearchResults() {
  const box = document.getElementById('searchResultsScroll');
  if (!box || !searchHasMore || searchLoading || box.offsetParent === null) return;
  if (box.scrollTop + box.clientHeight >= box.scrollHeight - 150) loadMoreSearchResults();
}

async function loadMoreSearchResults() {
  if (searchLoading || !searchHasMore || (!searchQuery && !hasSubject())) return;
  searchLoading = true;
  const generation = searchGeneration;

  const loadingEl = document.getElementById('searchResultsLoading');
  const endEl = document.getElementById('searchResultsEnd');
  const emptyEl = document.getElementById('searchResultsEmpty');
  loadingEl.style.display = 'block';
  endEl.style.display = 'none';

  let res;
  try {
    const ff = filterQuery();
    res = await fetch(`/api/media/search?q=${encodeURIComponent(searchQuery)}&mode=${encodeURIComponent(searchMode)}${activeTag ? '&tag=' + encodeURIComponent(activeTag) : ''}${activePerson ? '&person=' + encodeURIComponent(activePerson) : ''}${activeFavorites ? '&favorites=1' : ''}${hasSubject() ? '&narrow=1' : ''}&offset=${searchOffset}&limit=40` + (ff ? '&' + ff : ''));
  } catch (e) {
    if (generation !== searchGeneration) return;
    loadingEl.style.display = 'none';
    searchLoading = false;
    return;
  }
  // A newer search owns the grid, the counters and searchLoading now.
  if (generation !== searchGeneration) return;

  if (res.status === 401) {
    window.location.reload();
    return;
  }
  if (!res.ok) {
    loadingEl.style.display = 'none';
    searchLoading = false;
    return;
  }

  const data = await res.json();
  if (generation !== searchGeneration) return;
  if (typeof data.total === 'number') searchTotal = data.total;
  if (data.everything) searchNote();
  const grid = document.getElementById('searchResultsGrid');
  for (const f of data.items) {
    searchResultCount++;
    if (f.is_dir) {
      // A folder is a result, but NOT a preview item: the arrow keys walk
      // from one image to the next and a folder is not something to show.
      grid.appendChild(createSearchFolderTile(f));
      continue;
    }
    searchItems.push(f);
    // Folders rank first, so the first file ends their block.
    if (grid.lastElementChild && grid.lastElementChild.classList.contains('media-folder')) {
      grid.appendChild(galleryBreak());
    }
    grid.appendChild(createFileTile(f, searchItems));
  }
  scheduleGalleryLayout(grid);

  searchOffset += data.items.length;
  searchHasMore = !!data.has_more;
  searchLoading = false;
  loadingEl.style.display = 'none';
  // Deferred, not requestAnimationFrame: a background tab never runs frame
  // callbacks, and reading scrollHeight forces the layout anyway.
  setTimeout(fillSearchResults, 0);

  if (searchResultCount === 0) {
    emptyEl.style.display = 'block';
    // Two ways a search is blind to music without saying so: a search type
    // that reads the picture index (audio has no tags or captions), and a
    // size filter (audio has no pixels). Name whichever applies.
    let hint = emptyEl.querySelector('.search-empty-hint');
    if (!hint) {
      hint = document.createElement('div');
      hint.className = 'search-empty-hint media-search-note';
      emptyEl.appendChild(hint);
    }
    const lines = [];
    if (searchMode !== 'filename' && searchMode !== 'all' && !activeTag && !activePerson
        && !isEverythingQuery(searchQuery)) {
      lines.push('This search type only reads what the picture models saw, so music never shows up here - set the search type to File name to find songs.');
    }
    if (/(^|&)(min_hres|min_vres|min_res|orientation)=/.test(filterQuery() || '')) {
      lines.push('Size and orientation filters leave out music, which has no pixels.');
    }
    if (/(^|&)(min_fps|max_fps)=/.test(filterQuery() || '')) {
      lines.push('Frame rate filters show videos only - pictures, GIFs and music have no frame rate on record.');
    }
    hint.textContent = lines.join(' ');
    hint.hidden = !lines.length;
  } else if (!searchHasMore) {
    endEl.style.display = 'block';
  }
}

/* What this mode can currently answer over.

   A mode whose index is empty is still offered - it will fill in - but saying
   "0 of 480,354 files" is the difference between "nothing matched" and "this
   has not been looked at yet", which otherwise look identical. */
/* ---------------------------------------------------------------------------
   Characters
   ---------------------------------------------------------------------------
   A pool is a tag, not a folder: the same character appears across sources and
   accounts, which is exactly what a folder cannot express. Clicking one runs
   an EXACT tag search rather than a text search, because "aqua (konosuba)" as
   free text would also match every caption mentioning water.
   ------------------------------------------------------------------------ */


/* ---------------------------------------------------------------------------
   Browse views, and the Back button
   ---------------------------------------------------------------------------
   Characters, People and Review are places you can BE, so each gets a history
   entry and a ?view= in the URL - Back leaves the view and returns to the
   folder underneath, exactly as leaving a search does, and the URL reopens
   what it names.

   One switch for all three rather than an open/close pair each: with three
   cards and a folder listing there are twelve wrong combinations to get into
   and only four right ones, and a single function can only ever produce the
   four.
   ------------------------------------------------------------------------ */

const BROWSE_VIEWS = {
  characters: 'charactersCard',
  people: 'peopleCard',
  review: 'reviewCard',
  training: 'trainingCard',
  check: 'checkCard',
};

let currentView = null;

function renderBrowseView(view) {
  for (const [name, id] of Object.entries(BROWSE_VIEWS)) {
    const el = document.getElementById(id);
    if (el) el.hidden = name !== view;
  }
  const sections = document.getElementById('browseSections');
  if (sections) sections.hidden = !!view;
  currentView = view || null;
}

/* push: false when the caller is REACTING to history (popstate, first load)
   rather than causing it. Pushing there would add an entry for a navigation
   the browser has already performed - the classic way Back stops working. */
function setBrowseView(view, { push = true } = {}) {
  if (view === currentView) return Promise.resolve();
  // Read BEFORE rendering: renderBrowseView sets currentView, so asking it
  // afterwards always says "we were already in a view" and every entry became
  // a replace - which is precisely a Back button that skips the view.
  const cameFromFolder = currentView === null;
  renderBrowseView(view);
  if (push) {
    const state = { mediaPath: currentMediaPath, mediaRoot: currentMediaRoot,
                    view: view || undefined };
    const url = mediaUrlFor(currentMediaPath, currentMediaRoot, '', view);
    // Moving BETWEEN views replaces: three sideways steps should not cost
    // three Back presses to undo. Entering one from the folder pushes.
    if (cameFromFolder && view) history.pushState(state, '', url);
    else history.replaceState(state, '', url);
  }
  // Deck mode is applied AFTER the grid exists, because it decorates the
  // tiles; and on the way OUT as well, so the fixed full-screen card can never
  // be left covering the folder listing.
  const andDeck = (p) => Promise.resolve(p).then(syncDeckMode, (err) => {
    syncDeckMode();
    throw err;
  });
  if (view === 'characters') return loadCharacters();
  if (view === 'people') return openPeople();
  if (view === 'review') return andDeck(loadReview());
  if (view === 'training') return andDeck(loadTraining());
  if (view === 'check') return loadCheck();
  syncDeckMode();
  return Promise.resolve();
}

// Leaving a view is the same gesture as pressing Back, so it pops that entry
// rather than pushing a second one (cf. exitSearchMode).
function closeBrowseView() {
  if (history.state && history.state.view) {
    history.back();          // popstate renders the folder
    return;
  }
  setBrowseView(null);
}

function openCharacters() { return setBrowseView('characters'); }

// The fetch and the render, with the showing/hiding left to the switch.
const CHARACTER_PAGE = 1000;
// The list only changes as the tag pass finds new characters, which is slow -
// but it is not never, and a cache with no expiry is why the count could sit
// wrong for an afternoon without anyone being able to refresh it.
const CHARACTER_CACHE_MS = 60000;
let characterPoolsAt = 0;
let characterTotal = 0;

let charactersPager = null;
async function loadCharacters() {
  if (!charactersPager) charactersPager = new DiscoveryGrid({
    kind:'characters', grid:document.getElementById('charactersGrid'), search:document.getElementById('charactersSearch'),
    onPage:(rows,append,state)=>{characterPools=state.items;renderCharacters(rows,append);},
    onState:(message,state)=>{
      document.getElementById('charactersCount').textContent=message || `${state.items.length.toLocaleString()} of ${state.total.toLocaleString()} characters`;
      document.getElementById('charactersEmpty').hidden=!!message || state.items.length>0;
      document.getElementById('charactersEmpty').textContent='No characters match this search.';
    }
  });
  return charactersPager.open();
}

function closeCharacters() { closeBrowseView(); }

function renderCharacters(items, append = false) {
  const grid = document.getElementById('charactersGrid');
  if (!append) grid.innerHTML = '';
  for (const c of items) {
    const tile = document.createElement('div');
    tile.className = 'character-tile';
    tile.title = c.variants.length
      ? c.variants.map(v => v.tag).join('\n') : c.tag;

    const art = document.createElement('div');
    art.className = 'character-cover';
    if (c.cover) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = '';
      // Same URL shape as any other thumbnail, so it hits the same cache.
      img.src = thumbnailUrlFor(c.cover);
      img.addEventListener('error', () => { art.textContent = '🎭'; }, { once: true });
      art.appendChild(img);
    } else {
      art.textContent = '🎭';
    }

    const name = document.createElement('div');
    name.className = 'character-name';
    // The label arrives without the series: the server strips it, because
    // doing it here by string surgery is what printed "avenger) (fate" under
    // a name it had failed to clean.
    name.textContent = c.label;

    const meta = document.createElement('div');
    meta.className = 'character-meta';
    meta.textContent = c.series ? `${c.series} · ${c.count}` : String(c.count);

    tile.append(art, name, meta);
    if (c.variants.length) {
      // Otherwise the merge is invisible: the tile silently stands for six
      // tags and its count matches none of them.
      const extra = document.createElement('div');
      extra.className = 'character-variants';
      extra.textContent = `${c.variants.length} variants`;
      tile.appendChild(extra);
    }
    tile.addEventListener('click', () => browseCharacter(c));
    grid.appendChild(tile);
  }
}

/* One character, all her spellings.

   Clicking a tile browses the WHOLE pool - every costume tag at once - and
   puts a strip of circles above the results to narrow it to one. That is the
   only place the individual tags are still visible, which is the point: the
   grid should show a person once, and the person's outfits belong inside her
   rather than beside her. */
function browseCharacter(pool, variantTag = null) {
  // A circle in the strip moves within the pool on screen, so its words stay.
  const samePool = !!(searchModeActive && activePool && activePool.tag === pool.tag);
  activePool = pool;
  activeVariant = variantTag;
  const tags = variantTag ? [variantTag]
    : (pool.variants.length ? pool.variants.map(v => v.tag) : [pool.tag]);
  const variant = variantTag && pool.variants.find(v => v.tag === variantTag);
  const label = variant && variant.label !== 'Default'
    ? `${pool.label} (${variant.label})` : pool.label;
  return browseTag(tags, label, { keepWords: samePool });
}

/* The circles. Rebuilt from activePool on every search render, so a strip
   restored by the Back button is built by the same code as a fresh one. */
function renderVariantStrip() {
  // Rebuilt on every search render, exit and Back - the same moments the
  // share button has to follow what is being browsed.
  syncShareQueryButton();
  const strip = document.getElementById('characterVariants');
  const title = document.getElementById('searchResultsTitle');
  if (!strip) return;
  strip.innerHTML = '';
  const pool = activePool;
  if (!pool || !pool.variants.length) {
    strip.hidden = true;
    if (title) title.textContent = 'Search Results';
    return;
  }
  if (title) {
    title.textContent = pool.series ? `${pool.label} · ${pool.series}` : pool.label;
  }
  const entries = [{ tag: null, label: 'All', count: pool.count, cover: pool.cover }]
    .concat(pool.variants);
  for (const v of entries) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'variant-chip';
    chip.title = v.tag || pool.variants.map(x => x.tag).join('\n');
    // aria-pressed is the selected state AND what the CSS rings, so the two
    // can never disagree.
    chip.setAttribute('aria-pressed', String((v.tag || null) === activeVariant));

    const art = document.createElement('div');
    art.className = 'variant-chip-cover';
    if (v.cover) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = '';
      img.src = thumbnailUrlFor(v.cover);
      img.addEventListener('error', () => { art.textContent = '🎭'; }, { once: true });
      art.appendChild(img);
    } else {
      art.textContent = '🎭';
    }

    const name = document.createElement('div');
    name.className = 'variant-chip-name';
    name.textContent = v.label;

    const count = document.createElement('div');
    count.className = 'variant-chip-count';
    count.textContent = v.count.toLocaleString('en-GB');

    chip.append(art, name, count);
    chip.addEventListener('click', () => browseCharacter(pool, v.tag || null));
    strip.appendChild(chip);
  }
  strip.hidden = false;
}


/* ---------------------------------------------------------------------------
   People
   ---------------------------------------------------------------------------
   A person here is a CLUSTER of face embeddings, not a name someone typed, so
   the grid shows the machine's guess and every tile has to be correctable -
   hence rename and merge on the tile itself rather than in a settings page.

   Merging is one-directional and deliberately blunt: you pick a tile, then
   pick the tile it really is. Two halves of one person is the failure this
   library actually produces; a cluster holding two people is rarer and shows
   up in the purity check instead.
   ------------------------------------------------------------------------ */

let peopleList = null;
let mergeSource = null;      // cluster id awaiting its target, or null
let peopleHiddenTotal = 0;

let peoplePager = null;
async function openPeople({ reload = false } = {}) {
  if (!peoplePager) peoplePager = new DiscoveryGrid({
    kind:'people', grid:document.getElementById('peopleGrid'), search:document.getElementById('peopleSearch'),
    params:()=>({show:document.getElementById('peopleShow').value,named:document.getElementById('peopleNamedOnly').checked?'1':'0'}),
    onPage:(rows,append,state)=>{peopleList=state.items;renderPeople(rows,append);},
    onState:(message,state)=>{
      peopleHiddenTotal=state.hiddenTotal||0;
      document.getElementById('peopleCount').textContent=message || `${state.items.length.toLocaleString()} of ${state.total.toLocaleString()} grouped`+(peopleHiddenTotal?` · ${peopleHiddenTotal} hidden`:'');
      document.getElementById('peopleEmpty').hidden=!!message || state.items.length>0;
      document.getElementById('peopleEmpty').textContent='No recognized people match this search.';
    }
  });
  return peoplePager.open(reload);
}

function closePeople() {
  cancelMerge();
  closeBrowseView();
}

function cancelMerge() {
  mergeSource = null;
  document.querySelectorAll('.person-tile.is-merge-source')
    .forEach((el) => el.classList.remove('is-merge-source'));
  const grid = document.getElementById('peopleGrid');
  if (grid) grid.classList.remove('is-merging');
}

// Escape backs out of a half-finished merge before it backs out of anything
// else - the grid is showing a pending choice, and that is what it cancels.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && mergeSource !== null) {
    e.stopPropagation();
    cancelMerge();
  }
}, true);

function renderPeople(items, append = false) {
  const grid = document.getElementById('peopleGrid');
  if (!append) grid.innerHTML = '';
  for (const person of items) grid.appendChild(personTile(person));
}

function personTile(person) {
  const tile = document.createElement('div');
  tile.className = 'person-tile';
  tile.tabIndex = 0; tile.setAttribute('role', 'button');
  tile.setAttribute('aria-label', `Find media with ${person.name || person.auto_name || 'Person ' + person.id}`);
  tile.addEventListener('keydown', ev => { if (ev.target === tile && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); tile.click(); } });
  tile.dataset.id = person.id;

  const art = document.createElement('div');
  art.className = 'person-cover';
  if (person.face_id) {
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = '';
    // The server crops to the face box; a full thumbnail would show the
    // picture, and a grid of pictures is not a grid of people.
    img.src = '/api/media/face/' + person.face_id + '/thumb';
    img.addEventListener('error', () => { art.textContent = '\u{1F464}'; }, { once: true });
    art.appendChild(img);
  } else {
    art.textContent = '\u{1F464}';
  }

  const name = document.createElement('div');
  name.className = 'person-name';
  // Three states, and they must not look alike: a name the user typed, a
  // name the library worked out from where the pictures came from, and no
  // name at all. A guess shown as fact is worse than no guess.
  if (person.name) {
    name.textContent = person.name;
  } else if (person.auto_name) {
    // Every account whose main face this is (media_faces.account_main_faces):
    // the tile shows as many as fit on its line, the tooltip lists them all.
    name.textContent = person.auto_name;
    name.classList.add('is-auto');
    const accounts = person.auto_accounts || [];
    name.title = (accounts.length === 1 ? 'The main face of this account:\n'
      : `The main face of these ${accounts.length} accounts:\n`)
      + accounts.map((a) => `${a.handle} — ${a.files} of its ${a.account_files} `
        + 'pictures with a face show this one').join('\n')
      + `\n${Math.round(person.auto_share * 100)}% of this face's pictures come from `
      + (accounts.length === 1 ? 'it' : 'them') + ' — click Name to confirm or correct it';
  } else {
    name.textContent = 'Unnamed';
    name.classList.add('is-unnamed');
  }

  const meta = document.createElement('div');
  meta.className = 'person-meta';
  meta.textContent = person.count.toLocaleString('en-GB') + ' photos';

  const tools = document.createElement('div');
  tools.className = 'person-tools';
  const rename = document.createElement('button');
  rename.type = 'button';
  rename.className = 'btn btn-quiet';
  rename.textContent = person.name ? 'Rename' : person.auto_name ? 'Confirm' : 'Name';
  rename.addEventListener('click', (ev) => { ev.stopPropagation(); namePerson(person); });
  const merge = document.createElement('button');
  merge.type = 'button';
  merge.className = 'btn btn-quiet';
  merge.textContent = 'Merge';
  merge.title = 'This is the same person as another tile';
  merge.addEventListener('click', (ev) => { ev.stopPropagation(); beginMerge(person, tile); });
  const hide = document.createElement('button');
  hide.type = 'button';
  hide.className = 'btn btn-quiet';
  // The label says what the click DOES, not what the state is.
  hide.textContent = document.getElementById('peopleShow').value === 'hidden'
    ? 'Unhide' : 'Hide';
  hide.title = 'Keep this face out of the grid - the files are untouched';
  hide.addEventListener('click', (ev) => { ev.stopPropagation(); hidePerson(person, tile); });
  tools.append(rename, merge, hide);

  tile.append(art, name, meta, tools);
  tile.addEventListener('click', () => {
    if (mergeSource !== null && mergeSource !== person.id) return finishMerge(person);
    if (mergeSource === person.id) return cancelMerge();
    return browsePerson(person);
  });
  return tile;
}

async function namePerson(person) {
  // The guess is offered as the default, so confirming it is one keypress.
  const next = prompt('Who is this?', person.name || person.auto_name || '');
  if (next === null) return;                    // cancelled, not cleared
  const res = await fetch('/api/media/people/' + person.id, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: next.trim() }),
  });
  if (!res.ok) { alert('Could not save that name.'); return; }
  person.name = next.trim() || null;
  await openPeople({ reload: true });
}

async function hidePerson(person, tile) {
  const unhiding = document.getElementById('peopleShow').value === 'hidden';
  const res = await fetch('/api/media/people/' + person.id, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hidden: !unhiding }),
  });
  if (!res.ok) { alert('Could not change that.'); return; }
  // Taken out of the DOM rather than reloading: hiding is done in runs of
  // several and a reload would reshuffle the grid under the pointer.
  tile.remove();
  peopleList = peopleList.filter((p) => p.id !== person.id);
  if (peoplePager) { peoplePager.items=peopleList;peoplePager.offset=Math.max(0,peoplePager.offset-1);peoplePager.total=Math.max(0,peoplePager.total-1);peoplePager.schedule(); }
  peopleHiddenTotal = Math.max(0, peopleHiddenTotal + (unhiding ? -1 : 1));
  document.getElementById('peopleCount').textContent =
    peopleList.length.toLocaleString('en-GB') + ' grouped'
    + (peopleHiddenTotal ? ` · ${peopleHiddenTotal} hidden` : '');
  // renderPeople applies the name filter and its empty state.
}

function beginMerge(person, tile) {
  if (mergeSource === person.id) { cancelMerge(); return; }
  cancelMerge();
  mergeSource = person.id;
  tile.classList.add('is-merge-source');
  // The grid itself says what it is waiting for; a modal here would hide the
  // very tiles you have to choose between.
  document.getElementById('peopleGrid').classList.add('is-merging');
}

async function finishMerge(target) {
  const source = mergeSource;
  cancelMerge();
  const src = peopleList.find((p) => p.id === source);
  const label = (n) => n.name || (n.count + ' photos');
  if (!confirm('Merge "' + label(src) + '" into "' + label(target) + '"?')) return;
  const res = await fetch('/api/media/people/' + source, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ merge_into: target.id }),
  });
  if (!res.ok) { alert('Could not merge those.'); return; }
  // Refetched rather than patched locally: the merge moves face counts and
  // may hand the target a name, and guessing at that here would drift.
  await openPeople({ reload: true });
}

/* Rebuild activePool from a tag list that arrived in a URL rather than from a
   click - a reload, a shared link, or a Back press out of a costume.

   The pool is fetched by its key rather than kept in the history entry: a
   history state is cloned into every entry that follows it, and a pool with
   its covers is far too much to carry around for the sake of eight circles. */
async function restorePool(poolTag, tags) {
  if (!poolTag) {
    activePool = null;
    activeVariant = null;
    return;
  }
  if (!activePool || activePool.tag !== poolTag) {
    try {
      const res = await fetch(`/api/media/characters/pool?tag=${encodeURIComponent(poolTag)}`);
      activePool = res.ok ? await res.json() : null;
    } catch (e) {
      activePool = null;
    }
  }
  // One tag out of a pool that has several is a variant - including the
  // pool's own key, which is the "Default" circle. The whole list is "All".
  const list = (tags || '').split(',').filter(Boolean);
  activeVariant = activePool && activePool.variants.length && list.length === 1
    ? list[0] : null;
}


/* The viewer's own favourites (favorites=1 in search_media), through the ordinary
   search machinery like a person: filters, paging and the preview's arrow keys
   for nothing, newest favourite first. */
function browseFavorites() {
  cancelMerge();
  renderBrowseView(null);
  activeTag = null;
  activePool = null;
  activeVariant = null;
  activePerson = null;
  activeFavorites = true;
  return enterSubject('Favorites');
}

/* Their files, through the ordinary search machinery - see the person= branch
   in search_media. That buys the filters, the paging and the arrow-key walk
   through the preview for nothing. */
function browsePerson(person) {
  cancelMerge();
  renderBrowseView(null);
  // Order matters: enterSearchMode fires the request, and a subject set after
  // it arrives too late to be on the query (cf. browseTag).
  activeTag = null;
  activePool = null;
  activeVariant = null;
  activePerson = person.id;
  activeFavorites = false;
  return enterSubject(person.name || 'this person');
}


/* ---------------------------------------------------------------------------
   The review queue
   ---------------------------------------------------------------------------
   A list of PROPOSALS. Nothing on this screen has been deleted and nothing is
   deleted without a click here - see media_review.py, which only ever writes
   rows with state 'pending'.

   Each tile leads with the reason rather than the picture, because the
   question being asked is not "is this a nice photo" but "does the machine
   have a point". Delete wears the danger colour and Keep is the quiet one, so
   the irreversible button is never the one pressed by reflex.
   ------------------------------------------------------------------------ */

let reviewItems = [];
// media_ids picked out for the next action. A queue of 833 is worked in runs,
// so the unit of work is a selection, not a file.
let reviewSelection = new Set();

/* Where the queue got to, for keyset paging. Not an offset: deciding a batch
   removes those rows from `pending`, so the Nth row stops being the row it
   was and an offset would step over exactly as many suggestions as were just
   decided. */
/* THE DECK'S VERDICTS.
   The queue holds files the rules have already proposed for the trash, so
   moving past a card without saying anything CONFIRMS that proposal rather
   than deferring it - which is what makes several cards a second possible,
   because the common case costs one swipe and nothing else. Only the rare
   disagreement costs a press.

   `reviewPassed` holds cards moved past with nothing said. `reviewKept` holds
   the explicit rescues, keyed by media_id: 'keep' (the rule was wrong) or
   'keep_anyway' (the rule was right, keep the file regardless). A card in
   reviewKept is never in reviewPassed.

   Nothing here touches a file. Both are intentions, applied only when the
   person presses Apply - so the standing rule that nothing is deleted without
   a human saying so holds, and so does the weaker one that matters just as
   much: they see a COUNT before it happens. */
let reviewPassed = new Set();
let reviewKept = new Map();
/* Moved past without having been seen - the picture had not decoded, the tab
   was not visible, or it was on screen for less time than a glance takes.
   These are NOT agreements: they are never sent, they stay pending on the
   server, and the deck offers them again. Silence can only mean consent for a
   card that was actually shown. */
let reviewSkipped = new Set();

let reviewCursor = null;      // {score, id} of the last row received
let reviewHasMore = false;
let reviewPending = 0;        // how many the server says are waiting, in total
let reviewLoadingMore = false;

/* Which rule the queue is narrowed to; '' means every rule. Remembered per
   browser, so a phone that was working through sketches comes back to them. */
let reviewReason = (() => {
  try { return localStorage.getItem('mediaReviewReason') || ''; } catch (e) { return ''; }
})();

function reviewReasonQuery() {
  return reviewReason ? `&reason=${encodeURIComponent(reviewReason)}` : '';
}

function renderReasonPicker(data) {
  const sel = document.getElementById('reviewReason');
  if (!sel || !Array.isArray(data.reasons)) return;
  const total = data.reasons.reduce((n, r) => n + r.n, 0);
  const opts = [['', `All rules (${total.toLocaleString('en-GB')})`]];
  for (const r of data.reasons) {
    opts.push([r.reason, `${r.reason} (${r.n.toLocaleString('en-GB')})`]);
  }
  // A rule just worked down to zero stays selected, so the empty queue reads
  // as "done" rather than silently switching to everything.
  if (reviewReason && !data.reasons.some((r) => r.reason === reviewReason)) {
    opts.push([reviewReason, `${reviewReason} (0)`]);
  }
  sel.replaceChildren(...opts.map(([value, label]) => {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = label;
    return o;
  }));
  sel.value = reviewReason;
}

function pickReviewReason(sel) {
  const staged = reviewPassed.size + reviewKept.size;
  if (staged) {
    // Switching reloads the queue and a reload drops answers not yet applied,
    // so this refuses and says what to do instead of asking to throw them away.
    alert(`Apply or undo your ${staged} answer${staged === 1 ? '' : 's'} first.`);
    sel.value = reviewReason;
    return;
  }
  reviewReason = sel.value;
  try { localStorage.setItem('mediaReviewReason', reviewReason); } catch (e) { /* fine */ }
  loadReview();
}

async function loadReview() {
  const grid = document.getElementById('reviewGrid');
  grid.innerHTML = '<div class="media-loading">Loading…</div>';
  reviewCursor = null;
  reviewHasMore = false;
  try {
    const res = await fetch('/api/media/review?limit=200' + reviewReasonQuery());
    const data = res.ok ? await res.json() : { items: [], counts: {} };
    renderReasonPicker(data);
    reviewItems = data.items || [];
    reviewPassed = new Set();
    reviewKept = new Map();
    reviewSkipped = new Set();
    // What is waiting in THIS view: the deck counts against it.
    reviewPending = typeof data.pending_in_view === 'number'
      ? data.pending_in_view : ((data.counts || {}).pending || 0);
    reviewHasMore = !!data.has_more;
    reviewCursor = data.next_id === null || data.next_id === undefined
      ? null : { score: data.next_score, id: data.next_id };
    renderReview(data);
  } catch (e) {
    grid.innerHTML = '<div class="media-empty">Could not load the queue.</div>';
  }
}

/* The next page, APPENDED. Deliberately not a re-render: reviewSelection is
   keyed by media_id and the tiles already on screen carry their own marked
   state, so re-rendering would throw away everything picked so far - which is
   the whole point of a queue you work through in one pass. */
async function loadMoreReview() {
  if (reviewLoadingMore || !reviewHasMore || !reviewCursor) return;
  reviewLoadingMore = true;
  deckEndCard('review', 'Loading more…');
  try {
    const res = await fetch('/api/media/review?limit=200' + reviewReasonQuery()
      + `&after_score=${encodeURIComponent(reviewCursor.score)}`
      + `&after_id=${encodeURIComponent(reviewCursor.id)}`);
    const data = res.ok ? await res.json() : { items: [] };
    const fresh = data.items || [];
    const grid = document.getElementById('reviewGrid');
    const end = grid.querySelector('.deck-end');
    for (const item of fresh) {
      const tile = reviewTile(item);
      if (end) grid.insertBefore(tile, end);
      else grid.appendChild(tile);
      reviewItems.push(item);
    }
    reviewHasMore = !!data.has_more && fresh.length > 0;
    reviewCursor = data.next_id === null || data.next_id === undefined
      ? null : { score: data.next_score, id: data.next_id };
    syncReviewBar();
  } catch (e) {
    // A failed page must not look like the end of the queue.
    deckEndCard('review', 'Could not load more — scroll up and try again.');
    reviewLoadingMore = false;
    return;
  }
  reviewLoadingMore = false;
  refreshDeckEnd('review');
}

function renderReview(data) {
  const grid = document.getElementById('reviewGrid');
  grid.innerHTML = '';
  reviewSelection = new Set();
  for (const item of reviewItems) grid.appendChild(reviewTile(item));
  syncReviewBar();
  // The tiles are new nodes, so the deck's per-tile chrome has to be rebuilt
  // over them; the ANSWERS are not, because they live in the Set.
  if (deckActive) { rebuildDeckChrome('review'); }

  const counts = data.counts || {};
  document.getElementById('reviewCount').textContent =
    `${(counts.pending || 0).toLocaleString('en-GB')} to review · `
    + `${(counts.kept || 0).toLocaleString('en-GB')} kept · `
    + `${(counts.deleted || 0).toLocaleString('en-GB')} deleted`
    + reviewAccuracy(data.stats);
  document.getElementById('reviewEmpty').hidden = reviewItems.length > 0;
  setReviewBadge(counts.pending || 0);
}

/* How often a suggestion turns out to be a real mismatch - counting the ones
   kept anyway as correct, because they were. Silent until there is enough to
   be worth reading: a percentage from three decisions is noise wearing a
   number's clothes. */
function reviewAccuracy(stats) {
  if (!stats || stats.review_precision === null
      || (stats.review_precision_n || 0) < 10) return '';
  const kept = stats.kept_anyway
    ? `, ${stats.kept_anyway} kept anyway` : '';
  return ` · right ${Math.round(stats.review_precision * 100)}% of the time`
    + ` (n=${stats.review_precision_n}${kept})`;
}

function setReviewBadge(n) {
  const badge = document.getElementById('reviewBadge');
  if (!badge) return;
  badge.textContent = String(n);
  badge.hidden = !n;
}

function reviewTile(item) {
  const tile = document.createElement('div');
  tile.className = 'review-tile';
  tile.dataset.id = item.media_id;

  const shot = document.createElement('div');
  shot.className = 'review-shot';
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.alt = '';
  img.src = thumbnailUrlFor(item);
  img.addEventListener('error', () => {
    // NOT textContent: it wipes every child, including the zoom button added
    // just below - so a card whose thumbnail 404s became a card that could
    // not be inspected at all, only agreed with. Those are
    // disproportionately the odd formats a shape-based rule misflags in the
    // first place, which is exactly the set worth looking at.
    img.remove();
    tile.dataset.shotFailed = '1';
    const ph = document.createElement('div');
    ph.className = 'review-shot-failed';
    ph.textContent = '🖼';
    shot.insertBefore(ph, shot.firstChild);
  }, { once: true });
  shot.appendChild(img);
  // Selection is handled once, on the tile - the click bubbles up from the
  // picture. Binding it here AND on the tile toggled twice for every click on
  // the image, so the picture, which is the obvious target, did nothing at all.
  const zoom = document.createElement('button');
  zoom.type = 'button';
  zoom.className = 'review-zoom';
  zoom.textContent = '\u2922';
  zoom.title = 'Look at it properly';
  zoom.addEventListener('click', (ev) => {
    ev.stopPropagation();
    openMediaPreview(item, reviewItems);
  });
  shot.appendChild(zoom);

  const body = document.createElement('div');
  body.className = 'review-body';
  const reason = document.createElement('div');
  reason.className = 'review-reason';
  reason.textContent = item.reason;
  const detail = document.createElement('div');
  detail.className = 'review-detail';
  detail.textContent = item.detail || '';
  detail.title = item.detail || '';
  const where = document.createElement('div');
  where.className = 'review-where';
  where.textContent = item.profile || item.path;
  where.title = item.path;
  body.append(reason, detail, where);

  // Only the DISAGREEMENTS need a press. Swiping on agrees with the queue, so
  // these two are the exceptions - a rescue because the rule was wrong, and a
  // rescue because it was right but the file is wanted anyway. Measured
  // review-queue precision is 0.941, so roughly one card in seventeen needs
  // one of these and the other sixteen need nothing at all.
  const acts = document.createElement('div');
  acts.className = 'review-acts';
  for (const [label, verdict, title] of [
    ['Not a mismatch', 'keep',
     'The suggestion was wrong - this does belong here'],
    ['Keep anyway', 'keep_anyway',
     'The suggestion was right, but keep the file'],
  ]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-quiet';
    b.dataset.verdict = verdict;
    b.textContent = label;
    b.title = title;
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      setReviewVerdict(item, verdict, tile);
    });
    acts.appendChild(b);
  }
  // No zoom button here: .review-shot already carries one (.review-zoom), and
  // adding a second put two identical controls on every card.

  tile.append(shot, body, acts);
  tile.addEventListener('click', () => {
    // In the deck the card is not the checkbox - see the row above.
    if (deckActive) return;
    toggleReviewPick(item, tile);
  });
  return tile;
}

function toggleReviewPick(item, tile) {
  if (reviewSelection.has(item.media_id)) reviewSelection.delete(item.media_id);
  else reviewSelection.add(item.media_id);
  const picked = reviewSelection.has(item.media_id);
  tile.classList.toggle('is-picked', picked);
  if (deckActive) { deckStamp(tile, picked ? 'picked' : null, 'Marked'); }
  syncReviewBar();
}

function syncReviewBar() {
  const bar = document.getElementById('reviewBar');
  if (!bar) return;
  bar.hidden = reviewItems.length === 0;
  if (deckActive) return syncReviewDeckBar();

  const n = reviewSelection.size;
  document.getElementById('reviewSelected').textContent = n
    ? `${n} selected of ${reviewItems.length}`
    : `Pick the ones that are wrong — ${reviewItems.length} to go`;
  for (const id of ['reviewKeep', 'reviewKeepAnyway', 'reviewDelete']) {
    const b = document.getElementById(id);
    if (b) b.disabled = n === 0;
  }
  const all = document.getElementById('reviewSelectAll');
  if (all) all.textContent = n === reviewItems.length && n ? 'Clear selection' : 'Select all';
}

/* The deck's bar is a different thing from the grid's: the grid acts on a
   SELECTION, the deck acts on everything decided so far. The count is on
   screen the whole time on purpose - the number of files about to go must
   never first appear in the confirmation dialog. */
function syncReviewDeckBar() {
  const discard = reviewPassed.size;
  const kept = reviewKept.size;
  const skipped = reviewSkipped.size;
  const seen = discard + kept;
  // Same reasoning as the counter: count down the queue, not the page. "to go"
  // used to jump by 200 whenever the next page landed.
  const known = Math.max(reviewItems.length, reviewPending || 0);
  const rest = Math.max(0, known - seen - skipped);
  document.getElementById('reviewSelected').textContent = seen || skipped
    ? `${discard} to discard · ${kept} kept`
      + (skipped ? ` · ${skipped} went by too fast` : '')
      + ` · ${rest} to go`
    : `Swipe on to agree — ${reviewItems.length} loaded`;
  const all = document.getElementById('reviewSelectAll');
  if (all) { all.textContent = 'Undo all'; all.disabled = seen + skipped === 0; }
  const del = document.getElementById('reviewDelete');
  if (del) {
    del.textContent = discard ? `Apply — discard ${discard}` : 'Apply';
    del.disabled = seen === 0;
  }
  // The grid's two rescue buttons act on a selection that the deck does not
  // have; the per-card buttons replaced them.
  for (const id of ['reviewKeep', 'reviewKeepAnyway']) {
    const b = document.getElementById(id);
    if (b) b.hidden = true;
  }
}

function toggleSelectAllReview() {
  const everything = reviewSelection.size !== reviewItems.length;
  reviewSelection = new Set(everything ? reviewItems.map((i) => i.media_id) : []);
  document.querySelectorAll('#reviewGrid .review-tile:not(.deck-end)').forEach((t) => {
    t.classList.toggle('is-picked', everything);
  });
  syncReviewBar();
}

/* One question for the whole batch. Deleting is the irreversible-looking act -
   the files go to the trash, but the ban that stops them being downloaded
   again is the part worth confirming - and asking once for forty files is a
   question; asking forty times is an obstacle. */
/* Send everything decided in this pass: the agreed discards and the rescues,
   each as its own call because they are different decisions. Nothing before
   this point has touched a file. */
async function applyReviewDeck() {
  const discard = [...reviewPassed];
  const keeps = [...reviewKept].filter(([, v]) => v === 'keep').map(([id]) => id);
  const anyway = [...reviewKept].filter(([, v]) => v === 'keep_anyway').map(([id]) => id);
  if (!discard.length && !keeps.length && !anyway.length) return;

  const parts = [];
  if (discard.length) parts.push(`move ${discard.length} file(s) to the trash`);
  if (keeps.length) parts.push(`keep ${keeps.length} (the suggestion was wrong)`);
  if (anyway.length) parts.push(`keep ${anyway.length} anyway`);
  // The count is already on the bar; this repeats it because trashing is the
  // one thing here that a person would want to have been asked about.
  if (!confirm(`Apply this pass?\n\n` + parts.join('\n')
               + (discard.length
                  ? '\n\nThey go to the library trash on disk. There is no'
                    + ' restore screen yet, so getting one back needs a hand.'
                  : ''))) {
    return;
  }
  // Rescues first. If the run dies half way, the files that were SAVED are
  // already saved and the rest simply stay in the queue to be seen again -
  // the failure mode leans towards keeping things.
  if (keeps.length) await decideReview(keeps, 'keep');
  if (anyway.length) await decideReview(anyway, 'keep_anyway');
  if (discard.length) await decideReview(discard, 'delete');
  reviewPassed = new Set();
  reviewKept = new Map();
  reviewSkipped = new Set();
  // Back to the first of whatever is left - identity AND position, through
  // the one function that sets both. Rebuild the furniture first so the tile
  // list is the post-removal one.
  syncReviewBar();
  rebuildDeckChrome('review');
  const grid = document.getElementById('reviewGrid');
  const first = grid && grid.querySelector('.review-tile:not(.deck-end)');
  if (first) deckGoTo(grid, first);
  else if (grid) grid.scrollTop = 0;
}

function undoReviewDeck() {
  if (reviewPassed.size + reviewKept.size + reviewSkipped.size === 0) return;
  reviewPassed = new Set();
  reviewKept = new Map();
  reviewSkipped = new Set();
  document.querySelectorAll('#reviewGrid .review-tile').forEach((tile) => {
    tile.classList.remove('is-picked');
    tile.querySelectorAll('.review-acts button[data-verdict]').forEach((b) => {
      b.classList.remove('is-chosen');
      b.setAttribute('aria-pressed', 'false');
    });
    deckStamp(tile, null);
  });
  syncReviewBar();
}

async function applyReviewSelection(decision) {
  let ids = [...reviewSelection];
  if (!ids.length) return;
  if (decision === 'delete') {
    // An explicit rescue outranks a bulk selection. The grid's buttons act on
    // reviewSelection and know nothing about the deck's per-card verdicts, so
    // without this a file rescued on the phone and then caught by "Delete
    // selected" on the desktop would be trashed despite having been saved.
    const rescued = ids.filter((id) => reviewKept.has(id));
    if (rescued.length) {
      ids = ids.filter((id) => !reviewKept.has(id));
      if (!ids.length) {
        alert(`Those ${rescued.length} are marked to keep. Clear the keep first.`);
        return;
      }
    }
  }
  if (decision === 'delete'
      && !confirm(`Move ${ids.length} file(s) to the trash and ban them from `
                  + 'being downloaded again?')) return;
  await decideReview(ids, decision);
}

/* decision: 'keep' (the suggestion was wrong), 'keep_anyway' (it was right and
   the file is wanted regardless) or 'delete'. The two keeps look identical to
   the file and opposite to the statistics - see label_stats. */
async function decideReview(ids, decision) {
  const res = await fetch('/api/media/review/decide', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ media_ids: ids, decision, mode: 'ban' }),
  });
  if (!res.ok) { alert('That did not go through.'); return; }
  // Removed from the DOM rather than reloading the queue: a reload would
  // reflow every tile under the pointer and you would lose your place in a
  // list you are working through one item at a time.
  for (const id of ids) {
    const el = document.querySelector(`#reviewGrid .review-tile[data-id="${id}"]`);
    if (el) el.remove();
    reviewSelection.delete(id);
  }
  reviewItems = reviewItems.filter((i) => !ids.includes(i.media_id));
  document.getElementById('reviewEmpty').hidden = reviewItems.length > 0;
  syncReviewBar();
  refreshReviewCount();
}

async function refreshReviewCount() {
  try {
    const res = await fetch('/api/media/review?limit=1');
    if (!res.ok) return;
    const data = await res.json();
    const counts = data.counts || {};
    setReviewBadge(counts.pending || 0);
    document.getElementById('reviewCount').textContent =
      `${(counts.pending || 0).toLocaleString('en-GB')} to review · `
      + `${(counts.kept || 0).toLocaleString('en-GB')} kept · `
      + `${(counts.deleted || 0).toLocaleString('en-GB')} deleted`
      + reviewAccuracy(data.stats);
  } catch (e) { /* the badge is not worth an error */ }
}

async function rescanReview(btn) {
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = 'Scanning…';
  try {
    const res = await fetch('/api/media/review/scan', { method: 'POST' });
    if (!res.ok) throw new Error(res.status);
    await loadReview();
  } catch (e) {
    alert('The scan did not finish.');
  } finally {
    btn.disabled = false;
    btn.textContent = was;
  }
}

/* Profiles: what each account is expected to post. The measured shape is
   shown next to the mark so the choice is informed - and so a mark that
   contradicts the files is visible as such rather than silently overriding
   them. */
async function toggleProfiles() {
  const box = document.getElementById('reviewProfiles');
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '<div class="media-loading">Loading…</div>';
  let items = [];
  try {
    const res = await fetch('/api/media/profiles');
    if (res.ok) items = (await res.json()).items || [];
  } catch (e) { /* below */ }
  box.innerHTML = '';
  if (!items.length) {
    box.innerHTML = '<div class="media-empty">No accounts measured yet.</div>';
    return;
  }
  for (const p of items) box.appendChild(profileRow(p));
}

function profileRow(p) {
  const row = document.createElement('div');
  row.className = 'review-prof-row';

  const handle = document.createElement('div');
  handle.className = 'review-prof-handle';
  handle.textContent = p.handle;
  handle.title = `${p.source}/${p.handle}`;

  const files = document.createElement('div');
  files.className = 'review-prof-num';
  files.textContent = `${p.files} files`;

  const share = document.createElement('div');
  share.className = 'review-prof-num';
  share.textContent = `${Math.round(p.photo_share * 100)}% photo`;
  share.title = 'Share of this account\'s tagged files that look photographic';

  const sel = document.createElement('select');
  for (const [value, label] of [['', p.measured ? `auto (${p.measured})` : 'auto'],
                                ['cosplayer', 'cosplayer'],
                                ['anime', 'anime'],
                                ['mixed', 'mixed (never suggest)']]) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    sel.appendChild(opt);
  }
  sel.value = p.mark || '';

  const save = async (body) => {
    const res = await fetch(`/api/media/profiles/${encodeURIComponent(p.source)}/`
                + encodeURIComponent(p.handle), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return res.ok ? res.json() : null;
  };

  sel.addEventListener('change', async () => {
    // Only the kind is sent, so the sketch permission is left as it was.
    const out = await save({ kind: sel.value });
    p.mark = (out && out.kind) || null;
  });

  /* What this account is excused from. Some artists publish rough
     black-and-white work on purpose; some accounts exist to post captioned
     game-render memes. In each case the picture is exactly what the rule fires
     on and exactly what the account is for, and no tag separates "posted
     deliberately" from "escaped" - only a person can say, per account. */
  const exempt = new Set(p.exempt || []);
  const boxes = document.createElement('div');
  boxes.className = 'review-prof-exempt';
  for (const [key, label, why] of [
    ['sketch', 'sketches', 'Rough black-and-white work is this artist\u2019s output'],
    ['meme', 'memes', 'This account posts anime and game-render memes on purpose'
      + ' - photo memes are still suggested'],
    ['screenshot', 'screenshots', 'This account posts game captures on purpose'],
  ]) {
    const wrap = document.createElement('label');
    wrap.title = why;
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = exempt.has(key);
    box.addEventListener('change', async () => {
      box.disabled = true;
      // The whole set is sent, so the server never has to merge - and a
      // half-applied set is not a state this can end up in.
      const next = new Set(exempt);
      if (box.checked) next.add(key); else next.delete(key);
      const out = await save({ exempt: [...next] });
      box.disabled = false;
      if (!out) { box.checked = !box.checked; return; }   // put it back
      exempt.clear();
      for (const k of out.exempt || []) exempt.add(k);
      p.exempt = [...exempt];
      box.checked = exempt.has(key);
    });
    wrap.append(box, document.createTextNode(' ' + label));
    boxes.appendChild(wrap);
  }

  row.append(handle, files, share, sel, boxes);
  return row;
}

/* ---------------------------------------------------------------------------
   Deck mode - the two queues on a phone
   ---------------------------------------------------------------------------
   One card per screen, scrolled vertically. The movement is the BROWSER's:
   .review-grid.is-deck is a scroll-snap container and each tile is a snap
   point, so "next", "previous" and the inertia between them are native, and
   going back to change an answer is just scrolling up. The cards are never
   re-rendered, which is what makes that safe - a staged answer lives in the
   same Map it always did and the tile that shows it is the same node.

   Deliberately NOT a swipe-to-answer deck. A two-direction flick expresses two
   answers and the training queue has three; "?" being harder to reach than the
   other two would bias the dataset silently, which is the one failure this
   screen cannot afford. Every answer is a button, in the same place on every
   card, and the queue is moved by scrolling.
   --------------------------------------------------------------------------- */

const DECK_MAX_WIDTH = 760;
let deckActive = false;

/* A swipe has to be far enough to be meant, and quick enough to be a flick
   rather than a slow drag while reading. Below the distance nothing happens at
   all: the deck never moves part of a card, because a half-moved card is what
   makes a feed feel like it is slipping. */
const DECK_SWIPE_MIN_PX = 48;
const DECK_SWIPE_MAX_MS = 900;
// Under this, the gesture was a tap and the button underneath should get it.
const DECK_TAP_SLOP_PX = 10;
// A card has to have been on screen at least this long to count as looked at.
// A saccade plus a fixation is around 200ms; below this there was no fixation
// at all. It is a floor on the SWIPE, not a timer - nothing fires when it
// elapses, and a card that misses it is skipped rather than agreed to.
const DECK_MIN_DWELL_MS = 150;
// One card per gesture on the keyboard, matching the wheel.
const DECK_KEY_LOCK_MS = 350;

function deckShouldBeOn() {
  try {
    return window.matchMedia(`(max-width: ${DECK_MAX_WIDTH}px)`).matches;
  } catch (e) {
    return false;
  }
}

/* Turn deck mode on or off for whichever queue is open. Called on entering a
   view and on resize/orientation change; it only ever adds or removes classes
   and one floating control, so it can run as often as it likes and can never
   cost a staged answer. */
function syncDeckMode() {
  deckActive = deckShouldBeOn();
  for (const view of ['review', 'training']) {
    const card = document.getElementById(BROWSE_VIEWS[view]);
    const grid = document.getElementById(view === 'review' ? 'reviewGrid' : 'trainingGrid');
    if (!card || !grid) continue;
    const on = deckActive && currentView === view;
    card.classList.toggle('is-deck', on);
    grid.classList.toggle('is-deck', on);
    if (on) rebuildDeckChrome(view);
    else card.querySelectorAll('.deck-close, .deck-progress').forEach((el) => el.remove());
  }
}

/* The close button and the position counter. Built once per activation and
   removed when deck mode ends, rather than living in the markup hidden: they
   are meaningless outside the deck and a hidden control that a class can
   accidentally reveal is a bug this project has already had. */
function deckChrome(card, grid, view) {
  if (!card.querySelector('.deck-close')) {
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'deck-close';
    x.textContent = '\u2715';
    x.title = 'Back to files';
    x.setAttribute('aria-label', 'Close');
    // The same exit the header button uses, so leaving the deck and leaving
    // the view are one thing and the history entry is handled in one place.
    x.addEventListener('click', closeBrowseView);
    card.appendChild(x);
  }
  if (!card.querySelector('.deck-progress')) {
    const p = document.createElement('div');
    p.className = 'deck-progress';
    card.appendChild(p);
  }
  // The counter is written by deckStep, from the same node the decisions are
  // attached to. It used to come from an IntersectionObserver - a SECOND
  // answer to "which card is current", and a second answer is how the first
  // one gets to be wrong without anything on screen disagreeing.
  const tiles = [...grid.querySelectorAll('.review-tile')];
  if (!grid._deckCurrent || tiles.indexOf(grid._deckCurrent) < 0) {
    // The card it was on is gone - decided, or the queue reloaded underneath.
    grid._deckCurrent = tiles[0] || null;
    grid._deckSince = performance.now();
    deckAlign(grid);
  }
  deckPrefetch(tiles, tiles.indexOf(grid._deckCurrent));
  deckProgress(grid, tiles, Math.max(0, tiles.indexOf(grid._deckCurrent)));
}

/* Move the deck by whole cards. The ONLY thing that moves it: the gesture
   handlers below choose a direction and call this, so every movement is
   exactly one card however hard the swipe was. */
function deckStep(grid, delta) {
  // The listeners are bound once and never removed, so a deck that has been
  // switched off (a phone turned to landscape past 760px, a window widened)
  // would otherwise still be stepping and staging on every wheel notch while
  // the view is an ordinary grid.
  if (!grid.classList.contains('is-deck')) return;

  const tiles = [...grid.querySelectorAll('.review-tile')];
  if (!tiles.length) return;

  /* IDENTITY IS A NODE, NEVER AN INDEX COMPUTED FROM scrollTop.
     The old form read `Math.round(scrollTop / clientHeight)` and parked at
     `next * clientHeight`. clientHeight is an integer by specification; the
     card's real height is the fractional used height of a flex child. So the
     arithmetic stayed perfectly self-consistent - round(next*h/h) is always
     next - while the PICTURE slid a fraction of a pixel per step against the
     index, and the error accumulated with every swipe and was never
     re-anchored. Nothing on screen could contradict it, which is exactly why
     it was reported as a label bug: the words belonged to the card the code
     thought it was on, and the image belonged to the one it was actually
     showing. Past about a third of a card, a rescue press saves the wrong
     file. A node cannot drift, and it also survives a page being appended. */
  let cur = grid._deckCurrent;
  let nowIdx = cur ? tiles.indexOf(cur) : -1;
  if (nowIdx < 0) { cur = tiles[0]; nowIdx = 0; }
  const nextIdx = Math.max(0, Math.min(tiles.length - 1, nowIdx + delta));

  // Ask for the next page before the early return: sitting on the last loaded
  // card is when more is most wanted, and it is also when the step is a no-op.
  if (grid.id === 'reviewGrid' && Math.max(nowIdx, nextIdx) >= tiles.length - 3) {
    loadMoreReview();
  }
  if (nextIdx === nowIdx) return;

  // Agreement is recorded for the card being LEFT, and only going FORWARD.
  // Going back is the correcting gesture and must never add agreements.
  if (grid.id === 'reviewGrid' && delta > 0) markPassed(grid, cur);

  const target = tiles[nextIdx];
  grid._deckCurrent = target;
  grid._deckSince = performance.now();
  deckPrefetch(tiles, nextIdx);

  deckAlign(grid);
  deckProgress(grid, tiles, nextIdx);
}

/* Put the current card exactly at the top, measured from the real geometry.
   Every movement goes through here, so a rounding error can never accumulate:
   each call re-anchors on where the card actually is rather than on where a
   multiplication says it should be. No scroll-behavior in the CSS either,
   because a smooth scroll is animated by requestAnimationFrame and does not
   run in a backgrounded tab - measured, the deck stayed at 0 for ever. */
function deckAlign(grid) {
  const cur = grid && grid._deckCurrent;
  if (!cur || !grid.clientHeight) return;
  const delta = cur.getBoundingClientRect().top - grid.getBoundingClientRect().top;
  if (Math.abs(delta) > 0.5) grid.scrollTop += delta;
}

/* Move the deck to a card, identity and position together.

   Setting scrollTop on its own is not enough now that the current card is a
   NODE: after applying a pass, decideReview removes the decided tiles, and any
   card that was NOT decided - a rescue, a skip, or simply the one you were
   standing on - survives. The deck went on pointing at that surviving node
   while the scroll had been sent to the top, so the next movement jumped to
   wherever that node now sat. That is how applying landed on card 210 instead
   of card 1. */
function deckGoTo(grid, tile) {
  if (!grid || !tile) return;
  grid._deckCurrent = tile;
  grid._deckSince = performance.now();
  deckAlign(grid);
  const tiles = [...grid.querySelectorAll('.review-tile')];
  deckPrefetch(tiles, tiles.indexOf(tile));
  deckProgress(grid, tiles, Math.max(0, tiles.indexOf(tile)));
}

/* The position counter, written by the thing that moves the deck. It used to
   come from an IntersectionObserver, which is a SECOND answer to "which card
   is current" - and a second answer is how the first one gets to be wrong
   without anything noticing. */
function deckProgress(grid, tiles, index) {
  const card = grid.closest('.media-browser-card');
  const el = card && card.querySelector('.deck-progress');
  if (!el) return;
  const real = tiles.filter((t) => !t.classList.contains('deck-end'));
  const pos = real.indexOf(tiles[index]);
  /* The denominator is the QUEUE, not what happens to be loaded.

     Pages of 200 arrive as you approach the end, so a counter reading
     "400 / 400" became "400 / 600" the moment one landed. Nothing had moved
     and nothing had been staged - the total had simply grown - but a number
     that leaps by 200 mid-swipe reads as the deck having lost your place, and
     there is no way to tell it apart from a real jump by looking. The pending
     count from the server does not move while a pass is being worked. */
  const total = (grid.id === 'reviewGrid' && reviewPending > real.length)
    ? reviewPending : real.length;
  el.textContent = pos >= 0
    ? `${(pos + 1).toLocaleString('en-GB')} / ${total.toLocaleString('en-GB')}`
    : `${real.length.toLocaleString('en-GB')} done`;
}

/* Two cards ahead, so the picture is usually decoded before it is judged.
   Bounded at two: the thumbnail endpoint GENERATES thumbnails, video ones with
   ffmpeg, so an unbounded prefetch is a way to flatten the server from a
   phone. */
function deckPrefetch(tiles, index) {
  for (let i = index + 1; i <= index + 2 && i < tiles.length; i++) {
    const img = tiles[i] && tiles[i].querySelector('.review-shot img');
    if (img && img.loading === 'lazy') img.loading = 'eager';
  }
}


/* The card at `index` has been moved past with nothing said about it, so the
   proposal to discard it stands. An explicit rescue always wins - pressing a
   button and then swiping on must not undo the press. */
function markPassed(grid, tile) {
  if (!tile || tile.classList.contains('deck-end')) return;
  const id = Number(tile.dataset.id);
  if (!id || reviewKept.has(id)) return;      // an explicit rescue always wins

  /* SEEN, or it is not agreement. Three conjuncts, all synchronous and none
     depending on paint:
       - the picture actually decoded. An undecoded <img> over a dark card is
         indistinguishable from a dark photograph, and thumbnails here are
         lazy AND generated on demand, so on a cold cache a fast pass would be
         judging grey boxes.
       - the tab was visible. A held key or a stray wheel in a background tab
         is not a look.
       - it was on screen for at least a glance.
     A card failing any of them is SKIPPED: left pending, counted separately,
     and offered again. It is self-healing - swipe back, look, swipe on. */
  const img = tile.querySelector('.review-shot img');
  const decoded = tile.dataset.shotFailed !== '1'
    && !!img && img.complete && img.naturalWidth > 0;
  const visible = document.visibilityState === 'visible';
  const dwell = performance.now() - (grid._deckSince || 0);

  if (!decoded || !visible || dwell < DECK_MIN_DWELL_MS) {
    reviewPassed.delete(id);
    reviewSkipped.add(id);
    deckStamp(tile, 'skipped', 'Not looked at \u2014 still pending');
    syncReviewBar();
    return;
  }
  reviewSkipped.delete(id);
  reviewPassed.add(id);
  deckStamp(tile, 'discard', 'Discard');
  syncReviewBar();
}


/* An explicit rescue, or undoing one. Pressing the same verdict again returns
   the card to the default - which here means the discard stands, because that
   is what the queue is for. */
function setReviewVerdict(item, verdict, tile) {
  const id = item.media_id;
  if (reviewKept.get(id) === verdict) reviewKept.delete(id);
  else reviewKept.set(id, verdict);
  const chosen = reviewKept.get(id) || null;
  if (chosen) reviewPassed.delete(id);
  else reviewPassed.add(id);              // back to agreeing with the queue
  tile.querySelectorAll('.review-acts button[data-verdict]').forEach((b) => {
    const on = b.dataset.verdict === chosen;
    b.classList.toggle('is-chosen', on);
    b.setAttribute('aria-pressed', String(on));
  });
  // NOT is-picked: that is the grid's selection affordance and paints a huge
  // tick across the picture, which means "selected for the bulk action" and
  // not "this one is being discarded". In the deck the stamp says it.
  deckStamp(tile, chosen || 'discard',
            { keep: 'Not a mismatch', keep_anyway: 'Keep anyway' }[chosen] || 'Discard');
  syncReviewBar();
}

/* One swipe, one card. Bound once per grid.

   Pointer events rather than touch events so the same code answers a mouse
   drag, which is also how the harness drives it - there is no touch hardware
   in the test browser and a touch-only implementation could not be tested at
   all. */
function deckGestures(grid) {
  if (grid._deckGestures) return;
  grid._deckGestures = true;

  let startY = null, startX = 0, startT = 0, moved = false;
  let swipeCommitted = false;

  grid.addEventListener('pointerdown', (e) => {
    startY = e.clientY; startX = e.clientX; startT = e.timeStamp; moved = false;
    swipeCommitted = false;          // a new gesture, so nothing is owed
  });
  grid.addEventListener('pointermove', (e) => {
    if (startY === null) return;
    if (Math.abs(e.clientY - startY) > DECK_TAP_SLOP_PX
        || Math.abs(e.clientX - startX) > DECK_TAP_SLOP_PX) moved = true;
  });
  const finish = (e) => {
    if (startY === null) return;
    const dy = e.clientY - startY;
    const dx = e.clientX - startX;
    const dt = e.timeStamp - startT;
    startY = null;
    // Mostly sideways is not a card change - it may be the platform's back
    // gesture, and stealing it would make the deck a trap.
    if (Math.abs(dy) < DECK_SWIPE_MIN_PX || Math.abs(dy) <= Math.abs(dx)) return;
    if (dt > DECK_SWIPE_MAX_MS) return;
    // Finger up = the next card comes up from below, which is the direction
    // every feed on the phone already uses.
    deckStep(grid, dy < 0 ? 1 : -1);
    // The browser may still synthesise a click from this gesture, and the
    // answer buttons sit at the bottom of the card exactly where a thumb
    // starts a swipe. Observed on a real phone: swiping back from a card
    // pressed "Keep anyway" on the way past.
    swipeCommitted = true;
  };
  grid.addEventListener('pointerup', finish);
  grid.addEventListener('pointercancel', () => { startY = null; });

  /* A swipe that happens to begin on an answer button must not also press it.
     Capture phase, so it is decided before the button's own handler runs -
     otherwise a scroll from the bottom of a card silently records a verdict. */
  grid.addEventListener('click', (e) => {
    if (!swipeCommitted) return;
    // detail > 0 means a real pointer produced this click. A click from the
    // keyboard (Enter on a focused button) reports detail 0, so it is never
    // swallowed - which is why this is not simply time-bounded. The earlier
    // 400ms window was the bug: a phone can deliver the compatibility click
    // later than that, and then the swipe pressed the button after all.
    if (e.detail === 0) return;
    swipeCommitted = false;
    e.stopPropagation();
    e.preventDefault();
  }, true);

  // A wheel notch is the same discrete step, so the deck is usable on a
  // desktop with a narrow window and testable without synthesising touches.
  let wheelLock = 0;
  grid.addEventListener('wheel', (e) => {
    if (Math.abs(e.deltaY) < 4) return;
    if (e.timeStamp - wheelLock < 350) return;      // one card per gesture
    wheelLock = e.timeStamp;
    deckStep(grid, e.deltaY > 0 ? 1 : -1);
  }, { passive: true });

  let keyLock = 0;
  grid.addEventListener('keydown', (e) => {
    // PageDown and PageUp are gone on purpose: a key that means "page" in every
    // other context must not cast a discard vote on one card.
    const step = { ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    // A held arrow repeats at the OS rate - 25 to 30 a second - and each repeat
    // was staging a discard. Three seconds of a leaning thumb was eighty files.
    // The wheel already had this lock; the keyboard did not.
    if (e.repeat) return;
    if (e.timeStamp - keyLock < DECK_KEY_LOCK_MS) return;
    keyLock = e.timeStamp;
    deckStep(grid, step);
  });
  grid.tabIndex = 0;
}

/* Put the answer back on the card. Everything needed is already in the staging
   Map and the Set, so this is a pure redraw and can run after any render. */
/* The last thing in the deck. Without it the queue simply stops on card 200
   of 7,866 with nothing to say so, which reads as the queue being finished.
   It is a real snap point, so scrolling reaches it like any other card. */
function deckEndCard(view, message) {
  const grid = document.getElementById(view === 'review' ? 'reviewGrid' : 'trainingGrid');
  if (!grid || !grid.classList.contains('is-deck')) return null;
  let end = grid.querySelector('.deck-end');
  if (!end) {
    end = document.createElement('div');
    end.className = 'review-tile deck-end';
    grid.appendChild(end);
  }
  end.innerHTML = '';
  const text = document.createElement('div');
  text.className = 'deck-end-text';
  text.textContent = message;
  end.appendChild(text);
  return end;
}

function refreshDeckEnd(view) {
  const grid = document.getElementById(view === 'review' ? 'reviewGrid' : 'trainingGrid');
  if (!grid || !grid.classList.contains('is-deck')) return;
  if (view === 'review') {
    const seen = reviewItems.length;
    const more = reviewPending > seen;
    const decided = reviewPassed.size + reviewKept.size;
    const end = deckEndCard('review', reviewHasMore
      ? `${seen} loaded so far — keep scrolling for more`
      : more
        ? `${seen} of ${reviewPending.toLocaleString('en-GB')} shown.`
          + ' Apply this pass and the next lot will load.'
        : `That is all ${seen} of them.`);
    if (end && decided) {
      const note = document.createElement('div');
      note.className = 'deck-end-text';
      note.textContent = `${reviewPassed.size} will be discarded, `
        + `${reviewKept.size} kept. Nothing has happened yet.`;
      end.appendChild(note);
      const acts = document.createElement('div');
      acts.className = 'review-acts';
      const apply = document.createElement('button');
      apply.type = 'button';
      apply.className = 'btn review-del';
      apply.textContent = `Apply — discard ${reviewPassed.size}`;
      apply.addEventListener('click', applyReviewDeck);
      const undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'btn btn-quiet';
      undo.textContent = 'Undo all';
      undo.addEventListener('click', undoReviewDeck);
      acts.append(apply, undo);
      end.appendChild(acts);
    }
    return;
  }
  // Training samples a fixed batch, so the end of the deck is where a batch is
  // sent or swapped. Both live in the header, which deck mode hides, so they
  // are offered here instead - otherwise the deck is a dead end after 24.
  const end = deckEndCard('training', trainingAnswers.size
    ? `${trainingAnswers.size} answered of ${trainingItems.length}`
    : `${trainingItems.length} in this batch — none answered yet`);
  if (!end) return;
  const acts = document.createElement('div');
  acts.className = 'review-acts';
  const send = document.createElement('button');
  send.type = 'button';
  send.className = 'btn';
  send.textContent = 'Submit answers';
  send.disabled = !trainingAnswers.size;
  send.addEventListener('click', () => {
    const real = document.getElementById('trainingSubmit');
    if (real) submitTrainingAnswers(real);
  });
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'btn btn-quiet';
  more.textContent = 'Another batch';
  more.addEventListener('click', () => {
    if (trainingAnswers.size
        && !confirm('Discard the answers you have not submitted?')) return;
    loadTraining({ force: true });
  });
  acts.append(send, more);
  end.appendChild(acts);
}

function rebuildDeckChrome(view) {
  const card = document.getElementById(BROWSE_VIEWS[view]);
  const grid = document.getElementById(view === 'review' ? 'reviewGrid' : 'trainingGrid');
  if (!card || !grid) return;
  card.querySelectorAll('.deck-close, .deck-progress').forEach((el) => el.remove());
  const stale = grid.querySelector('.deck-end');
  if (stale) stale.remove();
  deckChrome(card, grid, view);
  deckGestures(grid);
  refreshDeckStamps();
  refreshDeckEnd(view);
  // The bar is rendered by syncReviewBar during the LOAD, which happens before
  // deck mode is switched on - so on first open it still carried the grid's
  // wording ("Pick the ones that are wrong", "Select all", "Delete selected")
  // and only corrected itself once something was pressed.
  if (view === 'review') syncReviewBar();
}

function refreshDeckStamps() {
  if (currentView === 'training') {
    document.querySelectorAll('#trainingGrid .review-tile:not(.deck-end)').forEach((tile) => {
      const verdict = trainingAnswers.get(Number(tile.dataset.id))
        || trainingAnswers.get(tile.dataset.id) || null;
      deckStamp(tile, verdict, {
        belongs: 'Fits', not_belongs: 'Does not fit', unsure: 'Unsure',
      }[verdict]);
    });
  } else if (currentView === 'review') {
    document.querySelectorAll('#reviewGrid .review-tile:not(.deck-end)').forEach((tile) => {
      const id = Number(tile.dataset.id);
      const kept = reviewKept.get(id);
      if (kept) {
        deckStamp(tile, kept,
                  { keep: 'Not a mismatch', keep_anyway: 'Keep anyway' }[kept]);
      } else if (reviewPassed.has(id)) {
        deckStamp(tile, 'discard', 'Discard');
      } else {
        deckStamp(tile, null);
      }
    });
  }
}

/* The verdict belongs WITH THE BUTTONS, not floating over the picture.

   It used to be an overlay pinned to the top-left of the image, which put it
   in the two worst possible places at once: underneath the fixed position
   counter, and - whenever the deck sat a few pixels off a card boundary -
   directly below the PREVIOUS card's buttons. The result was a lit "Keep
   anyway" with a "Discard" label seemingly attached to it, which is exactly
   what was reported. A label inside the card's own bottom block cannot be
   read as belonging to a neighbour, because the neighbour's is a screen away.

   The picture still carries the verdict as a coloured edge, which is what
   makes it glanceable while swiping; the WORDS are next to the controls they
   describe. */
function deckStamp(tile, key, text) {
  let row = tile.querySelector('.deck-verdict');
  const acts = tile.querySelector('.review-acts');
  tile.classList.remove('verdict-discard', 'verdict-keep', 'verdict-keep_anyway');
  if (!key) {
    if (row) row.remove();
    return;
  }
  if (!row) {
    row = document.createElement('div');
    row.className = 'deck-verdict';
    if (acts) tile.insertBefore(row, acts);
    else tile.appendChild(row);
  }
  row.className = `deck-verdict is-${key}`;
  row.textContent = text || '';
  tile.classList.add(`verdict-${key}`);
}

// A narrow window is as unusable for the grid as a phone is, so the layout
// decides, not the input device. Re-checked on rotation too, which fires
// resize on every mobile browser.
/* A phone's viewport changes height when the address bar shows or hides, and
   the deck is positioned at a whole multiple of the OLD height - so after the
   change it sits between two cards, showing the bottom of one and the top of
   the next. Re-snap to whichever card was current. */
window.addEventListener('resize', () => {
  if (!currentView) return;
  const grid = document.getElementById(
    currentView === 'review' ? 'reviewGrid' : 'trainingGrid');
  syncDeckMode();
  if (!grid || !grid.classList.contains('is-deck')) return;
  // The card is a NODE, so a viewport change needs no remembered index and no
  // arithmetic - just put the same node back at the top once the layout has
  // settled. Applied twice because the height reads back stale on the first
  // frame after the address bar moves, and rAF alone does not fire in a
  // hidden tab.
  deckAlign(grid);
  requestAnimationFrame(() => deckAlign(grid));
  setTimeout(() => deckAlign(grid), 120);
});


function initBrowseViews() {
  const on = (id, fn, event = 'click') => {
    const el = document.getElementById(id);
    if (el) el.addEventListener(event, fn);
  };
  on('charactersBtn', () => setBrowseView('characters'));
  on('charactersClose', closeCharacters);
  on('peopleBtn', () => setBrowseView('people'));
  on('favoritesBtn', browseFavorites);
  on('peopleClose', closePeople);
  on('reviewBtn', () => setBrowseView('review'));
  on('trainingBtn', () => setBrowseView('training'));
  on('checkBtn', () => setBrowseView('check'));
  on('checkClose', closeBrowseView);
  on('checkVerdict', loadCheck, 'change');
  on('trainingClose', closeBrowseView);
  on('trainingMoreBtn', () => {
    if (trainingAnswers.size
        && !confirm('Discard the answers you have not submitted?')) return;
    loadTraining({ force: true });
  });
  on('trainingClear', clearTrainingAnswers);
  const submitBtn = document.getElementById('trainingSubmit');
  if (submitBtn) submitBtn.addEventListener('click', () => submitTrainingAnswers(submitBtn));
  on('reviewClose', closeBrowseView);
  on('reviewProfilesBtn', toggleProfiles);
  // The grid acts on a selection; the deck acts on the pass. Same two buttons,
  // routed here rather than rebound when the mode changes - rebinding a
  // listener on a mode flip is how this project once got a control that fired
  // twice.
  on('reviewSelectAll', () => (deckActive ? undoReviewDeck() : toggleSelectAllReview()));
  on('reviewKeep', () => applyReviewSelection('keep'));
  on('reviewKeepAnyway', () => applyReviewSelection('keep_anyway'));
  on('reviewDelete', () => (deckActive ? applyReviewDeck() : applyReviewSelection('delete')));
  // Refetched rather than filtered client-side: the unnamed tail is thousands
  // of clusters long and the list is capped, so filtering what arrived would
  // show a handful of named people out of the first 500 by size.
  on('peopleNamedOnly', () => openPeople({ reload: true }), 'change');

  on('peopleShow', () => openPeople({ reload: true }), 'change');

  const scanBtn = document.getElementById('reviewScanBtn');
  if (scanBtn) scanBtn.addEventListener('click', () => rescanReview(scanBtn));
  const reasonSel = document.getElementById('reviewReason');
  if (reasonSel) reasonSel.addEventListener('change', () => pickReviewReason(reasonSel));

  // The badge is why the queue gets looked at at all, so it is filled before
  // anyone opens the card.
  refreshReviewCount();
}


/* ---------------------------------------------------------------------------
   The training queue
   ---------------------------------------------------------------------------
   A random sample of the same population the rules scan, judged WITHOUT being
   told what the rules thought. That blindness is the whole feature: a label
   cast while looking at the machine's guess is a vote on the guess, and the
   one number this queue exists to produce - how much the rules MISS - cannot
   be recovered from votes on what they already found.

   So a tile here shows the picture and the account and nothing else. No
   reason, no score, no hint that this file was or was not flagged.

   Nothing is deleted from this screen either. The sample has to stay a random
   sample of the library; deleting from it would make the next round a sample
   of whatever survived the last one.
   ------------------------------------------------------------------------ */

let trainingItems = [];
// media_id -> verdict, staged on the page and not yet sent. A slip has to be
// visible and undoable, which means the answer cannot leave until it is
// confirmed - and the tile has to stay put wearing what you chose.
let trainingAnswers = new Map();

/* force: throw away whatever is staged and fetch a new batch. Without it this
   is a no-op while answers are waiting to be submitted - anything that reloads
   the view for its own reasons must not take un-submitted work with it. */
async function loadTraining({ force = false } = {}) {
  if (!force && trainingAnswers.size) {
    // Already answered, still unsent: leave the screen exactly as it is.
    syncTrainingBar();
    return;
  }
  const grid = document.getElementById('trainingGrid');
  grid.innerHTML = '<div class="media-loading">Loading…</div>';
  try {
    const res = await fetch('/api/media/training?limit=24');
    const data = res.ok ? await res.json() : { items: [] };
    trainingItems = data.items || [];
    trainingAnswers = new Map();
    grid.innerHTML = '';
    for (const item of trainingItems) grid.appendChild(trainingTile(item));
    document.getElementById('trainingEmpty').hidden = trainingItems.length > 0;
    if (deckActive) { rebuildDeckChrome('training'); }
    renderLabelStats(data.stats);
    syncTrainingBar();
  } catch (e) {
    grid.innerHTML = '<div class="media-empty">Could not load the sample.</div>';
  }
}

function renderLabelStats(stats) {
  const el = document.getElementById('trainingStats');
  if (!el || !stats) return;
  const pct = (v) => (v === null || v === undefined ? '—' : Math.round(v * 100) + '%');
  const judged = Object.values(stats.training || {}).reduce((a, b) => a + b, 0);
  el.textContent = `${judged} judged · precision ${pct(stats.precision)}`
    + ` (n=${stats.precision_n || 0}) · recall ${pct(stats.recall)}`
    + ` (n=${stats.recall_n || 0})`;
  const c = stats.confusion || {};
  el.title = `flagged and you agreed: ${c.flagged_and_wrong || 0}\n`
    + `flagged but fine: ${c.flagged_but_fine || 0}\n`
    + `MISSED (you said it does not belong, the rules said nothing): ${c.missed || 0}\n`
    + `correctly left alone: ${c.correctly_left_alone || 0}`;
}

function trainingTile(item) {
  const tile = document.createElement('div');
  tile.className = 'review-tile';
  tile.dataset.id = item.media_id;

  const shot = document.createElement('div');
  shot.className = 'review-shot';
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.alt = '';
  img.src = thumbnailUrlFor(item);
  img.addEventListener('error', () => { shot.textContent = '🖼'; }, { once: true });
  shot.appendChild(img);
  shot.addEventListener('click', () => openMediaPreview(item, trainingItems));

  const body = document.createElement('div');
  body.className = 'review-body';
  const who = document.createElement('div');
  who.className = 'review-reason';
  who.textContent = item.profile || '(no account)';
  const ask = document.createElement('div');
  ask.className = 'review-detail';
  // Naming the account in the question rather than saying "here": "does this
  // belong here" reads as "is this the right person", which is a different
  // question with a different answer.
  ask.textContent = item.profile
    ? `Does it match the rest of ${item.profile}?`
    : 'Does this look like the rest of its folder?';
  body.append(who, ask);

  // What the rest of the account looks like. This is the reference the
  // question needs: an art account and a cosplay account both post pictures
  // of the same characters, and without seeing the account's own output there
  // is no way to answer without looking it up somewhere else.
  // Always built, even when empty: the strip is a fixed slice of the card's
  // height, so a file whose account has nothing else does not produce a
  // shorter card than its neighbours.
  const strip = document.createElement('div');
  strip.className = 'sibling-strip';
  strip.title = `Other files from ${item.profile || 'this account'}`;
  for (const sib of (item.siblings || [])) {
    const s2 = document.createElement('img');
    s2.loading = 'lazy';
    s2.alt = '';
    s2.src = thumbnailUrlFor(sib);
    s2.addEventListener('error', () => s2.remove(), { once: true });
    strip.appendChild(s2);
  }
  if (!strip.children.length) {
    const none = document.createElement('span');
    none.className = 'sibling-none';
    // Said out loud rather than left as an empty gap: with no reference this
    // file cannot really be judged, and "?" is the honest answer.
    none.textContent = 'nothing else from this account yet';
    strip.appendChild(none);
  }
  body.appendChild(strip);

  const acts = document.createElement('div');
  acts.className = 'review-acts';
  for (const [label, verdict, title] of [
    ['Fits', 'belongs',
     'Same kind of thing as the strip below - whoever is in it'],
    ['Does not fit', 'not_belongs',
     'A different kind of thing from the rest of this account: a photo among '
     + 'drawings, a drawing among photos, food, a screenshot'],
    ['?', 'unsure',
     'Cannot tell - recorded, and left out of the numbers'],
  ]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-quiet';
    b.textContent = label;
    b.title = title;
    if (verdict === 'unsure') b.style.flex = '0 0 auto';
    b.dataset.verdict = verdict;
    b.addEventListener('click', () => stageAnswer(item, verdict, tile));
    acts.appendChild(b);
  }

  tile.append(shot, body, acts);
  return tile;
}

/* Clicking an answer marks the tile. Clicking the SAME answer again clears it,
   which is how a mis-click is undone - the gesture that caused it undoes it,
   with no separate undo to find. */
function stageAnswer(item, verdict, tile) {
  if (trainingAnswers.get(item.media_id) === verdict) {
    trainingAnswers.delete(item.media_id);
  } else {
    trainingAnswers.set(item.media_id, verdict);
  }
  const chosen = trainingAnswers.get(item.media_id) || null;
  tile.classList.toggle('is-answered', chosen !== null);
  // In the deck the buttons are below the fold of the eye, not of the page:
  // the stamp is what tells you at a glance what you said last time.
  if (deckActive) {
    deckStamp(tile, chosen, {
      belongs: 'Fits', not_belongs: 'Does not fit', unsure: 'Unsure',
    }[chosen]);
  }
  tile.querySelectorAll('.review-acts button').forEach((b) => {
    const on = b.dataset.verdict === chosen;
    b.classList.toggle('is-chosen', on);
    b.setAttribute('aria-pressed', String(on));
  });
  syncTrainingBar();
  if (deckActive) refreshDeckEnd('training');
}

/* The browser's own "leave site?" prompt, which is the one people recognise,
   and the only warning that can catch a closed tab or a typed URL. Registered
   once and armed only while something is actually staged. */
window.addEventListener('beforeunload', (e) => {
  // Either queue: answers staged in training, or a review pass not yet applied.
  if (!trainingAnswers.size && !(reviewPassed.size || reviewKept.size)) return;
  e.preventDefault();
  e.returnValue = '';
});

function syncTrainingBar() {
  const bar = document.getElementById('trainingBar');
  if (!bar) return;
  const n = trainingAnswers.size;
  bar.hidden = trainingItems.length === 0;
  const left = trainingItems.length - n;
  document.getElementById('trainingStaged').textContent = n
    ? `${n} answered, ${left} still to go — nothing is saved until you submit`
    : 'Answer some, then submit them together';
  document.getElementById('trainingSubmit').disabled = n === 0;
}

function clearTrainingAnswers() {
  trainingAnswers = new Map();
  document.querySelectorAll('#trainingGrid .review-tile:not(.deck-end)').forEach((tile) => {
    tile.classList.remove('is-answered');
    tile.querySelectorAll('.review-acts button').forEach((b) => {
      b.classList.remove('is-chosen');
      b.setAttribute('aria-pressed', 'false');
    });
  });
  syncTrainingBar();
}

async function submitTrainingAnswers(btn) {
  if (!trainingAnswers.size) return;
  const decisions = [...trainingAnswers].map(([media_id, verdict]) =>
    ({ media_id, verdict }));
  const left = trainingItems.length - decisions.length;
  if (left && !confirm(`Send ${decisions.length} answer(s)?`
      + ` ${left} file(s) in this batch are unanswered and will come back later.`)) return;
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = 'Sending…';
  try {
    const res = await fetch('/api/media/training/decide', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decisions }),
    });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    renderLabelStats(data.stats);
    // Answered files are gone from the sample for good; whatever was left
    // unanswered comes back in a later batch, so a fresh one is fetched.
    trainingAnswers = new Map();
    await loadTraining({ force: true });
  } catch (e) {
    alert('Those did not go through — nothing was saved.');
  } finally {
    btn.disabled = false;
    btn.textContent = was;
  }
}


/* ---------------------------------------------------------------------------
   Checking your own answers
   ---------------------------------------------------------------------------
   The consistency checks found 9 suspects in 610 answers, which is a result
   rather than a shortfall - there were no same-post contradictions and no
   near-identical pictures answered both ways. So this view is not only a
   suspicion list: it shows the answers back, with the same account strip they
   were given against, and puts anything flagged at the top.

   "Does not fit" is the default filter because those are the answers that
   would drive a deletion; a slip there costs a file, a slip the other way
   costs only a statistic.
   ------------------------------------------------------------------------ */

let checkItems = [];

const VERDICT_LABEL = {
  belongs: 'Fits', not_belongs: 'Does not fit', unsure: 'Not sure',
};

async function loadCheck() {
  const grid = document.getElementById('checkGrid');
  grid.innerHTML = '<div class="media-loading">Loading…</div>';
  const verdict = document.getElementById('checkVerdict').value;
  // The empty state says which list is empty, not a generic nothing.
  document.getElementById('checkEmpty').textContent = verdict === 'disagree'
    ? 'You and the tagger agreed on everything you have answered.'
    : 'Nothing answered that way yet.';
  try {
    const res = await fetch(`/api/media/labels/mine?verdict=${encodeURIComponent(verdict)}&limit=120`);
    const data = res.ok ? await res.json() : { items: [], counts: {} };
    checkItems = data.items || [];
    grid.innerHTML = '';
    for (const item of checkItems) grid.appendChild(checkTile(item));
    document.getElementById('checkEmpty').hidden = checkItems.length > 0;
    const c = data.counts || {};
    // In the disagreement view the interesting numbers are the two directions
    // of disagreement, not the tally of answers.
    document.getElementById('checkCount').textContent = verdict === 'disagree'
      ? `${data.missed || 0} the tagger missed · `
        + `${data.kept_over_rule || 0} it flagged that you kept`
      : `${(c.not_belongs || 0)} does not fit · ${(c.belongs || 0)} fits · `
        + `${(c.unsure || 0)} not sure · ${data.suspect_total || 0} flagged as inconsistent`;
  } catch (e) {
    grid.innerHTML = '<div class="media-empty">Could not load your answers.</div>';
  }
}

function checkTile(item) {
  const tile = document.createElement('div');
  tile.className = 'review-tile';
  // Two different signals, and they must not share a colour. The amber outline
  // means "the consistency checks queried this ANSWER"; in the disagreement
  // view that is irrelevant, and showing it there just marked five tiles for a
  // reason that had nothing to do with why the list was on screen.
  if (item.disagreement) {
    tile.classList.add(item.rule_said ? 'is-rule-wrong' : 'is-rule-blind');
  } else if (item.suspect) {
    tile.classList.add('is-suspect');
  }
  tile.dataset.id = item.label_id;

  const shot = document.createElement('div');
  shot.className = 'review-shot';
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.alt = '';
  img.src = thumbnailUrlFor(item);
  img.addEventListener('error', () => { shot.textContent = '🖼'; }, { once: true });
  shot.appendChild(img);
  shot.addEventListener('click', () => openMediaPreview(item, checkItems));

  const body = document.createElement('div');
  body.className = 'review-body';
  const said = document.createElement('div');
  said.className = 'review-reason';
  // In the disagreement view the heading is the direction, because that is
  // what the tile is FOR; what you answered is already implied by it.
  said.textContent = item.disagreement
    ? (item.rule_said ? 'The tagger flagged this — you kept it'
                      : 'The tagger missed this')
    : `You said: ${VERDICT_LABEL[item.verdict] || item.verdict}`;
  const why = document.createElement('div');
  why.className = 'review-detail';
  // In order: what the tagger thought (the point of the disagreement view),
  // then any inconsistency, then just the account.
  why.textContent = item.rule_said
    ? `it said: ${item.rule_said}`
    : (item.disagreement ? (item.profile || '') : (item.suspect || item.profile || ''));
  why.title = item.disagreement || item.suspect || item.path;
  if (item.suspect && !item.disagreement) why.classList.add('is-suspect-note');
  if (item.disagreement) why.classList.add('is-disagree-note');
  body.append(said, why);

  const strip = document.createElement('div');
  strip.className = 'sibling-strip';
  strip.title = `Other files from ${item.profile || 'this account'}`;
  for (const sib of (item.siblings || [])) {
    const s2 = document.createElement('img');
    s2.loading = 'lazy';
    s2.alt = '';
    s2.src = thumbnailUrlFor(sib);
    s2.addEventListener('error', () => s2.remove(), { once: true });
    strip.appendChild(s2);
  }
  if (!strip.children.length) {
    const none = document.createElement('span');
    none.className = 'sibling-none';
    none.textContent = 'nothing else from this account';
    strip.appendChild(none);
  }
  body.appendChild(strip);

  const acts = document.createElement('div');
  acts.className = 'review-acts';
  for (const [label, verdict] of [['Fits', 'belongs'],
                                  ['Does not fit', 'not_belongs'],
                                  ['?', 'unsure']]) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-quiet';
    b.textContent = label;
    if (verdict === item.verdict) {
      b.classList.add('is-chosen');
      b.title = 'This is what you answered';
    }
    b.addEventListener('click', () => changeLabel(item, verdict, tile));
    acts.appendChild(b);
  }

  tile.append(shot, body, acts);
  return tile;
}

async function changeLabel(item, verdict, tile) {
  if (verdict === item.verdict) return;      // already what it says
  const res = await fetch(`/api/media/labels/${item.label_id}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ verdict }),
  });
  if (!res.ok) { alert('Could not change that answer.'); return; }
  item.verdict = verdict;
  // Rewritten in place rather than reloaded: the list is being read top to
  // bottom and a reload would move everything under the pointer.
  tile.querySelector('.review-reason').textContent =
    `You said: ${VERDICT_LABEL[verdict] || verdict}`;
  tile.querySelectorAll('.review-acts button').forEach((b) => {
    b.classList.toggle('is-chosen', b.textContent ===
      ({ belongs: 'Fits', not_belongs: 'Does not fit', unsure: '?' }[verdict]));
  });
  tile.classList.add('is-changed');
}

function browseTag(tag, label, { keepWords = false } = {}) {
  renderBrowseView(null);
  // FIRST, before anything can trigger a search: enterSearchMode below fires
  // the request, and a tag set afterwards arrives too late to be on it.
  // Several tags are one pool - see browseCharacter. No danbooru tag contains
  // a comma, so joining needs no escaping.
  activeTag = Array.isArray(tag) ? tag.join(',') : tag;
  activePerson = null;
  activeFavorites = false;
  return enterSubject(label || String(activeTag).replace(/_/g, ' '), { keepWords });
}

/* Type-ahead over the fixed tag vocabulary. Only offered in character mode:
   in filename mode the useful completions are folder names, which the box
   already searches directly. */
function hideSuggest() {
  const box = document.getElementById('mediaSuggest');
  if (box) { box.hidden = true; box.innerHTML = ''; }
}

async function updateSuggest(text) {
  const box = document.getElementById('mediaSuggest');
  if (!box || searchMode !== 'character' || text.trim().length < 2) {
    hideSuggest();
    return;
  }
  let items = [];
  try {
    const res = await fetch('/api/media/characters?limit=8&q='
                            + encodeURIComponent(text.trim()));
    if (res.ok) items = (await res.json()).items;
  } catch (e) { /* a failed suggest is not worth showing an error for */ }
  if (!items.length) { hideSuggest(); return; }
  box.innerHTML = '';
  for (const c of items) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'media-suggest-row';
    row.innerHTML = '';
    const n = document.createElement('span');
    n.textContent = c.label;
    const k = document.createElement('span');
    k.className = 'media-suggest-count';
    k.textContent = c.count;
    row.append(n, k);
    row.addEventListener('click', () => {
      // A type-ahead pick is one tag, not a pool: drop any strip left over
      // from a character browsed earlier.
      activePool = null;
      activeVariant = null;
      browseTag(c.tag, c.label);
    });
    box.appendChild(row);
  }
  box.hidden = false;
}

// "*" on its own lists every file (search_media in server.py).
function isEverythingQuery(q) {
  return !!q && q.includes('*') && !q.replace(/\*/g, '').trim();
}

function searchNote() {
  const note = document.getElementById('mediaSearchNote');
  if (!note) return;
  if (searchModeActive && isEverythingQuery(searchQuery) && !hasSubject()) {
    // The search type does not narrow "*", so the index-coverage note would
    // describe a limit that is not in force. Say what is listed instead.
    const filtered = !!filterQuery();
    note.hidden = false;
    note.textContent = searchTotal === null
      ? 'Listing every file in the library.'
      : `${searchTotal.toLocaleString('en-GB')} ${searchTotal === 1 ? 'file' : 'files'}`
        + (filtered ? ' pass the filters.' : ' in the library. Add filters to narrow it down.');
    return;
  }
  if (searchMode === 'filename' || !aiCoverage) { note.hidden = true; return; }
  const total = aiCoverage.library || 0;
  // Done means the pass RAN on a file, whether or not it found anything.
  const pick = (done, result) => (typeof aiCoverage[done] === 'number' ? aiCoverage[done] : aiCoverage[result]) || 0;
  const captions = pick('caption_done', 'captioned');
  const texts = pick('ocr_done', 'ocr');
  const tags = pick('tags_done', 'tagged');
  // Context reads captions AND tags since 2026-09-14 (media_ai.SEARCH_COLUMNS).
  const have = { context: Math.max(captions, tags), ocr: texts, character: tags,
                 all: Math.max(captions, texts, tags) }[searchMode] || 0;
  const pct = total ? Math.round(have * 100 / total) : 0;
  note.hidden = false;
  note.textContent = have >= total && total
    ? 'The whole library has been indexed for this.'
    : `Indexed so far: ${have.toLocaleString('en-GB')} of `
      + `${total.toLocaleString('en-GB')} files (${pct}%). `
      + 'Anything not yet indexed cannot match.';
}

/* NOT setSearchMode: that name was already taken by the boolean that shows or
   hides the search results panel, and defining a second function with the same
   name silently replaced it - enterSearchMode calls setSearchMode(true), which
   would have landed here and set the search TYPE to "true". */
function setSearchType(mode) {
  searchMode = mode;
  searchModeFromPool = false;
  try { localStorage.setItem('mediaSearchMode', mode); } catch (e) { /* private mode */ }
  syncSearchModeChip();
  // Suggestions belong to the mode that produced them. Leaving them up after
  // switching to Filename offers completions that mode cannot use.
  hideSuggest();
  searchNote();
  if (searchQuery || hasSubject()) runSearch(searchQuery);
}

let searchTotal = null;       // the server's total for the search on screen

function runSearch(query) {
  searchQuery = query;
  searchGeneration++;
  searchTotal = null;
  renderVariantStrip();
  searchItems = [];
  searchResultCount = 0;
  searchOffset = 0;
  searchHasMore = true;
  searchLoading = false;
  document.getElementById('searchResultsGrid').innerHTML = '';
  document.getElementById('searchResultsEmpty').style.display = 'none';
  document.getElementById('searchResultsEnd').style.display = 'none';
  return loadMoreSearchResults();
}

function initSearch() {
  const input = document.getElementById('mediaSearchInput');
  const scrollEl = document.getElementById('searchResultsScroll');

  input.addEventListener('input', () => {
    clearTimeout(searchDebounceTimer);
    clearTimeout(suggestTimer);
    const q = input.value.trim();
    // Words beside a subject chip narrow it. They used to replace it, so a
    // character and "at the beach" could not be asked at once (2026-09-14);
    // the chip's x is how a subject goes now.
    suggestTimer = setTimeout(() => updateSuggest(q), 200);
    searchDebounceTimer = setTimeout(() => {
      if (!q && !hasSubject()) {
        exitSearchMode();
        return;
      }
      enterSearchMode(q);
    }, 300);
  });
  // Backspace in an empty box takes the chip away, as in any token field.
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Backspace' && !input.value && hasSubject()) {
      e.preventDefault();
      clearSubject();
    }
  });
  const subjectChip = document.getElementById('mediaSubjectChip');
  if (subjectChip) subjectChip.addEventListener('click', () => { clearSubject(); input.focus(); });

  // A click anywhere else dismisses the suggestions; Escape does too. Both in
  // the capture phase for the same reason the context menu is (see below):
  // the preview modal's own handlers would otherwise act on the same gesture.
  document.addEventListener('click', (e) => {
    const box = document.getElementById('mediaSuggest');
    if (box && !box.hidden && !box.contains(e.target) && e.target !== input) {
      hideSuggest();
    }
  }, true);
  input.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideSuggest(); });

  scrollEl.addEventListener('scroll', () => {
    const nearBottom = scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 150;
    if (nearBottom) loadMoreSearchResults();
  });
  // A taller or wider window can leave the box with nothing to scroll.
  window.addEventListener('resize', () => fillSearchResults());
  const shareQueryBtn = document.getElementById('shareQueryBtn');
  if (shareQueryBtn) shareQueryBtn.addEventListener('click', () => shareCurrentQuery(shareQueryBtn));
}

// What is on screen right now, so a re-open of the SAME item can keep the
// player it already built instead of replacing it.
let currentPreviewKey = '';

// Counts preview bodies: bumped by every rebuild and by closing. A media
// element's late callbacks (a picture's load, a video's metadata, a track's
// cover probe) check it, because the item they belong to may be gone by then:
// a video whose Smooth motion was still starting got its metadata after the
// viewer had moved on to a picture, sized the player to its own 16:9 frame,
// and the picture's upscaled copy was pinned to that - stretched (2026-09-22).
let previewBuild = 0;

// Where the folder was scrolled to when the preview was opened.
//
// Closing the preview pops history, and the popstate handler re-runs
// loadMedia(). That replaces the grid with a one-line "Loading…", so the
// document collapses to nothing, the browser clamps the scroll to 0, and the
// re-rendered folder starts at the top - which in a folder of a few thousand
// files means losing your place entirely.
let gridScrollY = 0;
// Which folder that offset belongs to. Back out of a SUBFOLDER and popstate
// fires too, so without this the parent folder would be scrolled to an offset
// measured somewhere else entirely.
let gridScrollKey = null;

function folderKey(root, path) {
  return `${root || ''} ${path || ''}`;
}

function restoreGridScroll() {
  if (gridScrollKey === null
      || gridScrollKey !== folderKey(currentMediaRoot, currentMediaPath)) {
    return;
  }
  // A shorter folder clamps this on its own, which is the right answer.
  const y = gridScrollY;
  const apply = () => window.scrollTo(0, y);
  // Three times, because one is not reliable:
  //   now      - correct whenever the grid is already laid out;
  //   on paint - the document's height is not final until it has been laid
  //              out, and scrolling before that just clamps short;
  //   on a timer - requestAnimationFrame does NOT fire in a background or
  //              hidden tab, so rAF alone means no restore at all there.
  // Scrolling to the same offset repeatedly costs nothing and is invisible.
  apply();
  requestAnimationFrame(apply);
  setTimeout(apply, 120);
}

function previewKeyFor(f) {
  return `${f.root || ''} ${f.path}`;
}

// ---------------------------------------------------------------------------
// Live upscaling is js/upscale.js, shared with the share page. Its settings are
// read up front, so the first picture opened does not wait for them.
// ---------------------------------------------------------------------------
loadUpscaleConfig();

// The settings gear is for admins; a guest would only meet a login wall.
let viewerIsAdmin = false;
fetch('/api/me')
  .then((r) => (r.ok ? r.json() : null))
  .then((me) => {
    viewerIsAdmin = !!(me && me.role === 'admin');
    const link = document.getElementById('settingsLink');
    if (link && viewerIsAdmin) link.hidden = false;
    // The admin-only 3D controls appear now; the VR button needs no role.
    const shown = activePreviewList[currentPreviewIndex];
    if (shown) paint3dButton(shown);
    if (viewerIsAdmin) loadShares();          // adds the way to Settings > Shares
  })
  .catch(() => {});

function openMediaPreview(f, previewList = currentFilesList, { push = true } = {}) {
  activePreviewList = previewList;
  currentPreviewIndex = previewList.findIndex(x => x.path === f.path);

  // Re-opening the item already showing must NOT rebuild the body. The
  // rebuild starts with clearPreviewBody(), which pauses the media and
  // strips its src, so a fresh <audio> starts at 0:00 - a track being
  // played restarts from the beginning. Several paths re-open the current
  // item without meaning to change anything: popstate (any history.back(),
  // including the one closeMediaPreview fires itself) re-opens whatever
  // state.previewFile names, which is usually the item already on screen.
  // Nothing below this point changes what is displayed, only which item is
  // "current", so it is safe to keep the existing player.
  const modalEl = document.getElementById('mediaPreviewModal');
  const alreadyShowing = modalEl.style.display === 'flex'
                         && currentPreviewKey === previewKeyFor(f);

  // Only a genuine open from the grid records where to come back to. Arrow-key
  // navigation and the popstate re-open both pass push:false, and by then the
  // page is already behind the overlay - reading the scroll there would store
  // whatever the teardown left behind.
  if (push && !alreadyShowing) {
    gridScrollY = window.scrollY;
    gridScrollKey = folderKey(currentMediaRoot, currentMediaPath);
  }

  const url = previewUrlFor(f);
  const state = {
    mediaPath: currentMediaPath,
    mediaRoot: currentMediaRoot,
    // Carried so Back from the preview returns to the search results it
    // was opened from, not to the folder behind them.
    search: searchModeActive ? searchQuery : undefined,
    ...(searchModeActive ? { searching: true, ...subjectParams() } : {}),
    previewFile: f.path,
    previewFileRoot: f.root || currentMediaRoot
  };
  // Re-opening the item already on screen must never push a second entry
  // for it, or Back would land on the same preview it just left.
  if (push && !alreadyShowing) {
    history.pushState(state, '', url);
  } else {
    history.replaceState(state, '', url);
  }

  const modal = modalEl;
  const body = document.getElementById('mediaPreviewBody');
  document.getElementById('mediaPreviewTitle').textContent = f.name;
  paintFavoriteButton(f);
  paint3dButton(f);

  // The history entry and the current-item bookkeeping above are already
  // updated; leaving the player untouched is the whole point.
  if (alreadyShowing) return;
  currentPreviewKey = previewKeyFor(f);
  const build = ++previewBuild;
  const stillShown = () => previewBuild === build;

  clearPreviewBody();
  currentPreviewEl = null;
  setPreviewBackground('');
  resetLiveUpscale();                // js/upscale.js
  followPreviewWithInfoPanel();      // player.js
  // fullscreen-media is deliberately NOT reset here - nearly every
  // preview ends up fullscreen (see applyMediaSizing), so clearing the
  // class up front just collapses the frame from 100vw/100vh down to the
  // boxed 90vw/90vh one for the split second before the next item finishes
  // loading and re-adds it, which is exactly the flicker between two
  // full-size images/videos. Only the branches below that never call
  // applyMediaSizing (no visual media to size) reset it explicitly.

  if (f.type === 'image') {
    const img = document.createElement('img');
    img.className = 'media-preview-media';
    img.alt = f.name;
    img.style.opacity = '0';
    img.addEventListener('load', () => {
      // The upscaled swap fires load again: the frame keeps the size it has,
      // or a small picture would jump to twice its size on screen.
      if (img.dataset.upscaled || !stillShown()) return;
      applyMediaSizing(img, img.naturalWidth, img.naturalHeight);
      img.style.opacity = '1';
      startLiveUpscale(img, f);
    });
    img.src = f.url;
    enableImageZoomPan(img);
    attachContextMenu(img, () => f);
    body.appendChild(img);
  } else if (f.type === 'video') {
    const video = document.createElement('video');
    video.className = 'media-preview-media';
    video.autoplay = true;
    video.loop = true;
    video.style.opacity = '0';
    video.addEventListener('loadedmetadata', () => {
      if (!stillShown()) return;       // previewBuild: the next item's size is not this one's
      applyMediaSizing(video, video.videoWidth, video.videoHeight);
      video.style.opacity = '1';
    });
    // js/interp.js plays the file or a Smooth motion stream. Checked, not assumed:
    // a browser holding an older cached media.html has no interp.js at all.
    const smooth = typeof playPreviewVideo === 'function';
    if (smooth) {
      playPreviewVideo(video, f);
    } else {
      video.src = f.url;
      startPlayback(video);
    }
    attachContextMenu(video, () => f);
    trackPreviewListening(video, f);
    body.appendChild(wrapWithCustomControls(video, { extras: smooth ? [playerSettingsButton(video)] : [] }));
  } else if (f.type === 'audio') {
    // A track with an embedded cover is played through a <video> element
    // with the art as its poster instead of <audio>, so it gets sized and
    // controlled exactly like a real video. A track with no art has no
    // poster to show, so it falls back to the plain audio-only bar.
    const thumbUrl = thumbnailUrlFor(f);
    // Built and started HERE, still inside the click that opened the
    // preview. The cover probe below is an image load, and an element
    // created in its callback has lost the user gesture the browser's
    // autoplay policy keys on. Moving a playing element into the wrapper
    // afterwards does not interrupt it, so the cover still decides the
    // layout - it just no longer decides whether the music starts.
    const audio = document.createElement('audio');
    audio.src = f.url;
    audio.autoplay = true;
    audio.loop = true;
    startPlayback(audio);
    trackPreviewListening(audio, f);

    // Until the probe answers, the track is in no container, so moving on or
    // closing (clearPreviewBody) cannot stop it: a late answer stops it here
    // instead of dropping the old track's player into the next item.
    const stale = () => {
      if (stillShown()) return false;
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
      return true;
    };
    const probe = new Image();
    probe.addEventListener('load', () => {
      if (stale()) return;
      const cover = document.createElement('img');
      cover.className = 'media-preview-media';
      cover.alt = '';
      cover.src = thumbUrl;
      applyAmbientBackgroundFrom(probe);

      applyMediaSizing(cover, probe.naturalWidth, probe.naturalHeight);
      attachContextMenu(cover, () => f);
      body.appendChild(wrapWithCustomControls(audio, { coverEl: cover }));
      // Upscaled by the same rules as a picture. Once it has its own size on
      // screen: the upscale is sized from it. (The swap fires load again, which
      // `once` and the upscaled flag both ignore.)
      if (cover.complete && cover.naturalWidth) startLiveUpscale(cover, f);
      else cover.addEventListener('load', () => startLiveUpscale(cover, f), { once: true });
    }, { once: true });
    probe.addEventListener('error', () => {
      if (stale()) return;
      modal.classList.remove('fullscreen-media');
      const wrap = wrapWithCustomControls(audio, { audioOnly: true });
      attachContextMenu(wrap, () => f);
      body.appendChild(wrap);
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

  modal.style.display = 'flex';
  setPageScrollLocked(true);
}

/* Time listened, for the music charts (js/listen_time.js, vault KSF-052): a
   song played here counts as that song, and so does its music video (user,
   2026-09-24). Which files are songs is the server's to say - the music
   catalogue knows every name of a song and every copy of its video - and a
   file it says is not one is not reported again. Checked, not assumed: a
   cached media.html from before has no listen_time.js. */
function trackPreviewListening(el, f) {
  if (typeof ListenTime !== 'object' || !f || !f.path) return;
  const item = { root: f.root || '', path: f.path };
  const id = 'file:' + JSON.stringify([item.root, item.path]);
  ListenTime.track(el, () => ({ id, item }), playbackPosition);
}

function closeMediaPreview(fromPopstate = false) {
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
  // Must be cleared, or re-opening the item that was just closed would take
  // the "already showing" shortcut above and leave an empty preview body.
  currentPreviewKey = '';
  previewBuild += 1;
  hideInfoPanel();
  setPageScrollLocked(false);

  // Closing via the X / background click / Escape should behave exactly
  // like pressing Back (pop the history entry openMediaPreview pushed),
  // so the stack stays clean and a real Back press afterward doesn't need
  // an extra press to get past a dangling "preview open" entry.
  if (wasOpen && !fromPopstate && history.state && history.state.previewFile) {
    history.back();
  }
}

function navigatePreview(delta) {
  if (currentPreviewIndex === -1) return;
  const nextIndex = currentPreviewIndex + delta;
  if (nextIndex < 0 || nextIndex >= activePreviewList.length) return;
  openMediaPreview(activePreviewList[nextIndex], activePreviewList, { push: false });
}

document.getElementById('mediaPreviewModal').addEventListener('click', (e) => {
  if (e.target.id === 'mediaPreviewModal') closeMediaPreview();
});

document.getElementById('mediaFavoriteBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  const f = activePreviewList[currentPreviewIndex];
  if (f) toggleFavorite(f);
});

// View in VR (js/vr/app.js): the VR app starts where the page is - the open item, else the main view.
for (const id of ['mediaVrBtn', 'mediaVrTopBtn']) {
  document.getElementById(id).addEventListener('mouseenter', (e) => {
    const btn = e.currentTarget;
    if (btn.getAttribute('aria-disabled') !== 'true' || btn.hidden) return;
    Stereo3d.vrSupported(true).then((ok) => { if (ok && !btn.hidden) setVrAvailable(btn, true); });
  });
}

// Click on a VR button that looks unavailable: ask again now; true when VR has become available.
async function vrRecheck(btn) {
  btn.disabled = true;
  const ok = await Stereo3d.vrSupported(true);
  btn.disabled = false;
  return ok;
}

document.getElementById('mediaVrBtn').addEventListener('click', async (e) => {
  e.stopPropagation();
  const btn = e.currentTarget;
  if (endVrFromClick() || vrStarting) return;
  // The item is resolved now, from the preview's current state, never from an earlier paint.
  const f = btn._vrFor;
  if (!f || btn.disabled || f !== activePreviewList[currentPreviewIndex]) return;
  if (btn.getAttribute('aria-disabled') === 'true') {
    const ok = await vrRecheck(btn);
    if (btn._vrFor !== f) return;
    setVrAvailable(btn, ok);
    if (!ok) { showMediaToast(Stereo3d.VR_HINT); return; }
  }
  let st = btn._vrStatus;
  if (!btn._vrKnown) {
    // The status answer has not arrived yet: ask now, so a converted item is not opened in 2D.
    btn.disabled = true;
    try { st = await Stereo3d.fetchStatus(f.root || currentMediaRoot || '', f.path); } catch (er) { st = null; }
    btn.disabled = false;
    if (btn._vrFor !== f) return;
  }
  startVrFromClick(btn, buildVrContext(f, st));
});

document.getElementById('mediaVrTopBtn').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  if (endVrFromClick() || vrStarting) return;
  if (btn.disabled) return;
  if (btn.getAttribute('aria-disabled') === 'true') {
    const ok = await vrRecheck(btn);
    setVrAvailable(btn, ok);
    if (!ok) { showMediaToast(Stereo3d.VR_HINT); return; }
  }
  startVrFromClick(btn, buildVrContext(null, null));
});

startVrWatch();
paintVrTopButton();

document.getElementById('mediaConvert3dBtn').addEventListener('click', async (e) => {
  e.stopPropagation();
  const f = activePreviewList[currentPreviewIndex];
  if (!f || !viewerIsAdmin) return;
  await convertItemTo3d(f);
  paint3dButton(f);
});

document.addEventListener('keydown', (e) => {
  const modal = document.getElementById('mediaPreviewModal');
  if (modal.style.display !== 'flex') return;

  if (e.key === 'Escape') {
    // A fullscreen <video> already owns Escape (browser exits fullscreen on
    // its own) - don't also close the modal underneath it in the same press.
    if (document.fullscreenElement) return;
    closeMediaPreview();
  } else if (e.key === 'ArrowLeft') {
    navigatePreview(-1);
  } else if (e.key === 'ArrowRight') {
    navigatePreview(1);
  }
});

// A horizontal swipe on a touch screen is the arrow keys: left brings the
// next item, right the previous one, through the same list (the folder in
// its chosen sort, or the search results). Watched in the capture phase and
// never cancelled here, so the picture's own pinch/pan (enableImageZoomPan in
// player.js, which preventDefaults and captures the pointer) keeps working;
// a swipe only counts when the picture is NOT zoomed, both when the finger
// lands and when it lifts, because a zoomed picture's drag is a pan. One
// finger only - a second one makes it a pinch. The player's own controls
// (seek bar, volume, buttons) are left alone. The mouse never swipes: a
// mouse drag already pans a zoomed picture, and the arrow keys are there.
(() => {
  const body = document.getElementById('mediaPreviewBody');
  const SWIPE_MIN_PX = 50;       // at least this far ...
  const SWIPE_MIN_FRACTION = 0.12; // ... and this share of the body's width
  const SWIPE_MAX_MS = 800;      // a swipe, not a slow drag
  let swipe = null;              // { id, x, y, t } of the finger that may swipe
  let fingers = 0;

  const zoomed = () => Array.from(body.querySelectorAll('img, video'))
    .some((el) => (el._zoomScale || 1) > 1.001);

  body.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse') return;
    // The first finger of a gesture starts the count afresh, so a lift the
    // body never saw (the preview closed mid-touch) cannot leave it stuck.
    fingers = e.isPrimary ? 1 : fingers + 1;
    swipe = fingers === 1 && !zoomed()
      && !(e.target instanceof Element && e.target.closest('input, button, select, a, .custom-controls'))
      ? { id: e.pointerId, x: e.clientX, y: e.clientY, t: performance.now() }
      : null;
  }, true);

  const lift = (e) => {
    if (e.pointerType === 'mouse') return;
    fingers = Math.max(0, fingers - 1);
    const s = swipe;
    if (!s || s.id !== e.pointerId) return;
    swipe = null;
    if (e.type !== 'pointerup' || zoomed()) return;
    const dx = e.clientX - s.x;
    const dy = e.clientY - s.y;
    const need = Math.max(SWIPE_MIN_PX, body.clientWidth * SWIPE_MIN_FRACTION);
    if (Math.abs(dx) < need || Math.abs(dx) < 2 * Math.abs(dy)) return;
    if (performance.now() - s.t > SWIPE_MAX_MS) return;
    navigatePreview(dx < 0 ? 1 : -1);
  };
  body.addEventListener('pointerup', lift, true);
  body.addEventListener('pointercancel', lift, true);
})();

// Finds a file entry (and the list it belongs to, for arrow-key nav) by its
// full relative path - checking whatever's already loaded before falling
// back to a browse call for its containing folder, so opening a link to a
// file works regardless of whether it's in the currently browsed folder,
// the recent strip, or search results.
async function resolveFileEntry(filePath, fileRoot = currentMediaRoot) {
  const sameFile = (x) => x.path === filePath && (x.root || '') === (fileRoot || '');
  for (const list of [currentFilesList, recentMediaItems, searchItems]) {
    const match = list.find(sameFile);
    if (match) return { file: match, list };
  }

  const dir = filePath.includes('/') ? filePath.slice(0, filePath.lastIndexOf('/')) : '';
  let res;
  try {
    const q = `path=${encodeURIComponent(dir)}&root=${encodeURIComponent(fileRoot || '')}`;
    res = await fetch(`/api/media/browse?${q}`);
  } catch (e) {
    return null;
  }
  if (!res.ok) return null;
  const data = await res.json();
  const match = (data.files || []).find(x => x.path === filePath);
  return match ? { file: match, list: data.files } : null;
}

window.addEventListener('popstate', async (e) => {
  const params = new URLSearchParams(window.location.search);
  const path = (e.state && e.state.mediaPath) || params.get('path') || '';
  const root = (e.state && e.state.mediaRoot) || params.get('root') || '';
  const tag = (e.state && e.state.tag) || params.get('tag') || '';
  const pool = (e.state && e.state.pool) || params.get('pool') || '';
  const subject = legacySubject({
    tag, pool, person: (e.state && e.state.person) || params.get('person') || '',
    fav: (e.state && e.state.fav) || params.get('fav') || '',
    label: (e.state && e.state.label) || params.get('label') || '',
    mode: (e.state && e.state.mode) || params.get('mode') || '',
  }, (e.state && e.state.search) || params.get('q') || '');
  const query = subject.query;
  // A subject with no words is a search too, with an empty query.
  const searching = !!(query || subject.tag || subject.person || subject.fav);
  const subjectKey = `${subject.tag}|${subject.person}|${subject.fav}`;
  const currentSubjectKey = searchModeActive
    ? `${activeTag || ''}|${activePerson ? String(activePerson) : ''}|${activeFavorites ? '1' : ''}` : '||';
  const filePath = e.state && e.state.previewFile;
  const fileRoot = (e.state && e.state.previewFileRoot) || root;
  const view = (e.state && e.state.view) || params.get('view') || null;

  // A popstate that lands on the state already displayed must change nothing.
  //
  // overlay-dismiss.js gives every non-self-managed overlay its own history
  // entry, so closing one (the pool picker, reached from the context menu)
  // pops straight back to THIS state. Rebuilding for that tore down the
  // player and restarted the track at 0:00 - the same symptom the context
  // menu used to cause, from the same cause: an unconditional teardown at
  // the top of this handler. Any overlay added later gets this for free.
  const samePlace = path === currentMediaPath && root === currentMediaRoot
      && query === (searchModeActive ? searchQuery : '') && subjectKey === currentSubjectKey
      && searching === searchModeActive && view === currentView;
  if (samePlace && filePath
      && currentPreviewKey === previewKeyFor({ root: fileRoot, path: filePath })) {
    return;
  }
  // The same, for an overlay opened and closed with no preview involved: the
  // pool picker reached from the context menu inside a browse view. Its entry
  // is popped when it closes, and rebuilding the page for that discarded the
  // answers staged on the training screen.
  if (samePlace && !filePath && !currentPreviewKey) {
    return;
  }

  // Entering or leaving a browse view changes nothing else: the folder
  // underneath is already loaded and rebuilding it here would tear down the
  // player and reset the scroll, which is the same mistake the preview and
  // the pool picker each caused once before.
  if (view !== currentView && path === currentMediaPath && root === currentMediaRoot
      && query === (searchModeActive ? searchQuery : '') && subjectKey === currentSubjectKey
      && searching === searchModeActive && !filePath) {
    await setBrowseView(view, { push: false });
    return;
  }

  // One Back press should both dismiss an open preview and perform the
  // actual folder navigation, not require a second press just to close it.
  closeMediaPreview(true);
  await loadMedia(path, false, root);

  // The folder underneath is loaded either way, so leaving the search
  // afterwards lands on it already rendered.
  const input = document.getElementById('mediaSearchInput');
  input.value = query;
  if (searching) {
    setSearchMode(true);
    // The subject has to be restored BEFORE the search runs, or the request
    // goes out without it - a different set of files (cf. browseTag).
    const subjectChanged = subjectKey !== currentSubjectKey;
    restoreSubject(subject);
    if (subject.tag) await restorePool(pool, subject.tag);
    // Re-running an identical query would throw away results already
    // scrolled through, so only refetch when it actually changed.
    if (query !== searchQuery || subjectChanged || !searchItems.length) {
      await runSearch(query);
    } else {
      renderVariantStrip();
    }
  } else {
    exitSearchMode({ fromPopstate: true });
  }

  await setBrowseView(view, { push: false });

  if (filePath) {
    const resolved = await resolveFileEntry(filePath, fileRoot);
    if (resolved) openMediaPreview(resolved.file, resolved.list, { push: false });
  }

  // Last, once every re-render above is done - a restore before runSearch() or
  // exitSearchMode() would be undone by whichever of them rebuilds the list.
  restoreGridScroll();
});

document.addEventListener('DOMContentLoaded', async () => {
  // The grid is built after the navigation, so the browser's own attempt to
  // restore a scroll position lands on an empty page and then fights the
  // restore below. Owning it outright is the only way it stays put.
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

  const params = new URLSearchParams(window.location.search);
  const initialPath = params.get('path') || '';
  const initialRoot = params.get('root') || '';
  const initialFile = params.get('file') || '';
  const initialFileRoot = params.get('fileRoot') || initialRoot;
  const initialQuery = params.get('q') || '';
  const initialView = params.get('view') || null;
  const initialTag = params.get('tag') || '';
  const initialPool = params.get('pool') || '';
  const initialSubject = legacySubject({
    tag: initialTag, pool: initialPool, person: params.get('person') || '', fav: params.get('fav') || '',
    label: params.get('label') || '', mode: params.get('mode') || '',
  }, initialQuery);
  const initialWords = initialSubject.query;
  const initialSearching = !!(initialWords || initialTag || initialSubject.person || initialSubject.fav);
  const initialSubjectParams = {
    tag: initialSubject.tag, pool: initialSubject.pool, person: initialSubject.person,
    fav: initialSubject.fav, label: initialSubject.label, mode: initialSubject.mode,
  };

  history.replaceState(
    { mediaPath: initialPath, mediaRoot: initialRoot, search: initialWords || undefined,
      view: initialView || undefined, searching: initialSearching || undefined,
      ...Object.fromEntries(Object.entries(initialSubjectParams).filter(([, v]) => v)) },
    '',
    mediaUrlFor(initialPath, initialRoot, initialWords, initialView, initialSubjectParams)
  );
  await loadMedia(initialPath, false, initialRoot);
  initRecentMedia();
  initFolderActions();
  initSorting();
  initFilters();
  initSearch();
  initBrowseViews();
  initShares();
  initRootsAdmin();

  // A shared/reloaded ?q= link opens straight into those results. The
  // history entry is already in place from the replaceState above, so this
  // restores the view without adding one.
  if (initialSearching) {
    document.getElementById('mediaSearchInput').value = initialWords;
    setSearchMode(true);
    // Before the search, not after: the subject is part of the question.
    restoreSubject(initialSubject);
    if (initialTag) await restorePool(initialPool, initialTag);
    await runSearch(initialWords);
  }

  // Same for a shared ?view= link: the entry already exists, so this restores
  // the view rather than adding one on top of it.
  if (initialView) await setBrowseView(initialView, { push: false });

  if (initialFile) {
    const resolved = await resolveFileEntry(initialFile, initialFileRoot);
    if (resolved) openMediaPreview(resolved.file, resolved.list);
  }
});
