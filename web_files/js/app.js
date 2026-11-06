function escHtml(v) { return String(v == null ? '' : v).replace(/[&<>"'`]/g, (c) => '&#' + c.charCodeAt(0) + ';'); }
let currentTasks = {};
let activeModalTaskId = null;
let advancedMode = false;

// The download-box card is a single DOM node with a live form + listener;
// switching modes moves it between its normal-layout slot and the compact
// top-left slot in the advanced layout, rather than duplicating the form.
function applyAdvancedModeLayout() {
  document.body.classList.toggle('advanced-mode', advancedMode);

  const downloadBox = document.getElementById('downloadBoxCard');
  const advSlot = document.getElementById('advDownloadSlot');
  const dashboardGrid = document.querySelector('.dashboard-grid');

  if (advancedMode) {
    advSlot.appendChild(downloadBox);
  } else {
    dashboardGrid.insertBefore(downloadBox, dashboardGrid.firstChild);
  }
}

async function initPrefs() {
  try {
    const res = await fetch('/api/prefs');
    if (!res.ok) return;
    const prefs = await res.json();
    advancedMode = !!prefs.advanced_mode;
    document.getElementById('advancedModeToggle').checked = advancedMode;
    applyAdvancedModeLayout();
  } catch (err) {}
}

document.getElementById('advancedModeToggle').addEventListener('change', async (e) => {
  advancedMode = e.target.checked;
  applyAdvancedModeLayout();
  try {
    await fetch('/api/prefs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ advanced_mode: advancedMode })
    });
  } catch (err) {}
  if (advancedMode) fetchDashboardData();
});

document.getElementById('downloadForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('urlInput');
  const url = input.value.trim();
  if (!url) return;

  const res = await fetch('/api/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url })
  });

  if (res.ok) {
    input.value = '';
    fetchDashboardData();
  } else {
    const err = await res.json();
    alert(err.error || 'Failed to launch session');
  }
});

async function fetchDashboardData() {
  try {
    const fetches = [fetch('/api/tasks')];
    if (advancedMode) {
      fetches.push(fetch('/api/pipes'));
      fetches.push(fetch('/api/system-stats'));
    }

    const [tasksRes, pipesRes, statsRes] = await Promise.all(fetches);

    if (tasksRes.status === 401) {
      window.location.reload();
      return;
    }

    const tasks = await tasksRes.json();

    currentTasks = {};
    tasks.forEach(t => { currentTasks[t.id] = t; });

    renderTasks(tasks);

    if (advancedMode && pipesRes) {
      const pipes = await pipesRes.json();
      renderPipes(pipes);
    }

    if (advancedMode && statsRes) {
      const stats = await statsRes.json();
      renderStats(stats);
    }

    if (activeModalTaskId && currentTasks[activeModalTaskId]) {
      updateModalLogContent(activeModalTaskId);
    }
  } catch (err) {}
}

function formatBytes(bytes) {
  if (!bytes) return '0 MB';
  const gb = bytes / (1024 ** 3);
  if (gb >= 1) return gb.toFixed(2) + ' GB';
  return (bytes / (1024 ** 2)).toFixed(1) + ' MB';
}

function formatDiskUsage(usedGb, totalGb) {
  const useTB = usedGb >= 1024 || totalGb >= 1024;
  if (useTB) {
    return `${(usedGb / 1024).toFixed(2)} / ${(totalGb / 1024).toFixed(2)} TB`;
  }
  return `${usedGb.toFixed(1)} / ${totalGb.toFixed(1)} GB`;
}

function formatBitrate(mBps) {
  const kbit = mBps * 1024 * 8; // MiB/s -> bits/s -> Kbit/s (decimal, standard network convention)
  if (kbit >= 1000) {
    return (kbit / 1000).toFixed(2) + ' Mbit/s';
  }
  return kbit.toFixed(1) + ' Kbit/s';
}

