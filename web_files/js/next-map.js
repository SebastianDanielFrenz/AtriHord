"use strict";

/* The Map destination of media-next.html: every picture that carries a GPS position (phone photos;
 * downloaded art has none), on a Leaflet map. Client of GET /api/media/places, which lists the pictures
 * with lat/lon in the same entry shape as the Timeline.
 *
 * Pictures close together are one round marker with a count (a grid clustering in screen pixels,
 * redone on every zoom and pan - a few thousand points, so no index is needed). Clicking a marker lists
 * its pictures under the map as the usual tiles (createFileTile), so selection, favourites and the viewer
 * behave as everywhere. Leaflet is served from this site (js/vendor/leaflet.js); only the map's own
 * background tiles come from tile.openstreetmap.org, and the points show even if those cannot load.
 * Classic script; the shell calls NextMap.open() when the destination opens. */

(function () {
  const $ = (id) => document.getElementById(id);
  const CELL = 56;                    // px: markers closer than this become one
  const LIST_MAX = 400;

  let map = null;
  let layer = null;
  let items = [];
  let loaded = false;
  let loading = false;

  function marker(cluster) {
    const n = cluster.items.length;
    const size = n === 1 ? 14 : n < 10 ? 30 : n < 100 ? 38 : 46;
    const icon = L.divIcon({
      className: "nx-map-pin" + (n === 1 ? " is-single" : ""),
      html: n === 1 ? "" : `<span>${n > 999 ? Math.round(n / 100) / 10 + "k" : n}</span>`,
      iconSize: [size, size],
    });
    const m = L.marker([cluster.lat, cluster.lon], { icon, keyboard: true, title: n === 1 ? cluster.items[0].name : `${n} pictures` });
    m.on("click", () => showList(cluster.items));
    return m;
  }

  function redraw() {
    if (!map || !layer) return;
    layer.clearLayers();
    const bounds = map.getBounds().pad(0.2);
    const cells = new Map();
    for (const it of items) {
      if (!bounds.contains([it.lat, it.lon])) continue;
      const p = map.latLngToContainerPoint([it.lat, it.lon]);
      const key = Math.floor(p.x / CELL) + ":" + Math.floor(p.y / CELL);
      let c = cells.get(key);
      if (!c) { c = { items: [], lat: 0, lon: 0 }; cells.set(key, c); }
      c.items.push(it);
      c.lat += it.lat;
      c.lon += it.lon;
    }
    for (const c of cells.values()) {
      c.lat /= c.items.length;
      c.lon /= c.items.length;
      layer.addLayer(marker(c));
    }
  }

  function showList(list) {
    const host = $("nxMapList");
    const sorted = list.slice().sort((a, b) => b.mtime - a.mtime);
    const shown = sorted.slice(0, LIST_MAX);
    host.replaceChildren();
    const head = document.createElement("div");
    head.className = "nx-map-listhead";
    head.textContent = sorted.length > LIST_MAX
      ? `${sorted.length.toLocaleString("en-GB")} pictures here - the newest ${LIST_MAX} are listed; zoom in to narrow it down`
      : `${sorted.length.toLocaleString("en-GB")} picture${sorted.length === 1 ? "" : "s"}`;
    const grid = document.createElement("div");
    grid.className = "media-grid media-gallery";
    for (const f of shown) grid.appendChild(createFileTile(f, shown));
    host.append(head, grid);
    host.hidden = false;
    host.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function ensureMap() {
    if (map) return;
    if (typeof L === "undefined") { $("nxMapStatus").textContent = "The map library did not load."; $("nxMapStatus").hidden = false; return; }
    map = L.map("nxMapCanvas", { worldCopyJump: true, minZoom: 2 });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
    }).addTo(map);
    layer = L.layerGroup().addTo(map);
    map.on("moveend zoomend", redraw);
    map.setView([30, 10], 2);
  }

  function fit() {
    if (!items.length) return;
    map.fitBounds(L.latLngBounds(items.map((i) => [i.lat, i.lon])).pad(0.1), { maxZoom: 12 });
  }

  async function load() {
    if (loading) return;
    loading = true;
    const status = $("nxMapStatus");
    status.hidden = false;
    status.textContent = "Loading…";
    try {
      const res = await fetch("/api/media/places");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      items = data.items || [];
      loaded = true;
      const idx = data.index || {};
      if (items.length) status.hidden = true;
      else {
        status.textContent = idx.state === "scanning" || !idx.finished
          ? `No positions yet - the library is still being read for them (${(idx.checked || 0).toLocaleString("en-GB")} of ${(idx.total || 0).toLocaleString("en-GB")} pictures looked at).`
          : "No picture in the library carries a GPS position.";
      }
      $("nxMapSum").textContent = items.length ? `${items.length.toLocaleString("en-GB")} pictures with a position` : "";
      if (map) { fit(); redraw(); }
    } catch (e) {
      status.textContent = "The map data could not be loaded.";
    } finally {
      loading = false;
    }
  }

  async function open() {
    ensureMap();
    if (!map) return;
    $("nxMapList").hidden = true;
    // The canvas was display:none until now; Leaflet needs its real size.
    requestAnimationFrame(() => {
      map.invalidateSize();
      if (loaded) { fit(); redraw(); } else load();
    });
  }

  window.NextMap = { open };
})();
