/* Pure logic of the WebXR viewer (js/vr-viewer.js): no DOM, no three.js, no
 * WebXR objects - so the controller mapping, the clamps and the recenter math
 * can be unit-tested in a plain browser page (tests/ui/stereo3d_harness.js).
 *
 * Units: metres, seconds, radians. The screen is a rectangle of height
 * SCREEN_HEIGHT at DEFAULT_DISTANCE; moving it away scales it up so its
 * angular size stays the same. */

export const DEFAULT_DISTANCE = 1.4;
export const MIN_DISTANCE = 0.8;
export const MAX_DISTANCE = 8;
export const SCREEN_HEIGHT = 1.5;      // metres, at DEFAULT_DISTANCE
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 8;
export const SEEK_SECONDS = 10;

const DISTANCE_SPEED = 2.0;            // m/s at full stick deflection
const ZOOM_RATE = 1.2;                 // e-folds per second at full deflection
const DEADZONE = 0.3;                  // continuous axes (distance, zoom)
const FLICK_ON = 0.7;                  // a seek flick fires when |x| crosses this ...
const FLICK_OFF = 0.35;                // ... and re-arms below this
const FLICK_REFRACTORY = 0.3;          // s, minimum time between two flicks of one stick
const BUTTON_REFRACTORY = 0.2;         // s, minimum time between two presses of one button

// Standard xr-standard / oculus-touch gamepad layout (verified on the Steam Frame
// through SteamVR: 7 buttons, 4 axes, thumbstick = axes 2/3).
export const BTN = { TRIGGER: 0, GRIP: 1, STICK: 3, A: 4, B: 5 };

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const finite = (v) => typeof v === "number" && Number.isFinite(v);

export function clampDistance(d) {
  return clamp(finite(d) ? d : DEFAULT_DISTANCE, MIN_DISTANCE, MAX_DISTANCE);
}

export function clampZoom(z) {
  return clamp(finite(z) ? z : MIN_ZOOM, MIN_ZOOM, MAX_ZOOM);
}

// Width / height of ONE eye: from the conversion's meta (eye_w / eye_h), else
// from the decoded media (half the SBS width over its height), else 16:9.
export function eyeAspect(eyeW, eyeH, mediaW, mediaH) {
  if (finite(eyeW) && finite(eyeH) && eyeW > 0 && eyeH > 0) return eyeW / eyeH;
  if (finite(mediaW) && finite(mediaH) && mediaW > 0 && mediaH > 0) return (mediaW / 2) / mediaH;
  return 16 / 9;
}

// Screen rectangle at a distance, constant angular size.
export function screenSize(aspect, dist) {
  const k = clampDistance(dist) / DEFAULT_DISTANCE;
  const h = SCREEN_HEIGHT * k;
  return { w: h * (finite(aspect) && aspect > 0 ? aspect : 16 / 9), h };
}

// Continuous axis with a dead zone: 0 inside it, rescaled to 0..1 outside.
export function deadzone(v, zone = DEADZONE) {
  if (!finite(v) || Math.abs(v) <= zone) return 0;
  return Math.sign(v) * (Math.abs(v) - zone) / (1 - zone);
}

// Thumbstick pulled back (axis Y > 0) moves the screen away.
export function stepDistance(dist, axisY, dt) {
  return clampDistance(clampDistance(dist) + deadzone(axisY) * DISTANCE_SPEED * (finite(dt) ? dt : 0));
}

// Thumbstick pushed right zooms in a picture.
export function stepZoom(zoom, axisX, dt) {
  return clampZoom(clampZoom(zoom) * Math.exp(deadzone(axisX) * ZOOM_RATE * (finite(dt) ? dt : 0)));
}

// The window of a texture that one eye shows. The SBS image is L|R: the left
// eye owns u 0..0.5, the right 0.5..1. pan is in IMAGE fractions of the eye's
// half, 0 = centred; |pan| is limited so the window never leaves the image.
export function maxPan(zoom) {
  return (1 - 1 / clampZoom(zoom)) / 2;
}

