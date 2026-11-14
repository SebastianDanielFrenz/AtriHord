/* Settings page (settings.html).

   The config.json sections come with a schema from the server (/api/settings)
   and are rendered from it; every value is validated again on the server when
   saved, so the inputs' own min/max is only a convenience. Appearance is kept
   in this browser (localStorage, the Stylehub theme) or on the account
   (/api/prefs). Sections are routed by hash: settings.html#live-upscaling. */
'use strict';

const SECTIONS = [
  { id: 'general', title: 'General' },
  { id: 'appearance', title: 'Appearance' },
  { id: 'users', title: 'Users' },
  { id: 'shares', title: 'Shares' },
  { id: 'downloads', title: 'Download behaviour', group: 'Server' },
  { id: 'image-processing', title: 'Image processing', group: 'Server' },
  { id: 'live-upscaling', title: 'Live upscaling', group: 'Server' },
  { id: 'song-covers', title: 'Song covers', group: 'Server' },
  { id: 'video-trim', title: 'Intro & outro cutting', group: 'Server' },
  { id: 'character-blacklist', title: 'Character blacklist', group: 'Server' },
  { id: 'stereo3d', title: '2D to 3D', group: 'Server' },
  { id: 'vision-worker', title: 'Vision worker', group: 'Server' },
  { id: 'modules', title: 'Modules', group: 'Server' },
];

let settingsData = null;       // {schema, values, server_time}
let routeSeq = 0;
let fieldSeq = 0;

// ---------------------------------------------------------------- helpers --

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function toast(message, kind = 'ok') {
  const box = document.getElementById('settingsToast');
  box.textContent = message;
  box.className = `settings-toast is-${kind}`;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 3500);
}

const fmtNumber = (n) => Number(n || 0).toLocaleString('en-GB');

function fmtBytes(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(n || 0);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtAgo(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function stat(label, value) {
  return h('div', { class: 'settings-stat' },
    h('div', { class: 'settings-stat-value' }, value),
    h('div', { class: 'settings-stat-label' }, label));
}

// Refills a box. replaceChildren() writes a null argument out as the text
// "null" (h() skips it), and the cards leave optional parts as null.
function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

function card(title, ...children) {
  return h('section', { class: 'settings-card' }, title ? h('h3', {}, title) : null, ...children);
}

const capitalise = (text) => String(text).charAt(0).toUpperCase() + String(text).slice(1);

function localGet(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
}

function localSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* private mode: not remembered */ }
}

// ---------------------------------------------------------- schema forms --

function numberControl(field, value, onChange, id) {
  const input = h('input', {
    id, type: 'number', class: 'settings-input', min: field.min, max: field.max,
    step: field.type === 'int' ? 1 : (field.step || 'any'),
  });
  input.value = value ?? '';
  input.addEventListener('input', () => onChange(input.value === '' ? null : Number(input.value)));
  return h('span', { class: 'settings-number' }, input,
    field.unit ? h('span', { class: 'settings-unit' }, field.unit) : null);
}

function fieldControl(field, value, onChange) {
  const id = `field${++fieldSeq}`;
  switch (field.type) {
    case 'int':
    case 'float':
      return { id, node: numberControl(field, value, onChange, id) };
    case 'bool': {
      const input = h('input', { id, type: 'checkbox', class: 'settings-switch' });
      input.checked = !!value;
      input.addEventListener('change', () => onChange(input.checked));
      return { id, node: input };
    }
    case 'select': {
      const select = h('select', { id, class: 'settings-input' },
        field.options.map((o) => h('option', { value: String(o), text: String((field.labels && field.labels[o]) || o) })));
      select.value = String(value);
      select.addEventListener('change', () => onChange(field.options.find((o) => String(o) === select.value)));
      return { id, node: select };
    }
    case 'time': {
      const input = h('input', { id, type: 'time', class: 'settings-input' });
      input.value = value ?? '';
      input.addEventListener('input', () => onChange(input.value));
      return { id, node: input };
    }
    case 'text': {
      const input = h('input', { id, type: 'text', class: 'settings-input settings-wide' });
      input.value = value ?? '';
      input.addEventListener('input', () => onChange(input.value));
      return { id, node: input };
    }
    case 'tags': {
      const area = h('textarea', { id, class: 'settings-input settings-wide', rows: 2, spellcheck: 'false' });
      area.value = (value || []).join(', ');
      area.addEventListener('input', () => onChange(area.value.split(/[\s,]+/).filter(Boolean)));
      return { id, node: area };
    }
    case 'lines': {
      // One entry per line: a name with a space in it stays one entry.
      const area = h('textarea', { id, class: 'settings-input settings-wide', rows: 4, spellcheck: 'false' });
      area.value = (value || []).join('\n');
      area.addEventListener('input', () => onChange(area.value.split(/\r?\n/).map((t) => t.trim()).filter(Boolean)));
      return { id, node: area };
    }
    case 'multi': {
      const chosen = new Set(value || []);
      const box = h('div', { id, class: 'settings-checks', role: 'group' });
      for (const option of field.options) {
        const cb = h('input', { type: 'checkbox' });
        cb.checked = chosen.has(option);
        cb.addEventListener('change', () => {
          if (cb.checked) chosen.add(option); else chosen.delete(option);
          onChange(field.options.filter((o) => chosen.has(o)));
        });
        box.append(h('label', { class: 'settings-check' }, cb, option));
      }
      return { id, node: box };
    }
    case 'passes': {
      // Enabled passes first, in their order; the rest after them, unticked.
      const order = [...(value || [])];
      for (const option of field.options) if (!order.includes(option)) order.push(option);
      const enabled = new Set(value || []);
      const list = h('ol', { id, class: 'settings-passes' });
      const emit = () => onChange(order.filter((p) => enabled.has(p)));
      const draw = () => {
        list.replaceChildren(...order.map((pass, i) => {
          const cb = h('input', { type: 'checkbox' });
          cb.checked = enabled.has(pass);
          cb.addEventListener('change', () => {
            if (cb.checked) enabled.add(pass); else enabled.delete(pass);
            emit();
          });
          const up = h('button', { type: 'button', class: 'settings-mini', title: 'Earlier', disabled: i === 0, text: '↑' });
          const down = h('button', { type: 'button', class: 'settings-mini', title: 'Later', disabled: i === order.length - 1, text: '↓' });
          up.addEventListener('click', () => { [order[i - 1], order[i]] = [order[i], order[i - 1]]; draw(); emit(); });
          down.addEventListener('click', () => { [order[i + 1], order[i]] = [order[i], order[i + 1]]; draw(); emit(); });
          return h('li', { class: 'settings-pass' }, h('label', { class: 'settings-check' }, cb, pass), up, down);
        }));
      };
      draw();
      return { id, node: list };
    }
    case 'map_int': {
      const current = { ...(value || {}) };
      const grid = h('div', { id, class: 'settings-map' });
      for (const key of field.keys) {
        const input = h('input', { type: 'number', class: 'settings-input', min: field.min, max: field.max, step: 1 });
        input.value = current[key] ?? 0;
        input.addEventListener('input', () => {
          current[key] = input.value === '' ? null : Number(input.value);
          onChange({ ...current });
        });
        grid.append(h('label', { class: 'settings-map-item' }, h('span', {}, key),
          h('span', { class: 'settings-number' }, input, field.unit ? h('span', { class: 'settings-unit' }, field.unit) : null)));
      }
      return { id, node: grid };
    }
    default:
      return { id, node: h('code', {}, JSON.stringify(value)) };
  }
}

