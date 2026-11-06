/* The VR host: everything about running a WebGL scene that is not the scene itself.
 *   createXrHost(opts)      a real immersive-vr session (Chrome + SteamVR OpenXR)
 *   createPreviewHost(opts) the same scene in a normal canvas on the monitor (mouse ray, mouse-look
 *                           camera), for checking room, dock, HUD and screen without a headset
 * Both give the app the same object: renderer, scene, camera, hands() (gamepad snapshot + ray),
 * head(), a frame loop with frame-time statistics, end()/ended and a cleanup that disposes
 * every GPU resource and removes the canvas. three.js r170 is vendored (js/vendor/), nothing
 * comes from a CDN.
 *
 * Proven set-up (owner's headset test, 2026-10-06): "local" reference space (origin = the head at
 * session start, y = 0 is eye level); the left eye's meshes are on layer 1, the right eye's on
 * layer 2, both eyes see layer 0. Session request is retried once after ~1.5 s on NotSupportedError. */
import * as THREE from "../vendor/three.module.min.js";
import { VR_UNAVAILABLE_HINT } from "../vr-logic.js";
import { FrameStats } from "./logic.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ?vrdebug=1 on the page URL: step logging (console.warn, "[VRDIAG ...]").
const DEBUG = typeof location !== "undefined" && /[?&]vrdebug=1/.test(location.search);
export const D = DEBUG ? (...a) => console.warn("[VRDIAG " + (performance.now() / 1000).toFixed(2) + "]", ...a) : () => {};

// Features asked for besides the proven "local" space. ?vrprobe=1 on the page URL also asks for
// "layers" and "hand-tracking" so the HUD's session.enabledFeatures shows whether the runtime grants
// them. It stays opt-in: asking for "layers" makes three.js build its projection-layer path, which
// has not been tried in the headset yet.
export function optionalFeaturesFor(search) {
  const probe = /[?&]vrprobe=1/.test(search || "");
  return probe ? ["local-floor", "layers", "hand-tracking"] : ["local-floor"];
}

// Starts the session (one retry: right after SteamVR connects the first request can fail with
// NotSupportedError, the second works).
export async function requestImmersiveSession(xr, retryDelayMs = 1500, optionalFeatures = ["local-floor"]) {
  const options = { optionalFeatures };
  try {
    return await xr.requestSession("immersive-vr", options);
  } catch (first) {
    if (!first || first.name !== "NotSupportedError") throw first;
    await sleep(retryDelayMs);
    return xr.requestSession("immersive-vr", options);
  }
}

// A sentence for the page, from whatever requestSession rejected with.
export function sessionErrorText(e) {
  const name = (e && e.name) || "";
  if (name === "NotSupportedError") return `VR could not be started (is SteamVR running and connected?). ${VR_UNAVAILABLE_HINT}`;
  if (name === "SecurityError" || name === "NotAllowedError") return "VR was blocked: allow VR for this site in the browser's prompt and try again.";
  if (name === "InvalidStateError") return "VR is already running in another tab or window (an old VR session is still open): close it and try again.";
  const msg = e && e.message ? String(e.message) : "";
  return `Could not start VR${msg ? `: ${msg}` : "."}`;
}

