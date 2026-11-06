/* The P7 immersive viewer for converted (side-by-side) videos and pictures, now a thin shim over the
 * VR app's modules (js/vr/): host.js (renderer, session, input, frame loop, cleanup) and
 * stereo-screen.js (the screen). The media page starts the full VR app (js/vr/app.js, startVr) -
 * this file keeps the old entry point, enterVr({ url, kind, eyeW, eyeH, key, ... }), for anything
 * that still imports it: one stereo screen in an otherwise empty scene, B/Y or a load failure ends it. */
import { startVr, requestImmersiveSession, sessionErrorText } from "./vr/app.js";
import { applyEyeWindows } from "./vr/stereo-screen.js";

export { applyEyeWindows, requestImmersiveSession, sessionErrorText };

/* enterVr({ url, kind, eyeW, eyeH, key, listUrl, onStatus, onItem, retryDelayMs })
 * Call it from the click handler. Resolves once the session runs, with
 * { ended: Promise<{error?: string}>, end(), show(item) }; rejects with an Error whose
 * message is fit to show when no session could be started. */
export function enterVr(opts) {
  const kind = opts.kind === "picture" ? "picture" : "video";
  const context = {
    view: "item",
    item: {
      root: "", path: `${opts.key || "item"}${kind === "video" ? ".mp4" : ".jpg"}`, kind,
      status: { state: "done", key: opts.key || "", sbs_url: opts.url, kind, eye_w: opts.eyeW, eye_h: opts.eyeH },
    },
  };
  return startVr(context, { bare: true, noPersist: true, storage: null, onStatus: opts.onStatus, onItem: opts.onItem, listUrl: opts.listUrl, retryDelayMs: opts.retryDelayMs });
}
