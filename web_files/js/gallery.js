// The media gallery, shared by media.html (folder contents, search results,
// recent downloads) and public_share.html (every kind of share): justified
// rows in the manner of Immich's timeline, optional file names, video and GIF
// badges. Loaded before each page's own script, which builds the tiles and
// calls setTileAspect, fitTileToThumb, tileBadge, galleryBreak and
// scheduleGalleryLayout. Styles: .media-gallery in css/main.css.

// Tile size (Settings > Appearance): the row height the gallery aims for,
// remembered in this browser. Phones get the same proportion of it.
try {
  const rowH = Number(localStorage.getItem('mediaGalleryRowHeight'));
  if (rowH >= 80 && rowH <= 480) {
    document.documentElement.style.setProperty('--gallery-row-h', `${rowH}px`);
    document.documentElement.style.setProperty('--gallery-row-h-small', `${Math.round(rowH * 0.62)}px`);
  }
} catch (e) { /* private mode: the default size */ }

// Clamped, so a 1:4 manga page or a 5:1 panorama cannot become a sliver or a
// banner; object-fit crops the thumbnail to the clamp instead.
const TILE_AR_MIN = 0.5;
const TILE_AR_MAX = 3;

function setTileAspect(tile, w, h) {
  if (!(w > 0 && h > 0)) return false;
  const ar = Math.min(TILE_AR_MAX, Math.max(TILE_AR_MIN, w / h)).toFixed(4);
  tile.style.setProperty('--ar', ar);
  tile.dataset.ar = ar;
  return true;
}

// A file the dimensions store has not measured (downloaded since the last
// backfill) starts square and takes its thumbnail's shape once that loads.
// Thumbnails keep the source's proportions (scale=min(400,iw):-1) and are what
// is actually on screen, so they also overrule a stored size that disagrees -
// a phone video whose rotation flag swaps width and height, say.
function fitTileToThumb(tile, img) {
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  if (!(w > 0 && h > 0)) return;
  const was = parseFloat(tile.dataset.ar || '0');
  const now = Math.min(TILE_AR_MAX, Math.max(TILE_AR_MIN, w / h));
  if (!was || Math.abs(now - was) / was > 0.04) {
    setTileAspect(tile, w, h);
    scheduleGalleryLayout(tile.parentElement, true);
  }
}

function formatBadgeDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${r}` : `${m}:${r}`;
}

// Top-right corner, as in Immich: a GIF says GIF, a video its length and a
// play mark. The length is only there once the backfill has measured it.
function tileBadge(f) {
  const badge = document.createElement('div');
  badge.className = 'media-tile-badge';
  if (/\.gif$/i.test(f.name || '')) {
    badge.classList.add('is-gif');
    badge.textContent = 'GIF';
    return badge;
  }
  if (f.type !== 'video') return null;
  if (f.duration) {
    const t = document.createElement('span');
    t.textContent = formatBadgeDuration(f.duration);
    badge.appendChild(t);
  }
  badge.insertAdjacentHTML('beforeend',
    '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9.5" fill="none" '
    + 'stroke="currentColor" stroke-width="2"/><path d="M10 8.5l5.5 3.5-5.5 3.5z" fill="currentColor"/></svg>');
  return badge;
}

// Folders get their own line(s) above the files: a compact named card beside
// a row of photos would leave every mixed row ragged.
function galleryBreak() {
  const b = document.createElement('div');
  b.className = 'gallery-break';
  b.setAttribute('aria-hidden', 'true');
  return b;
}

// Justified rows, the way Immich builds them: each row is scaled so its tiles
// fill the width exactly, and a row ends wherever that lands closer to the
// target height than taking one more tile would. CSS alone (flex-grow by
// aspect ratio) can only GROW an under-full row: with 16:9 videos and wide art
// that left rows 194 to 306 px tall against a 190 px target. The CSS rules are
// still the layout for the moment before this has run.
function layoutGallery(grid, full) {
  const width = grid.clientWidth;
  if (!width) return;       // hidden - the resize observer sees its first width
  const style = getComputedStyle(grid);
  const target = parseFloat(style.getPropertyValue('--row-h')) || 190;
  const gap = parseFloat(style.columnGap) || 0;
  const tiles = Array.from(grid.children).filter((el) => el.classList.contains('media-file'));
  // Another page of results only re-lays the unfinished last row onwards.
  let start = 0;
  if (!full && grid._galleryWidth === width && grid._galleryRowStart) {
    start = Math.max(0, tiles.indexOf(grid._galleryRowStart));
  }
  grid._galleryWidth = width;

  const commit = (items, h, justified) => {
    let used = 0;
    items.forEach(([el, ar], i) => {
      // The last tile of a full row takes the rounding, less half a pixel, so
      // the row can never overflow and wrap.
      const w = justified && i === items.length - 1
        ? width - used - gap * (items.length - 1) - 0.5
        : ar * h;
      used += w;
      el.style.flex = '0 0 auto';
      el.style.width = `${w}px`;
      el.style.height = `${h}px`;
    });
  };

  let row = [];
  let sum = 0;
  let rowStart = start;
  for (let i = start; i < tiles.length; i++) {
    const ar = parseFloat(tiles[i].dataset.ar) || 1;
    row.push([tiles[i], ar]);
    sum += ar;
    if (sum * target + gap * (row.length - 1) < width) continue;
    const hWith = (width - gap * (row.length - 1)) / sum;
    const n = row.length - 1;
    const hWithout = n ? (width - gap * (n - 1)) / (sum - ar) : Infinity;
    if (n && Math.abs(hWithout - target) < Math.abs(hWith - target)) {
      commit(row.slice(0, n), hWithout, true);
      row = [[tiles[i], ar]];
      sum = ar;
      rowStart = i;
    } else {
      commit(row, hWith, true);
      row = [];
      sum = 0;
      rowStart = i + 1;
    }
  }
  if (row.length) commit(row, target, false);   // unfinished: target height, left-aligned
  grid._galleryRowStart = tiles[rowStart] || null;
}

// Coalesces the layout requests of one burst of appends. setTimeout, not
// requestAnimationFrame: a background tab never runs frame callbacks.
function scheduleGalleryLayout(grid, full) {
  if (!grid || !grid.classList || !grid.classList.contains('media-gallery')) return;
  grid._galleryFull = grid._galleryFull || !!full;
  if (grid._galleryTimer) return;
  grid._galleryTimer = setTimeout(() => {
    grid._galleryTimer = null;
    const wantFull = grid._galleryFull;
    grid._galleryFull = false;
    layoutGallery(grid, wantFull);
  }, 0);
}

// File names on the gallery tiles are optional and remembered per browser.
// Off by default: the gallery is for looking at the pictures, and a name is a
// hover away (the tile's tooltip). Folders always keep theirs - a folder icon
// on its own says nothing - and so does a tile with no picture to show.
let showTileNames = false;
try { showTileNames = localStorage.getItem('mediaShowTileNames') === '1'; } catch (e) { /* private mode */ }

function applyTileNames() {
  document.body.classList.toggle('show-tile-names', showTileNames);
  document.querySelectorAll('[data-names-toggle]').forEach((b) => {
    b.setAttribute('aria-pressed', String(showTileNames));
    b.title = showTileNames ? 'Hide file names on the tiles' : 'Show file names on the tiles';
  });
}

function initTileNames() {
  document.querySelectorAll('[data-names-toggle]').forEach((b) => {
    b.addEventListener('click', () => {
      showTileNames = !showTileNames;
      try { localStorage.setItem('mediaShowTileNames', showTileNames ? '1' : '0'); } catch (e) { /* private mode */ }
      applyTileNames();
    });
  });
  applyTileNames();
}

function initGallery() {
  initTileNames();
  // A width change - the window, a scrollbar appearing, the search panel
  // being shown - lays each gallery out again from its first row.
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const grid = entry.target;
        if (grid.clientWidth && grid.clientWidth !== grid._galleryWidth) scheduleGalleryLayout(grid, true);
      }
    });
    document.querySelectorAll('.media-gallery').forEach((grid) => observer.observe(grid));
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initGallery);
else initGallery();
