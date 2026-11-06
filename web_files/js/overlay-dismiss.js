"use strict";

/* Escape and the back button close any overlay.
 *
 * Every dialog here was dismissable only by its own little X. On a phone the
 * back gesture is the reflex, and pressing it used to leave the page entirely
 * -- losing your place in the media browser to close a preview. Escape is the
 * same reflex on a desktop.
 *
 * Page-specific close functions are preferred over just hiding the element, so
 * their cleanup still runs (pausing a video, clearing a selection). */

(function () {
  const OVERLAYS = ".modal, .context-menu";

  // Overlay id -> the global the page defines to close it properly.
  const CLOSERS = {
    logModal: "closeLogModal",
    mediaPreviewModal: "closeMediaPreview",
    poolPickerModal: "closePoolPicker",
  };

  // An overlay that already drives its own history and Escape handling opts
  // out with data-self-managed. Two controllers pushing an entry each for the
  // same dialog cancel each other out: closing consumes only one of them, so
  // the pop lands on the entry that reopens the dialog - which looked like
  // the media preview closing and instantly reopening. The media preview
  // needs its own handling anyway (it restores a specific item, syncs the
  // URL, and leaves Escape to the browser while a video is fullscreen).
  const selfManaged = (el) => el.hasAttribute("data-self-managed");

  const visible = () =>
    Array.from(document.querySelectorAll(OVERLAYS))
      .filter((el) => !selfManaged(el) && getComputedStyle(el).display !== "none");

  function close(el) {
    const fn = window[CLOSERS[el.id]];
    if (typeof fn === "function") fn();
    else el.style.display = "none";
  }

  function closeTopmost() {
    const open = visible();
    if (!open.length) return false;
    close(open[open.length - 1]);   // last in DOM order sits on top
    return true;
  }

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" && e.key !== "Esc") return;
    if (closeTopmost()) e.preventDefault();
  });

  // Back button. Opening an overlay pushes one history entry, so the back
  // gesture pops that instead of navigating away; closing by any other means
  // consumes the entry again so the history depth stays honest.
  let pushed = false;

  function sync() {
    const open = visible().length > 0;
    if (open && !pushed) {
      history.pushState({ ahOverlay: true }, "");
      pushed = true;
    } else if (!open && pushed) {
      pushed = false;
      if (history.state && history.state.ahOverlay) history.back();
    }
  }

  window.addEventListener("popstate", () => {
    if (visible().length) {
      pushed = false;      // our entry is already gone; don't try to consume it
      closeTopmost();
    }
  });

  // Overlays are shown by setting an inline style or toggling a class, so watch
  // for both rather than requiring every call site to announce itself.
  const observer = new MutationObserver(sync);
  function watch() {
    document.querySelectorAll(OVERLAYS).forEach((el) =>
      observer.observe(el, { attributes: true, attributeFilter: ["style", "class"] }));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watch);
  } else {
    watch();
  }
})();