// The part both hosts share: scene, camera, loop, statistics, disposers, end/cleanup.
function baseHost(renderer, mode) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x101014);
  const camera = new THREE.PerspectiveCamera(70, 1, 0.05, 100);
  const stats = new FrameStats(240);
  const disposers = [];
  let frameFn = null, last = -1, cleaned = false, resolveEnded;
  const ended = new Promise((r) => { resolveEnded = r; });
  const h = {
    mode, renderer, scene, camera, stats, ended,
    canvas: renderer.domElement,
    endError: null,
    leak: null,
    addDisposer(fn) { disposers.push(fn); },
    get cleaned() { return cleaned; },
    // Starts calling fn(dt seconds, t ms) every frame before the render.
    run(fn) { frameFn = fn; },
    tick(t) {
      const dt = last < 0 ? 0 : Math.min(0.1, Math.max(0, (t - last) / 1000));
      if (last >= 0) stats.push(t - last);
      last = t;
      if (frameFn) {
        try { frameFn(dt, t); } catch (e) { console.error("VR frame error:", e && e.message ? e.message : e); frameFn = null; }
      }
      renderer.render(scene, camera);
    },
    info() {
      const m = renderer.info.memory, r = renderer.info.render;
      return { mode, textures: m.textures, geometries: m.geometries, calls: r.calls };
    },
    finish(extra) {
      if (cleaned) return;
      cleaned = true;
      try { renderer.setAnimationLoop(null); } catch (e) { /* already gone */ }
      for (const fn of disposers.splice(0).reverse()) { try { fn(); } catch (e) { /* ignore */ } }
      // What is still allocated after every owner disposed its own: must be 0 (checked by the harness).
      h.leak = { textures: renderer.info.memory.textures, geometries: renderer.info.memory.geometries };
      try { renderer.dispose(); } catch (e) { /* ignore */ }
      try { renderer.forceContextLoss(); } catch (e) { /* ignore */ }
      if (extra) try { extra(); } catch (e) { /* ignore */ }
      const c = renderer.domElement;
      if (c && c.parentNode) c.parentNode.removeChild(c);
      resolveEnded(Object.assign({ textures: h.leak.textures, geometries: h.leak.geometries }, h.endError ? { error: h.endError } : {}));
    },
  };
  return h;
}

/* ------------------------------------------------------------------ the real headset */

/* createXrHost({ xr, retryDelayMs, optionalFeatures })
 * Call it from the click handler without awaiting anything first: requestSession needs the user
 * gesture. Rejects with an Error whose message is fit to show when no session could be started. */