export function clampPan(pan, zoom) {
  const m = maxPan(zoom);
  return { x: clamp(finite(pan && pan.x) ? pan.x : 0, -m, m), y: clamp(finite(pan && pan.y) ? pan.y : 0, -m, m) };
}

export function uvWindow(eye, zoom, pan) {
  const z = clampZoom(zoom);
  const p = clampPan(pan, z);
  return {
    repeatX: 0.5 / z,
    repeatY: 1 / z,
    offsetX: eye * 0.5 + 0.25 + 0.5 * p.x - 0.25 / z,
    offsetY: 0.5 + p.y - 0.5 / z,
  };
}

// Dragging the picture with a grip: the controller moved (dx, dy) metres in the
// screen's own right/up axes; the image follows the hand, so the window moves
// the other way, scaled by how much of the image the screen shows.
// Zooming toward where the controller points (user, 2026-10-18: "The zoom feature should zoom in on where the
// controller is pointing."). `at` is the point the ray hits on the screen, in screen fractions from its centre
// (-0.5..0.5, +y up). The image shows image fraction 0.5 + pan + at / zoom there, in both the side-by-side and the
// mono window, so keeping that point under the pointer from zoom z0 to z1 moves the pan by at * (1/z0 - 1/z1).
// Clamped like any pan: near an edge the window stops at the image border instead of showing past it.
export function zoomPanAt(pan, zoomFrom, zoomTo, at) {
  const z0 = clampZoom(zoomFrom);
  const z1 = clampZoom(zoomTo);
  const p = clampPan(pan, z0);
  const ax = clamp(finite(at && at.x) ? at.x : 0, -0.5, 0.5);
  const ay = clamp(finite(at && at.y) ? at.y : 0, -0.5, 0.5);
  const k = 1 / z0 - 1 / z1;
  return clampPan({ x: p.x + ax * k, y: p.y + ay * k }, z1);
}

export function panDelta(dx, dy, screenW, screenH, zoom) {
  const z = clampZoom(zoom);
  if (!(screenW > 0) || !(screenH > 0)) return { x: 0, y: 0 };
  return { x: -dx / (screenW * z), y: -dy / (screenH * z) };
}

// Recenter: put the screen in front of the head, level with it, facing it.
// head = world position, forward = the head's viewing direction (any length).
// The screen group sits at the head position and is rotated by yaw about Y; the
// screen itself is at z = -distance inside it. Looking straight up or down keeps
// the previous yaw. Returns {x, y, z, yaw}.
export function recenterPose(head, forward, dist, previousYaw = 0) {
  const fx = finite(forward && forward.x) ? forward.x : 0;
  const fz = finite(forward && forward.z) ? forward.z : 0;
  const len = Math.hypot(fx, fz);
  const yaw = len < 1e-3 ? previousYaw : Math.atan2(-fx / len, -fz / len);
  return { x: head.x, y: head.y, z: head.z, yaw };
}

/* Controller mapping. Feed it one snapshot per frame:
 *   update({ dt, kind: "video" | "picture", hands: [{ id, axes: [..], buttons: [bool..] }] })
 * and it returns the actions to perform, in order:
 *   { type: "toggle" }                  trigger          (video: play/pause)
 *   { type: "seek", seconds: +-10 }     stick flick X    (video)
 *   { type: "distance", dist }          stick Y held     (new value)
 *   { type: "zoom", zoom, id }          stick X held     (picture; id = the hand, whose ray is the zoom's centre)
 *   { type: "grip", id, down }          grip pressed/released (picture pan; the viewer tracks the hand)
 *   { type: "recenter" }                A / X (not h.left: there the app uses the buttons for Back and Forward)
 *   { type: "exit" }                    B / Y (not h.left)
 *   { type: "next" }                    stick press
 * Either hand works. Buttons act on press (edge), with a refractory time; a
 * seek flick fires once per push past FLICK_ON and re-arms only after the stick
 * has come back below FLICK_OFF. */