function renderStats(s) {
  const grid = document.getElementById('statsGrid');
  if (!grid) return;

  const diskUsedGb = s.disk_total_gb - s.disk_free_gb;

  const tiles = [
    ['CPU', `${s.cpu_percent}%`],
    ['RAM', `${s.ram_used_gb} / ${s.ram_total_gb} GB`],
    ['RAM %', `${s.ram_percent}%`],
    ['Net ↓', formatBitrate(s.net_recv_mbps)],
    ['Net ↑', formatBitrate(s.net_sent_mbps)],
    ['Items (60min)', `${s.items_last_60min}`],
    ['Downloaded (24h)', formatBytes(s.downloaded_bytes_24h)],
    ['Disk Usage', formatDiskUsage(diskUsedGb, s.disk_total_gb)],
    ['Disk Used', `${s.disk_used_percent}%`]
  ];

  grid.innerHTML = tiles.map(([label, value]) => `
    <div class="stat-tile">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${value}</div>
    </div>
  `).join('');
}

// Persistent per-category columns: renderPipes only ever reorders/updates
// existing DOM nodes (keyed by category / task id) instead of rebuilding
// everything, so a lightweight FLIP animation can slide tasks into place
// whenever a task finishes and the next one becomes current.
function renderPipes(pipes) {
  const container = document.getElementById('pipeList');
  if (!container) return;

  const sortedCats = Object.keys(pipes).sort((a, b) => (pipes[b].weekly_usage || 0) - (pipes[a].weekly_usage || 0));

  const existingCols = {};
  container.querySelectorAll('.pipe-col').forEach(el => { existingCols[el.dataset.cat] = el; });

  sortedCats.forEach((cat, idx) => {
    let col = existingCols[cat];
    if (!col) {
      col = document.createElement('div');
      col.className = 'pipe-col';
      col.dataset.cat = cat;
      col.innerHTML = `
        <div class="pipe-col-header">
          <strong>${cat.toUpperCase()}</strong>
          <button class="logout-btn pipe-purge-btn" style="padding:0.2rem 0.5rem; font-size:0.7rem;">Purge</button>
        </div>
        <div class="pipe-col-meta"></div>
        <div class="pipe-stack"></div>
      `;
      col.querySelector('.pipe-purge-btn').addEventListener('click', () => purgePipe(cat));
      container.appendChild(col);
      existingCols[cat] = col;
    }
    col.style.order = idx;

    const p = pipes[cat];
    col.querySelector('.pipe-col-meta').textContent =
      `Queue: ${p.queued} | ${p.rate_mbps} MiB/s | ${p.items_per_hr}/h | ${p.weekly_usage}x this week`;

    renderPipeStack(col.querySelector('.pipe-stack'), p);
  });

  Object.keys(existingCols).forEach(cat => {
    if (!sortedCats.includes(cat)) existingCols[cat].remove();
  });
}

function formatElapsed(startEpochSeconds) {
  if (!startEpochSeconds) return null;
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - startEpochSeconds));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

