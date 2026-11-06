"use strict";

/* The Explore destination of media-next.html: rows of the people and the characters the library
 * knows, each a strip of round tiles with "See all" into the full page. Same tiles and clicks as
 * those pages (browsePerson / browseCharacter in media.js), without their edit tools. A row shows
 * the biggest groups first (the server's own order) and is left out when it has nothing to show.
 * Classic script; the shell calls NextExplore.open() when the destination opens. */

(function () {
  const $ = (id) => document.getElementById(id);
  const COUNT = 16;
  let gen = 0;

  function row(title, hint, seeAll) {
    const sec = document.createElement("section");
    sec.className = "nx-ex-row";
    const head = document.createElement("div");
    head.className = "nx-ex-head";
    const h = document.createElement("h3");
    h.textContent = title;
    const all = document.createElement("button");
    all.type = "button";
    all.className = "btn btn-quiet";
    all.textContent = "See all";
    all.title = hint;
    all.addEventListener("click", seeAll);
    head.append(h, all);
    const strip = document.createElement("div");
    strip.className = "nx-ex-strip";
    sec.append(head, strip);
    return { sec, strip };
  }

  function personTileLite(p) {
    const tile = document.createElement("div");
    tile.className = "person-tile nx-ex-tile";
    tile.tabIndex = 0;
    tile.setAttribute("role", "button");
    const art = document.createElement("div");
    art.className = "person-cover";
    if (p.face_id) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "";
      img.src = "/api/media/face/" + p.face_id + "/thumb";
      img.addEventListener("error", () => { art.textContent = "\u{1F464}"; }, { once: true });
      art.appendChild(img);
    } else art.textContent = "\u{1F464}";
    const name = document.createElement("div");
    name.className = "person-name";
    name.textContent = p.name || p.auto_name || "Unnamed";
    if (!p.name) name.classList.add(p.auto_name ? "is-auto" : "is-unnamed");
    tile.append(art, name);
    tile.setAttribute("aria-label", `Find media with ${name.textContent}`);
    const go = () => browsePerson(p);
    tile.addEventListener("click", go);
    tile.addEventListener("keydown", (e) => { if (e.target === tile && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); go(); } });
    return tile;
  }

  function characterTileLite(c) {
    const tile = document.createElement("div");
    tile.className = "character-tile nx-ex-tile";
    const art = document.createElement("div");
    art.className = "character-cover";
    if (c.cover) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "";
      img.src = thumbnailUrlFor(c.cover);
      img.addEventListener("error", () => { art.textContent = "\u{1F3AD}"; }, { once: true });
      art.appendChild(img);
    } else art.textContent = "\u{1F3AD}";
    const name = document.createElement("div");
    name.className = "character-name";
    name.textContent = c.label;
    const meta = document.createElement("div");
    meta.className = "character-meta";
    meta.textContent = c.series ? `${c.series} · ${c.count}` : String(c.count);
    tile.append(art, name, meta);
    tile.addEventListener("click", () => browseCharacter(c));
    return tile;
  }

  async function fetchRows(kind, extra, limit) {
    const res = await fetch(`/api/media/discovery/${kind}?limit=${limit || COUNT}${extra || ""}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    return (await res.json()).items || [];
  }

  async function open() {
    const host = $("nxExRows");
    const status = $("nxExStatus");
    if (!host) return;
    const mine = ++gen;
    status.hidden = false;
    status.textContent = "Loading…";
    // The library's guessed names count as names (nobody types them in): people with a name or a
    // guess come first, the unnamed only fill a short row.
    let [people, chars] = await Promise.all([
      fetchRows("people", "&show=real", 48).catch(() => []),
      fetchRows("characters").catch(() => []),
    ]);
    const known = people.filter((p) => p.name || p.auto_name);
    people = known.length >= 8 ? known.slice(0, COUNT) : people.slice(0, COUNT);
    if (mine !== gen) return;
    host.replaceChildren();
    if (people.length) {
      const r = row("People", "All recognised people", () => setBrowseView("people"));
      for (const p of people) r.strip.appendChild(personTileLite(p));
      host.appendChild(r.sec);
    }
    if (chars.length) {
      const r = row("Characters", "All characters", () => setBrowseView("characters"));
      for (const c of chars) r.strip.appendChild(characterTileLite(c));
      host.appendChild(r.sec);
    }
    status.hidden = !!host.children.length;
    status.textContent = host.children.length ? "" : "Nothing to explore yet - name some people in People, and the tagger fills Characters.";
  }

  window.NextExplore = { open };
})();
