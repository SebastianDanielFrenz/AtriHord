/* The sources of the wall (P8 V1): what the flat page lists, as one paged list of tiles.
 *   folder     Library: GET /api/media/browse (roots -> folders -> files; folders first as folder tiles)
 *   recent     GET /api/media/recent (paged by offset/limit)
 *   favorites  GET /api/media/search?favorites=1 (paged)
 *   search     GET /api/media/search?q&mode&tag&person (paged)
 *   3d         GET /api/media/3d/list (every finished conversion)
 * They use the site's own listing routes and thumbnail URLs (same origin, cookies). Nothing here touches the
 * DOM or three.js; the network goes through an injected getJson so the harness can script it.
 *
 * A tile ("item"): { id, type: "folder"|"picture"|"video", name, root, path, aspect, thumb, mtime, size, favorite,
 *   duration, status? } - for a folder tile root/path name the folder it opens. */
import { mediaKindOf, fileUrl, sourceTitle, baseName } from "./logic.js";
import { discoverySpec, subjectItems, homeItems, loadSubjects, loadSubjectPage } from "./discovery.js";
import { WALL } from "./layout.js";

export class ApiError extends Error {
  constructor(status, message, extra) {
    super(message || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    if (extra) Object.assign(this, extra);
  }
  get expired() { return this.status === 401 || (this.status === 0 && this.opaque === true); }
}

// The one place every API call goes through: a lapsed session (a 401, or a redirect to the sign-in portal)
// becomes an ApiError with `expired`, which the app shows once instead of retrying.
export function makeGetJson(fetchImpl) {
  const f = fetchImpl || ((...a) => fetch(...a));
  return async function getJson(url, init) {
    let res;
    try {
      res = await f(url, Object.assign({ credentials: "same-origin", redirect: "manual" }, init || {}));
    } catch (e) {
      throw new ApiError(-1, "network", { network: true });
    }
    if (res.type === "opaqueredirect") throw new ApiError(0, "session expired", { opaque: true });
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) throw new ApiError(res.status, body && body.error ? String(body.error) : `HTTP ${res.status}`, body && typeof body === "object" ? { body } : undefined);
    return body;
  };
}

export const PAGE = 100;

// The Favourites tile (root listing only): a folder-like tile that opens the favourites source.
export const FAVOURITES_ID = "favorites|special";
export function favouritesTile() {
  return { id: FAVOURITES_ID, type: "folder", special: "favorites", name: "Favourites", root: "", path: "", aspect: WALL.folderAspect, thumb: "", mtime: 0, size: 0, favorite: false, duration: 0 };
}
export function favouritesSpec(base = {}) {
  return { kind: "favorites", root: "", path: "", query: "", filters: base.filters || "", sort: { key: "", dir: "" }, offset: 0, via: "libraries" };
}

export const thumbUrl = (root, path, mtime) =>
  `/api/media/thumbnail?path=${encodeURIComponent(path || "")}&root=${encodeURIComponent(root || "")}&v=${Math.floor(Number(mtime) || 0)}`;

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : 0);

// One entry of a browse/recent/search answer -> a tile, or null for what VR cannot show (audio, gif, ...).
export function entryToItem(e, fallbackRoot = "") {
  if (!e || typeof e !== "object") return null;
  const root = typeof e.root === "string" ? e.root : fallbackRoot;
  const path = typeof e.path === "string" ? e.path : "";
  if (!path && !e.is_dir) return null;
  if (e.is_dir || e.type === "folder") {
    return { id: `${root}|${path}|dir`, type: "folder", name: e.name || baseName(path), root, path, aspect: WALL.folderAspect, thumb: "", mtime: 0, size: 0, favorite: false, duration: 0 };
  }
  const kind = mediaKindOf(path);
  if (!kind) return null;
  const w = num(e.width), h = num(e.height);
  const known = w > 0 && h > 0;
  return {
    id: `${root}|${path}`, type: kind, name: e.name || baseName(path), root, path,
    aspect: known ? w / h : (kind === "video" ? 16 / 9 : 1),
    aspectKnown: known,
    thumb: thumbUrl(root, path, e.mtime), mtime: num(e.mtime), size: num(e.size), added: num(e.added),
    favorite: !!e.favorite, duration: num(e.duration),
    url: typeof e.url === "string" && e.url ? e.url : fileUrl(root, path),
    mediaId: e.id != null ? e.id : null,
    artist: typeof e.artist === "string" ? e.artist : "",
  };
}

const collator = typeof Intl !== "undefined" ? new Intl.Collator(undefined, { numeric: true, sensitivity: "base" }) : null;
const byName = (a, b) => (collator ? collator.compare(a.name, b.name) : (a.name < b.name ? -1 : (a.name > b.name ? 1 : 0)));