function schemaForm(schemaId) {
  const section = settingsData.schema.find((s) => s.id === schemaId);
  const draft = {};
  const errorBoxes = new Map();
  const saveBtn = h('button', { type: 'submit', class: 'btn settings-save', disabled: true, text: 'Save' });
  const undoBtn = h('button', { type: 'button', class: 'btn btn-quiet', disabled: true, text: 'Undo changes' });
  const status = h('span', { class: 'settings-status' });
  const form = h('form', { class: 'settings-form', novalidate: true });
  const refresh = () => {
    const n = Object.keys(draft).length;
    saveBtn.disabled = !n;
    undoBtn.disabled = !n;
    status.textContent = n ? `${n} unsaved change${n === 1 ? '' : 's'}` : '';
  };

  for (const field of section.fields) {
    const error = h('div', { class: 'settings-error', hidden: true });
    const saved = settingsData.values[field.key];
    const { id, node } = fieldControl(field, saved, (value) => {
      if (JSON.stringify(value) === JSON.stringify(saved)) delete draft[field.key];
      else draft[field.key] = value;
      error.hidden = true;
      refresh();
    });
    errorBoxes.set(field.key, error);
    form.append(h('div', { class: 'settings-field' + (field.danger ? ' is-danger' : ''), 'data-key': field.key },
      h('label', { class: 'settings-label', for: id }, field.label),
      h('div', { class: 'settings-control' }, node,
        field.help ? h('p', { class: 'settings-help' }, field.help) : null, error)));
  }

  form.append(h('div', { class: 'settings-actions' }, saveBtn, undoBtn, status));
  undoBtn.addEventListener('click', () => route());
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!Object.keys(draft).length) return;
    saveBtn.disabled = true;
    status.textContent = 'Saving…';
    try {
      const out = await api('PUT', '/api/settings', { values: draft });
      settingsData.values = out.values;
      toast('Saved');
      route();
    } catch (err) {
      const errors = (err.data && err.data.errors) || {};
      for (const [key, message] of Object.entries(errors)) {
        const box = errorBoxes.get(key);
        if (box) { box.textContent = capitalise(message); box.hidden = false; }
      }
      status.textContent = Object.keys(errors).length
        ? 'Nothing saved - see the marked fields' : `Nothing saved: ${err.message}`;
      saveBtn.disabled = false;
    }
  });
  return form;
}

// ------------------------------------------------------------- sections --

function renderGeneral(view) {
  view.append(schemaForm('general'));
}

async function renderAppearance(view) {
  // Theme: the shared control (atrihord-user.js), so every AtriHord page on this
  // site follows the same choice.
  const themeGroup = h('div', { class: 'settings-segmented', role: 'group', 'aria-label': 'Theme' });
  const readTheme = () => (window.ahTheme ? window.ahTheme.get() : localGet('ah.theme', 'dark'));
  const paintTheme = () => themeGroup.querySelectorAll('button').forEach((b) =>
    b.setAttribute('aria-pressed', String(b.dataset.mode === readTheme())));
  for (const [mode, label] of [['auto', 'Follow the system'], ['dark', 'Dark'], ['light', 'Light']]) {
    themeGroup.append(h('button', {
      type: 'button', class: 'settings-seg', 'data-mode': mode, text: label,
      onclick: () => {
        if (window.ahTheme) {
          window.ahTheme.set(mode);
        } else {
          localSet('ah.theme', mode);
          document.documentElement.setAttribute('data-theme', mode);
        }
        paintTheme();
      },
    }));
  }
  document.addEventListener('ah:theme', paintTheme);
  paintTheme();

  // Gallery: per browser, read by gallery.js on every page load.
  const names = h('input', { type: 'checkbox', class: 'settings-switch', id: 'appNames' });
  names.checked = localGet('mediaShowTileNames', '0') === '1';
  names.addEventListener('change', () => {
    localSet('mediaShowTileNames', names.checked ? '1' : '0');
    toast(names.checked ? 'File names shown on tiles' : 'File names hidden on tiles');
  });
  const size = h('select', { class: 'settings-input', id: 'appTileSize' },
    [['140', 'Small'], ['190', 'Medium'], ['260', 'Large'], ['340', 'Extra large']]
      .map(([v, label]) => h('option', { value: v, text: label })));
  size.value = localGet('mediaGalleryRowHeight', '190');
  size.addEventListener('change', () => { localSet('mediaGalleryRowHeight', size.value); toast('Tile size saved'); });

  // Dashboard layout: per account.
  const advanced = h('input', { type: 'checkbox', class: 'settings-switch', id: 'appAdvanced' });
  try {
    const prefs = await api('GET', '/api/prefs');
    advanced.checked = !!prefs.advanced_mode;
  } catch (e) { advanced.disabled = true; }
  advanced.addEventListener('change', async () => {
    try {
      await api('POST', '/api/prefs', { advanced_mode: advanced.checked });
      toast('Saved to your account');
    } catch (err) {
      advanced.checked = !advanced.checked;
      toast(`Not saved: ${err.message}`, 'error');
    }
  });

  const row = (label, control, help) => h('div', { class: 'settings-field' },
    h('label', { class: 'settings-label', for: control.id || null }, label),
    h('div', { class: 'settings-control' }, control, help ? h('p', { class: 'settings-help' }, help) : null));

  view.append(
    card('This browser',
      row('Theme', themeGroup, 'For every AtriHord page on this site, in this browser.'),
      row('File names on tiles', names, 'Folders always show theirs. The Names button in the library toggles the same setting.'),
      row('Tile size', size, 'The row height of the library and share galleries.')),
    card('Your account',
      row('Advanced dashboard', advanced, 'Statistics, pipelines and indexing on the dashboard, for you on every device.')));
}

function showTokenOnce(view, username, token, title = `New API token for ${username}`, note = 'Send it as the X-API-Key header.') {
  const copy = h('button', { type: 'button', class: 'settings-mini', text: 'Copy' });
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(token); toast('Token copied'); } catch (e) { toast('Copy failed - select it by hand', 'error'); }
  });
  view.querySelectorAll('.settings-token-card').forEach(el => el.remove());
  const tokenCard = card(title,
    h('p', { class: 'settings-warning' }, note),
    h('div', { class: 'settings-token' }, h('code', {}, token), copy));
  tokenCard.classList.add('settings-token-card');
  view.prepend(tokenCard);
}

async function renderUsers(view) {
  const data = await api('GET', '/api/settings/users');
  const tbody = h('tbody');
  for (const user of data.users) {
    const tokenCell = h('td', {}, user.has_token ? 'Issued' : 'None');
    const issue = h('button', { type: 'button', class: 'settings-mini', text: user.has_token ? 'Replace' : 'Issue' });
    const revoke = h('button', { type: 'button', class: 'settings-mini is-danger', text: 'Revoke', hidden: !user.has_token });
    const show = h('button', { type: 'button', class: 'settings-mini', text: 'View', hidden: !user.has_token });
    show.addEventListener('click', async () => {
      try {
        const out = await api('GET', `/api/settings/users/${encodeURIComponent(user.username)}/token`);
        showTokenOnce(view, out.username, out.token, `API token of ${out.username}`, 'Anyone who sees this can act as this account. Send it as the X-API-Key header.');
      } catch (err) { toast(`Not shown: ${err.message}`, 'error'); }
    });
    issue.addEventListener('click', async () => {
      if (user.has_token && !confirm(`Replace the API token of ${user.username}? Whatever uses the old one stops working.`)) return;
      try {
        const out = await api('POST', `/api/settings/users/${encodeURIComponent(user.username)}/token`);
        showTokenOnce(view, out.username, out.token);
        user.has_token = true;
        tokenCell.textContent = 'Issued';
        issue.textContent = 'Replace';
        revoke.hidden = false;
        show.hidden = false;
      } catch (err) { toast(`Not issued: ${err.message}`, 'error'); }
    });
    revoke.addEventListener('click', async () => {
      if (!confirm(`Revoke the API token of ${user.username}?`)) return;
      try {
        await api('DELETE', `/api/settings/users/${encodeURIComponent(user.username)}/token`);
        user.has_token = false;
        tokenCell.textContent = 'None';
        issue.textContent = 'Issue';
        revoke.hidden = true;
        show.hidden = true;
        toast('Token revoked');
      } catch (err) { toast(`Not revoked: ${err.message}`, 'error'); }
    });
    tbody.append(h('tr', {},
      h('td', {}, user.username, user.username === data.me ? h('span', { class: 'settings-badge' }, 'you') : null),
      h('td', {}, h('span', { class: `settings-badge role-${user.role}` }, user.role)),
      h('td', {}, fmtAgo(user.last_seen)),
      tokenCell,
      h('td', { class: 'settings-row-actions' }, show, issue, revoke)));
  }

  const roots = h('div', { class: 'settings-checks' });
  for (const root of data.roots) {
    const cb = h('input', { type: 'checkbox' });
    cb.checked = root.guest_visible;
    cb.addEventListener('change', async () => {
      try {
        await api('PATCH', `/api/roots/${encodeURIComponent(root.id)}`, { guest_visible: cb.checked });
        toast(`${root.name}: ${cb.checked ? 'visible to guests' : 'hidden from guests'}`);
      } catch (err) {
        cb.checked = !cb.checked;
        toast(`Not saved: ${err.message}`, 'error');
      }
    });
    roots.append(h('label', { class: 'settings-check' }, cb, root.name));
  }

  view.append(
    card('Accounts',
      h('p', { class: 'settings-help' },
        'Accounts and passwords belong to Authelia, the single sign-on - nobody is created here. ',
        'media-dl gives each person a role from their Authelia groups:'),
      h('ul', { class: 'settings-list' },
        h('li', {}, h('strong', {}, 'Admin: '), data.groups.admin.join(', ')),
        h('li', {}, h('strong', {}, 'Guest: '), data.groups.guest.join(', '),
          ' - the media library only, and only the libraries ticked below')),
      h('div', { class: 'settings-table-wrap' },
        h('table', { class: 'settings-table' },
          h('thead', {}, h('tr', {}, ['User', 'Role', 'Last seen', 'API token', ''].map((t) => h('th', {}, t)))),
          tbody))),
    card('Libraries guests can see', roots));
}

