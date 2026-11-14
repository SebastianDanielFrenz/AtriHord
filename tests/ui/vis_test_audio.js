// Test music for the visualizer previews: a 124 bpm groove (kick, offbeat hats, a bass line, a pad) with 2 bars of
// breakdown every 8 bars, so builds, drops and quiet parts all occur within a minute.
//   const s = VisTestAudio.startSynth(audioContext, destinationNode);  ...  s.stop();
window.VisTestAudio = {
  startSynth(ctx, out) {
    ctx.resume();
    const noise = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
    const nd = noise.getChannelData(0);
    for (let i = 0; i < nd.length; i++) nd[i] = Math.random() * 2 - 1;
    const bpm = 124, beat = 60 / bpm;
    const bassNotes = [41.2, 41.2, 49, 55, 36.7, 36.7, 49, 46.2];
    const chords = [[220, 261.6, 329.6], [196, 246.9, 293.7], [174.6, 220, 261.6], [196, 246.9, 329.6]];
    let next = ctx.currentTime + 0.1, step = 0;
    const pad = ctx.createBiquadFilter();
    pad.type = 'lowpass';
    pad.frequency.value = 900;
    pad.connect(out);
    function sched() {
      while (next < ctx.currentTime + 0.2) {
        const t = next, bar = Math.floor(step / 8), breakdown = bar % 8 >= 6, eighth = step % 8;
        if (!breakdown && eighth % 2 === 0) {
          const o = ctx.createOscillator(), g = ctx.createGain();
          o.frequency.setValueAtTime(150, t); o.frequency.exponentialRampToValueAtTime(42, t + 0.13);
          g.gain.setValueAtTime(1, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.35);
          o.connect(g).connect(out); o.start(t); o.stop(t + 0.4);
        }
        if (eighth % 2 === 1) {
          const s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
          s.buffer = noise; f.type = 'highpass'; f.frequency.value = 7000;
          g.gain.setValueAtTime(breakdown ? 0.05 : 0.18, t); g.gain.exponentialRampToValueAtTime(0.001, t + 0.06);
          s.connect(f).connect(g).connect(out); s.start(t, Math.random() * 0.5); s.stop(t + 0.08);
        }
        if (!breakdown) {
          const o = ctx.createOscillator(), f = ctx.createBiquadFilter(), g = ctx.createGain();
          o.type = 'sawtooth'; o.frequency.value = bassNotes[eighth] * 2;
          f.type = 'lowpass'; f.frequency.value = 500;
          g.gain.setValueAtTime(0.22, t); g.gain.exponentialRampToValueAtTime(0.001, t + beat * 0.45);
          o.connect(f).connect(g).connect(out); o.start(t); o.stop(t + beat * 0.5);
        }
        if (eighth === 0) {
          for (const hz of chords[bar % 4]) for (const det of [-6, 6]) {
            const o = ctx.createOscillator(), g = ctx.createGain();
            o.type = 'triangle'; o.frequency.value = hz; o.detune.value = det;
            g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.05, t + 0.4); g.gain.exponentialRampToValueAtTime(0.0001, t + beat * 4);
            o.connect(g).connect(pad); o.start(t); o.stop(t + beat * 4 + 0.05);
          }
        }
        next += beat / 2;
        step++;
      }
    }
    sched();
    const timer = setInterval(sched, 25);
    return { stop() { clearInterval(timer); } };
  },
};