export function createController(state = {}) {
  const s = {
    dist: clampDistance(state.dist),
    zoom: clampZoom(state.zoom),
    time: 0,
    hands: new Map(),                  // id -> { buttons: [], lastPress: [], flickArmed: true, lastFlick }
  };
  const hand = (id) => {
    if (!s.hands.has(id)) s.hands.set(id, { buttons: [], lastPress: [], flickArmed: true, lastFlick: -1e9 });
    return s.hands.get(id);
  };

  function stick(axes) {
    // The thumbstick is axes 2/3 on a 4-axis gamepad (0/1 = touchpad slot), 0/1 on a 2-axis one.
    const a = axes || [];
    return a.length >= 4 ? { x: a[2], y: a[3] } : { x: a[0] || 0, y: a[1] || 0 };
  }

  return {
    get dist() { return s.dist; },
    get zoom() { return s.zoom; },
    set dist(v) { s.dist = clampDistance(v); },
    set zoom(v) { s.zoom = clampZoom(v); },
    forget(id) { s.hands.delete(id); },
    update({ dt = 0, kind = "video", hands = [] } = {}) {
      const actions = [];
      s.time += finite(dt) && dt > 0 ? dt : 0;
      for (const h of hands) {
        const st = hand(h.id);
        const b = h.buttons || [];
        const pressed = (i) => !!b[i] && !st.buttons[i];
        const fire = (i, action) => {
          if (pressed(i) && s.time - (st.lastPress[i] ?? -1e9) >= BUTTON_REFRACTORY) {
            st.lastPress[i] = s.time;
            actions.push(action);
          }
        };
        if (kind === "video") fire(BTN.TRIGGER, { type: "toggle" });
        if (!h.left) {
          fire(BTN.A, { type: "recenter" });
          fire(BTN.B, { type: "exit" });
        }
        fire(BTN.STICK, { type: "next" });
        if (kind === "picture" && (!!b[BTN.GRIP] !== !!st.buttons[BTN.GRIP])) {
          actions.push({ type: "grip", id: h.id, down: !!b[BTN.GRIP] });
        }
        st.buttons.length = b.length;
        for (let k = 0; k < b.length; k += 1) st.buttons[k] = !!b[k];     // in place: no allocation per frame

        const { x, y } = stick(h.axes);
        const nd = stepDistance(s.dist, y, dt);
        if (nd !== s.dist) { s.dist = nd; actions.push({ type: "distance", dist: nd }); }
        if (kind === "video") {
          if (Math.abs(x) < FLICK_OFF) st.flickArmed = true;
          else if (st.flickArmed && Math.abs(x) >= FLICK_ON && s.time - st.lastFlick >= FLICK_REFRACTORY) {
            st.flickArmed = false;
            st.lastFlick = s.time;
            actions.push({ type: "seek", seconds: Math.sign(x) * SEEK_SECONDS });
          }
        } else {
          const nz = stepZoom(s.zoom, x, dt);
          if (nz !== s.zoom) { s.zoom = nz; actions.push({ type: "zoom", zoom: nz, id: h.id }); }
        }
      }
      return actions;
    },
  };
}

// Seek target, clamped into the media. A NaN/infinite duration (a stream) only clamps at 0.
export function seekTarget(current, delta, duration) {
  const c = finite(current) ? current : 0;
  const t = Math.max(0, c + delta);
  return finite(duration) && duration > 0 ? Math.min(t, Math.max(0, duration - 0.1)) : t;
}

// The next item after the one with key `key` in a /api/media/3d/list answer
// (newest first), wrapping around; null when there is nothing else to show.
export function nextItem(items, key) {
  const list = (items || []).filter((i) => i && i.sbs_url);
  if (list.length < 2) return null;
  const at = list.findIndex((i) => i.key === key);
  return list[(at + 1) % list.length];
}

export const VR_HELP = "Trigger: play/pause. Stick left/right: seek 10 s (picture: zoom). Stick up/down: screen distance. "
  + "Grip + move (picture): pan. A/X: recenter. Stick press: next converted item. B/Y: hold 3 s to exit VR.";

export const VR_UNAVAILABLE_HINT = "VR needs Chrome on the PC with SteamVR running and chrome://flags #webxr-runtime set to OpenXR.";
