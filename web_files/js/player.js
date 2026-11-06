// Shared preview-player logic used by both the logged-in Media Library
// (media.js) and the public, no-login share viewer (public_share.js) - so
// a share link gives exactly the same viewing experience (fill-screen
// sizing, custom controls, zoom/pan) as browsing while signed in.

// Browsers only allow playback to start from a user gesture (the click or
// keypress that opened the preview), and that permission is spent by the
// time any async callback runs. Relying on the `autoplay` attribute alone
// therefore fails silently in exactly one case - a fresh page load, before
// the origin has earned an autoplay exemption - which is why a track would
// only start on the *second* click.
//
// So: start it explicitly, and if the browser still refuses, arm a one-shot
// listener that starts it on the next interaction anywhere on the page.
// Without that, the only thing that works is hunting for the play button.
function startPlayback(mediaEl) {
  const attempt = mediaEl.play();
  if (!attempt || typeof attempt.catch !== 'function') return;
  attempt.catch(() => {
    const disarm = () => {
      document.removeEventListener('pointerdown', retry, true);
      document.removeEventListener('keydown', retry, true);
      mediaEl.removeEventListener('play', disarm);
    };
    // Gone from the DOM, or replaced by the next item: nothing to resume.
    const retry = (e) => {
      // A press on the player itself is the player's: its play button and a
      // click on the picture start playback themselves. Starting it here as
      // well, on the press, made that click find it playing and pause it -
      // the first click on a player whose autoplay was refused did nothing.
      const wrap = mediaEl.closest('.media-player-wrap');
      if (wrap && e.type === 'pointerdown' && e.target instanceof Node && wrap.contains(e.target)) return;
      disarm();
      if (mediaEl.isConnected && mediaEl.paused) mediaEl.play().catch(() => {});
    };
    document.addEventListener('pointerdown', retry, true);
    document.addEventListener('keydown', retry, true);
    // Started some other way (the player's own controls): nothing left to do,
    // and a later click elsewhere must not undo a pause the viewer chose.
    mediaEl.addEventListener('play', disarm);
  });
}

// Tracks whatever's currently in the preview modal so applyMediaSizing()
// can be re-run when the viewport itself changes (window resize, entering/
// exiting fullscreen, mobile rotation) - otherwise the sizing set at load
// time just goes stale and the media stops actually filling the screen.
let currentPreviewEl = null;
let currentPreviewNativeW = 0;
let currentPreviewNativeH = 0;

// Images/videos always go fullscreen - scaled to fit the viewport exactly
// (never overflowing it, so there's never a scrollbar), always touching the
// top and bottom edges, and the left/right edges too whenever the aspect
// ratio allows it. Small media gets scaled up to help fill the screen, but
// only up to MAX_UPSCALE times its native resolution - past that it's just
// upscaled blur with no real detail, so it stays capped at that size
// instead (centered, with empty space around it) rather than blown up.
// Real (browser) fullscreen is exempt: there it always fills the screen.
const MAX_UPSCALE = 2;

function applyMediaSizing(el, nativeWidth, nativeHeight) {
  document.getElementById('mediaPreviewModal').classList.add('fullscreen-media');

  currentPreviewEl = el;
  currentPreviewNativeW = nativeWidth;
  currentPreviewNativeH = nativeHeight;

  if (document.fullscreenElement && !el._picture) {
    // Real fullscreen is uncapped - fill the screen however far that
    // scales. Clearing every inline size hands sizing over to the
    // `:fullscreen` CSS rule (width/height 100% + object-fit: contain),
    // which inline styles would otherwise override. A picture is sized as in
    // the window - which in fullscreen is the screen - so its zoom, pan and
    // upscale sizing work unchanged.
    el.style.width = '';
    el.style.height = '';
    el.style.maxWidth = '';
    el.style.maxHeight = '';
    refreshDisplayedSize();
    return;
  }

  el.style.width = '';
  el.style.height = '';
  el.style.maxWidth = Math.min(nativeWidth * MAX_UPSCALE, window.innerWidth) + 'px';
  el.style.maxHeight = Math.min(nativeHeight * MAX_UPSCALE, window.innerHeight) + 'px';
  pinUpscaledSize(el);
  refreshDisplayedSize();
}

// A live-upscaled swap (media.js) must keep the size the ORIGINAL had on
// screen. .media-preview-media has only max-width/max-height, so an image is
// shown at its own pixel size up to those caps - a 2x file doubled on screen
// (measured: 400x300 became 800x600). So the original's pixels, scaled by the
// same caps, are pinned as inline px; applyMediaSizing re-pins after a resize.
//
// With the upscaled copy there are pixels to fill the frame with, so a picture
// that - zoom included - reaches neither pair of the frame's edges grows until
// it reaches one (user, 2026-09-18). Before the copy arrives a small picture
// stays at its own size: max-width/max-height only ever shrink. Grown under a
// zoom, the layout box is the fit divided by the zoom, so what is SEEN is the
// fit and the zoom carries on from there. Pictures only, not a track's cover:
// that sits inside the player, not in the frame.
function pinUpscaledSize(el) {
  if (!el || !el.dataset || !el.dataset.upscaled || (document.fullscreenElement && !el._picture)) return;
  const nw = currentPreviewNativeW;
  const nh = currentPreviewNativeH;
  if (!nw || !nh) return;
  const maxW = parseFloat(el.style.maxWidth) || window.innerWidth;
  const maxH = parseFloat(el.style.maxHeight) || window.innerHeight;
  let scale = Math.min(1, maxW / nw, maxH / nh);
  const frame = el.parentElement;
  if (frame && frame.id === 'mediaPreviewBody' && frame.clientWidth && frame.clientHeight) {
    const zoom = el._zoomScale || 1;
    const fw = frame.clientWidth;
    const fh = frame.clientHeight;
    // Half a pixel of slack: a picture fitted by the caps can land a hair short.
    if (nw * scale * zoom < fw - 0.5 && nh * scale * zoom < fh - 0.5) {
      scale = Math.min(fw / nw, fh / nh) / zoom;
      el.style.maxWidth = `${nw * scale}px`;     // the caps would clip the grown box
      el.style.maxHeight = `${nh * scale}px`;
    }
  }
  el.style.width = `${nw * scale}px`;
  el.style.height = `${nh * scale}px`;
  refreshDisplayedSize();
}

// The preview overlay covers the viewport, but the page behind it stays
// scrollable - which leaves its scrollbar sitting alongside the media for
// no reason (and lets a stray wheel event scroll the hidden page). Lock it
// while a preview is open.
function setPageScrollLocked(locked) {
  document.body.style.overflow = locked ? 'hidden' : '';
}

