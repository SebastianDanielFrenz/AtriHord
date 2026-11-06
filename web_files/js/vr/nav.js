/* Navigation of the VR app (P8 V1), pure: where it starts, the wall stack, Back, the dock's buttons.
 *
 * Levels (plan 2.2): wall -> viewer. Opening a folder tile pushes a new wall (the old one remembers where it
 * was scrolled to); Back pops it. Opening a file puts the viewer over the current wall; Back closes it and the wall
 * is scrolled to that file. Back at the very top (one wall, no viewer) means Exit VR.
 *
 * Start context (OWNER DECISION 2026-10-06 (2)): opened from an item -> the viewer on it with that item's own
 * list as the wall behind it; opened from the main view -> that wall at its current position. */
import { normalizeContext } from "./logic.js";
import { folderSpec, folderOfItem } from "./sources.js";

/* planStart(context) -> { view: "wall"|"item", spec, item?, scrollIndex }
 * scrollIndex is the tile index the wall should start scrolled to when there is no item (0 = top). */
export function planStart(context) {
  const c = normalizeContext(context);
  if (c.view === "item") {
    const spec = c.source && ["home", "folder", "recent", "favorites", "search", "3d", "people", "characters"].includes(c.source.kind) ? c.source : folderOfItem(c.item);
    return { view: "item", spec, item: c.item, scrollIndex: 0 };
  }
  if (c.view === "wall" && c.source && ["home", "folder", "recent", "favorites", "search", "3d", "people", "characters"].includes(c.source.kind)) {
    return { view: "wall", spec: c.source, item: null, scrollIndex: Math.max(0, c.source.offset | 0) };
  }
  // Home arrives in a later phase: the Library (roots, or the single root's top) stands in for it.
  return { view: "wall", spec: { ...folderSpec("", ""), kind: "home" }, item: null, scrollIndex: 0 };
}

/* The stack. State is plain data so a test can read it:
 *   walls = [{ spec, index }]   index = first visible tile when the wall was left (or 0 for the top entry's current)
 *   viewer = null | { id, root, path }  the item open over walls[top] */
export function createNav(start) {
  const walls = [{ spec: start.spec, index: start.scrollIndex || 0 }];
  let viewer = start.item ? { id: `${start.item.root}|${start.item.path}`, root: start.item.root, path: start.item.path, ctx: start.item } : null;
  const ahead = [];   // what Back left, newest last: { type: "viewer", item } | { type: "wall", spec, index }; a new place clears it
  const nav = {
    get walls() { return walls; },
    get viewer() { return viewer; },
    get top() { return walls[walls.length - 1]; },
    get depth() { return walls.length; },
    get canForward() { return ahead.length > 0; },
    // Opening a folder: remember where this wall was scrolled to (index), push the new one.
    go(spec, leavingIndex) {
      walls[walls.length - 1].index = Math.max(0, leavingIndex | 0);
      walls.push({ spec, index: 0 });
      viewer = null;
      ahead.length = 0;
      return nav.top;
    },
    openViewer(item) { ahead.length = 0; viewer = { id: item.id || `${item.root}|${item.path}`, root: item.root, path: item.path, ctx: item }; return viewer; },
    setViewer(item) { return nav.openViewer(item); },
    closeViewer() { const v = viewer; viewer = null; return v; },
    /* Back: -> { type: "viewer", closed } | { type: "wall", spec, index } | { type: "exit" } */
    back() {
      if (viewer) {
        if (viewer.ctx) ahead.push({ type: "viewer", item: viewer.ctx });
        return { type: "viewer", closed: nav.closeViewer() };
      }
      if (walls.length > 1) {
        const left = walls.pop();
        ahead.push({ type: "wall", spec: left.spec, index: left.index });
        const t = nav.top;
        return { type: "wall", spec: t.spec, index: t.index };
      }
      return { type: "exit" };
    },
    /* Forward: undoes the last Back -> { type: "viewer", item } (open it) | { type: "wall", spec, index } (already pushed) | null.
     * leavingIndex = where the current wall is scrolled to, for a later Back. */
    forward(leavingIndex) {
      const f = ahead.pop();
      if (!f) return null;
      if (f.type === "wall") {
        walls[walls.length - 1].index = Math.max(0, leavingIndex | 0);
        walls.push({ spec: f.spec, index: f.index });
      }
      return f;
    },
    // A breadcrumb or other jump to a place: just a push (Back returns to where we were).
    jump(spec, leavingIndex) { return nav.go(spec, leavingIndex); },
  };
  return nav;
}

/* The dock's buttons for the current state (ids are what the app dispatches on).
 *   extra: { ready3d: true } when a video finished converting and "switch to 3D" is offered. */
export function dockButtons(state, extra = {}) {
  if (state === "viewer") {
    const b = [{ id: "back", label: "Back" }, { id: "prev", label: "Prev" }, { id: "next", label: "Next" }];
    b.push({ id: "favorite", label: extra.favoriteBusy ? "Saving…" : (extra.favorite ? "Unfavorite" : "Favorite") });
    if (extra.ready3d) b.push({ id: "switch3d", label: "Switch to 3D" });
    b.push({ id: "hud", label: "HUD" }, { id: "recenter", label: "Recenter" }, { id: "exit", label: "Exit VR" });
    return b;
  }
  return [{ id: "back", label: "Back" }, { id: "home", label: "Home" }, { id: "top", label: "Top" }, { id: "search", label: "Search" }, { id: "filters", label: "Filters" }, { id: "settings", label: "Settings" },
    { id: "hud", label: "HUD" }, { id: "recenter", label: "Recenter" }, { id: "exit", label: "Exit VR" }];
}

// The next or previous VIEWABLE tile (files, not folders) after index i, walking the wall's own order.
// dir = +1 / -1, wraps nowhere (null at the ends).
export function neighbour(items, i, dir) {
  const step = dir < 0 ? -1 : 1;
  for (let k = i + step; k >= 0 && k < items.length; k += step) if (items[k] && ["picture", "video"].includes(items[k].type)) return k;
  return -1;
}

// Dock subtitle for a wall: "214 of 1,020" (position of the first visible tile, loaded, total when known).
export function countText(first, loaded, total) {
  const fmt = (n) => Number(n).toLocaleString("en-US");
  if (!loaded) return "empty";
  const t = Number.isFinite(total) && total >= loaded ? total : null;
  return t !== null && t !== loaded ? `${fmt(Math.min(loaded, first + 1))} of ${fmt(t)}` : `${fmt(Math.min(loaded, first + 1))} of ${fmt(loaded)}`;
}
