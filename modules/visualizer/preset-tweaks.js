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

  const originals = {};
  function apply(name, preset) {
    const edit = EDITS[name];
    if (!edit || !preset) return preset;
    const copy = JSON.parse(JSON.stringify(preset));
    edit(copy);
    return copy;
  }
  for (const name of Object.keys(EDITS)) {
    if (!C[name]) continue;
    originals[name] = C[name];
    try { C[name] = apply(name, C[name]); } catch (e) { console.warn('visualizer tweak', name, e); C[name] = originals[name]; }
  }
  window.VisTweaks = { apply, originals, names: Object.keys(EDITS) };
})();