const SHARE_ICONS = {
  character: '\u{1F3AD}', person: '\u{1F464}', account: '\u{1F4E1}', folder: '\u{1F4C1}',
  pool: '\u{1F5C2}\u{FE0F}', image: '\u{1F5BC}\u{FE0F}', video: '\u{1F3AC}', music: '\u{1F3B5}', file: '\u{1F4C4}',
};
const SHARE_KIND_NAMES = {
  character: 'Character', person: 'Person', account: 'Account', folder: 'Folder', pool: 'Pool',
  image: 'Image', video: 'Video', music: 'Music', file: 'File',
};
const EXPIRY_CHOICES = [['never', 'Never'], ['1', 'In 1 day'], ['7', 'In 7 days'], ['30', 'In 30 days'],
  ['90', 'In 90 days'], ['365', 'In a year'], ['date', 'On a date…'], ['now', 'Expire now']];

function expiryFromChoice(choice) {
  const now = Date.now() / 1000;
  if (choice === 'never') return null;
  if (choice === 'now') return now;
  const days = Number(choice);
  return days > 0 ? now + days * 86400 : undefined;
}

function expiryText(share) {
  if (!share.expires_at) return 'Never expires';
  const left = share.expires_at - Date.now() / 1000;
  const date = new Date(share.expires_at * 1000)
    .toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  if (left <= 0) return `Expired ${date}`;
  if (left < 86400) return `Expires in ${Math.max(1, Math.round(left / 3600))} h`;
  return `Expires ${date} (${Math.round(left / 86400)} days)`;
}

async function renderShares(view) {
  // The links themselves (list, rename, expiry, cover, pool items, revoke) are managed on the Shares page of
  // media-next.html; only the defaults for new links are a setting.
  view.append(card('New links', schemaForm('shares')),
    h('p', { class: 'settings-help' }, 'Existing share links are managed on the ',
      h('a', { href: '/media-next.html#shares', text: 'Shares page' }), '.'));
}

// The cookie file is only ever put on the server by hand. This says whether
// yt-dlp will count it as signed in - its own rule, see youtube_cookie_status.
function youtubeCookieCard(yt) {
  const state = !yt.present ? 'No cookie file' : (yt.signed_in ? 'Signed in' : 'Signed out');
  const parts = [h('h3', {}, 'YouTube sign-in'),
    h('div', { class: 'settings-stats' },
      stat('yt-dlp sees', h('span', { class: yt.signed_in ? 'settings-ok' : 'settings-warning' }, state)),
      stat('Cookies in the file', yt.present ? fmtNumber(yt.cookies) : '-'),
      stat('File last written', yt.present ? fmtAgo(yt.modified_at) : '-'))];
  if (yt.present) {
    parts.push(h('p', { class: 'settings-help' },
      'yt-dlp writes the file back after every run, so a recent time does not mean a fresh export.'));
  }
  if (!yt.signed_in) {
    parts.push(
      h('p', {}, yt.present
        ? 'YouTube has ended this session (LOGIN_INFO is gone), so downloads run signed out and age-restricted and members-only videos are skipped.'
        : 'Without a cookie file, downloads run signed out and age-restricted and members-only videos are skipped.'),
      h('ol', { class: 'settings-list' },
        h('li', {}, 'Open a private browser window and sign in to YouTube.'),
        h('li', {}, 'In that same tab, open youtube.com/robots.txt.'),
        h('li', {}, 'Export the cookies as cookies.txt with a cookie-export extension.'),
        h('li', {}, 'Close the private window without signing out, and do not use that session again - YouTube replaces cookies that stay in use.'),
        h('li', {}, 'Copy the file to ', h('code', {}, yt.path || 'cookies_youtube.txt'), ' on the server.')),
      h('p', { class: 'settings-help' },
        "Age-restricted videos also need the account itself to pass YouTube's age check. An account used for downloading can be banned, so a second account is safer. Skipped videos are not marked as downloaded: running the same link again fetches only them."));
  }
  return parts;
}

async function renderDownloads(view) {
  view.append(schemaForm('downloads'));
  const cookieBox = card('YouTube sign-in', h('p', { class: 'settings-help' }, 'Loading…'));
  const box = card('Recent activity', h('p', { class: 'settings-help' }, 'Loading…'));
  view.append(cookieBox, box);
  try {
    const st = await api('GET', '/api/settings/stats');
    cookieBox.replaceChildren(...youtubeCookieCard(st.youtube_cookies || {}));
    const statuses = Object.entries(st.task_statuses || {})
      .map(([status, n]) => `${fmtNumber(n)} ${status.toLowerCase()}`).join(', ') || 'none';
    const rows = Object.entries(st.pipelines || {})
      .sort((a, b) => (b[1].weekly_usage || 0) - (a[1].weekly_usage || 0));
    fill(box, h('h3', {}, 'Recent activity'),
      h('div', { class: 'settings-stats' },
        stat('Downloaded in the last hour', fmtBytes(st.volume_1h_bytes)),
        stat('Downloaded in the last 24 h', fmtBytes(st.volume_24h_bytes)),
        stat('Tasks on the dashboard', statuses)),
      h('div', { class: 'settings-table-wrap' },
        h('table', { class: 'settings-table' },
          h('thead', {}, h('tr', {}, ['Pipeline', 'Queued', 'Speed now', 'Since restart', 'Runs this week'].map((t) => h('th', {}, t)))),
          h('tbody', {}, rows.map(([cat, p]) => h('tr', {},
            h('td', {}, cat), h('td', {}, fmtNumber(p.queued)), h('td', {}, `${p.rate_mbps} MiB/s`),
            h('td', {}, `${fmtNumber(p.session_total_mb)} MB`), h('td', {}, fmtNumber(p.weekly_usage))))))));
  } catch (err) {
    cookieBox.remove();
    box.append(h('p', { class: 'settings-error' }, err.message));
  }
}