export async function createXrHost(opts = {}) {
  const xr = opts.xr || navigator.xr;
  if (!xr) throw new Error(`This browser has no WebXR. ${VR_UNAVAILABLE_HINT}`);
  let renderer = null, session = null;
  let host = null;
  const cleanupOnFail = () => {
    if (host) host.finish();
    else if (renderer) {
      try { renderer.dispose(); renderer.forceContextLoss(); } catch (e) { /* ignore */ }
      if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
    }
  };
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true });
    const canvas = renderer.domElement;
    canvas.id = "vrViewerCanvas";
    // In the page only so the XR-compatible context has an owner; never visible, never in the way.
    canvas.style.cssText = "position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;pointer-events:none;z-index:-1";
    document.body.appendChild(canvas);
    renderer.setSize(1, 1);
    renderer.xr.enabled = true;
    renderer.xr.setReferenceSpaceType("local");
    host = baseHost(renderer, "xr");
    if (typeof opts.onRenderer === "function") opts.onRenderer(renderer);   // the app starts loading its media now
    D("requesting session");
    const features = opts.optionalFeatures || optionalFeaturesFor(typeof location !== "undefined" ? location.search : "");
    session = await requestImmersiveSession(xr, opts.retryDelayMs === undefined ? 1500 : opts.retryDelayMs, features);
    session.addEventListener("end", () => host.finish());
    D("session granted", session.mode, session.visibilityState);
    // Chrome 156 gives every session renderState.layers, so three.js r170 builds its WebXR-layers
    // projection-layer path even when "layers" was not requested - and with SteamVR OpenXR the session
    // then never fires a single frame (Chrome shows "page not responding" in the headset, 2026-10-29).
    // Hide renderState.layers while three.js chooses, so it takes the XRWebGLLayer path that works.
    // The "?probe" start asks for "layers" on purpose and keeps the layers path.
    const hideLayers = !features.includes("layers");
    const rsDesc = hideLayers ? Object.getOwnPropertyDescriptor(Object.getPrototypeOf(session), "renderState") : null;
    if (rsDesc && rsDesc.get) {
      Object.defineProperty(session, "renderState", {
        configurable: true,
        get() { const rs = rsDesc.get.call(session); return new Proxy(rs, { get: (t, k) => (k === "layers" ? undefined : t[k]) }); },
      });
    }
    try {
      await renderer.xr.setSession(session);
    } finally {
      if (rsDesc && rsDesc.get) delete session.renderState;
    }
    D("setSession done");
  } catch (e) {
    const had = session;
    if (had) { try { await had.end(); } catch (e2) { /* already ended */ } }
    cleanupOnFail();
    throw new Error(had ? `Could not start VR: ${(e && e.message) || e}` : sessionErrorText(e));
  }
  host.session = session;
  host.xrInfo = () => ({
    mode: "xr",
    enabledFeatures: session.enabledFeatures ? Array.from(session.enabledFeatures) : undefined,
    frameRate: session.frameRate,
    supportedFrameRates: session.supportedFrameRates ? Array.from(session.supportedFrameRates) : undefined,
  });

  const controllers = [0, 1].map((i) => {
    const c = renderer.xr.getController(i);
    c.addEventListener("connected", (e) => { c.userData.source = e.data; });
    c.addEventListener("disconnected", () => { c.userData.source = null; });
    host.scene.add(c);
    return c;
  });
  host.addDisposer(() => controllers.forEach((c) => host.scene.remove(c)));
  const handKeys = new WeakMap();
  const handKey = (src, i) => {
    let k = handKeys.get(src);
    if (!k) { k = `${src.handedness || "none"}${i}`; handKeys.set(src, k); }
    return k;
  };
  const head = new THREE.Vector3(), fwd = new THREE.Vector3();
  const orientation = new THREE.Quaternion();
  let viewerPose = null;
  host.hasViewerPose = () => viewerPose !== null && session.visibilityState === "visible";

  // Steam Frame left d-pad left/right under the "media-dl d-pad" SteamVR binding (vr-bindings/): left = thumbrest +
  // X touched without a click, right = thumbrest + Y touched without a click. One thumb cannot touch the thumbrest and
  // the d-pad at once, so the default binding never produces either. -1 left, +1 right, 0 none.
  function frameDpad(left, bt) {
    if (!left || bt.length < 7 || !(bt[6].pressed || bt[6].touched)) return 0;
    if (bt[4].touched && !bt[4].pressed) return -1;
    if (bt[5].touched && !bt[5].pressed) return 1;
    return 0;
  }
  // One snapshot per input source with a gamepad: { id, axes, buttons (pressed), origin, dir, hasRay }.
  // The objects and arrays are reused every frame (no allocation in the frame loop): read them, do not keep them.
  const handPool = [], handsOut = [];
  host.hands = () => {
    handsOut.length = 0;
    let i = 0, n = 0;
    for (const src of session.inputSources) {
      i += 1;
      if (!src.gamepad) continue;
      let c = null;
      for (let k = 0; k < controllers.length; k += 1) if (controllers[k].userData.source === src) { c = controllers[k]; break; }
      let hand = handPool[n];
      if (!hand) { hand = { id: "", left: false, dpad: 0, axes: [], buttons: [], origin: new THREE.Vector3(), dir: new THREE.Vector3(0, 0, -1), hasRay: false }; handPool[n] = hand; }
      n += 1;
      hand.id = handKey(src, i);
      hand.left = src.handedness === "left";
      const ax = src.gamepad.axes, bt = src.gamepad.buttons;
      hand.axes.length = ax.length;
      for (let k = 0; k < ax.length; k += 1) hand.axes[k] = ax[k];
      hand.buttons.length = bt.length;
      for (let k = 0; k < bt.length; k += 1) hand.buttons[k] = bt[k].pressed;
      hand.dpad = frameDpad(hand.left, bt);
      hand.hasRay = !!c;
      if (c) {
        c.getWorldPosition(hand.origin);
        hand.dir.set(0, 0, -1).transformDirection(c.matrixWorld);
      } else {
        hand.origin.set(0, 0, 0);
        hand.dir.set(0, 0, -1);
      }
      handsOut.push(hand);
    }
    return handsOut;
  };
  // World position and viewing direction of the head (the XR camera).
  host.head = () => {
    if (viewerPose) {
      const { position: p, orientation: q } = viewerPose.transform;
      head.set(p.x, p.y, p.z);
      orientation.set(q.x, q.y, q.z, q.w);
      fwd.set(0, 0, -1).applyQuaternion(orientation);
      return { pos: head, fwd };
    }
    const cam = renderer.xr.getCamera();
    head.setFromMatrixPosition(cam.matrixWorld);
    fwd.set(0, 0, -1).transformDirection(cam.matrixWorld);
    return { pos: head, fwd };
  };
  host.end = () => session.end().catch(() => {});
  // Frames go to the compositor from now on, whatever the app does: a session that is running but not
  // rendering shows black and drops frames in the headset.
  renderer.setAnimationLoop((t, frame) => {
    // r170 also starts the window animation loop when this is set after setSession.
    // XR framebuffers are valid only during the session's own frame callback.
    if (!frame) return;
    const space = renderer.xr.getReferenceSpace();
    viewerPose = frame && space ? frame.getViewerPose(space) : null;
    host.tick(t);
  });
  D("session running, loop set");
  return host;
}

