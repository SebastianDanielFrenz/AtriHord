"use strict";

/* The Timeline destination of media-next.html: every photo and video, newest first, in month
 * sections. Client of GET /api/media/timeline/buckets (month and year counts, from the server's
 * date index) and GET /api/media/timeline?bucket=YYYY-MM (one page of a month). The date is the
 * file's own (mtime), not when it landed here: that is what /api/media/recent (Recent) answers.
 *
 * A month heading is drawn for every bucket at once (cheap: one row each); the tiles of a month
 * load only when it nears the screen, and a long month keeps loading while its end is visible.
 * Tiles are the same ones every other grid uses (createFileTile), so selection, the viewer and
 * favourites behave the same. The viewer's arrow keys walk the month that was opened.
 * Classic script; the shell calls NextTimeline.open() when the destination opens. */

(function () {
  const $ = (id) => document.getElementById(id);
  const PAGE = 120;
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  let kinds = "image,video";
  let buckets = [];
  let gen = 0;
  let observer = null;

  function label(key) {
    return key.length === 4 ? key : `${MONTHS[Number(key.slice(5, 7)) - 1]} ${key.slice(0, 4)}`;
  }

  async function loadSection(sec) {
    if (sec.dataset.busy || sec.dataset.done) return;
    sec.dataset.busy = "1";
    const myGen = gen;
    const list = sec._items || (sec._items = []);
    try {
      const res = await fetch(`/api/media/timeline?bucket=${sec.dataset.key}&type=${kinds}&offset=${list.length}&limit=${PAGE}`);
      if (myGen !== gen || !res.ok) return;
      const data = await res.json();
      const grid = sec.querySelector(".nx-tl-grid");
      for (const f of data.items) { list.push(f); grid.appendChild(createFileTile(f, list)); }
      sec.querySelector(".nx-tl-count").textContent = data.total.toLocaleString();
      if (!data.has_more || !data.items.length) {
        sec.dataset.done = "1";
        grid.style.minHeight = "";
      }
    } catch (e) { /* the next scroll retries */ } finally {
      delete sec.dataset.busy;
    }
    // Still near the screen (a tall window, a short month)? The observer will not fire again by itself.
    if (myGen === gen && !sec.dataset.done && sec.getBoundingClientRect().bottom < innerHeight + 800) loadSection(sec);
  }

  function draw() {
    const root = $("nxTlList");
    gen++;
    if (observer) observer.disconnect();
    observer = new IntersectionObserver((entries) => {
      for (const e of entries) if (e.isIntersecting) loadSection(e.target);
    }, { rootMargin: "900px 0px" });
    const frag = document.createDocumentFragment();
    let year = null;
    for (const b of buckets) {
      const y = b.key.slice(0, 4);
      if (y !== year) {
        year = y;
        const h = document.createElement("h3");
        h.className = "nx-tl-year";
        h.id = `nxTlY${y}`;
        h.textContent = y;
        frag.appendChild(h);
      }
      const sec = document.createElement("section");
      sec.className = "nx-tl-sec";
      sec.dataset.key = b.key;
      sec.innerHTML = '<header><h4></h4><span class="nx-tl-count"></span></header><div class="nx-tl-grid media-grid media-gallery"></div>';
      sec.querySelector("h4").textContent = label(b.key);
      sec.querySelector(".nx-tl-count").textContent = b.count.toLocaleString();
      // Room for the tiles before they arrive, so the scrollbar and the jump links are honest.
      sec.querySelector(".nx-tl-grid").style.minHeight = `${Math.min(900, Math.ceil(b.count / 6) * 170)}px`;
      frag.appendChild(sec);
      observer.observe(sec);
    }
    root.replaceChildren(frag);
    $("nxTlYears").replaceChildren(...[...new Set(buckets.map((b) => b.key.slice(0, 4)))].map((y) => {
      const a = document.createElement("button");
      a.type = "button";
      a.textContent = y;
      a.addEventListener("click", () => { const t = $(`nxTlY${y}`); if (t) t.scrollIntoView({ block: "start" }); });
      return a;
    }));
  }

  async function open() {
    const status = $("nxTlStatus");
    status.hidden = false;
    status.textContent = "Loading...";
    try {
      const res = await fetch(`/api/media/timeline/buckets?type=${kinds}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      buckets = data.buckets;
      const ix = data.index || {};
      const building = ix.state === "scanning" || (!buckets.length && ix.state !== "error");
      status.hidden = !building && !!buckets.length;
      status.textContent = building
        ? `Still indexing the library's dates${ix.files ? ` (${ix.files.toLocaleString()} files so far)` : ""}. Reload in a few minutes for the rest.`
        : buckets.length ? "" : "Nothing to show.";
      $("nxTlSum").textContent = data.total ? `${data.total.toLocaleString()} items` : "";
      draw();
    } catch (e) {
      status.hidden = false;
      status.textContent = `Could not load the timeline: ${e.message}`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const chip of document.querySelectorAll("#nxTlKinds [data-kinds]")) {
      chip.addEventListener("click", () => {
        kinds = chip.dataset.kinds;
        for (const c of document.querySelectorAll("#nxTlKinds [data-kinds]")) c.setAttribute("aria-pressed", String(c === chip));
        open();
      });
    }
  });

  window.NextTimeline = { open };
})();
