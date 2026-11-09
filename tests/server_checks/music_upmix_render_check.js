
const $ = () => null; const store = () => null; const toast = () => {};
const LOUD = { ctx: null };
  // ---- every ear-level speaker (user, 2026-11-04) ----------------------------
  // "The music page should detect a dolby atmos/multi channel output device if
  // it is the active output device and use every ear-level channel. Height
  // channels should be a toggle in the nav-bar, default off."
  // destination.maxChannelCount is the channel count of the device the browser
  // plays to. The browser hands a device its channels in WAVE order (FL FR FC
  // LFE BL BR SL SR, then the top four), so the count names the layout. Windows
  // gives a browser at most the 7.1 bed of a Dolby Atmos device - its height
  // channels are only reachable through Windows' spatial audio objects - so
  // there the Heights switch stays greyed out; Atmos for Headphones is a
  // two-channel device and is left alone.
  // The library is stereo, so the page upmixes: L and R stay on the front pair
  // untouched, the centre gets a little of the mid (L+R), and the surround and
  // height pairs get the side signal (L-R), low-passed, delayed and in
  // opposite polarity left and right - a passive matrix, no steering. The LFE
  // channel stays silent (the receiver's bass management feeds the sub). A
  // multichannel file is folded to stereo first.
  const SURROUND = {
    4: { name: 'Quad', front: [0, 1], center: -1, beds: [[2, 3, 0.5, 0.010]], tops: [] },
    6: { name: '5.1', front: [0, 1], center: 2, beds: [[4, 5, 0.5, 0.010]], tops: [] },
    8: { name: '7.1', front: [0, 1], center: 2, beds: [[6, 7, 0.5, 0.010], [4, 5, 0.4, 0.018]], tops: [] },
    10: { name: '5.1.4', front: [0, 1], center: 2, beds: [[4, 5, 0.5, 0.010]],
      tops: [[6, 7, 0.3, 0.022], [8, 9, 0.25, 0.028]] },
    12: { name: '7.1.4', front: [0, 1], center: 2, beds: [[6, 7, 0.5, 0.010], [4, 5, 0.4, 0.018]],
      tops: [[8, 9, 0.3, 0.022], [10, 11, 0.25, 0.028]] },
  };
  const CENTER_GAIN = 0.35;
  const UP = { input: null, nodes: [], layout: null, heights: store('heights') === true };

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

  // (Re)wire the bus to the device: straight through on stereo, the matrix on
  // a surround device. Again whenever the output device or the switch changes.
  function buildUpmix() {
    const ctx = LOUD.ctx;
    if (!ctx || !UP.input) return;
    try { UP.input.disconnect(); } catch (e) { /* not connected yet */ }
    for (const n of UP.nodes) { try { n.disconnect(); } catch (e) { /* gone */ } }
    UP.nodes = [];
    const dest = ctx.destination;
    const max = dest.maxChannelCount || 2;
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
      const gain = (v) => { const g = keep(ctx.createGain()); g.gain.value = v; return g; };
      const split = keep(ctx.createChannelSplitter(2));
      const merge = keep(ctx.createChannelMerger(max));
      UP.input.connect(split);
      merge.connect(dest);
      split.connect(merge, 0, lay.front[0]);
      split.connect(merge, 1, lay.front[1]);
      const mid = gain(0.5);
      split.connect(mid, 0);
      split.connect(mid, 1);
      if (lay.center >= 0) mid.connect(gain(CENTER_GAIN)).connect(merge, 0, lay.center);
      const side = gain(1);
      split.connect(gain(0.5), 0).connect(side);
      split.connect(gain(-0.5), 1).connect(side);
      const pair = ([l, r, level, delay], high) => {
        let n = side;
        if (high) {
          const hp = keep(ctx.createBiquadFilter());
          hp.type = 'highpass';
          hp.frequency.value = 150;
          n = n.connect(hp);
        }
        const lp = keep(ctx.createBiquadFilter());
        lp.type = 'lowpass';
        lp.frequency.value = high ? 6000 : 7000;
        const dl = keep(ctx.createDelay(0.1));
        dl.delayTime.value = delay;
        n.connect(lp).connect(dl);
        dl.connect(gain(level)).connect(merge, 0, l);
        dl.connect(gain(-level)).connect(merge, 0, r);
      };
      for (const b of lay.beds) pair(b, false);
      if (UP.heights) for (const t of lay.tops) pair(t, true);
    } catch (e) {
      // A device the browser will not open that wide: plain stereo.
      UP.layout = null;
      try { dest.channelCount = Math.min(2, max); dest.channelInterpretation = 'speakers'; } catch (e2) { /* as is */ }
      try { UP.input.connect(dest); } catch (e3) { /* already */ }
    } finally {
      renderSurround();
    }
  }

  function renderSurround() {
    const lay = UP.layout;
    const chip = $('muSurround');
    const btn = $('muHeights');
    if (!chip || !btn) return;
    chip.hidden = btn.hidden = !lay;
    if (!lay) return;
    chip.textContent = lay.name;
    chip.title = `Output: ${lay.name} - every ear-level speaker plays`;
    const can = lay.tops.length > 0;
    btn.disabled = !can;
    btn.setAttribute('aria-pressed', String(can && UP.heights));
    btn.title = can ? (UP.heights ? 'Height speakers on' : 'Height speakers off')
      : 'This output gives the browser no height channels (Windows passes a browser only the 7.1 bed of Dolby Atmos)';
  }

  function setHeights(on) {
    UP.heights = !!on;
    store('heights', UP.heights);
    buildUpmix();
    toast(UP.heights ? 'Height speakers on' : 'Height speakers off');
  }


async function run(n, heights) {
  const ctx = new OfflineAudioContext(n, 9600, 48000);
  LOUD.ctx = ctx; UP.input = null; UP.nodes = []; UP.heights = heights;
  const buf = ctx.createBuffer(2, 9600, 48000);
  const L = buf.getChannelData(0);
  for (let i = 0; i < 9600; i++) L[i] = 0.5 * Math.sin(2 * Math.PI * 1000 * i / 48000);
  const src = ctx.createBufferSource(); src.buffer = buf;
  src.connect(upmixInput()); buildUpmix(); src.start();
  const out = await ctx.startRendering();
  const rms = [];
  for (let c = 0; c < out.numberOfChannels; c++) {
    const d = out.getChannelData(c); let e = 0;
    for (let i = 2400; i < 9600; i++) e += d[i] * d[i];
    rms.push(Math.sqrt(e / 7200).toFixed(3));
  }
  return `${n}ch heights=${heights} layout=${UP.layout ? UP.layout.name : 'stereo'}: ${rms.join(' ')}`;
}
(async () => {
  const lines = [];
  for (const [n, h] of [[2,false],[6,false],[8,false],[8,true],[12,false],[12,true],[10,true]]) lines.push(await run(n, h));
  window.__r = lines;
})().catch(e => window.__r = ["ERR " + e]);