// Folder tiles keep their order (alphabetical, as the server sends them); files follow the flat page's sort.
export function sortFiles(files, sort) {
  const key = sort && sort.key ? sort.key : "name";
  const dir = sort && sort.dir === "desc" ? -1 : 1;
  const cmp = key === "size" ? (a, b) => a.size - b.size
    : (key === "latest" || key === "added") ? (a, b) => (key === "added" ? (a.added || a.mtime) - (b.added || b.mtime) : a.mtime - b.mtime)
      : key === "artist" ? (a, b) => collator.compare(a.artist || "", b.artist || "")
        : key === "id" ? (a, b) => collator.compare(String(a.mediaId ?? a.name), String(b.mediaId ?? b.name))
          : byName;
  return files.slice().sort((a, b) => dir * cmp(a, b) || byName(a, b));
}

export function folderSpec(root, path, base = {}) {
  return { kind: "folder", root: root || "", path: path || "", query: "", filters: base.filters || "", sort: base.sort || { key: "", dir: "" }, offset: 0 };
}

// The folder an item lives in, as a source (a context with an item but no source).
export function folderOfItem(item) {
  const parts = String(item && item.path || "").split("/").filter(Boolean);
  parts.pop();
  return folderSpec(item && item.root || "", parts.join("/"));
}

export const specKey = (s) => [s.kind, s.root, s.path, s.query, s.filters, s.mode || "", s.tag || "", s.person || "",
  s.poolTag || "", s.variant || "", s.show || "", s.named ? "1" : "0", s.sort && s.sort.key || "", s.sort && s.sort.dir || ""].join("\u0001");

/* createSource(spec, { getJson }) -> source
 *   source.items            the tiles so far (grows with loadMore; folder sources have everything at once)
 *   source.hasMore, loading, error ({status, expired, message} | null), total (null = unknown)
 *   source.crumbs           [{ label, spec }] for a folder source (the breadcrumb), else a single entry
 *   source.title
 *   await source.loadMore() one more page; await source.ensure(index) pages until `index` exists or no more
 *   source.indexOf(root, path)
 *   source.onChange = fn    called after items/state change */