async function renderImageProcessing(view) {
  view.append(schemaForm('image_processing'));
  const box = card('Measuring files');
  view.append(box);
  const draw = (dimensions, serverTime) => {
    const d = dimensions || {};
    let state = 'Not run since the server started.';
    if (d.running) state = `Running - started ${fmtAgo(d.started)} (${d.trigger}).`;
    else if (d.finished) state = `Last run finished ${fmtAgo(d.finished)} (${d.trigger}) - ${d.exit === 0 ? 'completed' : `exit code ${d.exit}`}.`;
    const run = h('button', { type: 'button', class: 'btn', text: 'Measure now', disabled: !!d.running });
    run.addEventListener('click', async () => {
      run.disabled = true;
      try {
        const out = await api('POST', '/api/settings/dimensions/run');
        toast(out.started ? 'Measuring started' : 'Already running');
        draw(out.state, serverTime);
      } catch (err) {
        toast(`Not started: ${err.message}`, 'error');
        run.disabled = false;
      }
    });
    fill(box, h('h3', {}, 'Measuring files'),
      h('p', { class: 'settings-help' },
        'Dimensions, frame rates and video lengths of new downloads, for the filters and the gallery; ',
        `entries of deleted files are removed. Server time now: ${serverTime || '?'}.`),
      h('p', {}, state),
      d.last_line ? h('pre', { class: 'settings-log' }, d.last_line) : null,
      run);
  };
  try {
    const st = await api('GET', '/api/settings/stats');
    draw(st.dimensions, st.server_time);
  } catch (err) {
    box.append(h('p', { class: 'settings-error' }, err.message));
  }
}

async function renderLiveUpscaling(view) {
  view.append(schemaForm('live_upscale'));
  const box = card('Cache', h('p', { class: 'settings-help' }, 'Loading…'));
  view.append(box);
  try {
    const st = await api('GET', '/api/media/upscale/stats');
    // Model names as the library shows them; the keys when the names cannot be had.
    const labels = await api('GET', '/api/media/upscale/config').then((c) => (c && c.model_labels) || {}, () => ({}));
    const models = Object.entries(st.models || {}).map(([m, n]) => `${fmtNumber(n)} ${labels[m] || m}`).join(', ') || 'none yet';
    const clear = h('button', { type: 'button', class: 'btn btn-danger', text: 'Clear cache', disabled: !st.entries });
    clear.addEventListener('click', async () => {
      if (!confirm(`Delete all ${fmtNumber(st.entries)} upscaled images? They are made again when looked at.`)) return;
      try {
        const out = await api('POST', '/api/media/upscale/clear');
        toast(`Removed ${fmtNumber(out.removed)} files`);
        route();
      } catch (err) { toast(`Not cleared: ${err.message}`, 'error'); }
    });
    const onThinPool = /^\/data2\//.test(st.cache_dir || '');
    fill(box, h('h3', {}, 'Cache'),
      h('div', { class: 'settings-stats' },
        stat('Upscaled images', fmtNumber(st.entries)),
        stat('Disk used', fmtBytes(st.bytes)),
        stat('By model', models),
        stat('Average GPU time', st.avg_seconds ? `${st.avg_seconds} s` : '-'),
        stat("Vision worker's limit", st.worker_max_output_mp ? `${st.worker_max_output_mp} MP` : 'unknown')),
      h('p', { class: 'settings-help' }, 'Folder: ', h('code', {}, st.cache_dir || '?')),
      onThinPool ? h('p', { class: 'settings-warning' },
        'This is the fallback on /data2, a thin-provisioned pool. Create the folder on the media disk with ',
        h('code', {}, 'sudo install -d -o <service user> -m 755 <folder shown above>'),
        ' on the media VM and the cache moves there within ten minutes.') : null,
      (st.jobs || []).length ? h('p', {}, 'In progress: ',
        st.jobs.map((j) => `${j.path.split('/').pop()} (${j.status})`).join(', ')) : null,
      clear);
  } catch (err) {
    box.append(h('p', { class: 'settings-error' }, err.message));
  }
}

async function renderVisionWorker(view) {
  const box = card('Current worker', h('p', { class: 'settings-help' }, 'Loading…'));
  view.append(box, h('section', { class: 'settings-card is-soon' },
    h('p', {}, "Coming next: editing the worker's own settings, switching the indexer to a worker on another ",
      'machine, and a step-by-step guide for installing one.')));
  try {
    const d = await api('GET', '/api/media/indexing');
    const w = d.worker || {};
    fill(box, h('h3', {}, 'Current worker'),
      h('div', { class: 'settings-stats' },
        stat('Reachable', w.reachable ? 'yes' : 'no'),
        stat('Models loaded', (w.loaded || []).join(', ') || 'none'),
        stat('VRAM in use', w.vram_allocated_mb != null ? `${fmtNumber(w.vram_allocated_mb)} MB` : '-'),
        stat('Upscales served', fmtNumber(w.upscales))),
      h('p', { class: 'settings-help' },
        `Indexer: ${d.state || '?'}${d.current_task ? `, pass ${d.current_task}` : ''}. `,
        `Processing hours: ${w.windows || '?'}.`),
      w.last_error ? h('p', { class: 'settings-error' }, `Last error: ${w.last_error}`) : null);
  } catch (err) {
    box.append(h('p', { class: 'settings-error' }, err.message));
  }
}