// Wiping the container's innerHTML is NOT enough to stop playback. Per
// spec, removing a media element from the document only *queues* a pause
// ("await a stable state, then pause if still not in a document"), and
// that step gets skipped or lost in practice - notably around fullscreen
// transitions - leaving a detached <video> happily playing audio with
// nothing on screen. Pausing and detaching the source is what actually
// stops it and releases the stream.
function teardownMediaIn(container) {
  if (!container) return;
  for (const el of container.querySelectorAll('video, audio')) {
    try {
      el.pause();
      el.removeAttribute('src');
      el.srcObject = null;
      el.load();
    } catch (e) {
      // A media element that never got a source can throw here; nothing
      // to clean up in that case anyway.
    }
  }
}

// Clears the preview body, stopping any playback first.
function clearPreviewBody() {
  const body = document.getElementById('mediaPreviewBody');
  teardownMediaIn(body);
  if (body) {
    body.innerHTML = '';
    // The next item may be a video: its frame must not keep the picture's
    // touch handling (enableImageZoomPan).
    body.classList.remove('zoom-frame');
    body._zoomHandlers = null;
  }
  previewEnhancements = [];          // they described what was just removed
}

// -------------------------------------------------------------------
// Ambient backdrop - a track's cover art tints the whole preview
// background, so playing music doesn't sit on the same flat grey every
// other file gets.
// -------------------------------------------------------------------

function setPreviewBackground(color) {
  const modal = document.getElementById('mediaPreviewModal');
  // '' restores whatever the stylesheet specifies.
  if (modal) modal.style.backgroundColor = color || '';

  // Real fullscreen lifts the player wrapper out of the modal's box, so
  // the modal's own background is no longer on screen and the tint has to
  // travel with it. A custom property on the root is reachable from the
  // :fullscreen rule (which matches the wrapper, not the modal); removing
  // it lets that rule fall back to black for everything without cover art.
  const root = document.documentElement;
  if (color) root.style.setProperty('--preview-ambient', color);
  else root.style.removeProperty('--preview-ambient');
}

function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
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

// Averages the cover down to one colour for the backdrop. A plain average
// trends toward grey (opposing hues cancel out), which would barely look
// like a change at all - so the result gets its saturation boosted and its
// lightness pinned low: clearly tinted by the artwork, still dark enough
// for white text to sit on.
function applyAmbientBackgroundFrom(img) {
  try {
    const SIZE = 16;
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = SIZE;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, SIZE, SIZE);

    const { data } = ctx.getImageData(0, 0, SIZE, SIZE);
    let r = 0, g = 0, b = 0, n = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 128) continue;  // ignore transparent pixels
      r += data[i]; g += data[i + 1]; b += data[i + 2]; n++;
    }
    if (!n) return;

    const [h, s] = rgbToHsl(r / n, g / n, b / n);
    const sat = Math.min(1, s * 1.8);
    setPreviewBackground(`hsl(${Math.round(h * 360)}, ${Math.round(sat * 100)}%, 12%)`);
  } catch (e) {
    // getImageData throws on a canvas tainted by a cross-origin image.
    // Covers are same-origin so this shouldn't fire, but if it ever does
    // the default background is a perfectly fine fallback.
  }
}

let resizeReflowTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeReflowTimer);
  resizeReflowTimer = setTimeout(() => {
    if (currentPreviewEl) {
      applyMediaSizing(currentPreviewEl, currentPreviewNativeW, currentPreviewNativeH);
    }
  }, 100);
});

function formatPlaybackTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

// A frame-interpolated video (js/interp.js) plays a live stream that starts at
// the position it was asked for, so its currentTime counts from there and its
// duration is unknown. It sets el._timeline = {offset, duration, loop, seek(t)},
// and the control bar reads the position through these instead - showing the
// file's own time, and seeking by asking for a new stream. A looping stream
// runs on past the end of the file, so its position wraps.
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
  else el.currentTime = seconds;
}

const VOLUME_STORAGE_KEY = 'mediaPlayerVolume';

function getStoredVolume() {
  const v = parseFloat(localStorage.getItem(VOLUME_STORAGE_KEY));
  return isFinite(v) && v >= 0 && v <= 1 ? v : 1;
}

// Neither <video> nor <audio>'s native controls can be restyled, and modern
// Chrome renders the exact same plain white bar for both anyway - so this
// builds a minimal custom bar (play/pause, seek, mute) instead. For a
// video/poster-video it's only shown on hover (or while focused, for
// keyboard use), overlaid on the bottom edge like a normal video site. For
// a plain audio track with no cover to hover over, it's always visible.
// `coverEl` is for an audio track that has cover art: the <audio> element
// is hidden and that image becomes the visual surface instead. (A <video>
// with just a `poster` and no video track can't be used for this - Chrome
// ignores the poster's dimensions and shapes the element to its default
// 2:1 ratio, letterboxing the artwork inside a wrongly-shaped box.)
// `extras`: further controls for the bar, placed before the volume - the video
// settings button (js/interp.js) is one.
// Explicit fullscreen - a double-click on a video or on a song's cover - is a
// mode the viewer stays in (user, 2026-09-18): the next or previous video or
// song plays in fullscreen too, until the viewer leaves it (Esc, another
// double-click). Kept in this browser, so it also outlasts closing the preview
// and a reload. It started as songs only (mediaSongFullscreen), read once as
// the starting value.
//
// What goes fullscreen is the whole page, not the player: moving on replaces
// the player, and a fullscreened player's removal would end fullscreen and
// start it again for the next item - the screen dropping back to the window
// and the browser's "Press Esc" notice on every item. The page, because only
// what is inside the fullscreen element is drawn: the preview already covers
// the window, and with the page in fullscreen its header (title, favorite,
// info, close), the info panel, the right-click menu and the dialogs that
// menu opens are all on screen too (user, 2026-09-18). The header comes and
// goes with the mouse, like the player's bar (showPreviewChrome). Pictures
// stay in it, sized as in the window, zoom and all.
const FULLSCREEN_KEY = 'mediaPreviewFullscreen';

function fullscreenWanted() {
  try {
    const mode = localStorage.getItem(FULLSCREEN_KEY);
    return (mode === null ? localStorage.getItem('mediaSongFullscreen') : mode) === '1';
  } catch (e) {
    return false;
  }
}

function rememberFullscreen(on) {
  try {
    localStorage.setItem(FULLSCREEN_KEY, on ? '1' : '0');
  } catch (e) { /* storage off: until the page is left */ }
}

