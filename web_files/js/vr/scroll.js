/* Scrolling of the wall (P8 V1), pure. The wall moves, never the user (plan 2.4):
 *   - thumbstick Y: velocity based, eased, capped (default 60 deg/s), dead zone
 *   - grip + move: the wall follows the hand's ray elevation 1:1, inertia on release
 *   - jump to top, jump to an offset
 * The model works in degrees of scroll offset (the same unit as layout.js). Everything is allocation free
 * per call (the frame loop calls it every frame). */

export const SCROLL = { maxVel: 60, easeS: 0.12, deadzone: 0.3, inertiaTauS: 0.35, stopVel: 0.5 };

const finite = (v) => typeof v === "number" && Number.isFinite(v);

export function stickDeadzone(v, zone = SCROLL.deadzone) {
  if (!finite(v) || Math.abs(v) <= zone) return 0;
  return Math.sign(v) * (Math.abs(v) - zone) / (1 - zone);
}

export function createScroll(opts = {}) {
  const c = Object.assign({}, SCROLL, opts);
  const s = { offset: 0, vel: 0, max: 0, dragging: false, dragStartElev: 0, dragStartOffset: 0, lastElev: 0, dragVel: 0 };
  const clampOff = (v) => (v < 0 ? 0 : (v > s.max ? s.max : v));
  const m = {
    cfg: c,
    get offset() { return s.offset; },
    get velocity() { return s.vel; },
    get max() { return s.max; },
    get dragging() { return s.dragging; },
    setMax(max) { s.max = Math.max(0, finite(max) ? max : 0); s.offset = clampOff(s.offset); },
    jump(offset) { s.offset = clampOff(finite(offset) ? offset : 0); s.vel = 0; },
    top() { m.jump(0); },
    /* One frame. stickY: the thumbstick Y axis (pushed forward = negative = further down the list), 0 when none.
     * Returns true when the offset changed. */
    step(dt, stickY = 0) {
      if (!(dt > 0)) return false;
      const before = s.offset;
      if (s.dragging) {
        // the offset is set by drag(); keep velocity for the release
        s.vel = s.dragVel;
        return false;
      }
      const target = -stickDeadzone(stickY) * c.maxVel;
      if (target !== 0) {
        s.vel += (target - s.vel) * (1 - Math.exp(-dt / c.easeS));
      } else {
        s.vel *= Math.exp(-dt / c.inertiaTauS);
        if (Math.abs(s.vel) < c.stopVel) s.vel = 0;
      }
      if (s.vel !== 0) {
        const next = s.offset + s.vel * dt;
        s.offset = clampOff(next);
        if (s.offset !== next) s.vel = 0;           // hit an end: no bounce
      }
      return s.offset !== before;
    },
    // Grip pressed with the ray at elevation `elev` (deg).
    dragStart(elev) {
      s.dragging = true;
      s.dragStartElev = s.lastElev = finite(elev) ? elev : 0;
      s.dragStartOffset = s.offset;
      s.vel = 0; s.dragVel = 0;
    },
    // The hand moved: the content follows. Moving the hand up scrolls on (offset grows).
    drag(elev, dt) {
      if (!s.dragging || !finite(elev)) return false;
      const before = s.offset;
      s.offset = clampOff(s.dragStartOffset + (elev - s.dragStartElev));
      if (dt > 0) {
        const inst = (s.offset - before) / dt;
        s.dragVel += (inst - s.dragVel) * 0.5;       // smoothed release velocity
      }
      s.lastElev = elev;
      return s.offset !== before;
    },
    dragEnd() {
      if (!s.dragging) return;
      s.dragging = false;
      s.vel = Math.max(-600, Math.min(600, s.dragVel));
    },
  };
  return m;
}

/* B/Y: a tap is "Back", holding it for holdS seconds is "Exit VR" (plan 2.3), whatever the level.
 * update(down, dt) -> null | "back" | "exit": "exit" fires once while still held, "back" on release of a tap. */
export function createBackHold(holdS = 1) {
  let held = 0, down = false, fired = false;
  return {
    get progress() { return down && !fired ? Math.min(1, held / holdS) : 0; },
    update(isDown, dt) {
      let out = null;
      if (isDown) {
        if (!down) { down = true; held = 0; fired = false; }
        held += dt > 0 ? dt : 0;
        if (!fired && held >= holdS) { fired = true; out = "exit"; }
      } else if (down) {
        if (!fired) out = "back";
        down = false; held = 0; fired = false;
      }
      return out;
    },
    reset() { held = 0; down = false; fired = false; },
  };
}
