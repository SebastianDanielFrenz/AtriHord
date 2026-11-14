// Music visualizer: bundles of thin glowing strands that flow and swell with the music (the look of the macOS Music
// app's visualizer). Everything is drawn on the GPU (WebGL2): each strand is a ribbon of quads whose path is computed
// in the vertex shader from layered sine waves and simplex noise, the strands are summed additively into a float
// buffer, optionally fed back as trails, bloomed (dual Kawase) and tone mapped onto the canvas.
//
// The picture is laid out in units of the screen's SHORT side, so it looks native at any aspect ratio (21:9 ... 9:16):
// a wider screen shows more of the lines, never stretched ones. In portrait the field turns so the lines run along the
// long side.
//
//   const vis = MusicVisualizer.create(canvas, { features: MusicVisualizer.audioFeatures(analyser) });
//   vis.start(); vis.setPalette(MusicVisualizer.paletteFromImage(img)); vis.params.bloom = 1.2; vis.stop();
//
// The simplex noise is Ashima Arts' / Stefan Gustavson's webgl-noise (MIT licence).
(function () {
  'use strict';

  const NOISE = `
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+10.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0);
  const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy));
  vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz);
  vec3 l=1.0-g;
  vec3 i1=min(g.xyz,l.zxy);
  vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx;
  vec3 x2=x0-i2+C.yyy;
  vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857;
  vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z);
  vec4 x_=floor(j*ns.z);
  vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy;
  vec4 y=y_*ns.x+ns.yyyy;
  vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy);
  vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0;
  vec4 s1=floor(b1)*2.0+1.0;
  vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy;
  vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x);
  vec3 p1=vec3(a0.zw,h.y);
  vec3 p2=vec3(a1.xy,h.z);
  vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0);
  m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`;

  // One strand = a triangle strip along u (0..1) with two vertices per point (side -1 / +1); one instance per strand.
  const LINE_VS = `#version 300 es
precision highp float;
layout(location=0) in vec2 a_base;     // u, side
layout(location=1) in vec3 a_inst;     // bundle index, place in the bundle (0..1), random
uniform vec2 u_res;                    // render target size, px
uniform float u_px;                    // css px -> target px
uniform float u_aspect;                // width / height
uniform float u_t;                     // integrated phase (advances with the music's energy)
uniform float u_seed;
uniform float u_bundles;
uniform vec4 u_bands;                  // bass, low-mid, mid, high (0..1, smoothed)
uniform float u_beat;                  // 0..1, decays after each onset
uniform float u_amp, u_spread, u_width, u_spanY;
out float v_side; out float v_u; out float v_bri; out float v_b;
${NOISE}
// The path of one strand in "frame" units: x along the long side (-L..L), y across the short side (-0.5..0.5).
vec2 curve(float u, float b, float s, float r, float L){
  float x = mix(-L * 1.08, L * 1.08, u);
  float bn = (b + 0.5) / u_bundles;
  float base = (bn - 0.5) * u_spanY + 0.07 * sin(u_t * 0.13 + b * 2.1 + u_seed);
  float amp = u_amp * (0.35 + 0.95 * u_bands.x);
  float w = sin(x * 1.25 + u_t * 0.50 + b * 1.7 + u_seed) * 0.55
          + sin(x * 2.70 - u_t * 0.37 + b * 0.9 + u_seed * 1.3) * 0.30
          + sin(x * 5.10 + u_t * 0.81 + b * 2.3) * 0.15 * (0.5 + u_bands.y);
  float n = snoise(vec3(x * 0.55, b * 1.37 + u_seed, u_t * 0.11));
  float y = base + amp * (w * 0.6 + n * 0.75);
  // the strands of a bundle: parted by the mids, braided by a slower noise, pinched where the sine says so
  float sp = u_spread * (0.25 + 1.2 * u_bands.z) * (0.45 + 0.55 * sin(x * 1.9 + u_t * 0.6 + b * 3.1));
  float so = (s - 0.5) * sp + snoise(vec3(x * 2.2, s * 3.0 + b * 5.0, u_t * 0.23)) * sp * 0.35;
  so += snoise(vec3(x * 9.0, r * 20.0, u_t * 1.7)) * 0.0045 * u_bands.w;   // shimmer on the highs
  return vec2(x, y + so);
}
vec2 toClip(vec2 p, float L){
  return u_aspect >= 1.0 ? vec2(p.x / L, p.y * 2.0) : vec2(p.y * 2.0, p.x / L);
}
void main(){
  float L = (u_aspect >= 1.0 ? u_aspect : 1.0 / u_aspect) * 0.5;
  float u = a_base.x, side = a_base.y;
  float b = a_inst.x, s = a_inst.y, r = a_inst.z;
  vec2 c0 = toClip(curve(u, b, s, r, L), L);
  vec2 c1 = toClip(curve(u + 0.004, b, s, r, L), L);
  vec2 d = (c1 - c0) * u_res;
  vec2 nrm = normalize(vec2(-d.y, d.x) + vec2(1e-6, 0.0));
  float core = u_width * u_px * (0.55 + 0.9 * fract(r * 3.731)) * (1.0 + 0.4 * u_beat);   // core width, target px
  float halfQuad = core * 1.6 + 0.8 * u_px;                                            // core + soft edge
  gl_Position = vec4(c0 + nrm * side * halfQuad * 2.0 / u_res, 0.0, 1.0);
  v_side = side * halfQuad / max(core * 0.5, 0.5);        // distance from the centre in core half-widths
  v_u = u;
  v_b = b;
  float edge = 1.0 - abs(s - 0.5) * 1.1;                  // outer strands of a bundle a little dimmer
  float travel = 0.65 + 0.35 * sin(u * 12.0 + u_t * 1.3 + r * 6.283);   // light running along the strand
  float glint = 1.0 + u_bands.w * 0.9 * snoise(vec3(u * 6.0, r * 13.0, u_t * 2.0));
  v_bri = (0.3 + 0.7 * fract(r * 7.13)) * edge * travel * max(glint, 0.0);
}`;

  const LINE_FS = `#version 300 es
precision highp float;
in float v_side; in float v_u; in float v_bri; in float v_b;
uniform vec3 u_pal[6];
uniform int u_npal;
uniform float u_bright, u_t, u_level, u_beat;
out vec4 o;
void main(){
  float core = exp(-v_side * v_side * 1.3);
  int n = max(u_npal, 1);
  int i = int(mod(v_b, float(n)));
  int j = int(mod(v_b + 1.0, float(n)));
  float g = 0.5 + 0.5 * sin(v_u * 3.2 + u_t * 0.21 + v_b * 1.9);
  vec3 col = mix(u_pal[i], u_pal[j], g * 0.6);
  float k = u_bright * v_bri * (0.3 + 0.9 * u_level + 0.6 * u_beat);
  o = vec4(col * core * k, 1.0);
}`;

  const QUAD_VS = `#version 300 es
out vec2 v_uv;
void main(){
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  v_uv = p;
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

  const FADE_FS = `#version 300 es
precision highp float;
in vec2 v_uv; uniform sampler2D u_src; uniform float u_decay; out vec4 o;
void main(){ o = vec4(texture(u_src, v_uv).rgb * u_decay, 1.0); }`;

  const DOWN_FS = `#version 300 es
precision highp float;
in vec2 v_uv; uniform sampler2D u_src; uniform vec2 u_texel; out vec4 o;
void main(){
  vec2 h = u_texel;
  vec3 c = texture(u_src, v_uv).rgb * 4.0
    + texture(u_src, v_uv - h).rgb + texture(u_src, v_uv + h).rgb
    + texture(u_src, v_uv + vec2(h.x, -h.y)).rgb + texture(u_src, v_uv - vec2(h.x, -h.y)).rgb;
  o = vec4(c / 8.0, 1.0);
}`;

  const UP_FS = `#version 300 es
precision highp float;
in vec2 v_uv; uniform sampler2D u_src; uniform vec2 u_texel; out vec4 o;
void main(){
  vec2 h = u_texel;
  vec3 c = texture(u_src, v_uv + vec2(-2.0 * h.x, 0.0)).rgb
    + texture(u_src, v_uv + vec2(-h.x, h.y)).rgb * 2.0
    + texture(u_src, v_uv + vec2(0.0, 2.0 * h.y)).rgb
    + texture(u_src, v_uv + vec2(h.x, h.y)).rgb * 2.0
    + texture(u_src, v_uv + vec2(2.0 * h.x, 0.0)).rgb
    + texture(u_src, v_uv + vec2(h.x, -h.y)).rgb * 2.0
    + texture(u_src, v_uv + vec2(0.0, -2.0 * h.y)).rgb
    + texture(u_src, v_uv + vec2(-h.x, -h.y)).rgb * 2.0;
  o = vec4(c / 12.0, 1.0);
}`;

  const COMPOSITE_FS = `#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_scene, u_bloom;
uniform float u_bloomK, u_exposure, u_time;
uniform vec3 u_bg;
uniform vec2 u_res;
out vec4 o;
void main(){
  vec3 c = texture(u_scene, v_uv).rgb + texture(u_bloom, v_uv).rgb * u_bloomK;
  c = vec3(1.0) - exp(-c * u_exposure);
  vec2 q = (v_uv - 0.5) * vec2(u_res.x / u_res.y, 1.0);
  float r = length(q);
  c += u_bg * clamp(0.9 - 0.8 * r, 0.0, 1.0);
  c *= 1.0 - 0.35 * smoothstep(0.45, 1.15, r);
  float h = fract(sin(dot(gl_FragCoord.xy + fract(u_time) * 97.0, vec2(12.9898, 78.233))) * 43758.5453);
  o = vec4(c + (h - 0.5) / 255.0, 1.0);
}`;

  function mulberry32(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      let t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  const DEFAULT_PALETTE = [[1.0, 0.18, 0.42], [0.75, 0.35, 0.95], [0.37, 0.36, 0.9], [0.39, 0.82, 1.0], [1.0, 0.62, 0.04]];

  const DEFAULTS = {
    bundles: 7,          // ribbons
    strands: 48,         // lines per ribbon
    points: 180,         // points per line
    width: 1.1,          // core width of a line, css px
    amp: 0.24,           // wave height (short-side units) at full bass
    spread: 0.14,        // how far the strands of a ribbon part at full mids
    spanY: 0.62,         // how much of the short side the ribbons spread over
    bright: 0.075,       // light per strand (they add up)
    bloom: 1.0,
    exposure: 1.6,
    trail: 0.0,          // 0 = none, 0.8 = long silky trails
    speed: 1.0,
    beatKick: 0.6,       // extra phase on a beat
    bgTint: 0.05,        // cover colour behind the lines
    delay: 0,            // ms: the picture is computed from the features of this long ago (output latency, bridge queue)
    renderScale: 1.0,    // 1 = full canvas resolution
    autoScale: true,     // lower the render scale when frames get slow
    maxDpr: 2,
  };

  function create(canvas, opts) {
    opts = opts || {};
    const gl = canvas.getContext('webgl2', { alpha: false, antialias: false, depth: false, stencil: false, powerPreference: 'high-performance' });
    if (!gl) throw new Error('WebGL2 is not available');
    const floatOk = !!gl.getExtension('EXT_color_buffer_float');
    const params = Object.assign({}, DEFAULTS, opts.params || {});
    let features = opts.features || null;

    function compile(type, src) {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error('shader: ' + gl.getShaderInfoLog(sh));
      return sh;
    }
    function program(vs, fs) {
      const p = gl.createProgram();
      gl.attachShader(p, compile(gl.VERTEX_SHADER, vs));
      gl.attachShader(p, compile(gl.FRAGMENT_SHADER, fs));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error('link: ' + gl.getProgramInfoLog(p));
      const u = {};
      const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
      for (let i = 0; i < n; i++) {
        const info = gl.getActiveUniform(p, i);
        u[info.name.replace(/\[0\]$/, '')] = gl.getUniformLocation(p, info.name);
      }
      return { p, u };
    }
    const P = {
      line: program(LINE_VS, LINE_FS),
      fade: program(QUAD_VS, FADE_FS),
      down: program(QUAD_VS, DOWN_FS),
      up: program(QUAD_VS, UP_FS),
      comp: program(QUAD_VS, COMPOSITE_FS),
    };
    const emptyVao = gl.createVertexArray();

    // ---- geometry
    const lineVao = gl.createVertexArray();
    const baseBuf = gl.createBuffer(), instBuf = gl.createBuffer();
    let geo = { points: 0, bundles: 0, strands: 0, count: 0, seed: -1 };
    let seed = 1;
    function buildGeometry() {
      const pts = Math.max(16, params.points | 0), nb = Math.max(1, params.bundles | 0), ns = Math.max(1, params.strands | 0);
      if (geo.points === pts && geo.bundles === nb && geo.strands === ns && geo.seed === seed) return;
      const base = new Float32Array(pts * 4);
      for (let i = 0; i < pts; i++) {
        const u = i / (pts - 1);
        base.set([u, -1, u, 1], i * 4);
      }
      const rnd = mulberry32(seed * 9301 + 49297);
      const inst = new Float32Array(nb * ns * 3);
      let k = 0;
      for (let b = 0; b < nb; b++) {
        for (let s = 0; s < ns; s++) {
          inst[k++] = b;
          inst[k++] = Math.min(1, Math.max(0, (s + 0.5 + (rnd() - 0.5) * 0.8) / ns));
          inst[k++] = rnd();
        }
      }
      gl.bindVertexArray(lineVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, baseBuf);
      gl.bufferData(gl.ARRAY_BUFFER, base, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, instBuf);
      gl.bufferData(gl.ARRAY_BUFFER, inst, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(1);
      gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
      gl.vertexAttribDivisor(1, 1);
      gl.bindVertexArray(null);
      geo = { points: pts, bundles: nb, strands: ns, count: nb * ns, seed };
    }

    // ---- render targets
    let rt = null;
    function target(w, h) {
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      if (floatOk) gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, null);
      else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      const fb = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      gl.clearColor(0, 0, 0, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      return { tex, fb, w, h };
    }
    function freeTargets() {
      if (!rt) return;
      for (const t of [rt.a, rt.b, ...rt.bloom]) { gl.deleteTexture(t.tex); gl.deleteFramebuffer(t.fb); }
      rt = null;
    }
    let scale = params.renderScale;
    function ensureTargets() {
      const dpr = Math.min(window.devicePixelRatio || 1, params.maxDpr);
      const cw = Math.max(1, Math.round(canvas.clientWidth * dpr));
      const ch = Math.max(1, Math.round(canvas.clientHeight * dpr));
      if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
      const sw = Math.max(1, Math.round(cw * scale)), sh = Math.max(1, Math.round(ch * scale));
      if (rt && rt.w === sw && rt.h === sh) return rt;
      freeTargets();
      const bloom = [];
      let bw = sw, bh = sh;
      for (let i = 0; i < 5; i++) {
        bw = Math.max(1, bw >> 1); bh = Math.max(1, bh >> 1);
        bloom.push(target(bw, bh));
        if (bw < 8 || bh < 8) break;
      }
      rt = { a: target(sw, sh), b: target(sw, sh), bloom, w: sw, h: sh, dpr };
      return rt;
    }

    // ---- state
    const pal = DEFAULT_PALETTE.map((c) => c.slice());
    let palTarget = DEFAULT_PALETTE.map((c) => c.slice());
    const IDLE = { bass: 0, lowmid: 0, mid: 0, high: 0, level: 0, levelSlow: 0, beat: 0 };
    let phase = 0, raf = 0, last = 0, running = false;
    const hist = [];                                    // [time, features] for params.delay
    let shown = IDLE;                                   // the features this frame was drawn from (after the delay)
    const perf = { fps: 0, ms: 0, frames: 0, acc: 0, slow: 0, fast: 0 };

    function drawQuad(prog) { gl.useProgram(prog.p); gl.bindVertexArray(emptyVao); gl.drawArrays(gl.TRIANGLES, 0, 3); }
    function bindTex(prog, name, unit, tex) {
      gl.activeTexture(gl.TEXTURE0 + unit);
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.uniform1i(prog.u[name], unit);
    }

    function frame(ts) {
      if (!running) return;
      raf = requestAnimationFrame(frame);
      const now = ts / 1000;
      const dt = last ? Math.min(0.1, Math.max(0, now - last)) : 1 / 60;
      last = now;
      // frame-time governor: ~1 s windows; below ~45 fps the render scale steps down, with headroom it steps back up
      perf.frames++; perf.acc += dt;
      if (perf.acc >= 1) {
        perf.fps = perf.frames / perf.acc; perf.ms = 1000 * perf.acc / perf.frames;
        if (params.autoScale) {
          if (perf.fps < 45) { perf.slow++; perf.fast = 0; } else if (perf.fps > 57) { perf.fast++; perf.slow = 0; } else { perf.slow = perf.fast = 0; }
          if (perf.slow >= 2 && scale > 0.5) { scale = Math.max(0.5, scale - 0.25); perf.slow = 0; }
          if (perf.fast >= 5 && scale < params.renderScale) { scale = Math.min(params.renderScale, scale + 0.25); perf.fast = 0; }
        } else scale = params.renderScale;
        perf.frames = 0; perf.acc = 0;
      }
      // Delay: the analyser hears the music before the speakers play it (the output's latency; on the bridge route also
      // the app's queue). Instead of holding finished frames back (a queue of whole pictures), the picture is computed
      // from the features of `delay` ms ago: the same result, at the cost of a small history of numbers.
      const raw = features ? features(now, dt) : null;
      let f = raw || IDLE;
      if (raw && params.delay > 0) {
        hist.push([now, Object.assign({}, raw)]);      // a copy: a source may hand out the same object every frame
        const cut = now - params.delay / 1000;
        while (hist.length > 1 && hist[1][0] <= cut) hist.shift();
        f = hist[0][1];
      } else hist.length = 0;
      shown = f;
      const kp = 1 - Math.exp(-dt / 1.2);               // palette follows a new cover over ~1-2 s
      for (let i = 0; i < pal.length; i++) for (let c = 0; c < 3; c++) pal[i][c] += ((palTarget[i] || palTarget[0])[c] - pal[i][c]) * kp;
      phase += dt * params.speed * (0.22 + 0.6 * f.levelSlow + 0.35 * f.bass) + dt * f.beat * params.beatKick;
      buildGeometry();
      const t = ensureTargets();
      const cur = t.a, prev = t.b;

      // 1. lines (on top of the faded previous frame when trails are on)
      gl.bindFramebuffer(gl.FRAMEBUFFER, cur.fb);
      gl.viewport(0, 0, cur.w, cur.h);
      gl.disable(gl.BLEND);
      if (params.trail > 0) {
        gl.useProgram(P.fade.p);
        bindTex(P.fade, 'u_src', 0, prev.tex);
        gl.uniform1f(P.fade.u.u_decay, Math.min(0.97, params.trail));
        drawQuad(P.fade);
      } else {
        gl.clearColor(0, 0, 0, 1);
        gl.clear(gl.COLOR_BUFFER_BIT);
      }
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);
      const L = P.line;
      gl.useProgram(L.p);
      gl.uniform2f(L.u.u_res, cur.w, cur.h);
      gl.uniform1f(L.u.u_px, t.dpr * scale);
      gl.uniform1f(L.u.u_aspect, canvas.width / canvas.height);
      gl.uniform1f(L.u.u_t, phase);
      gl.uniform1f(L.u.u_seed, (seed % 997) * 0.1371);
      gl.uniform1f(L.u.u_bundles, geo.bundles);
      gl.uniform4f(L.u.u_bands, f.bass, f.lowmid, f.mid, f.high);
      gl.uniform1f(L.u.u_beat, f.beat);
      gl.uniform1f(L.u.u_amp, params.amp);
      gl.uniform1f(L.u.u_spread, params.spread);
      gl.uniform1f(L.u.u_width, params.width);
      gl.uniform1f(L.u.u_spanY, params.spanY);
      gl.uniform3fv(L.u.u_pal, new Float32Array(pal.slice(0, 6).flat()));
      gl.uniform1i(L.u.u_npal, Math.min(6, pal.length));
      gl.uniform1f(L.u.u_bright, params.bright);
      gl.uniform1f(L.u.u_level, f.level);
      gl.bindVertexArray(lineVao);
      gl.drawArraysInstanced(gl.TRIANGLE_STRIP, 0, geo.points * 2, geo.count);
      gl.disable(gl.BLEND);

      // 2. bloom: down the chain, then up again, each level added onto the one above
      let src = cur;
      for (const b of t.bloom) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, b.fb);
        gl.viewport(0, 0, b.w, b.h);
        gl.useProgram(P.down.p);
        bindTex(P.down, 'u_src', 0, src.tex);
        gl.uniform2f(P.down.u.u_texel, 1 / src.w, 1 / src.h);
        drawQuad(P.down);
        src = b;
      }
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);
      for (let i = t.bloom.length - 1; i > 0; i--) {
        const from = t.bloom[i], to = t.bloom[i - 1];
        gl.bindFramebuffer(gl.FRAMEBUFFER, to.fb);
        gl.viewport(0, 0, to.w, to.h);
        gl.useProgram(P.up.p);
        bindTex(P.up, 'u_src', 0, from.tex);
        gl.uniform2f(P.up.u.u_texel, 1 / from.w, 1 / from.h);
        drawQuad(P.up);
      }
      gl.disable(gl.BLEND);

      // 3. onto the canvas
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.useProgram(P.comp.p);
      bindTex(P.comp, 'u_scene', 0, cur.tex);
      bindTex(P.comp, 'u_bloom', 1, t.bloom[0].tex);
      gl.uniform1f(P.comp.u.u_bloomK, params.bloom);
      gl.uniform1f(P.comp.u.u_exposure, params.exposure);
      gl.uniform1f(P.comp.u.u_time, now);
      const bg = pal[0];
      gl.uniform3f(P.comp.u.u_bg, bg[0] * params.bgTint, bg[1] * params.bgTint, bg[2] * params.bgTint);
      gl.uniform2f(P.comp.u.u_res, canvas.width, canvas.height);
      drawQuad(P.comp);

      t.a = prev; t.b = cur;              // this frame is the next one's "previous"
    }

    const api = {
      params,
      start() { if (running) return; running = true; last = 0; raf = requestAnimationFrame(frame); },
      stop() { running = false; cancelAnimationFrame(raf); },
      get running() { return running; },
      setFeatures(fn) { features = fn; },
      shownFeatures() { return shown; },
      setPalette(list) { if (list && list.length) palTarget = list.slice(0, 6).map((c) => c.slice()); while (pal.length < palTarget.length) pal.push(palTarget[pal.length].slice()); pal.length = Math.max(palTarget.length, 1); },
      setSeed(n) { seed = (n >>> 0) || 1; },
      stats() { return { fps: Math.round(perf.fps), ms: +perf.ms.toFixed(1), scale, strands: geo.count, size: rt ? rt.w + 'x' + rt.h : '', float: floatOk }; },
      destroy() { api.stop(); freeTargets(); },
    };
    return api;
  }

  // ---- audio features from an AnalyserNode -------------------------------------------------------------------------
  // Band energies with an automatic gain per band (quiet masters still move the picture), envelopes with a fast attack
  // and a slow release, and onsets from the spectral flux above an adaptive threshold. Onsets are at most ~3 per second
  // (photosensitivity: no strobing). The latency to the speakers is made up by the visualizer's `delay` parameter.
  function audioFeatures(analyser) {
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0;
    const n = analyser.frequencyBinCount;
    const db = new Float32Array(n), prevMag = new Float32Array(n);
    const hz = analyser.context.sampleRate / analyser.fftSize;
    const edges = [[25, 150], [150, 500], [500, 2000], [2000, 10000]].map(([a, b]) => [Math.max(1, Math.round(a / hz)), Math.min(n - 1, Math.round(b / hz))]);
    const fluxTop = Math.min(n - 1, Math.round(5000 / hz));
    const peak = [1e-7, 1e-7, 1e-7, 1e-7], env = [0, 0, 0, 0];
    let levelPeak = 1e-7, level = 0, levelSlow = 0, fluxPeak = 1e-6, beat = 0, lastBeat = -1;
    const fluxHist = [];
    const fn = function (now, dt) {
      analyser.getFloatFrequencyData(db);
      const out = {};
      let total = 0;
      for (let i = 0; i < 4; i++) {
        const [a, b] = edges[i];
        let p = 0;
        for (let k = a; k <= b; k++) p += Math.pow(10, db[k] / 10);
        p /= (b - a + 1);
        total += p;
        peak[i] = Math.max(p, peak[i] * Math.exp(-dt / 4), 1e-7);
        const v = p < 1e-9 ? 0 : Math.min(1, Math.sqrt(p / peak[i]));
        env[i] += (v - env[i]) * (1 - Math.exp(-dt / (v > env[i] ? 0.03 : 0.3)));
      }
      levelPeak = Math.max(total, levelPeak * Math.exp(-dt / 6), 1e-7);
      const lv = total < 1e-9 ? 0 : Math.min(1, Math.sqrt(total / levelPeak));
      level += (lv - level) * (1 - Math.exp(-dt / (lv > level ? 0.05 : 0.4)));
      levelSlow += (lv - levelSlow) * (1 - Math.exp(-dt / 3));
      let flux = 0;
      for (let k = 1; k <= fluxTop; k++) {
        const m = Math.pow(10, db[k] / 20) || 0;
        const d = m - prevMag[k];
        if (d > 0) flux += d * (k < edges[0][1] ? 2 : 1);   // the bass carries the beat
        prevMag[k] = m;
      }
      fluxPeak = Math.max(flux, fluxPeak * Math.exp(-dt / 5), 1e-6);
      fluxHist.push(flux);
      if (fluxHist.length > 60) fluxHist.shift();
      const sorted = fluxHist.slice().sort((x, y) => x - y);
      const thr = sorted[sorted.length >> 1] * 1.6 + 0.08 * fluxPeak;
      if (flux > thr && flux > 0.2 * fluxPeak && now - lastBeat > 0.3) { beat = 1; lastBeat = now; }
      else beat *= Math.exp(-dt / 0.16);
      out.bass = env[0]; out.lowmid = env[1]; out.mid = env[2]; out.high = env[3];
      out.level = level; out.levelSlow = levelSlow; out.beat = beat;
      return out;
    };
    return fn;
  }

  // ---- simulated features (no audio needed): a 4/4 groove with a breakdown every 8 bars -----------------------------
  function simulatedFeatures(bpm) {
    bpm = bpm || 124;
    const env = { bass: 0, lowmid: 0, mid: 0, high: 0, level: 0, levelSlow: 0, beat: 0 };
    let lastBeatIdx = -1;
    return function (now, dt) {
      const beatLen = 60 / bpm;
      const idx = Math.floor(now / beatLen);
      const bar = Math.floor(idx / 4);
      const breakdown = bar % 8 >= 6;
      const pos = (now % beatLen) / beatLen;
      if (idx !== lastBeatIdx) { lastBeatIdx = idx; if (!breakdown) env.beat = 1; }
      env.beat *= Math.exp(-dt / 0.16);
      const kick = breakdown ? 0 : Math.exp(-pos * 7);
      const hat = breakdown ? 0.1 : Math.exp(-Math.abs(pos - 0.5) * 18) * 0.9;
      const tgt = {
        bass: breakdown ? 0.15 : 0.35 + 0.65 * kick,
        lowmid: 0.4 + 0.2 * Math.sin(now * 0.7),
        mid: (breakdown ? 0.65 : 0.4) + 0.25 * Math.sin(now * 0.31),
        high: hat,
        level: breakdown ? 0.45 : 0.75 + 0.2 * kick,
      };
      for (const k of Object.keys(tgt)) env[k] += (tgt[k] - env[k]) * (1 - Math.exp(-dt / (tgt[k] > env[k] ? 0.03 : 0.3)));
      env.levelSlow += (env.level - env.levelSlow) * (1 - Math.exp(-dt / 3));
      return env;
    };
  }

  // ---- palette from a cover: k-means over a 32x32 copy, the clusters lifted to luminous colours ---------------------
  function rgbToHsl(r, g, b) {
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    if (mx === mn) return [0, 0, l];
    const d = mx - mn, s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    const h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [h / 6, s, l];
  }
  function hslToRgb(h, s, l) {
    if (!s) return [l, l, l];
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
    const f = (t) => { t = (t + 1) % 1; return t < 1 / 6 ? p + (q - p) * 6 * t : t < 0.5 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p; };
    return [f(h + 1 / 3), f(h), f(h - 1 / 3)];
  }
  function paletteFromImage(img, k) {
    k = k || 5;
    const S = 32;
    const c = document.createElement('canvas');
    c.width = c.height = S;
    const x = c.getContext('2d', { willReadFrequently: true });
    x.drawImage(img, 0, 0, S, S);
    const d = x.getImageData(0, 0, S, S).data;
    const px = [];
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] < 128) continue;
      const r = d[i] / 255, g = d[i + 1] / 255, b = d[i + 2] / 255;
      const [, s, l] = rgbToHsl(r, g, b);
      px.push([r, g, b, 0.15 + s * (1 - Math.abs(2 * l - 1))]);     // weight: colourful pixels count more than grey/black/white
    }
    if (!px.length) return DEFAULT_PALETTE.map((c) => c.slice());
    // k-means++ style start: the most colourful pixel, then each time the pixel farthest from the chosen ones
    const cent = [px.reduce((a, p) => (p[3] > a[3] ? p : a)).slice(0, 3)];
    const dist = (p, q) => (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2;
    while (cent.length < k) {
      let best = null, bd = -1;
      for (const p of px) { const m = Math.min(...cent.map((q) => dist(p, q))) * p[3]; if (m > bd) { bd = m; best = p; } }
      cent.push(best.slice(0, 3));
    }
    let wsum = new Array(k).fill(0);
    for (let it = 0; it < 8; it++) {
      const acc = cent.map(() => [0, 0, 0]);
      wsum = new Array(k).fill(0);
      for (const p of px) {
        let bi = 0, bd = Infinity;
        for (let i = 0; i < k; i++) { const m = dist(p, cent[i]); if (m < bd) { bd = m; bi = i; } }
        acc[bi][0] += p[0] * p[3]; acc[bi][1] += p[1] * p[3]; acc[bi][2] += p[2] * p[3]; wsum[bi] += p[3];
      }
      for (let i = 0; i < k; i++) if (wsum[i]) cent[i] = acc[i].map((v) => v / wsum[i]);
    }
    return cent.map((c, i) => [c, wsum[i]]).sort((a, b) => b[1] - a[1]).map(([c]) => {
      const [h, s, l] = rgbToHsl(c[0], c[1], c[2]);
      return hslToRgb(h, s < 0.08 ? s : Math.min(1, Math.max(0.6, s * 1.3)), Math.min(0.68, Math.max(0.52, l)));
    });
  }

  window.MusicVisualizer = { create, audioFeatures, simulatedFeatures, paletteFromImage, DEFAULT_PALETTE, DEFAULTS };
})();