// The element that goes fullscreen for a player: the page, for a player in
// the preview; the player itself anywhere else.
function fullscreenStage(wrap) {
  return wrap.closest('#mediaPreviewBody') ? document.documentElement : wrap;
}

function playerInFullscreen(wrap) {
  const el = document.fullscreenElement;
  return !!el && (el === wrap || el.contains(wrap));
}

const previewModal = () => document.getElementById('mediaPreviewModal');
const previewOpen = () => { const m = previewModal(); return !!m && m.style.display === 'flex'; };

// Entering or leaving fullscreen while the preview is open is the viewer's
// choice - whatever is on screen, a picture included (Esc on a picture ends
// the mode too). Closing the preview ends fullscreen with the preview already
// shut (the event comes after), so that records nothing and the mode stays.
document.addEventListener('fullscreenchange', () => {
  if (!previewOpen()) return;
  const el = document.fullscreenElement;
  if (el && el !== document.documentElement) return;   // a player outside the preview
  rememberFullscreen(!!el);
  if (el) showPreviewChrome();
});

// The preview's header - title, favorite, info, close - is on screen only
// while the mouse has moved in the last CHROME_HIDE_MS (user, 2026-09-19), in
// the window and in fullscreen, whether something plays or not (CSS:
// .chrome-visible). Two exceptions keep it: the pointer resting on it (a
// button about to be clicked would otherwise vanish under it) and keyboard
// focus inside it (CSS :focus-visible).
const CHROME_HIDE_MS = 600;
// A finger cannot hover: after a tap there is no "mouse still moving" to keep
// the header or the player's bar up, and 600 ms is gone before the finger has
// travelled to a button (user, 2026-09-30: "fades away too quickly" on the
// phone). So the last input being a touch or a pen buys a longer stay.
const TOUCH_HIDE_MS = 3500;
let lastInputTouch = false;
document.addEventListener('pointerdown', (e) => { lastInputTouch = e.pointerType !== 'mouse'; }, true);
document.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse') lastInputTouch = false; }, true);
const uiHideMs = () => (lastInputTouch ? TOUCH_HIDE_MS : CHROME_HIDE_MS);
let chromeTimer = null;
let overChrome = false;
let lastPointerAt = -Infinity;       // any mouse movement or press on the page

function showPreviewChrome() {
  const modal = previewModal();
  if (!modal) return;
  modal.classList.add('chrome-visible');
  clearTimeout(chromeTimer);
  chromeTimer = setTimeout(hidePreviewChrome, uiHideMs());
}

function hidePreviewChrome() {
  const modal = previewModal();
  if (!modal) return;
  if (overChrome) {
    chromeTimer = setTimeout(hidePreviewChrome, uiHideMs());
    return;
  }
  modal.classList.remove('chrome-visible');
}

(() => {
  const modal = previewModal();
  if (!modal) return;
  const wake = () => { if (previewOpen()) showPreviewChrome(); };
  modal.addEventListener('mousemove', wake);
  modal.addEventListener('pointerdown', wake);
  const header = modal.querySelector('.modal-header');
  if (header) {
    header.addEventListener('pointerenter', () => { overChrome = true; wake(); });
    header.addEventListener('pointerleave', () => { overChrome = false; });
  }
  const touched = () => { lastPointerAt = performance.now(); };
  document.addEventListener('mousemove', touched, true);
  document.addEventListener('pointerdown', touched, true);
  // Opening: shown if the mouse was just used (the click on a thumbnail), for
  // CHROME_HIDE_MS; opened by keyboard, it waits for the mouse. Closing: gone,
  // so the next opening starts from nothing shown.
  let open = previewOpen();
  new MutationObserver(() => {
    const now = previewOpen();
    if (now === open) return;
    open = now;
    clearTimeout(chromeTimer);
    overChrome = false;
    modal.classList.remove('chrome-visible');
    if (now && performance.now() - lastPointerAt < CHROME_HIDE_MS) showPreviewChrome();
  }).observe(modal, { attributes: true, attributeFilter: ['style'] });
})();