// The cover job: the settings, then what it has done and what is left. It
// runs for weeks, so this page is where anyone finds out whether it is still
// going and what it changed last.
async function renderSongCovers(view) {
  view.append(schemaForm('song_covers'));
  const box = card('Progress', h('p', { class: 'settings-help' }, 'Loading\u2026'));
  view.append(box);
  const draw = (st) => {
    const states = st.states || {};
    const queued = states.queued || 0;
    const reasons = Object.entries(st.queued_by_reason || {})
      .map(([r, n]) => `${fmtNumber(n)} ${{ asked: 'asked for', missing: 'without a cover', black: 'black',
        channel: 'from an intro channel', all: 'everything else' }[r] || r}`).join(', ');
    const scan = h('button', { type: 'button', class: 'btn', text: 'Look for songs to fix' });
    scan.addEventListener('click', async () => {
      scan.disabled = true;
      try {
        const out = await api('POST', '/api/music/covers/scan', {});
        toast(out.running ? 'Reading the songs\u2026 (it keeps going in the background)'
          : `${fmtNumber(out.seen)} songs read, ${fmtNumber(out.queued)} queued`);
        const again = await api('GET', '/api/music/covers');
        draw(again);
      } catch (err) {
        toast(`Not started: ${err.message}`, 'error');
        scan.disabled = false;
      }
    });
    fill(box, h('h3', {}, 'Progress'),
      h('div', { class: 'settings-stats' },
        stat('Waiting', fmtNumber(queued)),
        stat('Remade', fmtNumber(st.changed || 0)),
        stat('Left as they were', fmtNumber(states.kept || 0)),
        stat('Could not be fetched', fmtNumber(states.failed || 0)),
        stat('Nothing to do', fmtNumber(states.skipped || 0)),
        stat('At this rate, done in', queued ? `${st.hours_left} h of running` : '-')),
      h('p', { class: 'settings-help' },
        st.enabled ? 'Running: ' : 'Switched off. ',
        queued ? `${reasons}.` : 'nothing waiting.',
        st.current ? ` Now: ${st.current}.` : '',
        st.scanned_at ? ` Songs last read ${fmtAgo(st.scanned_at)}.` : ' The songs have not been read yet.'),
      (st.recent || []).length ? h('table', { class: 'settings-table' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Song'), h('th', {}, 'What happened'), h('th', {}, 'When'))),
        h('tbody', {}, (st.recent || []).map((r) => h('tr', {},
          h('td', {}, r.path), h('td', {}, `${r.state}: ${r.note || ''}`), h('td', {}, fmtAgo(r.last)))))) : null,
      scan);
  };
  try {
    draw(await api('GET', '/api/music/covers'));
  } catch (err) {
    fill(box, h('h3', {}, 'Progress'), h('p', { class: 'settings-error' }, err.message));
  }
}

// Intro & outro cutting (video_trim.py): the settings form, then the review list.
// Nothing is cut until an entry is approved here. The list refreshes itself while
// the section stays open, because the work runs in the background.
const TRIM_KIND = { intro: 'Intro', outro: 'Outro' };
const TRIM_STATES = { pending: 'waiting for approval', approved: 'approved', rejected: 'rejected' };

async function renderVideoTrim(view) {
  view.append(schemaForm('video_trim'));
  const box = card('Detected intros and outros', h('p', { class: 'settings-help' }, 'Loading…'));
  view.append(box);
  let timer = null;
  const expandedVideos = new Set();
  const videoPreviews = new Map();

  const act = async (sig, action, body, message) => {
    try {
      await api('POST', `/api/media/trim/${sig.id}/${action}`, body || {});
      toast(message);
      await load();
    } catch (err) {
      toast(`Not done: ${err.message}`, 'error');
    }
  };

  const entry = (sig) => {
    const counts = sig.counts || {};
    const videoList = h('ul', { class: 'settings-help',
      style: 'max-height:320px;overflow:auto;padding-left:20px;overflow-wrap:anywhere' });
    const videos = h('details', {},
      h('summary', { style: 'cursor:pointer' }, `Videos (${sig.videos}) — open a preview`), videoList);
    let loadingVideos = false;
    videos.addEventListener('toggle', async () => {
      if (!videos.open) { expandedVideos.delete(sig.id); return; }
      expandedVideos.add(sig.id);
      if (loadingVideos) return;
      loadingVideos = true;
      try {
        let preview = videoPreviews.get(sig.id);
        if (!preview || preview.count !== sig.videos) {
          fill(videoList, h('li', {}, 'Loading videos…'));
          const data = await api('GET', `/api/media/trim/${encodeURIComponent(sig.id)}/preview`);
          preview = { count: sig.videos, members: data.members || [] };
          videoPreviews.set(sig.id, preview);
        }
        fill(videoList, ...preview.members.map((member) => h('li', {},
          h('a', {
            href: '/media.html?' + new URLSearchParams({ file: member.path, fileRoot: sig.root }),
            target: '_blank', rel: 'noopener noreferrer', title: member.path,
          }, member.path.split('/').pop() || member.path))));
      } catch (err) {
        fill(videoList, h('li', {}, `Could not load videos: ${err.message}. Close and reopen to retry.`));
      } finally {
        loadingVideos = false;
      }
    });
    videos.open = expandedVideos.has(sig.id);
    const frames = h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;margin:6px 0' },
      Array.from({ length: sig.preview_frames || 0 }, (_, i) => h('img', {
        src: `/api/media/trim/${sig.id}/frame/${i}`, alt: `${TRIM_KIND[sig.kind]} frame ${i + 1}`,
        loading: 'lazy', style: 'height:72px;border-radius:4px', onerror: (e) => e.target.remove(),
      })));
    const auto = h('input', { type: 'checkbox', class: 'settings-switch' });
    auto.checked = !!sig.auto_delete;
    auto.addEventListener('change', () => act(sig, 'auto-delete', { value: auto.checked },
      auto.checked ? 'Originals are deleted right after each cut' : 'Originals are kept'));
    const buttons = [];
    if (sig.status !== 'approved') {
      buttons.push(h('button', { type: 'button', class: 'btn', text: 'Approve cut',
        onclick: () => act(sig, 'approve', null, 'Approved - cutting in the background') }));
    }
    if (sig.status === 'pending') {
      buttons.push(h('button', { type: 'button', class: 'btn btn-quiet', text: 'Reject',
        onclick: () => act(sig, 'reject', null, 'Rejected') }));
    }
    if (sig.originals_kept) {
      buttons.push(h('button', { type: 'button', class: 'btn btn-quiet', text: 'Undo',
        title: 'Write the kept originals back',
        onclick: () => confirm(`Restore ${sig.originals_kept} original video(s)?`)
          && act(sig, 'undo', null, 'Restoring in the background') }));
      buttons.push(h('button', { type: 'button', class: 'btn btn-quiet', text: 'Delete originals',
        title: 'Delete the kept originals; the cut can then not be undone',
        onclick: () => confirm(`Delete ${sig.originals_kept} kept original(s), ${fmtBytes(sig.originals_bytes)}? `
          + 'The cut can then not be undone.') && act(sig, 'delete-originals', null, 'Deleting in the background') }));
    }
    const done = counts.cut || 0;
    const parts = [`${sig.videos} video${sig.videos === 1 ? '' : 's'}`, `${sig.length_s} s`,
      TRIM_STATES[sig.status] || sig.status];
    if (done) parts.push(`${done} cut`);
    if (counts.skipped) parts.push(`${counts.skipped} skipped`);
    if (counts.error) parts.push(`${counts.error} failed`);
    if (sig.originals_kept) parts.push(`originals kept: ${sig.originals_kept} (${fmtBytes(sig.originals_bytes)})`);
    return h('div', { class: 'settings-field', 'data-id': sig.id },
      h('div', { class: 'settings-label' }, `${TRIM_KIND[sig.kind] || sig.kind} · ${sig.root}/${sig.folder || ''}`),
      h('div', { class: 'settings-control' },
        h('p', { class: 'settings-help' }, parts.join(' · ')),
        frames,
        videos,
        (sig.errors || []).length ? h('p', { class: 'settings-help' },
          sig.errors.map((e) => h('div', {}, `${e.path}: ${e.state} - ${e.note}`))) : null,
        h('label', { class: 'settings-check' }, auto, 'Delete originals automatically'),
        h('p', { class: 'settings-actions' }, buttons)));
  };

  // After a cut, the 3D and Smooth motion work that existed for the old version is made again.
  const redoLine = (redo) => {
    if (!redo) return null;
    const d = redo.stereo3d || {};
    const i = redo.interp || {};
    const parts = [];
    const three = [d.queued ? `${d.queued} waiting` : '', d.running ? `${d.running} converting` : '',
      d.failed ? `${d.failed} failed` : ''].filter(Boolean);
    if (three.length || d.requeued_total) parts.push(`3D redo: ${three.join(', ') || 'none waiting'}`
      + (d.requeued_total ? ` (${d.requeued_total} re-queued since start)` : ''));
    const smooth = [i.queued ? `${i.queued} waiting` : '', i.running ? `${i.running} running` : '',
      i.failed ? `${i.failed} failed` : ''].filter(Boolean);
    if (smooth.length || i.finished) parts.push(`Smooth motion redo: ${smooth.join(', ') || 'none waiting'}`
      + (i.finished ? ` (${i.finished} done since start)` : ''));
    const bad = (i.jobs || []).filter((j) => j.state === 'failed' || (j.error && j.state === 'queued'));
    return parts.length ? h('p', { class: 'settings-help' }, `After a cut: ${parts.join(' · ')}.`,
      bad.slice(0, 3).map((j) => h('div', { title: j.error || '' }, `${j.path}: ${j.state}${j.error ? ` - ${j.error}` : ''}`))) : null;
  };

  const draw = (data) => {
    const st = data.status;
    const run = h('button', { type: 'button', class: 'btn', text: 'Look for intros and outros now',
      onclick: async () => {
        try { await api('POST', '/api/media/trim/run', {}); toast('Looking in the background'); }
        catch (err) { toast(`Not started: ${err.message}`, 'error'); }
      } });
    const groups = ['pending', 'approved', 'rejected'].map((status) => {
      const list = data.signatures.filter((x) => x.status === status);
      return list.length ? h('div', {}, h('h4', {}, `${capitalise(TRIM_STATES[status])} (${list.length})`),
        list.map(entry)) : null;
    });
    fill(box, h('h3', {}, 'Detected intros and outros'),
      h('p', { class: 'settings-help' },
        st.enabled ? 'On. ' : 'Switched off. ',
        st.current ? `Now: ${st.current}. ` : '',
        st.queued ? `${st.queued} job${st.queued === 1 ? '' : 's'} waiting. ` : '',
        st.last_pass ? `Last daily pass ${fmtAgo(st.last_pass)}.` : 'No daily pass yet.'),
      redoLine(data.redo),
      data.signatures.length ? groups : h('p', { class: 'settings-help' }, 'Nothing found yet.'), run);
  };

  async function load() {
    try {
      draw(await api('GET', '/api/media/trim'));
    } catch (err) {
      fill(box, h('h3', {}, 'Detected intros and outros'), h('p', { class: 'settings-error' }, err.message));
    }
  }
  await load();
  timer = setInterval(() => {
    if (!box.isConnected) { clearInterval(timer); return; }
    // Not while a button's confirm dialog or a checkbox is being used.
    if (!document.hidden) load();
  }, 15000);
}

