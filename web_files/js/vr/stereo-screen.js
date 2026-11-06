/* The screen that shows one item in front of the viewer: a converted side-by-side video or picture
 * ("sbs": two planes, the left eye's on layer 1 showing u 0..0.5, the right eye's on layer 2 showing
 * u 0.5..1) or the 2D original on the same screen ("mono": one plane on layer 0, both eyes see it).
 * Same distance (constant angular size), zoom, pan, seek and play/pause either way.
 *
 *   const s = createStereoScreen({ renderer, item, onStatus });   // item from itemDisplay()
 *   scene.add(s.group); await s.ready;                            // s.failed = text when it could not load
 *   s.mount();                                                    // after ready: size, place, texture window
 *   frame: const pass = s.update({ dt, hands });                  // handles toggle/seek/distance/zoom/grip,
 *                                                                 // returns the actions for the app: recenter, exit, next
 *   s.recenter(head, forward);                                    // -> pose { x, y, z, yaw } of the screen's frame
 *   s.dispose();
 * The controller mapping and the geometry are the pure functions of js/vr-logic.js (unit-tested). */
import * as THREE from "../vendor/three.module.min.js";
import {
  DEFAULT_DISTANCE, clampDistance, clampZoom, eyeAspect, screenSize, uvWindow, clampPan, panDelta, zoomPanAt,
  recenterPose, createController, seekTarget,
} from "../vr-logic.js";
import { monoAspect, monoUvWindow, VideoStats } from "./logic.js";
import { D } from "./host.js";

// The texture window every eye mesh shows. A video is always the full eye half (zoom 1, no pan);
// only a picture zooms and pans. Exported for the test.
export function applyEyeWindows(meshes, kind, zoom, pan) {
  const z = kind === "picture" ? zoom : 1;
  const pn = kind === "picture" ? pan : { x: 0, y: 0 };
  meshes.forEach((m, eye) => {
    const u = uvWindow(eye, z, pn);
    const t = m.material.map;
    t.repeat.set(u.repeatX, u.repeatY);
    t.offset.set(u.offsetX, u.offsetY);
  });
}

// The same for a 2D screen: one mesh, the whole texture.
export function applyMonoWindow(mesh, kind, zoom, pan) {
  const z = kind === "picture" ? zoom : 1;
  const pn = kind === "picture" ? pan : { x: 0, y: 0 };
  const u = monoUvWindow(z, pn);
  const t = mesh.material.map;
  t.repeat.set(u.repeatX, u.repeatY);
  t.offset.set(u.offsetX, u.offsetY);
}

const MEDIA_TIMEOUT_MS = 20000;

