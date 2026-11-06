/* The geometry of the tile wall (P8 V1), pure: no DOM, no three.js. Unit-tested in tests/ui/stereo3d_harness.js.
 *
 * The wall is a section of a cylinder around the viewer. Positions on it are angles: az = azimuth in
 * degrees (0 straight ahead, positive to the right), and vertically a distance "down from the top of the
 * wall" in degrees of arc, which the renderer turns into metres (y = R * rad) so that angular sizes stay
 * the same when the radius setting changes.
 *
 * Tiles are laid out in justified rows like the site's gallery: every tile keeps its (clamped) aspect, a row
 * is scaled so it fills the span exactly, so rows differ a little in height. Layout only needs one aspect
 * number per tile, so a 2000-file folder costs microseconds. */

export const WALL = {
  rowDeg: 9,            // nominal row height, degrees of arc (about 180 panel px, 0.31 m at R = 2 m)
  gapDeg: 1.5,
  minAspect: 0.5,       // tile width / height is clamped to this range (the full picture shows on open)
  maxAspect: 2.2,
  maxStretch: 1.25,     // an unfinished last row is stretched to the span only up to this factor
  minScale: 0.7,        // a row is never squeezed below this factor of the nominal height
  bandTopDeg: 20,       // the rows live between these elevations (looking down is more comfortable)
  bandBottomDeg: -35,
  folderAspect: 1.6,
  SPANS: [90, 120, 150, 180],
};

const finite = (v) => typeof v === "number" && Number.isFinite(v);
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function tileAspect(a, o = WALL) {
  return clamp(finite(a) && a > 0 ? a : 1, o.minAspect, o.maxAspect);
}

/* layoutRows(aspects, spanDeg, opts) -> layout
 *   aspects  array-like of width/height per tile (raw, clamped here)
 *   layout = {
 *     n, spanDeg, totalDeg,                       // totalDeg = height of all rows plus gaps
 *     az:   Float32Array(n)   tile centre azimuth (deg)
 *     w:    Float32Array(n)   tile width (deg)
 *     h:    Float32Array(n)   tile height (deg)
 *     row:  Int32Array(n)
 *     rowStart: Int32Array(rows+1)  first tile of each row (last entry = n)
 *     rowTop:   Float32Array(rows)  distance from the wall top to the row's top (deg)
 *     rowH:     Float32Array(rows)  row height (deg)
 *     rows
 *   } */
export function layoutRows(aspects, spanDeg, opts = {}) {
  const o = Object.assign({}, WALL, opts);
  const n = aspects ? aspects.length : 0;
  const span = Math.max(10, finite(spanDeg) ? spanDeg : 120);
  const az = new Float32Array(n), w = new Float32Array(n), h = new Float32Array(n), row = new Int32Array(n);
  const starts = [], tops = [], heights = [];
  const nat = new Float64Array(n);
  for (let i = 0; i < n; i += 1) nat[i] = Math.min(span, o.rowDeg * tileAspect(aspects[i], o));
  let i = 0, y = 0;
  while (i < n) {
    let sum = nat[i], cnt = 1, j = i + 1;
    // add tiles while they fit; at the first one that does not, take the choice nearer to the span
    while (j < n) {
      const withNext = sum + nat[j] + o.gapDeg * cnt;
      if (withNext <= span) { sum += nat[j]; cnt += 1; j += 1; continue; }
      const kOut = (span - o.gapDeg * (cnt - 1)) / sum;
      const kIn = (span - o.gapDeg * cnt) / (sum + nat[j]);
      if (kIn >= o.minScale && Math.abs(Math.log(kIn)) < Math.abs(Math.log(kOut))) { sum += nat[j]; cnt += 1; j += 1; }
      break;
    }
    const last = j >= n;
    let k = (span - o.gapDeg * (cnt - 1)) / sum;
    if (last && k > o.maxStretch) k = 1;           // a short last row stays at natural size, left aligned
    k = Math.max(o.minScale * 0.5, k);
    const rowH = o.rowDeg * k;
    const total = sum * k + o.gapDeg * (cnt - 1);
    let x = -span / 2 + (last && k === 1 && total < span ? 0 : (span - total) / 2);
    const r = starts.length;
    starts.push(i); tops.push(y); heights.push(rowH);
    for (let t = i; t < j; t += 1) {
      const wt = nat[t] * k;
      w[t] = wt; h[t] = rowH; az[t] = x + wt / 2; row[t] = r;
      x += wt + o.gapDeg;
    }
    y += rowH + o.gapDeg;
    i = j;
  }
  starts.push(n);
  const rows = tops.length;
  return {
    n, spanDeg: span, rows, totalDeg: rows ? y - o.gapDeg : 0,
    az, w, h, row,
    rowStart: Int32Array.from(starts), rowTop: Float32Array.from(tops), rowH: Float32Array.from(heights),
  };
}

// Height of the band the rows are visible in.
export const bandDeg = (o = WALL) => o.bandTopDeg - o.bandBottomDeg;