// Character blacklist (server.py, "Character blacklist"): one list for everybody, because
// trashing removes the files for everybody. Adding goes through a preview and an explicit
// confirmation that states the number of files; the files go to the trash (restorable) and
// new ones are trashed as the tagger finds them. Removing an entry restores nothing - the
// trash does that, and files restored from it are exempted below.
async function renderCharacterBlacklist(view) {
  const addBox = card('Add a character');
  const listBox = card('Blacklisted characters', h('p', { class: 'settings-help' }, 'Loading…'));
  const exemptBox = card('Exemptions');
  view.append(
    h('p', { class: 'settings-help' },
      'A blacklisted character is hidden from the character pages. Files showing only blacklisted characters are moved to the trash with a ban, so the downloaders do not fetch them again; a picture that also shows a character not on the list is kept. '
      + 'The tagger\'s new results are checked every 10 minutes. Removing a character from the list restores nothing; '
      + 'restore its batch from the trash instead.'),
    addBox, listBox, exemptBox);

  // ---- add: type-ahead, preview, confirm
  const input = h('input', { type: 'text', class: 'settings-input settings-wide', list: 'charblSuggest',
    placeholder: 'Type a character name…', autocomplete: 'off' });
  const suggest = h('datalist', { id: 'charblSuggest' });
  const variants = h('input', { type: 'checkbox', class: 'settings-switch' });
  variants.checked = true;
  const minP = h('input', { type: 'number', class: 'settings-input', min: 0, max: 1, step: 0.05,
    placeholder: 'default', 'aria-label': 'Minimum confidence for this character' });
  const found = new Map();                       // label text -> pool tag
  let typing = 0;
  input.addEventListener('input', async () => {
    const q = input.value.trim();
    const mine = ++typing;
    if (q.length < 2 || found.has(q)) return;
    try {
      const out = await api('GET', `/api/media/characters?limit=8&q=${encodeURIComponent(q)}`);
      if (mine !== typing) return;
      found.clear();
      const options = out.items.map((it) => {
        const text = `${it.label}${it.series ? ' · ' + it.series : ''}`;
        found.set(text, it.tag);
        return h('option', { value: text }, `${fmtNumber(it.count)} files`);
      });
      fill(suggest, options);
    } catch (err) { /* the type-ahead is a convenience */ }
  });

  const dialog = h('dialog', { class: 'settings-card', style: 'max-width:34rem' });
  document.body.append(dialog);
  const closeDialog = () => { if (dialog.open) dialog.close(); };

  const preview = async () => {
    const text = input.value.trim();
    const tag = found.get(text) || text.replace(/ /g, '_');
    if (!tag) { toast('Pick a character first', 'error'); return; }
    const body = { tag, include_variants: variants.checked };
    if (minP.value !== '') body.min_p = Number(minP.value);
    let pv;
    try { pv = await api('POST', '/api/media/characters/blacklist/preview', body); }
    catch (err) { toast(`No preview: ${err.message}`, 'error'); return; }
    const confirmBtn = h('button', { type: 'button', class: 'btn', text:
      pv.count ? `Trash ${fmtNumber(pv.count)} file${pv.count === 1 ? '' : 's'} and blacklist` : 'Blacklist (nothing to trash now)' });
    confirmBtn.disabled = pv.already_listed;
    confirmBtn.addEventListener('click', async () => {
      confirmBtn.disabled = true;
      try {
        await api('POST', '/api/media/characters/blacklist',
          { ...body, confirm: true, expected_count: pv.count });
        toast('Blacklisted - trashing in the background');
        closeDialog();
        input.value = '';
        await load();
      } catch (err) {
        const grew = err.data && err.data.code === 'count_grew';
        toast(grew ? `${err.data.count} files match now, ${pv.count} were previewed. Preview again.`
          : `Not added: ${err.message}`, 'error');
        closeDialog();
      }
    });
    fill(dialog,
      h('h3', {}, `Blacklist ${pv.tag}?`),
      h('p', {}, pv.count
        ? `${fmtNumber(pv.count)} file${pv.count === 1 ? '' : 's'} will be moved to the trash with a ban, for everybody. `
          + 'They stay restorable from the trash for 30 days.'
        : 'No file matches right now. New ones will be trashed as the tagger finds them.'),
      h('p', {}, pv.kept_shared
        ? `${fmtNumber(pv.kept_shared)} picture${pv.kept_shared === 1 ? ' that also shows' : 's that also show'} a character not on the list ${pv.kept_shared === 1 ? 'is' : 'are'} kept.`
        : 'Pictures that also show a character not on the list are kept.'),
      pv.already_listed ? h('p', { class: 'settings-error' }, 'This character is already on the list.') : null,
      h('p', { class: 'settings-help' },
        `Covers ${pv.variants.length} tag${pv.variants.length === 1 ? '' : 's'} at confidence ${pv.min_p} or more: `,
        pv.variants.join(', ')),
      pv.samples.length ? h('p', { class: 'settings-help' }, 'For example: ',
        pv.samples.map((s) => s.path).join(' · ')) : null,
      h('p', { class: 'settings-actions' }, confirmBtn,
        h('button', { type: 'button', class: 'btn btn-quiet', text: 'Cancel', onclick: closeDialog })));
    dialog.showModal();
  };

  fill(addBox,
    h('h3', {}, 'Add a character'),
    h('div', { class: 'settings-field' },
      h('label', { class: 'settings-label' }, 'Character'),
      h('div', { class: 'settings-control' }, input, suggest)),
    h('label', { class: 'settings-check' }, variants, 'Include costumes and other variants of the character'),
    h('div', { class: 'settings-field' },
      h('label', { class: 'settings-label' }, 'Minimum confidence'),
      h('div', { class: 'settings-control' }, minP,
        h('p', { class: 'settings-help' }, 'Empty = the server default. A higher value leaves doubtful files alone.'))),
    h('p', { class: 'settings-actions' },
      h('button', { type: 'button', class: 'btn', text: 'Preview…', onclick: preview })));

  // ---- the list and the exemptions
  const draw = (data) => {
    const st = data.status || {};
    const last = st.last;
    const sweep = st.running ? 'A sweep is running. ' : st.pending ? 'A sweep is waiting. ' : '';
    const lastText = last
      ? `Last sweep ${fmtAgo(last.at)}: ${fmtNumber(last.trashed)} trashed`
        + (last.deferred ? `, ${fmtNumber(last.deferred)} left for the next sweep` : '')
        + (last.failed ? `, ${fmtNumber(last.failed)} failed` : '')
        + (last.missing ? `, ${fmtNumber(last.missing)} in the index but gone from disk` : '')
        + (last.error ? ` (error: ${last.error})` : '') + '.'
      : 'No sweep since the server started.';
    const rows = data.entries.map((e) => h('div', { class: 'settings-field', 'data-tag': e.tag },
      h('div', { class: 'settings-label' }, e.tag),
      h('div', { class: 'settings-control' },
        h('p', { class: 'settings-help' },
          `${fmtNumber(e.matches)} file${e.matches === 1 ? '' : 's'} still match`,
          e.kept_shared ? ` · ${fmtNumber(e.kept_shared)} shared with other characters kept` : '',
          e.include_variants ? ` · with variants (${e.variants.length} tags)` : ' · this tag only',
          e.min_p !== null && e.min_p !== undefined ? ` · min confidence ${e.min_p}` : '',
          ` · added by ${e.added_by || '?'} ${fmtAgo(e.added_at)}`),
        h('p', { class: 'settings-actions' },
          h('button', { type: 'button', class: 'btn btn-quiet', text: 'Remove',
            title: 'Stops trashing new files. Restores nothing.',
            onclick: async () => {
              if (!confirm(`Remove ${e.tag} from the blacklist? Files already trashed stay in the trash.`)) return;
              try { await api('DELETE', `/api/media/characters/blacklist?tag=${encodeURIComponent(e.tag)}`); toast('Removed'); await load(); }
              catch (err) { toast(`Not removed: ${err.message}`, 'error'); }
            } })))));
    fill(listBox, h('h3', {}, 'Blacklisted characters'),
      h('p', { class: 'settings-help' }, sweep + lastText,
        ` Minimum confidence ${data.min_p}, at most ${fmtNumber(data.max_per_sweep)} files per sweep.`),
      rows.length ? rows : h('p', { class: 'settings-help' }, 'The list is empty.'));

    const ex = data.exemptions.map((x) => h('div', { class: 'settings-field' },
      h('div', { class: 'settings-label' }, `${x.root}`),
      h('div', { class: 'settings-control' },
        h('p', { class: 'settings-help' }, x.path, ` · restored ${fmtAgo(x.at)}`),
        h('p', { class: 'settings-actions' },
          h('button', { type: 'button', class: 'btn btn-quiet', text: 'Clear',
            title: 'The next sweep may trash this file again',
            onclick: async () => {
              try {
                await api('DELETE', `/api/media/characters/blacklist/exemptions?root=${encodeURIComponent(x.root)}&path=${encodeURIComponent(x.path)}`);
                await load();
              } catch (err) { toast(`Not cleared: ${err.message}`, 'error'); }
            } })))));
    fill(exemptBox, h('h3', {}, `Exemptions (${data.exemptions.length})`),
      h('p', { class: 'settings-help' },
        'Files restored from a blacklist batch. The sweep never trashes them again until the exemption is cleared.'),
      ex.length ? ex : h('p', { class: 'settings-help' }, 'None.'),
      ex.length > 1 ? h('p', { class: 'settings-actions' }, h('button', { type: 'button', class: 'btn btn-quiet',
        text: 'Clear all',
        onclick: async () => {
          if (!confirm('Clear every exemption? The next sweep may trash those files again.')) return;
          try { await api('DELETE', '/api/media/characters/blacklist/exemptions?all=1'); await load(); }
          catch (err) { toast(`Not cleared: ${err.message}`, 'error'); }
        } })) : null);
  };

  async function load() {
    try {
      draw(await api('GET', '/api/media/characters/blacklist'));
    } catch (err) {
      fill(listBox, h('h3', {}, 'Blacklisted characters'), h('p', { class: 'settings-error' }, err.message));
    }
  }
  await load();
  const poll = () => setTimeout(async () => {
    if (!listBox.isConnected) { dialog.remove(); return; }    // another section was opened
    if (!document.hidden && !dialog.open) await load();
    poll();
  }, 10000);
  poll();
}