function renderPipeStack(stackEl, p) {
  const items = [];
  if (p.current_task) items.push(Object.assign({ _role: 'current' }, p.current_task));
  (p.upcoming_tasks || []).forEach(t => items.push(Object.assign({ _role: 'upcoming' }, t)));

  // FIRST: capture current on-screen positions before touching the DOM.
  const oldRects = {};
  stackEl.querySelectorAll('[data-task-id]').forEach(el => {
    oldRects[el.dataset.taskId] = el.getBoundingClientRect();
  });

  const newIds = new Set(items.map(t => t.id));
  stackEl.querySelectorAll('[data-task-id]').forEach(el => {
    if (!newIds.has(el.dataset.taskId)) el.remove();
  });

  if (items.length === 0) {
    stackEl.innerHTML = '<div class="pipe-empty">Idle</div>';
  } else {
    const emptyEl = stackEl.querySelector('.pipe-empty');
    if (emptyEl) emptyEl.remove();
  }

  const newlyCreated = [];
  items.forEach(t => {
    let el = stackEl.querySelector(`[data-task-id="${t.id}"]`);
    if (!el) {
      el = document.createElement('div');
      el.dataset.taskId = t.id;
      newlyCreated.push(el);
    }
    el.className = t._role === 'current' ? 'pipe-current-task' : 'pipe-upcoming-item';
    if (t._role === 'current') {
      const elapsed = formatElapsed(t.started_at);
      el.innerHTML = `
        <div class="pipe-task-label">▶ ${escHtml(t.progress || 'Running')}</div>
        <div class="pipe-task-url">${escHtml(t.url)}</div>
        ${elapsed ? `<div class="pipe-task-elapsed">running for ${elapsed}</div>` : ''}
        <button class="pipe-terminate-btn">Terminate</button>`;
    } else {
      const queuedSince = formatElapsed(t.created_at);
      el.innerHTML = `
        <div class="pipe-task-url">${escHtml(t.url)}</div>
        ${queuedSince ? `<div class="pipe-task-elapsed">queued for ${queuedSince}</div>` : ''}`;
    }
    el.onclick = () => openLogModal(t.id);
    if (t._role === 'current') {
      el.querySelector('.pipe-terminate-btn').addEventListener('click', (ev) => {
        ev.stopPropagation();
        terminateTask(t.id);
      });
    }
    stackEl.appendChild(el); // re-appending an existing node just moves it into place
  });

  newlyCreated.forEach(el => {
    el.style.transform = 'translateY(16px)';
    el.style.opacity = '0';
  });

  // LAST + INVERT + PLAY: diff old vs new position and animate the delta away.
  requestAnimationFrame(() => {
    stackEl.querySelectorAll('[data-task-id]').forEach(el => {
      const id = el.dataset.taskId;
      const newRect = el.getBoundingClientRect();
      const old = oldRects[id];

      if (old) {
        const dx = old.left - newRect.left;
        const dy = old.top - newRect.top;
        if (dx || dy) {
          el.style.transition = 'none';
          el.style.transform = `translate(${dx}px, ${dy}px)`;
        }
      }

      requestAnimationFrame(() => {
        el.style.transition = 'transform 0.35s ease, opacity 0.35s ease';
        el.style.transform = '';
        el.style.opacity = '1';
      });
    });
  });
}

function renderTasks(tasks) {
  const container = document.getElementById('taskList');
  if (!container) return;
  container.innerHTML = '';

  tasks.slice().reverse().forEach(task => {
    const card = document.createElement('div');
    card.className = 'task-card';

    const prioritizeBtn = task.status === 'Queued'
      ? `<button onclick="event.stopPropagation(); prioritizeTask('${task.id}')" style="background:#10b981; border:none; color:#000; padding:0.2rem 0.4rem; border-radius:4px; font-weight:bold; cursor:pointer;">↑ Next</button>`
      : '';

    const terminateBtn = task.status === 'Running' || task.status === 'Queued'
      ? `<button onclick="event.stopPropagation(); terminateTask('${task.id}')" style="background:#f43f5e; border:none; color:#fff; padding:0.2rem 0.4rem; border-radius:4px; font-weight:bold; cursor:pointer;">Terminate</button>`
      : '';

    const filesBtn = task.media_path
      ? `<button class="view-files-btn" style="background:#334155; border:none; color:#f8fafc; padding:0.2rem 0.4rem; border-radius:4px; font-weight:bold; cursor:pointer;">📁 Files</button>`
      : '';

    // A failed or terminated task is the one case where the URL is worth
    // resubmitting as-is - queue it again without retyping it.
    const rerunBtn = (task.status === 'Failed' || task.status === 'Terminated')
      ? `<button class="rerun-btn" style="background:#38bdf8; border:none; color:#000; padding:0.2rem 0.4rem; border-radius:4px; font-weight:bold; cursor:pointer;">↻ Re-run</button>`
      : '';

    card.onclick = () => openLogModal(task.id);
    card.innerHTML = `
      <div class="task-header">
        <span class="badge badge-${escHtml(task.category)}">${escHtml(task.category)}</span>
        <span>${escHtml(task.status)}</span>
      </div>
      <div style="font-size:0.8rem; word-break:break-all; margin: 0.3rem 0; color: #cbd5e1;">${escHtml(task.url)}</div>
      <div class="task-progress">${escHtml(task.progress)}</div>
      <div class="task-skips" hidden></div>
      <div style="display:flex; justify-content:space-between; align-items:center; margin-top:0.5rem;">
        <span style="font-size:0.75rem; color:#38bdf8; text-decoration:underline;">View Logs</span>
        <div style="display:flex; gap:0.5rem;">${filesBtn} ${rerunBtn} ${prioritizeBtn} ${terminateBtn}</div>
      </div>
    `;

    const skipped = Object.entries(task.skipped || {});
    // A finished task's progress already names its skips (download_outcome in
    // server.py); this line is for a run still going, or one that was stopped.
    if (skipped.length && !String(task.progress).includes('skipped:')) {
      // As text: the reasons are the server's own keys, but nothing that comes
      // out of a downloader belongs in innerHTML.
      const box = card.querySelector('.task-skips');
      const signedOut = task.cookies_signed_out && (task.skipped.age_restricted || task.skipped.members_only);
      box.textContent = 'Skipped: ' + skipped
        .sort((a, b) => b[1] - a[1])
        .map(([reason, n]) => `${n} ${reason === 'error' ? (n === 1 ? 'other error' : 'other errors') : (SKIP_LABELS[reason] || reason)}`)
        .join(' · ') + (signedOut ? ' - YouTube cookies are signed out' : '');
      box.title = 'Skipped videos are not marked as downloaded: running the same link again fetches only them.';
      box.hidden = false;
    }

    if (task.media_path) {
      // Set via dataset + a real listener (not string-interpolated onclick)
      // since media_path is derived from external metadata (channel/artist
      // names) the user doesn't control - keeps it out of any JS/HTML
      // injection context.
      const btn = card.querySelector('.view-files-btn');
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        window.open(`/media.html?path=${encodeURIComponent(task.media_path)}`, '_blank');
      });
    }

    if (rerunBtn) {
      // Real listener rather than an inline onclick: the URL is arbitrary
      // user input and would otherwise be interpolated into an HTML
      // attribute (quotes in a URL would break out of it).
      const btn = card.querySelector('.rerun-btn');
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        rerunTask(task.url, btn);
      });
    }

    container.appendChild(card);
  });
}

