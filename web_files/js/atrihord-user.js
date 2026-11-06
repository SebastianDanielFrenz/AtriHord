"use strict";

/* AtriHord's own copy, cut loose from the estate's Stylehub ksf-user.js on 2026-11-05: edits to either do not
 * reach the other, nothing syncs them. Do not copy the Stylehub file over this one. */

/* AtriHord signed-in user — the standard top-right identity control.
 *
 * Every AtriHord page puts the same thing in the same corner: who you are, what you
 * are allowed to be, and how to leave. Written once here and served from /js/atrihord-user.js.
 *
 * Use it by putting a mount point in your header:
 *
 *     <div data-ah-user></div>
 *     <script src="/js/atrihord-user.js" defer></script>
 *
 * Optional attributes on the mount point:
 *     data-endpoint="/api/me"   where to read identity from (default /api/me)
 *     data-logout="/logout"     app-local sign-out; omit to end the SSO session
 *
 * The endpoint may answer in any of the shapes our apps already use --
 * {user, groups} or {username, role} -- because normalising the apps was not
 * worth breaking them for.
 *
 * It renders nothing at all when no one is signed in, so a page served without
 * the proxy (local development, the anonymous share viewer) looks untouched
 * rather than showing an empty chrome.
 */

(function () {
  // Sign-in site of the identity provider; set window.ATRIHORD_AUTH_URL before this script to enable the
  // "Account settings" link. Empty: the app itself handles sign-out (data-logout, default /logout).
  const AUTH = (window.ATRIHORD_AUTH_URL || "").replace(/\/$/, "");
  const STYLE_ID = "ah-user-style";

  const CSS = `
.ah-user { position: relative; margin-left: auto; }
.ah-user__btn {
  display: flex; align-items: center; gap: 8px;
  appearance: none; background: none;
  border: 1px solid transparent; border-radius: var(--r-pill, 999px);
  padding: 4px 10px 4px 4px;
  color: var(--ink, #e6e4db); font: inherit; font-size: var(--text-sm, .8rem);
  cursor: pointer;
}
.ah-user__btn:hover { background: var(--wash, rgba(230,228,219,.05)); border-color: var(--ring, rgba(230,228,219,.13)); }
.ah-user__btn:focus-visible { outline: 2px solid var(--accent, #3987e5); outline-offset: 2px; }
.ah-user__avatar {
  display: grid; place-items: center; width: 26px; height: 26px;
  border-radius: 50%; background: var(--accent-wash, rgba(57,135,229,.13));
  color: var(--accent, #3987e5); font-weight: 600; font-size: var(--text-sm, .8rem);
}
.ah-user__name { font-weight: 500; }
.ah-user__role {
  font-size: var(--text-xs, .72rem); text-transform: uppercase; letter-spacing: .04em;
  padding: 1px 6px; border-radius: var(--r-xs, 4px);
  border: 1px solid var(--ring, rgba(230,228,219,.13)); color: var(--muted, #898781);
}
.ah-user__role--admin { color: var(--accent, #3987e5); border-color: var(--accent-wash, rgba(57,135,229,.13)); }
.ah-user__menu {
  position: absolute; right: 0; top: calc(100% + 6px); min-width: 13rem; z-index: 200;
  background: var(--surface, #1a1a19); border: 1px solid var(--ring, rgba(230,228,219,.13));
  border-radius: var(--r-sm, 8px); box-shadow: var(--shadow-pop, 0 8px 32px rgba(0,0,0,.45));
  padding: 5px;
}
.ah-user__menu[hidden] { display: none; }
.ah-user__head {
  padding: 6px 9px 7px; font-size: var(--text-xs, .72rem); color: var(--muted, #898781);
  border-bottom: 1px solid var(--line, #2c2c2a); margin-bottom: 4px;
}
.ah-user__groups { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
.ah-user__group {
  font-size: var(--text-xs, .72rem); padding: 1px 6px; border-radius: var(--r-xs, 4px);
  background: var(--wash, rgba(230,228,219,.05)); color: var(--ink-2, #c3c2b7);
}
.ah-user__item {
  display: block; padding: 7px 9px; border-radius: var(--r-xs, 4px);
  color: var(--ink, #e6e4db); text-decoration: none; font-size: var(--text-sm, .8rem);
}
.ah-user__item:hover { background: var(--surface-hover, #2e2e2c); text-decoration: none; }
.ah-user__item:focus-visible { outline: 2px solid var(--accent, #3987e5); outline-offset: -2px; }
.ah-user__item--out { color: var(--down, #d03b3b); }
`;

  /* ---- theme ------------------------------------------------------------
     One key, three values, this device only. DARK IS THE DEFAULT: the
     stylesheet renders anything without a data-theme attribute dark, so a
     page that never loads this file - or a browser where storage is blocked -
     looks like the rest of the estate rather than following the device.
     "auto" is therefore a stored value like any other, not the absence of
     one; it is the only state that follows prefers-color-scheme. */
  const THEME_KEY = "ah.theme";
  const LEGACY_THEME_KEY = "ksf.theme";
  const MODES = ["auto", "light", "dark"];

  function readTheme() {
    try {
      const v = localStorage.getItem(THEME_KEY) || localStorage.getItem(LEGACY_THEME_KEY);
      return MODES.includes(v) ? v : "dark";
    } catch (err) { return "dark"; }        // private mode, blocked storage
  }

  function applyTheme(mode) {
    const root = document.documentElement;
    // Always an attribute, "auto" included - it is what the light media
    // query keys on, and removing it would mean dark, not device-follow.
    root.setAttribute("data-theme", MODES.includes(mode) ? mode : "dark");
    document.dispatchEvent(new CustomEvent("ah:theme", { detail: { mode, effective: effectiveTheme() } }));
  }

  function effectiveTheme() {
    const set = document.documentElement.getAttribute("data-theme");
    if (set === "light" || set === "dark") return set;
    if (set !== "auto") return "dark";      // no attribute means dark now
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light" : "dark";
  }

  function setTheme(mode) {
    try {
      // "auto" is stored too: with dark as the default, "nothing stored"
      // and "follow the device" are no longer the same state.
      localStorage.setItem(THEME_KEY, MODES.includes(mode) ? mode : "dark");
    } catch (err) { /* not fatal: it just will not be remembered */ }
    applyTheme(mode);
  }

  applyTheme(readTheme());
  // Following the device means following it as it changes, not only at load.
  if (window.matchMedia) {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => { if (readTheme() === "auto") applyTheme("auto"); };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }

  window.ahTheme = { get: readTheme, set: setTheme, effective: effectiveTheme };

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // Apps answer /api/me differently; take whichever fields are there.
  function normalise(d) {
    if (!d) return null;
    const user = d.user || d.username || null;
    if (!user) return null;
    const groups = Array.isArray(d.groups) ? d.groups : [];
    const role = d.role || (groups.includes("admins") ? "admin" : null);
    return { user, groups, role };
  }

  function render(mount, me, logout) {
    const initial = me.user.charAt(0).toUpperCase();
    const isAdmin = me.role === "admin" || me.groups.includes("admins");
    mount.classList.add("ah-user");
    mount.innerHTML = `
      <button type="button" class="ah-user__btn" aria-haspopup="menu" aria-expanded="false">
        <span class="ah-user__avatar" aria-hidden="true">${esc(initial)}</span>
        <span class="ah-user__name">${esc(me.user)}</span>
        ${me.role ? `<span class="ah-user__role${isAdmin ? " ah-user__role--admin" : ""}">${esc(me.role)}</span>` : ""}
      </button>
      <div class="ah-user__menu" role="menu" hidden>
        <div class="ah-user__head">
          Signed in via single sign-on
          ${me.groups.length ? `<div class="ah-user__groups">${
            me.groups.map((g) => `<span class="ah-user__group">${esc(g)}</span>`).join("")}</div>` : ""}
        </div>
        <div class="ah-user__theme" role="group" aria-label="Appearance">
          <span>Appearance</span>
          <div class="ah-user__seg">
            ${["auto", "light", "dark"].map((m) => `<button type="button" data-theme-set="${m}"
               class="ah-user__segbtn" aria-pressed="false">${m}</button>`).join("")}
          </div>
        </div>
        ${AUTH ? `<a class="ah-user__item" role="menuitem" href="${AUTH}/settings">Account settings</a>` : ""}
        <a class="ah-user__item ah-user__item--out" role="menuitem" href="${esc(logout)}">Sign out</a>
      </div>`;

    const paintTheme = () => {
      const now = readTheme();
      mount.querySelectorAll("[data-theme-set]").forEach((b) => {
        b.setAttribute("aria-pressed", String(b.dataset.themeSet === now));
      });
    };
    mount.querySelectorAll("[data-theme-set]").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        setTheme(b.dataset.themeSet);
        paintTheme();
      });
    });
    paintTheme();

    const btn = mount.querySelector(".ah-user__btn");
    const menu = mount.querySelector(".ah-user__menu");
    const setOpen = (open) => {
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    };
    btn.addEventListener("click", (e) => { e.stopPropagation(); setOpen(menu.hidden); });
    document.addEventListener("click", (e) => {
      if (!menu.hidden && !mount.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if ((e.key === "Escape" || e.key === "Esc") && !menu.hidden) {
        setOpen(false);
        btn.focus();
        e.stopPropagation();   // leave any dialog underneath alone
      }
    }, true);

    // Let the host page react to who is signed in -- the hub hides its editing
    // controls from non-admins this way.
    document.documentElement.classList.toggle("ah-is-admin", isAdmin);
    document.dispatchEvent(new CustomEvent("ah:user", { detail: me }));
  }

  function mountAll() {
    injectStyle();
    document.querySelectorAll("[data-ah-user]").forEach((mount) => {
      const endpoint = mount.dataset.endpoint || "/api/me";
      // Signing out of the app alone would leave the SSO session standing and
      // let you straight back in, so the default ends the Authelia session.
      const logout = mount.dataset.logout ||
        (AUTH ? `${AUTH}/logout?rd=${encodeURIComponent(location.origin + "/")}` : "/logout");
      fetch(endpoint, { cache: "no-store", credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          const me = normalise(d);
          if (me) render(mount, me, logout);
        })
        .catch(() => { /* no identity, no chrome */ });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountAll);
  } else {
    mountAll();
  }
})();
