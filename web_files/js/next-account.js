/* Delete a whole account (user, 2026-11-05: "I need the ability to delete entire accounts (twitter,
   pixiv, youtube, ...)"). Admins get "Delete account..." in the right-click menu of an account folder
   (<source>/<account> in the downloads library). The dialog shows what is in it, then trashes it
   (restorable from Trash) and by default also keeps it from being downloaded again. */
(function () {
  const SOURCES = new Set(['twitter', 'pixiv', 'instagram', 'bluesky', 'deviantart', 'patreon', 'civitai',
    'bilibili', 'youtube']);
  const nf = (n) => Number(n).toLocaleString('en-GB');
  const size = (b) => (b >= 1073741824 ? `${(b / 1073741824).toFixed(1)} GB`
    : b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.ceil(b / 1024)} KB`);

  function isAccount(item) {
    if (!item || !item.isFolder || typeof viewerIsAdmin === 'undefined' || !viewerIsAdmin) return false;
    const parts = String(item.path || '').split('/').filter(Boolean);
    return parts.length === 2 && SOURCES.has(parts[0]);
  }

  async function call(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const e = new Error(data.error || `HTTP ${res.status}`); e.data = data; throw e; }
    return data;
  }

  async function accountDialog(item) {
    const q = `root=${encodeURIComponent(item.root || '')}&path=${encodeURIComponent(item.path)}`;
    let pv;
    try { pv = await call(`/api/media/account/preview?${q}`); }
    catch (err) { alert(`Cannot delete this account: ${err.message}`); return; }
    const dialog = document.createElement('dialog');
    dialog.style.maxWidth = '34rem';
    const title = document.createElement('h3');
    title.textContent = `Delete ${pv.source} account ${pv.name}?`;
    const what = document.createElement('p');
    what.textContent = `${nf(pv.files)} file${pv.files === 1 ? '' : 's'} (${size(pv.bytes)}) move to the trash, `
      + 'for everybody, and stay restorable from the trash for 30 days. '
      + 'Trash only leaves the account free to be downloaded again later; the other choice keeps it out.';
    if (pv.source === 'pixiv') {
      what.textContent += ' Pixiv: if this artist is followed, the slow follow sync fetches the deleted works again '
        + "within weeks after 'Trash only'; 'Trash and don't download again' keeps them out.";
    }
    const actions = document.createElement('p');
    const mk = (text, cls) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = cls; b.textContent = text; return b;
    };
    const banBtn = mk("Trash and don't download again", 'btn btn-quiet');
    const relBtn = mk('Trash only', 'btn');
    const cancel = mk('Cancel', 'btn btn-quiet');
    const close = () => { dialog.close(); dialog.remove(); };
    cancel.addEventListener('click', close);
    const run = async (mode) => {
      banBtn.disabled = relBtn.disabled = true;
      try {
        await call('/api/media/account/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ root: item.root || '', path: item.path, mode, confirm: true, expected_files: pv.files }) });
        close();
        if (typeof loadMedia === 'function') loadMedia(currentMediaPath, false, currentMediaRoot);
      } catch (err) {
        close();
        alert(err.data && err.data.code === 'count_changed'
          ? `The account holds ${err.data.files} files now, ${pv.files} were previewed. Open the dialog again.`
          : `Not deleted: ${err.message}`);
      }
    };
    banBtn.addEventListener('click', () => run('ban'));
    relBtn.addEventListener('click', () => run('release'));
    actions.append(relBtn, banBtn, cancel);
    dialog.append(title, what, actions);
    document.body.appendChild(dialog);
    dialog.showModal();
  }

  const orig = window.showContextMenu;
  window.showContextMenu = function (x, y, item) {
    orig(x, y, item);
    if (!isAccount(item)) return;
    const menu = document.getElementById('mediaContextMenu');
    addContextMenuItem(menu, 'Delete account…', () => { hideContextMenu(); accountDialog(item); });
    const r = menu.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 8)) + 'px';
    menu.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
  };
})();