// Animated pictures (GIF, animated WebP): three's TextureLoader uploads an <img> once, so only the first frame
// showed. Where the browser has ImageDecoder, such a file is decoded frame by frame into a canvas texture instead
// (decoded frames are already composited); tickAnimation() advances it. Exported for the test.
const ANIMATABLE_RE = /\.(gif|webp)(?:$|[?&#])/i;
export function looksAnimatable(url) {
  let u = String(url || "");
  try { u = decodeURIComponent(u); } catch (e) { /* keep the raw string */ }
  return ANIMATABLE_RE.test(u);
}
const MIN_FRAME_MS = 20;              // browsers show GIF delays of 0-10 ms as 100 ms
const DEFAULT_FRAME_MS = 100;
export function frameDelayMs(durationUs) {
  const ms = Number(durationUs) / 1000;
  if (!(ms > 0)) return DEFAULT_FRAME_MS;
  return ms < MIN_FRAME_MS ? DEFAULT_FRAME_MS : ms;
}

async function openAnimation(url) {
  if (typeof ImageDecoder === "undefined" || !looksAnimatable(url)) return null;
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const blob = await res.blob();
  const type = blob.type && blob.type.startsWith("image/") ? blob.type : (/\.webp/i.test(url) ? "image/webp" : "image/gif");
  if (ImageDecoder.isTypeSupported && !(await ImageDecoder.isTypeSupported(type))) return { blob };
  const decoder = new ImageDecoder({ data: await blob.arrayBuffer(), type });
  try {
    await decoder.tracks.ready;                      // selectedTrack is null before this
    await decoder.completed;
    const track = decoder.tracks.selectedTrack;
    if (!track || !track.animated || !(track.frameCount > 1)) { decoder.close(); return { blob }; }
    const first = await decoder.decode({ frameIndex: 0 });
    const frame = first.image;
    const canvas = document.createElement("canvas");
    canvas.width = frame.displayWidth; canvas.height = frame.displayHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(frame, 0, 0);
    const delay = frameDelayMs(frame.duration);
    frame.close();
    return { anim: { decoder, canvas, ctx, count: track.frameCount, index: 0, due: 0, delay, pending: false, closed: false } };
  } catch (e) {
    decoder.close();
    throw e;
  }
}

function makeContent(item, renderer) {
  const kind = item.kind === "picture" ? "picture" : "video";
  const mode = item.mode === "mono" ? "mono" : "sbs";
  const c = { kind, mode, key: item.key, name: item.name || "", aspect: mode === "mono" ? monoAspect(item.eyeW, item.eyeH) : eyeAspect(item.eyeW, item.eyeH), meshes: [], video: null, tex: null, textures: [], failed: null, requested: item.url, uploaded: [] };
  const geo = new THREE.PlaneGeometry(1, 1);
  const mats = [];
  c.metaAspect = item.eyeW > 0 && item.eyeH > 0;      // else taken from the decoded media
  c.group = new THREE.Group();
  c.dispose = () => {
    if (c.stats) { c.stats.dispose(); c.stats = null; }
    if (c.video) { c.video.pause(); c.video.removeAttribute("src"); c.video.load(); }
    if (c.anim && !c.anim.closed) { c.anim.closed = true; try { c.anim.decoder.close(); } catch (e) { /* already closed */ } }
    for (const t of c.textures) t.dispose();
    c.tex && c.tex.dispose();
    geo.dispose();
    mats.forEach((m) => m.dispose());
    if (c.group.parent) c.group.parent.remove(c.group);
  };

  const planeMesh = (tex, layer) => {
    // sbs: every eye gets its own texture object (own repeat/offset) over the one decoded image/video.
    const t = layer === 0 ? tex : tex.clone();
    t.needsUpdate = true;
    if (layer !== 0) c.textures.push(t);
    // V2c: the textures that are really uploaded (sbs: one clone per eye, each with its own requestVideoFrameCallback and upload).
    if (kind === "video" && c.stats) { t.onUpdate = c.stats.onTex; c.stats.texN += 1; c.uploaded.push(t); }
    const m = new THREE.MeshBasicMaterial({ map: t, toneMapped: false });
    mats.push(m);
    const mesh = new THREE.Mesh(geo, m);
    mesh.layers.set(layer);          // three.js: layer 1 = left eye, layer 2 = right eye in XR; 0 = both
    c.group.add(mesh);
    return mesh;
  };
  const meshesFor = (tex) => (mode === "mono" ? [planeMesh(tex, 0)] : [planeMesh(tex, 1), planeMesh(tex, 2)]);

  c.ready = new Promise((resolve) => {
    if (kind === "video") {
      const v = document.createElement("video");
      v.loop = true;
      v.muted = false;               // the page's click is the user gesture
      v.playsInline = true;
      v.crossOrigin = "anonymous";
      v.preload = "auto";
      v.src = item.url;
      c.video = v;
      c.stats = new VideoStats(v, { fps: item.fps, sourceFps: item.sourceFps, interpolated: item.interpolated, codec: item.codec }, typeof navigator !== "undefined" ? navigator : null);
      c.tex = new THREE.VideoTexture(v);
      c.tex.colorSpace = THREE.SRGBColorSpace;
      c.meshes = meshesFor(c.tex);
      v.addEventListener("loadedmetadata", () => {
        // Sound only: the browser dropped a video track it cannot decode.
        if (!v.videoWidth) c.failed = mode === "mono" ? "This browser cannot decode this video." : "This browser cannot decode the 3D video (HEVC).";
        if (!c.metaAspect) c.aspect = mode === "mono" ? monoAspect(v.videoWidth, v.videoHeight) : eyeAspect(0, 0, v.videoWidth, v.videoHeight);
        resolve();
      }, { once: true });
      v.addEventListener("error", () => {
        c.failed = mode === "mono" ? "The video could not be loaded or decoded." : "The 3D video could not be loaded or decoded.";
        resolve();
      }, { once: true });
    } else {
      const fail = () => {
        c.failed = mode === "mono" ? "The picture could not be loaded." : "The 3D picture could not be loaded.";
        resolve();
      };
      const still = (url, revoke) => {
        new THREE.TextureLoader().load(url, (tex) => {
          if (revoke) URL.revokeObjectURL(url);
          tex.colorSpace = THREE.SRGBColorSpace;
          tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
          c.tex = tex;
          if (!c.metaAspect && tex.image) c.aspect = mode === "mono" ? monoAspect(tex.image.width, tex.image.height) : eyeAspect(0, 0, tex.image.width, tex.image.height);
          c.meshes = meshesFor(tex);
          resolve();
        }, undefined, () => { if (revoke) URL.revokeObjectURL(url); fail(); });
      };
      openAnimation(item.url).then((r) => {
        if (r && r.anim) {
          const a = r.anim;
          const tex = new THREE.CanvasTexture(a.canvas);
          tex.colorSpace = THREE.SRGBColorSpace;
          tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
          c.tex = tex;
          c.anim = a;
          if (!c.metaAspect) c.aspect = mode === "mono" ? monoAspect(a.canvas.width, a.canvas.height) : eyeAspect(0, 0, a.canvas.width, a.canvas.height);
          c.meshes = meshesFor(tex);
          D("animated picture", a.count, "frames");
          resolve();
        } else if (r && r.blob) still(URL.createObjectURL(r.blob), true);   // fetched already, not animated
        else still(item.url, false);
      }, (e) => { D("animation decode failed", String(e && e.message || e)); still(item.url, false); });
    }
  });
  return c;
}

const RVFC_STALL_MS = 300;

// Advance an animated picture: once the current frame's delay has run out, decode the next one (async, one at a
// time) into the canvas and flag every texture over it for upload. A slow decode delays the frame, it never skips.
function tickAnimation(c) {
  const a = c.anim;
  if (!a || a.closed || a.pending) return;
  const now = performance.now();
  if (!a.due) { a.due = now + a.delay; return; }
  if (now < a.due) return;
  a.pending = true;
  const next = (a.index + 1) % a.count;
  a.decoder.decode({ frameIndex: next }).then((r) => {
    const f = r.image;
    if (!a.closed) {
      a.ctx.clearRect(0, 0, a.canvas.width, a.canvas.height);
      a.ctx.drawImage(f, 0, 0, a.canvas.width, a.canvas.height);
      a.index = next;
      a.delay = frameDelayMs(f.duration);
      const t = performance.now();
      a.due = Math.max(a.due + a.delay, t);          // keep the GIF's own pace, but never queue a burst after a stall
      if (c.tex) c.tex.needsUpdate = true;
      for (const tx of c.textures) tx.needsUpdate = true;
    }
    f.close();
    a.pending = false;
  }, (e) => {
    a.pending = false;
    a.closed = true;                                 // keep the frame on show, stop trying
    try { a.decoder.close(); } catch (e2) { /* already closed */ }
    D("animation frame failed", String(e && e.message || e));
  });
}

export function createStereoScreen({ renderer, item, onStatus, dist = DEFAULT_DISTANCE }) {
  const status = typeof onStatus === "function" ? onStatus : () => {};
  const content = makeContent(item, renderer);
  const group = new THREE.Group();            // positioned at the head on recenter, rotated by yaw
  group.add(content.group);
  const input = createController({ dist });
  let everyFrame = false;
  let scale = 1;                              // V2d: the slider's size factor, composes with the distance (and the picture zoom, a texture window)
  const state = { pan: { x: 0, y: 0 }, yaw: 0, grips: new Map(), mounted: false, disposed: false, zoomShown: 1, rvfcShown: 0, rvfcAt: 0, rvfcTime: -1 };
  const inv = new THREE.Quaternion();
  const tmp = new THREE.Vector3();
  const invWorld = new THREE.Matrix4();
  const rayO = new THREE.Vector3();
  const rayP = new THREE.Vector3();
  // Where a hand's ray meets the screen, in screen fractions from its centre (-0.5..0.5, +y up), or null when the
  // ray points away from it or along it. content.group is a 1x1 plane scaled to the screen, so its local frame is
  // exactly those fractions. Off the screen the point is clamped to its edge.
  const pointAt = (hand) => {
    if (!hand || !hand.origin || !hand.dir) return null;
    content.group.updateWorldMatrix(true, false);
    invWorld.copy(content.group.matrixWorld).invert();
    rayO.copy(hand.origin).applyMatrix4(invWorld);
    rayP.copy(hand.origin).add(hand.dir).applyMatrix4(invWorld);
    const dz = rayP.z - rayO.z;
    if (!(Math.abs(dz) > 1e-9)) return null;
    const t = -rayO.z / dz;
    if (!(t > 0)) return null;
    const x = rayO.x + (rayP.x - rayO.x) * t;
    const y = rayO.y + (rayP.y - rayO.y) * t;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return { x: Math.min(0.5, Math.max(-0.5, x)), y: Math.min(0.5, Math.max(-0.5, y)) };
  };

  const placeScreen = () => {
    const { w, h } = screenSize(content.aspect, input.dist);
    content.group.scale.set(w * scale, h * scale, 1);
    content.group.position.set(0, 0, -clampDistance(input.dist));
  };
  const applyUv = () => {
    if (content.kind === "picture") state.pan = clampPan(state.pan, input.zoom);
    state.zoomShown = clampZoom(input.zoom);
    if (content.mode === "mono") applyMonoWindow(content.meshes[0], content.kind, input.zoom, state.pan);
    else applyEyeWindows(content.meshes, content.kind, input.zoom, state.pan);
  };

  const play = async () => {
    if (content.kind !== "video" || !content.video) return;
    try { await content.video.play(); }
    catch (e) {
      // Autoplay with sound refused: start muted rather than not at all.
      content.video.muted = true;
      try { await content.video.play(); status("Playing without sound (the browser blocked audio)."); } catch (e2) { /* shown as paused */ }
    }
  };

  const screen = {
    group, content, input,
    get kind() { return content.kind; },
    get mode() { return content.mode; },
    get key() { return content.key; },
    get aspect() { return content.aspect; },
    get failed() { return content.failed; },
    get mounted() { return state.mounted; },
    get zoom() { return clampZoom(input.zoom); },
    get dist() { return input.dist; },
    get scale() { return scale; },
    // The screen's current size in metres (tests, diagnostics).
    get size() { return { w: content.group.scale.x, h: content.group.scale.y }; },
    // V2d: uniform size factor (aspect and centre unchanged); the live distance and the picture zoom/pan are untouched.
    setScale(f) {
      scale = Number.isFinite(f) && f > 0 ? f : 1;
      if (state.mounted && !state.disposed) placeScreen();
    },
    get yaw() { return state.yaw; },
    ready: content.ready,
    // The file this screen asked the server for (tests, diagnostics).
    get requestedUrl() { return content.requested; },
    mount() {
      if (state.mounted || content.failed || state.disposed) return;
      state.mounted = true;
      placeScreen();
      applyUv();
      play();
    },
    play,
    // Recenter: the screen in front of the head, level with it, facing it. Returns the pose (the app
    // moves its own furniture - dock, HUD - to the same frame).
    recenter(head, forward) {
      const pose = recenterPose(head, forward, input.dist, state.yaw);
      state.yaw = pose.yaw;
      group.position.set(pose.x, pose.y, pose.z);
      group.rotation.set(0, pose.yaw, 0);
      return pose;
    },
    setPose(pose) {
      state.yaw = pose.yaw;
      group.position.set(pose.x, pose.y, pose.z);
      group.rotation.set(0, pose.yaw, 0);
    },
    /* One frame: hands = [{ id, axes, buttons, origin, dir }]. Returns the actions the app has to
     * handle itself ({type: "recenter" | "exit" | "next"}). */
    // V2c: flag the video texture for upload on every frame update() runs (an XR frame) while the video plays, instead
    // of only when three.js' own requestVideoFrameCallback fires. Default off.
    setEveryFrame(on) { everyFrame = !!on; },
    update({ dt, hands, suppressToggle = false }) {
      if (content.anim && state.mounted) tickAnimation(content);
      const v = content.video;
      if (v && !v.paused && v.readyState >= 2) {
        // requestVideoFrameCallback follows the PAGE's rendering, which can stop for the whole session (SteamVR on a
        // PC, the browser window not on show): the sound plays and the screen stays black. When it has been quiet
        // for a while, upload from here, once per new video time.
        const now = performance.now();
        const shown = content.stats ? content.stats.presented : 0;
        if (shown !== state.rvfcShown || !state.rvfcAt) { state.rvfcShown = shown; state.rvfcAt = now; }
        const stalled = now - state.rvfcAt > RVFC_STALL_MS && v.currentTime !== state.rvfcTime;
        if (everyFrame || stalled) {
          state.rvfcTime = v.currentTime;
          const up = content.uploaded;
          for (let i = 0; i < up.length; i += 1) up[i].needsUpdate = true;
        }
      } else if (state.rvfcAt) state.rvfcAt = 0;
      const pass = [];
      if (!state.mounted) return pass;
      for (const a of input.update({ dt, kind: content.kind, hands })) {
        switch (a.type) {
          case "toggle":
            if (content.video && !suppressToggle) { if (content.video.paused) play(); else content.video.pause(); }
            break;
          case "seek":
            if (content.video) content.video.currentTime = seekTarget(content.video.currentTime, a.seconds, content.video.duration);
            break;
          case "distance": placeScreen(); break;
          case "zoom": {
            // Zoom toward where that hand points (user, 2026-10-18), not toward the middle of the picture.
            const at = content.kind === "picture" ? pointAt(hands.find((x) => x.id === a.id)) : null;
            if (at) state.pan = zoomPanAt(state.pan, state.zoomShown, a.zoom, at);
            applyUv();
            break;
          }
          case "grip": {
            const h = hands.find((x) => x.id === a.id);
            if (a.down && h) state.grips.set(a.id, h.origin.clone());
            else state.grips.delete(a.id);
            break;
          }
          default: pass.push(a); break;
        }
      }
      // Picture pan: while a grip is held the image follows that hand.
      if (content.kind === "picture" && state.grips.size) {
        group.updateWorldMatrix(true, false);
        group.getWorldQuaternion(inv).invert();
        for (const h of hands) {
          const prev = state.grips.get(h.id);
          if (!prev) continue;
          tmp.copy(h.origin).sub(prev).applyQuaternion(inv);
          state.grips.set(h.id, h.origin.clone());
          const { w, h: sh } = screenSize(content.aspect, input.dist);
          const pd = panDelta(tmp.x, tmp.y, w * scale, sh * scale, input.zoom);
          state.pan = clampPan({ x: state.pan.x + pd.x, y: state.pan.y + pd.y }, input.zoom);
          applyUv();
        }
      }
      return pass;
    },
    // Rays against the screen are not needed (the trigger acts anywhere); the video element for tests/HUD.
    get video() { return content.video; },
    // V2b: frame counting for the HUD (null for a picture).
    get videoStats() { return content.stats || null; },
    dispose() {
      if (state.disposed) return;
      state.disposed = true;
      content.dispose();
      if (group.parent) group.parent.remove(group);
    },
  };
  D("screen created", content.mode, content.kind);
  return screen;
}

export { MEDIA_TIMEOUT_MS };
