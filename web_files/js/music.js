/* The music page (music.html): a YouTube Music style front
 * end for media-dl's music. One page; the router swaps views inside <main>, so
 * the player keeps playing while you browse.
 *
 * Data: GET /api/music/library once - every song this viewer may see, the
 * playlists, channels and albums as lists of row numbers into the songs, and
 * the viewer's own playlists. Search runs here, over that, as you type.
 * Playback: /api/media/file (the library's own route: Range, and the .mka
 * remux). Covers: /api/music/cover at 96/300/600 px.
 *
 * Radios and autoplay work OUTWARDS FROM ONE ORIGIN on the genre map: the
 * next song is the nearest to the one playing plus the nearest to the origin
 * (see HOME_PULL), so a neighbourhood is played out and then the nearest
 * unplayed one to HOME follows - rather than a free chain of nearest
 * neighbours (genres.js's "Keep playing nearby"), which walks away from the
 * song it started from and never comes back. Each song carries its map
 * position (p3); what the viewer played, their radios and the autoplay
 * switch are kept per account (/api/music/played, /radio, /settings) and come
 * back with the library as `me`.
 */
(function () {
  'use strict';

  // ---- small helpers -------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function fmtTime(s) {
    s = Math.max(0, Math.round(s || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
  }

  function fmtTotal(s) {
    const m = Math.round((s || 0) / 60);
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60);
    return `${h} hr ${m % 60} min`;
  }

  function plural(n, word) {
    return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
  }

  // Lower case, no accents, no punctuation runs: what search compares.
  function norm(s) {
    return String(s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '')
      .toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  }

  function store(key, value) {
    try {
      if (value === undefined) return JSON.parse(localStorage.getItem('music.' + key) || 'null');
      localStorage.setItem('music.' + key, JSON.stringify(value));
    } catch (e) { /* private mode, blocked storage: the page works without it */ }
    return null;
  }

  // ---- icons ------------------------------------------------------------------
  const ICONS = {
    play: 'M8 5v14l11-7z',
    pause: 'M6 5h4v14H6zM14 5h4v14h-4z',
    next: 'M6 18l8.5-6L6 6v12zm10-12v12h2V6h-2z',
    prev: 'M6 6h2v12H6zm3.5 6l8.5 6V6z',
    shuffle: 'M10.6 9.2L5.4 4 4 5.4l5.2 5.2 1.4-1.4zM14.5 4l2 2L4 18.6 5.4 20 18 7.5l2 2V4h-5.5zm.3 9.4l-1.4 1.4 3.1 3.1L14.5 20H20v-5.5l-2 2-3.2-3.1z',
    repeat: 'M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z',
    repeat1: 'M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4zm-4-2V9h-1l-2 1v1h1.5v4H13z',
    volume: 'M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z',
    mute: 'M16.5 12A4.5 4.5 0 0 0 14 8v2.2l2.5 2.5V12zM19 12c0 .9-.2 1.8-.5 2.6l1.5 1.5A8.9 8.9 0 0 0 21 12a9 9 0 0 0-7-8.8v2.1a7 7 0 0 1 5 6.7zM4.3 3L3 4.3 7.7 9H3v6h4l5 5v-6.7l4.3 4.3a7 7 0 0 1-2.3 1.2v2.1a9 9 0 0 0 3.7-1.8l2 2 1.3-1.3-9-9L4.3 3zM12 4L9.9 6.1 12 8.2V4z',
    queue: 'M3 6h12v2H3zm0 4h12v2H3zm0 4h8v2H3zm14 0v-4h-2v4h-4v2h4v4h2v-4h4v-2z',
    more: 'M12 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm0 2a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm0 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4z',
    home: 'M4 10.5L12 4l8 6.5V20h-5v-6H9v6H4z',
    library: 'M4 6H2v14a2 2 0 0 0 2 2h14v-2H4V6zm16-4H8a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zm-2 5h-3v5.5a2.5 2.5 0 1 1-1-2V5h4v2z',
    search: 'M15.5 14h-.8l-.3-.3A6.5 6.5 0 1 0 9.5 16a6.5 6.5 0 0 0 4.2-1.6l.3.3v.8l5 5 1.5-1.5-5-5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z',
    plus: 'M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6z',
    share: 'M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81a3 3 0 1 0-3-3c0 .24.04.47.09.7L8.04 9.81A3 3 0 1 0 6 15c.79 0 1.5-.31 2.04-.81l7.12 4.16c-.05.21-.08.43-.08.65a2.92 2.92 0 1 0 2.92-2.92z',
    close: 'M19 6.4L17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z',
    sync: 'M12 4V1L8 5l4 4V6a6 6 0 0 1 5.7 7.9l1.5 1.5A8 8 0 0 0 12 4zm0 14a6 6 0 0 1-5.7-7.9L4.8 8.6A8 8 0 0 0 12 20v3l4-4-4-4v3z',
    menu: 'M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z',
    radio: 'M3.24 6.15C2.51 6.43 2 7.17 2 8v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8c0-1.11-.89-2-2-2H8.3l8.26-3.34L15.88 1 3.24 6.15zM7 20a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm13-8h-2v-2h-2v2H4V8h16v4z',
    history: 'M13 3a9 9 0 0 0-9 9H1l3.9 3.9.1.1L9 12H6a7 7 0 1 1 7 7 6.9 6.9 0 0 1-4.6-1.8L7 18.6A9 9 0 1 0 13 3zm-1 5v5l4.3 2.5.7-1.2-3.5-2.1V8H12z',
    expand: 'M7.4 15.4 12 10.8l4.6 4.6L18 14l-6-6-6 6z',
    collapse: 'M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z',
    info: 'M11 7h2v2h-2V7zm0 4h2v6h-2v-6zm1-9a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16z',
    gear: 'M19.4 13a7.8 7.8 0 0 0 0-2l2.1-1.6a.5.5 0 0 0 .1-.6l-2-3.5a.5.5 0 0 0-.6-.2l-2.5 1a7.3 7.3 0 0 0-1.7-1l-.4-2.6a.5.5 0 0 0-.5-.4h-4a.5.5 0 0 0-.5.4l-.4 2.6a7.3 7.3 0 0 0-1.7 1l-2.5-1a.5.5 0 0 0-.6.2l-2 3.5a.5.5 0 0 0 .1.6L4.6 11a7.8 7.8 0 0 0 0 2l-2.1 1.6a.5.5 0 0 0-.1.6l2 3.5c.1.2.4.3.6.2l2.5-1c.5.4 1.1.8 1.7 1l.4 2.6c0 .3.2.4.5.4h4c.3 0 .5-.1.5-.4l.4-2.6c.6-.2 1.2-.6 1.7-1l2.5 1c.2.1.5 0 .6-.2l2-3.5a.5.5 0 0 0-.1-.6L19.4 13zM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z',
    fullscreen: 'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
    fullscreenExit: 'M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z',
    // The song menu's entries, after YouTube Music's: a play mark ahead of
    // the list for "next", behind it for "queue".
    playNext: 'M3 4v6l5-3zM10 6h11v2H10zM3 12h18v2H3zm0 5h18v2H3z',
    addQueue: 'M3 5h18v2H3zm0 5h18v2H3zM3 14v6l5-3zm7 2h11v2H10z',
    playlist: 'M15 6H3v2h12V6zm0 4H3v2h12v-2zM3 16h8v-2H3v2zM17 6v8.18c-.31-.11-.65-.18-1-.18-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3V8h3V6h-5z',
    person: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z',
    album: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 14.5a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9zm0-5.5a1 1 0 1 0 0 2 1 1 0 0 0 0-2z',
    trash: 'M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z',
    wand: 'M7.5 5.6 10 7 8.6 4.5 10 2 7.5 3.4 5 2l1.4 2.5L5 7zm12 9.8L17 14l1.4 2.5L17 19l2.5-1.4L22 19l-1.4-2.5L22 14zM22 2l-2.5 1.4L17 2l1.4 2.5L17 7l2.5-1.4L22 7l-1.4-2.5zm-7.63 5.29a1 1 0 0 0-1.41 0L1.29 18.96a1 1 0 0 0 0 1.41l2.34 2.34a1 1 0 0 0 1.41 0L16.7 11.05a1 1 0 0 0 0-1.41l-2.33-2.35zm-1.03 5.49-2.12-2.12 2.44-2.44 2.12 2.12-2.44 2.44z',
    autoplay: 'M18.6 6.62c-1.44 0-2.8.56-3.77 1.53L12 10.66 10.48 12h.01L7.8 14.39c-.64.64-1.49.99-2.4.99-1.87 0-3.39-1.51-3.39-3.38S3.53 8.62 5.4 8.62c.91 0 1.76.35 2.44 1.03l1.13 1 1.51-1.34L9.22 8.2A5.37 5.37 0 0 0 5.4 6.62C2.42 6.62 0 9.04 0 12s2.42 5.38 5.4 5.38c1.44 0 2.8-.56 3.77-1.53l2.83-2.5.01.01L13.52 12h-.01l2.69-2.39c.64-.64 1.49-.99 2.4-.99 1.87 0 3.39 1.51 3.39 3.38s-1.52 3.38-3.39 3.38c-.9 0-1.76-.35-2.44-1.03l-1.14-1.01-1.51 1.34 1.27 1.12a5.39 5.39 0 0 0 3.82 1.57c2.98 0 5.4-2.41 5.4-5.38s-2.42-5.37-5.4-5.37z',
  };

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('fill', 'currentColor');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', ICONS[name] || '');
    svg.append(path);
    return svg;
  }

  function setIcon(node, name) {
    node.replaceChildren(icon(name));
    node.dataset.icon = name;
  }

  function hydrateIcons(root) {
    for (const node of root.querySelectorAll('[data-icon]')) node.replaceChildren(icon(node.dataset.icon));
  }

  // ---- the library ---------------------------------------------------------
  const LIB = {
    songs: [], playlists: [], channels: [], albums: [], mine: [], canSync: false, built: 0,
    loud: { target: null, unknown: 0 },   // the common loudness (LUFS) and an unmeasured file's gain
    playlistById: new Map(), albumById: new Map(), channelByName: new Map(),
    byRef: new Map(), artists: new Map(), genres: new Map(),
    positioned: [],              // songs with a place on the genre map: what radios can reach
  };
  // The viewer's listening, kept by the server: [[ref, src, t]], their radios
  // (newest first, without the played lists) and the autoplay switch.
  const ME = { events: [], radios: [], autoplay: true };

  function songRef(s) {
    return s.vid ? 'v:' + s.vid : `p:${s.root}:${s.path}`;
  }

  function decode(data) {
    const f = data.fields;
    const at = (name) => f.indexOf(name);
    const iTitle = at('title'), iArtist = at('artist'), iAlbum = at('album'), iDur = at('duration'),
      iRoot = at('root'), iPath = at('path'), iVid = at('video_id'), iGenre = at('genre'),
      iAdded = at('added'), iCover = at('cover'), iP3 = at('p3'), iVideo = at('video'),
      iGain = at('gain'), iVideoGain = at('video_gain'), iLevel = at('level'), iVideoLevel = at('video_level');
    const lvl = (r, i) => (i >= 0 && Array.isArray(r[i]) ? r[i] : null);
    const num = (r, i) => (i >= 0 && typeof r[i] === 'number' ? r[i] : null);
    LIB.songs = data.songs.map((r, i) => {
      const s = {
        i, title: r[iTitle], artist: r[iArtist], album: r[iAlbum], dur: r[iDur], root: r[iRoot],
        path: r[iPath], vid: r[iVid], genre: r[iGenre], added: r[iAdded], cover: !!r[iCover],
        p3: iP3 >= 0 && Array.isArray(r[iP3]) ? r[iP3] : null,
        // [root, path] of this song's music video, when the library holds it.
        video: iVideo >= 0 && Array.isArray(r[iVideo]) ? { root: r[iVideo][0], path: r[iVideo][1] } : null,
        // dB that play the song's file, and its video's, at the common
        // loudness; null: not measured yet (see gainDbOf).
        gain: num(r, iGain), videoGain: num(r, iVideoGain),
        // [lufs, clipping ceiling] of each, for the viewer's own target.
        level: lvl(r, iLevel), videoLevel: lvl(r, iVideoLevel),
      };
      s.ref = songRef(s);
      s.hay = norm(`${s.title} ${s.artist} ${s.album}`);
      s.nTitle = norm(s.title);
      s.nArtist = norm(s.artist);
      return s;
    });
    LIB.playlists = data.playlists || [];
    LIB.channels = data.channels || [];
    LIB.albums = data.albums || [];
    LIB.mine = data.mine || [];
    LIB.canSync = !!data.can_sync;
    LIB.built = data.built || 0;
    LIB.loud = data.loudness || { target: null, unknown: 0 };
    LIB.playlistById = new Map([...LIB.playlists, ...LIB.mine].map((p) => [p.id, p]));
    LIB.albumById = new Map(LIB.albums.map((a) => [a.id, a]));
    LIB.channelByName = new Map(LIB.channels.map((c) => [c.name.toLowerCase(), c]));
    LIB.byRef = new Map();
    for (const s of LIB.songs) {
      LIB.byRef.set(s.ref, s.i);
      LIB.byRef.set(`p:${s.root}:${s.path}`, s.i);
    }
    // A song merged with its second copy answers to that copy's refs too
    // (song_merge.py): history, radios and saved queues may still hold them.
    for (const [ref, i] of Object.entries(data.aliases || {})) {
      if (!LIB.byRef.has(ref) && LIB.songs[i]) LIB.byRef.set(ref, i);
    }
    // Artists: every artist named on a song, and every channel folder.
    LIB.artists = new Map();
    for (const s of LIB.songs) {
      const key = s.artist.toLowerCase();
      if (!LIB.artists.has(key)) LIB.artists.set(key, { name: s.artist, songs: [] });
      LIB.artists.get(key).songs.push(s.i);
    }
    for (const c of LIB.channels) {
      const key = c.name.toLowerCase();
      if (!LIB.artists.has(key)) LIB.artists.set(key, { name: c.name, songs: [] });
    }
    LIB.genres = new Map();
    for (const s of LIB.songs) {
      if (!s.genre) continue;
      if (!LIB.genres.has(s.genre)) LIB.genres.set(s.genre, []);
      LIB.genres.get(s.genre).push(s.i);
    }
    LIB.positioned = LIB.songs.filter((s) => s.p3).map((s) => s.i);
    const me = data.me || {};
    ME.events = Array.isArray(me.events) ? me.events : [];
    ME.radios = Array.isArray(me.radios) ? me.radios : [];
    ME.autoplay = me.autoplay !== false;
    ME.targetLufs = typeof me.target_lufs === 'number' ? me.target_lufs : null;
    renderTargetLufs();
  }

  // Where a song is being played from, as it is kept with the play
  // (music_library.Listening): a short code this page writes and reads back.
  // A radio wins over the list it started from - that is what it is playing.
  function ctxCode() {
    if (Q.radio) return 'radio:' + Q.radio.id;
    const c = Q.context;
    if (!c) return '';
    if (LIB.playlistById.has(c)) return 'pl:' + c;
    if (LIB.albumById.has(c)) return 'al:' + c;
    if (c.startsWith('artist:')) return 'ar:' + c.slice(7);
    if (c.startsWith('genre:')) return 'ge:' + c.slice(6);
    if (c === 'library') return 'lib';
    if (c === 'search') return 'search';
    return '';
  }

  // "pl:u3f2..." -> ["pl", "u3f2..."]; a radio's id keeps its own colon
  // ("radio:song:v:abc" -> ["radio", "song:v:abc"]).
  function ctxParts(code) {
    const at = String(code || '').indexOf(':');
    return at < 0 ? [String(code || ''), ''] : [code.slice(0, at), code.slice(at + 1)];
  }

  function radioTitle(id) {
    const known = ME.radios.find((r) => r.id === id);
    if (known && known.title) return known.title;
    const [kind, key] = ctxParts(id);
    if (kind === 'auto') return 'Autoplay';
    const desc = radioFor(kind, key);
    return desc ? desc.title : 'a radio';
  }

  // What a play's context is called and where it leads, or null for a code
  // this library no longer knows (a playlist that was deleted, a song gone).
  function ctxLabel(code) {
    const [kind, rest] = ctxParts(code);
    if (kind === 'pl') {
      const pl = LIB.playlistById.get(rest);
      return pl ? { text: pl.title, href: `#/playlist/${pl.id}` } : { text: 'a playlist that is gone' };
    }
    if (kind === 'al') {
      const al = LIB.albumById.get(rest);
      return al ? { text: al.title, href: `#/album/${al.id}` } : { text: 'an album that is gone' };
    }
    if (kind === 'ar') {
      const a = LIB.artists.get(rest.toLowerCase());
      return { text: a ? a.name : rest, href: artistHref(rest) };
    }
    if (kind === 'ge') return { text: rest, href: `#/genre/${encodeURIComponent(rest)}` };
    if (kind === 'radio') return { text: radioTitle(rest), radio: rest };
    if (code === 'lib') return { text: 'your library', href: '#/library/songs' };
    if (code === 'search') return { text: 'a search' };
    return null;
  }

  function artistSongs(name) {
    const key = String(name || '').toLowerCase();
    const set = new Set((LIB.artists.get(key) || { songs: [] }).songs);
    const ch = LIB.channelByName.get(key);
    if (ch) ch.songs.forEach((i) => set.add(i));
    return [...set].sort((a, b) => LIB.songs[b].added - LIB.songs[a].added);
  }

  function totalDur(list) {
    return list.reduce((t, i) => t + (LIB.songs[i] ? LIB.songs[i].dur : 0), 0);
  }

  // ---- talking to the server --------------------------------------------------
  async function api(url, opts = {}) {
    const init = { credentials: 'same-origin', method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    let res;
    try {
      res = await fetch(url, init);
    } catch (e) {
      return { ok: false, status: 0, data: { error: 'the server could not be reached' } };
    }
    if (res.status === 401) {
      // The session ended: a reload goes through the sign-in and back here.
      location.reload();
      return { ok: false, status: 401, data: {} };
    }
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    return { ok: res.ok, status: res.status, data };
  }

  let loadTimer = null;
  async function loadLibrary(opts = {}) {
    const res = await api('/api/music/library');
    if (res.status === 202) {
      showLoading(res.data.error
        ? `The library could not be read: ${res.data.error}`
        : 'Reading your music for the first time - every song\'s tags, once. This takes about two minutes…');
      clearTimeout(loadTimer);
      loadTimer = setTimeout(() => loadLibrary(opts), 5000);
      return false;
    }
    if (!res.ok) {
      showLoading(res.data.error || 'The library could not be loaded.');
      return false;
    }
    const before = LIB.built;
    const oldSongs = LIB.songs;
    decode(res.data);
    if (oldSongs.length) remapQueue(oldSongs);
    updateAutoplayButton();
    renderSide();
    if (!opts.quiet || before !== LIB.built) render();
    restoreQueueOnce();
    return true;
  }

  // A rebuilt library numbers its songs anew (a download lands anywhere in
  // the order), and the queue, its unshuffled order and a radio's played
  // songs are song numbers: carry them over by song, dropping what is gone.
  function remapQueue(oldSongs) {
    const map = (i) => (oldSongs[i] ? LIB.byRef.get(oldSongs[i].ref) : undefined);
    const known = (i) => i !== undefined;
    const keep = (list) => list.map(map).filter(known);
    const mapped = Q.items.map(map);
    // The same place in the queue (a song can be in it twice), counted in
    // what is left; a vanished current song leaves the next one current.
    const pos = mapped.slice(0, Math.max(0, Q.pos)).filter(known).length;
    Q.items = mapped.filter(known);
    Q.pos = Q.pos < 0 || !Q.items.length ? -1 : Math.min(pos, Q.items.length - 1);
    if (Q.original) Q.original = keep(Q.original);
    if (Q.radio) Q.radio.played = new Set(keep([...Q.radio.played]));
  }

  function showLoading(text) {
    $('muMain').replaceChildren(el('div', { class: 'mu-loading', text }));
  }

  // ---- covers ----------------------------------------------------------------
  function coverUrl(s, size) {
    return `/api/music/cover?root=${encodeURIComponent(s.root)}&path=${encodeURIComponent(s.path)}&s=${size}`;
  }

  // What the now-playing screen asks for. Rows and the bar take a rendition -
  // 96 or 300 px, made once and kept - but the screen draws the cover across
  // the whole display, so it asks for the display's own size and the server
  // answers above its ladder with the picture inside the file (server.py,
  // MUSIC_COVER_SIZES). The screen, not the window: the window can still be
  // enlarged or made fullscreen, which is the same rule the upscale asks by
  // (upscale.js, `need` for audio) - and the two must agree, or the panel
  // says a 4K cover needs nothing while a 600 px copy of it is on screen
  // (user, 2026-09-23).
  function stageCoverSize() {
    const dpr = window.devicePixelRatio || 1;
    const scr = window.screen || {};
    const w = scr.width || window.innerWidth;
    const h = scr.height || window.innerHeight;
    // Never below the largest rendition: a window that reports no size at all
    // - a hidden one in a test harness does, and nothing says a browser may
    // not - would otherwise have the screen sent a 96 px thumbnail.
    return Math.max(600, Math.round(Math.max(w, h) * dpr));
  }

  function placeholder(text, small) {
    return el('div', { class: 'mu-ph' + (small ? ' small' : ''), 'aria-hidden': 'true',
      text: (String(text || '?').trim()[0] || '?').toUpperCase() });
  }

  function coverImg(s, size, small) {
    if (!s || !s.cover) return placeholder(s ? s.title : '?', small);
    const img = el('img', { src: coverUrl(s, size), alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', () => img.replaceWith(placeholder(s.title, small)), { once: true });
    return img;
  }

  // The first four songs of a list as a 2x2, or one cover, or a letter.
  // In the list's own order (user, 2026-09-22): these songs are YouTube
  // uploads, so a track carries the cover of ITS video - an album of ten
  // songs has ten different pictures, and the four at the top say more about
  // it than one of them cropped into a square.
  function mosaic(list, title, size) {
    const withCover = [];
    for (const i of list) {
      const s = LIB.songs[i];
      if (s && s.cover) withCover.push(s);
      if (withCover.length === 4) break;
    }
    if (withCover.length === 4) {
      return el('div', { class: 'mu-mosaic' }, withCover.map((s) => coverImg(s, Math.round(size / 2))));
    }
    if (withCover.length) return coverImg(withCover[0], size);
    return placeholder(title);
  }

  // ---- menus, toasts, dialogs ------------------------------------------------------
  const menu = $('muMenu');
  let menuAnchor = null;
  let menuAtPoint = false;             // opened at the pointer (a right click), not from a button
  // What the last right click found: was a menu open, and on which anchor. A right click closes an open
  // menu before the element's own handler runs; a right click on the anchor of the open menu then leaves it closed.
  let ctxWasOpenFor = null;

  function closeMenu() {
    menu.hidden = true;
    menu.replaceChildren();
    if (menuAnchor) menuAnchor.setAttribute('aria-expanded', 'false');
    menuAnchor = null;
    menuAtPoint = false;
  }

  function openMenu(anchor, items, at) {
    closeMenu();
    // An icon ahead of every entry, as YouTube Music has them; where one entry
    // has none, an empty slot keeps its text in line with the others.
    const withIcons = items.some((item) => item && item.icon);
    for (const item of items) {
      if (!item) continue;              // an entry that does not apply to this one
      if (item === '-') { menu.append(el('div', { class: 'mu-menu-sep' })); continue; }
      if (item.label) { menu.append(el('div', { class: 'mu-menu-label', text: item.label })); continue; }
      menu.append(el('button', { type: 'button', role: 'menuitem',
        onclick: (e) => { e.stopPropagation(); closeMenu(); item.run(); } },
        withIcons ? (item.icon ? icon(item.icon) : el('span', { class: 'mu-menu-noicon' })) : null,
        el('span', { class: 'mu-menu-text', text: item.text })));
    }
    menu.hidden = false;
    menuAnchor = anchor;
    menuAtPoint = !!at;
    if (anchor) anchor.setAttribute('aria-expanded', 'true');
    const r = at || anchor.getBoundingClientRect();
    const w = menu.offsetWidth, h = menu.offsetHeight;
    // A menu hangs from the right edge of its button, towards the page - except
    // where the button asks to be the menu's left edge: the bar's ⋯ sits in the
    // middle of the screen, and there the menu opens rightwards over it (user,
    // 2026-09-23: "more to the right ... but not a lot to its left").
    const fromLeft = !at && anchor && anchor.dataset.menuAlign === 'start';
    let x = fromLeft ? r.left : (r.right !== undefined ? r.right : r.x) - w;
    let y = (r.bottom !== undefined ? r.bottom : r.y) + 4;
    if (x < 8) x = 8;
    if (x + w > innerWidth - 8) x = innerWidth - w - 8;
    if (y + h > innerHeight - 8) y = Math.max(8, (r.top !== undefined ? r.top : r.y) - h - 4);
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    const first = menu.querySelector('button');
    if (first) first.focus({ preventScroll: true });
  }

  // A click outside the menu closes it - but not the click that opened it,
  // which bubbles here right after openMenu() and would close the menu before
  // it was ever seen. The bar's ⋯ and the headers' "Add to playlist" did
  // exactly that (user, 2026-09-23: "a menu icon that doesn't do anything").
  document.addEventListener('click', (e) => {
    if (menu.hidden || menu.contains(e.target)) return;
    // (only a menu opened from a button has its opening click to skip; one opened at the pointer by a right
    // click is closed by any left click, on its cover or the song row it belongs to as well - user, 2026-11-06:
    // "not possible to close the right-click menu on a cover with a right click or left click")
    if (!menuAtPoint && menuAnchor && menuAnchor.contains(e.target)) return;
    closeMenu();
  });
  // A right click anywhere outside the menu closes it first (capture, ahead of the element's own handler, which
  // may open a menu of its own at the new spot).
  document.addEventListener('contextmenu', (e) => {
    ctxWasOpenFor = menu.hidden ? null : menuAnchor;
    if (!menu.hidden && !menu.contains(e.target)) closeMenu();
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !menu.hidden) closeMenu(); });
  window.addEventListener('resize', closeMenu);

  let toastTimer = null;
  function toast(text) {
    const t = $('muToast');
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
  }

  function promptDialog({ title, text, value = '', placeholder = '', ok = 'OK', input = true }) {
    const dlg = $('muDialog');
    $('muDialogTitle').textContent = title;
    $('muDialogText').textContent = text || '';
    const field = $('muDialogInput');
    field.hidden = !input;
    field.value = value;
    field.placeholder = placeholder;
    $('muDialogOk').textContent = ok;
    return new Promise((resolve) => {
      // The answer comes from the buttons themselves; the dialog's `close`
      // event only covers Escape. It is dispatched later as a task, and a page
      // in a hidden tab was seen never to get it - so it must not be what
      // the OK depends on.
      let settled = false;
      const finish = (yes) => {
        if (settled) return;
        settled = true;
        dlg.removeEventListener('close', onEscape);
        if (dlg.open) dlg.close();
        resolve(yes ? (input ? field.value.trim() : true) : null);
      };
      const onEscape = () => finish(false);
      dlg.addEventListener('close', onEscape);
      $('muDialogCancel').onclick = () => finish(false);
      $('muDialogForm').onsubmit = (e) => { e.preventDefault(); finish(true); };
      dlg.showModal();
      if (input) field.select();
    });
  }

  // ---- the player -----------------------------------------------------------------
  // Two elements can carry a song: the <audio>, and the <video> that takes
  // the cover's place when the music video is being shown. Everything below
  // drives `mediaEl`, whichever of them that is, so the transport, the clock,
  // the media session and the queue behave the same either way.
  const audio = $('muAudio');
  const videoEl = $('muStageVideo');
  let mediaEl = audio;

  // From media-dl's player.js. A frame-interpolated video plays a stream
  // whose currentTime starts wherever it was asked for, and it carries
  // el._timeline = {offset, duration, loop, seek(t)} to say what the file's
  // own time is. Going through these is what lets Smooth motion sit under an
  // ordinary seek bar.
  function playbackPosition(el) {
    if (!el._timeline) return el.currentTime;
    const pos = el._timeline.offset + el.currentTime;
    const d = el._timeline.duration;
    return el._timeline.loop && d > 0 && isFinite(d) ? pos % d : pos;
  }

  function playbackDuration(el) {
    return el._timeline ? el._timeline.duration : el.duration;
  }

  function seekPlayback(el, seconds) {
    if (el._timeline) el._timeline.seek(seconds);
    else { try { el.currentTime = seconds; } catch (e) { /* not seekable yet */ } }
  }

  // ---- one loudness for everything (vault KSF-058) --------------------------
  // User, 2026-09-27: "All media files should play at equal volume on the
  // music page", and "target the highest adjusted felt volume that does not
  // cause cut-off". Songs ran from -4 to -18 LUFS, and a song's .m4a copy
  // could sit 10 dB under its own video. The server measures every file and
  // sends the gain in dB that brings it to one loudness, as high as the
  // library allows without any file's true peak passing 0 dB - so a quiet
  // file is turned UP. element.volume stops at 1, so the gain is a GainNode:
  // element -> gain -> speakers. The slider stays on element.volume and works
  // as it always did. Until Web Audio runs (a page may only start it from a
  // click or a key), and where it cannot, files are only turned down.
  const LOUD = { ctx: null, nodes: new Map(), off: false };
  // A timeline of what the audio path did, for debugging a hitch: in the console, copy(JSON.stringify(musicRouteLog))
  // (ms = performance.now(), so the gaps between lines are real time).
  const RLOG = (window.musicRouteLog = []);
  function rlog(ev, x) {
    RLOG.push(Object.assign({ ms: Math.round(performance.now()), ev }, x || {}));
    if (RLOG.length > 300) RLOG.shift();
  }
  console.info('[music] build 2026-11-08f (musicRouteLog = audio timeline, musicLevelLog = signal level after the first start, musicDiag = cuts: press Alt+G when you hear one)');
  // The level of the signal inside the page for 8 s from the moment the elements are routed, every 25 ms:
  // [ms, rms, element position, blocks sent to the bridge since the previous row, socket backlog]. A hole in the
  // sound that comes from the page shows as rms 0 (or a position that stands still); a hole that does not show here
  // is downstream (the bridge, Windows, the device).
  const LEVELS = (window.musicLevelLog = []);
  const RM = { on: false, blocks: 0 };

  // ---- playback diagnostics: window.musicDiag --------------------------------------------------------------
  // For "the music cuts out for a moment, or for seconds with chunks every couple of seconds" (user, 2026-11-08;
  // suspected: the tab not in front while the machine is busy). Everything is collected quietly; the console gets
  // at most one "[music-diag]" line every 5 s. When it happens: press Alt+G at once (marks the moment), then
  //   musicDiag.summary()          a table per kind of event, with the usual spacing between repeats
  //   copy(musicDiag.dump())       events + one snapshot per second for the last 5 min, to paste back
  // Event kinds - the page's side of the bridge path:
  //   tap-gap        the main thread got the next 21 ms block of audio late (ms; audioMs = how far apart the audio
  //                  engine made them, so mainDelayMs = ms - audioMs is the main thread's delay)
  //   tap-burst      several blocks arrived back to back after such a gap (the "chunk")
  //   tap-audio-gap  the audio engine itself produced the blocks late (the browser's audio thread starved)
  //   ws-backlog / ws-drop   the socket to the app holds unsent data / blocks were dropped
  //   main-late      a 20 ms timer ran late: the page's main thread was busy or starved
  //   longtask / long-frame   what kept it busy (long-frame names the script)
  //   audio-*, pos-stalled, watch-*   the media element itself (data not arriving, position standing still)
  // and the app's side (bridge 1.2.3+, once a second): bridge-underrun (its queue ran dry; queued_ms),
  // bridge-recv-late (the page's blocks reached the app late), bridge-pull-late (Windows asked the app late).
  // Every event carries hid: true when the tab was hidden or behind another window.
  const DG = (() => {
    const T0 = performance.now();
    const ev = [], snaps = [];
    const S = { blocks: 0, lastWall: 0, lastAud: -1, gapMax: 0, run: 0, lateMax: 0, bridge: null, bridgeAt: 0,
      under: 0, lastPrint: 0, started: false };
    const now = () => Math.round(performance.now() - T0);
    // While the app's own log is on (tray menu "Log playback problems"; the app says so in its info message), the page
    // writes its events into that file too, so both sides share one timeline. At most 30 lines a second.
    let sentWin = 0, sentN = 0;
    function toBridge(text) {
      if (!(BR.ws && BR.ws.readyState === 1 && BR.info && BR.info.logging)) return;
      const p = performance.now();
      if (p - sentWin > 1000) { sentWin = p; sentN = 0; }
      if (++sentN > 30) return;
      bridgeSend({ type: 'log', line: text.replace(/["\\]/g, "'").slice(0, 400) });
    }
    const LOUDKINDS = /^(tap-|ws-|main-late|long-frame|bridge-|pos-stalled|audio-(waiting|stalled|error)|HEARD)/;
    function add(k, x) {
      ev.push(Object.assign({ t: now(), at: new Date().toISOString().slice(11, 23), k, hid: document.hidden }, x || {}));
      if (ev.length > 1200) ev.shift();
      if (k !== 'focus' && k !== 'blur') {
        toBridge(k + (document.hidden ? ' [tab hidden]' : '') + ' ' + Object.entries(x || {}).map(([a, v]) => a + '=' + (v && typeof v === 'object' ? JSON.stringify(v) : v)).join(' '));
      }
      const p = performance.now();
      if (LOUDKINDS.test(k) && p - S.lastPrint > 5000) {
        S.lastPrint = p;
        console.warn('[music-diag] ' + k + (x ? ' ' + JSON.stringify(x) : '') + (document.hidden ? ' (tab hidden)' : '')
          + ' - musicDiag.summary(), copy(musicDiag.dump())');
      }
    }
    // One block from the audio tap reached the main thread. audT = the audio engine's clock when it made it.
    function tap(audT) {
      const p = performance.now();
      S.blocks++;
      if (S.lastWall) {
        const g = p - S.lastWall;
        if (g > S.gapMax) S.gapMax = g;
        const audMs = S.lastAud >= 0 && typeof audT === 'number' ? (audT - S.lastAud) * 1000 : null;
        if (g > 80) add('tap-gap', { ms: Math.round(g), audioMs: audMs === null ? null : Math.round(audMs), mainDelayMs: audMs === null ? null : Math.round(g - audMs) });
        if (audMs !== null && audMs > 45) add('tap-audio-gap', { audioMs: Math.round(audMs) });
        if (g < 5) S.run++; else { if (S.run >= 3) add('tap-burst', { blocks: S.run + 1 }); S.run = 0; }
      }
      S.lastWall = p;
      if (typeof audT === 'number') S.lastAud = audT;
    }
    // The app's once-a-second numbers.
    function stats(m) {
      S.bridge = m; S.bridgeAt = performance.now();
      if (m.underruns > S.under) add('bridge-underrun', { n: m.underruns - S.under, total: m.underruns, queued_ms: m.queued_ms });
      S.under = m.underruns;
      if (m.recv_late > 0) add('bridge-recv-late', { n: m.recv_late, maxGapMs: m.recv_gap_max_ms });
      if (m.pull_late > 0) add('bridge-pull-late', { n: m.pull_late, maxGapMs: m.pull_gap_max_ms });
    }
    const aheadOf = (el) => {
      try {
        for (let i = 0; i < el.buffered.length; i++) {
          if (el.buffered.start(i) <= el.currentTime + 0.1 && el.buffered.end(i) >= el.currentTime) return +(el.buffered.end(i) - el.currentTime).toFixed(1);
        }
      } catch (e) { /* none */ }
      return null;
    };
    // Called once the elements exist.
    function start(getEl, els) {
      if (S.started) return;
      S.started = true;
      let lt = performance.now();
      setInterval(() => {
        const p = performance.now(), late = p - lt - 20; lt = p;
        if (late > S.lateMax) S.lateMax = late;
        if (late > 60) add('main-late', { ms: Math.round(late) });
      }, 20);
      for (const type of ['longtask', 'long-animation-frame']) {
        try {
          new PerformanceObserver((l) => l.getEntries().forEach((e) => {
            if (e.duration > (type === 'longtask' ? 50 : 80)) {
              add(type === 'longtask' ? 'longtask' : 'long-frame', { ms: Math.round(e.duration),
                scripts: (e.scripts || []).slice(0, 2).map((x) => ((x.sourceURL || '').split('/').pop() + ' ' + (x.invoker || '')).trim()) });
            }
          })).observe({ entryTypes: [type] });
        } catch (e) { /* not supported */ }
      }
      for (const el of els) {
        for (const e of ['waiting', 'stalled', 'emptied', 'error', 'pause', 'playing', 'seeking']) {
          el.addEventListener(e, () => { if (el === getEl()) add('audio-' + e, { pos: +el.currentTime.toFixed(2), ahead: aheadOf(el) }); });
        }
      }
      let lastPos = -1, lastAt = 0, stalled = 0;
      setInterval(() => {
        const el = getEl(), p = performance.now();
        if (el && !el.paused && !document.hidden && el.readyState > 0) {
          if (el.currentTime === lastPos) {
            if (!stalled && p - lastAt > 300) { stalled = lastAt; add('pos-stalled', { pos: +lastPos.toFixed(2), ahead: aheadOf(el) }); }
          } else {
            if (stalled) { add('pos-moving', { stalledMs: Math.round(p - stalled) }); stalled = 0; }
            lastPos = el.currentTime; lastAt = p;
          }
        } else { lastPos = -1; lastAt = p; stalled = 0; }
      }, 100);
      for (const [t, o] of [['visibilitychange', document], ['online', window], ['offline', window], ['focus', window], ['blur', window]]) o.addEventListener(t, () => add(t));
      if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) navigator.mediaDevices.addEventListener('devicechange', () => add('devicechange'));
      let lb = 0;
      setInterval(() => {
        const el = getEl(), c = LOUD.ctx;
        if (BR.ws && BR.ws.readyState === 1 && BR.info && S.bridgeAt && /^(1\.2\.([3-9]|\d\d)|1\.[3-9]|[2-9]\.)/.test(String(BR.info.version || ''))
            && performance.now() - S.bridgeAt > 3000) add('bridge-silent', { ms: Math.round(performance.now() - S.bridgeAt) });
        snaps.push({ t: now(), pos: el ? +el.currentTime.toFixed(1) : null, ahead: el ? aheadOf(el) : null, blocks: S.blocks - lb,
          tapGapMax: Math.round(S.gapMax), mainLateMax: Math.round(S.lateMax), wsBuf: BR.ws ? BR.ws.bufferedAmount : -1,
          ctx: c ? c.state : 'none', outLat: c && c.outputLatency ? +c.outputLatency.toFixed(3) : null, hid: document.hidden,
          bq: S.bridge ? S.bridge.queued_ms : null, bUnder: S.bridge ? S.bridge.underruns : null });
        const last = snaps[snaps.length - 1];
        if (snaps.length % 10 === 0) {
          toBridge('snap pos=' + last.pos + ' ahead=' + last.ahead + ' blocks/s=' + last.blocks + ' tapGapMax=' + last.tapGapMax + ' mainLateMax=' + last.mainLateMax
            + ' wsBuf=' + last.wsBuf + ' ctx=' + last.ctx + (last.hid ? ' [tab hidden]' : ''));
        }
        lb = S.blocks; S.gapMax = 0; S.lateMax = 0;
        if (snaps.length > 300) snaps.shift();
      }, 1000);
      document.addEventListener('keydown', (e) => {
        if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.toLowerCase() === 'g') { add('HEARD-IT'); console.info('[music-diag] marked at ' + now() + ' ms'); }
      }, true);
      if (LOUD.ctx) LOUD.ctx.addEventListener('statechange', () => add('ctx-' + LOUD.ctx.state));
    }
    window.musicDiag = {
      mark: (text) => add('MARK', { text }),
      dump: () => JSON.stringify({ ua: navigator.userAgent, now: now(), hidden: document.hidden,
        route: { mode: UP.mode, heights: UP.heights, viaBridge: UP.viaBridge, layout: UP.layout && UP.layout.name },
        bridge: BR.info ? { version: BR.info.version, device: BR.info.device, bed: BR.info.bed } : null,
        ctx: LOUD.ctx ? { state: LOUD.ctx.state, rate: LOUD.ctx.sampleRate, base: LOUD.ctx.baseLatency, out: LOUD.ctx.outputLatency } : null,
        events: ev, snaps, routeLog: RLOG.slice(-40) }),
      summary: () => {
        const by = {};
        ev.forEach((e) => (by[e.k] = by[e.k] || []).push(e));
        console.table(Object.entries(by).map(([k, es]) => {
          const ts = es.map((e) => e.t), c = {};
          ts.slice(1).forEach((t, i) => { const v = Math.round((t - ts[i]) / 50) * 50; c[v] = (c[v] || 0) + 1; });
          const top = Object.entries(c).sort((a, b) => b[1] - a[1])[0];
          return { kind: k, count: es.length, hidden: es.filter((e) => e.hid).length, firstMs: ts[0], lastMs: ts[ts.length - 1],
            commonGapMs: top ? top[0] : '', times: top ? top[1] : '' };
        }));
      },
    };
    return { add, tap, stats, start };
  })();
  function startRouteMeter() {
    if (RM.on || !LOUD.ctx) return;
    RM.on = true;
    const an = LOUD.ctx.createAnalyser();
    an.fftSize = 512;
    for (const node of LOUD.nodes.values()) node.connect(an);
    const data = new Float32Array(512);
    const t0 = performance.now();
    let lastBlocks = 0;
    const iv = setInterval(() => {
      an.getFloatTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      LEVELS.push([Math.round(performance.now()), +Math.sqrt(sum / data.length).toFixed(4), +mediaEl.currentTime.toFixed(3),
        RM.blocks - lastBlocks, BR.ws ? BR.ws.bufferedAmount : -1]);
      lastBlocks = RM.blocks;
      if (performance.now() - t0 > 8000) { clearInterval(iv); try { an.disconnect(); } catch (e) { /* gone */ } }
    }, 25);
  }
  // iOS stops Web Audio when the screen locks - that would end playback in
  // the pocket; it also ignores element.volume, so it gets neither.
  const IOS = /iP(hone|od|ad)/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let userVolume = 1;

  // The gain in dB that plays a file at the common loudness: the server's,
  // or the typical one for a file it has not measured yet. With a target of
  // the viewer's own (user, 2026-11-03: "Add a value for target LUFS"), the
  // file's level decides: min(target, its clipping ceiling) - its loudness,
  // rounded down like the server's.
  function gainDbOf(g, level) {
    if (typeof LIB.loud.target !== 'number') return 0;
    const want = ME.targetLufs;
    if (typeof want !== 'number') return typeof g === 'number' ? g : (LIB.loud.unknown || 0);
    const delta = want - LIB.loud.target;
    if (Array.isArray(level)) return Math.floor((Math.min(want, level[1]) - level[0]) * 10 + 1e-9) / 10;
    // Measured, but from a catalogue without levels: down is always safe, up is not.
    if (typeof g === 'number') return delta < 0 ? g + delta : g;
    return Math.min(0, (LIB.loud.unknown || 0) + delta);
  }

  // The loudness picker in the side bar (user, 2026-11-05: "increments of 3 dB, a Max option, no auto,
  // one option may carry recommended"). Steps of 3 dB, "Max" (0: every file as loud as it can go
  // without clipping - its own ceiling decides) and the library's own target, marked recommended;
  // choosing that one stores null, which keeps following the library target.
  const TARGET_LUFS_CHOICES = [-6, -9, -12, -15, -18, -21, -24, -27, -30];
  const TARGET_MAX = 0;
  const lufsText = (v) => `${String(v.toFixed(1)).replace('-', '−')} LUFS`;
  function renderTargetLufs() {
    const sel = $('muTargetLufs');
    if (!sel) return;
    const rec = typeof LIB.loud.target === 'number' ? LIB.loud.target : null;
    const values = [...TARGET_LUFS_CHOICES];
    if (typeof ME.targetLufs === 'number' && ME.targetLufs !== TARGET_MAX && !values.includes(ME.targetLufs)
        && ME.targetLufs !== rec) {
      values.push(ME.targetLufs);                       // an older setting that is not on the grid
    }
    if (rec !== null && !values.includes(rec)) values.push(rec);
    values.sort((a, b) => b - a);
    sel.replaceChildren(
      el('option', { value: String(TARGET_MAX), text: 'Max (loudest without clipping)' }),
      ...values.map((v) => el('option', { value: v === rec ? 'rec' : String(v),
        text: v === rec ? `${lufsText(v)} (recommended)` : lufsText(v) })));
    sel.value = typeof ME.targetLufs === 'number' ? String(ME.targetLufs) : (rec !== null ? 'rec' : String(TARGET_MAX));
  }

  function setTargetLufs(value) {
    ME.targetLufs = typeof value === 'number' && Number.isFinite(value) ? value : null;
    const s = current();
    if (s) {
      audio._gainDb = gainDbOf(s.gain, s.level);
      videoEl._gainDb = gainDbOf(s.videoGain, s.videoLevel);
      applyVolume();
    }
    api('/api/music/settings', { method: 'PUT', body: { target_lufs: ME.targetLufs } });
    toast(ME.targetLufs === null ? 'Loudness: recommended'
      : ME.targetLufs === TARGET_MAX ? 'Loudness: as loud as each file allows'
        : `Everything plays at ${lufsText(ME.targetLufs)}`);
  }

  // The bus gain: sqrt(2/N) for N speakers, times whatever the slider holds beyond the stop.
  function busFactor() {
    const n = speakerCount();
    return n > 2 ? Math.sqrt(2 / n) * Math.max(1, userVolume) : 1;
  }

  // What each element plays at: the slider times the file's gain - through
  // the GainNode when the element has one, else folded into element.volume,
  // where only a cut fits.
  function applyVolume() {
    if (UP.input && !UP.muting) {
      const g = UP.input.gain;
      const now = UP.input.context.currentTime;
      g.cancelScheduledValues(now);
      g.setTargetAtTime(busFactor(), now, 0.01);      // no jump: a click otherwise
    }
    for (const m of [audio, videoEl]) {
      const factor = Math.pow(10, (m._gainDb || 0) / 20);
      const node = LOUD.nodes.get(m);
      if (node) {
        node.gain.value = factor;
        m.volume = Math.min(1, userVolume);
      } else {
        m.volume = Math.min(1, userVolume) * Math.min(1, factor);
      }
    }
  }

  // Route both elements through a GainNode, once. Called on every click and
  // key and on every play: the AudioContext is made on the first one that is
  // allowed to start it, and the elements are routed only once it RUNS - an
  // element routed into a context that is not running is silent.
  // Returns a promise that settles once the elements are routed (or it is clear they will not be).
  function startLoudness() {
    if (LOUD.nodes.size) {
      if (LOUD.ctx.state !== 'running') LOUD.ctx.resume().catch(() => {});
      return Promise.resolve();
    }
    if (LOUD.off) return Promise.resolve();
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || IOS) { LOUD.off = true; return Promise.resolve(); }
    // Not before the first click or key: a context made earlier (the queue
    // restored on load) is refused and only warns in the console.
    if (!LOUD.ctx && navigator.userActivation && !navigator.userActivation.hasBeenActive) return Promise.resolve();
    try {
      if (!LOUD.ctx) {
        LOUD.ctx = new AC();
        // Something else took the sound (a call, another app): back as soon
        // as the browser lets it, or the routed elements stay silent.
        LOUD.ctx.addEventListener('statechange', () => {
          if (LOUD.nodes.size && LOUD.ctx.state !== 'running' && !mediaEl.paused) LOUD.ctx.resume().catch(() => {});
        });
      }
    } catch (e) { LOUD.off = true; return Promise.resolve(); }
    const route = () => {
      if (LOUD.ctx.state !== 'running' || LOUD.nodes.size || LOUD.off) return;
      for (const m of [audio, videoEl]) {
        try {
          const node = LOUD.ctx.createGain();
          LOUD.ctx.createMediaElementSource(m).connect(node).connect(upmixInput());
          LOUD.nodes.set(m, node);
        } catch (e) {
          LOUD.off = true;       // that element stays on element.volume
        }
      }
      rlog('elements routed into the graph');
      startRouteMeter();
      buildUpmix();
      applyVolume();
      if (infoOpen) renderInfo();
    };
    if (LOUD.ctx.state === 'running') { route(); return Promise.resolve(); }
    return LOUD.ctx.resume().then(route, () => {});
  }

  // The first start. play() used to begin on the element's own output; the 'play' event then built the audio engine
  // and captured the element into it, and that handover was audible: the song, a gap of about 200 ms, the song
  // again (and on a bridge, once more when the route moved). Now the engine is built and the element routed
  // BEFORE the element starts, so the first sound already comes from the graph. Never longer than 500 ms: playback
  // must not depend on it.
  for (const m of [audio, videoEl]) {
    const nativePlay = m.play.bind(m);
    m.play = function () {
      rlog('play() called', { routed: LOUD.nodes.size, ctx: LOUD.ctx ? LOUD.ctx.state : 'none', el: m === audio ? 'audio' : 'video' });
      if (LOUD.nodes.size || LOUD.off) return nativePlay();
      const routed = startLoudness();
      return Promise.race([routed, new Promise((r) => setTimeout(r, 500))]).then(() => { rlog('element starts (after routing)', { routed: LOUD.nodes.size }); return nativePlay(); });
    };
  }

  // ---- every ear-level speaker (user, 2026-11-04/05) -------------------------
  // "The music page should detect a dolby atmos/multi channel output device if
  // it is the active output device and use every ear-level channel. Height
  // channels should be a toggle in the nav-bar, default off." Then: "Deliver to
  // ear-level channels equally. The stereo/all ear-level selection should be a
  // toggle between those two states." Then: a companion app for the height
  // channels, which Windows keeps from a browser.
  //
  // Two ways to reach the speakers:
  //  1. The browser's own output. destination.maxChannelCount is the channel
  //     count of the device; the browser hands a device its channels in WAVE
  //     order (FL FR FC LFE BL BR SL SR, then the top four), so the count names
  //     the layout. Windows gives a browser at most the 7.1 bed of a Dolby Atmos
  //     device, so heights are out of reach on this path.
  //  2. The companion app (ksf-spatial-bridge, Windows): this page taps the
  //     stereo bus, sends it to the app over a local WebSocket, and the app plays
  //     it through Windows Spatial Sound, which does reach the height speakers.
  //     While it is connected the browser's own output is silent.
  //
  // The library is stereo. "Stereo" plays L and R on the front pair and nothing
  // else. "All" plays every ear-level speaker at the same level: each speaker
  // on the left side gets L, each on the right side gets R, the centre gets the
  // mid (L+R)/2. The heights switch adds the height speakers the same way. The
  // LFE stays silent (the receiver's bass management feeds the sub). A
  // multichannel file is folded to stereo first.
  //   ears: [left, right] index pairs of the ear-level speakers beyond the front pair
  //   tops: the same for the height speakers
  const SURROUND = {
    4: { name: 'Quad', center: -1, ears: [[2, 3]], tops: [] },
    6: { name: '5.1', center: 2, ears: [[4, 5]], tops: [] },
    8: { name: '7.1', center: 2, ears: [[4, 5], [6, 7]], tops: [] },
    10: { name: '5.1.4', center: 2, ears: [[4, 5]], tops: [[6, 7], [8, 9]] },
    12: { name: '7.1.4', center: 2, ears: [[4, 5], [6, 7]], tops: [[8, 9], [10, 11]] },
  };
  // probed: the output's channel count read at load, before any sound runs
  // (user: "I don't see the button" - it used to wait for the first play).
  // mode: 'all' (every ear-level speaker) or 'stereo' (the front pair only).
  const UP = { input: null, nodes: [], layout: null, probed: 0, heights: store('heights') === true,
    mode: store('upmode') === 'stereo' ? 'stereo' : 'all' };

  // ---- the companion app ------------------------------------------------------
  // Show the "Spatial app" download link only when this server can actually offer it (hosted zip or a configured
  // redirect); a manual redirect answer counts as "there is something to download".
  (function showSpatialLink() {
    const a = document.getElementById('muSpatialLink');
    if (!a) return;
    fetch(a.getAttribute('href'), { method: 'HEAD', redirect: 'manual', credentials: 'same-origin' })
      .then((r) => { if (r.ok || r.type === 'opaqueredirect') a.hidden = false; })
      .catch(() => {});
  })();

  const BRIDGE_URL = 'ws://127.0.0.1:47321/';
  const BR = { ws: null, node: null, silent: null, info: null, retry: 0, loading: false };
  const TAP_CODE = `class AhTap extends AudioWorkletProcessor {
    constructor() { super(); this.b = new Float32Array(2048); this.n = 0; }
    process(inputs, outputs) {
      const x = inputs[0];
      if (x && x[0]) {
        const l = x[0], r = x[1] || x[0];
        for (let k = 0; k < l.length; k++) { this.b[this.n++] = l[k]; this.b[this.n++] = r[k]; }
        if (this.n >= 2048) { this.port.postMessage({ b: this.b.slice(0, this.n), t: currentTime }); this.n = 0; }
      }
      return true;
    }
  }
  registerProcessor('ah-tap', AhTap);`;

  function bridgeActive() { return !!(BR.ws && BR.ws.readyState === 1 && BR.info && BR.node); }

  function bridgeSend(obj) {
    if (BR.ws && BR.ws.readyState === 1) BR.ws.send(JSON.stringify(obj));
  }

  function bridgeSettings() {
    bridgeSend({ type: 'mode', mode: UP.mode, heights: UP.heights, rate: LOUD.ctx ? LOUD.ctx.sampleRate : 48000 });
  }

  // Tries every 3 s for 30 s; then it stops until the user touches the output or heights switch (bridgeKick).
  const BRIDGE_TRY_MS = 3000, BRIDGE_WINDOW_MS = 30000;
  function bridgeKick() {
    if (!BR.stopped || BR.ws) return;
    BR.stopped = false;
    BR.windowStart = Date.now();
    if (BR.yielded) bridgeClaim();
    bridgeConnect();
  }

  // ---- one tab at a time ------------------------------------------------------------------------------------
  // The app serves one page at a time: a second page's connection replaces the first's, whose retry then replaces
  // it again (user, 2026-11-08: tabs fighting over the app, a reconnect every second or so, cut-outs each time). So
  // the tabs agree who has it, over a BroadcastChannel: a tab that starts playing (or whose Heights/7.1 button is
  // pressed) CLAIMS the app and every other tab lets go and stays on the browser's own output until it claims it
  // back; a tab that opens while another one is playing through the app does not take it.
  const BR_TAB = Math.random().toString(36).slice(2);
  let BR_BC = null;
  try { BR_BC = new BroadcastChannel('ksf-spatial-bridge'); } catch (e) { /* no channel: tabs behave as before */ }
  function bridgeHeld() { return !!(BR.ws && BR.ws.readyState === 1); }
  function bridgeYield(why) {
    if (BR.yielded) return;
    BR.yielded = true;
    BR.stopped = true;
    BR.state = 'yielded';
    clearTimeout(BR.retry);
    console.info('Bridge: another tab is using it, this tab plays through the browser (' + why + '). Press play, Heights or 7.1 here to take it back.');
    if (typeof DG !== 'undefined') DG.add('bridge-yield', { why });
    if (BR.ws) { try { BR.ws.close(); } catch (e) { /* already closed */ } }
  }
  function bridgeClaim() {
    BR.yielded = false;
    if (BR_BC) BR_BC.postMessage({ t: 'claim', id: BR_TAB });
    if (typeof DG !== 'undefined') DG.add('bridge-claim', {});
  }
  if (BR_BC) {
    BR_BC.onmessage = (ev) => {
      const m = ev.data || {};
      if (m.id === BR_TAB) return;
      if (m.t === 'claim' && (bridgeHeld() || !BR.stopped || BR.ws)) bridgeYield('claimed by another tab');
      else if (m.t === 'who' && bridgeHeld() && !mediaEl.paused) BR_BC.postMessage({ t: 'busy', id: BR_TAB });
      else if (m.t === 'busy') BR.busyOther = true;
      else if (m.t === 'playing' && !mediaEl.paused) mediaEl.pause();      // another tab of this site started: this one pauses
    };
  }
  // First connection: ask whether another tab is playing through the app; if so stay out of its way.
  function bridgeArbitrate() {
    if (BR.arbDone) return true;
    if (!BR_BC) { BR.arbDone = true; return true; }
    if (!BR.arbPending) {
      BR.arbPending = true;
      BR_BC.postMessage({ t: 'who', id: BR_TAB });
      setTimeout(() => {
        BR.arbDone = true;
        BR.arbPending = false;
        if (BR.busyOther) bridgeYield('another tab is playing through it');
        else bridgeConnect();
      }, 250);
    }
    return false;
  }

  function bridgeConnect() {
    if (BR.ws || BR.stopped || IOS || !('WebSocket' in window)) return;
    if (BR.holdOff && Date.now() < BR.holdOff) return;      // just lost it: the retry timer decides, not a rebuild
    if (!bridgeArbitrate()) return;
    if (!BR.windowStart) BR.windowStart = Date.now();
    let ws;
    try { ws = new WebSocket(BRIDGE_URL); } catch (e) { console.warn('[music-bridge] cannot open', BRIDGE_URL, e); return; }
    BR.ws = ws;
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      BR.fails = 0;
      BR.windowStart = 0;
      BR.openAt = Date.now();
      rlog('bridge: socket open');
      if (BR.state !== 'connected') console.info('Bridge connected at ' + BRIDGE_URL);
      BR.state = 'connected';
      bridgeSend({ type: 'hello', rate: LOUD.ctx ? LOUD.ctx.sampleRate : 48000, mode: UP.mode, heights: UP.heights });
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'stats') { DG.stats(msg); return; }
      if (msg.type === 'replaced') { bridgeYield('the app was taken by another client'); return; }     // app 1.2.4+: no reconnect fight
      if (msg.type === 'info') {
        rlog('bridge: info', { bed: msg.bed, heights: msg.heights });
        BR.info = msg;
        // (a rebuild is a short mute and reconnect: not when the bridge route already stands, as two of them in a row
        // were an audible gap)
        bridgeEnsureTap().then(() => { if (LOUD.ctx && UP.input && !UP.viaBridge) buildUpmix(); else renderSurround(); });
      } else if (msg.type === 'gone') {           // the app lost Windows Spatial Sound: the browser plays again
        console.warn('[music-bridge] the app reports no Windows Spatial Sound');
        BR.info = null;
        if (LOUD.ctx && UP.input) buildUpmix(); else renderSurround();
      }
    };
    const gone = (ev) => {
      if (BR.ws !== ws) return;
      // Taken away again and again within seconds of connecting = another client keeps taking it (the 'replaced'
      // notice may not arrive): stop fighting, like after a notice (user, 2026-11-08: 7.1.4 -> 7.1 every ~2 s).
      if (!BR.yielded && BR.openAt) {
        if (Date.now() - BR.openAt < 4000) BR.shortLives = (BR.shortLives || 0) + 1; else BR.shortLives = 0;
        BR.openAt = 0;
        if (BR.shortLives >= 2) {
          BR.shortLives = 0;
          BR.yielded = true; BR.stopped = true; BR.state = 'yielded';
          clearTimeout(BR.retry);
          console.info('Bridge: another client keeps taking it, this page plays through the browser. Press play, Heights or 7.1 to take it back.');
          if (typeof DG !== 'undefined') DG.add('bridge-yield', { why: 'taken repeatedly' });
        }
      }
      if (BR.yielded) {                       // let go on purpose: no warning, no retry; the browser plays again
        BR.ws = null;
        const had = BR.info;
        BR.info = null;
        if (had) { if (LOUD.ctx && UP.input) buildUpmix(); else renderSurround(); }
        return;
      }
      // One line per change of state, not per attempt. (The browser itself prints a red "WebSocket connection
      // failed" for every refused attempt; page code cannot silence that, so the attempts are limited instead.)
      if (BR.state === 'connected') { console.warn('Bridge connection lost. Attempting to reconnect.'); BR.state = 'lost'; }
      else if (!BR.state) { console.warn('Bridge not reachable at ' + BRIDGE_URL + ' (app not running, or the browser blocked local network access for this site)'); BR.state = 'unreachable'; }
      BR.fails = (BR.fails || 0) + 1;
      BR.holdOff = Date.now() + BRIDGE_TRY_MS - 200;
      BR.ws = null;
      const had = BR.info;
      BR.info = null;
      if (had) {
        if (LOUD.ctx && UP.input) buildUpmix();
        else renderSurround();
      }
      clearTimeout(BR.retry);
      if (!BR.windowStart) BR.windowStart = Date.now();          // a connection that was up and dropped: a fresh 30 s
      if (Date.now() - BR.windowStart < BRIDGE_WINDOW_MS) BR.retry = setTimeout(bridgeConnect, BRIDGE_TRY_MS);   // the app may be started later
      else {
        BR.stopped = true;
        console.info('Bridge: not found, no longer trying. Touch the output or heights switch to try again.');
      }
    };
    ws.onclose = gone;
    ws.onerror = () => { /* onclose follows */ };
  }

  // The tap: an AudioWorklet on the stereo bus whose blocks go to the app.
  // One shared promise: whoever asks while it is being made waits for the same tap (a caller that returned at once
  // used to leave the page on the browser's own 7.1 output until the next toggle).
  function bridgeEnsureTap() {
    if (BR.node || !LOUD.ctx || !LOUD.ctx.audioWorklet) return Promise.resolve();
    if (!BR.tapP) BR.tapP = bridgeMakeTap().finally(() => { BR.tapP = null; });
    return BR.tapP;
  }

  async function bridgeMakeTap() {
    BR.loading = true;
    const t0 = performance.now(), state0 = LOUD.ctx ? LOUD.ctx.state : 'none';
    try {
      const url = URL.createObjectURL(new Blob([TAP_CODE], { type: 'text/javascript' }));
      await LOUD.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      const node = new AudioWorkletNode(LOUD.ctx, 'ah-tap', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2] });
      node.port.onmessage = (ev) => {
        RM.blocks++;
        DG.tap(ev.data.t);
        if (BR.ws && BR.ws.readyState === 1) {
          if (BR.ws.bufferedAmount < (1 << 20)) BR.ws.send(ev.data.b.buffer);
          else DG.add('ws-drop', { buffered: BR.ws.bufferedAmount });
          if (BR.ws.bufferedAmount > 262144) DG.add('ws-backlog', { bytes: BR.ws.bufferedAmount });
        }
      };
      BR.silent = LOUD.ctx.createGain();       // keeps the worklet pulled without making a sound
      BR.silent.gain.value = 0;
      node.connect(BR.silent).connect(LOUD.ctx.destination);
      BR.node = node;
      rlog('bridge: tap ready');
      console.info('Bridge tap ready in ' + Math.round(performance.now() - t0) + ' ms (audio context was ' + state0 + ')');
    } catch (e) {
      BR.node = null;
    } finally {
      BR.loading = false;
    }
  }

  // A context made only to read the device's channel count, closed at once. Made
  // before the first click it stays suspended, which is all this needs.
  function probeOutput() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC || IOS) { renderSurround(); return; }
    try {
      const c = new AC();
      UP.probed = c.destination.maxChannelCount || 2;
      c.close().catch(() => {});
    } catch (e) { /* no audio output at all */ }
    renderSurround();
    bridgeConnect();
  }

  // Where both elements' gains go: a stereo bus the matrix reads from.
  function upmixInput() {
    if (!UP.input) {
      UP.input = LOUD.ctx.createGain();
      UP.input.channelCount = 2;
      UP.input.channelCountMode = 'explicit';
      UP.input.channelInterpretation = 'speakers';
    }
    return UP.input;
  }

  // (Re)wire the bus: to the companion app when it is there, else straight
  // through on stereo or the matrix on a surround device. Again whenever the
  // output device, the app or a switch changes.
  // A change of mode used to be heard: the graph was torn down and rebuilt while the music ran, and
  // the bus gain jumped to its new value before the new routing stood (user, 2026-11-05: "a
  // fraction of a second of crackling and excessive volume"). Now the bus fades to silence in
  // 30 ms, the rebuild happens while it is silent, and the new level fades in over 50 ms.
  let upFadeTimer = 0;
  function buildUpmix() {
    const ctx = LOUD.ctx;
    if (!ctx || !UP.input || ctx.state !== 'running') { buildUpmixNow(); return; }
    const g = UP.input.gain;
    const t = ctx.currentTime;
    UP.muting = true;
    g.cancelScheduledValues(t);
    g.setValueAtTime(g.value, t);
    g.linearRampToValueAtTime(0, t + 0.03);
    clearTimeout(upFadeTimer);
    upFadeTimer = setTimeout(() => {
      if (!UP.input) { UP.muting = false; return; }
      buildUpmixNow();
      const g2 = UP.input.gain;
      const t2 = ctx.currentTime;
      g2.cancelScheduledValues(t2);
      g2.setValueAtTime(0, t2);
      g2.linearRampToValueAtTime(busFactor(), t2 + 0.05);
      UP.muting = false;
    }, 60);
  }

  function buildUpmixNow() {
    const ctx = LOUD.ctx;
    if (!ctx || !UP.input) return;
    try { UP.input.disconnect(); } catch (e) { /* not connected yet */ }
    for (const n of UP.nodes) { try { n.disconnect(); } catch (e) { /* gone */ } }
    UP.nodes = [];
    UP.viaBridge = false;
    if (SPK.sp) { try { UP.input.connect(SPK.sp); } catch (e) { /* as is */ } }
    bridgeConnect();
    // The app has answered but its tap is still being made: route through the browser for now and switch over the
    // moment the tap exists, instead of staying on the browser's output until the next toggle.
    if (!BR.node && BR.info) bridgeEnsureTap().then(() => { if (BR.node && bridgeActive() && !UP.viaBridge) buildUpmix(); });
    // At the first start the bridge is usually a few milliseconds from being ready (connecting, or its tap being
    // made). Playing the browser's own 7.1 meanwhile and moving over afterwards is audible: a different sound and
    // a hitch. So wait in silence, at most 600 ms, and start on the bridge route; after that (or without a bridge)
    // the browser plays as before.
    if (bridgeActive()) { BR.waitDone = true; clearTimeout(BR.waitT); }
    const waiting = !BR.waitDone && ((BR.info && !BR.node) || (BR.ws && !BR.info && BR.ws.readyState <= 1));   // connecting, or open and not yet answered
    if (waiting) {
      rlog('buildUpmix: silent, waiting for the bridge', { info: !!BR.info, node: !!BR.node, ws: BR.ws ? BR.ws.readyState : 'none' });
      clearTimeout(BR.waitT);
      // the safety net only acts if the bridge route still does not stand when it fires (it fired a second rebuild
      // 600 ms after the route was already chosen: a fade out, rewire and fade in, about 200 ms of silence)
      BR.waitT = setTimeout(() => { BR.waitDone = true; if (!UP.viaBridge) buildUpmix(); }, 600);
      renderSurround();
      return;
    }
    const dest = ctx.destination;
    const max = dest.maxChannelCount || 2;
    rlog('buildUpmix: route chosen', { bridge: bridgeActive(), max });
    if (bridgeActive()) {
      UP.layout = null;
      UP.viaBridge = true;
      try {
        dest.channelCount = Math.min(2, max);
        dest.channelCountMode = 'explicit';
        dest.channelInterpretation = 'speakers';
      } catch (e) { /* as is */ }
      UP.input.connect(BR.node);
      bridgeSettings();
      renderSurround();
      return;
    }
    const lay = SURROUND[max] || null;
    UP.layout = lay;
    try {
      if (!lay) {
        dest.channelCount = Math.min(2, max);
        dest.channelCountMode = 'explicit';
        dest.channelInterpretation = 'speakers';
        UP.input.connect(dest);
        return;
      }
      dest.channelCount = max;
      dest.channelCountMode = 'explicit';
      dest.channelInterpretation = 'discrete';
      const keep = (n) => { UP.nodes.push(n); return n; };
      const merge = keep(ctx.createChannelMerger(max));
      const split = keep(ctx.createChannelSplitter(2));
      UP.input.connect(split);
      merge.connect(dest);
      split.connect(merge, 0, 0);
      split.connect(merge, 1, 1);
      if (UP.mode === 'all') {
        const mid = keep(ctx.createGain());
        mid.gain.value = 0.5;
        split.connect(mid, 0);
        split.connect(mid, 1);
        if (lay.center >= 0) mid.connect(merge, 0, lay.center);
        const pairs = UP.heights ? lay.ears.concat(lay.tops) : lay.ears;
        for (const [l, r] of pairs) {
          split.connect(merge, 0, l);
          split.connect(merge, 1, r);
        }
      }
    } catch (e) {
      // A device the browser will not open that wide: plain stereo.
      UP.layout = null;
      try { dest.channelCount = Math.min(2, max); dest.channelInterpretation = 'speakers'; } catch (e2) { /* as is */ }
      try { UP.input.connect(dest); } catch (e3) { /* already */ }
    } finally {
      renderSurround();
    }
  }

  // The output button (stereo <-> all ear-level) and the Heights switch, in the player bar.
  function renderSurround() {
    const chip = $('muSurround');
    const btn = $('muHeights');
    if (!chip || !btn) return;
    // The app counts as there as soon as it has answered: its tap needs the audio engine, which only exists once
    // something has played, but the switches should not wait for that.
    const viaApp = !!(BR.ws && BR.ws.readyState === 1 && BR.info);
    const routed = LOUD.ctx && UP.input;
    const lay = viaApp ? null : (routed ? UP.layout : (SURROUND[UP.probed] || null));
    const multi = viaApp || !!lay;
    chip.hidden = false;
    btn.hidden = false;
    const name = viaApp ? (BR.info.bed || 'Spatial') : lay ? lay.name : '';
    chip.disabled = !multi;
    chip.setAttribute('aria-pressed', String(multi && UP.mode === 'all'));
    chip.textContent = !multi ? (UP.probed === 1 ? 'Mono' : 'Stereo') : (UP.mode === 'all' ? `${name} all` : 'Stereo');
    chip.title = viaApp ? `Playing through the companion app (${BR.info.device || 'Windows Spatial Sound'}). Click: ${UP.mode === 'all' ? 'front pair only' : 'every ear-level speaker'}`
      : lay ? `Output: ${lay.name}. Click: ${UP.mode === 'all' ? 'stereo, the front pair only' : 'every ear-level speaker, at the same level'}`
        : IOS || LOUD.off ? 'This browser plays stereo only'
          : 'Output: stereo - the music plays as it is';
    const can = viaApp ? !!BR.info.heights : !!lay && lay.tops.length > 0;
    btn.disabled = !can || UP.mode !== 'all';
    btn.setAttribute('aria-pressed', String(UP.heights));
    btn.title = can ? (UP.heights ? 'Height speakers on' : 'Height speakers off')
      : multi ? 'The browser gets no height channels from Windows - run the companion app (the Spatial app link in the top bar) to reach them'
        : 'This output has no height speakers';
    syncVolumeRange();
  }

  function setHeights(on) {
    UP.heights = !!on;
    store('heights', UP.heights);
    buildUpmix();
    toast(UP.heights ? 'Height speakers on' : 'Height speakers off');
  }

  function setUpMode(mode) {
    UP.mode = mode === 'stereo' ? 'stereo' : 'all';
    store('upmode', UP.mode);
    buildUpmix();
    toast(UP.mode === 'all' ? 'Every ear-level speaker' : 'Stereo, front pair only');
  }

  // ---- speaker activity (user, 2026-11-05) -------------------------------------
  // Right-click on the cover of the song that is playing (the player bar's or the big one) opens a
  // menu with "Speaker activity": a live top-down room with every speaker of the output, each
  // glowing with the level it is being given right now. The levels are measured on the stereo bus
  // (L, R and the mid) and handed to the speakers exactly as the upmix hands them: the left-side
  // speakers carry L, the right-side ones R, the centre the mid; "stereo" leaves everything but
  // the front pair dark and the heights stay dark until their switch is on.
  const SPK = { el: null, canvas: null, raf: 0, sp: null, an: null, buf: null, lv: {}, nodes: [] };

  // x: -1 left .. 1 right, y: -1 front .. 1 back (listener at 0,0)
  const SPK_POS = {
    FL: { x: -0.5, y: -0.95, src: 'L' }, FR: { x: 0.5, y: -0.95, src: 'R' }, C: { x: 0, y: -1.05, src: 'M' },
    SL: { x: -1.0, y: 0.0, src: 'L' }, SR: { x: 1.0, y: 0.0, src: 'R' },
    BL: { x: -0.65, y: 0.9, src: 'L' }, BR: { x: 0.65, y: 0.9, src: 'R' },
    TFL: { x: -0.4, y: -0.5, src: 'L', top: true }, TFR: { x: 0.4, y: -0.5, src: 'R', top: true },
    TBL: { x: -0.4, y: 0.5, src: 'L', top: true }, TBR: { x: 0.4, y: 0.5, src: 'R', top: true },
  };

  function spkNames() {
    const viaApp = bridgeActive();
    const lay = viaApp ? null : (LOUD.ctx && UP.input ? UP.layout : (SURROUND[UP.probed] || null));
    const bed = viaApp ? String(BR.info.bed || '') : lay ? lay.name : '';
    if (!bed) return { bed: '', names: ['FL', 'FR'] };
    const tops = /\.4$/.test(bed) ? ['TFL', 'TFR', 'TBL', 'TBR'] : [];
    if (bed === 'Quad') return { bed, names: ['FL', 'FR', 'BL', 'BR'] };
    if (/^7\.1/.test(bed)) return { bed, names: ['FL', 'FR', 'C', 'SL', 'SR', 'BL', 'BR', ...tops] };
    if (/^5\.1/.test(bed)) return { bed, names: ['FL', 'FR', 'C', 'SL', 'SR', ...tops] };
    return { bed, names: ['FL', 'FR'] };
  }

  function spkActive(name) {
    if (name === 'FL' || name === 'FR') return true;
    if (UP.mode !== 'all') return false;
    return !SPK_POS[name].top || UP.heights;
  }

  // Taps on the stereo bus; buildUpmix reconnects the splitter after it clears the bus.
  function spkTap() {
    if (SPK.an) return true;
    const ctx = LOUD.ctx;
    if (!ctx || !UP.input) return false;
    const mk = () => { const a = ctx.createAnalyser(); a.fftSize = 1024; a.smoothingTimeConstant = 0; return a; };
    SPK.sp = ctx.createChannelSplitter(2);
    UP.input.connect(SPK.sp);
    SPK.an = { L: mk(), R: mk(), M: mk() };
    SPK.sp.connect(SPK.an.L, 0);
    SPK.sp.connect(SPK.an.R, 1);
    const mid = ctx.createGain();
    mid.gain.value = 0.5;
    SPK.sp.connect(mid, 0);
    SPK.sp.connect(mid, 1);
    mid.connect(SPK.an.M);
    SPK.nodes = [mid];
    SPK.buf = new Float32Array(1024);
    return true;
  }

  function spkLevel(a) {
    a.getFloatTimeDomainData(SPK.buf);
    let sum = 0;
    for (let i = 0; i < SPK.buf.length; i++) sum += SPK.buf[i] * SPK.buf[i];
    const db = 20 * Math.log10(Math.sqrt(sum / SPK.buf.length) + 1e-9);
    return Math.max(0, Math.min(1, (db + 60) / 60));
  }

  function spkDraw() {
    SPK.raf = 0;
    if (!SPK.el) return;
    const cv = SPK.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    // From the panel itself, not from :root: the page's theme tokens can resolve differently there (the labels came
    // out black on the dark panel). The panel's own heading and info line are what is readable on it.
    const css = getComputedStyle(SPK.el);
    const accent = css.getPropertyValue('--accent').trim() || '#4c8dff';
    const muted = getComputedStyle($('muSpkInfo')).color || '#9aa0a6';
    const heading = SPK.el.querySelector('strong');
    const ink = (heading && getComputedStyle(heading).color) || '#eee';
    const line = css.getPropertyValue('--line-strong').trim() || '#555';
    const cx = w / 2, cy = h / 2 + 4, R = Math.min(w, h) / 2 - 30;
    const ok = spkTap();
    const lv = { L: 0, R: 0, M: 0 };
    if (ok && !document.hidden && !mediaEl.paused) {
      lv.L = spkLevel(SPK.an.L); lv.R = spkLevel(SPK.an.R); lv.M = spkLevel(SPK.an.M);
    }
    g.strokeStyle = line;
    g.lineWidth = 1;
    g.beginPath(); g.arc(cx, cy, R * 1.12, 0, Math.PI * 2); g.stroke();
    g.beginPath(); g.arc(cx, cy, R * 0.55, 0, Math.PI * 2); g.stroke();
    g.fillStyle = muted;
    g.beginPath(); g.arc(cx, cy, 3, 0, Math.PI * 2); g.fill();       // the listener
    g.font = '11px system-ui, sans-serif';
    g.textAlign = 'center';
    const { bed, names } = spkNames();
    for (const name of names) {
      const p = SPK_POS[name];
      const on = spkActive(name);
      const target = on ? lv[p.src] : 0;
      SPK.lv[name] = Math.max(target, (SPK.lv[name] || 0) * 0.92);    // quick up, slow down
      const v = SPK.lv[name];
      const x = cx + p.x * R, y = cy + p.y * R;
      const r = (p.top ? 8 : 11) + 16 * v;
      if (on) {
        g.fillStyle = accent;
        g.globalAlpha = 0.18 + 0.82 * v;
        g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
        g.globalAlpha = 1;
      }
      g.strokeStyle = on ? accent : line;
      g.lineWidth = p.top ? 1 : 2;
      g.setLineDash(p.top ? [3, 3] : []);
      g.beginPath(); g.arc(x, y, p.top ? 8 : 11, 0, Math.PI * 2); g.stroke();
      g.setLineDash([]);
      g.fillStyle = on ? ink : muted;
      g.fillText(name, x, y + (p.top ? 22 : 28));
    }
    $('muSpkInfo').textContent = !ok ? 'Play something to see the speakers'
      : `${bed || 'Stereo'} - ${UP.mode === 'all' ? (UP.heights ? 'every speaker, heights on' : 'every ear-level speaker') : 'front pair only'}`
        + (bridgeActive() ? ' - via the Spatial app' : '');
    SPK.raf = requestAnimationFrame(spkDraw);
  }

  function closeSpeakerChart() {
    if (SPK.raf) cancelAnimationFrame(SPK.raf);
    SPK.raf = 0;
    if (SPK.el) SPK.el.remove();
    SPK.el = SPK.canvas = null;
  }

  function openSpeakerChart() {
    if (SPK.el) return;
    SPK.canvas = el('canvas', { class: 'mu-spk-canvas' });
    SPK.el = el('div', { class: 'mu-spk', role: 'dialog', 'aria-label': 'Speaker activity' },
      el('div', { class: 'mu-spk-head' },
        el('strong', { text: 'Speaker activity' }),
        el('button', { type: 'button', class: 'mu-icon-btn', 'aria-label': 'Close', text: '×', onclick: closeSpeakerChart })),
      SPK.canvas,
      el('div', { class: 'mu-spk-info', id: 'muSpkInfo' }));
    document.body.append(SPK.el);
    SPK.raf = requestAnimationFrame(spkDraw);
  }

  // The menu of the playing song's cover: the speaker chart, and the cover itself to the clipboard.
  //
  // The picture is fetched, decoded and re-encoded as PNG in a WORKER. An upscaled cover is tens of
  // megapixels; decoding and encoding that on the page's own thread is a task of a hundred milliseconds
  // or more, and the music hitches while it runs (user, 2026-11-06: "clicking copy upscaled cover can cause a
  // hitch"; the same cost caused the hitch when the upscaled cover is swapped in, see js/upscale.js).
  const COVER_WORKER = `self.onmessage = async (e) => {
    try {
      const res = await fetch(e.data.url, { credentials: 'same-origin' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      let blob = await res.blob();
      if (blob.type !== 'image/png') {
        const bmp = await createImageBitmap(blob);
        const oc = new OffscreenCanvas(bmp.width, bmp.height);
        oc.getContext('bitmaprenderer').transferFromImageBitmap(bmp);
        blob = await oc.convertToBlob({ type: 'image/png' });
      }
      self.postMessage({ blob });
    } catch (err) { self.postMessage({ error: String(err) }); }
  };`;
  function coverPngInWorker(url) {
    return new Promise((resolve, reject) => {
      let worker, workerUrl;
      const done = (fn, v) => { clearTimeout(timer); try { worker.terminate(); } catch (e) { /* gone */ } URL.revokeObjectURL(workerUrl); fn(v); };
      const timer = setTimeout(() => done(reject, new Error('timeout')), 60000);
      try {
        if (typeof Worker !== 'function' || typeof OffscreenCanvas !== 'function' || typeof createImageBitmap !== 'function') throw new Error('no worker');
        workerUrl = URL.createObjectURL(new Blob([COVER_WORKER], { type: 'text/javascript' }));
        worker = new Worker(workerUrl);
      } catch (e) { clearTimeout(timer); if (workerUrl) URL.revokeObjectURL(workerUrl); reject(e); return; }
      worker.onmessage = (ev) => (ev.data && ev.data.blob ? done(resolve, ev.data.blob) : done(reject, new Error((ev.data && ev.data.error) || 'no picture')));
      worker.onerror = () => done(reject, new Error('worker'));
      worker.postMessage({ url: new URL(url, location.href).href });
    });
  }
  // The same on the page's own thread - only when no worker could do it. KNOWN TO CAUSE AUDIO HITCHES for a
  // large cover while a song plays; it exists so the copy still works.
  async function coverPngOnPage(url) {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const blob = await r.blob();
    if (blob.type === 'image/png') return blob;
    const bmp = await createImageBitmap(blob);
    const c = document.createElement('canvas');
    c.width = bmp.width; c.height = bmp.height;
    c.getContext('2d').drawImage(bmp, 0, 0);
    return new Promise((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error('no png'))), 'image/png'));
  }
  // The write starts inside the click and takes a promise of the picture: a browser lets a page write the
  // clipboard only within the gesture, and the picture has to be fetched first.
  async function copyCover(url, what) {
    try {
      const made = coverPngInWorker(url).catch((e) => {
        console.warn('[music] cover copy: the worker path failed (' + (e && e.message) + '), converting on the main thread; this can cause an audio hitch');
        return coverPngOnPage(url);
      });
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': made })]);
      toast(`Copied the ${what} cover`);
    } catch (e) {
      toast(`The ${what} cover could not be copied`);
    }
  }
  function coverMenu(e) {
    // a right click on the cover whose menu is open closes it (the capture handler above already did)
    if (ctxWasOpenFor && ctxWasOpenFor === e.currentTarget) { ctxWasOpenFor = null; return; }
    const idx = Q.pos >= 0 ? Q.items[Q.pos] : -1;
    const s = idx >= 0 ? LIB.songs[idx] : null;
    const items = [{ text: SPK.el ? 'Hide speaker activity' : 'Speaker activity',
      run: () => (SPK.el ? closeSpeakerChart() : openSpeakerChart()) }];
    if (s && s.cover) {
      items.push('-',
        // above the ladder (96/300/600) the server answers with the picture inside the file
        { text: 'Copy original cover', run: () => copyCover(coverUrl(s, 100000), 'original') },
        { text: 'Copy upscaled cover', run: () => {
          const up = typeof previewUpscale !== 'undefined' ? previewUpscale : null;
          if (!up || !up.url || up.key !== s.ref) { toast('This cover has no upscaled copy yet'); return; }
          copyCover(up.url, 'upscaled');
        } });
    }
    openMenu(e.currentTarget, items, { x: e.clientX, y: e.clientY });
  }

  // One volume for both elements: it belongs to the player, not to whichever
  // element happens to be playing.
  //
  // User, 2026-11-05: in a mode beyond stereo the slider has a stop at the normal maximum and goes
  // beyond. With N speakers playing, the normal maximum gives each one sqrt(2/N) of the
  // amplitude, so the speakers together carry the acoustic POWER of a stereo pair at full volume
  // (owner, 2026-11-05: measure power, not amplitude); past the stop the slider keeps
  // going up to sqrt(N/2) times that, where every speaker plays at full power (the old maximum of a
  // single speaker). Units: 1 is the stop (stereo's full volume); the slider shows percent.
  // The element's own volume stops at 1; what lies beyond (and the 2/N share) is the bus gain,
  // the gain node every upmix path starts from.
  let wantedVolume = 1;                    // what the user chose; the range may clamp what plays
  function speakerCount() {
    if (UP.mode !== 'all') return 2;
    const n = spkNames().names.filter(spkActive).length;
    return Math.max(2, n);
  }
  function volumeMax() { return Math.sqrt(speakerCount() / 2); }
  function syncVolumeRange() {
    const n = speakerCount();
    const max = Math.sqrt(n / 2);
    userVolume = Math.min(wantedVolume, max);
    const slider = $('muVolume');
    if (slider) {
      slider.max = String(Math.round(max * 100));
      slider.value = String(Math.round(userVolume * 100));
      if (max > 1) slider.setAttribute('list', 'muVolStop'); else slider.removeAttribute('list');
      slider.style.width = max > 1 ? '150px' : '';
      slider.title = max > 1 ? `Past the mark every speaker plays louder than a stereo pair would (${n} speakers)` : '';
    }
    applyVolume();
  }
  function setVolume(v) {
    wantedVolume = v;
    userVolume = Math.min(v, volumeMax());
    applyVolume();
  }

  function setMuted(m) {
    audio.muted = m;
    videoEl.muted = m;
  }

  // The player's volume and the slider that shows it, together.
  function showVolume(v) {
    setVolume(v);
    $('muVolume').value = String(Math.round(userVolume * 100));
  }
  // radio: {id, kind, key, title, played: Set} while a radio (or autoplay)
  // fills the queue; autoStart: where autoplay's songs begin in it.
  const Q = { items: [], pos: -1, shuffle: false, original: null, repeat: 'off', context: null,
    radio: null, autoStart: -1 };
  // How the next song to start came to play, for the listening history:
  // "user" - picked by hand, "list" - the list went on to it (a radio's
  // songs are "radio" whatever this says).
  let playSrc = 'list';

  function current() {
    return Q.pos >= 0 ? LIB.songs[Q.items[Q.pos]] : null;
  }

  // The length to show and seek against: the browser's when it knows one - an
  // Ogg stream it cannot seek in reports Infinity - else the catalogue's.
  function playDur() {
    const d = playbackDuration(mediaEl);
    if (Number.isFinite(d) && d > 0) return d;
    const s = current();
    return s ? s.dur || 0 : 0;
  }

  function fileUrl(s) {
    return `/api/media/file?root=${encodeURIComponent(s.root)}&path=${encodeURIComponent(s.path)}`;
  }

  // ---- the next song, fetched whole in advance ---------------------------------------------------------------
  // A song is normally streamed: the element asks the server for it piece by piece while it plays, so the whole song
  // depends on the server answering steadily. When the server is thrashing (user, 2026-11-08: the music cuts out
  // for 0.4-3 s, plays a chunk, cuts out again, for 30-120 s at a time; the console showed six "waiting" events in a
  // row on one song, and the host was out of swap with a load average near 40), every piece is a chance to stall.
  // So a little after a song starts, the NEXT one in the queue is downloaded completely, into memory, and played from
  // there when its turn comes: nothing about it depends on the server any more. The song that was picked by hand (or
  // skipped to) has no copy yet and streams as before.
  // Kept: at most PRE_MAX songs and PRE_BYTES in all, never the one the element is playing, never one over PRE_ONE.
  const PRE = { map: new Map(), inflight: new Set(), timer: 0 };
  const PRE_MAX = 4, PRE_BYTES = 160 << 20, PRE_ONE = 80 << 20;
  const preKey = (s) => s.root + '\u0000' + s.path;
  function preUrl(s) {
    const e = s && PRE.map.get(preKey(s));
    return e ? e.url : null;
  }
  function preEvict() {
    let total = 0;
    for (const e of PRE.map.values()) total += e.size;
    for (const [k, e] of PRE.map) {
      if (PRE.map.size <= PRE_MAX && total <= PRE_BYTES) break;
      if (audio.src === e.url || audio.currentSrc === e.url) continue;     // never the one that is playing
      PRE.map.delete(k);
      total -= e.size;
      URL.revokeObjectURL(e.url);
    }
  }
  async function prefetchSong(s) {
    if (!s || !s.root || !s.path || (typeof wantVideo !== 'undefined' && wantVideo && s.video)) return;
    const k = preKey(s);
    if (PRE.map.has(k) || PRE.inflight.has(k)) return;
    PRE.inflight.add(k);
    const t0 = performance.now();
    try {
      const res = await fetch(fileUrl(s), { credentials: 'same-origin' });
      const len = Number(res.headers.get('content-length')) || 0;
      if (!res.ok || len > PRE_ONE) { DG.add('prefetch-skip', { why: res.ok ? 'size ' + Math.round(len / 1048576) + ' MB' : 'HTTP ' + res.status }); return; }
      const blob = await res.blob();
      if (blob.size > PRE_ONE) return;
      PRE.map.set(k, { url: URL.createObjectURL(blob), size: blob.size, at: Date.now() });
      DG.add('prefetch-done', { ms: Math.round(performance.now() - t0), mb: +(blob.size / 1048576).toFixed(1), title: String(s.title || '').slice(0, 40) });
      preEvict();
    } catch (e) {
      DG.add('prefetch-fail', { ms: Math.round(performance.now() - t0), why: String(e && e.message || e).slice(0, 60) });
    } finally {
      PRE.inflight.delete(k);
    }
  }
  function prefetchNext() {
    if (!Q.items.length || Q.pos < 0) return;
    let i = Q.pos + 1;
    if (i >= Q.items.length) { if (Q.repeat === 'all') i = 0; else return; }
    prefetchSong(LIB.songs[Q.items[i]]);
  }
  window.musicPrefetch = { has: (s) => !!preUrl(s), cached: () => [...PRE.map.values()].map((e) => ({ mb: +(e.size / 1048576).toFixed(1), at: new Date(e.at).toISOString().slice(11, 19) })) };

  function shuffled(list) {
    const a = list.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  // Play `list` (song numbers) from `start`; `context` names where it came
  // from; src "user" when that song itself was clicked, not just its list.
  function playList(list, start = 0, opts = {}) {
    if (!list.length) return;
    Q.context = opts.context || null;
    Q.radio = null;
    Q.autoStart = -1;
    playSrc = opts.src || 'list';
    if (opts.shuffle) Q.shuffle = true;
    if (Q.shuffle) {
      const first = list[start] !== undefined && !opts.shuffle ? list[start] : list[Math.floor(Math.random() * list.length)];
      Q.original = list.slice();
      Q.items = [first, ...shuffled(list.filter((i, n) => !(i === first && n === list.indexOf(first))))];
      Q.pos = 0;
    } else {
      Q.original = null;
      Q.items = list.slice();
      Q.pos = Math.min(Math.max(0, start), list.length - 1);
    }
    loadCurrent(true);
  }

  function loadCurrent(autoplay, at = 0) {
    const s = current();
    updateShuffleButton();
    if (!s) return;
    $('muPlayer').hidden = false;
    playInto(s, { at, autoplay });
    if (autoplay) recordPlay(s);
    topUpRadio();
    showNow(s);
    renderQueue();
    markPlaying();
    saveQueue();
  }

  // ---- the song, or its video ----------------------------------------------
  // YouTube Music can show a song's music video instead of its cover, and the
  // switch stays on for the next song that has one. Here the video is the
  // same upload as the song - the catalogue matched them by video id, or by
  // the file name for the visualiser channels whose audio was pulled out of
  // their videos (music_library.video_index) - so it is played from the
  // library's own file, through media-dl's js/interp.js.
  const VIDEO_MODE_KEY = 'videoMode';
  let wantVideo = store(VIDEO_MODE_KEY) === true;

  function videoFileUrl(v) {
    return `/api/media/file?root=${encodeURIComponent(v.root)}&path=${encodeURIComponent(v.path)}`;
  }

  function videoShowing() {
    return mediaEl === videoEl;
  }

  // Hand the transport to an element, taking the volume across and stopping
  // whatever the other one was doing.
  function useElement(el) {
    if (mediaEl !== el) {
      el.muted = mediaEl.muted;
      mediaEl.pause();
    }
    mediaEl = el;
  }

  // Let go of what is on the video element: interp.js keeps a watcher on it
  // that re-plans and starts a stream again by itself, and segments already
  // asked for keep arriving. Before EVERY new video as well, not only when
  // the song goes back to its own audio: one video following another left
  // the plan of the one before running, and it took the picture and the
  // sound back to that song while the page had moved on (user, 2026-09-23).
  function releaseVideo() {
    const st = videoEl._smooth;
    if (st) {
      st.active = false;
      st.generation = (st.generation || 0) + 1;
      if (typeof stopSegments === 'function') stopSegments(st);
    }
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl._timeline = null;
    videoEl.load();
  }

  // ... and show the cover again: the song is playing on its own now.
  function stopVideo() {
    releaseVideo();
    videoEl.hidden = true;
    $('muStageCover').hidden = false;
    setEnhancement('interpolation', null);
    $('muStageSettings').hidden = true;
  }

  // The seek bar and the clock belong to the song that is STARTING. Without
  // this they keep the one that ended - a full blue line, when it played to
  // the end - until the new song first reports a time, which for an
  // interpolated video is several seconds of planning later (user,
  // 2026-09-23: "The forward button highlights the progress bar"). The
  // catalogue's length is used until the file says otherwise.
  function showProgress(pos, dur) {
    const seek = $('muSeek');
    const pct = dur > 0 ? Math.max(0, Math.min(1000, Math.round((pos / dur) * 1000))) : 0;
    seek.value = pct;
    seek.style.setProperty('--pct', `${pct / 10}%`);
    $('muTime').textContent = `${fmtTime(pos)} / ${fmtTime(dur)}`;
  }

  function playInto(s, { at = 0, autoplay = true } = {}) {
    renderModes(s);
    showProgress(at, s.dur || 0);
    // Each element at its own file's gain: the song's .m4a and its video can
    // be 10 dB apart, and the Song | Video switch must not jump.
    audio._gainDb = gainDbOf(s.gain, s.level);
    videoEl._gainDb = gainDbOf(s.videoGain, s.videoLevel);
    applyVolume();
    startLoudness();
    if (wantVideo && s.video) {
      useElement(videoEl);
      releaseVideo();
      audio.removeAttribute('src');
      audio.load();
      videoEl.hidden = false;
      $('muStageCover').hidden = true;
      $('muStageSettings').hidden = typeof renderPlayerSettings !== 'function';
      const f = { root: s.video.root, path: s.video.path, type: 'video',
        name: s.title, url: videoFileUrl(s.video) };
      if (at > 0) videoEl.addEventListener('loadedmetadata', () => seekPlayback(videoEl, at), { once: true });
      if (typeof playPreviewVideo === 'function') {
        playPreviewVideo(videoEl, f);          // Smooth motion, or the file itself
      } else {
        videoEl.src = f.url;
        videoEl.play().catch(() => updatePlayButton());
      }
      if (!autoplay) videoEl.pause();
      refreshInfo();
      return;
    }
    useElement(audio);
    stopVideo();
    const cached = preUrl(s);
    audio.src = cached || fileUrl(s);
    DG.add('track-start', { from: cached ? 'memory' : 'stream', title: String(s.title || '').slice(0, 40) });
    clearTimeout(PRE.timer);
    PRE.timer = setTimeout(prefetchNext, 1500);         // once this song is under way: the one after it
    if (at) {
      audio.addEventListener('loadedmetadata', () => { try { audio.currentTime = at; } catch (e) { /* not seekable yet */ } }, { once: true });
    }
    if (autoplay) audio.play().catch(() => updatePlayButton());
    refreshInfo();
  }

  // The Song | Video pill: only for a song whose video the library holds.
  function renderModes(s) {
    const box = $('muStageModes');
    const has = !!(s && s.video);
    box.hidden = !has;
    $('muModeSong').setAttribute('aria-pressed', String(!(has && wantVideo)));
    $('muModeVideo').setAttribute('aria-pressed', String(has && wantVideo));
  }

  // Switching mid-song keeps the place and whether it was playing: the same
  // recording either way.
  function setVideoMode(on) {
    if (wantVideo === on) return;
    wantVideo = on;
    store(VIDEO_MODE_KEY, on);
    const s = current();
    if (!s) { renderModes(s); return; }
    playInto(s, { at: playbackPosition(mediaEl) || 0, autoplay: !mediaEl.paused });
    // The screen changes with the mode. showStage() is what puts the cover
    // there, asks for it at the screen's size and starts the upscale, and it
    // stops short while a video is showing - so the song this switch goes back
    // to would otherwise keep whatever small copy was last loaded, with no
    // upscale ever asked for (user, 2026-09-23).
    if (stageOpen()) showStage(s);
  }

  function showNow(s) {
    $('muNowTitle').textContent = s.title;
    const sub = $('muNowSub');
    sub.replaceChildren(el('a', { href: artistHref(s.artist), text: s.artist }),
      s.album && s.album !== s.title ? ' • ' : '', s.album && s.album !== s.title ? s.album : '');
    const img = $('muNowCover');
    if (s.cover) {
      img.src = coverUrl(s, 96);
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
        artwork: s.cover ? [96, 300, 600].map((n) => ({ src: coverUrl(s, n), sizes: `${n}x${n}`, type: 'image/webp' })) : [],
      });
    }
  }

  function next(auto) {
    if (!Q.items.length) return;
    if (auto && Q.repeat === 'one') {
      seekPlayback(mediaEl, 0);
      mediaEl.play().catch(() => {});
      return;
    }
    if (Q.pos < Q.items.length - 1) {
      Q.pos += 1;
    } else if (Q.repeat === 'all') {
      Q.pos = 0;
    } else if (ME.autoplay && startAutoplay()) {
      Q.pos += 1;
    } else {
      if (auto) updatePlayButton();
      return;
    }
    loadCurrent(true);
  }

  function prev() {
    if (playbackPosition(mediaEl) > 4 || Q.pos <= 0) {
      seekPlayback(mediaEl, 0);
      return;
    }
    Q.pos -= 1;
    loadCurrent(true);
  }

  function playNext(i) {
    if (Q.pos < 0) { playList([i], 0, { src: 'user' }); return; }
    Q.items.splice(Q.pos + 1, 0, i);
    if (Q.autoStart >= Q.pos + 1) Q.autoStart += 1;
    renderQueue();
    saveQueue();
    toast('Playing next');
  }

  // At the end of what was queued - before autoplay's songs, as in YouTube
  // Music: autoplay only fills what comes after.
  function addToQueue(list) {
    if (Q.pos < 0) { playList(list); return; }
    if (Q.autoStart > Q.pos) {
      Q.items.splice(Q.autoStart, 0, ...list);
      Q.autoStart += list.length;
    } else {
      Q.items.push(...list);
    }
    renderQueue();
    saveQueue();
    toast(list.length === 1 ? 'Added to the queue' : `${plural(list.length, 'song')} added to the queue`);
  }

  function togglePlay() {
    if (!current()) return;
    if (mediaEl.paused) mediaEl.play().catch(() => {});
    else mediaEl.pause();
  }

  function updatePlayButton() {
    const playing = !mediaEl.paused && !mediaEl.ended;
    setIcon($('muPlay'), playing ? 'pause' : 'play');
    $('muPlay').setAttribute('aria-label', playing ? 'Pause' : 'Play');
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
  }

  function updateShuffleButton() {
    $('muShuffle').setAttribute('aria-pressed', String(Q.shuffle));
    const rep = $('muRepeat');
    rep.setAttribute('aria-pressed', String(Q.repeat !== 'off'));
    rep.setAttribute('aria-label', `Repeat: ${Q.repeat === 'off' ? 'off' : Q.repeat === 'all' ? 'all' : 'this song'}`);
    setIcon(rep, Q.repeat === 'one' ? 'repeat1' : 'repeat');
  }

  function toggleShuffle() {
    if (!Q.items.length) {
      Q.shuffle = !Q.shuffle;
      updateShuffleButton();
      return;
    }
    const cur = Q.items[Q.pos];
    if (!Q.shuffle) {
      Q.original = Q.items.slice();
      const rest = Q.items.filter((_, n) => n !== Q.pos);
      Q.items = [cur, ...shuffled(rest)];
      Q.pos = 0;
      Q.shuffle = true;
    } else {
      Q.shuffle = false;
      if (Q.original) {
        Q.items = Q.original;
        Q.pos = Math.max(0, Q.items.indexOf(cur));
      }
      Q.original = null;
    }
    updateShuffleButton();
    renderQueue();
    saveQueue();
  }

  function cycleRepeat() {
    Q.repeat = Q.repeat === 'off' ? 'all' : Q.repeat === 'all' ? 'one' : 'off';
    updateShuffleButton();
    saveQueue();
  }

  function saveQueue() {
    const r = Q.radio;
    store('queue', {
      refs: Q.items.map((i) => LIB.songs[i] && LIB.songs[i].ref).filter(Boolean),
      pos: Q.pos, shuffle: Q.shuffle, repeat: Q.repeat, t: Math.floor(playbackPosition(mediaEl) || 0),
      muted: mediaEl.muted, context: Q.context,
      radio: r ? { id: r.id, kind: r.kind, key: r.key, title: r.title, originRef: r.originRef } : null,
      autoStart: Q.autoStart,
    });
  }

  let restored = false;
  function restoreQueueOnce() {
    if (restored) return;
    restored = true;
    const saved = store('queue');
    if (!saved) return;
    // The volume used to travel with the queue; a page from before it had a
    // key of its own brings it over once.
    if (typeof saved.volume === 'number' && typeof store('volume') !== 'number') {
      store('volume', saved.volume);
      showVolume(saved.volume);
    }
    setMuted(!!saved.muted);
    updateVolumeIcon();
    Q.repeat = ['off', 'all', 'one'].includes(saved.repeat) ? saved.repeat : 'off';
    Q.shuffle = !!saved.shuffle;
    const items = (saved.refs || []).map((r) => LIB.byRef.get(r)).filter((i) => i !== undefined);
    if (!items.length) { updateShuffleButton(); return; }
    Q.items = items;
    Q.pos = Math.min(Math.max(0, saved.pos || 0), items.length - 1);
    Q.context = typeof saved.context === 'string' ? saved.context : null;
    const r = saved.radio;
    if (r && typeof r.id === 'string') {
      // What it played before this page came back is with the server: until
      // that answers, what the queue has passed counts as played.
      Q.radio = { id: r.id, kind: r.kind, key: r.key, title: r.title,
        originRef: typeof r.originRef === 'string' ? r.originRef : null,
        played: new Set(items.slice(0, Q.pos + 1)) };
      Q.autoStart = Number.isInteger(saved.autoStart) ? saved.autoStart : -1;
      loadRadioPlayed(Q.radio);
    }
    loadCurrent(false, saved.t || 0);
    updatePlayButton();
  }

  function updateVolumeIcon() {
    const off = mediaEl.muted || userVolume === 0;
    setIcon($('muMute'), off ? 'mute' : 'volume');
    $('muMute').setAttribute('aria-label', off ? 'Unmute' : 'Mute');
  }

  function bindPlayer() {
    $('muPlay').addEventListener('click', togglePlay);
    $('muNext').addEventListener('click', () => next(false));
    $('muPrev').addEventListener('click', prev);
    $('muShuffle').addEventListener('click', toggleShuffle);
    $('muRepeat').addEventListener('click', cycleRepeat);
    $('muQueueBtn').addEventListener('click', () => toggleQueue());
    $('muQueueClose').addEventListener('click', () => toggleQueue(false));
    $('muQueueClear').addEventListener('click', clearQueue);
    $('muAutoplay').addEventListener('click', () => setAutoplay(!ME.autoplay));
    $('muAutoplaySwitch').addEventListener('change', (e) => setAutoplay(e.target.checked));
    $('muNowMore').addEventListener('click', (e) => {
      const s = current();
      if (s) songMenu(e.currentTarget, s.i, null);
    });
    $('muMute').addEventListener('click', () => { setMuted(!mediaEl.muted); updateVolumeIcon(); saveQueue(); });
    // The volume is whatever was last chosen, from the moment the page starts
    // - 100 % until something is (user, 2026-09-23: "The volume should be
    // restored from whatever the user last selected. Default is 100%"). A key
    // of its own, written on every move of the slider: it used to ride along
    // in the saved queue, written only when the slider was let go and read
    // only once the library had loaded, and it started at 80 %.
    $('muVolume').addEventListener('input', (e) => {
      let v = Number(e.target.value) / 100;
      // The stop: close to the normal maximum the slider holds on it.
      if (volumeMax() > 1 && v !== 1 && Math.abs(v - 1) <= 0.04) { v = 1; e.target.value = '100'; }
      setVolume(v);
      store('volume', v);
      setMuted(false);
      updateVolumeIcon();
    });
    $('muVolume').addEventListener('change', saveQueue);
    const kept = store('volume');
    showVolume(typeof kept === 'number' && kept >= 0 && kept <= 10 ? kept : 1);
    // A click or a key is what a page may start Web Audio from; a play that
    // comes from the media keys or autoplay alone may not.
    for (const type of ['pointerdown', 'keydown']) {
      document.addEventListener(type, startLoudness, { capture: true, passive: true });
    }

    const seek = $('muSeek');
    let dragging = false;
    seek.addEventListener('input', () => {
      dragging = true;
      seek.style.setProperty('--pct', `${seek.value / 10}%`);
      const d = playDur();
      if (d) $('muTime').textContent = `${fmtTime(d * seek.value / 1000)} / ${fmtTime(d)}`;
    });
    seek.addEventListener('change', () => {
      const d = playDur();
      if (d) {
        seekPlayback(mediaEl, d * seek.value / 1000);
      }
      dragging = false;
    });
    let lastSave = 0;
    let lastPos = 0;
    function bindMedia(el) {
      el.addEventListener('timeupdate', () => {
        if (el !== mediaEl) return;                  // the one being left behind
        const d = playDur();
        const pos = playbackPosition(el);
        if (!dragging && d) {
          const pct = Math.min(1000, Math.round((pos / d) * 1000));
          seek.value = pct;
          seek.style.setProperty('--pct', `${pct / 10}%`);
        }
        $('muTime').textContent = `${fmtTime(pos)} / ${fmtTime(d)}`;
        if (Date.now() - lastSave > 5000) { lastSave = Date.now(); saveQueue(); }
        if ('mediaSession' in navigator && navigator.mediaSession.setPositionState && d) {
          try {
            navigator.mediaSession.setPositionState({ duration: d, position: Math.min(pos, d), playbackRate: 1 });
          } catch (e) { /* a browser that dislikes the numbers mid-load */ }
        }
        // Smooth motion's fallback is a stream the server loops, so it never
        // ends and 'ended' never fires: the song is over when the position
        // wraps round to the start.
        if (el._timeline && el._timeline.loop && d > 2 && lastPos > d - 1.5 && pos < 1.5) next(true);
        lastPos = pos;
      });
      el.addEventListener('play', () => {
        if (BR_BC) BR_BC.postMessage({ t: 'playing', id: BR_TAB });       // the other tabs of this site pause
        if (BR.yielded) bridgeKick();         // playing here now: this tab takes the app back
        else if (bridgeHeld() && BR_BC) bridgeClaim();      // and tells the others it has it
        if (el !== mediaEl) { el.pause(); return; }
        startLoudness();
        updatePlayButton();
      });
      el.addEventListener('pause', () => { if (el === mediaEl) { updatePlayButton(); saveQueue(); } });
      el.addEventListener('ended', () => { if (el === mediaEl) next(true); });
      el.addEventListener('error', () => {
        if (el !== mediaEl || !current()) return;
        // The video may simply not be playable here (a codec this browser has
        // no decoder for): fall back to the song rather than skipping it.
        // interp.js has its own handler for a stream that fails - it falls
        // back to the file - so only a video that cannot be played at all
        // sends us back to the song.
        if (el === videoEl) {
          if (el._smooth && el._smooth.active) return;
          toast('That video could not be played here - playing the song');
          setVideoMode(false);
          return;
        }
        toast(`"${current().title}" could not be played - skipping it`);
        setTimeout(() => next(true), 800);
      });
    }
    bindMedia(audio);
    bindMedia(videoEl);
    // Playback watch (console only, "[music-watch]"): when the music hiccups or stops, was it the page, the
    // network/server, or the audio engine? Each line carries what is needed to tell them apart:
    //  - bufferedAheadS ~0 and networkState 2 (loading)  -> the data did not arrive (server / network)
    //  - longTasksLast3s not empty                        -> the page's main thread was busy (client)
    //  - "audio clock lagged"                             -> the browser's audio engine missed deadlines (client / system)
    //  - "pause" with a large msSinceInput                -> nothing the user did paused it
    const WATCH = { events: [], long: [], lastInput: 0, stallStart: 0, lastPos: -1, lastPosAt: 0, clk: null };
    const watchState = () => {
      const el = mediaEl;
      let ahead = null;
      try {
        for (let i = 0; el && el.buffered && i < el.buffered.length; i++) {
          if (el.buffered.start(i) <= el.currentTime + 0.1 && el.buffered.end(i) >= el.currentTime) ahead = +(el.buffered.end(i) - el.currentTime).toFixed(2);
        }
      } catch (e) { /* no buffered ranges */ }
      const now = performance.now();
      return {
        at: new Date().toISOString(),
        pos: el ? +el.currentTime.toFixed(2) : null,
        dur: el && isFinite(el.duration) ? +el.duration.toFixed(1) : null,
        paused: el ? el.paused : null,
        readyState: el ? el.readyState : null,
        networkState: el ? el.networkState : null,
        bufferedAheadS: ahead,
        ctx: LOUD.ctx ? LOUD.ctx.state : 'none',
        longTasksLast3s: WATCH.long.filter((x) => now - x.at < 3000).map((x) => Math.round(x.dur) + 'ms'),
        online: navigator.onLine,
        bridge: BR.ws ? BR.ws.readyState : 'none',
        msSinceInput: Math.round(now - WATCH.lastInput),
        src: el ? String(el.currentSrc || '').slice(-70) : '',
      };
    };
    window.musicWatchLog = WATCH.events;
    const watchLog = (kind, extra) => {
      const w = watchState();
      DG.add('watch-' + kind, Object.assign({ pos: w.pos, ahead: w.bufferedAheadS, longTasks: w.longTasksLast3s.length }, extra || {}));
      console.warn('[music-watch] ' + kind, Object.assign(w, extra || {}));
    };
    for (const ev of ['pointerdown', 'keydown']) document.addEventListener(ev, () => { WATCH.lastInput = performance.now(); }, true);
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) { WATCH.long.push({ at: e.startTime + e.duration, dur: e.duration }); }
        if (WATCH.long.length > 40) WATCH.long.splice(0, WATCH.long.length - 40);
      }).observe({ entryTypes: ['longtask'] });
    } catch (e) { /* long-task timing not available */ }
    for (const ev of ['playing', 'pause', 'waiting', 'stalled', 'seeking', 'seeked']) audio.addEventListener(ev, () => rlog('audio element: ' + ev, { pos: +audio.currentTime.toFixed(2) }));
    LOUD.ctx && LOUD.ctx.addEventListener('statechange', () => rlog('ctx state: ' + LOUD.ctx.state));
    for (const el of [audio, videoEl]) {
      for (const ev of ['waiting', 'stalled', 'emptied', 'abort']) {
        el.addEventListener(ev, () => { if (el === mediaEl && !el.paused) watchLog(ev); });
      }
      el.addEventListener('error', () => { if (el === mediaEl) watchLog('error', { code: el.error && el.error.code, message: el.error && el.error.message }); });
      // pause / resume are collected for debugging, not printed: in the console type  musicWatchLog  (or
      // copy(JSON.stringify(musicWatchLog, null, 1)))
      for (const ev of ['pause', 'play']) {
        el.addEventListener(ev, () => {
          if (el !== mediaEl || (ev === 'pause' && el.ended)) return;
          WATCH.events.push(Object.assign({ event: ev }, watchState()));
          if (WATCH.events.length > 300) WATCH.events.splice(0, WATCH.events.length - 300);
        });
      }
      el.addEventListener('ended', () => {
        if (el !== mediaEl) return;
        const d = el.duration;
        if (isFinite(d) && d - el.currentTime > 2) watchLog('ended early', { missingS: +(d - el.currentTime).toFixed(1) });
      });
    }
    setInterval(() => {
      const el = mediaEl, now = performance.now();
      // the position stopped moving while it should be playing
      if (el && !el.paused && !el.ended && el.readyState > 0 && !document.hidden) {
        if (el.currentTime === WATCH.lastPos) {
          if (!WATCH.stallStart && now - WATCH.lastPosAt > 350) { WATCH.stallStart = WATCH.lastPosAt; watchLog('position stopped'); }
        } else {
          if (WATCH.stallStart) { watchLog('position moving again', { stalledMs: Math.round(now - WATCH.stallStart) }); WATCH.stallStart = 0; }
          WATCH.lastPos = el.currentTime; WATCH.lastPosAt = now;
        }
      } else { WATCH.lastPos = -1; WATCH.lastPosAt = now; WATCH.stallStart = 0; }
      // the audio engine's clock against the wall clock
      const c = LOUD.ctx;
      if (c && c.state === 'running') {
        if (WATCH.clk) {
          const wall = (now - WATCH.clk.wall) / 1000, aud = c.currentTime - WATCH.clk.aud;
          if (wall - aud > 0.05) watchLog('audio clock lagged', { lagMs: Math.round((wall - aud) * 1000), overMs: Math.round(wall * 1000) });
        }
        WATCH.clk = { wall: now, aud: c.currentTime };
      } else WATCH.clk = null;
    }, 250);
    DG.start(() => mediaEl, [audio, videoEl]);
    // Time listened, for the charts (js/listen_time.js, vault KSF-052):
    // counted on whichever element carries the song, so its music video counts
    // as the song (user, 2026-09-24). Checked, not assumed: a cached
    // music.html from before has no listen_time.js.
    if (typeof ListenTime === 'object') {
      for (const el of [audio, videoEl]) {
        ListenTime.track(el, () => {
          const s = el === mediaEl ? current() : null;
          return s ? { id: 'ref:' + s.ref, item: { ref: s.ref } } : null;
        }, playbackPosition);
      }
    }
    // interp.js loops what it plays, the way the preview does - it sets .loop
    // at every start AND at every re-plan (the screen's rate changed, the
    // multiplier changed), with no event in between to undo it on. Here the
    // queue decides what comes next, so the end of a song has to be an end:
    // the property is pinned rather than cleared after the fact.
    Object.defineProperty(videoEl, 'loop', { get: () => false, set: () => {}, configurable: true });
    // The video's own shape decides the box it is drawn in, as the cover's does.
    videoEl.addEventListener('loadedmetadata', () => {
      if (videoEl.videoWidth && videoEl.videoHeight) {
        $('muStageFit').style.setProperty('--cover-ar', (videoEl.videoWidth / videoEl.videoHeight).toFixed(4));
      }
    });
    // ONE press of a media key, TWO ways in: the system sends it to
    // navigator.mediaSession, and the very same press is also an ordinary key
    // press in the page that has the focus. Both are needed (another tab
    // playing media takes the media session away, and then only the key
    // arrives), so the same thing asked for twice in a moment is one press:
    // it was skipping two songs at a time, and "back" left the song instead
    // of starting it again (user, 2026-09-23). Long enough to catch the pair,
    // which arrives in the same moment, and short enough to let a second,
    // deliberate press through.
    const MEDIA_AGAIN_MS = 250;
    const mediaLast = new Map();
    const once = (what, fn) => () => {
      const now = performance.now();
      if (now - (mediaLast.get(what) || -Infinity) < MEDIA_AGAIN_MS) return;
      mediaLast.set(what, now);
      fn();
    };
    if ('mediaSession' in navigator) {
      const ms = navigator.mediaSession;
      const on = (action, fn) => { try { ms.setActionHandler(action, fn); } catch (e) { /* unsupported action */ } };
      // play/pause/MediaPlayPause are one thing under two names: answering
      // both would pause and start again on a single press.
      on('play', once('play', () => mediaEl.play().catch(() => {})));
      on('pause', once('play', () => mediaEl.pause()));
      on('nexttrack', once('next', () => next(false)));
      on('previoustrack', once('prev', prev));
      on('seekto', (d) => { if (d.seekTime !== undefined) seekPlayback(mediaEl, d.seekTime); });
      on('seekbackward', (d) => seekPlayback(mediaEl, Math.max(0, playbackPosition(mediaEl) - (d.seekOffset || 10))));
      on('seekforward', (d) => seekPlayback(mediaEl, Math.min(playDur(), playbackPosition(mediaEl) + (d.seekOffset || 10))));
      on('stop', () => mediaEl.pause());
    }
    // Which controls Space belongs to. A control clicked with the mouse keeps
    // the focus, so the browser would press it again on Space: a click on Play
    // and then Space restarted the song instead of pausing it, and drew a
    // focus ring around whatever had been clicked last (user, 2026-09-22).
    // Space is the player's key here. It is left to a control only while the
    // page is being used BY KEYBOARD - tabbed to, where Space must still press
    // what the focus is on.
    let keyboardNav = false;
    document.addEventListener('pointerdown', () => { keyboardNav = false; }, true);
    document.addEventListener('keydown', (e) => { if (e.key === 'Tab') keyboardNav = true; }, true);
    const TAKES_SPACE = ['BUTTON', 'A', 'INPUT', 'SELECT', 'SUMMARY'];
    // The keyboard's media keys. Two ways reach the player and both are
    // needed: navigator.mediaSession above is what the system sends them to,
    // but only while this page is the session it counts as playing - another
    // tab with a video takes that over. These are the same keys arriving as
    // ordinary key presses while this page has the focus, which is why they
    // come before the "is someone typing?" test: they are never typing.
    const MEDIA_KEYS = {
      MediaPlayPause: once('play', () => togglePlay()),
      MediaPlay: once('play', () => { if (mediaEl.paused) togglePlay(); }),
      MediaPause: once('play', () => { if (!mediaEl.paused) togglePlay(); }),
      MediaTrackNext: once('next', () => next(false)),
      MediaTrackPrevious: once('prev', () => prev()),
      MediaStop: once('play', () => mediaEl.pause()),
    };
    document.addEventListener('keydown', (e) => {
      const t = e.target;
      const media = MEDIA_KEYS[e.key];
      if (media) {
        e.preventDefault();
        if (current()) media();
        return;
      }
      // Only a field text goes into: a slider (the seek bar, the volume) or a
      // switch is not typing, and clicking one must not swallow the shortcut.
      const texty = t && t.tagName === 'INPUT'
        && !['range', 'checkbox', 'radio', 'button', 'submit', 'color'].includes(t.type);
      const typing = !!(texty || (t && (t.tagName === 'TEXTAREA' || t.isContentEditable)));
      if (e.key === '/' && !typing) {
        e.preventDefault();
        $('muSearch').focus();
        return;
      }
      // Escape leaves the now-playing screen - but only once the menu, a
      // dialog and fullscreen have had it (the browser takes it first there).
      if (e.key === 'Escape' && stageOpen() && menu.hidden && !$('muDialog').open
          && !document.fullscreenElement) {
        toggleStage(false);
        return;
      }
      if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
      // J / K / L, as YouTube has them: ten seconds back, play or pause, ten
      // seconds forward. Through seekPlayback, so they also work on a Smooth
      // motion stream, whose own clock starts wherever the stream was asked
      // for (the seek asks for a new one).
      const JKL = { j: -10, l: 10 };
      const step = JKL[e.key.toLowerCase()];
      if (step !== undefined && current()) {
        e.preventDefault();
        const d = playDur();
        const to = playbackPosition(mediaEl) + step;
        seekPlayback(mediaEl, Math.max(0, d ? Math.min(d, to) : to));
        return;
      }
      if (e.key.toLowerCase() === 'k' && current()) {
        e.preventDefault();
        togglePlay();
        return;
      }
      if (e.code === 'Space' && !(keyboardNav && t && TAKES_SPACE.includes(t.tagName))) {
        e.preventDefault();
        // Otherwise the browser rings the control that still has the focus,
        // the moment any key is pressed - and this key was not meant for it.
        if (t && t !== document.body && typeof t.blur === 'function') t.blur();
        togglePlay();
      }
    });
    window.addEventListener('pagehide', saveQueue);
  }

  // Everything but the song playing: what both Clear buttons do.
  function clearQueue() {
    const cur = Q.items[Q.pos];
    Q.items = cur === undefined ? [] : [cur];
    Q.pos = Q.items.length ? 0 : -1;
    Q.original = null;
    Q.radio = null;                 // an emptied queue is not a radio any more
    Q.autoStart = -1;
    renderQueue();
    saveQueue();
  }

  // ---- radios and autoplay ----------------------------------------------------------------
  // A radio has an ORIGIN - one song's place on the genre map - and works
  // outwards from it: the neighbourhood the origin is in, played out, then
  // the nearest neighbourhood to the ORIGIN that it has not played, and so
  // on. 3D, which keeps more of the genre model's neighbourhoods than the 2D
  // the map draws.
  //
  // It is a chain of nearest songs (genres.js's "Keep playing nearby", which
  // this page walked until 2026-09-23) with a LEASH: the next song is the one
  // with the smallest
  //     distance from the song playing  +  HOME_PULL x distance from the origin.
  // Inside a neighbourhood every candidate is about equally far from home, so
  // the first term decides and the songs come as a run - what a subgenre
  // needs. Once it is played out the second term decides, and it is the
  // unplayed neighbourhood nearest to HOME that follows, not whichever one
  // happens to lie beyond the song that just ended. That last step is the
  // whole difference: an unleashed chain leaves every neighbourhood by its far
  // side and never comes back, so an evening of listening ends up on the other
  // side of the map (the user's "a complete 180 degree switch in music genre
  // over time"); leashed, the radio keeps circling home.
  //
  // Shells of a fixed number of songs (the first draft: the 12 nearest to the
  // origin, then the next 12) hold it just as close, but they cut across the
  // clusters instead of following them - at any distance from home the songs
  // that far out lie in every direction, so a shell of 12 was 12 different
  // subgenres. Measured in the harness on a lattice of 27 styles x 20 songs,
  // 121 songs played, styles entered / mean step / furthest from home:
  //   the old free chain   39 / 4.53 / 11.4, and never turning back
  //   shells of 12         41 / 4.76 / 11.7
  //   leash, HOME_PULL 1    6 / 1.76 / 15.8, back to 9.0 by song 100
  // and the leash itself: 0.4 wanders off to a corner and stays there (21.0),
  // 2 keeps it tighter still (12.4) but starts breaking a style in two.
  const RADIO_AHEAD = 20;        // songs a radio adds at a time
  const RADIO_LOW = 5;           // ... when fewer than this are still to come
  const AUTOPLAY_PREVIEW = 5;    // what the queue shows autoplay would play next
  // How far the leash pulls. 0 is the old chain, free to walk away; the
  // higher it is the sooner the radio turns back towards the origin, and at
  // some point it stops finishing a neighbourhood before it does. 1 weighs a
  // step out of the way exactly as much as the same distance from home.
  const HOME_PULL = 1;

  function dist(a, b) {
    return Math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2);
  }

  // A radio may never play the same song twice (user, 2026-09-23) - and the
  // library holds some songs twice: one upload that was downloaded AND sits in
  // the Jellyfin library, two files of one recording under different names.
  // Two rows in the catalogue, two points a hair apart on the map, one song to
  // an ear. So what a radio has heard is kept by IDENTITY: the video id, or,
  // for a file that carries none, its artist and title (already folded and
  // stripped of punctuation by norm()). Everywhere else they stay two songs.
  function songKey(i) {
    const s = LIB.songs[i];
    if (!s) return '';
    return s.vid ? `v:${s.vid}` : `n:${s.nArtist}|${s.nTitle}`;
  }

  function keysOf(list) {
    const out = new Set();
    for (const i of list) out.add(songKey(i));
    return out;
  }

  // `n` songs around `origin`, starting from `from` (where the queue stands,
  // the origin itself when there is nothing yet). `skip` - the played and the
  // queued - is not changed.
  function walk(origin, n, skip, from = -1) {
    const home = LIB.songs[origin] && LIB.songs[origin].p3;
    if (!home) return [];
    const seen = new Set(skip);
    const heard = keysOf(seen);
    const out = [];
    let at = from >= 0 && LIB.songs[from] && LIB.songs[from].p3 ? from : origin;
    while (out.length < n) {
      const p = LIB.songs[at].p3;
      let best = -1;
      let bestCost = Infinity;
      for (const j of LIB.positioned) {
        if (seen.has(j) || j === at || heard.has(songKey(j))) continue;
        const q = LIB.songs[j].p3;
        const cost = dist(q, p) + HOME_PULL * dist(q, home);
        if (cost < bestCost) { bestCost = cost; best = j; }
      }
      if (best < 0) break;
      out.push(best);
      seen.add(best);
      heard.add(songKey(best));
      at = best;
    }
    return out;
  }

  // The first / the last song of `list` that is on the map: a radio's origin
  // when it is taken up again, and where its next shell is entered from.
  function firstOnMap(list) {
    for (let n = 0; n < list.length; n++) if (LIB.songs[list[n]] && LIB.songs[list[n]].p3) return list[n];
    return -1;
  }

  function lastOnMap(list) {
    for (let n = list.length - 1; n >= 0; n--) if (LIB.songs[list[n]] && LIB.songs[list[n]].p3) return list[n];
    return -1;
  }

  // The origin of the radio that is playing, by ref: a rebuilt library
  // numbers its songs anew, and this one has to survive that and a reload.
  // Gone from the library (or a queue saved before radios had an origin):
  // the last song played holds the radio where it is rather than ending it.
  // What a radio has played in the sessions before this one lives on the
  // server. Until it has arrived the radio must not walk: it would hand out
  // songs it has already played and only find out afterwards. `pending` holds
  // the top-up back for those few hundred milliseconds - the queue always has
  // songs left over that long - and the answer prunes what is already queued.
  function loadRadioPlayed(r) {
    r.pending = true;
    const done = () => {
      if (Q.radio !== r) return;
      r.pending = false;
      if (!pruneRadioQueue()) topUpRadio();
      renderQueue();
      saveQueue();
    };
    api(`/api/music/radio?id=${encodeURIComponent(r.id)}`).then((res) => {
      if (Q.radio !== r) return;
      if (res.ok && Array.isArray(res.data.played)) {
        for (const ref of res.data.played) {
          const i = LIB.byRef.get(ref);
          if (i !== undefined) r.played.add(i);
        }
      }
      done();
    }, done);
  }

  // Everything still to come that this radio turns out to have played - or
  // that is the same song under another name - leaves the queue before it is
  // reached. What has already played stays: the queue may be looked back over.
  function pruneRadioQueue() {
    const r = Q.radio;
    if (!r || Q.pos < 0) return false;
    const keep = Q.items.slice(0, Q.pos + 1);
    const heard = keysOf(keep);
    let auto = Q.autoStart;
    let dropped = 0;
    Q.items.slice(Q.pos + 1).forEach((i, n) => {
      const key = songKey(i);
      if (r.played.has(i) || heard.has(key)) {
        dropped += 1;
        if (Q.autoStart > Q.pos + 1 + n) auto -= 1;
        return;
      }
      heard.add(key);
      keep.push(i);
    });
    if (!dropped) return false;
    Q.items = keep;
    if (Q.autoStart >= 0) Q.autoStart = Math.max(Q.pos + 1, auto);
    topUpRadio();
    return true;
  }

  function radioOrigin() {
    if (!Q.radio) return -1;
    const i = LIB.byRef.get(Q.radio.originRef);
    if (i !== undefined && LIB.songs[i] && LIB.songs[i].p3) return i;
    return lastOnMap(Q.items);
  }

  // What a radio is made from: {id, kind, key, title, seeds}. The id is
  // "<kind>:<key>", so the same radio started again goes on past what it
  // already played.
  function radioFor(kind, key) {
    let seeds = [];
    let title = '';
    if (kind === 'song') {
      const i = LIB.byRef.get(key);
      if (i === undefined) return null;
      seeds = [i];
      title = `${LIB.songs[i].title} radio`;
    } else if (kind === 'artist') {
      seeds = artistSongs(key);
      const a = LIB.artists.get(String(key).toLowerCase());
      title = `${a ? a.name : key} radio`;
    } else if (kind === 'genre') {
      seeds = LIB.genres.get(key) || [];
      title = `${key} radio`;
    } else if (kind === 'playlist') {
      const pl = LIB.playlistById.get(key);
      if (!pl) return null;
      seeds = pl.songs;
      title = `${pl.title} radio`;
    } else if (kind === 'album') {
      const a = LIB.albumById.get(key);
      if (!a) return null;
      seeds = a.songs;
      title = `${a.title} radio`;
    } else {
      return null;
    }
    seeds = seeds.filter((i) => LIB.songs[i] && LIB.songs[i].p3);
    return seeds.length ? { id: `${kind}:${key}`, kind, key, title, seeds } : null;
  }

  // Starts `desc`. THE ORIGIN, which its shells grow around for as long as
  // the radio lives: its own song for a song radio, else the song it first
  // played - so the same radio picked up tomorrow carries on outwards from
  // where it began, not from wherever it happened to stop - and for one that
  // has never played, one of its songs at random. (The server keeps a
  // radio's newest 500 songs; past that the origin moves with the window,
  // one shell every few hundred songs.) Songs it played before do not come
  // again.
  async function startRadio(desc, opts = {}) {
    if (!desc) {
      toast('Not on the genre map yet - radios need songs the genre model has heard');
      return;
    }
    // "Go on where it stopped", on the radio that is playing: it never
    // stopped. Building its queue again here would throw away what is queued
    // and jump out of the song that is playing - the last thing a click on the
    // radio you are listening to should do (user, 2026-09-23). Take them to it
    // instead. A radio asked for by name (no resume) does start again.
    if (opts.resume && Q.radio && Q.radio.id === desc.id && Q.items.length) {
      toggleStage(true);
      return;
    }
    const res = await api(`/api/music/radio?id=${encodeURIComponent(desc.id)}`);
    const history = res.ok && Array.isArray(res.data.played) ? res.data.played : [];
    const known = history.map((r) => LIB.byRef.get(r)).filter((i) => i !== undefined);
    const played = new Set(known);
    let origin = desc.kind === 'song' ? desc.seeds[0] : firstOnMap(known);
    if (origin < 0) {
      const fresh = desc.seeds.filter((i) => !played.has(i));
      const pool = fresh.length ? fresh : desc.seeds;
      origin = pool[Math.floor(Math.random() * pool.length)];
    }
    // It opens with its origin. A SONG radio always opens with its song: that
    // is the picture on its card, the name it carries and what the Radio
    // button on a song means, and someone choosing it again is not the radio
    // repeating itself - the walk that follows still skips everything played
    // (user, 2026-09-23: "the cover song of the radio is not played; some
    // other song is played instead"). Any other radio opens with a seed it has
    // not played; a resumed one goes straight on from where it stopped.
    const opensWith = desc.kind === 'song'
      || (!played.has(origin) && !keysOf(played).has(songKey(origin)));
    const head = !opts.resume && opensWith ? [origin] : [];
    const skip = new Set([...played, ...head]);
    const items = [...head, ...walk(origin, RADIO_AHEAD - head.length, skip, head.length ? origin : lastOnMap(known))];
    if (!items.length) {
      toast(`${desc.title} has played everything the map has around it`);
      return;
    }
    Q.radio = { id: desc.id, kind: desc.kind, key: desc.key, title: desc.title,
      originRef: LIB.songs[origin].ref, played };
    Q.autoStart = -1;
    Q.context = `radio:${desc.id}`;
    Q.shuffle = false;
    Q.original = null;
    Q.items = items;
    Q.pos = 0;
    loadCurrent(true);
    toast(`${desc.title}: songs that sound alike, one leading to the next`);
  }

  // A radio keeps its queue filled: when little is left, the next shell out
  // from its origin, past everything played or already queued, entered at the
  // song closest to the last one in the queue.
  function topUpRadio() {
    if (!Q.radio || Q.radio.pending || Q.items.length - Q.pos - 1 >= RADIO_LOW) return false;
    const origin = radioOrigin();
    if (origin < 0) return false;
    const more = walk(origin, RADIO_AHEAD, new Set([...Q.radio.played, ...Q.items]), lastOnMap(Q.items));
    Q.items.push(...more);
    return more.length > 0;
  }

  // The queue ran out with autoplay on: it goes on as a radio from its last
  // song (kind "auto", keyed by where it came from, so the same list's
  // autoplay does not repeat itself either). False when nothing can follow.
  function startAutoplay() {
    if (Q.radio) return topUpRadio();
    const from = lastOnMap(Q.items);
    if (from < 0) return false;
    const more = walk(from, RADIO_AHEAD, new Set(Q.items), from);
    if (!more.length) return false;
    const key = Q.context || LIB.songs[from].ref;
    // The song the list ended on is the origin: what follows a list stays
    // around where it left off instead of walking away from it.
    Q.radio = { id: `auto:${key}`, kind: 'auto', key, title: `Autoplay after ${contextTitle(Q.context, from)}`,
      originRef: LIB.songs[from].ref, played: new Set(Q.items) };
    Q.autoStart = Q.items.length;
    Q.items.push(...more);
    // The same list going on again tomorrow is the same radio: what it
    // suggested then must not come round again now.
    loadRadioPlayed(Q.radio);
    return true;
  }

  function contextTitle(ctx, from) {
    if (ctx) {
      const pl = LIB.playlistById.get(ctx);
      if (pl) return pl.title;
      const al = LIB.albumById.get(ctx);
      if (al) return al.title;
      if (ctx.startsWith('artist:')) {
        const a = LIB.artists.get(ctx.slice(7));
        return a ? a.name : ctx.slice(7);
      }
      if (ctx.startsWith('genre:')) return ctx.slice(6);
      if (ctx === 'library') return 'your library';
      if (ctx === 'search') return 'your search';
    }
    return LIB.songs[from] ? LIB.songs[from].title : 'this song';
  }

  // What autoplay would play once the queue is done: shown, dimmed, under it.
  function autoplayPreview() {
    if (!ME.autoplay || Q.radio || Q.repeat === 'all' || !Q.items.length) return [];
    const from = lastOnMap(Q.items);
    return from < 0 ? [] : walk(from, AUTOPLAY_PREVIEW, new Set(Q.items), from);
  }

  function recordPlay(s) {
    const src = Q.radio ? 'radio' : playSrc;
    playSrc = 'list';
    const now = Date.now() / 1000;
    const ctx = ctxCode();
    ME.events.push([s.ref, src, now, ctx]);
    if (ME.events.length > 500) ME.events.splice(0, ME.events.length - 500);
    // The history view is open behind the player often enough: put the play
    // at its front rather than let it go stale until the next load.
    if (HIST.loaded) HIST.rows.unshift([s.ref, src, now, ctx]);
    const body = { ref: s.ref, src, ctx };
    if (Q.radio) {
      Q.radio.played.add(s.i);
      const r = Q.radio;
      body.radio = { id: r.id, kind: r.kind, key: r.key, title: r.title };
      const was = ME.radios.find((x) => x.id === r.id);
      ME.radios = [{ ...(was || { started: now }), id: r.id, kind: r.kind, key: r.key, title: r.title, t: now,
        last: s.ref, count: (was ? was.count : 0) + 1 }, ...ME.radios.filter((x) => x.id !== r.id)].slice(0, 30);
    }
    api('/api/music/played', { method: 'POST', body });
  }

  function setAutoplay(on) {
    ME.autoplay = !!on;
    // Off ends autoplay's radio and takes back the songs it queued that have
    // not played yet; a radio started by hand is a radio, not autoplay.
    if (!ME.autoplay && Q.radio && Q.radio.kind === 'auto') {
      Q.items.splice(Math.max(Q.pos + 1, Q.autoStart));
      Q.radio = null;
      Q.autoStart = -1;
    }
    updateAutoplayButton();
    renderQueue();
    saveQueue();
    api('/api/music/settings', { method: 'PUT', body: { autoplay: ME.autoplay } });
    toast(ME.autoplay ? 'Autoplay on: similar songs follow when the queue runs out' : 'Autoplay off');
  }

  function updateAutoplayButton() {
    const b = $('muAutoplay');
    b.setAttribute('aria-pressed', String(ME.autoplay));
    b.setAttribute('aria-label', `Autoplay: ${ME.autoplay ? 'on' : 'off'}`);
    b.title = ME.autoplay ? 'Autoplay is on: similar songs follow when the queue runs out' : 'Autoplay is off';
    $('muAutoplaySwitch').checked = ME.autoplay;
    $('muStageAutoplay').checked = ME.autoplay;
  }

  // ---- the queue drawer -----------------------------------------------------------------
  function toggleQueue(force) {
    const panel = $('muQueue');
    const open = force === undefined ? panel.hidden : force;
    panel.hidden = !open;
    $('muQueueBtn').setAttribute('aria-pressed', String(open));
    if (open) renderQueue();
  }

  // The drawer on the right and the now-playing screen's "Up next" column
  // show the same queue. The rows are built again for each of them: one node
  // cannot be in two lists.
  function queueLists() {
    return [[$('muQueue'), $('muQueueList')], [$('muStage'), $('muStageQueueList')]]
      .filter(([panel]) => !panel.hidden).map(([, list]) => list);
  }

  function renderQueue() {
    const lists = queueLists();
    if (!lists.length) return;
    for (const list of lists) list.replaceChildren(...queueRows());
    const sub = Q.radio && Q.radio.kind !== 'auto' ? Q.radio.title : '';
    $('muQueueSub').textContent = sub;
    $('muStageQueueSub').textContent = sub;
  }

  function queueRows() {
    const rows = [];
    const auto = Q.radio && Q.radio.kind === 'auto';
    Q.items.forEach((i, n) => {
      const s = LIB.songs[i];
      if (!s) return;
      if (n === Q.pos) rows.push(el('li', { class: 'mu-q-label', text: 'Now playing' }));
      if (n === Q.pos + 1 || (auto && n === Q.autoStart && n > Q.pos)) {
        const from = auto && n >= Q.autoStart ? 'Autoplay: similar songs'
          : Q.radio && !auto ? `Up next on ${Q.radio.title}` : 'Up next';
        rows.push(el('li', { class: 'mu-q-label', text: from }));
      }
      if (n > Q.pos + 400) return;                    // a long queue: the next 400 are plenty to show
      rows.push(el('li', { class: n === Q.pos ? 'current' : null,
        onclick: () => { Q.pos = n; playSrc = 'user'; loadCurrent(true); } },
        el('div', { class: 'mu-track-cover' }, coverImg(s, 96, true)),
        el('div', { class: 'mu-track-main' },
          el('div', { class: 'mu-track-title', text: s.title }),
          el('div', { class: 'mu-track-artist', text: s.artist })),
        n === Q.pos ? el('span') : el('button', { type: 'button', class: 'mu-icon-btn', 'aria-label': `Remove ${s.title} from the queue`,
          onclick: (e) => {
            e.stopPropagation();
            Q.items.splice(n, 1);
            if (n < Q.pos) Q.pos -= 1;
            if (n < Q.autoStart) Q.autoStart -= 1;
            renderQueue();
            saveQueue();
          } }, icon('close'))));
    });
    // Autoplay's next songs, before they are queued: a click starts autoplay
    // there, as the end of the queue would.
    const preview = autoplayPreview();
    if (preview.length) {
      rows.push(el('li', { class: 'mu-q-label', text: 'Autoplay: similar songs' }));
      for (const i of preview) {
        const s = LIB.songs[i];
        rows.push(el('li', { class: 'mu-q-auto', title: 'Plays after the queue - autoplay is on',
          onclick: () => {
            if (!startAutoplay()) return;
            const at = Q.items.indexOf(i, Q.autoStart);
            Q.pos = at >= 0 ? at : Q.autoStart;
            playSrc = 'user';
            loadCurrent(true);
          } },
        el('div', { class: 'mu-track-cover' }, coverImg(s, 96, true)),
        el('div', { class: 'mu-track-main' },
          el('div', { class: 'mu-track-title', text: s.title }),
          el('div', { class: 'mu-track-artist', text: s.artist })),
        el('span')));
      }
    }
    if (!rows.length) rows.push(el('li', { class: 'mu-q-label', text: 'Nothing queued' }));
    return rows;
  }

  function markPlaying() {
    const s = current();
    for (const row of document.querySelectorAll('.mu-track.playing')) row.classList.remove('playing');
    if (!s) return;
    for (const row of document.querySelectorAll(`.mu-track[data-song="${s.i}"]`)) row.classList.add('playing');
  }

  // ---- the now-playing screen ---------------------------------------------
  // The bottom bar, expanded over the page: the cover as large as it fits,
  // what is playing under it, and what comes next beside it - and a button
  // that puts the page into fullscreen. The transport stays in the bar, which
  // is still there below the screen: one set of controls, wherever you are.
  const stage = $('muStage');

  function stageOpen() {
    return !stage.hidden;
  }

  function toggleStage(force) {
    const open = force === undefined ? !stageOpen() : !!force;
    if (open === stageOpen()) return;
    if (open && !current()) return;            // nothing to show yet
    stage.hidden = !open;
    document.body.classList.toggle('mu-stage-open', open);
    const b = $('muExpand');
    b.setAttribute('aria-expanded', String(open));
    b.setAttribute('aria-label', open ? 'Close the player' : 'Open the player');
    setIcon(b, open ? 'collapse' : 'expand');
    if (open) {
      toggleQueue(false);                      // the queue is part of the screen
      showStage(current());
      renderQueue();
      markPlaying();
    } else {
      leaveStageCover();
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    }
  }

  // What is playing, in the screen. The cover is asked for at 600 px and then
  // upscaled on the GPU to fill this screen: js/upscale.js does the asking,
  // the polling and the swap - media-dl's own file, the one its library and
  // share pages use - and bindStage() gives it the few things it reads from
  // the page it is on. `currentPreviewKey` is its "what is on screen now": an
  // answer for a song already left behind is dropped by it.
  function showStage(s) {
    if (!s) return;
    leaveStageCover();
    refreshInfo();                                // they describe another song now
    // No title under the cover: the bar below already says what is playing,
    // and YouTube Music leaves that space to the artwork.
    const img = $('muStageCover');
    const ph = $('muStagePh');
    if (!s.cover) {
      img.removeAttribute('src');
      img.hidden = true;
      ph.hidden = videoShowing();
      ph.textContent = (String(s.title || '?').trim()[0] || '?').toUpperCase();
      setAmbient(null);
      return;
    }
    // A rendition while the music video has the screen: the cover is not
    // being looked at then, only averaged into the backdrop, and the whole
    // picture is a download nobody sees. Coming back to it re-runs this.
    img.src = coverUrl(s, videoShowing() ? 300 : stageCoverSize());
    // Not while its music video is on screen: this runs for every song, and
    // it used to put the cover back over the video that had just started.
    img.hidden = videoShowing();
    ph.hidden = true;
    window.currentPreviewKey = s.ref;
    if (videoShowing()) return;                   // the cover is not on screen
    if (typeof startLiveUpscale === 'function') {
      startLiveUpscale(img, { type: 'audio', root: s.root, path: s.path });
    }
  }

  // Leaving a cover: no chip, nothing described, and no swap when an answer
  // for it arrives late (upscale.js drops what is not on screen any more).
  function leaveStageCover() {
    window.currentPreviewKey = null;
    shownFor = null;
    delete $('muStageCover').dataset.upscaled;
    setEnhancement('upscale', null);
    if (typeof resetLiveUpscale === 'function') resetLiveUpscale();
  }

  // ---- the cover's shape and its colour ------------------------------------
  // Both are read from the picture once it is on screen. Its shape sizes the
  // box it is drawn in, so nothing is letterboxed; its average colour becomes
  // the background of the whole screen, which is what media-dl's preview does
  // and reads far better than the same grey behind every song.
  // Hue and saturation only: music.css makes the colour, 12% light on dark
  // and a pale wash on light, so a theme switch needs nothing from here.
  function setAmbient(hue, sat) {
    const root = document.documentElement;
    if (hue != null) {
      root.style.setProperty('--mu-ambient-h', String(hue));
      root.style.setProperty('--mu-ambient-s', `${sat}%`);
    } else {
      root.style.removeProperty('--mu-ambient-h');
      root.style.removeProperty('--mu-ambient-s');
    }
  }

  function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const l = (max + min) / 2;
    if (max === min) return [0, 0, l];
    const d = max - min;
    const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    let h;
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else h = ((r - g) / d + 4) / 6;
    return [h, s, l];
  }

  // A plain average trends towards grey (opposing hues cancel out), so the
  // saturation is lifted: clearly the artwork's colour. The lightness is
  // music.css's (--mu-ambient). media-dl's player.js does the arithmetic the
  // same way.
  function ambientFrom(img) {
    try {
      const SIZE = 16;
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = SIZE;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0, SIZE, SIZE);
      const { data } = ctx.getImageData(0, 0, SIZE, SIZE);
      let r = 0, g = 0, b = 0, n = 0;
      for (let i = 0; i < data.length; i += 4) {
        if (data[i + 3] < 128) continue;
        r += data[i]; g += data[i + 1]; b += data[i + 2]; n++;
      }
      if (!n) return;
      const [h, s] = rgbToHsl(r / n, g / n, b / n);
      setAmbient(Math.round(h * 360), Math.round(Math.min(1, s * 1.8) * 100));
    } catch (e) {
      // getImageData throws on a canvas tainted by a cross-origin picture.
      // Covers are same-origin; if it ever happens the page keeps its own.
    }
  }

  // The ratio, never the size: the upscaled copy has several times as many
  // pixels and the same shape, so the box must not move when it is swapped in.
  let shownFor = null;               // the song the shape and the colour came from

  function coverShown(img) {
    // Once per song, from the FIRST copy that arrives - and from whichever
    // copy that is. A cached upscale is swapped in the moment the song opens,
    // and setting src cancels the load already running, so for such a song the
    // upscaled copy is the only load there is: reading nothing from it left
    // the box on the shape of the song before, grey bars beside a square cover
    // (user, 2026-09-23: "The cover should not cause bars like this"). And not
    // a second time, for either of them: the upscale is the same picture with
    // more pixels, so the box must not move under it, and its colour cannot
    // have changed - while reading one back out of a canvas means decoding
    // tens of megapixels on the main thread, in the moment of the swap.
    if (shownFor === window.currentPreviewKey) return;
    if (!img.naturalWidth || !img.naturalHeight) return;
    shownFor = window.currentPreviewKey;
    $('muStageFit').style.setProperty('--cover-ar', (img.naturalWidth / img.naturalHeight).toFixed(4));
    ambientFrom(img);
  }

  // ---- details -------------------------------------------------------------
  // media-dl's preview puts what is being DONE to what you see - the upscale,
  // Smooth motion - at the top of its info panel, not on the screen itself.
  // The same here: the ⓘ button opens it, and nothing of it is permanent.
  let stageEnhancements = [];
  let infoOpen = false;
  let infoFor = null;
  let infoData = null;

  function setEnhancement(kind, entry) {
    stageEnhancements = stageEnhancements.filter((e) => e.kind !== kind);
    if (entry) stageEnhancements.push({ kind, ...entry });
    if (infoOpen) renderInfo();
  }

  function infoRows(info) {
    if (!info) return [];
    const dims = info.width && info.height ? `${info.width} × ${info.height}` : null;
    const kHz = info.sample_rate ? `${(info.sample_rate / 1000).toFixed(1)} kHz` : null;
    return [
      ['File', info.name],
      ['Folder', (info.root_name ? info.root_name + ' / ' : '') + (info.folder || '')],
      ['Size', info.size ? `${(info.size / 1e6).toFixed(1)} MB` : null],
      ['Duration', info.duration ? fmtTime(info.duration) : null],
      ['Audio codec', info.audio_codec],
      ['Channels', info.channels],
      ['Sample rate', kHz],
      ['Bitrate', info.bit_rate ? `${Math.round(info.bit_rate / 1000)} kbit/s` : null],
      ['Video codec', info.video_codec],
      ['Frame rate', info.fps ? `${info.fps} fps` : null],
      ['Dimensions', dims],
      ['Container', info.container],
      ['Style (AI)', Array.isArray(info.ai_genres)
        ? info.ai_genres.map((g) => String(g.tag || g).split('---').pop()).join(', ') : null],
      ['Video ID', info.source_id, sourceUrl(info)],
      ['Downloaded', info.added ? window.formatDateTime(info.added) : null],
    ].filter(([, v]) => v !== null && v !== undefined && v !== '');
  }

  // Where the upload it came from lives. media-dl's own panel links the id
  // the same way (player.js): it is kept so the upload can be found again,
  // and nobody should retype 11 case-sensitive characters by hand. The
  // registry's own url when there is one, else built from the id - but only
  // when it IS a YouTube id, not some other source's.
  const YT_ID_RE = /^[A-Za-z0-9_-]{11}$/;
  function sourceUrl(info) {
    if (info.source_url) return info.source_url;
    return YT_ID_RE.test(info.source_id || '') ? `https://www.youtube.com/watch?v=${info.source_id}` : null;
  }

  function rowList(rows) {
    const dl = el('dl', { class: 'mu-info-list' });
    for (const [label, value, href] of rows) {
      dl.append(el('dt', { text: label }), el('dd', {}, href
        ? el('a', { href, target: '_blank', rel: 'noopener noreferrer', text: String(value) })
        : String(value)));
    }
    return dl;
  }

  // What the details describe: whatever is on screen, as media-dl's panel
  // does - the music video while it is playing, the song's own file
  // otherwise.
  function infoTarget() {
    const s = current();
    if (!s) return null;
    if (videoShowing() && s.video) return { root: s.video.root, path: s.video.path, kind: 'video', song: s };
    return { root: s.root, path: s.path, kind: 'song', song: s };
  }

  function renderInfo() {
    const panel = $('muInfoPanel');
    const t = infoTarget();
    const s = t && t.song;
    const parts = [];
    for (const e of stageEnhancements) {
      if (e.note) { parts.push(el('p', { class: 'mu-info-note', text: e.note })); continue; }
      const section = el('div', { class: 'mu-info-block' }, el('div', { class: 'mu-info-h', text: e.title }),
        rowList((e.rows || []).filter(([, v]) => v !== null && v !== undefined && v !== '')));
      for (const a of e.actions || []) {
        if (!a || !a.label) continue;
        section.append(el('button', { type: 'button', class: 'mu-text-btn', text: a.label,
          onclick: () => a.run && a.run() }));
      }
      parts.push(section);
    }
    const rows = infoRows(infoData);
    if (rows.length) parts.push(el('div', { class: 'mu-info-block' },
      el('div', { class: 'mu-info-h',
        text: t && t.kind === 'video' ? 'Music video' : (s ? s.title : 'This song') }), rowList(rows)));
    else if (!parts.length) parts.push(el('p', { class: 'mu-info-note', text: 'Reading the file…' }));
    const loud = loudnessRows(t);
    if (loud.length) parts.push(el('div', { class: 'mu-info-block' },
      el('div', { class: 'mu-info-h', text: 'Volume' }), rowList(loud)));
    panel.replaceChildren(...parts);
  }

  // What the loudness matching does to the file on screen.
  function loudnessRows(t) {
    if (!t || typeof LIB.loud.target !== 'number') return [];
    const raw = t.kind === 'video' ? t.song.videoGain : t.song.gain;
    const db = gainDbOf(raw, t.kind === 'video' ? t.song.videoLevel : t.song.level);
    const target = typeof ME.targetLufs === 'number' ? ME.targetLufs : LIB.loud.target;
    const routed = LOUD.nodes.has(t.kind === 'video' ? videoEl : audio);
    const fmt = (d) => `${d > 0 ? '+' : d < 0 ? '−' : '±'}${Math.abs(d).toFixed(1)} dB`;
    return [
      ['Every file plays at', (ME.targetLufs === TARGET_MAX ? 'Max (each file at its own ceiling)' : lufsText(target))
        + (typeof ME.targetLufs === 'number' ? ' (your setting)' : ' (recommended)')],
      ['This file', raw === null ? `${fmt(db)} (not measured yet)` : fmt(db)],
      ['Applied', routed || db <= 0 ? fmt(db)
        : LOUD.off ? `${fmt(0)} - this browser can only turn files down`
          : `${fmt(0)} - turned up after the first click on the page`],
    ];
  }

  async function loadInfoData() {
    const t = infoTarget();
    if (!t) return;
    const key = `${t.song.ref}:${t.kind}`;
    if (infoFor === key) return;
    infoFor = key;
    infoData = null;
    const res = await api(`/api/media/info?root=${encodeURIComponent(t.root)}&path=${encodeURIComponent(t.path)}`);
    if (infoFor !== key) return;                   // it moved on while this loaded
    infoData = res.ok ? res.data : null;
    if (infoOpen) renderInfo();
  }

  // The song changed, or the video went on or off: the details describe
  // something else now.
  function refreshInfo() {
    infoFor = null;
    infoData = null;
    if (infoOpen) { renderInfo(); loadInfoData(); }
  }

  function toggleInfo(force) {
    const open = force === undefined ? !infoOpen : !!force;
    infoOpen = open;
    $('muInfoPanel').hidden = !open;
    $('muStageInfo').setAttribute('aria-expanded', String(open));
    if (open) { renderInfo(); loadInfoData(); }
  }

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
    // The page, not this panel: only what is inside the fullscreen element is
    // drawn, and the player's bar, the menus and the dialogs are outside it -
    // the same reason media-dl's preview fullscreens the page.
    const no = () => toast('This browser would not go fullscreen');
    try {
      // Not everywhere: a page inside another app's window is often not
      // allowed to (throwing, or answering with nothing at all).
      const req = document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
      if (req && req.catch) req.catch(no);
      else if (!req) setTimeout(() => { if (!document.fullscreenElement) no(); }, 300);
    } catch (e) {
      no();
    }
  }

  function bindStage() {
    // js/upscale.js reads these from whatever page it runs on.
    window.currentPreviewKey = null;
    // Nothing to pin: this cover's box is sized by CSS, so a file with more
    // pixels in it cannot change the layout. media-dl has to pin it because
    // there a picture is drawn at its own pixel size.
    window.pinUpscaledSize = function () { };
    window.formatDateTime = (t) => (t ? new Date(t * 1000).toLocaleString('en-GB') : '-');
    // What it says about the upscale - why there is none, that one is running,
    // or what the one on screen is - goes where media-dl puts it: the details
    // panel, behind the ⓘ button. js/interp.js reports Smooth motion through
    // the same call.
    window.setPreviewEnhancement = setEnhancement;
    if (typeof upscaleHost === 'object' && upscaleHost) upscaleHost.chip = () => $('muStageChips');
    // js/interp.js expects these from js/player.js, which this page does not
    // load: they are the whole seam between it and a player.
    window.startPlayback = (el) => { el.play().catch(() => updatePlayButton()); };
    window.playbackPosition = playbackPosition;
    window.playbackDuration = playbackDuration;
    window.formatPlaybackTime = fmtTime;
    window.currentPreviewEl = null;
    window.infoRequestUrl = (f) => `/api/media/info?root=${encodeURIComponent(f.root || '')}&path=${encodeURIComponent(f.path)}`;

    $('muModeSong').addEventListener('click', () => setVideoMode(false));
    $('muModeVideo').addEventListener('click', () => setVideoMode(true));
    // Smooth motion, the screen's refresh rate and the bandwidth cap: the
    // menu is js/interp.js's own, the same one the library's player has.
    $('muStageSettings').addEventListener('click', (e) => {
      const open = $('muSettingsMenu').hidden;
      $('muSettingsMenu').hidden = !open;
      e.currentTarget.setAttribute('aria-expanded', String(open));
      if (open && typeof renderPlayerSettings === 'function') renderPlayerSettings($('muSettingsMenu'), videoEl);
    });
    document.addEventListener('pointerdown', (e) => {
      if ($('muSettingsMenu').hidden) return;
      if (e.target.closest('#muSettingsMenu, #muStageSettings')) return;
      $('muSettingsMenu').hidden = true;
      $('muStageSettings').setAttribute('aria-expanded', 'false');
    });

    $('muExpand').addEventListener('click', () => toggleStage());
    $('muStageClose').addEventListener('click', () => toggleStage(false));
    $('muFullscreen').addEventListener('click', toggleFullscreen);
    $('muStageInfo').addEventListener('click', () => toggleInfo());
    // Every cover that arrives, the first one and the upscaled copy that
    // replaces it: both are read for their shape and their colour.
    $('muStageCover').addEventListener('load', (e) => coverShown(e.target));
    $('muStageClear').addEventListener('click', clearQueue);
    $('muStageAutoplay').addEventListener('change', (e) => setAutoplay(e.target.checked));
    // The artwork box, not the cover in it: the video plays in the same box
    // and a click or a double click on it must work there as well (user,
    // 2026-09-23).
    clickOrDouble($('muStageFit'), togglePlay, toggleFullscreen);
    // The WHOLE bar opens the player, the way a phone's does - not only the
    // song in the middle of it. The transport sits in a column of its own and
    // the volume in another, both packed to their outer edge, so on a wide
    // screen most of what looks like the bar is the empty half of those
    // columns, and a click there did nothing at all (user, 2026-09-23: "I had
    // to click on the bottom bar 3 times for it to expand"). Anything that is
    // itself a control keeps its own click - including a drag of the seek or
    // the volume that happens to end over the bar.
    // Asked of the event's PATH, fixed when it was dispatched - never of the
    // target's ancestors as they are by the time the click gets here. The
    // repeat button swaps its icon inside its own click handler, so the <path>
    // under the pointer was already out of the document, closest('button')
    // found nothing, and a click on the icon opened the player as well (user,
    // 2026-09-24: "clicking on the play once, loop playlist, loop song button
    // toggles the expanded player sometimes" - only when the icon was hit,
    // not the button's edge). The press is judged the moment it happens.
    let barFromControl = false;
    const barControl = (e) => e.composedPath().some((n) => n instanceof Element && n.matches('button, a, input'));
    $('muPlayer').addEventListener('pointerdown', (e) => { barFromControl = barControl(e); });
    $('muPlayer').addEventListener('click', (e) => {
      const fromControl = barFromControl;
      barFromControl = false;
      if (fromControl || barControl(e)) return;
      toggleStage();
    });
    // A link in the screen leads to a page behind it: show that page.
    stage.addEventListener('click', (e) => {
      if (e.target.closest('a[href]')) toggleStage(false);
    });
    // In fullscreen the artwork is the point, so everything else is there
    // only while the page is being used - the same rule as the preview's
    // header in media-dl (player.js, showPreviewChrome). An open details
    // panel counts as being used; so does the pointer resting on a control,
    // or the screen would fade out from under a hand on the volume slider.
    // The same 600 ms media-dl's preview waits (player.js, CHROME_HIDE_MS):
    // long enough that moving the mouse across the screen does not flicker,
    // short enough that the artwork is alone again as soon as you stop.
    // A touch screen has no resting pointer to keep it up, so a tap is its
    // only signal - at 600 ms the controls were gone before the hand reached
    // the next one, so touch waits 2 s (user, 2026-09-25: "linger for 2s").
    const chromeHideMs = () => (TOUCH.matches ? 2000 : 600);
    let chromeTimer = null;
    let overChrome = false;
    // Kept, rather than cancelled, while the pointer rests on a control or a
    // panel is open: the timer comes back round instead of never firing again
    // once whatever held it is gone.
    const hideChrome = () => {
      if (overChrome || infoOpen || !$('muSettingsMenu').hidden) {
        chromeTimer = setTimeout(hideChrome, chromeHideMs());
        return;
      }
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
    for (const node of [stage.querySelector('.mu-stage-bar'), stage.querySelector('.mu-stage-text'),
      stage.querySelector('.mu-stage-queue'), $('muInfoPanel'), $('muPlayer')]) {
      if (!node) continue;
      node.addEventListener('pointerenter', () => { overChrome = true; showChrome(); });
      node.addEventListener('pointerleave', () => { overChrome = false; hideChromeLater(); });
    }

    document.addEventListener('fullscreenchange', () => {
      const on = !!document.fullscreenElement;
      document.body.classList.toggle('mu-fs', on);
      if (on) showChrome();
      const b = $('muFullscreen');
      b.setAttribute('aria-pressed', String(on));
      b.setAttribute('aria-label', on ? 'Leave fullscreen' : 'Fullscreen');
      setIcon(b, on ? 'fullscreenExit' : 'fullscreen');
      if (on) toggleStage(true);
    });
  }

  // ---- your own playlists --------------------------------------------------------------
  async function createPlaylist(songs) {
    const name = await promptDialog({ title: 'New playlist', value: '', placeholder: 'Name', ok: 'Create' });
    if (name === null) return null;
    const res = await api('/api/music/playlists', { method: 'POST',
      body: { name: name || 'New playlist', songs: (songs || []).map((i) => LIB.songs[i].ref) } });
    if (!res.ok) { toast(res.data.error || 'The playlist could not be made'); return null; }
    await loadLibrary({ quiet: true });
    toast(songs && songs.length ? `Added to "${name || 'New playlist'}"` : `"${name || 'New playlist'}" created`);
    return res.data.id;
  }

  async function addToPlaylist(pid, songs) {
    const pl = LIB.playlistById.get(pid);
    const res = await api(`/api/music/playlists/${pid}`, { method: 'PATCH',
      body: { add: songs.map((i) => LIB.songs[i].ref) } });
    if (!res.ok) { toast(res.data.error || 'Not added'); return; }
    await loadLibrary({ quiet: true });
    toast(`Added to "${pl ? pl.title : 'the playlist'}"`);
  }

  async function removeFromPlaylist(pid, i) {
    const s = LIB.songs[i];
    const pl = LIB.playlistById.get(pid);
    // Every ref that points at this song: the playlist may hold it by id or by path.
    const refs = (pl && pl.refs ? pl.refs : []).filter((r) => LIB.byRef.get(r) === i);
    const res = await api(`/api/music/playlists/${pid}`, { method: 'PATCH', body: { remove: refs.length ? refs : [s.ref] } });
    if (!res.ok) { toast(res.data.error || 'Not removed'); return; }
    await loadLibrary({ quiet: true });
    render();
    toast(`Removed from "${pl ? pl.title : 'the playlist'}"`);
  }

  async function renamePlaylist(pid) {
    const pl = LIB.playlistById.get(pid);
    const name = await promptDialog({ title: 'Rename playlist', value: pl ? pl.title : '', ok: 'Rename' });
    if (!name) return;
    const res = await api(`/api/music/playlists/${pid}`, { method: 'PATCH', body: { name } });
    if (!res.ok) { toast(res.data.error || 'Not renamed'); return; }
    await loadLibrary({ quiet: true });
    render();
  }

  async function deletePlaylist(pid) {
    const pl = LIB.playlistById.get(pid);
    const yes = await promptDialog({ title: 'Delete playlist?', input: false, ok: 'Delete',
      text: `"${pl ? pl.title : ''}" goes away. The songs stay in your library.` });
    if (!yes) return;
    const res = await api(`/api/music/playlists/${pid}`, { method: 'DELETE' });
    if (!res.ok) { toast(res.data.error || 'Not deleted'); return; }
    await loadLibrary({ quiet: true });
    location.hash = '#/library/playlists';
  }

  function addToPlaylistMenu(anchor, songs, at) {
    const items = [{ label: 'Add to playlist' }, { text: 'New playlist…', icon: 'plus', run: () => createPlaylist(songs) }];
    for (const pl of LIB.mine) items.push({ text: pl.title, icon: 'playlist', run: () => addToPlaylist(pl.id, songs) });
    openMenu(anchor, items, at);
  }

  // The "⋯" menu of a song. `inPlaylist`: the user playlist it is shown in.
  function songMenu(anchor, i, inPlaylist, at, extra) {
    const s = LIB.songs[i];
    const items = [
      ...(extra ? [extra, '-'] : []),
      { text: 'Play next', icon: 'playNext', run: () => playNext(i) },
      { text: 'Add to queue', icon: 'addQueue', run: () => addToQueue([i]) },
      { text: 'Add to playlist…', icon: 'queue', run: () => addToPlaylistMenu(anchor, [i], at) },
      s.p3 ? { text: 'Start radio', icon: 'radio', run: () => startRadio(radioFor('song', s.ref)) } : null,
      { text: 'Share', icon: 'share', run: () => shareSongs([i], { kind: 'song', label: s.title, subtitle: s.artist }) },
      // Only an admin may ask, because it fetches the video again. TEMPORARY:
      // this goes out with the cover job itself (song_covers.py).
      LIB.canSync ? { text: 'Improve this cover', icon: 'wand', run: () => improveCover(s) } : null,
      '-',
      { text: `Go to ${s.artist}`, icon: 'person', run: () => { location.hash = artistHref(s.artist); } },
    ];
    const al = albumOf(s);
    if (al) items.push({ text: `Go to album "${al.title}"`, icon: 'album', run: () => { location.hash = `#/album/${al.id}`; } });
    if (inPlaylist) items.push('-', { text: 'Remove from this playlist', icon: 'trash', run: () => removeFromPlaylist(inPlaylist, i) });
    // Admins only, like every delete in media-dl (LIB.canSync is the admin flag).
    if (LIB.canSync) items.push('-', { text: 'Delete song (every copy)…', icon: 'trash', run: () => deleteSong(i) });
    openMenu(anchor, items, at);
  }

  // Deleting a song (user, 2026-10-14): the song is one file hard-linked into
  // every playlist that holds it, so the server lists every name of that file
  // and this dialog shows them all and asks again before anything happens.
  // Everything goes to the trash as one batch (restorable for 30 days).
  async function deleteSong(i) {
    const s = LIB.songs[i];
    if (!s) return;
    let res = await api('/api/music/song/copies', { method: 'POST', body: { root: s.root, path: s.path } });
    if (!res.ok) { toast(res.data.error || 'The copies could not be listed'); return; }
    for (;;) {
      const answer = await deleteSongDialog(s, res.data);
      if (!answer) return;
      const copies = res.data.copies.map((c) => `${c.root}:${c.path}`);
      const del = await api('/api/music/song/delete', { method: 'POST',
        body: { root: s.root, path: s.path, copies, allow_redownload: answer.allowRedownload } });
      if (del.status === 409 && del.data.copies) {
        // The file gained or lost a name since the dialog opened: ask again with the new list.
        toast(del.data.error);
        res = del;
        continue;
      }
      if (!del.ok) { toast(del.data.error || 'Not deleted'); return; }
      const n = del.data.deleted.length;
      const failed = (del.data.failed || []).length;
      toast(failed ? `Deleted ${n} of ${n + failed} copies - ${failed} could not be moved to the trash`
        : `Deleted "${s.title}" (${n} ${n === 1 ? 'copy' : 'copies'}) - restorable from the trash for 30 days`);
      await loadLibrary({ quiet: true });
      render();
      return;
    }
  }

  // Resolves {allowRedownload} on "Delete", null on Cancel/Escape.
  function deleteSongDialog(s, info) {
    const copies = info.copies || [];
    const confirmBox = el('input', { type: 'checkbox' });
    const redownload = el('input', { type: 'checkbox' });
    const okBtn = el('button', { type: 'submit', class: 'ah-btn danger mu-primary', disabled: true },
      `Delete ${copies.length} ${copies.length === 1 ? 'copy' : 'copies'}`);
    confirmBox.addEventListener('change', () => { okBtn.disabled = !confirmBox.checked; });
    const cancelBtn = el('button', { type: 'button', class: 'ah-btn' }, 'Cancel');
    const form = el('form', { method: 'dialog', class: 'mu-dialog-form' },
      el('h2', { text: `Delete "${s.title}"?` }),
      el('p', { class: 'mu-dialog-text', text: `${s.artist} - this deletes the song's file under every name it has: `
        + `${copies.length} ${copies.length === 1 ? 'copy' : 'copies'}. It disappears from every playlist below.` }),
      el('ul', { class: 'mu-delete-copies' },
        copies.map((c) => el('li', null, el('span', { class: 'mu-delete-root', text: c.root_name }), ' ', c.path))),
      info.unlisted ? el('p', { class: 'mu-dialog-text mu-delete-warn',
        text: `The file has ${info.unlisted} more ${info.unlisted === 1 ? 'name' : 'names'} outside the music library `
          + `(${info.nlink} in all). ${info.unlisted === 1 ? 'That one stays' : 'Those stay'}, so the space is not freed until `
          + `${info.unlisted === 1 ? 'it goes' : 'they go'} too.` }) : null,
      el('label', { class: 'mu-delete-check' }, redownload, ' Allow a playlist sync to download it again'),
      el('label', { class: 'mu-delete-check' }, confirmBox, ` Yes, delete all ${copies.length} ${copies.length === 1 ? 'copy' : 'copies'}`),
      el('div', { class: 'mu-dialog-actions' }, cancelBtn, okBtn));
    const dlg = el('dialog', { class: 'mu-dialog mu-delete-dialog' }, form);
    document.body.append(dlg);
    return new Promise((resolve) => {
      let settled = false;
      const finish = (yes) => {
        if (settled) return;
        settled = true;
        if (dlg.open) dlg.close();
        dlg.remove();
        resolve(yes ? { allowRedownload: redownload.checked } : null);
      };
      dlg.addEventListener('close', () => finish(false));
      cancelBtn.onclick = () => finish(false);
      form.onsubmit = (e) => { e.preventDefault(); if (confirmBox.checked) finish(true); };
      dlg.showModal();
      cancelBtn.focus();
    });
  }

  // The cover of a song is a frame ten seconds into its video, which on a
  // re-upload channel is the intro and elsewhere a fade to black. This asks
  // the background job for a better frame, chosen for that video.
  async function improveCover(s) {
    const res = await api('/api/music/covers/song', { method: 'POST', body: { root: s.root, path: s.path } });
    if (!res.ok) { toast(res.data.error || 'That cover could not be queued'); return; }
    const st = res.data.status || {};
    toast(st.enabled ? 'Queued: the cover is taken from a better frame shortly'
      : 'Queued - but the cover job is switched off in Settings');
  }

  function albumOf(s) {
    if (!s.album) return null;
    for (const a of LIB.albums) if (a.songs.includes(s.i)) return a;
    return null;
  }

  // ---- syncing a downloaded playlist -------------------------------------------------------
  const SYNC = new Map();            // playlist id -> {task, status, progress}

  async function syncPlaylist(pl, url) {
    const res = await api('/api/music/sync', { method: 'POST', body: url ? { playlist: pl.id, url } : { playlist: pl.id } });
    if (res.status === 400 && res.data.need_url) {
      const link = await promptDialog({
        title: 'Link this playlist to YouTube Music',
        text: 'It was downloaded before its link was kept. Paste the playlist\'s link from YouTube Music once - Sync remembers it from then on.',
        placeholder: 'https://music.youtube.com/playlist?list=…', ok: 'Sync',
      });
      if (link) syncPlaylist(pl, link);
      return;
    }
    if (!res.ok) { toast(res.data.error || 'The sync could not start'); return; }
    SYNC.set(pl.id, { task: res.data.task_id, status: 'Queued', progress: '' });
    toast('Sync started: new songs download in the background');
    render();
    pollSync(pl.id);
  }

  async function pollSync(pid) {
    const st = SYNC.get(pid);
    if (!st) return;
    const res = await api(`/api/music/task/${st.task}`);
    if (res.ok) {
      st.status = res.data.status || st.status;
      st.progress = res.data.progress || '';
    }
    updateSyncState(pid);
    if (['Queued', 'Running'].includes(st.status)) {
      setTimeout(() => pollSync(pid), 4000);
      return;
    }
    // Done: the library notices the new files within a minute or so.
    st.done = Date.now();
    waitForRebuild(LIB.built, 0);
  }

  async function waitForRebuild(before, tries) {
    if (tries > 40) return;
    await loadLibrary({ quiet: true });
    if (LIB.built === before) setTimeout(() => waitForRebuild(before, tries + 1), 6000);
    else toast('The library is up to date');
  }

  function syncText(pid) {
    const st = SYNC.get(pid);
    if (!st) return '';
    if (st.status === 'Queued') return 'Sync queued…';
    if (st.status === 'Running') return `Syncing… ${st.progress || ''}`.trim();
    return `Sync ${String(st.status || 'finished').toLowerCase()}`;
  }

  function updateSyncState(pid) {
    const node = document.querySelector(`[data-sync-state="${pid}"]`);
    if (node) node.textContent = syncText(pid);
  }

  // ---- views -------------------------------------------------------------------------------
  const main = $('muMain');
  const TOUCH = window.matchMedia('(hover: none)');

  // What a view is made of, into <main>: nested lists flattened, and the parts
  // a view left out (null, false) dropped - replaceChildren would print them
  // as the text "null" and "[object HTMLDivElement]".
  function setMain(...parts) {
    main.replaceChildren(...parts.flat(Infinity).filter((p) => p !== null && p !== undefined && p !== false));
  }

  function artistHref(name) {
    return `#/artist/${encodeURIComponent(name)}`;
  }

  function playlistSub(pl) {
    if (pl.source === 'user') return `Your playlist • ${plural(pl.songs.length, 'song')}`;
    if (pl.source === 'youtube') return `${pl.owner} • ${plural(pl.songs.length, 'song')}`;
    return `${pl.library || pl.owner} • ${plural(pl.songs.length, 'song')}`;
  }

  // `open`: what a click on the card does instead of going to `href` (a radio
  // has no page of its own - it plays).
  function card({ href, art, title, sub, round, play, open, extra }) {
    const node = el('a', { class: 'mu-card' + (round ? ' round' : '') + (extra ? ' ' + extra : ''), href: href || '#',
      onclick: open ? (e) => { e.preventDefault(); open(); } : null },
      el('div', { class: 'mu-card-art' }, art,
        play ? el('button', { type: 'button', class: 'mu-card-play', 'aria-label': `Play ${title}`,
          onclick: (e) => { e.preventDefault(); e.stopPropagation(); play(); } }, icon('play')) : null),
      el('div', { class: 'mu-card-title', text: title, title }),
      sub ? el('div', { class: 'mu-card-sub', text: sub, title: sub }) : null);
    return node;
  }

  function playlistCard(pl) {
    return card({ href: `#/playlist/${pl.id}`, art: mosaic(pl.songs, pl.title, 300), title: pl.title,
      sub: playlistSub(pl), play: () => playList(pl.songs, 0, { context: pl.id }) });
  }

  function albumCard(a) {
    return card({ href: `#/album/${a.id}`, art: mosaic(a.songs, a.title, 300), title: a.title,
      sub: `Album • ${a.artist}`, play: () => playList(a.songs, 0, { context: a.id }) });
  }

  function channelPlaylists(name) {
    const ch = LIB.channelByName.get(String(name).toLowerCase());
    return ch ? ch.playlists.map((id) => LIB.playlistById.get(id)).filter(Boolean) : [];
  }

  // A channel may hold playlists and no songs of its own (the account the
  // playlists were made with): its art and its Play come from those.
  function artistCard(name, list) {
    const pls = channelPlaylists(name);
    const all = list.length ? list : pls.flatMap((p) => p.songs);
    const withCover = all.map((i) => LIB.songs[i]).find((s) => s && s.cover);
    const sub = [pls.length ? plural(pls.length, 'playlist') : null, list.length ? plural(list.length, 'song') : null]
      .filter(Boolean).join(' • ');
    return card({ href: artistHref(name), art: withCover ? coverImg(withCover, 300) : placeholder(name),
      title: name, sub, round: true, play: all.length ? () => playList(all, 0) : null });
  }

  function songCard(i, list, n) {
    const s = LIB.songs[i];
    return card({ href: artistHref(s.artist), art: coverImg(s, 300), title: s.title, sub: s.artist,
      play: () => playList(list, n, { src: 'user' }) });
  }

  function radioCard(desc, sub, resume) {
    const start = () => startRadio(desc, { resume });
    return card({ art: el('div', { class: 'mu-radio-art' }, mosaic(desc.seeds, desc.title, 300),
      el('span', { class: 'mu-radio-badge', 'aria-hidden': 'true' }, icon('radio'))),
    title: desc.title, sub, play: start, open: start, extra: 'mu-radio-card' });
  }

  // ---- a link to songs ------------------------------------------------------
  // Made in media-dl's own share store, so that everything already built for
  // links - the expiry, revoking, view counts, the Shares page in Settings -
  // holds for these too (user, 2026-09-23: "consider piggy-backing media-dl's
  // system such that the shared can be managed there"). Nothing of the library
  // travels with it: the token IS the access, and the page it opens knows only
  // the songs that were put in it.
  const SHARE_MAX = 500;

  // Share copies the link, no box in between (user, 2026-09-23: "Instead of
  // giving me a 'link ready' box, it should just copy the link into my
  // clipboard"). The catch: the link exists only once the server has made it,
  // and a browser lets a page write the clipboard only within the click that
  // asked - Safari refuses after any await, Chrome after a few seconds. A
  // ClipboardItem takes a PROMISE of its contents, so the write is started
  // here, still inside the click, and filled in when the link arrives. Where
  // that is missing or refused (no secure context, the app's WebView), the old
  // box with the link in its field comes back, so the link is never lost.
  async function shareSongs(list, opts = {}) {
    const songs = list.filter((i) => LIB.songs[i]);
    if (!songs.length) { toast('Nothing to share here'); return; }
    const cut = songs.length > SHARE_MAX;
    const refs = songs.slice(0, SHARE_MAX).map((i) => LIB.songs[i].ref);
    const made = api('/api/music/share', { method: 'POST',
      body: { refs, kind: opts.kind || 'songs', label: opts.label || '', subtitle: opts.subtitle || '' } })
      .then((res) => {
        if (!res.ok) throw new Error((res.data && res.data.error) || 'That link could not be made');
        return { url: location.origin + (res.data.url || `/share/${res.data.id}`), data: res.data };
      });
    let copying = null;
    try {
      if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
        copying = navigator.clipboard.write([new ClipboardItem({
          'text/plain': made.then((m) => new Blob([m.url], { type: 'text/plain' })) })]);
        copying.catch(() => {});        // judged below; never an unhandled rejection
      }
    } catch (e) { copying = null; }

    let link;
    try { link = await made; } catch (e) { toast(e.message); return; }
    const { url, data } = link;
    let copied = false;
    if (copying) { try { await copying; copied = true; } catch (e) { /* try the plain way */ } }
    if (!copied && navigator.clipboard && navigator.clipboard.writeText) {
      try { await navigator.clipboard.writeText(url); copied = true; } catch (e) { /* the box, then */ }
    }
    if (copied) {
      toast(cut ? `Link copied - it holds the first ${SHARE_MAX} songs` : 'Link copied');
      return;
    }
    const until = data.expires_at ? `, until ${new Date(data.expires_at * 1000).toLocaleDateString()}` : '';
    const answer = await promptDialog({
      title: 'Copy this link',
      text: `Anyone with this link can play and download ${plural(data.songs || refs.length, 'song')}${until}`
        + `.${cut ? ` Only the first ${SHARE_MAX} are in it.` : ''}`
        + ' Settings > Shares lists it, and is where it is ended.',
      value: url,
      ok: 'Copy link',
    });
    if (answer === null) return;
    try {
      await navigator.clipboard.writeText(url);   // the button's click is a fresh gesture
      toast('Link copied');
    } catch (e) {
      await promptDialog({ title: 'Copy this link',
        text: 'This browser does not let the page copy it - select the link and copy it yourself.',
        value: url, ok: 'Done' });
    }
  }

  function shareButton(list, opts) {
    return el('button', { type: 'button', class: 'ah-btn',
      title: 'A link that plays these songs, for someone with no account here',
      onclick: () => shareSongs(list, opts) }, icon('share'), 'Share');
  }

  function radioButton(desc) {
    return desc ? el('button', { type: 'button', class: 'ah-btn', title: 'Songs that sound alike, one leading to the next',
      onclick: () => startRadio(desc) }, icon('radio'), 'Radio') : null;
  }

  // A day's own order: the styles on offer change daily, not on every visit
  // to Home.
  function dailyShuffle(list) {
    let h = 2166136261;
    for (const c of new Date().toDateString()) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
    const rand = () => {
      h = Math.imul(h ^ (h >>> 15), 2246822507);
      h = Math.imul(h ^ (h >>> 13), 3266489909);
      return ((h ^= h >>> 16) >>> 0) / 4294967296;
    };
    const a = list.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(rand() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  // "Radios for you": the radios listened to lately (to go on with), radios
  // from songs picked by hand, from the artists played most, and from styles
  // at random. Autoplay's own radios are not offered: nobody chose those.
  function radiosForYou() {
    if (!LIB.positioned.length) return [];
    const cards = [];
    const used = new Set();
    const add = (desc, sub, resume) => {
      if (!desc || used.has(desc.id)) return false;
      used.add(desc.id);
      cards.push(radioCard(desc, sub, resume));
      return true;
    };
    let n = 0;
    for (const r of ME.radios) {
      if (n >= 4) break;
      if (r.kind === 'auto') continue;
      // A song radio is not resumed: it opens with its song, whatever it
      // played last time, so its card must not promise otherwise.
      const song = r.kind === 'song';
      if (add(radioFor(r.kind, r.key), song ? 'Radio • from a song you played' : 'Radio • go on where it stopped',
              !song)) n += 1;
    }
    n = 0;
    for (let k = ME.events.length - 1; k >= 0 && n < 4; k--) {
      const [ref, src] = ME.events[k];
      if (src !== 'user') continue;
      const i = LIB.byRef.get(ref);
      if (i !== undefined && add(radioFor('song', LIB.songs[i].ref), `Radio • from a song you played`)) n += 1;
    }
    const plays = new Map();
    for (const [ref] of ME.events) {
      const i = LIB.byRef.get(ref);
      if (i === undefined || LIB.songs[i].artist === 'Unknown artist') continue;
      const key = LIB.songs[i].artist.toLowerCase();
      plays.set(key, (plays.get(key) || 0) + 1);
    }
    n = 0;
    for (const [key, count] of [...plays.entries()].sort((a, b) => b[1] - a[1])) {
      if (n >= 2 || count < 3) break;
      if (add(radioFor('artist', LIB.artists.get(key).name), 'Radio • an artist you play often')) n += 1;
    }
    n = 0;
    const styles = dailyShuffle([...LIB.genres.keys()].filter((g) => (LIB.genres.get(g) || []).length >= 5));
    for (const g of styles) {
      if (n >= 4) break;
      if (add(radioFor('genre', g), 'Radio • a style to explore')) n += 1;
    }
    return cards;
  }

  function shelf(title, cards, moreHref) {
    if (!cards.length) return null;
    const row = el('div', { class: 'mu-row' }, cards);
    return el('section', { class: 'mu-shelf' },
      el('div', { class: 'mu-shelf-head' },
        el('h2', { class: 'mu-h2', text: title }),
        moreHref ? el('a', { class: 'mu-more-link', href: moreHref, text: 'Show all' }) : null),
      row);
  }

  // A track list, drawn in chunks as it scrolls into view: the whole library is
  // thousands of rows, and a phone should not build them all at once.
  function trackList(list, opts = {}) {
    const ol = el('ol', { class: 'mu-tracks' });
    const CHUNK = 120;
    let drawn = 0;
    // The observer draws the next rows as the end comes into view; the button
    // is there for when it does not (a browser without it, a hidden tab).
    const moreBtn = el('button', { type: 'button', class: 'mu-text-btn', onclick: () => drawMore() });
    const sentinel = el('li', { class: 'mu-list-foot' }, moreBtn);
    function drawMore() {
      const end = Math.min(list.length, drawn + CHUNK);
      const frag = document.createDocumentFragment();
      for (let n = drawn; n < end; n++) frag.append(trackRow(list, n, opts));
      sentinel.before(frag);
      drawn = end;
      if (drawn >= list.length) {
        observer.disconnect();
        sentinel.textContent = opts.foot || '';
      } else {
        moreBtn.textContent = `Show ${Math.min(CHUNK, list.length - drawn)} more (${list.length - drawn} to go)`;
      }
      markPlaying();
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) drawMore();
    }, { root: main, rootMargin: '600px' });
    ol.append(sentinel);
    drawMore();
    if (drawn < list.length) observer.observe(sentinel);
    return ol;
  }

  function trackRow(list, n, opts) {
    const i = list[n];
    const s = LIB.songs[i];
    const play = () => playList(list, n, { context: opts.context, src: 'user' });
    const more = el('button', { type: 'button', class: 'mu-icon-btn mu-track-more', 'aria-label': `More for ${s.title}`,
      'aria-haspopup': 'menu', 'aria-expanded': 'false',
      onclick: (e) => { e.stopPropagation(); songMenu(e.currentTarget, i, opts.userPlaylist); } }, icon('more'));
    const al = s.album && s.album !== s.title ? albumOf(s) : null;
    const row = el('li', { class: 'mu-track', dataset: { song: String(i) },
      ondblclick: play,
      // A touch screen has no double click and no hover: a tap plays, as in
      // YouTube Music - unless it was on a link or a button of the row.
      onclick: (e) => { if (TOUCH.matches && !e.target.closest('a, button')) play(); },
      oncontextmenu: (e) => { e.preventDefault(); songMenu(more, i, opts.userPlaylist, { x: e.clientX, y: e.clientY }); } },
      el('div', { class: 'mu-track-num' },
        el('span', { class: 'mu-n', text: String(n + 1) }),
        el('button', { type: 'button', class: 'mu-track-play', 'aria-label': `Play ${s.title}`, onclick: play }, icon('play'))),
      el('div', { class: 'mu-track-cover' }, coverImg(s, 96, true)),
      el('div', { class: 'mu-track-main' },
        el('div', { class: 'mu-track-title', text: s.title, title: s.title }),
        el('div', { class: 'mu-track-artist' }, el('a', { href: artistHref(s.artist), text: s.artist }))),
      el('div', { class: 'mu-track-album' },
        al ? el('a', { href: `#/album/${al.id}`, text: s.album }) : (s.album && s.album !== s.title ? s.album : '')),
      el('div', { class: 'mu-track-dur', text: s.dur ? fmtTime(s.dur) : '' }),
      more);
    return row;
  }

  function hero({ art, round, kind, title, sub, meta, actions }) {
    return el('header', { class: 'mu-hero' },
      el('div', { class: 'mu-hero-art' + (round ? ' round' : '') }, art),
      el('div', { class: 'mu-hero-text' },
        el('div', { class: 'mu-hero-kind', text: kind }),
        el('h1', { class: 'mu-hero-title', text: title }),
        sub ? el('div', { class: 'mu-hero-sub' }, sub) : null,
        meta ? el('div', { class: 'mu-hero-meta', text: meta }) : null,
        actions ? el('div', { class: 'mu-actions' }, actions) : null));
  }

  function playButtons(list, context) {
    return [
      el('button', { type: 'button', class: 'ah-btn mu-primary', onclick: () => playList(list, 0, { context }) }, icon('play'), 'Play'),
      el('button', { type: 'button', class: 'ah-btn', onclick: () => playList(list, 0, { context, shuffle: true }) }, icon('shuffle'), 'Shuffle'),
    ];
  }

  function viewHome() {
    const recent = LIB.songs.map((s) => s.i).sort((a, b) => LIB.songs[b].added - LIB.songs[a].added);
    // Channels with playlists first (whose the playlists are), then the
    // artists with the most songs.
    const artists = [...LIB.artists.values()]
      .map((a) => ({ name: a.name, list: artistSongs(a.name), pls: channelPlaylists(a.name).length }))
      .filter((a) => (a.pls || a.list.length >= 3) && a.name !== 'Unknown artist')
      .sort((a, b) => b.pls - a.pls || b.list.length - a.list.length).slice(0, 24);
    const albums = LIB.albums.slice().sort((a, b) =>
      Math.max(...b.songs.map((i) => LIB.songs[i].added)) - Math.max(...a.songs.map((i) => LIB.songs[i].added))).slice(0, 24);
    const genres = [...LIB.genres.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, 24);
    const recent24 = recent.slice(0, 24);
    // What was played lately, each song once: from the plays the library
    // brought along, newest first.
    const played = [];
    const seenPlayed = new Set();
    for (let n = ME.events.length - 1; n >= 0 && played.length < 24; n--) {
      const i = LIB.byRef.get(ME.events[n][0]);
      if (i === undefined || seenPlayed.has(i)) continue;
      seenPlayed.add(i);
      played.push(i);
    }
    setMain(
      el('h1', { class: 'mu-h1', text: 'Home' }),
      shelf('Recently played', played.map((i, n) => songCard(i, played, n)), '#/history'),
      shelf('Radios for you', radiosForYou()),
      LIB.mine.length ? shelf('Your playlists', LIB.mine.map(playlistCard), '#/library/playlists') : null,
      shelf('Recently added', recent24.map((i, n) => songCard(i, recent, n)), '#/library/songs'),
      shelf('Playlists', LIB.playlists.map(playlistCard), '#/library/playlists'),
      shelf('Artists and channels', artists.map((a) => artistCard(a.name, a.list)), '#/library/artists'),
      shelf('Albums', albums.map(albumCard), '#/library/albums'),
      genres.length ? el('section', { class: 'mu-shelf' },
        el('div', { class: 'mu-shelf-head' }, el('h2', { class: 'mu-h2', text: 'Styles' })),
        el('div', { class: 'mu-chips' }, genres.map(([g, list]) =>
          el('a', { href: `#/genre/${encodeURIComponent(g)}`, text: `${g} · ${list.length}` })))) : null,
      LIB.songs.length ? null : el('div', { class: 'mu-empty', text: 'No music here yet.' }));
  }

  function viewLibrary(tab) {
    const tabs = [['playlists', 'Playlists'], ['songs', 'Songs'], ['albums', 'Albums'], ['artists', 'Artists']];
    const nav = el('nav', { class: 'mu-tabs', 'aria-label': 'Library' },
      tabs.map(([t, label]) => el('a', { href: `#/library/${t}`, text: label, 'aria-current': t === tab ? 'page' : null })));
    let body;
    if (tab === 'songs') {
      const list = LIB.songs.map((s) => s.i).sort((a, b) => LIB.songs[b].added - LIB.songs[a].added);
      body = [el('div', { class: 'mu-actions', style: 'margin:0 0 var(--sp-4)' }, playButtons(list, 'library')),
        trackList(list, { context: 'library', foot: `${plural(list.length, 'song')} • ${fmtTotal(totalDur(list))}` })];
    } else if (tab === 'albums') {
      body = LIB.albums.length ? el('div', { class: 'mu-grid' }, LIB.albums.map(albumCard))
        : el('div', { class: 'mu-empty', text: 'No albums - songs with an album tag of two or more songs show up here.' });
    } else if (tab === 'artists') {
      const all = [...LIB.artists.values()].map((a) => ({ name: a.name, list: artistSongs(a.name) }))
        .filter((a) => a.list.length || channelPlaylists(a.name).length).sort((a, b) => a.name.localeCompare(b.name));
      body = gridInChunks(all, (a) => artistCard(a.name, a.list));
    } else {
      body = el('div', { class: 'mu-grid' },
        el('a', { class: 'mu-card', href: '#', onclick: (e) => { e.preventDefault(); createPlaylist([]); } },
          el('div', { class: 'mu-card-art' }, el('div', { class: 'mu-ph' }, icon('plus'))),
          el('div', { class: 'mu-card-title', text: 'New playlist' })),
        LIB.mine.map(playlistCard), LIB.playlists.map(playlistCard));
    }
    setMain(el('h1', { class: 'mu-h1', text: 'Library' }), nav, body);
  }

  function gridInChunks(items, make) {
    const grid = el('div', { class: 'mu-grid' });
    let drawn = 0;
    const moreBtn = el('button', { type: 'button', class: 'mu-text-btn', onclick: () => draw() });
    const sentinel = el('div', { style: 'grid-column:1/-1' }, moreBtn);
    grid.append(sentinel);
    const draw = () => {
      const end = Math.min(items.length, drawn + 120);
      const frag = document.createDocumentFragment();
      for (let n = drawn; n < end; n++) frag.append(make(items[n]));
      sentinel.before(frag);
      drawn = end;
      if (drawn >= items.length) {
        observer.disconnect();
        sentinel.remove();
      } else {
        moreBtn.textContent = `Show more (${items.length - drawn} to go)`;
      }
    };
    const observer = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) draw(); },
      { root: main, rootMargin: '600px' });
    draw();
    if (drawn < items.length) observer.observe(sentinel);
    return grid;
  }

  function viewPlaylist(pid) {
    const pl = LIB.playlistById.get(pid);
    if (!pl) return notFound('That playlist is not here (any more).');
    const user = pl.source === 'user';
    const list = pl.songs;
    const actions = playButtons(list, pl.id);
    actions.push(radioButton(radioFor('playlist', pl.id)));
    actions.push(shareButton(list, { kind: 'playlist', label: pl.title,
      subtitle: pl.source === 'youtube' ? pl.owner || '' : '' }));
    if (user) {
      actions.push(
        el('button', { type: 'button', class: 'ah-btn', onclick: () => renamePlaylist(pl.id) }, 'Rename'),
        el('button', { type: 'button', class: 'ah-btn', onclick: () => deletePlaylist(pl.id) }, 'Delete'));
    } else {
      actions.push(el('button', { type: 'button', class: 'ah-btn', onclick: (e) => addToPlaylistMenu(e.currentTarget, list) },
        icon('plus'), 'Add to playlist'));
      if (pl.source === 'youtube' && LIB.canSync) {
        const syncing = SYNC.get(pl.id) && ['Queued', 'Running'].includes(SYNC.get(pl.id).status);
        actions.push(el('button', { type: 'button', class: 'ah-btn', disabled: syncing,
          title: pl.url ? `Download what is new in ${pl.url}` : 'Download what is new - you will be asked for the link once',
          onclick: () => syncPlaylist(pl) }, icon('sync'), 'Sync'));
        actions.push(el('span', { class: 'mu-sync-state', dataset: { syncState: pl.id }, text: syncText(pl.id) }));
      }
    }
    const sub = user ? 'Your playlist'
      : pl.source === 'youtube' ? el('span', {}, el('a', { href: artistHref(pl.owner), text: pl.owner }), ' • YouTube Music')
        : `${pl.library || ''}${pl.where ? ' • ' + pl.where : ''}`;
    const syncedText = pl.synced ? ` • synced ${new Date(pl.synced * 1000).toLocaleDateString()}` : '';
    setMain(
      hero({ art: mosaic(list, pl.title, 600), kind: 'Playlist', title: pl.title, sub,
        meta: `${plural(list.length, 'song')} • ${fmtTotal(totalDur(list))}${syncedText}`, actions }),
      list.length ? trackList(list, { context: pl.id, userPlaylist: user ? pl.id : null })
        : el('div', { class: 'mu-empty', text: user ? 'Empty so far - add songs with their ⋯ menu.' : 'No songs.' }));
  }

  function viewAlbum(aid) {
    const a = LIB.albumById.get(aid);
    if (!a) return notFound('That album is not here (any more).');
    setMain(
      hero({ art: mosaic(a.songs, a.title, 600), kind: 'Album', title: a.title,
        sub: el('a', { href: artistHref(a.artist), text: a.artist }),
        meta: `${plural(a.songs.length, 'song')} • ${fmtTotal(totalDur(a.songs))}`,
        actions: [...playButtons(a.songs, a.id), radioButton(radioFor('album', a.id)),
          el('button', { type: 'button', class: 'ah-btn', onclick: (e) => addToPlaylistMenu(e.currentTarget, a.songs) }, icon('plus'), 'Add to playlist'),
          shareButton(a.songs, { kind: 'album', label: a.title, subtitle: a.artist })] }),
      trackList(a.songs, { context: a.id }));
  }

  // An artist, and the channel of that name: its playlists and its songs -
  // "channel view (playlists and directly downloaded titles)".
  function viewArtist(name) {
    const key = name.toLowerCase();
    const ch = LIB.channelByName.get(key);
    const list = artistSongs(name);
    const pls = ch ? ch.playlists.map((id) => LIB.playlistById.get(id)).filter(Boolean) : [];
    const albums = LIB.albums.filter((a) => a.artist.toLowerCase() === key);
    if (!list.length && !pls.length) return notFound(`Nothing by ${name} here.`);
    // A channel with playlists and no songs of its own plays its playlists.
    const playable = list.length ? list : [...new Set(pls.flatMap((p) => p.songs))];
    const withCover = playable.map((i) => LIB.songs[i]).find((s) => s && s.cover);
    setMain(
      hero({ art: withCover ? coverImg(withCover, 600) : placeholder(name), round: true,
        kind: ch ? 'Channel' : 'Artist', title: name,
        meta: [pls.length ? plural(pls.length, 'playlist') : null, list.length ? plural(list.length, 'song') : null].filter(Boolean).join(' • '),
        actions: playable.length ? [...playButtons(playable, `artist:${key}`), radioButton(radioFor('artist', name)),
          shareButton(playable, { kind: 'artist', label: name })] : null }),
      pls.length ? shelf('Playlists', pls.map(playlistCard)) : null,
      albums.length ? shelf('Albums', albums.map(albumCard)) : null,
      list.length ? el('section', { class: 'mu-shelf' },
        el('div', { class: 'mu-shelf-head' }, el('h2', { class: 'mu-h2', text: 'Songs' })),
        trackList(list, { context: `artist:${key}` })) : null);
  }

  function viewGenre(g) {
    const list = (LIB.genres.get(g) || []).slice().sort((a, b) => LIB.songs[b].added - LIB.songs[a].added);
    if (!list.length) return notFound('No songs in that style.');
    setMain(
      hero({ art: mosaic(list, g, 600), kind: 'Style', title: g,
        meta: `${plural(list.length, 'song')} • ${fmtTotal(totalDur(list))} • as the AI index hears it`,
        actions: [...playButtons(list, `genre:${g}`), radioButton(radioFor('genre', g)),
          shareButton(list, { kind: 'style', label: g })] }),
      trackList(list, { context: `genre:${g}` }));
  }

  // ---- history ----------------------------------------------------------------
  // Every play the server kept, newest first: the song, when it played, and
  // what it was played from. The library brings the newest few hundred along
  // for the radios (ME.events); this pages back through the rest.
  const HIST = { rows: [], more: false, loaded: false, loading: false, error: '', at: 0 };
  const HIST_STALE_MS = 30000;      // a page left open: read it again on the way in

  async function loadHistory(opts = {}) {
    if (HIST.loading) return;
    HIST.loading = true;
    const oldest = HIST.rows.length ? HIST.rows[HIST.rows.length - 1][2] : 0;
    const before = opts.more && oldest ? `&before=${oldest}` : '';
    const res = await api(`/api/music/history?limit=200${before}`);
    HIST.loading = false;
    if (!res.ok) {
      HIST.error = res.data.error || 'The history could not be read.';
    } else {
      const rows = Array.isArray(res.data.events) ? res.data.events : [];
      HIST.rows = opts.more ? HIST.rows.concat(rows) : rows;
      HIST.more = !!res.data.more;
      HIST.error = '';
      HIST.loaded = true;
      HIST.at = Date.now();
    }
    if (route().view === 'history') viewHistory();
  }

  // Dates in the page's own language, not the browser's: everything else
  // here is English, and a German weekday between English headings reads
  // like a bug.
  const DATES = 'en-GB';

  function dayLabel(t) {
    const d = new Date(t * 1000);
    const now = new Date();
    const day = (x) => x.toDateString();
    if (day(d) === day(now)) return 'Today';
    if (day(d) === day(new Date(now.getTime() - 86400000))) return 'Yesterday';
    const opts = { weekday: 'long', day: 'numeric', month: 'long' };
    if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString(DATES, opts);
  }

  function clockTime(t) {
    return new Date(t * 1000).toLocaleTimeString(DATES, { hour: '2-digit', minute: '2-digit' });
  }

  // One play: the time instead of the track number, and where it was played
  // from where the album would be. Playing it here plays that one song - its
  // list is one click away, on the link.
  function historyRow(p) {
    const s = LIB.songs[p.i];
    const one = () => playList([p.i], 0, { src: 'user' });
    const place = ctxLabel(p.ctx);
    const more = el('button', { type: 'button', class: 'mu-icon-btn mu-track-more', 'aria-label': `More for ${s.title}`,
      'aria-haspopup': 'menu', 'aria-expanded': 'false',
      onclick: (e) => { e.stopPropagation(); songMenu(e.currentTarget, p.i, null); } }, icon('more'));
    return el('li', { class: 'mu-track', dataset: { song: String(p.i) },
      ondblclick: one,
      onclick: (e) => { if (TOUCH.matches && !e.target.closest('a, button')) one(); },
      oncontextmenu: (e) => { e.preventDefault(); songMenu(more, p.i, null, { x: e.clientX, y: e.clientY }); } },
      el('div', { class: 'mu-track-num' },
        el('span', { class: 'mu-n', text: clockTime(p.t) }),
        el('button', { type: 'button', class: 'mu-track-play', 'aria-label': `Play ${s.title}`, onclick: one }, icon('play'))),
      el('div', { class: 'mu-track-cover' }, coverImg(s, 96, true)),
      el('div', { class: 'mu-track-main' },
        el('div', { class: 'mu-track-title', text: s.title, title: s.title }),
        el('div', { class: 'mu-track-artist' }, el('a', { href: artistHref(s.artist), text: s.artist }))),
      el('div', { class: 'mu-track-album' }, place
        ? [el('span', { class: 'mu-from', text: 'from ' }),
          place.href ? el('a', { href: place.href, text: place.text }) : el('span', { text: place.text })]
        : ''),
      el('div', { class: 'mu-track-dur', text: s.dur ? fmtTime(s.dur) : '' }),
      more);
  }

  // The playlists, albums, artists, styles and radios those plays came from,
  // the most recent first: songs played THROUGH something, as a row of cards
  // to go back to.
  function historyPlaces(plays) {
    const cards = [];
    const seen = new Set();
    for (const p of plays) {
      if (!p.ctx || seen.has(p.ctx)) continue;
      seen.add(p.ctx);
      const [kind, rest] = ctxParts(p.ctx);
      let made = null;
      if (kind === 'pl' && LIB.playlistById.has(rest)) {
        made = playlistCard(LIB.playlistById.get(rest));
      } else if (kind === 'al' && LIB.albumById.has(rest)) {
        made = albumCard(LIB.albumById.get(rest));
      } else if (kind === 'ar') {
        const list = artistSongs(rest);
        if (list.length || channelPlaylists(rest).length) made = artistCard(ctxLabel(p.ctx).text, list);
      } else if (kind === 'ge' && LIB.genres.has(rest)) {
        const list = LIB.genres.get(rest);
        made = card({ href: `#/genre/${encodeURIComponent(rest)}`, art: mosaic(list, rest, 300), title: rest,
          sub: `Style • ${plural(list.length, 'song')}`,
          play: () => playList(list, 0, { context: `genre:${rest}`, shuffle: true }) });
      } else if (kind === 'radio') {
        const [rk, rkey] = ctxParts(rest);
        const desc = radioFor(rk, rkey);
        if (desc) made = radioCard(desc, 'Radio - go on where it stopped', true);
      }
      if (made) cards.push(made);
      if (cards.length >= 20) break;
    }
    return cards;
  }

  function viewHistory() {
    // Coming back to a tab that has been open: read it again behind what is
    // already there, so the list is not one session old.
    if (HIST.loaded && Date.now() - HIST.at > HIST_STALE_MS) loadHistory();
    if (!HIST.loaded && !HIST.error) {
      setMain(el('h1', { class: 'mu-h1', text: 'History' }),
        el('div', { class: 'mu-loading', text: 'Reading what you played…' }));
      loadHistory();
      return;
    }
    // A play of a song that is not in the library any more has nothing to
    // show: the file was removed, or this account cannot see its root.
    const plays = HIST.rows.map((e) => ({ i: LIB.byRef.get(e[0]), src: e[1], t: e[2] || 0, ctx: e[3] || '' }))
      .filter((p) => p.i !== undefined);
    const parts = [el('h1', { class: 'mu-h1', text: 'History' })];
    if (HIST.error) parts.push(el('div', { class: 'mu-empty', text: HIST.error }));
    if (!plays.length) {
      if (!HIST.error) {
        parts.push(el('div', { class: 'mu-empty', text: 'Nothing played yet - what you listen to is listed here.' }));
      }
      setMain(...parts);
      return;
    }
    parts.push(shelf('Played from', historyPlaces(plays)));
    const list = el('ol', { class: 'mu-tracks mu-hist' });
    let day = '';
    for (const p of plays) {
      const label = dayLabel(p.t);
      if (label !== day) {
        day = label;
        list.append(el('li', { class: 'mu-day', text: label }));
      }
      list.append(historyRow(p));
    }
    list.append(el('li', { class: 'mu-list-foot' }, HIST.more
      ? el('button', { type: 'button', class: 'mu-text-btn', text: 'Show more',
        onclick: (e) => { e.currentTarget.textContent = 'Loading…'; loadHistory({ more: true }); } })
      : `${plural(plays.length, 'play')} - everything this account still has`));
    parts.push(list);
    setMain(...parts.filter(Boolean));
    markPlaying();
  }

  // ---- search ------------------------------------------------------------------------------
  function score(text, q) {
    if (!text) return 0;
    if (text === q) return 100;
    if (text.startsWith(q)) return 80;
    if (text.includes(' ' + q)) return 60;
    if (text.includes(q)) return 40;
    return 0;
  }

  function search(qRaw) {
    const q = norm(qRaw);
    if (!q) return null;
    const words = q.split(' ');
    const songs = [];
    for (const s of LIB.songs) {
      if (!words.every((w) => s.hay.includes(w))) continue;
      const sc = Math.max(score(s.nTitle, q), score(s.nArtist, q) * 0.8, score(norm(s.album), q) * 0.6, 20);
      songs.push([sc, s.i]);
    }
    songs.sort((a, b) => b[0] - a[0] || LIB.songs[b[1]].added - LIB.songs[a[1]].added);
    const artists = [];
    for (const a of LIB.artists.values()) {
      const n = norm(a.name);
      const sc = words.every((w) => n.includes(w)) ? Math.max(score(n, q), 30) : 0;
      if (sc) artists.push([sc, a.name]);
    }
    artists.sort((a, b) => b[0] - a[0] || artistSongs(b[1]).length - artistSongs(a[1]).length);
    const albums = [];
    for (const a of LIB.albums) {
      const n = norm(`${a.title} ${a.artist}`);
      if (words.every((w) => n.includes(w))) albums.push([Math.max(score(norm(a.title), q), 30), a]);
    }
    albums.sort((a, b) => b[0] - a[0]);
    const pls = [];
    for (const p of [...LIB.mine, ...LIB.playlists]) {
      const n = norm(`${p.title} ${p.owner || ''} ${p.where || ''}`);
      if (words.every((w) => n.includes(w))) pls.push([Math.max(score(norm(p.title), q), 30), p]);
    }
    pls.sort((a, b) => b[0] - a[0]);
    const styles = [...LIB.genres.keys()].filter((g) => words.every((w) => norm(g).includes(w)));
    return { songs: songs.map((x) => x[1]), artists: artists.map((x) => x[1]), albums: albums.map((x) => x[1]),
      playlists: pls.map((x) => x[1]), styles, best: [...artists, ...pls.map(([s, p]) => [s, p]), ...songs].sort((a, b) => b[0] - a[0])[0] };
  }

  function viewSearch(q) {
    const input = $('muSearch');
    if (document.activeElement !== input) input.value = q;
    if (!q.trim()) {
      setMain(el('h1', { class: 'mu-h1', text: 'Search' }),
        el('div', { class: 'mu-empty', text: 'Type in the box above - songs, artists, albums, playlists and styles, as you type.' }));
      return;
    }
    const r = search(q);
    const parts = [el('h1', { class: 'mu-h1', text: `Results for "${q}"` })];
    const total = r.songs.length + r.artists.length + r.albums.length + r.playlists.length + r.styles.length;
    if (!total) {
      parts.push(el('div', { class: 'mu-empty', text: 'Nothing matches.' }));
      setMain(...parts);
      return;
    }
    const best = r.best;
    if (best) {
      const [, thing] = best;
      let top = null;
      if (typeof thing === 'string') {
        const list = artistSongs(thing);
        top = { href: artistHref(thing), art: list.length ? coverImg(LIB.songs[list[0]], 300) : placeholder(thing),
          title: thing, sub: `Artist • ${plural(list.length, 'song')}`, round: true, play: () => playList(list, 0) };
      } else if (typeof thing === 'number') {
        const s = LIB.songs[thing];
        top = { href: artistHref(s.artist), art: coverImg(s, 300), title: s.title, sub: `Song • ${s.artist}`,
          play: () => playList(r.songs, r.songs.indexOf(thing), { context: 'search', src: 'user' }) };
      } else if (thing && thing.songs) {
        top = { href: `#/playlist/${thing.id}`, art: mosaic(thing.songs, thing.title, 300), title: thing.title,
          sub: `Playlist • ${playlistSub(thing)}`, play: () => playList(thing.songs, 0, { context: thing.id }) };
      }
      if (top) {
        parts.push(el('a', { class: 'mu-top-result', href: top.href },
          el('div', { class: 'mu-hero-art' + (top.round ? ' round' : '') }, top.art),
          el('div', {}, el('div', { class: 'mu-hero-kind', text: 'Top result' }),
            el('div', { class: 'mu-top-result-title', text: top.title }),
            el('div', { class: 'mu-card-sub', text: top.sub }),
            el('div', { class: 'mu-actions' }, el('button', { type: 'button', class: 'ah-btn mu-primary',
              onclick: (e) => { e.preventDefault(); top.play(); } }, icon('play'), 'Play')))));
      }
    }
    if (r.songs.length) {
      parts.push(el('section', { class: 'mu-shelf' },
        el('div', { class: 'mu-shelf-head' }, el('h2', { class: 'mu-h2', text: `Songs · ${r.songs.length}` })),
        trackList(r.songs.slice(0, 500), { context: 'search',
          foot: r.songs.length > 500 ? `The first 500 of ${r.songs.length} - narrow the search for the rest.` : '' })));
    }
    parts.push(shelf(`Artists and channels · ${r.artists.length}`, r.artists.slice(0, 30).map((n) => artistCard(n, artistSongs(n)))));
    parts.push(shelf(`Albums · ${r.albums.length}`, r.albums.slice(0, 30).map(albumCard)));
    parts.push(shelf(`Playlists · ${r.playlists.length}`, r.playlists.slice(0, 30).map(playlistCard)));
    if (r.styles.length) {
      parts.push(el('section', { class: 'mu-shelf' },
        el('div', { class: 'mu-shelf-head' }, el('h2', { class: 'mu-h2', text: 'Styles' })),
        el('div', { class: 'mu-chips' }, r.styles.map((g) =>
          el('a', { href: `#/genre/${encodeURIComponent(g)}`, text: `${g} · ${LIB.genres.get(g).length}` })))));
    }
    setMain(...parts.filter(Boolean));
  }

  function notFound(text) {
    setMain(el('div', { class: 'mu-empty', text }));
  }

  // ---- router ------------------------------------------------------------------------------
  function route() {
    const h = location.hash.replace(/^#\/?/, '');
    // The query first: "#/search?q=a/b" must not be split on its slash.
    const [pathPart, query] = h.split('?');
    const [head, ...rest] = pathPart.split('/');
    if (head === 'search') {
      return { view: 'search', arg: new URLSearchParams(query || '').get('q') || '' };
    }
    let arg = rest.join('/');
    try { arg = decodeURIComponent(arg); } catch (e) { /* a hand-typed stray % */ }
    return { view: head || 'home', arg };
  }

  function render() {
    if (!LIB.songs.length && !LIB.playlists.length && !LIB.built) return;
    closeMenu();
    const r = route();
    if (r.view === 'library') viewLibrary(r.arg || 'playlists');
    else if (r.view === 'playlist') viewPlaylist(r.arg);
    else if (r.view === 'album') viewAlbum(r.arg);
    else if (r.view === 'artist') viewArtist(r.arg);
    else if (r.view === 'genre') viewGenre(r.arg);
    else if (r.view === 'history') viewHistory();
    else if (r.view === 'search') viewSearch(r.arg);
    else viewHome();
    for (const a of document.querySelectorAll('.mu-nav')) {
      const on = a.dataset.nav === r.view || (a.dataset.nav === 'home' && r.view === 'home');
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
    for (const a of document.querySelectorAll('.mu-side-list a')) {
      if (a.getAttribute('href') === location.hash) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    markPlaying();
    if (r.view !== 'search') {
      main.scrollTop = 0;
      $('muSearch').value = '';        // a search box still holding the last query reads as a filter
    }
    $('muSide').classList.remove('open');
    $('muMenuBtn').setAttribute('aria-expanded', 'false');
  }

  function renderSide() {
    const mine = $('muSideMine');
    mine.replaceChildren(...(LIB.mine.length
      ? LIB.mine.map((p) => el('li', {}, el('a', { href: `#/playlist/${p.id}`, text: p.title, title: p.title })))
      : [el('li', { class: 'mu-side-empty', text: 'None yet' })]));
    const pls = $('muSidePlaylists');
    pls.replaceChildren(...LIB.playlists.map((p) => el('li', {},
      el('a', { href: `#/playlist/${p.id}`, title: `${p.title} - ${p.owner}` }, p.title,
        el('span', { class: 'mu-side-sub', text: p.source === 'youtube' ? p.owner : (p.library || p.owner) })))));
  }

  // ---- start ------------------------------------------------------------------------------
  function start() {
    hydrateIcons(document);
    setIcon($('muMenuBtn'), 'menu');
    document.querySelector('.mu-search-icon').replaceChildren(icon('search'));
    bindPlayer();
    bindStage();
    updateShuffleButton();
    updatePlayButton();
    $('muNewPlaylist').addEventListener('click', () => createPlaylist([]));
    for (const id of ['muNowCover', 'muStageCover']) {
      $(id).addEventListener('contextmenu', (e) => { e.preventDefault(); coverMenu(e); });
    }
    // A disabled button gets no hover events, and Heights is disabled exactly when the bridge is missing, so
    // while the page has given up the pointer is checked against the two buttons' rectangles instead.
    document.addEventListener('pointermove', (e) => {
      if (!BR.stopped || BR.ws || BR.yielded) return;
      for (const id of ['muHeights', 'muSurround']) {
        const r = $(id).getBoundingClientRect();
        if (r.width && e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) { bridgeKick(); return; }
      }
    }, { passive: true });
    for (const id of ['muHeights', 'muSurround']) {
      for (const ev of ['focus', 'pointerdown']) $(id).addEventListener(ev, bridgeKick);
    }
    $('muHeights').addEventListener('click', () => setHeights(!UP.heights));
    $('muSurround').addEventListener('click', () => setUpMode(UP.mode === 'all' ? 'stereo' : 'all'));
    // Another output device became the active one: its channels decide again.
    if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
      navigator.mediaDevices.addEventListener('devicechange', () => setTimeout(() => {
        if (LOUD.ctx && UP.input) buildUpmix();
        else probeOutput();
      }, 500));
    }
    probeOutput();
    $('muTargetLufs').addEventListener('change', (e) =>
      setTargetLufs(e.target.value === 'rec' ? null : Number(e.target.value)));
    $('muMenuBtn').addEventListener('click', () => {
      const open = !$('muSide').classList.contains('open');
      $('muSide').classList.toggle('open', open);
      $('muMenuBtn').setAttribute('aria-expanded', String(open));
    });
    let typing = null;
    $('muSearch').addEventListener('input', (e) => {
      clearTimeout(typing);
      const q = e.target.value;
      typing = setTimeout(() => {
        const target = `#/search?q=${encodeURIComponent(q)}`;
        // Typing refines one search: replace, so Back leaves the search.
        if (route().view === 'search') history.replaceState(null, '', target);
        else history.pushState(null, '', target);
        render();
      }, 180);
    });
    $('muSearchForm').addEventListener('submit', (e) => {
      e.preventDefault();
      clearTimeout(typing);
      location.hash = `#/search?q=${encodeURIComponent($('muSearch').value)}`;
    });
    window.addEventListener('hashchange', render);
    // A tab brought back after a while: pick up new songs.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && LIB.built) loadLibrary({ quiet: true });
    });
    loadLibrary();
  }

  start();
})();