export function createSource(spec, { getJson, pageSize = () => PAGE }) {
  const s = {
    spec,
    items: [],
    hasMore: true,
    loading: false,
    error: null,
    total: null,
    crumbs: [],
    title: sourceTitle(spec),
    onChange: null,
    ready: false,
    multiRoot: false,
    rootListing: false,
    rootName: "",
  };
  let pending = null, nextOffset = 0, version = 0;
  const changed = () => { if (typeof s.onChange === "function") s.onChange(s); };
  const fail = (e) => {
    s.error = { status: e && e.status, expired: !!(e && e.expired), network: !!(e && e.network), message: e && e.message ? String(e.message) : "error" };
    s.hasMore = false;
  };
  const add = (list) => { for (const it of list) if (it) s.items.push(it); };
  const sp = spec;
  if (sp.kind === "favorites" && sp.via === "libraries") {
    s.title = "Libraries / Favourites";
    s.crumbs = [{ label: "Libraries", spec: folderSpec("", "", sp) }, { label: "Favourites", spec: sp }];
  }
  const filters = sp.filters ? `&${sp.filters}` : "";

  async function loadFolder() {
    const q = `path=${encodeURIComponent(sp.path)}&root=${encodeURIComponent(sp.root)}${filters}`;
    const data = await getJson(`/api/media/browse?${q}`);
    s.multiRoot = !!data.multi_root;
    s.rootListing = !!data.is_root_listing;
    if (data.is_root_listing) {
      s.crumbs = [{ label: "Libraries", spec: folderSpec("", "", sp) }];
      s.title = "Libraries";
      add([favouritesTile()]);
      add((data.roots || []).map((r) => entryToItem({ is_dir: true, name: r.name || r.id, root: r.id, path: "" }, r.id)));
    } else {
      const root = data.root || sp.root;
      s.rootName = data.root_name || root;
      s.crumbs = [];
      if (data.multi_root) s.crumbs.push({ label: "Libraries", spec: folderSpec("", "", sp) });
      s.crumbs.push({ label: s.rootName, spec: folderSpec(root, "", sp) });
      const parts = String(data.path || sp.path || "").split("/").filter(Boolean);
      parts.forEach((p, i) => s.crumbs.push({ label: p, spec: folderSpec(root, parts.slice(0, i + 1).join("/"), sp) }));
      s.title = s.crumbs.map((c) => c.label).join(" / ");
      const dirs = Array.isArray(data.dir_info) && data.dir_info.length ? data.dir_info.map((d) => d.name) : (data.dirs || []);
      add(dirs.map((name) => entryToItem({ is_dir: true, name, root, path: [data.path || sp.path || "", name].filter(Boolean).join("/") }, root)));
      const files = (data.files || []).map((e) => entryToItem(e, root)).filter((x) => x && x.type !== "folder");
      add(sortFiles(files, sp.sort));
    }
    s.total = s.items.length;
    s.hasMore = false;
  }

  async function loadPaged(url) {
    const data = await getJson(`${url}${url.includes("?") ? "&" : "?"}offset=${nextOffset}&limit=${PAGE}`);
    const list = Array.isArray(data.items) ? data.items : [];
    add(list.map((e) => entryToItem(e, "")));
    // The server's own step, not list.length: it leaves gone and empty files out of a page, and counting only
    // what came back would ask for the end of that page again (repeated tiles) or stop at a page it emptied.
    const step = typeof data.limit === "number" && data.limit > 0 ? data.limit : list.length;
    nextOffset = (typeof data.offset === "number" ? data.offset : nextOffset) + step;
    if (typeof data.total === "number") s.total = data.total;
    s.hasMore = !!data.has_more && step > 0;
  }

  const searchUrl = () => {
    const p = [];
    if (sp.kind === "favorites") p.push("favorites=1");
    if (sp.query) p.push(`q=${encodeURIComponent(sp.query)}`);
    if (sp.mode) p.push(`mode=${encodeURIComponent(sp.mode)}`);
    if (sp.tag) p.push(`tag=${encodeURIComponent(sp.tag)}`);
    if (sp.person) p.push(`person=${encodeURIComponent(sp.person)}`);
    if (sp.query && (sp.tag || sp.person || sp.kind === "favorites")) p.push("narrow=1");
    return `/api/media/search?${p.join("&")}${filters}`;
  };

  async function loadDiscovery() {
    if (sp.kind === "home") {
      add(homeItems());
      changed();
      await Promise.all(["characters","people"].map(async kind => {
        try { add(subjectItems(kind,await loadSubjects(kind,getJson,Math.max(1,Math.floor((pageSize()-6)/2))),"",sp));changed(); }
        catch(e) { if(e.expired)throw e; }
      }));
      s.total = s.items.length; s.hasMore = false;
    } else {
      const data=await loadSubjectPage(sp.kind,getJson,{limit:pageSize(),offset:nextOffset,query:sp.query||'',show:sp.show||'real',named:!!sp.named});
      add(subjectItems(sp.kind,data.items||[], '',sp));
      nextOffset=data.next_offset??nextOffset+(data.items||[]).length;
      s.total=data.total??s.items.length;s.hasMore=!!data.has_more;
    }
  }

  async function load3d() {
    const data = await getJson("/api/media/3d/list");
    const list = Array.isArray(data.items) ? data.items : [];
    add(list.map((e) => {
      const it = entryToItem({ root: e.root, path: e.path, name: e.title || baseName(e.path || ""), favorite: e.favorite, width: e.eye_w, height: e.eye_h, duration: e.duration }, e.root || "");
      if (it) it.status = { state: "done", key: e.key, sbs_url: e.sbs_url, kind: e.kind, eye_w: e.eye_w, eye_h: e.eye_h };
      return it;
    }));
    s.total = s.items.length;
    s.hasMore = false;
  }

  s.loadMore = () => {
    if (pending) return pending;
    if (!s.hasMore || s.error) return Promise.resolve(false);
    s.loading = true;
    changed();
    const my = version;
    pending = (async () => {
      try {
        if (["home", "people", "characters"].includes(sp.kind)) await loadDiscovery();
        else if (sp.kind === "folder") await loadFolder();
        else if (sp.kind === "recent") await loadPaged("/api/media/recent?");
        else if (sp.kind === "3d") await load3d();
        else await loadPaged(searchUrl());
        s.ready = true;
      } catch (e) {
        fail(e);
      } finally {
        if (my === version) { s.loading = false; pending = null; changed(); }
      }
      return true;
    })();
    return pending;
  };

  // Pages until item `index` exists (or nothing more comes); a cap keeps a bad answer from looping.
  s.ensure = async (index) => {
    for (let guard = 0; guard < 60 && s.items.length <= index && s.hasMore && !s.error; guard += 1) await s.loadMore();
    return s.items.length > index;
  };
  s.indexOf = (root, path) => {
    const items = s.items;
    for (let i = 0; i < items.length; i += 1) if (items[i].type !== "folder" && items[i].path === path && items[i].root === root) return i;
    return -1;
  };
  s.dispose = () => { version += 1; s.onChange = null; };
  return s;
}