function wrapWithCustomControls(mediaEl, { audioOnly = false, coverEl = null, extras = [] } = {}) {
  mediaEl.removeAttribute('controls');
  mediaEl.volume = getStoredVolume();

  const wrap = document.createElement('div');
  wrap.className = 'media-player-wrap'
    + (audioOnly ? ' audio-only' : '')
    + (coverEl ? ' has-cover' : '');
  if (coverEl) wrap.appendChild(coverEl);
  wrap.appendChild(mediaEl);

  // Whatever the viewer actually sees and clicks on - the media element
  // itself for video, or the cover art standing in for hidden audio.
  const visualEl = coverEl || mediaEl;

  // Single click toggles play/pause, double click goes fullscreen - delay
  // the single-click action so a second click within the window cancels it
  // instead of toggling play state twice right before entering fullscreen.
  let clickTimer = null;
  visualEl.addEventListener('click', () => {
    if (clickTimer) return;
    clickTimer = setTimeout(() => {
      clickTimer = null;
      if (mediaEl.paused) mediaEl.play(); else mediaEl.pause();
    }, 250);
  });

  if (!audioOnly) {
    visualEl.addEventListener('dblclick', () => {
      clearTimeout(clickTimer);
      clickTimer = null;
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        // A container, never the <video> element itself - Chrome swaps in
        // its own native controls overlay (and audio-only "video" content
        // gets a jarring native audio-bar UI plus a brief stutter/pause) the
        // moment a <video> is the fullscreen element, regardless of the
        // `controls` attribute. The frame, so the mode survives moving on.
        fullscreenStage(wrap).requestFullscreen().catch(() => {});
      }
    });

    // applyMediaSizing() sizes against the current viewport, which changes
    // completely on entering/leaving fullscreen - so re-run it either way
    // (it branches internally on document.fullscreenElement).
    const onFullscreenChange = () => {
      // Self-removing once this preview's gone, instead of accumulating a
      // stale document-level listener for every preview ever opened.
      if (!document.body.contains(wrap)) {
        document.removeEventListener('fullscreenchange', onFullscreenChange);
        return;
      }
      // In the preview the choice is recorded once, for the page (above); a
      // player anywhere else records its own.
      if (fullscreenStage(wrap) === wrap) rememberFullscreen(playerInFullscreen(wrap));
      if (currentPreviewEl === visualEl) {
        applyMediaSizing(visualEl, currentPreviewNativeW, currentPreviewNativeH);
      }
    };
    document.addEventListener('fullscreenchange', onFullscreenChange);

    if (fullscreenWanted()) {
      // Once the caller has put the player in the page. Moving on in
      // fullscreen finds the page still in it and asks nothing. Otherwise
      // (opening from the grid, or after a picture opened that way) browsers
      // allow this only while the click or key that got here still counts as
      // the viewer's gesture (a few seconds); else it stays windowed.
      setTimeout(() => {
        if (wrap.isConnected && !playerInFullscreen(wrap)) {
          fullscreenStage(wrap).requestFullscreen().catch(() => {});
        }
      }, 0);
    }
  }

  const bar = document.createElement('div');
  bar.className = 'custom-controls';
  bar.tabIndex = -1;

  const playBtn = document.createElement('button');
  playBtn.type = 'button';
  playBtn.className = 'custom-controls-btn';
  playBtn.textContent = '⏸';
  playBtn.addEventListener('click', () => {
    if (mediaEl.paused) mediaEl.play(); else mediaEl.pause();
  });
  mediaEl.addEventListener('play', () => { playBtn.textContent = '⏸'; });
  mediaEl.addEventListener('pause', () => { playBtn.textContent = '▶'; });

  const curTime = document.createElement('span');
  curTime.className = 'custom-controls-time';
  curTime.textContent = '0:00';

  const seek = document.createElement('input');
  seek.type = 'range';
  seek.className = 'custom-controls-seek';
  seek.min = '0';
  seek.max = '1000';
  seek.value = '0';
  let seeking = false;
  seek.addEventListener('input', () => {
    seeking = true;
    const duration = playbackDuration(mediaEl);
    if (!duration || !isFinite(duration)) return;
    const target = (seek.value / 1000) * duration;
    // A stream is re-requested only once the thumb is let go (change), not for
    // every step of a drag; the time label follows the thumb meanwhile.
    if (mediaEl._timeline) curTime.textContent = formatPlaybackTime(target);
    else mediaEl.currentTime = target;
  });
  seek.addEventListener('change', () => {
    const duration = playbackDuration(mediaEl);
    if (mediaEl._timeline && duration && isFinite(duration)) {
      seekPlayback(mediaEl, (seek.value / 1000) * duration);
    }
    seeking = false;
  });

  const durTime = document.createElement('span');
  durTime.className = 'custom-controls-time';
  durTime.textContent = '0:00';

  mediaEl.addEventListener('timeupdate', () => {
    const duration = playbackDuration(mediaEl);
    if (!seeking && duration && isFinite(duration)) {
      seek.value = String((playbackPosition(mediaEl) / duration) * 1000);
    }
    if (!seeking) curTime.textContent = formatPlaybackTime(playbackPosition(mediaEl));
  });
  mediaEl.addEventListener('loadedmetadata', () => {
    durTime.textContent = formatPlaybackTime(playbackDuration(mediaEl));
  });

  const muteBtn = document.createElement('button');
  muteBtn.type = 'button';
  muteBtn.className = 'custom-controls-btn';
  muteBtn.textContent = '\u{1F50A}';
  muteBtn.addEventListener('click', () => {
    mediaEl.muted = !mediaEl.muted;
    muteBtn.textContent = mediaEl.muted ? '\u{1F507}' : '\u{1F50A}';
  });

  const volume = document.createElement('input');
  volume.type = 'range';
  volume.className = 'custom-controls-volume';
  volume.min = '0';
  volume.max = '100';
  volume.value = String(Math.round(mediaEl.volume * 100));
  volume.addEventListener('input', () => {
    const vol = Number(volume.value) / 100;
    mediaEl.volume = vol;
    mediaEl.muted = vol === 0;
    muteBtn.textContent = mediaEl.muted ? '\u{1F507}' : '\u{1F50A}';
    localStorage.setItem(VOLUME_STORAGE_KEY, String(vol));
  });
  mediaEl.addEventListener('volumechange', () => {
    volume.value = String(Math.round((mediaEl.muted ? 0 : mediaEl.volume) * 100));
    muteBtn.textContent = mediaEl.muted ? '\u{1F507}' : '\u{1F50A}';
  });

  bar.appendChild(playBtn);
  bar.appendChild(curTime);
  bar.appendChild(seek);
  bar.appendChild(durTime);
  for (const extra of extras) bar.appendChild(extra);
  bar.appendChild(muteBtn);
  bar.appendChild(volume);
  wrap.appendChild(bar);

  if (!audioOnly) {
    // Auto-hide after inactivity, like a normal video site - hovering
    // without moving the mouse shouldn't keep the bar (or the cursor) up
    // forever.
    let hideTimer = null;
    // The bar stays while the pointer is on it or holds one of its controls
    // (a seek being dragged): hiding it there took the thumb away mid-drag.
    let overBar = false;
    let holding = false;

    const hideLater = () => {
      clearTimeout(hideTimer);
      if (mediaEl.paused || overBar || holding) return;
      hideTimer = setTimeout(() => {
        bar.classList.remove('controls-visible');
        wrap.classList.add('cursor-hidden');
      }, uiHideMs());
    };

    const showControls = () => {
      bar.classList.add('controls-visible');
      wrap.classList.remove('cursor-hidden');
      hideLater();
    };

    // Pointer events, so a finger that lifts also leaves (pointerleave follows
    // pointerup for touch); mouse events alone would leave overBar set after a
    // tap and the bar up for good.
    bar.addEventListener('pointerenter', () => { overBar = true; showControls(); });
    bar.addEventListener('pointerleave', () => { overBar = false; hideLater(); });
    bar.addEventListener('pointerdown', () => { holding = true; showControls(); });
    const letGo = () => {
      // Self-removing once this player is gone, like onFullscreenChange.
      if (!wrap.isConnected) {
        document.removeEventListener('pointerup', letGo, true);
        document.removeEventListener('pointercancel', letGo, true);
        return;
      }
      if (!holding) return;
      holding = false;
      hideLater();
    };
    // On the document: a drag can end anywhere, off the bar or the window.
    document.addEventListener('pointerup', letGo, true);
    document.addEventListener('pointercancel', letGo, true);

    wrap.addEventListener('mouseenter', showControls);
    wrap.addEventListener('mousemove', showControls);
    wrap.addEventListener('mouseleave', () => {
      if (holding) return;               // dragging the seek thumb past the edge
      clearTimeout(hideTimer);
      overBar = false;
      bar.classList.remove('controls-visible');
      wrap.classList.remove('cursor-hidden');
    });
    // Stay visible while paused (so the play button - and cursor - stay
    // reachable), then resume the normal auto-hide countdown once
    // playback resumes.
    mediaEl.addEventListener('pause', () => {
      clearTimeout(hideTimer);
      bar.classList.add('controls-visible');
      wrap.classList.remove('cursor-hidden');
    });
    // Only RESUME the countdown when the bar is already up - i.e. it was
    // paused, or the pointer is on the player. Playback starting on its own
    // is not a reason to show controls to someone whose mouse is nowhere
    // near them, and that is every arrow-key step through an album: each
    // new track fired 'play', flashed the bar, and hid it 600ms later.
    mediaEl.addEventListener('play', () => {
      if (bar.classList.contains('controls-visible')) showControls();
    });
  }

  return wrap;
}