async function terminateTask(taskId) {
  await fetch(`/api/task/${taskId}/terminate`, { method: 'POST' });
  fetchDashboardData();
}

// Resubmits a failed/terminated task's URL as a fresh download. The old
// card is left alone - it ages out on its own, and keeping it means the
// logs explaining the failure stay readable while the retry runs.
async function rerunTask(url, btn) {
  if (btn) {
    // The polling refresh rebuilds these cards every second, so this only
    // guards against double-clicks within that window - enough to stop a
    // stray second submission.
    btn.disabled = true;
    btn.textContent = '…';
  }
  try {
    await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
  } catch (e) {
    // Network failure - the next poll restores the button anyway.
  }
  fetchDashboardData();
}

async function prioritizeTask(taskId) {
  await fetch(`/api/task/${taskId}/prioritize`, { method: 'POST' });
  fetchDashboardData();
}

async function purgePipe(category) {
  await fetch(`/api/pipe/${category}/purge`, { method: 'POST' });
  fetchDashboardData();
}

function updateModalLogContent(taskId) {
  const task = currentTasks[taskId];
  if (!task) return;
  const logBody = document.getElementById('modalLogOutput');
  const newText = task.logs && task.logs.length > 0 ? task.logs.join('\n') : 'No logs available yet...';

  // Only auto-scroll if the user was already looking at the bottom (latest
  // logs) — otherwise a scroll-up-to-read-history gets yanked back down
  // every second by the polling refresh.
  const wasNearBottom = logBody.scrollHeight - logBody.scrollTop - logBody.clientHeight < 40;

  logBody.innerText = newText;

  if (wasNearBottom) {
    logBody.scrollTop = logBody.scrollHeight;
  }
}

function openLogModal(taskId) {
  activeModalTaskId = taskId;
  document.getElementById('logModal').style.display = 'flex';
  updateModalLogContent(taskId);
  const logBody = document.getElementById('modalLogOutput');
  logBody.scrollTop = logBody.scrollHeight;
}

function closeLogModal() {
  activeModalTaskId = null;
  document.getElementById('logModal').style.display = 'none';
}

// -------------------------------------------------------------------
// -------------------------------------------------------------------