// The largest scroll offset (deg): the end of the last row at the bottom of the band.
export function maxOffset(layout, o = WALL) {
  return Math.max(0, layout.totalDeg - bandDeg(o));
}

// Index of the row containing `pos` (deg from the wall top); clamped into [0, rows-1]; -1 when there are no rows.
export function rowAt(layout, pos) {
  const r = layout.rows;
  if (!r) return -1;
  let lo = 0, hi = r - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (layout.rowTop[mid] <= pos) lo = mid; else hi = mid - 1;
  }
  return lo;
}

/* The rows that matter for a scroll offset: those intersecting the band, widened by `margin` rows on both
 * sides (the virtualisation window: meshes exist only for these). { r0, r1 } inclusive, r0 > r1 when empty. */
export function visibleRows(layout, offsetDeg, margin = 3, o = WALL) {
  if (!layout.rows) return { r0: 0, r1: -1 };
  const first = rowAt(layout, offsetDeg);
  const last = rowAt(layout, offsetDeg + bandDeg(o));
  return { r0: Math.max(0, first - margin), r1: Math.min(layout.rows - 1, last + margin) };
}

// Elevation (deg) of the centre of row r at a scroll offset.
export function rowElevation(layout, r, offsetDeg, o = WALL) {
  return o.bandTopDeg - (layout.rowTop[r] + layout.rowH[r] / 2 - offsetDeg);
}

/* Which tile is at (azDeg, elevDeg)? -1 for none. Gaps count as none. */
export function hitTile(layout, azDeg, elevDeg, offsetDeg, o = WALL) {
  if (!layout.rows || !finite(azDeg) || !finite(elevDeg)) return -1;
  const pos = o.bandTopDeg - elevDeg + offsetDeg;          // deg from the wall top
  if (pos < 0) return -1;
  const r = rowAt(layout, pos);
  if (r < 0 || pos > layout.rowTop[r] + layout.rowH[r]) return -1;
  let lo = layout.rowStart[r], hi = layout.rowStart[r + 1] - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (layout.az[mid] - layout.w[mid] / 2 <= azDeg) lo = mid; else hi = mid - 1;
  }
  return azDeg >= layout.az[lo] - layout.w[lo] / 2 && azDeg <= layout.az[lo] + layout.w[lo] / 2 ? lo : -1;
}

// The scroll offset that puts tile `index` at the middle of the band (clamped).
export function offsetForTile(layout, index, o = WALL) {
  if (!layout.n || index < 0 || index >= layout.n) return 0;
  const r = layout.row[index];
  const mid = layout.rowTop[r] + layout.rowH[r] / 2;
  return clamp(mid - bandDeg(o) / 2, 0, maxOffset(layout, o));
}

// The first tile index in the top row that is (at least half) inside the band, for "where was I" state.
export function firstVisibleTile(layout, offsetDeg) {
  if (!layout.rows) return 0;
  const r = rowAt(layout, offsetDeg + 0.5);
  return layout.rowStart[r];
}

/* Metres: where a tile centre sits on the cylinder of radius R, and how big it is, for the renderer.
 * out = {x, y, z, yaw, tilt, width, height}; y = R * elevation (rad) so the wall is a strip unrolled
 * vertically. The tilt turns the lower and upper rows towards the viewer. */
export function tilePlacement(layout, i, offsetDeg, radius, out, o = WALL) {
  const r = layout.row[i];
  const elev = rowElevation(layout, r, offsetDeg, o);
  const az = (layout.az[i] * Math.PI) / 180;
  const R = radius;
  out.x = R * Math.sin(az);
  out.z = -R * Math.cos(az);
  out.y = (R * elev * Math.PI) / 180;
  out.yaw = -az;
  out.tilt = Math.atan2(out.y, R) * 0.85;
  out.width = (R * layout.w[i] * Math.PI) / 180;
  out.height = (R * layout.h[i] * Math.PI) / 180;
  return out;
}

/* A ray (origin, direction, in the wall's own frame: viewer at the middle, y up, -z ahead) against the
 * cylinder of radius R seen from inside. Returns the distance along the ray, or -1. out gets
 * {az (deg), elev (deg), x, y, z} of the hit. */
export function rayCylinder(ox, oy, oz, dx, dy, dz, R, out) {
  const a = dx * dx + dz * dz;
  if (a < 1e-12) return -1;
  const b = ox * dx + oz * dz;
  const c = ox * ox + oz * oz - R * R;
  const disc = b * b - a * c;
  if (disc < 0) return -1;
  const t = (-b + Math.sqrt(disc)) / a;
  if (!(t > 0)) return -1;
  const x = ox + t * dx, y = oy + t * dy, z = oz + t * dz;
  out.x = x; out.y = y; out.z = z;
  out.az = (Math.atan2(x, -z) * 180) / Math.PI;
  out.elev = ((y / R) * 180) / Math.PI;
  return t;
}
