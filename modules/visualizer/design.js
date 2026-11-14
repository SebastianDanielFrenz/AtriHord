// Visual design language of the music visualizer ("AtriHord Spectrum"): the colours every animation uses, and what
// each colour stands for in the music. Our own presets never pick colours freely: they draw into channels (R = low
// end, G = mids / harmony, B = highs) and the composite pass maps those channels through these colours, so every
// animation shares one palette however different its shapes are.
//
//   VisDesign.colors.blue           -> '#3987e5'
//   VisDesign.vec3('blue')          -> 'vec3(0.2235, 0.5294, 0.8980)'   (for GLSL)
//   VisDesign.roles.low             -> ['blue', 'violet']
(function () {
  'use strict';
  const colors = {
    ink: '#05060c',      // the ground: what the feedback fades back to; never pure black (no banding, no "off" look)
    night: '#0d1030',    // the deep field around the centre; may take a hint of the cover's hue
    blue: '#3987e5',     // AtriHord's accent (main.css --accent, dark theme): the low end, the kick rings
    violet: '#8a4dff',   // the low end further out / the bass body
    magenta: '#ff3fa4',  // mids and harmony: snares, chords, the lead
    cyan: '#3fe0ff',     // highs: hats, sparks, shimmer
    amber: '#ffb347',    // energy: only where low and mid hit together at high energy (drops); rare by design
    white: '#f4f7ff',    // peaks only: flash tips, capped (<= 3 per second, never full screen)
  };
  // What drives which colour (the pseudo-MIDI voices map onto these roles).
  const roles = {
    ground: ['ink', 'night'],
    low: ['blue', 'violet'],      // kick, bass
    mid: ['magenta'],             // snare / clap, chords, lead, vocals
    high: ['cyan'],               // hats, cymbals, sparks
    energy: ['amber'],            // drops, the loudest moments
    peak: ['white'],
  };
  // Moods of a song section: which roles may show and how strong. A preset reads the current mood from q-variables
  // set by the conductor (later: the pseudo-MIDI's section events).
  const moods = {
    calm: { roles: ['ground', 'low', 'high'], saturation: 0.7, energy: 0 },
    build: { roles: ['ground', 'low', 'mid', 'high'], saturation: 0.9, energy: 0.3 },
    drop: { roles: ['ground', 'low', 'mid', 'high', 'energy', 'peak'], saturation: 1, energy: 1 },
    breakdown: { roles: ['ground', 'high'], saturation: 0.5, energy: 0 },
  };
  // Shared motion rules (for every animation, ours and curated ones alike).
  const motion = {
    zoom: 'always inward: shapes are born at the centre and grow past the viewer',
    tempo: 'travel speed and symmetry changes follow the beat grid, never free-running',
    symmetry: [1, 2, 4, 6, 8],    // allowed kaleidoscope orders
    flashPerSecond: 3,            // photosensitivity cap over all layers together
    flashStep: 0.25,              // at most this much white is mixed in by one flash
  };

  // Schemes: variants with the SAME roles and lightness steps, only other hues, so every animation works with any of
  // them. "spectrum" (above) is the default. The cover is one way to choose a scheme, not the source of the colours:
  // many covers have too little colour (dark blue on black, greyscale photos), and those keep the default. A
  // colourful cover picks the scheme whose low colour is nearest its main hue, and may tint the ground (night).
  const schemes = {
    spectrum: colors,
    aurora: Object.assign({}, colors, { blue: '#2fd39a', violet: '#2f8be0', magenta: '#b15cff', cyan: '#c8ff6a', amber: '#ffd84a', night: '#07181c' }),
    ember: Object.assign({}, colors, { blue: '#ff5a1f', violet: '#d62f5c', magenta: '#ffb000', cyan: '#ffe7a3', amber: '#ffffff', night: '#1c0a0a' }),
    ice: Object.assign({}, colors, { blue: '#5b8cff', violet: '#4a5bd8', magenta: '#9fd8ff', cyan: '#e8f6ff', amber: '#7af0ff', night: '#0a1424' }),
    orchid: Object.assign({}, colors, { blue: '#c04dff', violet: '#7a3cff', magenta: '#ff5fd2', cyan: '#ffc2f0', amber: '#ffb347', night: '#160b26' }),
  };
  let current = colors;

  function toRgb(h) { return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255); }
  function hueSat(c) {
    const mx = Math.max(...c), mn = Math.min(...c), d = mx - mn, l = (mx + mn) / 2;
    if (!d) return [0, 0];
    const s = d / (1 - Math.abs(2 * l - 1));
    const h = mx === c[0] ? ((c[1] - c[2]) / d + 6) % 6 : mx === c[1] ? (c[2] - c[0]) / d + 2 : (c[0] - c[1]) / d + 4;
    return [h / 6, s];
  }
  // `cover` = an array of [r, g, b] (0..1), e.g. from MusicVisualizer.paletteFromImage. Returns the scheme name.
  function schemeForCover(cover) {
    if (!cover || !cover.length) return 'spectrum';
    // colourfulness: the strongest saturation among the cover's main colours, ignoring very dark and very light ones
    let best = null;
    for (const c of cover) {
      const l = (Math.max(...c) + Math.min(...c)) / 2;
      if (l < 0.12 || l > 0.92) continue;
      const [h, s] = hueSat(c);
      if (!best || s > best[1]) best = [h, s];
    }
    if (!best || best[1] < 0.35) return 'spectrum';          // too little colour: the default palette
    let pick = 'spectrum', dist = 9;
    for (const [name, sc] of Object.entries(schemes)) {
      const d = Math.abs(hueSat(toRgb(sc.blue))[0] - best[0]);
      const dd = Math.min(d, 1 - d);
      if (dd < dist) { dist = dd; pick = name; }
    }
    return pick;
  }
  function use(name) { current = schemes[name] || colors; return current; }
  function rgb(name) { return toRgb(current[name] || colors[name]); }
  function vec3(name) {
    return 'vec3(' + rgb(name).map((v) => v.toFixed(4)).join(', ') + ')';
  }
  window.VisDesign = {
    name: 'AtriHord Spectrum', colors, schemes, roles, moods, motion, rgb, vec3, use, schemeForCover,
    get scheme() { return Object.keys(schemes).find((k) => schemes[k] === current) || 'spectrum'; },
  };
})();
