/* The shell of media-next.html: the sidebar, the phone tab bar, the viewer's extra controls, the
   storage box and the keyboard shortcuts.

   Loaded last, after media.js, and it patches nothing in it. Everything media.js does is still
   media.js's: the sidebar rows are the old buttons (#peopleBtn, #charactersBtn, #favoritesBtn,
   #reviewBtn, ...) or call the same functions (setBrowseView, loadMedia, exitSearchMode,
   showContextMenu, navigatePreview), and the history model is untouched - a row that opens
   something pushes its entry the way the old button did. The one thing it replaces is the two
   functions that build URLs, so the address bar says /media-next.html instead of /media.html.

   Classic script, shared globals (media.js's `let` state is read by name), no build step. */

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const body = document.body;
  const NEXT_PATH = location.pathname;

  // ---------------------------------------------------------------------------
  // URLs. media.js builds '/media.html?...' for every history entry and link, which on this page
  // would turn the address bar into the OLD page after the first click - and a reload would land
  // there. Both builders are plain global functions that media.js and player.js call by name, so
  // wrapping them here is enough. The check in loadMedia() that skips a pushState for the URL
  // already shown compares against location, which now matches.
  // ---------------------------------------------------------------------------
  const rehome = (url) => (typeof url === 'string' ? url.replace(/^\/media\.html(?=[?#]|$)/, NEXT_PATH) : url);
  if (typeof window.mediaUrlFor === 'function') {
    const base = window.mediaUrlFor;
    window.mediaUrlFor = function () { return rehome(base.apply(this, arguments)); };
  }
  if (typeof window.previewUrlFor === 'function') {
    const base = window.previewUrlFor;
    window.previewUrlFor = function () { return rehome(base.apply(this, arguments)); };
  }

  // ---------------------------------------------------------------------------
  // Destinations. Characters, People, Review, Training and Check are browse views (media.js owns
  // their history entries and ?view=), Favorites and a search are searches. Recent, Libraries
  // and Shares used to be cards stacked under the folder grid, and Trash (M2) is new; each is a
  // destination now, shown by body[data-dest] (css/media-next.css). They have no URL parameter of their own: the hash
  // (#recent) keeps a reload there, and the history entry carries `nextDest` so Back and Forward
  // return to it. media.js's popstate handler ignores the field.
  // ---------------------------------------------------------------------------
  // Trash (js/next-trash.js) and Upload (js/next-upload.js) are destinations like the others; Libraries,
  // Trash and Upload are the admin's.
  const PSEUDO = ['timeline', 'explore', 'map', 'recent', 'shares', 'libraries', 'trash', 'upload', 'downloads'];
  const ADMIN_PSEUDO = ['libraries', 'trash', 'upload', 'downloads'];
  let pseudo = PSEUDO.includes(location.hash.slice(1)) ? location.hash.slice(1) : null;

  // Who is looking, asked once: /api/me is cheap and media.js asks it too, but asking here keeps this
  // file independent of when that answer lands. Resolves true for an admin, false for anyone else
  // (a guest, or an answer that never came).
  const roleReady = fetch('/api/me', { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : null))
    .then((me) => !!(me && me.role === 'admin'))
    .catch(() => false);

  // Review, Training and Check are admin pages here (owner, 2026-11-01), although the old page shows
  // them to guests. A guest who arrives with ?view=review in the URL (a bookmark, a link, Back) gets
  // the folders, and the address bar is cleaned in place. media.js calls setBrowseView by name from
  // its first load and its popstate handler, so wrapping the global is enough; the wrapper runs the
  // original synchronously for every other view.
  const ADMIN_VIEWS = ['review', 'training', 'check'];
  if (typeof window.setBrowseView === 'function') {
    const base = window.setBrowseView;
    window.setBrowseView = async function (view, opts) {
      let coerced = false;
      if (ADMIN_VIEWS.includes(view) && !(await roleReady)) { view = null; coerced = true; }
      const out = await base.call(this, view, opts);
      if (coerced) {
        history.replaceState({ mediaPath: currentMediaPath, mediaRoot: currentMediaRoot }, '',
          mediaUrlFor(currentMediaPath, currentMediaRoot));
      }
      return out;
    };
  }

  // Opening a folder from anywhere (the viewer's info box, a breadcrumb, a link) is a move to the
  // folders: a pseudo destination (Timeline, Recent, ...) must not stay on screen over it.
  // History-less loads (Back, Forward, the first load) keep whatever state they restore.
  if (typeof window.loadMedia === 'function') {
    const baseLoad = window.loadMedia;
    window.loadMedia = function (path, updateHistory, root) {
      if (pseudo && updateHistory !== false) { pseudo = null; syncNav(); }
      return baseLoad.apply(this, arguments);
    };
  }

  // The row that is active follows what is really on screen, not what was last clicked.
  function derivedDest() {
    if (currentView) return currentView;
    if (searchModeActive) {
      if (activePerson) return 'people';
      if (activePool || activeTag) return 'characters';
      if (activeFavorites) return 'favorites';
      return 'search';
    }
    return pseudo || 'folders';
  }

  const TAB_DESTS = ['people', 'favorites', 'folders', 'recent'];

  function syncNav() {
    const dest = derivedDest();
    body.dataset.dest = dest;
    for (const el of document.querySelectorAll('#nxNav [data-nav], #nxTabbar [data-go]')) {
      if ((el.dataset.nav || el.dataset.go) === dest) {
        el.setAttribute('aria-current', 'page');
        // A short window hides the lower rows (Libraries, Settings): bring the lit one into view.
        if (el.closest('#nxNav') && el.offsetParent) el.scrollIntoView({ block: 'nearest' });
      } else el.removeAttribute('aria-current');
    }
    // A destination with no tab of its own lights the More tab, so the bar never shows nothing.
    const more = $('nxMoreBtn');
    if (more) {
      if (!TAB_DESTS.includes(dest) && dest !== 'search') more.setAttribute('aria-current', 'page');
      else more.removeAttribute('aria-current');
    }
  }

  // Pseudo destinations are history entries, like a view is: entering one from the folders pushes,
  // moving between them replaces (three sideways steps should not cost three Back presses).
  function enterPseudo(dest) {
    if (pseudo === dest) { syncNav(); return; }
    const had = pseudo;
    pseudo = dest;
    if (dest === 'shares') window.loadShares();
    if (dest === 'timeline' && window.NextTimeline) window.NextTimeline.open();
    if (dest === 'explore' && window.NextExplore) window.NextExplore.open();
    if (dest === 'map' && window.NextMap) window.NextMap.open();
    if (dest === 'trash' && window.NextTrash) window.NextTrash.open();
    if (dest === 'upload' && window.NextUpload) window.NextUpload.open();
    if (dest === 'downloads' && window.NextDownloads) window.NextDownloads.open();
    const state = { mediaPath: currentMediaPath, mediaRoot: currentMediaRoot, nextDest: dest };
    const url = mediaUrlFor(currentMediaPath, currentMediaRoot) + '#' + dest;
    if (had) history.replaceState(state, '', url);
    else history.pushState(state, '', url);
    syncNav();
  }

  function leavePseudo() {
    if (!pseudo) return;
    pseudo = null;
    history.pushState({ mediaPath: currentMediaPath, mediaRoot: currentMediaRoot }, '',
      mediaUrlFor(currentMediaPath, currentMediaRoot));
    syncNav();
  }

  // ---------------------------------------------------------------------------
  // Shares. media.js's loadShares() only writes a one-line summary, because the list lives in
  // Settings > Shares. Here the destination is the list: a card per link with its cover, which
  // opens the share. Replacing the global is enough - initShares() and Create Pool call it by name.
  // /api/share answers guests too; renaming, expiry and revoking stay in Settings (admin).
  // ---------------------------------------------------------------------------
  // Admins load /api/share/manage: it adds the category (Characters, People, Accounts, Pools, ...),
  // view counts and the links revoked in the last 24 h. Everyone else gets /api/share and filters by
  // kind. Filter chips: All (active), one per category, Expired, Removed.
  const SHARE_KINDS = { pool: 'Pools', folder: 'Folders', file: 'Files', query: 'Live searches' };
  const shareState = { filter: 'all', query: '', shares: [], cats: null };
  const EYE = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';

  const TRASH = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';

  const shareGroup = (s) => (shareState.cats ? s.category : s.kind) || s.kind;
  const shareGroupName = (key) => {
    const cat = shareState.cats && shareState.cats.find((c) => c.key === key);
    return (cat && cat.label) || SHARE_KINDS[key] || key;
  };

  async function revokeShares(ids) {
    const res = ids.length === 1
      ? await fetch(`/api/share/${encodeURIComponent(ids[0])}`, { method: 'DELETE' })
      : await fetch('/api/share/bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, action: 'revoke' }),
      });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const now = Date.now() / 1000;
    for (const s of shareState.shares) {
      if (ids.includes(s.id)) { s.revoked = true; s.revoked_at = now; }
    }
  }

  const PENCIL = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/></svg>';
  const EXPIRY_CHOICES = [['keep', 'Keep expiry'], ['never', 'Never expires'], ['1', 'In 1 day'], ['7', 'In 7 days'],
    ['30', 'In 30 days'], ['90', 'In 90 days'], ['365', 'In a year'], ['date', 'On a date...']];

  function expiryLabel(share) {
    if (!share.expires_at) return '';
    const date = new Date(share.expires_at * 1000).toLocaleDateString();
    return share.expires_at * 1000 < Date.now() ? `expired ${date}` : `expires ${date}`;
  }

  function openShareEditor(card, share) {
    const old = card.querySelector('.nx-share-editor');
    if (old) { old.remove(); return; }
    const panel = document.createElement('form');
    panel.className = 'nx-share-editor';
    const label = document.createElement('input');
    label.type = 'text';
    label.maxLength = 120;
    label.value = share.label;
    label.setAttribute('aria-label', 'Name');
    const select = document.createElement('select');
    select.setAttribute('aria-label', 'Expiry');
    for (const [v, l] of EXPIRY_CHOICES) select.add(new Option(l, v));
    const day = document.createElement('input');
    day.type = 'date';
    day.hidden = true;
    day.min = new Date().toISOString().slice(0, 10);
    select.addEventListener('change', () => { day.hidden = select.value !== 'date'; });
    const note = document.createElement('div');
    note.className = 'nx-share-editor-note';
    note.textContent = share.expires_at ? `Now: ${expiryLabel(share)}` : 'Now: never expires';
    const row = document.createElement('div');
    row.className = 'nx-share-editor-row';
    const save = document.createElement('button');
    save.type = 'submit';
    save.textContent = 'Save';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', () => panel.remove());
    row.append(save, cancel);
    panel.append(label, select, day, note, row);
    // Cover and pool items (moved here from Settings > Shares).
    const extra = document.createElement('div');
    extra.className = 'nx-share-editor-row';
    const cover = document.createElement('button');
    cover.type = 'button';
    cover.textContent = share.has_custom_thumbnail ? 'New cover…' : 'Cover…';
    const file = document.createElement('input');
    file.type = 'file';
    file.accept = 'image/png,image/jpeg,image/webp,image/gif';
    file.hidden = true;
    cover.addEventListener('click', () => file.click());
    file.addEventListener('change', async () => {
      const f = file.files && file.files[0];
      file.value = '';
      if (!f) return;
      const form = new FormData();
      form.append('thumbnail', f);
      try {
        const res = await fetch(`/api/share/${encodeURIComponent(share.id)}/thumbnail`, { method: 'POST', body: form });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
        window.loadShares();
      } catch (err) { note.textContent = `Cover not saved: ${err.message}`; }
    });
    extra.append(cover, file);
    panel.appendChild(extra);
    if (share.kind === 'pool' && Array.isArray(share.items)) {
      const list = document.createElement('ul');
      list.className = 'nx-share-items';
      const draw = () => {
        list.replaceChildren(...share.items.map((item) => {
          const li = document.createElement('li');
          const name = document.createElement('span');
          name.textContent = `${item.kind === 'folder' ? '\u{1F4C1}' : '\u{1F4C4}'} ${item.path}`;
          const rm = document.createElement('button');
          rm.type = 'button';
          rm.textContent = 'Remove';
          rm.addEventListener('click', async () => {
            try {
              const res = await fetch(`/api/share/${encodeURIComponent(share.id)}/items`, {
                method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: item.path }),
              });
              if (!res.ok) throw new Error(`HTTP ${res.status}`);
              share.items = share.items.filter((i) => i.path !== item.path);
              share.detail = `${share.items.length} item${share.items.length === 1 ? '' : 's'}`;
              draw();
            } catch (err) { note.textContent = `Not removed: ${err.message}`; }
          });
          li.append(name, rm);
          return li;
        }));
      };
      draw();
      panel.appendChild(list);
    }
    panel.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = {};
      const name = label.value.trim();
      if (name !== share.label) body.label = name;
      if (select.value === 'never') body.expires_at = null;
      else if (select.value === 'date') {
        const t = Date.parse(`${day.value}T23:59:59`);        // end of that day, local time
        if (Number.isNaN(t)) { note.textContent = 'Pick a day.'; return; }
        body.expires_at = t / 1000;
      } else if (select.value !== 'keep') body.expires_at = Date.now() / 1000 + Number(select.value) * 86400;
      if (!Object.keys(body).length) { panel.remove(); return; }
      save.disabled = true;
      try {
        const res = await fetch(`/api/share/${encodeURIComponent(share.id)}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        if ('label' in body) share.label = body.label || share.label;
        if ('expires_at' in body) {
          share.expires_at = body.expires_at;
          share.expired = !!body.expires_at && body.expires_at * 1000 < Date.now();
        }
      } catch (err) {
        save.disabled = false;
        note.textContent = `Not saved: ${err.message}`;
        return;
      }
      drawShares();
    });
    card.appendChild(panel);
    label.focus();
  }

  function shareCard(share) {
    const card = document.createElement('div');
    card.className = 'nx-share' + (share.revoked ? ' is-removed' : '');
    const open = document.createElement(share.revoked ? 'div' : 'a');
    open.className = 'nx-share-open';
    if (!share.revoked) {
      open.href = share.url;
      open.target = '_blank';
      open.rel = 'noopener';
    }
    const img = document.createElement('img');
    img.className = 'nx-share-cover';
    img.src = share.cover_url;
    img.alt = '';
    img.loading = 'lazy';
    img.addEventListener('error', () => img.removeAttribute('src'), { once: true });
    const name = document.createElement('span');
    name.className = 'nx-share-name';
    name.textContent = share.label;
    const meta = document.createElement('span');
    meta.className = 'nx-share-meta';
    const when = share.revoked && share.revoked_at ? share.revoked_at : share.created_at;
    const date = when ? new Date(when * 1000).toLocaleDateString() : '';
    meta.textContent = [shareGroupName(shareGroup(share)), share.revoked ? `removed ${date}` : date, share.revoked ? '' : expiryLabel(share)]
      .filter(Boolean).join(' · ');
    if (typeof share.views === 'number') {
      const eye = document.createElement('span');
      eye.className = 'nx-share-views';
      eye.title = `${share.views} view${share.views === 1 ? '' : 's'}, ${share.visitors || 0} visitor${share.visitors === 1 ? '' : 's'}`;
      eye.innerHTML = EYE;
      eye.append(String(share.views));
      meta.appendChild(eye);
    }
    open.append(img, name, meta);
    card.appendChild(open);
    if (share.revoked) return card;

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'nx-share-copy';
    copy.textContent = 'Copy link';
    copy.addEventListener('click', async () => {
      const link = new URL(share.url, location.origin).href;
      try {
        await navigator.clipboard.writeText(link);
        copy.textContent = 'Copied';
        setTimeout(() => { copy.textContent = 'Copy link'; }, 1500);
      } catch (e) {
        window.prompt('Copy this link:', link);
      }
    });
    card.appendChild(copy);
    if (viewerIsAdmin) {
      // Revokes the link at once; the tile moves to "Removed", where it stays for 24 h. Files untouched.
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'nx-share-remove';
      remove.innerHTML = TRASH;
      remove.setAttribute('aria-label', 'Remove this share');
      remove.title = 'Remove this share - the link stops working, the files are not touched';
      remove.addEventListener('click', async () => {
        remove.disabled = true;
        try {
          await revokeShares([share.id]);
        } catch (e) {
          remove.disabled = false;
          window.alert(`Not removed: ${e.message}`);
          return;
        }
        drawShares();
      });
      card.appendChild(remove);
      // Rename and expiry (PATCH /api/share/<id> {label, expires_at}) in a small panel over the card.
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'nx-share-edit-btn';
      edit.innerHTML = PENCIL;
      edit.setAttribute('aria-label', 'Rename or set expiry');
      edit.title = 'Rename or set expiry';
      edit.addEventListener('click', () => openShareEditor(card, share));
      card.appendChild(edit);
    }
    return card;
  }

  function drawShares() {
    const listEl = $('sharesList');
    if (!listEl) return;
    const live = shareState.shares.filter((s) => !s.revoked && !s.expired);
    const expired = shareState.shares.filter((s) => !s.revoked && s.expired);
    const removed = shareState.shares.filter((s) => s.revoked);
    const groups = [];
    for (const s of live) if (!groups.includes(shareGroup(s))) groups.push(shareGroup(s));
    if (shareState.filter !== 'all' && shareState.filter !== 'expired' && shareState.filter !== 'removed'
      && !groups.includes(shareState.filter)) shareState.filter = 'all';

    const bar = document.createElement('div');
    bar.className = 'nx-share-bar';
    const addChip = (key, label, count) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'nx-share-chip';
      chip.setAttribute('aria-pressed', String(shareState.filter === key));
      chip.textContent = `${label} ${count}`;
      chip.addEventListener('click', () => { shareState.filter = key; drawShares(); });
      bar.appendChild(chip);
    };
    addChip('all', 'All', live.length);
    for (const key of groups) addChip(key, shareGroupName(key), live.filter((s) => shareGroup(s) === key).length);
    if (expired.length) addChip('expired', 'Expired', expired.length);
    if (removed.length) addChip('removed', 'Removed', removed.length);

    const search = document.createElement('input');
    search.type = 'search';
    search.className = 'nx-share-search';
    search.placeholder = 'Filter by name';
    search.value = shareState.query;
    search.addEventListener('input', () => { shareState.query = search.value; drawGrid(); });
    bar.appendChild(search);
    if (viewerIsAdmin && shareState.filter === 'expired' && expired.length) {
      const sweep = document.createElement('button');
      sweep.type = 'button';
      sweep.className = 'nx-share-chip nx-share-sweep';
      sweep.textContent = `Remove all ${expired.length} expired`;
      sweep.addEventListener('click', async () => {
        sweep.disabled = true;
        try {
          await revokeShares(expired.map((s) => s.id));
        } catch (e) {
          sweep.disabled = false;
          window.alert(`Not removed: ${e.message}`);
          return;
        }
        shareState.filter = 'removed';
        drawShares();
      });
      bar.appendChild(sweep);
    }

    const grid = document.createElement('div');
    grid.className = 'nx-share-grid';
    function drawGrid() {
      const words = shareState.query.trim().toLowerCase();
      const pool = shareState.filter === 'expired' ? expired : shareState.filter === 'removed' ? removed : live;
      const shown = pool
        .filter((s) => ['all', 'expired', 'removed'].includes(shareState.filter) || shareGroup(s) === shareState.filter)
        .filter((s) => !words || String(s.label).toLowerCase().includes(words));
      grid.replaceChildren(...shown.map(shareCard));
      if (!shown.length) {
        const none = document.createElement('p');
        none.className = 'shares-summary';
        none.textContent = live.length || expired.length || removed.length ? 'No share matches.'
          : 'No active share links - use Share in the viewer\'s menu or right-click any file or folder.';
        grid.appendChild(none);
      }
    }
    drawGrid();
    listEl.replaceChildren(bar, grid);
  }

  window.loadShares = async function () {
    try {
      let res = viewerIsAdmin ? await fetch('/api/share/manage') : null;
      if (res && res.ok) {
        const data = await res.json();
        shareState.shares = data.shares;
        shareState.cats = data.categories;
      } else {
        res = await fetch('/api/share');
        if (!res.ok) return;
        shareState.shares = await res.json();
        shareState.cats = null;
      }
    } catch (e) {
      return;
    }
    drawShares();
  };

  // A reload on #shares runs loadShares before the role is known: redo it as admin for categories/views.
  roleReady.then((admin) => { if (admin && pseudo === 'shares') window.loadShares(); });

  window.addEventListener('popstate', (e) => {
    pseudo = (e.state && e.state.nextDest) || null;
    syncNav();
    if (pseudo === 'timeline' && window.NextTimeline) window.NextTimeline.open();
    if (pseudo === 'explore' && window.NextExplore) window.NextExplore.open();
    if (pseudo === 'map' && window.NextMap) window.NextMap.open();
    if (pseudo === 'trash' && window.NextTrash) window.NextTrash.open();
    if (pseudo === 'upload' && window.NextUpload) window.NextUpload.open();
    if (pseudo === 'downloads' && window.NextDownloads) window.NextDownloads.open();
  });

  // Reload on #recent: media.js's first replaceState drops the hash and the state, so it is put
  // back once that has run (its listener is registered first, and replaces synchronously).
  document.addEventListener('DOMContentLoaded', () => {
    // Landing page (user, 2026-11-05 plan): the bare address opens the Timeline. Any path, view, search,
    // file or hash in the address is a link to somewhere specific and is left alone.
    if (!pseudo && !location.search && !location.hash && window.NextTimeline) pseudo = 'timeline';
    if (pseudo) {
      history.replaceState({ mediaPath: currentMediaPath, mediaRoot: currentMediaRoot, nextDest: pseudo }, '',
        mediaUrlFor(currentMediaPath, currentMediaRoot) + '#' + pseudo);
    }
    syncNav();
    if (pseudo === 'timeline' && window.NextTimeline) window.NextTimeline.open();
    if (pseudo === 'explore' && window.NextExplore) window.NextExplore.open();
    if (pseudo === 'map' && window.NextMap) window.NextMap.open();
    // A guest with #trash, #upload or #libraries in the address gets the folders; an admin on #trash gets the
    // list (enterPseudo, which loads it, is not on the way in from a reload).
    roleReady.then((admin) => {
      if (!admin && ADMIN_PSEUDO.includes(pseudo)) {
        pseudo = null;
        history.replaceState({ mediaPath: currentMediaPath, mediaRoot: currentMediaRoot }, '',
          mediaUrlFor(currentMediaPath, currentMediaRoot));
        syncNav();
      } else if (admin && pseudo === 'trash' && window.NextTrash) window.NextTrash.open();
      else if (admin && pseudo === 'upload' && window.NextUpload) window.NextUpload.open();
      else if (admin && pseudo === 'downloads' && window.NextDownloads) window.NextDownloads.open();
    });
  });

  // media.js flips these with attributes, not events: watching them is how the active row keeps up
  // with Back, Forward, a reload and anything the page does by itself.
  (() => {
    const mo = new MutationObserver(syncNav);
    for (const id of ['charactersCard', 'peopleCard', 'reviewCard', 'trainingCard', 'checkCard']) {
      mo.observe($(id), { attributes: true, attributeFilter: ['hidden'] });
    }
    mo.observe($('browseSections'), { attributes: true, attributeFilter: ['hidden', 'style'] });
    mo.observe($('searchResultsCard'), { attributes: true, attributeFilter: ['style'] });
    mo.observe($('mediaSubjectChip'), { attributes: true, childList: true, subtree: true, characterData: true });
  })();

  // The library's root in the address. A plain /media-next.html (or ?path=x) loads the server's default
  // library, but the history entry still says root '' while currentMediaRoot becomes that library's id.
  // media.js's popstate handler then finds "another place" the first time anything pops an entry
  // (closing the pool picker or the delete dialog does) and reloads the folder, which throws the
  // scroll position away. Writing the root into the entry once the folder is up makes it the same place.
  function pinRoot() {
    if (!currentMediaRoot || (history.state && history.state.view)) return;
    const params = new URLSearchParams(location.search);
    if (params.get('root')) return;
    params.set('root', currentMediaRoot);
    const st = Object.assign({}, history.state || {});
    if (!st.mediaRoot) st.mediaRoot = currentMediaRoot;
    if (st.mediaPath === undefined) st.mediaPath = currentMediaPath;
    history.replaceState(st, '', location.pathname + '?' + params + location.hash);
  }
  new MutationObserver(pinRoot).observe($('mediaBreadcrumb'), { childList: true });

  // ---------------------------------------------------------------------------
  // Going somewhere. The search pill and the sidebar are on screen together now, which the old page
  // never had: the folder-header buttons only existed while the folders were showing. So a row
  // first leaves whatever is open (a search, a browse view) the way that thing's own close does.
  // ---------------------------------------------------------------------------

  // The pill emptied: the box, the pending debounce and the subject chip go. keepEntry because the
  // caller is about to navigate itself - popping the search's history entry here would fire a
  // popstate that reloads the folder after (and over) that navigation, see exitSearchMode().
  function leaveSearch() {
    const input = $('mediaSearchInput');
    if (!searchModeActive && !input.value) return;
    clearTimeout(searchDebounceTimer);
    clearTimeout(suggestTimer);
    input.value = '';
    hideSuggest();
    if (searchModeFromPool) {              // what clearSubject() restores
      searchModeFromPool = false;
      try { searchMode = localStorage.getItem('mediaSearchMode') || 'filename'; } catch (e) { searchMode = 'filename'; }
      if (!SEARCH_MODE_LABELS[searchMode]) searchMode = 'filename';
      syncSearchModeChip();
    }
    if (searchModeActive) exitSearchMode({ keepEntry: true });
    else searchNote();
  }

  // A row that is one of media.js's own buttons: leave what is open, then let its handler run.
  function prepareNav(dest) {
    leaveSearch();
    // A browse view and a search cannot be on screen together; moving to Favorites from a view
    // closes the view in place (a replace, not a push), exactly as 'Back to files' would end it.
    if (dest === 'favorites' && currentView) setBrowseView(null);
    if (pseudo) pseudo = null;
    window.scrollTo(0, 0);
  }

  $('nxNav').addEventListener('click', (e) => {
    const row = e.target.closest('[data-nav]');
    if (!row) return;
    const dest = row.dataset.nav;
    if (['people', 'characters', 'review', 'training', 'check', 'favorites'].includes(dest)) prepareNav(dest);
  }, true);

  async function goPseudo(dest) {
    leaveSearch();
    if (currentView) await setBrowseView(null);
    enterPseudo(dest);
    window.scrollTo(0, 0);
  }

  // Folders: from another destination, back to the folder that was open. On the folders already,
  // the top of the library (the brand does the same from anywhere).
  async function goFolders(top) {
    const here = derivedDest() === 'folders';
    leaveSearch();
    if (currentView) await setBrowseView(null);
    leavePseudo();
    if (top || here) loadMedia('', true, '');
    window.scrollTo(0, 0);
  }

  $('navFolders').addEventListener('click', () => goFolders(false));
  $('navTimeline').addEventListener('click', () => goPseudo('timeline'));
  $('navExplore').addEventListener('click', () => goPseudo('explore'));
  $('navMap').addEventListener('click', () => goPseudo('map'));
  $('navRecent').addEventListener('click', () => goPseudo('recent'));
  $('navShares').addEventListener('click', () => goPseudo('shares'));
  $('navLibraries').addEventListener('click', () => goPseudo('libraries'));
  $('navTrash').addEventListener('click', () => goPseudo('trash'));
  $('navUpload').addEventListener('click', () => goPseudo('upload'));
  $('navDownloads').addEventListener('click', () => goPseudo('downloads'));
  $('nxBrand').addEventListener('click', (e) => {
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    goFolders(true);
  });

  // Typing in the pill while a browse view is open: the view steps aside (no history entry - the
  // search pushes its own), where the old page left both on screen at once.
  $('mediaSearchInput').addEventListener('input', () => {
    if (currentView) {
      renderBrowseView(null);
      if (typeof syncDeckMode === 'function') syncDeckMode();
    }
  }, true);

  // ---------------------------------------------------------------------------
  // The VR row drives the real top-bar button, which media.js shows, hides and paints.
  // ---------------------------------------------------------------------------
  (() => {
    const real = $('mediaVrTopBtn');
    const row = $('navVr');
    const sync = () => {
      row.hidden = real.hidden;
      row.querySelector('.nx-label').textContent = real.textContent.trim() || 'VR';
      row.setAttribute('aria-pressed', real.getAttribute('aria-pressed') || 'false');
      row.style.opacity = real.getAttribute('aria-disabled') === 'true' ? '0.55' : '';
      row.title = real.title || 'View in VR';
    };
    new MutationObserver(sync).observe(real, { attributes: true, childList: true, characterData: true, subtree: true });
    row.addEventListener('click', () => real.click());
    row.addEventListener('mouseenter', () => real.dispatchEvent(new Event('mouseenter')));
    sync();
  })();

  $('navImageSearch').addEventListener('click', () => $('imageSearchBtn').click());

  // ---------------------------------------------------------------------------
  // Phone: the tab bar clicks the sidebar row of the same name; More opens the sidebar as a sheet.
  // ---------------------------------------------------------------------------
  function setSheet(open) {
    body.classList.toggle('nx-more-open', open);
    $('nxMoreBtn').setAttribute('aria-expanded', String(open));
  }
  $('nxMoreBtn').addEventListener('click', () => setSheet(!body.classList.contains('nx-more-open')));
  $('nxSheetBackdrop').addEventListener('click', () => setSheet(false));
  $('nxTabbar').addEventListener('click', (e) => {
    const tab = e.target.closest('[data-go]');
    if (!tab) return;
    setSheet(false);
    const row = document.querySelector(`#nxNav [data-nav="${tab.dataset.go}"]`);
    if (row) row.click();
  });
  // Any row (or link) in the sheet ends it.
  $('nxNav').addEventListener('click', (e) => { if (e.target.closest('.nx-row')) setSheet(false); });
  window.matchMedia('(max-width: 760px)').addEventListener('change', (m) => { if (!m.matches) setSheet(false); });

  // ---------------------------------------------------------------------------
  // Who is looking: the Admin group and the storage box are for admins. /api/me is cheap and
  // media.js already asks it; asking again keeps this file independent of when that answer lands.
  // ---------------------------------------------------------------------------
  roleReady.then((admin) => {
    if (!admin) return;
    body.classList.add('nx-admin');
    $('nxAdminGroup').hidden = false;
    refreshStorage();
    setInterval(refreshStorage, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStorage(); });
  });

  // The storage box: /api/system-stats (admin only) reports the downloads volume's disk_total_gb,
  // disk_free_gb and disk_used_percent, in GiB. The box shows that volume's use, not the library's
  // own size. Total 0 means the server's sampler has not run yet: the box stays hidden.
  const storageBox = $('nxStorage');
  function sizeText(gb) {
    return gb >= 1024 ? `${(gb / 1024).toFixed(1)} TiB` : `${Math.round(gb)} GiB`;
  }
  async function refreshStorage() {
    let stats = null;
    try {
      const res = await fetch('/api/system-stats', { cache: 'no-store' });
      if (res.ok) stats = await res.json();
    } catch (e) { /* offline: keep what is shown */ return; }
    const total = Number(stats && stats.disk_total_gb);
    const free = Number(stats && stats.disk_free_gb);
    if (!(total > 0) || !(free >= 0)) { storageBox.hidden = true; return; }
    const used = Math.max(0, total - free);
    const pct = Math.min(100, (used / total) * 100);
    $('nxStorageText').textContent = `${sizeText(used)} of ${sizeText(total)} used`;
    $('nxStorageFill').style.width = `${pct.toFixed(1)}%`;
    $('nxStorageBar').setAttribute('aria-valuenow', String(Math.round(pct)));
    storageBox.classList.toggle('is-full', pct >= 90);
    storageBox.hidden = false;
  }

  // ---------------------------------------------------------------------------
  // The viewer. Everything here sits beside media.js's preview code, never inside it.
  // ---------------------------------------------------------------------------
  const modal = $('mediaPreviewModal');
  const viewerOpen = () => modal.style.display === 'flex';

  // Previous / next: the same call the arrow keys make. Shown only where there is one to go to.
  const prevBtn = $('nxPrevBtn');
  const nextBtn = $('nxNextBtn');
  prevBtn.addEventListener('click', (e) => { e.stopPropagation(); navigatePreview(-1); });
  nextBtn.addEventListener('click', (e) => { e.stopPropagation(); navigatePreview(1); });
  function syncViewerNav() {
    const open = viewerOpen();
    prevBtn.hidden = !open || currentPreviewIndex <= 0;
    nextBtn.hidden = !open || currentPreviewIndex < 0 || currentPreviewIndex >= activePreviewList.length - 1;
  }
  // The title is rewritten on every open and every step, and the modal's style on open and close.
  new MutationObserver(syncViewerNav).observe($('mediaPreviewTitle'), { childList: true, characterData: true, subtree: true });
  new MutationObserver(syncViewerNav).observe(modal, { attributes: true, attributeFilter: ['style'] });

  // The info drawer: player.js shows and hides #mediaInfoPanel with display, and rewrites its
  // contents, so its header is a sibling that follows the display.
  const infoPanel = $('mediaInfoPanel');
  new MutationObserver(() => {
    const on = infoPanel.style.display !== 'none' && viewerOpen();
    modal.classList.toggle('nx-info-open', on);
    $('nxInfoHead').hidden = !on;
  }).observe(infoPanel, { attributes: true, attributeFilter: ['style'] });
  $('nxInfoClose').addEventListener('click', (e) => { e.stopPropagation(); $('mediaInfoBtn').click(); });

  // The three dots: the context menu of the item on screen. Called directly rather than by a
  // synthetic contextmenu event on the picture: the menu is the same function, and a file with no
  // inline preview (nothing carries a contextmenu handler there) and a song whose cover has not
  // loaded yet get it too.
  $('mediaMoreBtn').addEventListener('click', (e) => {
    e.stopPropagation();
    const item = activePreviewList[currentPreviewIndex];
    if (!item) return;
    const r = e.currentTarget.getBoundingClientRect();
    showContextMenu(r.right, r.bottom + 6, item);
    // showContextMenu clamps into the window; line the menu's right edge up with the button's.
    const menu = $('mediaContextMenu');
    // Admin: the viewer's Delete (js/next-select.js), the same dialog Del opens. The menu is the
    // old page's own; this one entry is added to it here, never on the grid's right-click menu.
    if (window.NextSelect && window.NextSelect.canDelete() && typeof addContextMenuItem === 'function') {
      const del = addContextMenuItem(menu, 'Delete…', () => { hideContextMenu(); window.NextSelect.deleteCurrent(); });
      if (del) del.classList.add('nx-menu-danger');
    }
    menu.style.left = Math.max(4, Math.min(r.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8)) + 'px';
    menu.style.top = Math.max(4, Math.min(parseFloat(menu.style.top) || 0, window.innerHeight - menu.offsetHeight - 8)) + 'px';
  });

  // ---------------------------------------------------------------------------
  // Keyboard. Never while typing, never with a modifier, never under a dialog.
  // ---------------------------------------------------------------------------
  const help = $('nxHelp');
  let helpReturn = null;
  function setHelp(open) {
    help.hidden = !open;
    if (open) {
      helpReturn = document.activeElement;
      $('nxHelpClose').focus();
    } else if (helpReturn && helpReturn.focus) {
      helpReturn.focus();
      helpReturn = null;
    }
  }
  $('nxHelpClose').addEventListener('click', () => setHelp(false));
  help.addEventListener('click', (e) => { if (e.target === help) setHelp(false); });

  const isTyping = (t) => !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  const dialogOpen = () => ['deleteModal', 'poolPickerModal'].some((id) => $(id).style.display === 'flex')
    || $('imageSearchDialog').open || !$('nxConfirm').hidden;

  // Escape belongs to the help list first, then to the sheet; capture on window runs before
  // media.js's handlers, which would close the viewer under it.
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!help.hidden) { e.stopPropagation(); e.preventDefault(); setHelp(false); }
    else if (body.classList.contains('nx-more-open')) { e.stopPropagation(); setSheet(false); }
    // The info drawer closes before the viewer does (a dialog or the context menu on top of it goes first).
    else if (modal.classList.contains('nx-info-open') && !dialogOpen() && !(typeof contextMenuIsOpen === 'function' && contextMenuIsOpen())) {
      e.stopPropagation(); e.preventDefault(); $('mediaInfoBtn').click();
    }
  }, true);

  let spaceHandled = false;
  document.addEventListener('keyup', (e) => {
    // A button's Space activates on keyup: swallow the one whose keydown already toggled playback.
    if (e.key === ' ' && spaceHandled) { spaceHandled = false; e.preventDefault(); }
  }, true);

  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return;
    if (!help.hidden) {
      if (e.key === '?') { e.preventDefault(); setHelp(false); }
      return;
    }
    if (dialogOpen()) return;
    const open = viewerOpen();
    switch (e.key) {
      case '?':
        e.preventDefault();
        setHelp(true);
        break;
      case '/':
        if (open) return;
        e.preventDefault();
        $('mediaSearchInput').focus();
        $('mediaSearchInput').select();
        break;
      case 'i':
        if (!open || e.repeat) return;
        e.preventDefault();
        $('mediaInfoBtn').click();
        break;
      case 'f':
        if (!open || e.repeat) return;
        e.preventDefault();
        $('mediaFavoriteBtn').click();
        break;
      case ' ': {
        if (!open) return;
        const media = modal.querySelector('#mediaPreviewBody video, #mediaPreviewBody audio');
        if (!media) return;
        e.preventDefault();
        spaceHandled = true;
        if (e.repeat) return;
        if (media.paused) { const p = media.play(); if (p && p.catch) p.catch(() => {}); } else media.pause();
        break;
      }
      // Del (admin) is js/next-select.js's: the picture in the viewer, or the selection.
      default:
    }
  });
})();
