// Our edits to picked MilkDrop presets (owner feedback, 2026-11-14). The preset files stay as they came from
// butterchurn-presets; this file patches the copies in window.VisCurated when it loads (Butterchurn compiles the
// equation and shader strings itself at loadPreset) and keeps the originals in VisTweaks.originals for comparison.
//   VisTweaks.apply(name, preset) -> an edited copy (or the preset itself when there is no edit for it)
// Shader edits go in front of the final "ret = ..." of the shader body, so `ret` is the colour about to be output.
// The audio uniforms there (bass, mid, treb and the smoothed *_att) are about 1 for average music and 0 in silence.
(function () {
  'use strict';
  const C = window.VisCurated || {};

  // insert GLSL just before the shader's last closing brace
  const tail = (src, code) => { const i = src.lastIndexOf('}'); return src.slice(0, i) + code + '\n' + src.slice(i); };
  const must = (src, a, b) => { if (!src.includes(a)) throw new Error('tweak: not found: ' + a); return src.split(a).join(b); };

  const EDITS = {
    // gold and lilac -> the AtriHord blue with magenta highlights
    'flexi - borderline imagery': (p) => {
      p.comp = must(p.comp, 'vec3(0.7, 0.5, 0.1)', 'vec3(0.16, 0.38, 1.0)');
      p.comp = must(p.comp, 'vec3(0.7, 0.5, 1.0)', 'vec3(1.0, 0.32, 0.78)');
    },
    // orange and blue -> violet-magenta with cyan highlights (the colour ramp at the end of its comp shader)
    'fishbrain + flexi - stitchcraft': (p) => {
      p.comp = must(p.comp, 'vec3(4.0, 2.8, 0.0)', 'vec3(3.2, 0.4, 3.6)');
      p.comp = must(p.comp, 'vec3(-0.3, 0.2, 0.6)', 'vec3(-0.2, 0.55, 0.6)');
    },
    // steel blue and brown -> violet-magenta on deep navy, with cyan-white highlights
    'Flexi - mindblob mix': (p) => {
      p.comp = must(p.comp, 'vec3(0.3, 0.5, 0.7)', 'vec3(0.78, 0.26, 0.9)');
      p.comp = must(p.comp, 'vec3(0.2, 0.1, 0.0)', 'vec3(0.0, 0.07, 0.24)');
      p.comp = must(p.comp, 'texture (sampler_main, uv).zzz', '(texture (sampler_main, uv).zzz * vec3(0.75, 1.0, 1.0))');
    },
    // a particle pond that never read the audio: the bass kicks the particles (its "shock"), the pond breathes with
    // the bass and the brightness pulses with the beat
    'Flexi - alien fish pond': (p) => {
      p.frame_eqs_str = must(p.frame_eqs_str, 'a.shock=.002;', 'a.shock=.002+.02*Math.max(0,a.bass-.8);');
      p.frame_eqs_str += 'a.zoom=1+.025*(a.bass_att-1);a.rot=.01*(a.treb_att-1);';
      p.comp = tail(p.comp, '  ret *= 0.5 + 0.3 * clamp(bass_att, 0.0, 1.6) + 0.4 * clamp(bass, 0.0, 2.0);');
      // the cells are drawn by the first shape: they swell on the kick
      const cells = (p.shapes || [])[0];
      if (cells) cells.frame_eqs_str += 'a.rad*=.8+.35*Math.min(2,a.bass);';
    },
    // overreactive: the dot count was 4^(bass+mid+treb) of the instant level (dozens to 1024 from frame to frame) and
    // two shapes took a new random transparency from the level every frame (a strobe). Now the smoothed level, a
    // gentler dot count and a faint steady transparency that follows the level.
    'suksma - water cooled red uranium vs dotes - freeenergynow.net': (p) => {
      p.frame_eqs_str = must(p.frame_eqs_str, 'a.vol3=.3*(a.bass+a.mid+a.treb);', 'a.vol3=.3*(a.bass_att+a.mid_att+a.treb_att);');
      const sh = p.shapes || [];
      sh[0].frame_eqs_str = must(sh[0].frame_eqs_str, 'pow(4,a.bass+a.treb+a.mid)', 'pow(3,.75*(a.bass_att+a.treb_att+a.mid_att))');
      for (const s of sh.slice(1, 3)) s.frame_eqs_str = must(s.frame_eqs_str, 'div(Math.floor(randint(Math.floor(a.q30))),5)', 'div(a.q30,40)');
      // the camera and colour drift ran at a speed proportional to loudness: half as fast now
      p.frame_eqs_str = must(p.frame_eqs_str, 'a.vol=.03*(', 'a.vol=.015*(');
    },
    // the 3D dot spiral only turned a little faster with louder music: now the spiral swells with the bass, spins on
    // a kick, and the feedback behind it zooms on the kick and turns with the highs
    'EVET - RGB Singularity': (p) => {
      for (const w of (p.waves || []).slice(0, 2)) {
        w.point_eqs_str = must(w.point_eqs_str, 'a.zoom=.35*div(1,a.z+a.a)', 'a.zoom=.35*(.7+.45*Math.min(2,a.bass_att))*div(1,a.z+a.a)');
        w.frame_eqs_str = must(w.frame_eqs_str, 'a.j+=.01*a.bass_att;', 'a.j+=.01*a.bass_att+.03*Math.max(0,a.bass-1);');
      }
      p.frame_eqs_str += 'a.zoom=.9901+.04*Math.max(0,a.bass-.9);a.rot=.02*(a.treb_att-1);';
      // the colour flow of its warp shader runs with the bass (green) and the mids (blue) instead of at a fixed speed
      p.warp = must(p.warp, '((tmpvar_6 * texsize.zw) * 6.0)', '((tmpvar_6 * texsize.zw) * (1.5 + 4.5 * clamp(bass_att, 0.0, 2.0)))');
      p.warp = must(p.warp, '((tmpvar_8 * texsize.zw) * 2.0)', '((tmpvar_8 * texsize.zw) * (0.5 + 1.5 * clamp(mid_att, 0.0, 2.0)))');
      // it has no comp shader of its own: one that shows the picture as is, with the brightness on the beat
      p.comp = ' shader_body {\n  ret = texture (sampler_main, uv).xyz;\n' +
        '  ret *= 0.55 + 0.3 * clamp(bass_att, 0.0, 1.6) + 0.35 * clamp(bass, 0.0, 2.0);\n }';
    },
    // the branching tree only bent slowly with the averaged level: its branches swing three times as far with the
    // music, it grows and lights up with the bass, the picture zooms on the kick and turns with the highs, and the brightness follows the bass
    'shifter - dark tides bdrv mix': (p) => {
      const tree = (p.waves || [])[3];
      for (const q of ['q1', 'q2', 'q3']) {
        const v = { q1: 'tva', q2: 'tvb', q3: 'tvc' }[q];
        tree.frame_eqs_str = must(tree.frame_eqs_str, 'a.' + v + '=a.tic*a.' + q + '*.5;', 'a.' + v + '=a.tic*a.' + q + '*1.5;');
      }
      // the tree's length `len` is a frame variable of the wave, but its point equations use a `len` of their own;
      // Butterchurn runs both on one variable set and keeps what the last point left (MilkDrop keeps them apart), so
      // the length collapsed every frame and the tree never grew past a dot:
      // the main frame now hands it over in q7, growing with the bass (none in silence); its lines light up on the
      // beat (they were drawn at a fixed 10 %)
      tree.frame_eqs_str = must(tree.frame_eqs_str, 'a.t4=a.len;', 'a.t4=a.q7;');
      p.frame_eqs_str += 'a.q7=above(a.q6,0)*(.16+.16*Math.min(2,a.bass_att));';
      tree.point_eqs_str = must(tree.point_eqs_str, 'a.x=.75*(a.x-.5)+.5;a.a=.1;', 'a.x=.75*(a.x-.5)+.5;a.a=.08+.3*Math.min(2,a.bass);');
      p.frame_eqs_str = must(p.frame_eqs_str, 'a.zoom=1.01;', 'a.zoom=1.01+.035*Math.max(0,a.bass-.9);a.rot=.015*(a.treb_att-1);');
      p.comp = tail(p.comp, '  ret *= 0.45 + 0.3 * clamp(bass_att, 0.0, 1.6) + 0.45 * clamp(bass, 0.0, 2.0);');
    },
    // the mashup lost the frame equations of the preset it came from: the brightness of its 3D ribbon reads q26, which
    // nothing set, so the ribbon stayed faint whatever played. The frame equations now set q26 from the bass and a spin
    // (q27) that steps on peaks of the mids and highs; the picture zooms on the kick and turns with the highs
    'Adam Eatit Mashup FX 2 martin - disco mix + Lodus + Geiss + Ludicrous speed + Baked Ft another AdamFX Mashup 7_1': (p) => {
      p.init_eqs_str = (p.init_eqs_str || '') + 'a.vol_m=1;a.pulse=0;a.winkel=0;a.q26=0;a.q27=0;';
      p.frame_eqs_str = (p.frame_eqs_str || '') +
        'a.vol=a.mid_att+a.treb_att;a.vol_m=.99*a.vol_m+.01*a.vol;a.exc=above(a.vol,1.1*a.vol_m);a.pulse=.9*a.pulse+.1*a.exc;' +
        'a.winkel+=.05*a.pulse+.01*a.bass_att;a.q27=a.winkel;a.q26=4*Math.min(2,a.bass_att);' +
        'a.zoom=.99853+.035*Math.max(0,a.bass-.9);a.rot=.002+.012*(a.treb_att-1);';
      const ribbon = (p.waves || [])[0];
      ribbon.frame_eqs_str = must(ribbon.frame_eqs_str, 'a.winkel+=div(a.pulse,3);a.t3=a.winkel;', 'a.t3=a.q27;');
      // (no comp shader of our own: Butterchurn's default one gives it its gamma and darkened centre)
    },
    // looked the same with and without music: dim at rest, brightens and flashes with the bass, lights pulse with the
    // mids, and the camera sways a little on the beat
    'martin - crystal alley': (p) => {
      p.comp = tail(p.comp, '  ret *= 0.45 + 0.55 * clamp(bass_att, 0.0, 1.8) + 0.2 * clamp(bass - 1.15, 0.0, 1.0);');
      p.frame_eqs_str = (p.frame_eqs_str || '') +
        'a.zoom=.9999+.012*Math.max(0,a.bass_att-.9);a.rot=.006*(a.mid_att-1);';
      for (const s of p.shapes || []) {
        if (!s.baseVals || !s.baseVals.enabled) continue;
        s.frame_eqs_str = (s.frame_eqs_str || '') + 'a.a*=.35+.65*Math.min(1.6,a.mid_att);a.rad*=.8+.35*Math.min(1.6,a.bass_att);';
      }
    },
    // its zoom only reacted to bass above 1.2 (rare): reacts from 0.75 now and harder, the picture breathes with the
    // bass, turns with the highs, leaves longer trails on a kick, and the brightness follows the music
    '_Vovan + Geiss - Bass With Flover (Feedback Mix) 2': (p) => {
      p.pixel_eqs_str = must(p.pixel_eqs_str, 'Math.max(.001,a.bass-1.2)', '.9*Math.max(.001,a.bass-.7)');
      p.frame_eqs_str += 'a.zoom+=.05*(a.bass_att-1);a.rot+=.03*(a.treb_att-1);a.decay=.975-.03*Math.max(0,a.bass-1.1);';
      p.comp = tail(p.comp, '  ret *= 0.4 + 0.35 * clamp(bass_att, 0.0, 1.6) + 0.45 * clamp(bass, 0.0, 2.0) + 0.15 * clamp(treb - 1.0, 0.0, 1.0);');
    },
  };

  // The presets that looked nearly the same with and without music in the scan of all presets (2026-11-15,
  // tests/ui/vis_reactivity.html): one mild beat reaction for all of them - a zoom kick, a slight turn with the highs
  // and a brightness pulse (in their comp shader, or through the gamma of Butterchurn's default comp when they have
  // none). Silence dims them to three quarters; average music is about as bright as before.
  const BOOST = [
    'Flexi - mindblob mix', '$$$ Royal - Mashup (160)', 'martin + stahlregen - martin in da mash 12',
    'martin + stahlregen - martin in da mash 12a', 'Eo.S. + Phat - Emergent factors', 'Aderrasi - Curse of the Mirror Emu',
    'Eo.S. + Geiss - glowsticks v2 02 (Relief Mix)',
    'Martin N AdamFX Infusion = Phat+Yin+Eo.S_Mandala Chaser Ft AdamFX n Martin - The Beast Mandala Chaser FX H',
    'Unchained - Fuzzy Sciences', 'Geiss - Desert Rose 4', 'ORB - Blue Emotion',
    'Stahlregen + martin + others - Psychedelic Metal Flower', 'Flexi - blame hexcollie twice',
    'Aderrasi - Storm of the Eye (Thunder) - mash0000 - quasi pseudo meta concentrics',
    'Adam Eatit Mashup FX 2 martin - disco mix + Lodus + Geiss + Ludicrous speed + Aderrasi 2_1',
  ];
  function boost(p) {
    const own = !!(p.comp || '').trim();
    p.frame_eqs_str = (p.frame_eqs_str || '') + 'a.zoom+=.03*Math.max(0,a.bass-.9);a.rot+=.008*(a.treb_att-1);' +
      (own ? '' : 'a.gammaadj*=.75+.15*Math.min(1.6,a.bass_att)+.2*Math.min(2,a.bass);');
    if (own) p.comp = tail(p.comp, '  ret *= 0.75 + 0.15 * clamp(bass_att, 0.0, 1.6) + 0.2 * clamp(bass, 0.0, 2.0);');
  }

  const originals = {};
  function apply(name, preset) {
    const edit = EDITS[name];
    if ((!edit && !BOOST.includes(name)) || !preset) return preset;
    const copy = JSON.parse(JSON.stringify(preset));
    if (edit) edit(copy);
    if (BOOST.includes(name)) boost(copy);
    return copy;
  }
  for (const name of new Set([...Object.keys(EDITS), ...BOOST])) {
    if (!C[name]) continue;
    originals[name] = C[name];
    try { C[name] = apply(name, C[name]); } catch (e) { console.warn('visualizer tweak', name, e); C[name] = originals[name]; }
  }
  window.VisTweaks = { apply, originals, names: [...new Set([...Object.keys(EDITS), ...BOOST])], boosted: BOOST };
})();
