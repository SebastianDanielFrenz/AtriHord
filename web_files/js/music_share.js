/* The viewer a shared music link opens (music_share.html, /share/<token>).
 *
 * A music link is an ordinary POOL share in media-dl's own store - the same
 * tokens, expiry, revoking and view counts, and it is managed on the Shares
 * page like every other link (user, 2026-09-23). Only `view: "music"` marks
 * it, and that is what makes /share/<token> send this page instead of the
 * file viewer.
 *
 * It shows the link the way the music page shows the same thing to someone
 * signed in (user, 2026-09-23: "single song shares should be shown like a
 * normal user would see a single song play. same for playlists"): a playlist,
 * an album, an artist or a style as that page's header and rows, and a song
 * straight on the now-playing screen - which every link has, opened from the
 * bar as on the page, with its queue beside the cover. Same classes as the
 * page, so it cannot drift from it; only what needs the library is missing.
 *
 * Everything here is public: no account, no library, no playlists. The token
 * in the URL is the whole of the access control, so this page asks for
 * GET /api/public/share/<token>/music for the songs, the per-song file and
 * cover URLs that answer carries, and the token's own upscale routes.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const TOUCH = window.matchMedia('(hover: none)');
  const TOKEN = decodeURIComponent((location.pathname.match(/\/share\/([^/?#]+)/) || [, ''])[1] || '');

  // The music page's own glyphs (music.js ICONS), for the same buttons.
  const ICONS = {
    play: 'M8 5v14l11-7z',
    pause: 'M6 5h4v14H6zM14 5h4v14h-4z',
    prev: 'M6 6h2v12H6zm3.5 6l8.5 6V6z',
    next: 'M6 18l8.5-6L6 6v12zm10-12v12h2V6h-2z',
    shuffle: 'M10.6 9.2L5.4 4 4 5.4l5.2 5.2 1.4-1.4zM14.5 4l2 2L4 18.6 5.4 20 18 7.5l2 2V4h-5.5zm.3 9.4l-1.4 1.4 3.1 3.1L14.5 20H20v-5.5l-2 2-3.2-3.1z',
    volume: 'M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z',
    mute: 'M16.5 12A4.5 4.5 0 0 0 14 8v2.2l2.5 2.5V12zM19 12c0 .9-.2 1.8-.5 2.6l1.5 1.5A8.9 8.9 0 0 0 21 12a9 9 0 0 0-7-8.8v2.1a7 7 0 0 1 5 6.7zM4.3 3L3 4.3 7.7 9H3v6h4l5 5v-6.7l4.3 4.3a7 7 0 0 1-2.3 1.2v2.1a9 9 0 0 0 3.7-1.8l2 2 1.3-1.3-9-9L4.3 3zM12 4L9.9 6.1 12 8.2V4z',
    download: 'M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z',
    expand: 'M7.4 15.4 12 10.8l4.6 4.6L18 14l-6-6-6 6z',
    collapse: 'M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z',
    fullscreen: 'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
    fullscreenExit: 'M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z',
  };

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('fill', 'currentColor');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', ICONS[name] || '');
    svg.appendChild(path);
    return svg;
  }

  function setIcon(btn, name) {
    btn.replaceChildren(icon(name));
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function fmtTime(s) {
    if (!Number.isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  }

  // The page's "5 songs • 21 min" / "1 hr 5 min".
  function fmtTotal(s) {
    const m = Math.round((s || 0) / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} hr ${m % 60} min`;
  }

  function plural(n, word) {
    return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
  }

  // ---- the ambient colour, as the music page and media-dl's player make it:
  // the average of the cover's opaque pixels, lifted in saturation; music.css
  // sets the lightness per theme. It paints the now-playing screen only - the
  // page behind the rows keeps its own colour, as the music page's does.
  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h = 0;
    const l = (max + min) / 2;
    const d = max - min;
    const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
    if (d !== 0) {
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60;
      if (h < 0) h += 360;
    }
    return [h, s, l];
  }

  function ambientFrom(img) {
    try {
      const c = document.createElement('canvas');
      c.width = 16; c.height = 16;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, 16, 16);
      const { data } = ctx.getImageData(0, 0, 16, 16);
      let r = 0, g = 0, b = 0, n = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i + 3] < 128) continue;
        r += data[i]; g += data[i + 1]; b += data[i + 2]; n += 1;
      }
      if (!n) return null;
      const [h, s] = rgbToHsl(r / n, g / n, b / n);
      return { hue: Math.round(h), sat: Math.round(Math.min(100, s * 180)) };
    } catch (e) {
      return null;                      // a cover from another origin, one day
    }
  }

  // Hue and saturation only: music.css makes the colour for the theme, as on
  // the music page (dark 12%, a pale wash on light).
  function setAmbient(img) {
    const tint = img && img.naturalWidth ? ambientFrom(img) : null;
    const root = document.documentElement.style;
    if (tint) {
      root.setProperty('--mu-ambient-h', String(tint.hue));
      root.setProperty('--mu-ambient-s', `${tint.sat}%`);
    } else {
      root.removeProperty('--mu-ambient-h');
      root.removeProperty('--mu-ambient-s');
    }
  }

  // ---- covers ----------------------------------------------------------------
  // The share's cover route takes the music page's sizes: 96 and 300 for rows
  // and the bar, 600 for a header, and the screen's own size for the
  // now-playing screen, which the server answers with the file's own picture.
  function coverSrc(s, size) {
    return s && s.cover_url ? `${s.cover_url}&s=${size}` : null;
  }

  function initial(text) {
    return (String(text || '?').trim()[0] || '?').toUpperCase();
  }

  function placeholder(text, small) {
    return el('div', { class: 'mu-ph' + (small ? ' small' : ''), 'aria-hidden': 'true', text: initial(text) });
  }

  function coverImg(s, size, small) {
    if (!s || !s.cover_url) return placeholder(s ? s.title : '?', small);
    const img = el('img', { src: coverSrc(s, size), alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', () => img.replaceWith(placeholder(s.title, small)), { once: true });
    return img;
  }

  // The page's header art for a list (music.js mosaic): its first four covers
  // as a 2x2, or one cover, or a letter.
  function mosaic(list, title, size) {
    const withCover = list.filter((s) => s.cover_url).slice(0, 4);
    if (withCover.length === 4) {
      return el('div', { class: 'mu-mosaic' }, withCover.map((s) => coverImg(s, Math.round(size / 2))));
    }
    if (withCover.length) return coverImg(withCover[0], size);
    return placeholder(title);
  }

  // As music.js stageCoverSize: the screen, not the window, and never below
  // the largest rendition.
  function stageCoverSize() {
    const dpr = window.devicePixelRatio || 1;
    const scr = window.screen || {};
    const w = scr.width || window.innerWidth;
    const h = scr.height || window.innerHeight;
    return Math.max(600, Math.round(Math.max(w, h) * dpr));
  }

  // ---- the link ------------------------------------------------------------
  const audio = $('msAudio');
  const stage = $('msStage');
  let SONGS = [];
  let LINK = {};
  // What plays, as the music page keeps it: the list's positions in the order
  // they play, and where in it we are. Shuffle reorders this, not the page.
  const Q = { items: [], pos: -1 };

  function current() {
    return Q.pos >= 0 ? SONGS[Q.items[Q.pos]] || null : null;
  }

  // ---- one loudness for every song ------------------------------------------
  // As music.js (vault KSF-058): the server sends each song's gain in dB to
  // the library's common loudness, as high as it goes without a true peak
  // passing 0 dB, so a quiet song is turned UP. element.volume stops at 1:
  // the gain is a GainNode (audio -> gain -> speakers), made once Web Audio
  // runs, which a page may only start from a click or a key. Until then, and
  // where it cannot run, songs are only turned down. iOS stops Web Audio when
  // the screen locks and ignores element.volume, so it gets neither.
  const LOUD = { ctx: null, node: null, off: false };
  const IOS = /iP(hone|od|ad)/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let userVolume = 1;

  function gainDb() {
    const loud = LINK.loudness || {};
    if (typeof loud.target !== 'number') return 0;
    const s = current();
    return s && typeof s.gain === 'number' ? s.gain : (loud.unknown || 0);
  }

  function applyVolume() {
    const factor = Math.pow(10, gainDb() / 20);
    if (LOUD.node) {
      LOUD.node.gain.value = factor;
      audio.volume = userVolume;
    } else {
      audio.volume = userVolume * Math.min(1, factor);
    }
  }

  function startLoudness() {
    if (LOUD.node) {
      if (LOUD.ctx.state !== 'running') LOUD.ctx.resume().catch(() => {});
      return;
    }
    if (LOUD.off) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || IOS) { LOUD.off = true; return; }
    if (!LOUD.ctx && navigator.userActivation && !navigator.userActivation.hasBeenActive) return;
    try {
      if (!LOUD.ctx) {
        LOUD.ctx = new AC();
        LOUD.ctx.addEventListener('statechange', () => {
          if (LOUD.node && LOUD.ctx.state !== 'running' && !audio.paused) LOUD.ctx.resume().catch(() => {});
        });
      }
    } catch (e) { LOUD.off = true; return; }
    const route = () => {
      if (LOUD.ctx.state !== 'running' || LOUD.node || LOUD.off) return;
      try {
        const node = LOUD.ctx.createGain();
        LOUD.ctx.createMediaElementSource(audio).connect(node).connect(LOUD.ctx.destination);
        LOUD.node = node;
      } catch (e) {
        LOUD.off = true;
      }
      applyVolume();
    };
    if (LOUD.ctx.state === 'running') route();
    else LOUD.ctx.resume().then(route, () => {});
  }

  function showMessage(title, text) {
    $('msPlayer').hidden = true;
    $('msMain').replaceChildren(el('div', { class: 'ms-empty' },
      el('h1', { text: title }), text ? el('p', { text }) : null));
  }

  async function load() {
    let res;
    try {
      res = await fetch(`/api/public/share/${encodeURIComponent(TOKEN)}/music`);
    } catch (e) {
      showMessage('This link could not be opened', 'The server did not answer. Try again in a moment.');
      return;
    }
    if (res.status === 410) {
      showMessage('This link has run out', 'Ask whoever sent it for a new one.');
      return;
    }
    if (!res.ok) {
      showMessage('This link does not exist', 'It may have been revoked, or the address is not quite right.');
      return;
    }
    LINK = await res.json();
    SONGS = Array.isArray(LINK.songs) ? LINK.songs : [];
    document.title = LINK.title ? `${LINK.title} • Shared music` : 'Shared music';
    if (!SONGS.length) { showMessage('Nothing here any more', 'The songs this link pointed at are gone.'); return; }
    render();
    // One song is shown the way it is seen while it plays: on the now-playing
    // screen, with nothing beside it to come next. Playing starts if the
    // browser lets a page start sound by itself; most do not before the first
    // click, and then the bar's play button is where it starts.
    if (LINK.kind === 'song') {
      stage.classList.add('ms-solo');
      playList(0, { autoplay: true });
      toggleStage(true);
    }
  }

  // What the header calls it: the music page's own words. The top bar already
  // says it is a shared link.
  const KINDS = {
    playlist: 'Playlist', album: 'Album', artist: 'Artist',
    style: 'Style', song: 'Song', songs: 'Songs',
  };

  function render() {
    const kind = KINDS[LINK.kind] ? LINK.kind : 'songs';
    const total = SONGS.reduce((n, s) => n + (s.duration || 0), 0);
    // An artist's header is round and shows one cover, as on the page; a song
    // shows its own; every list shows its first four.
    const art = kind === 'artist' || kind === 'song'
      ? coverImg(SONGS.find((s) => s.cover_url) || SONGS[0], 600)
      : mosaic(SONGS, LINK.title, 600);
    const head = el('header', { class: 'mu-hero' },
      el('div', { class: 'mu-hero-art' + (kind === 'artist' ? ' round' : '') }, art),
      el('div', { class: 'mu-hero-text' },
        el('div', { class: 'mu-hero-kind', text: KINDS[kind] }),
        el('h1', { class: 'mu-hero-title', text: LINK.title || 'Shared music' }),
        LINK.subtitle ? el('div', { class: 'mu-hero-sub', text: LINK.subtitle }) : null,
        el('div', { class: 'mu-hero-meta', text: `${plural(SONGS.length, 'song')} • ${fmtTotal(total)}` }),
        el('div', { class: 'mu-actions' },
          el('button', { type: 'button', class: 'ah-btn mu-primary', onclick: () => playList(0) }, icon('play'), 'Play'),
          SONGS.length > 1
            ? el('button', { type: 'button', class: 'ah-btn', onclick: () => playList(0, { shuffle: true }) }, icon('shuffle'), 'Shuffle')
            : null,
          LINK.expires_at
            ? el('span', { class: 'ms-expiry',
              text: `The link ends ${new Date(LINK.expires_at * 1000).toLocaleDateString()}` })
            : null)));

    const ol = el('ol', { class: 'mu-tracks' }, SONGS.map((s, n) => {
      const go = () => playList(n);
      return el('li', { class: 'mu-track', dataset: { n: String(n) }, ondblclick: go,
        onclick: (e) => { if (TOUCH.matches && !e.target.closest('a, button')) go(); } },
      el('div', { class: 'mu-track-num' },
        el('span', { class: 'mu-n', text: String(n + 1) }),
        el('button', { type: 'button', class: 'mu-track-play', 'aria-label': `Play ${s.title}`, onclick: go },
          icon('play'))),
      el('div', { class: 'mu-track-cover' }, coverImg(s, 96, true)),
      el('div', { class: 'mu-track-main' },
        el('div', { class: 'mu-track-title', text: s.title, title: s.title }),
        el('div', { class: 'mu-track-artist', text: s.artist })),
      el('div', { class: 'mu-track-album', text: s.album && s.album !== s.title ? s.album : '' }),
      el('div', { class: 'mu-track-dur', text: s.duration ? fmtTime(s.duration) : '' }),
      el('a', { class: 'mu-icon-btn ms-dl', href: s.url, download: '',
        title: `Download "${s.title}"`, 'aria-label': `Download "${s.title}"` }, icon('download')));
    }));

    $('msMain').replaceChildren(head, ol);
    markPlaying();
  }

  function markPlaying() {
    const at = Q.pos >= 0 ? Q.items[Q.pos] : -1;
    for (const row of document.querySelectorAll('.mu-tracks .mu-track')) {
      row.classList.toggle('playing', Number(row.dataset.n) === at);
    }
  }

  // The list from `start`, or all of it shuffled - as the page's Play,
  // Shuffle and a click on a row do.
  function playList(start, opts = {}) {
    const order = SONGS.map((s, i) => i);
    if (opts.shuffle) {
      for (let i = order.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [order[i], order[j]] = [order[j], order[i]];
      }
      start = 0;
    }
    Q.items = order;
    Q.pos = Math.max(0, Math.min(order.length - 1, start));
    loadCurrent(opts.autoplay !== false);
  }

  function loadCurrent(autoplay) {
    const s = current();
    if (!s) return;
    $('msPlayer').hidden = false;
    audio.src = s.url;
    applyVolume();
    startLoudness();
    if (autoplay) audio.play().catch(updatePlay);
    showProgress(0, s.duration || 0);
    showNow(s);
    renderQueue();
    markPlaying();
    updatePlay();
  }

  function showProgress(pos, dur) {
    const seek = $('msSeek');
    const pct = dur > 0 ? Math.max(0, Math.min(1000, Math.round((pos / dur) * 1000))) : 0;
    seek.value = pct;
    seek.style.setProperty('--pct', `${pct / 10}%`);
    $('msTime').textContent = `${fmtTime(pos)} / ${fmtTime(dur)}`;
  }

  function showNow(s) {
    $('msNowTitle').textContent = s.title;
    $('msNowSub').textContent = [s.artist, s.album && s.album !== s.title ? s.album : ''].filter(Boolean).join(' • ');
    const img = $('msNowCover');
    if (s.cover_url) {
      img.src = coverSrc(s, 96);
      img.hidden = false;
    } else {
      img.removeAttribute('src');
      img.hidden = true;
    }
    if (stageOpen()) showStage(s);
    document.title = `${s.title} • ${s.artist}`;
    if ('mediaSession' in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: s.title, artist: s.artist, album: s.album || '',
        artwork: s.cover_url ? [{ src: coverSrc(s, 300), sizes: '300x300' }] : [],
      });
    }
  }

  function next() {
    if (Q.pos < Q.items.length - 1) { Q.pos += 1; loadCurrent(true); }
  }

  function prev() {
    if (audio.currentTime > 4 || Q.pos <= 0) { audio.currentTime = 0; return; }
    Q.pos -= 1;
    loadCurrent(true);
  }

  // ---- what comes next, beside the cover (music.js queueRows) --------------
  function renderQueue() {
    const rows = [];
    Q.items.forEach((i, n) => {
      const s = SONGS[i];
      if (!s || n > Q.pos + 400) return;
      if (n === Q.pos) rows.push(el('li', { class: 'mu-q-label', text: 'Now playing' }));
      if (n === Q.pos + 1) rows.push(el('li', { class: 'mu-q-label', text: 'Up next' }));
      rows.push(el('li', { class: n === Q.pos ? 'current' : null,
        onclick: () => { Q.pos = n; loadCurrent(true); } },
      el('div', { class: 'mu-track-cover' }, coverImg(s, 96, true)),
      el('div', { class: 'mu-track-main' },
        el('div', { class: 'mu-track-title', text: s.title }),
        el('div', { class: 'mu-track-artist', text: s.artist })),
      el('span')));
    });
    if (!rows.length) rows.push(el('li', { class: 'mu-q-label', text: 'Nothing queued' }));
    $('msQueueList').replaceChildren(...rows);
  }

  // ---- the now-playing screen (music.js showStage/toggleStage) -------------
  function stageOpen() {
    return !stage.hidden;
  }

  function toggleStage(force) {
    const open = force === undefined ? !stageOpen() : !!force;
    if (open === stageOpen()) return;
    if (open && !current()) return;            // nothing to show yet
    stage.hidden = !open;
    document.body.classList.toggle('mu-stage-open', open);
    const b = $('msExpand');
    b.setAttribute('aria-expanded', String(open));
    b.setAttribute('aria-label', open ? 'Close the player' : 'Open the player');
    setIcon(b, open ? 'collapse' : 'expand');
    if (open) {
      showStage(current());
      renderQueue();
    } else {
      leaveStageCover();
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    }
  }

  // The cover as large as it fits, asked for at the screen's size and then
  // upscaled for it by js/upscale.js - media-dl's own file, pointed at this
  // link's routes below. No title under it: the bar says what is playing.
  function showStage(s) {
    if (!s) return;
    leaveStageCover();
    const dl = $('msStageDl');
    dl.href = s.url;
    dl.title = `Download "${s.title}"`;
    const img = $('msStageCover');
    const ph = $('msStagePh');
    if (!s.cover_url) {
      img.removeAttribute('src');
      img.hidden = true;
      ph.hidden = false;
      ph.textContent = initial(s.title);
      setAmbient(null);
      return;
    }
    img.hidden = false;
    ph.hidden = true;
    img.src = coverSrc(s, stageCoverSize());
    window.currentPreviewKey = `${s.root}\n${s.path}`;
    if (typeof startLiveUpscale === 'function') {
      startLiveUpscale(img, { type: 'audio', root: s.root, path: s.path });
    }
  }

  function leaveStageCover() {
    window.currentPreviewKey = null;
    shownFor = null;
    delete $('msStageCover').dataset.upscaled;
    if (typeof resetLiveUpscale === 'function') resetLiveUpscale();
  }

  // Shape and colour, once per song, from whichever copy arrives first - the
  // upscale is the same picture with more pixels (music.js coverShown).
  let shownFor = null;

  function coverShown(img) {
    if (shownFor === window.currentPreviewKey) return;
    if (!img.naturalWidth || !img.naturalHeight) return;
    shownFor = window.currentPreviewKey;
    $('msStageFit').style.setProperty('--cover-ar', (img.naturalWidth / img.naturalHeight).toFixed(4));
    setAmbient(img);
  }

  // ---- fullscreen, as the music page does it: the page, with the bars fading
  // out while nothing is being done -------------------------------------------
  // One click on the artwork plays or pauses, two go fullscreen (user,
  // 2026-09-23) - and a double click must never leave it paused. The click
  // waits 150 ms (user: "Make the pause 150ms"; media-dl's player.js waits
  // 250) so the second click of a quick double click can cancel it. A slower
  // double click - the system may allow 500 ms - finds the first click
  // already done, and puts it back: `single` runs once more for that click. e.detail counts the
  // clicks of one multi-click, so the second click never starts a timer.
  // On a touch screen a tap does nothing - only a double tap, which
  // fullscreens (user, 2026-09-24: "The single click to toggle playback
  // feature should be disabled on mobile"). Asked at every tap, as the rows'
  // tap-to-play does, so a tablet that gains a mouse follows along.
  const CLICK_WAIT_MS = 150;

  function clickOrDouble(node, single, double) {
    let timer = null;
    let first = -1;                   // the click that may begin a double click
    let ranFor = -2;                  // the click `single` already answered
    node.addEventListener('click', (e) => {
      if (e.detail > 1) return;
      first = e.timeStamp;
      clearTimeout(timer);
      if (TOUCH.matches) return;
      timer = setTimeout(() => { timer = null; ranFor = first; single(); }, CLICK_WAIT_MS);
    });
    node.addEventListener('dblclick', () => {
      if (timer) { clearTimeout(timer); timer = null; } else if (ranFor === first) { ranFor = -2; single(); }
      double();
    });
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => { /* already left */ });
      return;
    }
    try {
      const req = document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
      if (req && req.catch) req.catch(() => {});
    } catch (e) { /* not allowed here */ }
  }

  function bindFullscreen() {
    // 600 ms with a mouse, 2 s on a touch screen: there the controls come and
    // go with taps, and at 600 ms they were gone before the hand reached the
    // next control (user, 2026-09-25: "linger for 2s").
    const chromeHideMs = () => (TOUCH.matches ? 2000 : 600);
    let chromeTimer = null;
    let overChrome = false;
    const hideChrome = () => {
      if (overChrome) { chromeTimer = setTimeout(hideChrome, chromeHideMs()); return; }
      document.body.classList.remove('mu-chrome');
    };
    const hideChromeLater = () => {
      clearTimeout(chromeTimer);
      chromeTimer = setTimeout(hideChrome, chromeHideMs());
    };
    const showChrome = () => {
      document.body.classList.add('mu-chrome');
      hideChromeLater();
    };
    for (const ev of ['pointermove', 'pointerdown', 'wheel', 'keydown']) {
      document.addEventListener(ev, showChrome, { passive: true });
    }
    document.addEventListener('focusin', showChrome);
    for (const node of [stage.querySelector('.mu-stage-bar'), stage.querySelector('.mu-stage-queue'), $('msPlayer')]) {
      node.addEventListener('pointerenter', () => { overChrome = true; showChrome(); });
      node.addEventListener('pointerleave', () => { overChrome = false; hideChromeLater(); });
    }
    document.addEventListener('fullscreenchange', () => {
      const on = !!document.fullscreenElement;
      document.body.classList.toggle('mu-fs', on);
      if (on) showChrome();
      const b = $('msFullscreen');
      b.setAttribute('aria-pressed', String(on));
      b.setAttribute('aria-label', on ? 'Leave fullscreen' : 'Fullscreen');
      setIcon(b, on ? 'fullscreenExit' : 'fullscreen');
      if (on) toggleStage(true);
    });
  }

  // ---- the transport -----------------------------------------------------------
  function togglePlay() {
    if (!current()) { playList(0); return; }
    if (audio.paused) audio.play().catch(updatePlay); else audio.pause();
  }

  function updatePlay() {
    const playing = !audio.paused && !audio.ended;
    setIcon($('msPlay'), playing ? 'pause' : 'play');
    $('msPlay').setAttribute('aria-label', playing ? 'Pause' : 'Play');
  }

  function bind() {
    setIcon($('msPrev'), 'prev');
    setIcon($('msNext'), 'next');
    setIcon($('msPlay'), 'play');
    setIcon($('msMute'), 'volume');
    setIcon($('msExpand'), 'expand');
    setIcon($('msStageClose'), 'collapse');
    setIcon($('msStageDl'), 'download');
    setIcon($('msFullscreen'), 'fullscreen');
    $('msPlay').addEventListener('click', togglePlay);
    $('msNext').addEventListener('click', next);
    $('msPrev').addEventListener('click', prev);
    $('msExpand').addEventListener('click', () => toggleStage());
    $('msStageClose').addEventListener('click', () => toggleStage(false));
    $('msFullscreen').addEventListener('click', toggleFullscreen);
    $('msStageCover').addEventListener('load', (e) => coverShown(e.currentTarget));
    // A click on the artwork plays or pauses, a double click fullscreens it,
    // as on the music page (user, 2026-09-23).
    clickOrDouble($('msStageFit'), togglePlay, toggleFullscreen);
    // The whole bar opens the screen, as on the page - anything that is itself
    // a control keeps its click, a seek or volume drag that ends over the bar
    // included (the press is remembered for that). Judged by the event's path
    // as dispatched, not by the target's ancestors afterwards: a button that
    // swaps its icon in its own click handler (Expand, Mute, Play) leaves a
    // detached <path> as the target, and closest('button') on that finds
    // nothing - the bar then toggled the screen too (music.js, 2026-09-24).
    let barFromControl = false;
    const barControl = (e) => e.composedPath().some((n) => n instanceof Element && n.matches('button, a, input'));
    $('msPlayer').addEventListener('pointerdown', (e) => { barFromControl = barControl(e); });
    $('msPlayer').addEventListener('click', (e) => {
      const fromControl = barFromControl; barFromControl = false;
      if (fromControl || barControl(e)) return;
      toggleStage();
    });
    const seek = $('msSeek');
    let dragging = false;
    seek.addEventListener('input', () => {
      dragging = true;
      seek.style.setProperty('--pct', `${seek.value / 10}%`);
    });
    seek.addEventListener('change', () => {
      if (audio.duration) audio.currentTime = audio.duration * seek.value / 1000;
      dragging = false;
    });
    // Whatever was last chosen, 100 % until something is - the music page's
    // own key: a link is opened on the same site, so someone who listens there
    // brings their volume along.
    // The slider is the listener's volume; the song's gain comes on top.
    $('msVolume').addEventListener('input', (e) => {
      userVolume = Number(e.target.value) / 100;
      audio.muted = false;
      applyVolume();
      try { localStorage.setItem('music.volume', JSON.stringify(userVolume)); } catch (err) { /* blocked storage */ }
      setIcon($('msMute'), userVolume ? 'volume' : 'mute');
    });
    $('msMute').addEventListener('click', () => {
      audio.muted = !audio.muted;
      setIcon($('msMute'), audio.muted || !userVolume ? 'mute' : 'volume');
    });
    let kept = null;
    try { kept = JSON.parse(localStorage.getItem('music.volume') || 'null'); } catch (err) { /* blocked storage */ }
    userVolume = typeof kept === 'number' && kept >= 0 && kept <= 1 ? kept : 1;
    applyVolume();
    $('msVolume').value = String(Math.round(userVolume * 100));
    setIcon($('msMute'), userVolume ? 'volume' : 'mute');
    for (const type of ['pointerdown', 'keydown']) {
      document.addEventListener(type, startLoudness, { capture: true, passive: true });
    }
    audio.addEventListener('timeupdate', () => {
      const s = current();
      const d = audio.duration || (s && s.duration) || 0;
      if (!dragging) showProgress(audio.currentTime, d);
    });
    audio.addEventListener('play', () => { startLoudness(); updatePlay(); });
    audio.addEventListener('pause', updatePlay);
    audio.addEventListener('ended', () => { if (Q.pos < Q.items.length - 1) next(); else updatePlay(); });
    audio.addEventListener('error', () => { if (current()) $('msNowSub').textContent = 'This song could not be played'; });
    if ('mediaSession' in navigator) {
      const on = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch (e) { /* not here */ } };
      on('play', () => audio.play().catch(updatePlay));
      on('pause', () => audio.pause());
      on('nexttrack', next);
      on('previoustrack', prev);
    }
    // The space bar belongs to the player unless something was tabbed to;
    // Escape leaves the screen, as it does on the page.
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && stageOpen() && !document.fullscreenElement) { toggleStage(false); return; }
      if (e.key !== ' ' && e.key !== 'Spacebar') return;
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'BUTTON' || t.tagName === 'A')) return;
      e.preventDefault();
      $('msPlay').click();
    });
    bindFullscreen();
  }

  // js/upscale.js reads these from whatever page it runs on (music.js
  // bindStage does the same). The link's own routes, quiet while upscaling is
  // off for shares - a visitor has no Settings to turn it on in - and no model
  // choice: this is somebody else's library.
  function bindUpscale() {
    window.currentPreviewKey = null;
    window.pinUpscaledSize = function () { };     // the box is sized by CSS
    window.formatDateTime = (t) => (t ? new Date(t * 1000).toLocaleString('en-GB') : '-');
    window.setPreviewEnhancement = function () { };  // no details panel to report to
    if (typeof upscaleRoutes === 'object' && upscaleRoutes) {
      upscaleRoutes.config = `/api/public/share/${encodeURIComponent(TOKEN)}/upscale/config`;
      upscaleRoutes.ask = `/api/public/share/${encodeURIComponent(TOKEN)}/upscale`;
      upscaleRoutes.quietWhenOff = true;
      upscaleRoutes.setModel = null;
    }
    if (typeof upscaleHost === 'object' && upscaleHost) upscaleHost.chip = () => $('msStageChips');
  }

  bindUpscale();
  bind();
  if (!TOKEN) showMessage('This link does not exist', null);
  else load();
})();