/* ---------- what the refresh must not take away ---------- */
/*
 * Every render replaces whole containers with innerHTML, and the browser drops
 * everything it was holding in them: the text you had selected, how far you
 * had scrolled inside a table, where the caret was, which <details> you had
 * opened. At a two second interval that made parts of the page unusable - you
 * could not finish selecting an address before it was taken away.
 *
 * Rather than rewrite every render (and risk binding a handler twice to a
 * surviving node), this watches what you are doing and repairs the damage
 * afterwards. Two halves:
 *
 *   Selecting text pauses painting. While a selection is live the tick is
 *   skipped entirely, because there is no way to replace the node under a
 *   selection without destroying it. Clicking anywhere resumes.
 *
 *   Everything else is remembered and repaired. Scroll offsets, the focused
 *   field and its caret, and open <details> are recorded as you use them, and
 *   after each tick anything the render reset is put back. It repairs rather
 *   than forces: if a value was NOT reset, it is left alone, so this can never
 *   fight you for the scrollbar.
 */
const UIMemory = {
  scroll: new Map(),        // path -> {top, left, atBottom}
  open: new Map(),          // path -> boolean
  focus: null,              // {path, start, end}
  paused: false,

  // A path the same element can be found by after its parent was rebuilt: an
  // id when there is one, otherwise the position among siblings.
  path(node) {
    if (!node || node.nodeType !== 1) return "";
    const parts = [];
    for (let n = node; n && n !== document.documentElement; n = n.parentElement) {
      if (n.id) { parts.push("#" + n.id); break; }
      const p = n.parentElement;
      if (!p) break;
      parts.push(String(Array.prototype.indexOf.call(p.children, n)));
      if (parts.length > 30) return "";        // absurdly deep: not worth tracking
    }
    return parts.reverse().join("/");
  },

  find(path) {
    if (!path) return null;
    let node = document.documentElement;
    for (const part of path.split("/")) {
      if (!node) return null;
      if (part.charAt(0) === "#") { node = document.getElementById(part.slice(1)); continue; }
      node = node.children[Number(part)];
    }
    return node || null;
  },

  selectionLive() {
    const sel = document.getSelection ? document.getSelection() : null;
    return !!(sel && sel.rangeCount && !sel.isCollapsed && String(sel).length);
  },

  watch() {
    // scroll does not bubble, so listen in the capture phase.
    document.addEventListener("scroll", (e) => {
      const el = e.target;
      if (!el || el === document || el.nodeType !== 1) return;
      const path = this.path(el);
      if (!path) return;
      this.scroll.set(path, {
        top: el.scrollTop, left: el.scrollLeft,
        // Following live output: stay at the end rather than at an offset that
        // stops meaning the end as soon as a line is added.
        atBottom: el.scrollHeight - el.scrollTop - el.clientHeight < 8 && el.scrollTop > 0,
      });
      if (this.scroll.size > 200) this.scroll.delete(this.scroll.keys().next().value);
    }, true);

    document.addEventListener("focusin", (e) => {
      const el = e.target;
      if (!el || el.nodeType !== 1) { this.focus = null; return; }
      const path = this.path(el);
      this.focus = path ? {
        path,
        start: typeof el.selectionStart === "number" ? el.selectionStart : null,
        end: typeof el.selectionEnd === "number" ? el.selectionEnd : null,
      } : null;
    });
    document.addEventListener("focusout", () => { this.focus = null; });

    document.addEventListener("toggle", (e) => {
      const el = e.target;
      if (!el || el.tagName !== "DETAILS") return;
      const path = this.path(el);
      if (path) this.open.set(path, el.open);
    }, true);
  },

  // Put back only what the render actually reset. A value the user changed in
  // the meantime is already the newer one in these maps, so this never fights.
  repair() {
    for (const [path, want] of this.scroll) {
      const el = this.find(path);
      if (!el) { this.scroll.delete(path); continue; }
      if (want.atBottom) {
        const end = el.scrollHeight - el.clientHeight;
        if (end > 0 && el.scrollTop < end - 8) el.scrollTop = end;
        continue;
      }
      if (el.scrollTop === 0 && want.top > 0 && el.scrollHeight > el.clientHeight) el.scrollTop = want.top;
      if (el.scrollLeft === 0 && want.left > 0) el.scrollLeft = want.left;
    }
    for (const [path, open] of this.open) {
      const el = this.find(path);
      if (!el || el.tagName !== "DETAILS") { this.open.delete(path); continue; }
      if (el.open !== open) el.open = open;
    }
    const f = this.focus;
    if (f && (document.activeElement === document.body || document.activeElement === null)) {
      const el = this.find(f.path);
      if (el && typeof el.focus === "function") {
        el.focus({ preventScroll: true });
        if (f.start != null && el.setSelectionRange) {
          try { el.setSelectionRange(f.start, f.end); } catch (err) { /* not a text field */ }
        }
      }
    }
  },
};

