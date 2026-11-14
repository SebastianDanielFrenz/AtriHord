// The AtriHord scene set: our own Butterchurn (MilkDrop) presets in the AtriHord Spectrum design language
// (vis/design.js), meant to play one after another (vis/conductor.js changes them on phrases, drops or a trigger).
// VisPresets.set(VisDesign) -> [{ name, preset }]; a scheme change = build again and load with a blend.
//
// What every scene shares:
//  - Everything is drawn into CHANNELS, not colours: red = low end (kick, bass, the main shape), green = mids and
//    harmony, blue = highs. The composite pass tone maps each channel on its own and maps it through the design's
//    colours, so a loud bass layer cannot drown the others and all three roles are always on screen.
//  - Every channel has a CONTINUOUS layer driven by its band level, plus accents on onsets (kick / snare / hat), so
//    music without a clear snare or hats still shows all colours.
//  - The main shape is a superformula loop (red): its lobe count morphs to a new value every 8 kicks, kicks make it
//    spikier, bigger and give it a turn (direction flips every 4 kicks), the bass swells it, the waveform roughens
//    its edge. The feedback pass carries every frame of it outward into a tunnel of its own past.
//  - The composite pass folds the picture into a kaleidoscope whose order changes every 16 kicks, adds glow, lays
//    the ground (night -> ink) under it, caps the flash (<= 3 per second, <= 0.25 white).
//  - Onsets come from bass/mid/treb against their slow averages; if window.__visIn exists (the pseudo-MIDI, later),
//    its kick / snare / hat / energy values win.
//
// Shared q-variables (frame -> shapes, waves, shaders):
//   q1 zoom/frame (<1 = in)  q2 rotation/frame  q3 swirl  q4..q6 channel decay (R, G, B)  q7 symmetry order
//   q8 amber (energy)  q9 flash  q10 kick env  q15 snare env  q16 hat env  q18/q19 shape-position scale x/y
//   q20/q21 lobe count from/to  q22 morph 0..1  q23 superformula exponent  q24 its normaliser  q25 main rotation
//   q26 main scale  q27 mid level  q28 high level  q29 bass level (levels 0..1)
//   q32 wave scale (see the aspect note in frameEqs)   q11..q14, q17, q30, q31: free for the scene
(function () {
  'use strict';

  const fn = (body) => new Function('a', body + ' return a;');   // Butterchurn runs equations as functions of `a`
  const TAU = '6.2831853';
  const list = (v) => '[' + v.join(', ') + ']';

  // ---- shared equations ----------------------------------------------------------------------------------------
  function initEqs(o) {
    return 'a.kt=-9;a.st=-9;a.ht=-9;a.ft=-9;a.kicks=0;a.fl=0;a.ke=0;a.se=0;a.he=0;a.dir=1;a.rv=0;a.rot=0;a.mt=1;' +
      `a.sym=${o.sym[0]};a.m0=${o.lobes[0]};a.m1=${o.lobes[0]};` + (o.init || '');
  }
  function frameEqs(o) {
    return [
      'a.vin = (typeof window !== "undefined" && window.__visIn) ? window.__visIn : null;',
      'a.energy = Math.min(2, (a.bass_att + a.mid_att + a.treb_att) / 3);',
      'a.kick = a.vin ? (a.vin.kick ? 1 : 0) : ((a.bass > 1.25 * a.bass_att && a.bass > 0.55 && a.time - a.kt > 0.25) ? 1 : 0);',
      'a.snare = a.vin ? (a.vin.snare ? 1 : 0) : ((a.mid > 1.3 * a.mid_att && a.mid > 0.6 && a.time - a.st > 0.2) ? 1 : 0);',
      'a.hat = a.vin ? (a.vin.hat ? 1 : 0) : ((a.treb > 1.3 * a.treb_att && a.time - a.ht > 0.09) ? 1 : 0);',
      'if (a.vin && a.vin.energy != null) a.energy = 2 * a.vin.energy;',
      'if (a.kick) { a.kt = a.time; a.kicks += 1; }',
      'if (a.snare) a.st = a.time;',
      'if (a.hat) a.ht = a.time;',
      // envelopes live in own variables (q-variables are not carried from frame to frame) and are copied out
      'a.ke = a.kick ? 1 : a.ke * 0.82; a.se = a.snare ? 1 : a.se * 0.8; a.he = a.hat ? 1 : a.he * 0.7;',
      'a.q10 = a.ke; a.q15 = a.se; a.q16 = a.he;',
      // continuous levels 0..1 (the band values hover around 1 on average)
      'a.q29 = Math.max(0, Math.min(1, (a.bass_att - 0.35) / 1.3));',
      'a.q27 = Math.max(0, Math.min(1, (a.mid_att - 0.35) / 1.3));',
      'a.q28 = Math.max(0, Math.min(1, (a.treb_att - 0.35) / 1.3));',
      // travel: always inward, faster with energy and on the kick
      `a.q1 = ${o.zoom[0]} - ${o.zoom[1]} * a.energy - ${o.zoom[2]} * a.ke;`,
      'a.q2 = 0.0025 * Math.sin(a.time * 0.17) + 0.003 * Math.max(0, a.bass_att - 1);',
      'a.q3 = Math.sin(a.time * 0.11);',
      `a.q4 = ${o.decay[0]}; a.q5 = ${o.decay[1]}; a.q6 = ${o.decay[2]};`,
      // kaleidoscope order: a new one every 16 kicks
      `a.symset = ${list(o.sym)};`,
      'if (a.kick && a.kicks % 16 === 0) a.sym = a.symset[Math.floor(Math.random() * a.symset.length)];',
      'a.q7 = a.sym;',
      // main shape: a new lobe count every 8 kicks, morphed over ~0.7 s
      `a.lobeset = ${list(o.lobes)};`,
      'if (a.kick && a.kicks % 8 === 4) { a.m0 = a.m1; a.nm = a.m1; while (a.nm === a.m1 && a.lobeset.length > 1) a.nm = a.lobeset[Math.floor(Math.random() * a.lobeset.length)]; a.m1 = a.nm; a.mt = 0; }',
      'a.mt = Math.min(1, a.mt + 1.4 / Math.max(20, a.fps || 60));',
      'a.q20 = a.m0; a.q21 = a.m1; a.q22 = a.mt * a.mt * (3 - 2 * a.mt);',
      // spikiness: rounded polygon (0) .. star (1); the kick and the bass push it toward the star
      `a.sp = Math.max(0, Math.min(1, ${o.spike} + 0.5 * a.ke + 0.25 * a.q29));`,
      'a.q23 = 6 + (0.35 - 6) * a.sp;',
      'a.q24 = a.q23 > 2 ? Math.SQRT2 * Math.pow(2, -1 / a.q23) : 1;',
      // turn: each kick gives a push, the direction flips every 4 kicks
      'if (a.kick) { if (a.kicks % 4 === 0) a.dir = -a.dir; a.rv += 0.045 * a.dir; }',
      'a.rv *= 0.93; a.rot += a.rv + 0.002 * a.dir; a.q25 = a.rot;',
      'a.q26 = 1 + 0.35 * a.ke + 0.2 * a.q29;',
      // energy colour and the capped flash: <= 3 per second, at most 0.25 white
      'a.q8 = Math.max(0, Math.min(1, a.energy - 1.1));',
      'if (a.kick && a.energy > 1.35 && a.time - a.ft > 0.34) { a.fl = 0.25; a.ft = a.time; }',
      'a.fl *= 0.8; a.q9 = a.fl;',
      // Keeping things round. Butterchurn hands the equations the INVERSE aspects (wide: aspectx = 1, aspecty = w/h).
      // Shape centres are plain screen coordinates: x scales by 1/aspecty, y by 1/aspectx (q18, q19). Custom waves
      // are already aspect-corrected by Butterchurn, in units of the LONGER side; q32 rescales them to the shorter
      // side, so one radius means the same size in waves and shape placements (0.5 = half the shorter side).
      'a.q18 = 1 / (a.aspecty || 1); a.q19 = 1 / (a.aspectx || 1); a.q32 = a.q18 * a.q19;',
      o.frame || '',
    ].join('\n');
  }

  // ---- shaders --------------------------------------------------------------------------------------------------
  // Feedback: each scene moves the previous frame its own way; the body sets `an` (angle) and `rs` (source radius).
  const WARPS = {
    // straight tunnel: zoom in, slow roll, a little swirl near the centre
    tunnel: 'an += q2 + q3 * 0.005 * (0.3 - r); float rs = r * q1;',
    // spiral: the further out, the more it turns, so the trails become spiral arms; the kick winds them up
    twist: 'an += q2 + 0.004 * q3 + (0.025 + 0.03 * q10) * r; float rs = r * q1;',
    // bloom: zooms faster along q11 directions, so the trails grow into petals; q12 turns the petals
    bloom: 'float rs = r * (q1 - 0.01 * (0.5 + q29) * cos(q11 * an + q12)); an += q2;',
    // ripple: a wave runs outward through the radius, so every trail wobbles as it travels
    ripple: 'float rs = r * q1 + 0.003 * (0.4 + q29) * sin(r * 40.0 - time * 5.0); an += q2;',
  };
  const warpShader = (body) => `shader_body {
  vec2 p = (uv - 0.5) * aspect.xy;
  float r = length(p);
  float an = atan(p.y, p.x);
  ${body}
  vec2 src = vec2(cos(an), sin(an)) * rs * aspect.zw + 0.5;
  vec3 c = texture(sampler_main, src).xyz * vec3(q4, q5, q6);
  ret = max(c - 0.002, 0.0);
}`;
  // Composite: kaleidoscope, glow, each channel tone mapped on its own and mapped to its colour, ground, flash.
  const compShader = (C) => `shader_body {
  vec2 p = (uv - 0.5) * aspect.xy;
  float r = length(p);
  float an = atan(p.y, p.x);
  if (q7 > 1.5) {
    float s = 6.2831853 / q7;
    an = abs(mod(an + 0.5 * s, s) - 0.5 * s);
  }
  vec2 k = vec2(cos(an), sin(an)) * r * aspect.zw + 0.5;
  vec3 ch = texture(sampler_main, k).xyz;
  ch += (texture(sampler_blur1, k).xyz * scale1 + bias1) * 1.2;
  vec3 tm = 1.0 - exp(-ch * vec3(1.3, 1.8, 2.2));
  vec3 low = mix(${C('blue')}, ${C('violet')}, clamp(r * 1.8, 0.0, 1.0));
  vec3 col = tm.x * low + tm.y * ${C('magenta')} + tm.z * ${C('cyan')};
  col += ${C('amber')} * pow(clamp(tm.x * tm.y, 0.0, 1.0), 0.75) * q8 * 1.5;
  col = 1.0 - exp(-col * 1.25);
  vec3 ground = mix(${C('night')}, ${C('ink')}, clamp(r * 1.5, 0.0, 1.0));
  col = ground + col * (1.0 - ground);
  col = mix(col, ${C('white')}, q9);
  ret = col;
}`;

  // ---- building blocks ------------------------------------------------------------------------------------------
  const pos = (rr) => `a.x = 0.5 + ${rr} * Math.cos(a.t) * a.q32; a.y = 0.5 + ${rr} * Math.sin(a.t) * a.q32;`;   // waves only
  // superformula radius (normalised to max 1) for lobe count m at angle u -> into variable `out`
  const sf = (m, u, out) => `a.cc = Math.pow(Math.abs(Math.cos(${m} * ${u} / 4)), a.q23) + Math.pow(Math.abs(Math.sin(${m} * ${u} / 4)), a.q23); ${out} = Math.pow(Math.max(a.cc, 1e-4), -1 / a.q23) / a.q24;`;
  const CH = { r: [1, 0, 0], g: [0, 1, 0], b: [0, 0, 1] };
  const colourEqs = (c) => `a.r = ${CH[c][0]}; a.g = ${CH[c][1]}; a.b = ${CH[c][2]};`;

  function wave(o) {
    const [r, g, b] = CH[o.ch];
    return {
      baseVals: Object.assign({ enabled: 1, samples: 512, sep: 0, scaling: 1, smoothing: 0, r, g, b, a: 0, spectrum: 0, usedots: 0, thick: 1, additive: 1 }, o.base || {}),
      init_eqs_str: '',
      frame_eqs_str: (o.frame || '') + ' ' + colourEqs(o.ch),
      point_eqs_str: o.point,
    };
  }
  function shape(o) {
    const [r, g, b] = CH[o.ch];
    const fill = o.fill ? 1 : 0;
    return {
      baseVals: Object.assign({ enabled: 1, sides: 72, additive: 1, thickoutline: 1, textured: 0, num_inst: 1, x: 0.5, y: 0.5, rad: 0.05, ang: 0,
        r: r * fill, g: g * fill, b: b * fill, a: 0, r2: r * fill, g2: g * fill, b2: b * fill, a2: 0, border_r: r, border_g: g, border_b: b, border_a: 0 }, o.base || {}),
      init_eqs_str: '',
      frame_eqs_str: o.frame,
    };
  }
  // the main shape: superformula loop, morphing between q20 and q21 lobes; `spin` -1 turns it the other way
  function mainShape(o) {
    const m = o.mul || 1;
    return wave({
      ch: o.ch || 'r',
      frame: `a.a = ${o.alpha};`,
      point: [
        `a.t = ${TAU} * a.sample;`,
        `a.u = a.t - ${o.spin || 1} * a.q25;`,
        sf(`a.q20 * ${m}`, 'a.u', 'a.r0'),
        sf(`a.q21 * ${m}`, 'a.u', 'a.r1'),
        `a.rr = (a.r0 + (a.r1 - a.r0) * a.q22) * ${o.scale} * a.q26 + 0.012 * a.value1;`,
        pos('a.rr'),
      ].join('\n'),
    });
  }
  const OFF_SHAPE = { baseVals: { enabled: 0 }, init_eqs_str: '', frame_eqs_str: '' };
  const OFF_WAVE = { baseVals: { enabled: 0 }, init_eqs_str: '', frame_eqs_str: '', point_eqs_str: '' };

  function preset(C, o) {
    const shapes = (o.shapes || []).concat([OFF_SHAPE, OFF_SHAPE, OFF_SHAPE, OFF_SHAPE]).slice(0, 4).map((s) => Object.assign({}, s));
    const waves = (o.waves || []).concat([OFF_WAVE, OFF_WAVE, OFF_WAVE, OFF_WAVE]).slice(0, 4).map((w) => Object.assign({}, w));
    // Butterchurn takes the equations both as text and as compiled functions; give both
    for (const s of shapes) { s.init_eqs = fn(s.init_eqs_str || ''); s.frame_eqs = fn(s.frame_eqs_str || ''); }
    for (const w of waves) { w.init_eqs = fn(w.init_eqs_str || ''); w.frame_eqs = fn(w.frame_eqs_str || ''); w.point_eqs = fn(w.point_eqs_str || ''); }
    const init = initEqs(o), frame = frameEqs(o);
    return {
      baseVals: {
        decay: 1, gammaadj: 1, zoom: 1, rot: 0, warp: 0, wave_a: 0, wave_mode: 0, ob_size: 0, ib_size: 0, mv_a: 0,
        echo_alpha: 0, darken_center: 0, brighten: 0, darken: 0, invert: 0, solarize: 0, additivewave: 1,
      },
      shapes, waves,
      init_eqs_str: init, frame_eqs_str: frame, pixel_eqs_str: '',
      init_eqs: fn(init), frame_eqs: fn(frame), pixel_eqs: fn(''),
      warp: warpShader(WARPS[o.warp]),
      comp: compShader(C),
    };
  }

  // ---- the scenes -----------------------------------------------------------------------------------------------
  // a jagged ring (green): a triangle-wave radius of q11 spikes; always there with the mids, deeper on the snare
  const jaggedRing = (radius) => wave({
    ch: 'g',
    frame: 'a.a = Math.min(1, 0.18 + 0.55 * a.q27 + 0.6 * a.q15);',
    point: [
      `a.t = ${TAU} * a.sample;`,
      'a.f = (a.sample * a.q11 + a.q12) % 1;',
      'a.tri = Math.abs(a.f - 0.5) * 2;',
      `a.rr = ${radius} * (1 + 0.15 * a.q10) * (1 + (0.18 + 0.45 * a.q15) * (a.tri - 0.5)) + 0.012 * a.value1;`,
      pos('a.rr'),
    ].join('\n'),
  });
  // sparks (blue): small diamonds on a ring, flickering with the highs and the hats
  const sparks = (radius, n) => shape({
    ch: 'b', fill: true,
    base: { sides: 4, thickoutline: 0, num_inst: n, rad: 0.006 },
    frame: [
      `a.th = ${TAU} * a.instance / a.num_inst + a.time * 0.4;`,
      `a.rr = ${radius} * (1 + 0.2 * a.q10);`,
      'a.x = 0.5 + a.rr * Math.cos(a.th) * a.q18; a.y = 0.5 + a.rr * Math.sin(a.th) * a.q19;',
      'a.a = (0.25 * a.q28 + 0.75 * a.q16) * (0.4 + 0.6 * Math.random()); a.a2 = a.a;',
      'a.rad = 0.004 + 0.007 * a.q16;',
    ].join('\n'),
  });
  // treble shimmer (blue): the waveform as a thin outer ring, always on with the highs
  const shimmer = (radius) => wave({
    ch: 'b', base: { samples: 360, smoothing: 0.3 },
    frame: 'a.a = 0.12 + 0.5 * a.q28 + 0.3 * a.q16;',
    point: [`a.t = ${TAU} * a.sample;`, `a.rr = ${radius} + 0.03 * a.value1;`, pos('a.rr')].join('\n'),
  });

  function scenes(D) {
    const C = (n) => D.vec3(n);
    return [
      {
        name: 'AtriHord - Ring Tunnel',
        preset: preset(C, {
          warp: 'tunnel', sym: [1, 2, 4, 6, 8], lobes: [3, 5, 6, 8], spike: 0.35,
          zoom: [0.988, 0.010, 0.010], decay: [0.97, 0.962, 0.9],
          init: 'a.jp=0;',
          frame: 'if (a.snare) a.jp = Math.random(); a.jp += 0.002; a.q12 = a.jp; a.q11 = 5 + 2 * Math.floor(3 * Math.min(0.99, a.q27));',
          waves: [
            mainShape({ scale: 0.17, alpha: '0.5 + 0.5 * a.q10' }),
            mainShape({ scale: 0.075, mul: 2, spin: -1, alpha: '0.35 + 0.4 * a.q29' }),
            jaggedRing(0.27),
            shimmer(0.42),
          ],
          shapes: [
            shape({ ch: 'r', base: { num_inst: 2 }, frame: 'a.rad = 0.08 + 0.3 * (1 - a.q10) + 0.006 * a.instance; a.border_a = 0.8 * a.q10 * a.q10;' }),
            sparks(0.34, 24),
          ],
        }),
      },
      {
        name: 'AtriHord - Star Gate',
        preset: preset(C, {
          warp: 'twist', sym: [2, 4, 6], lobes: [3, 5, 6], spike: 0.05,
          zoom: [0.986, 0.008, 0.012], decay: [0.972, 0.965, 0.9],
          init: 'a.rp=0;a.seed=0;',
          frame: 'a.rp -= 0.004 + 0.01 * a.q27 + 0.03 * a.q15; a.q12 = a.rp; a.q11 = 3 + Math.floor(3 * Math.min(0.99, a.q27)); if (a.hat) a.seed = Math.random(); a.q13 = a.seed;',
          waves: [
            mainShape({ scale: 0.2, alpha: '0.55 + 0.45 * a.q10' }),
            mainShape({ scale: 0.1, spin: -1, alpha: '0.4 + 0.4 * a.q29' }),
            // rose petals (green): q11 petals turning against the gate, fuller with the mids
            wave({
              ch: 'g',
              frame: 'a.a = Math.min(1, 0.2 + 0.6 * a.q27 + 0.4 * a.q15);',
              point: [
                `a.t = ${TAU} * a.sample;`,
                'a.pe = Math.pow(Math.abs(Math.cos(a.q11 * (a.t + a.q12) / 2)), 1.6 - a.q27);',
                'a.rr = 0.3 * (1 + 0.15 * a.q15) * (0.2 + 0.8 * a.pe);',
                pos('a.rr'),
              ].join('\n'),
            }),
            // hat dust (blue): dots scattered anew on each hat
            wave({
              ch: 'b', base: { samples: 96, usedots: 1, thick: 1 },
              frame: 'a.a = 0.15 * a.q28 + 0.85 * a.q16;',
              point: [
                `a.t = ${TAU} * ((a.sample * 7.31 + a.q13) % 1);`,
                'a.rr = 0.06 + 0.45 * ((a.sample * 13.77 + a.q13 * 3.1) % 1);',
                pos('a.rr'),
              ].join('\n'),
            }),
          ],
          shapes: [
            shape({ ch: 'r', base: { sides: 4, num_inst: 1 }, frame: 'a.sides = Math.round(a.q21); a.ang = -a.q25; a.rad = 0.1 + 0.35 * (1 - a.q10); a.border_a = 0.7 * a.q10 * a.q10;' }),
          ],
        }),
      },
      {
        name: 'AtriHord - Mandala Bloom',
        preset: preset(C, {
          warp: 'bloom', sym: [6, 8], lobes: [5, 6, 8], spike: 0.55,
          zoom: [0.992, 0.008, 0.008], decay: [0.968, 0.965, 0.91],
          init: 'a.pp=0;a.orb=0;',
          frame: 'a.pp += 0.003 + 0.004 * a.q27; a.q12 = a.pp; a.q11 = a.m1; a.orb += 0.004 + 0.03 * a.q10; a.q13 = a.orb;',
          waves: [
            // the flower (red): q20/q21 petals opening with the bass
            wave({
              ch: 'r',
              frame: 'a.a = 0.5 + 0.5 * a.q10;',
              point: [
                `a.t = ${TAU} * a.sample;`,
                'a.u = a.t - a.q25;',
                'a.p0 = Math.pow(Math.abs(Math.cos(a.q20 * a.u / 2)), 2.2 - 1.6 * a.q29);',
                'a.p1 = Math.pow(Math.abs(Math.cos(a.q21 * a.u / 2)), 2.2 - 1.6 * a.q29);',
                'a.rr = 0.2 * a.q26 * (0.3 + 0.7 * (a.p0 + (a.p1 - a.p0) * a.q22)) + 0.01 * a.value1;',
                pos('a.rr'),
              ].join('\n'),
            }),
            mainShape({ scale: 0.07, mul: 1, spin: -1, alpha: '0.3 + 0.5 * a.q29' }),
            jaggedRing(0.33),
            shimmer(0.45),
          ],
          shapes: [
            // orbiting rings (green): six circles turning with the beat, swelling with the mids
            shape({ ch: 'g', base: { num_inst: 6, sides: 48 }, frame: [
              `a.th = ${TAU} * a.instance / a.num_inst + a.q13;`,
              'a.x = 0.5 + 0.26 * Math.cos(a.th) * a.q18; a.y = 0.5 + 0.26 * Math.sin(a.th) * a.q19;',
              'a.rad = 0.018 + 0.03 * a.q27 + 0.02 * a.q15; a.border_a = 0.25 + 0.6 * a.q27 + 0.3 * a.q15;',
            ].join('\n') }),
            // twinkles (blue): small diamonds at random places on the hats
            shape({ ch: 'b', fill: true, base: { sides: 4, thickoutline: 0, num_inst: 14, rad: 0.005 }, frame: [
              'a.x = 0.5 + (Math.random() - 0.5) * 0.7 * a.q18; a.y = 0.5 + (Math.random() - 0.5) * 0.7 * a.q19;',
              'a.a = a.q16 * Math.random(); a.a2 = a.a; a.rad = 0.003 + 0.006 * a.q16;',
            ].join('\n') }),
          ],
        }),
      },
      {
        name: 'AtriHord - Pulse Field',
        preset: preset(C, {
          warp: 'ripple', sym: [1, 2, 4], lobes: [3, 5, 6], spike: 0.0,
          zoom: [0.99, 0.008, 0.01], decay: [0.966, 0.955, 0.9],
          waves: [
            mainShape({ scale: 0.14, alpha: '0.55 + 0.45 * a.q10' }),
            // circular spectrum (green): the frequency spectrum as a ring of spikes
            wave({
              ch: 'g', base: { samples: 256, spectrum: 1, smoothing: 0.3 },
              frame: 'a.a = 0.25 + 0.6 * a.q27 + 0.3 * a.q15;',
              point: [`a.t = ${TAU} * a.sample - a.q25 * 0.3;`, 'a.rr = 0.26 + 0.14 * Math.min(1, Math.abs(a.value1) * 0.5);', pos('a.rr')].join('\n'),
            }),
            shimmer(0.42),
          ],
          shapes: [
            // kick burst (red): a polygon of the main shape's lobe count that flies out on every kick
            shape({ ch: 'r', base: { sides: 4, num_inst: 2 }, frame: 'a.sides = Math.max(3, Math.round(a.q21)); a.ang = a.q25 + 0.3 * a.instance; a.rad = 0.1 + 0.4 * (1 - a.q10) + 0.015 * a.instance; a.border_a = 0.8 * a.q10;' }),
            sparks(0.36, 18),
          ],
        }),
      },
    ];
  }

  window.VisPresets = Object.assign(window.VisPresets || {}, {
    set: scenes,
    rings: (D) => scenes(D)[0].preset,   // the first scene on its own (older callers)
  });
})();
