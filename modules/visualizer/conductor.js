// Conductor of the AtriHord scene set: plays the scenes one after another and decides WHEN to change.
//   const c = VisConductor.create({ scenes: ['A', 'B', ...], load(name, blendSeconds), mode: 'auto', phrases: 2 });
//   every frame: c.update(now, features)   // features from MusicVisualizer.audioFeatures(analyser): bass, level, beat
//   c.next()                               // the trigger (button, key, later the music page)
// Modes:
//   auto   - change at the end of a phrase (32 beats = 8 bars of 4/4) after `phrases` phrases, AND at once on a drop
//   drops  - change only on a drop (with a long time fallback, so a track without drops still moves on)
//   manual - change only on next()
// A drop = the beat comes back after the low end was quiet for a while (the end of a breakdown). The beat count is
// taken from the onsets; it is not aligned to real downbeats yet (the pseudo-MIDI's bar grid will do that later).
(function () {
  'use strict';
  const PHRASE = 32;            // beats
  const QUIET_BASS = 0.3;       // bass level below this counts as "quiet"
  const QUIET_MIN = 2.5;        // seconds of quiet before the return of the beat counts as a drop
  const DROP_WINDOW = 1.5;      // the first strong beat within this many seconds after the quiet ends
  const MIN_STAY = 8;           // seconds a scene stays at least (auto changes only; next() always works)
  const FALLBACK = 75;          // seconds: change anyway when no phrase/drop came (ambient, no clear beat)

  function create(opts) {
    const st = {
      scenes: opts.scenes.slice(), i: -1, mode: opts.mode || 'auto', phrases: opts.phrases || 2,
      since: 0, beats: 0, quiet: 0, armed: -99, lastBeat: 0, last: '',
    };
    let started = false;
    function go(i, blend, why) {
      st.i = ((i % st.scenes.length) + st.scenes.length) % st.scenes.length;
      st.beats = 0;
      st.since = performance.now() / 1000;
      st.last = why;
      opts.load(st.scenes[st.i], blend);
    }
    const api = {
      start(i) { started = true; go(i == null ? 0 : i, 1.5, 'start'); },
      stop() { started = false; },
      get active() { return started; },
      next(why) { if (started) go(st.i + 1, why === 'drop' ? 0.6 : 2.5, why || 'trigger'); },
      setMode(m) { st.mode = m; },
      setPhrases(n) { st.phrases = Math.max(1, n | 0); },
      setScenes(list) { st.scenes = list.slice(); },
      state() { return { scene: st.scenes[st.i], beats: st.beats, phrase: Math.floor(st.beats / PHRASE), mode: st.mode, last: st.last, quiet: +st.quiet.toFixed(1) }; },
      update(now, f) {
        if (!started || !f) return;
        const dt = Math.min(0.1, Math.max(0, now - (api._t || now)));
        api._t = now;
        // quiet low end -> arm the drop when it ends
        if (f.bass < QUIET_BASS) st.quiet += dt;
        else { if (st.quiet >= QUIET_MIN) st.armed = now; st.quiet = 0; }
        const onset = f.beat >= 0.999 && now - st.lastBeat > 0.2;
        if (onset) { st.lastBeat = now; st.beats += 1; }
        if (st.mode === 'manual') return;
        const stay = now - st.since;
        if (onset && now - st.armed < DROP_WINDOW && f.bass > 0.5 && stay > MIN_STAY) { st.armed = -99; api.next('drop'); return; }
        if (st.mode === 'auto' && onset && stay > MIN_STAY && st.beats >= st.phrases * PHRASE && st.beats % PHRASE === 1) { api.next('phrase'); return; }
        if (stay > (st.mode === 'drops' ? FALLBACK * 2 : FALLBACK)) api.next('time');
      },
    };
    return api;
  }
  window.VisConductor = { create };
})();