UIMemory.setPaused = function (on) {
  if (this.paused === on) return;
  this.paused = on;
  let chip = document.getElementById("ui-paused");
  if (!on) { if (chip) chip.remove(); return; }
  if (!chip) {
    chip = document.createElement("div");
    chip.id = "ui-paused";
    chip.className = "ui-paused";
    chip.textContent = "paused while text is selected";
    document.body.appendChild(chip);
  }
};

UIMemory.watch();


// A poll that never overlaps itself, never paints over a selection, and puts
// back what it resets. `pollBusy` matters on a slow link: at one second the
// old interval could stack requests on top of each other.

/* ---------------------------------------------------------------------------
   Library indexing progress
   ---------------------------------------------------------------------------
   Four passes run over the same library at different speeds and in whatever
   order was chosen, so "percent done" is four numbers rather than one. Each is
   measured against the WHOLE library, not against what has been looked at -
   the latter always reads 100% and tells you nothing.
   ------------------------------------------------------------------------ */

const AI_PASSES = [
  ['captioned', 'Descriptions', 'what is in the picture'],
  ['ocr', 'Text in images', 'words found inside pictures'],
  ['tagged', 'Tags & characters', 'danbooru tags and character names'],
  ['faces_files', 'Faces', 'files with a detected face'],
];