// How to ask the server for one file's info. Set by whichever page loaded
// this module: the library passes a path and a root, a share link passes its
// token and must never be handed the file's location. Everything below is
// identical either way - the panel renders whatever rows the server chose to
// send, so omitting the location server-side is enough to hide it.
let infoRequestUrl = null;

// Fixed-width, zero-padded throughout: locale formats vary in length
// ("Nov 20, 2013" vs "May 5, 2014"), which makes a column of dates ragged
// and hard to compare at a glance. Local time, not UTC.
function formatShortDate(epochSeconds) {
  if (!epochSeconds) return '——————————';
  const d = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Formatting shared by the info panel and the grids on both pages.
// These live here rather than in media.js because the info panel
// does - the share viewer renders the same rows and needs them too.

function formatFileSize(bytes) {
  if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(2) + ' GB';
  if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toFixed(1) + ' MB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return bytes + ' B';
}
function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return null;
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
function formatBitrate(bitsPerSecond) {
  if (!bitsPerSecond) return null;
  return bitsPerSecond >= 1e6
    ? (bitsPerSecond / 1e6).toFixed(1) + ' Mbit/s'
    : Math.round(bitsPerSecond / 1e3) + ' kbit/s';
}
function formatDateTime(epochSeconds) {
  if (!epochSeconds) return '—';
  const d = new Date(epochSeconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${formatShortDate(epochSeconds)} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// -------------------------------------------------------------------
// Info panel - technical metadata for whatever is open in the preview.
// All of it lives here, for the library, share links and pools alike: the
// pages only say where the info comes from (infoRequestUrl) and call
// followPreviewWithInfoPanel / hideInfoPanel as the preview moves on.
// -------------------------------------------------------------------

let infoPanelOpen = false;
// Kept so the panel can be re-rendered without another round trip when what
// the preview shows changes (setPreviewEnhancement).
let lastInfoData = null;
// The Dimensions value on screen. Zooming, the picture's first sizing, a
// window resize and fullscreen update its text in place (refreshDisplayedSize)
// rather than rebuilding the panel, which would drop a text selection in it
// on every wheel notch.
let infoDimsCell = null;

// What the preview shows instead of the file as stored - an AI-upscaled copy
// (media.js startLiveUpscale) and, once it exists, frame-interpolated video -
// so the details panel can say so. Whoever swaps something in calls
// setPreviewEnhancement(kind, {title, rows: [[label, value], ...], actions}), or
// {note} for a one-line state such as why nothing was swapped in;
// clearPreviewBody() forgets them together with the item they described.
// actions is [{label, title, active, disabled, onClick}] - buttons under the
// rows, for changing what is shown from where it is described (the upscaler's
// "the rules picked the wrong model for this file").
let previewEnhancements = [];

function setPreviewEnhancement(kind, entry) {
  previewEnhancements = previewEnhancements.filter((e) => e.kind !== kind);
  if (entry) previewEnhancements.push({ kind, ...entry });
  if (infoPanelOpen && lastInfoData) renderInfoPanel(lastInfoData);
}

// How large the media is actually being drawn on screen right now, which
// is not its native size - applyMediaSizing scales to fit the viewport.
//
// Reported in real device pixels, not CSS pixels. getBoundingClientRect()
// returns CSS pixels, which on a display with OS scaling (e.g. Windows at
// 150%) are fewer than the pixels actually painted - so an image drawn at
// 1440 real pixels would read as 960 and look downscaled next to its
// 1366px native width when it is in fact being upscaled.
function displayedMediaSize() {
  if (!currentPreviewEl) return null;
  const rect = currentPreviewEl.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const dpr = window.devicePixelRatio || 1;
  return `${Math.round(rect.width * dpr)} × ${Math.round(rect.height * dpr)}`;
}

// Native size, then how large it's actually being drawn right now - zoom
// included - so you can see at a glance whether the preview is showing it
// upscaled (see applyMediaSizing and its MAX_UPSCALE cap).
function formatDimensions(info) {
  if (!info.width || !info.height) return null;
  const shown = displayedMediaSize();
  return `${info.width} × ${info.height}` + (shown ? `  (${shown} displayed)` : '');
}

// Called whenever the drawn size may have changed: every zoom step
// (enableImageZoomPan's setScale) and every applyMediaSizing, which runs when
// the picture has loaded, after a window resize and on entering or leaving
// fullscreen.
function refreshDisplayedSize() {
  if (!infoPanelOpen || !lastInfoData || !infoDimsCell || !infoDimsCell.isConnected) return;
  const text = formatDimensions(lastInfoData);
  if (text && infoDimsCell.textContent !== text) infoDimsCell.textContent = text;
}

function hideInfoPanel() {
  infoPanelOpen = false;
  lastInfoData = null;
  infoDimsCell = null;
  const panel = document.getElementById('mediaInfoPanel');
  if (panel) panel.style.display = 'none';
}

// The preview moved on to another item: an open panel stays open and is
// repopulated for the item now on screen.
function followPreviewWithInfoPanel() {
  if (!infoPanelOpen) return;
  infoPanelOpen = false;
  setTimeout(toggleInfoPanel, 0);
}

async function toggleInfoPanel() {
  const panel = document.getElementById('mediaInfoPanel');
  if (!panel) return;

  if (infoPanelOpen) {
    hideInfoPanel();
    return;
  }

  const f = activePreviewList[currentPreviewIndex];
  if (!f) return;

  infoPanelOpen = true;
  panel.style.display = 'block';
  panel.innerHTML = '<div class="media-loading">Loading...</div>';

  let res;
  try {
    res = await fetch(infoRequestUrl(f));
  } catch (e) {
    panel.innerHTML = '<div class="media-error">Could not load file info.</div>';
    return;
  }
  if (!res.ok) {
    panel.innerHTML = '<div class="media-error">Could not load file info.</div>';
    return;
  }
  renderInfoPanel(await res.json());
}

// The AI index's content level and what decided it, strongest first - e.g.
// "Explicit - rating questionable 72%, tag nipples 81%". Only pictures and
// videos carry the key, so anything else leaves the row out.
function formatContentLevel(content) {
  if (content === undefined) return null;
  if (!content || !content.bucket) return 'not rated yet';
  const names = { safe: 'Safe', suggestive: 'Suggestive', explicit: 'Explicit' };
  const why = (content.evidence || []).slice(0, 3).map((e) => (
    `${e.kind === 'rating' ? 'rating' : 'tag'} ${String(e.tag).replace(/_/g, ' ')} ${Math.round((e.p || 0) * 100)}%`));
  return (names[content.bucket] || content.bucket) + (why.length ? ` - ${why.join(', ')}` : '');
}

// What the genre pass heard (media_indexer.store_genre): Discogs styles with
// their genre, strongest first - "Electronic › Techno 80%, Electronic › House 34%".
function formatAiGenres(g) {
  if (!g) return null;
  if (!g.done) return 'not analysed yet';
  if (!g.styles || !g.styles.length) return 'nothing recognised';
  return g.styles.slice(0, 4)
    .map(([label, p]) => `${String(label).replace('---', ' › ')} ${Math.round((p || 0) * 100)}%`)
    .join(', ');
}

function newTabLink(href, text) {
  const a = document.createElement('a');
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.textContent = text;
  // The panel sits inside the preview, which closes on a click outside it.
  a.addEventListener('click', (e) => e.stopPropagation());
  return a;
}

function renderInfoPanel(info) {
  lastInfoData = info;
  infoDimsCell = null;
  const panel = document.getElementById('mediaInfoPanel');
  const dims = formatDimensions(info);

  const rows = [
    ['Name', info.name],
    ['Artist', info.source ? `${info.source.artist_name} (${info.source.artist_id})`.replace(/^(@\S+) \(\1\)$/, '$1') : null],
    ['Folder', `${info.root_name || ''}${info.folder ? ' / ' + info.folder : ''}`],
    ['Type', info.type],
    ['Content', formatContentLevel(info.content)],
    ['Size', formatFileSize(info.size)],
    ['Dimensions', dims],
    ['Duration', formatDuration(info.duration)],
    ['Video codec', info.video_codec],
    ['Frame rate', info.fps ? info.fps + ' fps' : null],
    ['Audio codec', info.audio_codec],
    ['Channels', info.channels],
    ['Sample rate', info.sample_rate ? (info.sample_rate / 1000).toFixed(1) + ' kHz' : null],
    ['Bitrate', formatBitrate(info.bit_rate)],
    ['Container', info.container],
    ['Title', info.title],
    ['Artist', info.source ? null : info.artist],
    ['Album', info.album],
    ['Category', info.genre],
    ['Style (AI)', formatAiGenres(info.ai_genres)],
    ['Created', formatDateTime(info.modified)],
    ['Downloaded', formatDateTime(info.added)],
    ['Video ID', info.source_id],
    ['Tags', (info.tags && info.tags.length) ? info.tags.join(', ') : 'none yet'],
  ].filter(([, value]) => value !== null && value !== undefined && value !== '');

  panel.innerHTML = '';
  // First: it is why what is on screen differs from the file described below.
  for (const enhancement of previewEnhancements) {
    if (enhancement.note) {
      const note = document.createElement('p');
      note.className = 'media-info-note';
      note.textContent = enhancement.note;
      panel.appendChild(note);
      continue;
    }
    const section = document.createElement('div');
    section.className = 'media-info-enhanced';
    const title = document.createElement('div');
    title.className = 'media-info-enhanced-title';
    title.textContent = enhancement.title;
    const list = document.createElement('dl');
    list.className = 'media-info-list';
    for (const [label, value] of enhancement.rows || []) {
      if (value === null || value === undefined || value === '') continue;
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = String(value);
      list.append(dt, dd);
    }
    section.append(title, list);
    for (const action of enhancement.actions || []) {
      if (!action || !action.label) continue;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'media-info-action';
      btn.textContent = action.label;
      if (action.title) btn.title = action.title;
      if (action.active) btn.setAttribute('aria-pressed', 'true');
      btn.disabled = !!action.disabled;
      // The panel sits inside the preview, which closes on a click outside it.
      btn.addEventListener('click', (e) => { e.stopPropagation(); action.onClick(); });
      section.appendChild(btn);
    }
    panel.appendChild(section);
  }
  const table = document.createElement('dl');
  table.className = 'media-info-list';
  for (const [label, value] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    // The video id is the one row worth being a link: it is the whole
    // reason the id is kept, and retyping 11 case-sensitive characters by
    // hand is exactly the chore having it here is supposed to remove.
    // Twitter and pixiv: the name opens the post, the account id the artist.
    const source = info.source;
    if (label === 'Name' && source && source.post_url) {
      dd.appendChild(newTabLink(source.post_url, String(value)));
    } else if (label === 'Artist' && source) {
      if (source.site === 'twitter' || source.site === 'instagram') {
        dd.appendChild(newTabLink(source.artist_url, source.artist_id));
      } else {
        dd.append(source.artist_name + ' (', newTabLink(source.artist_url, source.artist_id), ')');
      }
    } else if (label === 'Folder' && info.folder && typeof loadMedia === 'function') {
      // The library page only (the share viewer is never sent a folder):
      // jump to the folder the file sits in - for a download, its artist.
      const a = document.createElement('a');
      a.href = mediaUrlFor(info.folder, info.root || '');
      a.textContent = String(value);
      a.addEventListener('click', (e) => {
        e.stopPropagation();
        if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        // The preview's own history entry becomes the folder, so Back still
        // returns to wherever the preview was opened from.
        closeMediaPreview(true);
        if (searchModeActive) exitSearchMode({ keepEntry: true });
        history.replaceState({ mediaPath: info.folder, mediaRoot: info.root || '' }, '', a.href);
        loadMedia(info.folder, false, info.root || '');
      });
      dd.appendChild(a);
    } else if (label === 'Video ID' && info.source_url) {
      const a = document.createElement('a');
      a.href = info.source_url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = String(value);
      dd.appendChild(a);
    } else if (label === 'Style (AI)' && info.ai_genres && info.ai_genres.on_map) {
      // Where the track sits among the rest of the library's music.
      const a = document.createElement('a');
      a.href = `/genres.html?root=${encodeURIComponent(info.root || '')}&path=${encodeURIComponent(info.path || '')}`;
      a.textContent = 'show on the genre map';
      dd.append(String(value), ' · ', a);
    } else {
      dd.textContent = String(value);
    }
    if (label === 'Dimensions') infoDimsCell = dd;
    table.appendChild(dt);
    table.appendChild(dd);
  }
  panel.appendChild(table);
}

// The panel's button, the same on every page that has the preview.
document.getElementById('mediaInfoBtn')?.addEventListener('click', (e) => {
  e.stopPropagation();
  toggleInfoPanel();
});

// Double-click toggles a zoomed-in state; while zoomed, the scroll wheel
// adjusts the zoom level further and dragging pans around the image. Uses
// CSS transform (translate + scale) rather than resizing the element, so
// applyMediaSizing()'s own sizing/fullscreen frame is untouched underneath.
//
// Fingers and pens get what a phone's own gallery does: pinch to zoom about the
// fingers, drag to pan once zoomed, a flick that coasts to a stop, double-tap
// to zoom in and back. On pointer events rather than touch events, so a harness
// can drive them without touch hardware (the review deck does the same). They
// are bound to the picture's FRAME, not the picture: a pinch that starts on the
// bars beside a landscape photo would otherwise zoom the whole page. The mouse
// keeps its own handlers, unchanged.
function enableImageZoomPan(img) {
  // Called for pictures only, before they are sized: applyMediaSizing and
  // pinUpscaledSize size them as in the window, fullscreen or not.
  img._picture = true;
  const MIN_SCALE = 1;
  const MAX_SCALE = 8;
  const DOUBLE_CLICK_SCALE = 2.5;
  // A tap moves less than TAP_SLOP px and lasts under TAP_MS; a second tap
  // within DOUBLE_TAP_MS and DOUBLE_TAP_PX of the first makes a double-tap.
  const TAP_SLOP = 10;
  const TAP_MS = 250;
  const DOUBLE_TAP_MS = 300;
  const DOUBLE_TAP_PX = 30;
  // A flick keeps the speed the finger had over its last 100 ms and slows by
  // FLING_FRICTION per 16 ms; under FLING_MIN_SPEED px/ms it is not a flick.
  const FLING_FRICTION = 0.92;
  const FLING_MIN_SPEED = 0.05;

  let scale = 1;
  let panX = 0;
  let panY = 0;
  // A double-tap is answered by the touch code; the dblclick the browser may
  // make of the same two taps must not toggle it straight back.
  let ignoreDblclickUntil = 0;
  let fling = 0;             // requestAnimationFrame id while a flick coasts
  let samples = [];          // [time, x, y] of the panning finger, for a flick's speed

  function stopFling() {
    if (fling) cancelAnimationFrame(fling);
    fling = 0;
  }

  function startFling() {
    const now = performance.now();
    const recent = samples.filter((s) => now - s[0] <= 100);
    if (scale === MIN_SCALE || recent.length < 2) return;
    const first = recent[0];
    const lastSample = recent[recent.length - 1];
    const dt = Math.max(1, lastSample[0] - first[0]);
    let vx = (lastSample[1] - first[1]) / dt;
    let vy = (lastSample[2] - first[2]) / dt;
    if (Math.hypot(vx, vy) < FLING_MIN_SPEED) return;
    let last = now;
    const step = (t) => {
      const elapsed = Math.min(64, Math.max(0, t - last));
      last = Math.max(last, t);
      if (!img.isConnected) {
        fling = 0;
        return;
      }
      if (elapsed > 0) {
        const wantX = panX + vx * elapsed;
        const wantY = panY + vy * elapsed;
        panX = wantX;
        panY = wantY;
        clampPan();
        if (panX !== wantX) vx = 0;          // an edge stops that direction
        if (panY !== wantY) vy = 0;
        applyTransform();
        const decay = Math.pow(FLING_FRICTION, elapsed / 16);
        vx *= decay;
        vy *= decay;
      }
      fling = Math.hypot(vx, vy) >= FLING_MIN_SPEED / 4 ? requestAnimationFrame(step) : 0;
    };
    fling = requestAnimationFrame(step);
  }

  function clamp(val, min, max) {
    return Math.min(max, Math.max(min, val));
  }

  // An axis the zoomed picture still fits stays centred; one it overfills may
  // pan until an edge reaches the frame. The point under the cursor therefore
  // slides while a portrait picture is narrower than a wide frame - measured
  // at most 40 px per wheel notch. Letting the picture move inside the frame
  // instead was tried and measured worse: when the picture reached the frame's
  // width that room vanished in one notch and pulled it back 95 px at once.
  function clampPan() {
    const container = img.parentElement;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const overflowX = Math.max(0, (img.offsetWidth * scale - rect.width) / 2);
    const overflowY = Math.max(0, (img.offsetHeight * scale - rect.height) / 2);
    panX = clamp(panX, -overflowX, overflowX);
    panY = clamp(panY, -overflowY, overflowY);
  }

  function applyTransform() {
    img.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
    img._zoomScale = scale;          // for pinUpscaledSize: is the frame filled yet?
  }

  // Rescales while keeping whatever's under (clientX, clientY) visually
  // fixed in place, so zooming feels anchored to the cursor instead of
  // always zooming toward the image's center.
  function setScale(newScaleRaw, clientX, clientY) {
    const newScale = clamp(newScaleRaw, MIN_SCALE, MAX_SCALE);
    if (newScale === scale) return;

    if (newScale === MIN_SCALE) {
      panX = 0;
      panY = 0;
    } else if (typeof clientX === 'number') {
      // getBoundingClientRect() reports the *transformed* box, so its
      // centre is already the panned one - the offset measured from it is
      // the distance from the cursor to where the image sits right now.
      // Holding the pixel under the cursor still then means growing that
      // offset by exactly the scale change: pan += offset * (1 - ratio).
      // (Scaling the old pan by the ratio as well, as this did before,
      // double-counts it and makes every zoom after the first drift.)
      const rect = img.getBoundingClientRect();
      const cursorOffsetX = clientX - (rect.left + rect.width / 2);
      const cursorOffsetY = clientY - (rect.top + rect.height / 2);
      const ratio = newScale / scale;
      panX += cursorOffsetX * (1 - ratio);
      panY += cursorOffsetY * (1 - ratio);
    }

    scale = newScale;
    img.classList.toggle('zoomed', scale > MIN_SCALE);
    if (scale > MIN_SCALE) clampPan();
    applyTransform();
    // The details panel's "displayed" size includes the zoom.
    if (img === currentPreviewEl) refreshDisplayedSize();
  }

  img.classList.add('zoomable');

  img.addEventListener('dblclick', (e) => {
    e.preventDefault();
    if (performance.now() < ignoreDblclickUntil) return;
    stopFling();
    if (scale > MIN_SCALE) {
      setScale(MIN_SCALE);
    } else {
      setScale(DOUBLE_CLICK_SCALE, e.clientX, e.clientY);
    }
  });

  img.addEventListener('wheel', (e) => {
    // Sideways scrolling is not a zoom (a deltaY of 0 used to count as
    // "not up" and zoomed OUT).
    if (!e.deltaY) return;
    // Zoom by how far the wheel turned, not a fixed step per event. A mouse
    // notch is ~100 px and still zooms ~16%; a trackpad or smooth-scrolling
    // wheel sends dozens of small events, and at 15% EACH they flung the
    // picture about (measured: 40 events of 4 px went straight to 8x).
    const px = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? window.innerHeight : 1);
    if (scale === MIN_SCALE && px > 0) return;
    e.preventDefault();
    stopFling();
    setScale(scale * Math.pow(1.0015, -clamp(px, -300, 300)), e.clientX, e.clientY);
  }, { passive: false });

  img.addEventListener('mousedown', (e) => {
    // Left button only: a middle or right press started a drag that then
    // followed the mouse around.
    if (e.button !== 0 || scale === MIN_SCALE) return;
    e.preventDefault();
    stopFling();

    // Moved by how far the mouse went since the LAST event, as the touch pan
    // is - not "pan at the press + distance travelled". A smooth-scrolling
    // wheel or a touchpad keeps sending small zoom steps for a moment after
    // the last notch; each re-anchors the picture at the cursor, and the next
    // move threw that away, so the picture slid back towards where the drag
    // alone would have put it (user, 2026-09-22; measured: ten trailing steps
    // left the grabbed point 26 px off the cursor, now 0).
    let lastX = e.clientX;
    let lastY = e.clientY;
    img.classList.add('dragging');

    const onMove = (moveEvent) => {
      panX += moveEvent.clientX - lastX;
      panY += moveEvent.clientY - lastY;
      lastX = moveEvent.clientX;
      lastY = moveEvent.clientY;
      clampPan();
      applyTransform();
    };
    const onUp = () => {
      img.classList.remove('dragging');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // ------------------------------------------------------------- touch
  const points = new Map();  // the fingers on the glass, by pointer id
  let pinch = null;          // two fingers: their distance and the scale when they landed, and the last midpoint
  let pan = null;            // one finger: its id and where it last was
  let tap = null;            // a touch that may still turn out to be a tap
  let lastTap = null;        // the last tap, waiting for a second one

  function midpoint() {
    const [a, b] = [...points.values()];
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, dist: Math.hypot(a.x - b.x, a.y - b.y) };
  }

  // Whatever the fingers are doing now: two pinch, one pans from where it is -
  // so lifting one of two fingers carries on as a pan.
  function beginGesture() {
    samples = [];
    pinch = null;
    pan = null;
    if (points.size >= 2) {
      const m = midpoint();
      pinch = { dist: m.dist || 1, scale, x: m.x, y: m.y };
    } else if (points.size === 1) {
      const [id, p] = [...points.entries()][0];
      pan = { id, x: p.x, y: p.y };
    }
  }

  function onPointerDown(e) {
    if (e.pointerType === 'mouse') return;       // the mouse has its own handlers above
    e.preventDefault();                          // and a touch makes no emulated mouse events
    stopFling();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch (err) { /* a synthetic pointer has nothing to capture */ }
    points.set(e.pointerId, { x: e.clientX, y: e.clientY });
    tap = points.size === 1 ? { id: e.pointerId, x: e.clientX, y: e.clientY, t: performance.now() } : null;
    beginGesture();
  }

  function onPointerMove(e) {
    const p = points.get(e.pointerId);
    if (!p) return;
    p.x = e.clientX;
    p.y = e.clientY;
    if (tap && tap.id === e.pointerId && Math.hypot(p.x - tap.x, p.y - tap.y) > TAP_SLOP) tap = null;
    if (pinch && points.size >= 2) {
      const m = midpoint();
      // Scaled about the fingers, then moved along with them.
      setScale(pinch.scale * (m.dist / pinch.dist), m.x, m.y);
      if (scale > MIN_SCALE) {
        panX += m.x - pinch.x;
        panY += m.y - pinch.y;
        clampPan();
        applyTransform();
      }
      pinch.x = m.x;
      pinch.y = m.y;
    } else if (pan && pan.id === e.pointerId) {
      if (scale > MIN_SCALE) {
        panX += p.x - pan.x;
        panY += p.y - pan.y;
        clampPan();
        applyTransform();
        samples.push([performance.now(), p.x, p.y]);
        if (samples.length > 12) samples.shift();
      }
      pan.x = p.x;
      pan.y = p.y;
    }
  }

  function onPointerUp(e) {
    if (!points.has(e.pointerId)) return;
    points.delete(e.pointerId);
    const now = performance.now();
    const panning = e.type === 'pointerup' && pan && pan.id === e.pointerId;
    if (e.type === 'pointerup' && tap && tap.id === e.pointerId && points.size === 0 && now - tap.t <= TAP_MS) {
      if (lastTap && now - lastTap.t <= DOUBLE_TAP_MS
          && Math.hypot(tap.x - lastTap.x, tap.y - lastTap.y) <= DOUBLE_TAP_PX) {
        lastTap = null;
        ignoreDblclickUntil = now + 500;
        if (scale > MIN_SCALE) setScale(MIN_SCALE);
        else setScale(DOUBLE_CLICK_SCALE, tap.x, tap.y);
      } else {
        lastTap = { x: tap.x, y: tap.y, t: now };
      }
    }
    tap = null;
    if (panning && points.size === 0) startFling();
    beginGesture();
  }

  // The frame is the picture's parent, and the preview builds the picture
  // before appending it - so this runs once it is on the page. One set of
  // listeners per frame, handing each event to the picture on show now.
  function bindFrame() {
    const frame = img.parentElement;
    if (!frame || !img.isConnected) return;
    frame.classList.add('zoom-frame');
    frame._zoomHandlers = { down: onPointerDown, move: onPointerMove, up: onPointerUp };
    if (frame._zoomBound) return;
    frame._zoomBound = true;
    const hand = (name) => (e) => {
      if (frame._zoomHandlers) frame._zoomHandlers[name](e);
    };
    frame.addEventListener('pointerdown', hand('down'));
    frame.addEventListener('pointermove', hand('move'));
    frame.addEventListener('pointerup', hand('up'));
    frame.addEventListener('pointercancel', hand('up'));
    // Safari zooms the page on its own pinch events whatever touch-action says.
    frame.addEventListener('gesturestart', (e) => e.preventDefault());
  }
  queueMicrotask(bindFrame);
  img.addEventListener('load', bindFrame);
}