// 2D -> 3D (stereo3d.py): the settings form, then the queue with progress and
// the 3D cache. The queue refreshes itself while the section stays open.
const LANE_NAMES = { manual: 'Requested', favorite: 'Favourite', redo: 'Redo after cut' };

function stereo3dStatusText(it) {
  if (it.state === 'failed') return `Failed${it.error ? `: ${it.error}` : ''}`;
  if (it.state === 'queued') {
    const wait = Math.round(Number(it.not_before || 0) - Date.now() / 1000);
    return wait > 0 ? `Queued, trying again in ${wait} s${it.error ? ` (${it.error})` : ''}` : 'Queued';
  }
  return Stereo3d.progressText(it);
}

async function renderStereo3d(view) {
  view.append(schemaForm('stereo3d'));
  const box = card('Conversion queue', h('p', { class: 'settings-help' }, 'Loading…'));
  const cacheBox = card('3D cache');
  view.append(box, cacheBox);

  // One place for "ask, do it, say so, redraw".
  const act = async (button, method, url, body, done) => {
    button.disabled = true;
    try {
      const out = await api(method, url, body);
      toast(done(out));
      await load();
    } catch (err) {
      toast(`Not done: ${err.message}`, 'error');
      button.disabled = false;
    }
  };

  const draw = (ov) => {
    const items = ov.items || [];
    const failed = items.filter((i) => i.state === 'failed');
    const w = ov.worker || {};
    const ws = w.status || {};
    const cache = ov.cache || {};
    const counts = Object.entries(ws.counts || {}).filter(([, n]) => n).map(([k, n]) => `${k} ${n}`).join(', ');

    const rows = items.map((it) => {
      const name = String(it.path || '').split('/').pop() || it.key;
      const buttons = [];
      if (it.state === 'failed') {
        const retry = h('button', { type: 'button', class: 'btn', text: 'Retry' });
        retry.addEventListener('click', () => act(retry, 'POST', '/api/media/3d/retry', { key: it.key },
          (o) => (o.retried ? 'Back in the queue' : 'Nothing to retry')));
        buttons.push(retry);
      }
      const running = !['queued', 'failed'].includes(it.state);
      const remove = h('button', { type: 'button', class: 'btn btn-danger', text: 'Remove' });
      remove.addEventListener('click', () => {
        const what = running ? `Stop the running conversion of ${name} and remove it?` : `Remove ${name} from the queue?`;
        if (!window.confirm(what)) return;
        act(remove, 'DELETE', `/api/media/3d/queue?key=${encodeURIComponent(it.key)}`, null,
          (o) => (o.removed ? 'Removed' : 'It was gone already'));
      });
      buttons.push(remove);
      return h('tr', { class: it.state === 'failed' ? 'is-muted' : null },
        h('td', { class: 'is-wrap', title: it.path || '' }, name),
        h('td', {}, it.kind === 'picture' ? 'Picture' : 'Video'),
        h('td', {}, LANE_NAMES[it.lane] || capitalise(it.lane || '')),
        h('td', { class: 'is-wrap' }, stereo3dStatusText(it)),
        h('td', {}, fmtNumber(it.attempts)),
        h('td', {}, h('div', { class: 'settings-row-actions' }, buttons)));
    });

    const retryAll = h('button', { type: 'button', class: 'btn', text: `Retry ${failed.length} failed` });
    retryAll.addEventListener('click', () => act(retryAll, 'POST', '/api/media/3d/retry', { all: true },
      (o) => `${fmtNumber(o.retried)} back in the queue`));
    const clear = h('button', { type: 'button', class: 'btn btn-danger', text: 'Clear queue' });
    clear.addEventListener('click', () => {
      if (!window.confirm(`Remove all ${items.length} item(s) from the queue? A running conversion is stopped.`)) return;
      act(clear, 'DELETE', '/api/media/3d/queue?all=1', null, (o) => `${fmtNumber(o.removed)} removed`);
    });

    fill(box, h('h3', {}, 'Conversion queue'),
      ov.enabled ? null : h('p', { class: 'settings-help' }, 'Switched off: nothing is started, the queue is kept.'),
      cache.problem ? h('p', { class: 'settings-error' }, cache.problem) : null,
      w.error ? h('p', { class: 'settings-error' },
        `Vision worker: ${w.error}${w.retry_in_s ? ` (next try in ${w.retry_in_s} s)` : ''}`) : null,
      h('div', { class: 'settings-stats' },
        stat('In the queue', fmtNumber(items.length)),
        stat('Failed', fmtNumber(failed.length)),
        stat('Worker', ws.available ? 'ready' : (w.error ? 'not reachable' : (ws.missing ? 'not ready' : '-'))),
        stat('Worker disk free', ws.disk_free_gb != null ? `${fmtNumber(Math.round(ws.disk_free_gb))} GB` : '-')),
      ws.available === false && ws.missing ? h('p', { class: 'settings-error' },
        `The worker cannot convert yet: ${Array.isArray(ws.missing) ? ws.missing.join(', ') : ws.missing}`) : null,
      counts ? h('p', { class: 'settings-help' }, `On the worker: ${counts}${ws.paused ? ' (paused)' : ''}.`) : null,
      rows.length ? h('div', { class: 'settings-table-wrap' },
        h('table', { class: 'settings-table' },
          h('thead', {}, h('tr', {}, ['File', 'Kind', 'Started by', 'Status', 'Attempts', ''].map((t) => h('th', {}, t)))),
          h('tbody', {}, rows)))
        : h('p', { class: 'settings-help' }, 'Nothing is waiting. Admins start a conversion with Convert to 3D '
          + 'in the media library.'),
      h('div', { class: 'settings-row-actions' }, failed.length ? retryAll : null, items.length ? clear : null),
      ov.last_step ? h('p', { class: 'settings-help' }, `Last step: ${ov.last_step}`) : null);

    const wipe = h('button', { type: 'button', class: 'btn btn-danger', text: 'Clear cache', disabled: !cache.entries });
    wipe.addEventListener('click', () => {
      if (!window.confirm(`Delete all ${cache.entries} converted file(s) (${cache.gb} GB)? `
        + 'They have to be converted again before they can be watched.')) return;
      act(wipe, 'DELETE', '/api/settings/3d/cache', null, (o) => `${fmtNumber(o.removed)} conversion(s) deleted`);
    });
    fill(cacheBox, h('h3', {}, '3D cache'),
      h('div', { class: 'settings-stats' },
        stat('Conversions kept', fmtNumber(cache.entries)),
        stat('Size', `${cache.gb != null ? cache.gb : 0} GB`),
        stat('Limit', cache.max_gb ? `${fmtNumber(cache.max_gb)} GB` : '-')),
      h('p', { class: 'settings-help' }, `Folder: ${cache.dir || 'not set'}. Over the limit, the least recently watched `
        + 'conversions go first; favourites of admins last.'),
      wipe);
  };

  async function load() {
    try {
      draw(await api('GET', '/api/settings/3d'));
    } catch (err) {
      fill(box, h('h3', {}, 'Conversion queue'), h('p', { class: 'settings-error' }, err.message));
    }
  }
  await load();
  const poll = () => setTimeout(async () => {
    if (!box.isConnected) return;              // another section was opened
    await load();
    poll();
  }, 5000);
  poll();
}