function aiEsc(v) {
  return String(v == null ? '' : v).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

let aiProgressAt = 0;
async function refreshAiProgress() {
  const box = document.getElementById('aiProgress');
  // offsetParent is null when the panel is in the hidden simple-mode layout;
  // polling a database aggregate for a panel nobody is looking at is waste.
  if (!box || box.offsetParent === null) return;
  const now = Date.now();
  if (now - aiProgressAt < 5000) return;      // the counts move slowly
  aiProgressAt = now;
  try {
    const res = await fetch('/api/media/indexing');
    if (!res.ok) throw new Error(res.status);
    renderAiProgress(box, await res.json());
  } catch (e) {
    box.innerHTML = '<div class="ai-progress-note">indexing status unavailable</div>';
  }
}

// A pass is done for a file once it RAN on it, found something or not.
const AI_PASS_DONE = { captioned: 'caption_done', ocr: 'ocr_done',
                       tagged: 'tags_done', faces_files: 'faces_done' };

function renderAiProgress(box, d) {
  // Files a pass can process, trash excluded. library_files counts music,
  // tombstones and sidecars too, so bars measured against it never filled.
  const total = d.indexable || d.library_files || 0;
  const state = d.state || (d.running ? 'indexing' : 'stopped');
  const rows = AI_PASSES.map(([key, label, hint]) => {
    const attempt = AI_PASS_DONE[key];
    const done = (typeof d[attempt] === 'number' ? d[attempt] : d[key]) || 0;
    const pct = total ? Math.min(100, done * 100 / total) : 0;
    const active = state === 'indexing' && key === ({
      'caption,ocr': 'captioned', caption: 'captioned', ocr: 'ocr',
      tags: 'tagged', faces: 'faces_files' }[d.current_task]);
    return `<div class="ai-pass${active ? ' ai-pass-active' : ''}">
        <div class="ai-pass-head">
          <span class="ai-pass-name" title="${aiEsc(hint)}">${aiEsc(label)}</span>
          <span class="ai-pass-num">${done.toLocaleString('en-GB')} / ${total.toLocaleString('en-GB')}</span>
        </div>
        <div class="ai-bar"><div class="ai-bar-fill${done ? '' : ' ai-bar-empty'}" style="width:${pct.toFixed(2)}%"></div></div>
      </div>`;
  });

  const w = d.worker || {};
  const bits = [
    state === 'indexing' ? `indexing <b>${aiEsc(d.current_task || '?')}</b>`
      : state === 'idle' ? `idle: nothing left for <b>${aiEsc(d.current_task || '?')}</b>, checking for new files every few minutes`
      : state === 'waiting' ? 'waiting for the worker\'s processing window'
      : 'indexer stopped'];
  if (!w.reachable) bits.push('<b>worker unreachable</b>');
  else if (w.loaded && w.loaded.length) bits.push(`model: ${w.loaded.map(aiEsc).join(', ')}`);
  else bits.push('models unloaded (idle)');
  // The worker's lifetime average mixes every pass it ever ran, and a
  // description takes several times as long as a tag - so the running pass's
  // own rate, measured over its current sweep, is what the estimate uses.
  const sw = d.sweep || {};
  const elapsed = (sw.at || 0) - (sw.started || 0);
  const sweepRate = state === 'indexing' && elapsed > 60
    ? ((sw.done || 0) + (sw.failed || 0)) / elapsed : 0;
  if (w.seconds_per_image || sweepRate) {
    const rate = sweepRate || 1 / w.seconds_per_image;
    bits.push(`${rate.toFixed(1)} files/s`);
    // What people actually want is not the rate but when it ends.
    // Only what the running sweep still has queued is work left.
    const left = state === 'indexing' ? (d.left || 0) : 0;
    if (left && rate > 0) {
      const hours = left / rate / 3600;
      // Work, not wall-clock: the passes take turns, so it ends later.
      bits.push(hours >= 1 ? `~${hours.toFixed(1)} h of work left in this pass`
                           : `~${Math.round(hours * 60)} min of work left in this pass`);
    }
  }
  if (w.in_window === false) bits.push(`outside window (${aiEsc(w.windows || '')})`);

  const found = [];
  if (d.characters) found.push(`${d.characters.toLocaleString('en-GB')} characters`);
  if (d.people) found.push(`${d.people.toLocaleString('en-GB')} people`);
  if (d.faces) found.push(`${d.faces.toLocaleString('en-GB')} faces`);

  box.innerHTML = rows.join('')
    + `<div class="ai-progress-note">${bits.join(' · ')}</div>`
    + (found.length ? `<div class="ai-progress-note">${found.join(' · ')}</div>` : '');
}

let pollBusy = false;
setInterval(async () => {
  if (pollBusy) return;
  if (UIMemory.selectionLive()) { UIMemory.setPaused(true); return; }
  UIMemory.setPaused(false);
  pollBusy = true;
  try {
    await fetchDashboardData();
    // Throttled inside; the download poll is the only clock on this page.
    await refreshAiProgress();
  } finally {
    pollBusy = false;
  }
  UIMemory.repair();
}, 1000);
initPrefs().then(fetchDashboardData);

// Why yt-dlp gave up on a video (server.py YOUTUBE_SKIP_LABELS).
const SKIP_LABELS = {
  age_restricted: 'age-restricted', members_only: 'members-only', private: 'private',
  geo_blocked: 'blocked in this country', not_released: 'not released yet', unavailable: 'unavailable',
};

// yt-dlp never says when the YouTube cookies have stopped being signed in -
// age-restricted and members-only videos just fail - so the dashboard does.
async function checkYoutubeCookies() {
  const box = document.getElementById('cookieWarning');
  if (!box) return;
  try {
    const res = await fetch('/api/downloads/cookies');
    if (!res.ok) return;
    const yt = (await res.json()).youtube || {};
    box.hidden = !(yt.present && !yt.signed_in);
    if (box.hidden) return;
    const link = document.createElement('a');
    link.href = '/settings.html#downloads';
    link.textContent = 'How to fix';
    box.replaceChildren('YouTube cookies are signed out - age-restricted and members-only videos will be skipped. ', link);
  } catch (e) {
    // The next check tries again.
  }
}

checkYoutubeCookies();
setInterval(checkYoutubeCookies, 60000);
