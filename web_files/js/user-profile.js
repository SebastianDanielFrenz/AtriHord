"use strict";

/* Who am I, and how do I sign out.
 *
 * Identity comes from Authelia now, so the top bar shows the account you are
 * actually signed in as rather than a bare Logout link -- on a shared setup it
 * matters whether you are looking at the admin view or a guest's. Signing out
 * is still one click, tucked into the menu where it cannot be hit by accident.
 *
 * The role shown is web-dl's own (admin/guest), derived from your Authelia
 * groups, which is what decides what the page will actually let you do. */

(function () {
  const btn = document.getElementById("userProfileBtn");
  const menu = document.getElementById("userMenu");
  if (!btn || !menu) return;

  fetch("/api/me", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((me) => {
      if (!me || !me.username) return;
      const name = document.getElementById("userName");
      const role = document.getElementById("userRole");
      const avatar = document.getElementById("userAvatar");
      name.textContent = me.username;
      avatar.textContent = me.username.charAt(0).toUpperCase();
      if (role && me.role) {
        role.textContent = me.role;
        role.classList.add(me.role === "admin" ? "is-admin" : "is-guest");
      }
      btn.title = `Signed in as ${me.username} (${me.role || "user"})`;
    })
    .catch(() => { /* the page is still usable without the badge */ });

  const setOpen = (open) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(menu.hidden);
  });

  // Dismissable the same ways every other popup here is: click away, Escape.
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Escape" || e.key === "Esc") && !menu.hidden) {
      setOpen(false);
      btn.focus();
      e.stopPropagation();   // don't also close a dialog underneath
    }
  }, true);
})();