// Optional modules (module_manager.py, MODULE_SYSTEM_PLAN.md): browser-only feature folders such as the music
// visualizer. Installed ones live in web_files/js/modules/; the online repo's list is signed, and the server checks
// the signature and every file before it installs anything.
async function renderModules(view) {
  const box = card('Modules', h('p', { class: 'settings-help' }, 'Loading…'));
  view.append(box);
  const act = async (button, method, url, body, done) => {
    button.disabled = true;
    try {
      const out = await api(method, url, body);
      toast(done(out));
      draw(out.overview);
    } catch (err) {
      toast(`Not done: ${err.message}`, 'error');
      button.disabled = false;
    }
  };
  const draw = (ov) => {
    const rows = (ov.modules || []).map((m) => {
      const buttons = [];
      const from = m.available ? 'online' : (m.local ? 'local' : null);
      const ver = m.available || m.local;
      if (!m.installed && from) {
        const b = h('button', { type: 'button', class: 'btn', text: `Install ${ver}` });
        b.addEventListener('click', () => act(b, 'POST', `/api/modules/${m.id}/install`, { source: from },
          (o) => `${m.name} ${o.installed} installed`));
        buttons.push(b);
      }
      if (m.installed && (m.update || m.broken) && from) {
        const b = h('button', { type: 'button', class: 'btn', text: m.broken ? `Repair (${ver})` : `Update to ${ver}` });
        b.addEventListener('click', () => act(b, 'POST', `/api/modules/${m.id}/install`, { source: from },
          (o) => `${m.name} ${o.installed} installed`));
        buttons.push(b);
      }
      if (m.installed || m.broken) {
        const b = h('button', { type: 'button', class: 'btn btn-danger', text: 'Remove' });
        b.addEventListener('click', () => {
          if (!window.confirm(`Remove the module "${m.name}"? Its feature disappears for everyone; it can be installed again.`)) return;
          act(b, 'DELETE', `/api/modules/${m.id}`, null, () => `${m.name} removed`);
        });
        buttons.push(b);
      }
      const state = m.broken ? 'broken' : m.installed ? `installed ${m.installed}${m.update ? ' (update available)' : ''}` : 'not installed';
      return h('tr', { class: m.installed ? null : 'is-muted' },
        h('td', { style: 'white-space: normal; min-width: 14rem' }, h('strong', {}, m.name),
          h('div', { class: m.broken ? 'settings-error' : 'settings-help' }, m.broken || m.description)),
        h('td', {}, state),
        h('td', {}, m.size ? fmtBytes(m.size) : '-'),
        h('td', {}, h('div', { class: 'settings-row-actions' }, buttons)));
    });
    const refresh = h('button', { type: 'button', class: 'btn', text: 'Check the online repo again' });
    refresh.addEventListener('click', async () => {
      refresh.disabled = true;
      try { draw(await api('GET', '/api/modules?refresh=1')); } catch (err) { toast(`Not done: ${err.message}`, 'error'); refresh.disabled = false; }
    });
    fill(box, h('h3', {}, 'Modules'),
      h('p', { class: 'settings-help' },
        'Optional features that run in the browser. Installing one adds it for everyone on this server; ',
        'online modules come from ', h('code', {}, `${ov.repo} (${ov.branch})`),
        ' and are only installed when their signature and every file check out.'),
      ov.online_error ? h('p', { class: 'settings-error' }, `Online repo: ${ov.online_error}`) : null,
      rows.length ? h('div', { class: 'settings-table-wrap' },
        h('table', { class: 'settings-table' },
          h('thead', {}, h('tr', {}, h('th', {}, 'Module'), h('th', {}, 'State'), h('th', {}, 'Size'), h('th', {}, ''))),
          h('tbody', {}, rows)))
        : h('p', { class: 'settings-help' }, 'No modules installed or available.'),
      h('div', { class: 'settings-row-actions' }, refresh));
  };
  draw(await api('GET', '/api/modules'));
}

const RENDERERS = {
  general: renderGeneral,
  appearance: renderAppearance,
  users: renderUsers,
  shares: renderShares,
  downloads: renderDownloads,
  'image-processing': renderImageProcessing,
  'live-upscaling': renderLiveUpscaling,
  'song-covers': renderSongCovers,
  'video-trim': renderVideoTrim,
  'character-blacklist': renderCharacterBlacklist,
  stereo3d: renderStereo3d,
  'vision-worker': renderVisionWorker,
  modules: renderModules,
};

// ---------------------------------------------------------------- router --

function currentSection() {
  const id = (window.location.hash || '#general').slice(1);
  return SECTIONS.find((s) => s.id === id) || SECTIONS[0];
}

async function route() {
  const seq = ++routeSeq;
  const section = currentSection();
  document.querySelectorAll('#settingsNav a').forEach((a) =>
    a.setAttribute('aria-current', String(a.dataset.section === section.id)));
  document.title = `${section.title} · Settings`;
  const main = document.getElementById('settingsMain');
  const view = h('div', { class: 'settings-view' });
  main.replaceChildren(h('h2', { class: 'settings-title' }, section.title), view);
  try {
    if (!settingsData) settingsData = await api('GET', '/api/settings');
    if (seq !== routeSeq) return;         // another section was picked meanwhile
    await RENDERERS[section.id](view);
  } catch (err) {
    if (seq !== routeSeq) return;
    view.append(h('section', { class: 'settings-card' },
      h('p', { class: 'settings-error' },
        err.status === 403 ? 'Settings are for admins.' : `Could not load: ${err.message}`)));
  }
}

function buildNav() {
  const nav = document.getElementById('settingsNav');
  let group = null;
  for (const section of SECTIONS) {
    if (section.group && section.group !== group) {
      group = section.group;
      nav.append(h('div', { class: 'settings-nav-group' }, group));
    }
    nav.append(h('a', { href: `#${section.id}`, 'data-section': section.id, class: section.group ? 'is-sub' : null },
      section.title));
  }
}

buildNav();
window.addEventListener('hashchange', route);
route();