/* ------------------------------------------------------------------ desktop preview */

/* createPreviewHost({ onClose })  A full-window canvas over the page.
 *   mouse move: the ray (a controller)          left button: trigger
 *   right-button drag: look around              wheel: stick Y (scroll the wall / screen distance)
 *   G (held): grip    R: A/X recenter    Escape or B: B/Y    Left/Right arrows: stick X
 * The same scene as in the headset, with only the left eye's meshes shown (camera layers 0 and 1). */
export function createPreviewHost(opts = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  const overlay = document.createElement("div");
  overlay.id = "vrPreviewRoot";
  overlay.style.cssText = "position:fixed;inset:0;z-index:2147483000;background:#000;touch-action:none";
  const canvas = renderer.domElement;
  canvas.id = "vrPreviewCanvas";
  canvas.style.cssText = "width:100%;height:100%;display:block";
  const bar = document.createElement("div");
  bar.id = "vrPreviewBar";
  bar.style.cssText = "position:absolute;left:8px;top:8px;padding:4px 10px;border-radius:6px;background:rgba(0,0,0,.55);color:#cfd3dc;font:12px system-ui,sans-serif;pointer-events:none";
  bar.textContent = "Desktop preview - mouse: ray, click: trigger, right-drag: look, wheel: scroll / distance, G: grip, R: recenter, Esc: exit";
  overlay.append(canvas, bar);
  document.body.appendChild(overlay);

  const host = baseHost(renderer, "preview");
  const { camera } = host;
  camera.layers.enable(1);              // the left eye's meshes of side-by-side items
  const size = () => {
    const w = Math.max(1, overlay.clientWidth || window.innerWidth), hh = Math.max(1, overlay.clientHeight || window.innerHeight);
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    renderer.setSize(w, hh, false);
    camera.aspect = w / hh;
    camera.updateProjectionMatrix();
  };
  size();

  const pointer = { x: 0, y: 0, in: false };          // NDC
  const keys = { trigger: false, grip: false, recenter: false, exit: false, left: false, right: false };
  let looking = false;
  // Looking a little down at the start: the dock sits 40 degrees below the line of sight, the screen at it.
  let yaw = 0, pitch = -0.3;
  camera.rotation.set(pitch, yaw, 0, "YXZ");
  const onMove = (e) => {
    const r = canvas.getBoundingClientRect();
    pointer.x = ((e.clientX - r.left) / Math.max(1, r.width)) * 2 - 1;
    pointer.y = -(((e.clientY - r.top) / Math.max(1, r.height)) * 2 - 1);
    pointer.in = true;
    if (looking) {
      yaw -= (e.movementX || 0) * 0.004;
      pitch = Math.max(-1.2, Math.min(1.2, pitch - (e.movementY || 0) * 0.004));
      camera.rotation.set(pitch, yaw, 0, "YXZ");
    }
  };
  const onDown = (e) => {
    if (e.button === 2) { looking = true; return; }
    if (e.button === 0) keys.trigger = true;
  };
  const onUp = (e) => {
    if (e.button === 2) { looking = false; return; }
    if (e.button === 0) keys.trigger = false;
  };
  let wheelDir = 0, wheelUntil = 0;
  // A wheel notch holds the stick for a moment (a real stick is held, a notch is one event).
  const onWheel = (e) => { wheelDir = Math.sign(e.deltaY) || 0; wheelUntil = performance.now() + 140; e.preventDefault(); };
  // host.captureKeys: the app has a text field open (the search keyboard) - key presses are its input, not the preview's controls.
  const onKey = (down) => (e) => {
    if (down && host.captureKeys) return;
    const k = (e.key || "").toLowerCase();
    if (k === "g") keys.grip = down;
    else if (k === "r") keys.recenter = down;
    else if (k === "escape" || k === "b") keys.exit = down;
    else if (k === "arrowleft") keys.left = down;
    else if (k === "arrowright") keys.right = down;
    else return;
    e.preventDefault();
  };
  const kd = onKey(true), ku = onKey(false);
  const noMenu = (e) => e.preventDefault();
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerdown", onDown);
  window.addEventListener("pointerup", onUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("contextmenu", noMenu);
  window.addEventListener("keydown", kd);
  window.addEventListener("keyup", ku);
  window.addEventListener("resize", size);
  const ray = new THREE.Raycaster();
  const head = new THREE.Vector3(), fwd = new THREE.Vector3();

  const pvHand = { id: "mouse", dpad: 0, axes: [0, 0, 0, 0], buttons: [false, false, false, false, false, false], origin: new THREE.Vector3(), dir: new THREE.Vector3(0, 0, -1), hasRay: false };
  const pvOut = [pvHand];
  host.hands = () => {
    ray.setFromCamera(pointer, camera);
    pvHand.origin.copy(ray.ray.origin);
    pvHand.dir.copy(ray.ray.direction);
    pvHand.axes[2] = (keys.right ? 1 : 0) - (keys.left ? 1 : 0);
    pvHand.axes[3] = performance.now() < wheelUntil ? wheelDir : 0;
    pvHand.buttons[0] = keys.trigger; pvHand.buttons[1] = keys.grip; pvHand.buttons[4] = keys.recenter; pvHand.buttons[5] = keys.exit;
    pvHand.hasRay = pointer.in;
    return pvOut;
  };
  host.head = () => {
    camera.getWorldPosition(head);
    fwd.set(0, 0, -1).transformDirection(camera.matrixWorld);
    return { pos: head, fwd };
  };
  host.xrInfo = () => ({ mode: "preview", enabledFeatures: ["desktop preview"], frameRate: undefined, supportedFrameRates: undefined });
  host.end = async () => { host.finish(teardown); };
  function teardown() {
    canvas.removeEventListener("pointermove", onMove);
    canvas.removeEventListener("pointerdown", onDown);
    window.removeEventListener("pointerup", onUp);
    canvas.removeEventListener("wheel", onWheel);
    canvas.removeEventListener("contextmenu", noMenu);
    window.removeEventListener("keydown", kd);
    window.removeEventListener("keyup", ku);
    window.removeEventListener("resize", size);
    if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
  }
  renderer.setAnimationLoop((t) => host.tick(t));
  void opts;
  return host;
}
