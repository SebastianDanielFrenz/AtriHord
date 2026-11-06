/* Recognized subjects shared by the immersive wall and standalone launch page. */
export const discoverySpec = (kind, query = '', base = {}) => ({ kind, root: '', path: '', query,
  filters: base.filters || '', sort: { key: '', dir: '' }, offset: 0,
  ...(kind === 'people' ? { show: base.show || 'real', named: !!base.named } : {}) });
const match = (value, query) => String(value || '').normalize('NFKD').toLocaleLowerCase().includes(String(query || '').normalize('NFKD').toLocaleLowerCase().trim());
const coverUrl = c => c && c.path ? `/api/media/thumbnail?${new URLSearchParams({root:c.root || '',path:c.path,v:String(c.mtime || 0)})}` : '';
export function subjectItems(kind, rows, query = '', base = {}) {
  return (rows || []).filter(row => kind === 'people'
    ? match([row.name, row.auto_name, ...(row.auto_accounts || []), `Person ${row.id}`].join(' '), query)
    : match([row.label, row.tag, row.series].join(' '), query)).map(row => {
    const person = kind === 'people';
    const name = person ? (row.name || row.auto_name || `Person ${row.id}`) : (row.label || row.tag);
    const source = discoverySpec('search', '', base);
    source.label = name; source.mode = 'context';
    let variants = [];
    if (person) source.person = String(row.id);
    else {
      variants = (Array.isArray(row.variants) ? row.variants : []).filter(v => v && v.tag).map(v => ({
        tag: String(v.tag), label: String(v.label || v.tag), count: Number(v.count) || 0, cover: v.cover || null,
      }));
      const tag = String(row.tag || row.key || '');
      if (!variants.length && tag) variants = [{ tag, label: name, count: Number(row.count) || 0, cover: row.cover || null }];
      source.poolTag = tag;
      source.variant = '';
      source.variants = variants;
      source.tag = variants.length ? variants.map(v => v.tag).join(',') : tag;
      source.series = String(row.series || '');
      source.count = Number(row.count) || 0;
      source.cover = row.cover || null;
    }
    return { id: `${kind}|${person ? row.id : row.tag}`, type: 'subject', name, root:'', path:'',
      subjectKind: kind, count: Number(row.count) || 0, aspect: 1, aspectKnown: true, favorite:false,
      thumb: person ? (row.face_id ? `/api/media/face/${row.face_id}/thumb` : '') : coverUrl(row.cover),
      source, variants, series: String(row.series || ''), poolTag: person ? '' : String(row.tag || row.key || ''),
      mtime:0, size:0, duration:0 };
  });
}

function cleanPool(row) {
  if (!row || typeof row !== 'object') return null;
  const tag = String(row.poolTag || row.tag || row.key || '').trim();
  if (!tag) return null;
  const variants = (Array.isArray(row.variants) ? row.variants : []).filter(v => v && v.tag).map(v => ({
    tag: String(v.tag), label: String(v.label || v.tag), count: Number(v.count) || 0, cover: v.cover || null,
  }));
  return { tag, label: String(row.label || tag), series: String(row.series || ''), count: Number(row.count) || 0,
    cover: row.cover || null, variants: variants.length ? variants : [{ tag, label: String(row.label || tag), count: Number(row.count) || 0, cover: row.cover || null }] };
}

// Matches the desktop's canonical character-tag typeahead.
export async function loadCharacterSuggestions(getJson, query, limit = 8) {
  const q = String(query || '').trim();
  if (q.length < 2) return [];
  const params = new URLSearchParams({ limit: String(Math.max(1, Math.min(30, limit))), q });
  const data = await getJson(`/api/media/characters?${params}`);
  return (data && Array.isArray(data.items) ? data.items : []).map(cleanPool).filter(Boolean);
}

export async function loadCharacterPool(getJson, tag) {
  const key = String(tag || '').trim();
  if (!key) return null;
  try {
    const params = new URLSearchParams({ tag: key });
    return cleanPool(await getJson(`/api/media/characters/pool?${params}`));
  } catch (_) { return null; }
}

export function characterSource(pool, variantTag = '', base = {}) {
  const p = cleanPool(pool);
  if (!p) return null;
  const variant = p.variants.find(v => v.tag === variantTag);
  const source = discoverySpec('search', '', base);
  const samePool = !!base && base.poolTag === p.tag;
  source.query = samePool ? (base.query || '') : '';
  source.mode = samePool ? (base.mode || 'context') : 'context';
  source.label = variant && variant.label !== 'Default' ? `${p.label} (${variant.label})` : p.label;
  source.tag = variant ? variant.tag : p.variants.map(v => v.tag).join(',');
  source.poolTag = p.tag;
  source.variant = variant ? variant.tag : '';
  source.series = p.series;
  source.count = p.count;
  source.cover = p.cover;
  source.variants = p.variants;
  return source;
}
export const HOME_LINKS = [
  ['recent','Recently added','The newest pictures and videos'], ['favorites','Favourites','Your saved media'],
  ['characters','Characters','Browse character circles'], ['people','Faces','Find a recognized person'],
  ['folder','Libraries','Folders across your media libraries'], ['3d','Ready in 3D','Finished stereoscopic media'],
];
export function homeItems() {
  return HOME_LINKS.map(([kind,name,description]) => ({id:`home|${kind}`,type:'folder',name,description,
    special:kind,source:discoverySpec(kind),root:'',path:'',aspect:1.6,thumb:'',mtime:0,size:0,favorite:false,duration:0}));
}
export async function loadSubjectPage(kind,getJson,{limit=40,offset=0,query='',show='real',named=false}={}) {
  const q=new URLSearchParams({limit:String(limit),offset:String(offset),q:query,
    show:kind === 'people' ? (show || 'real') : 'real'});
  if (kind === 'people' && named) q.set('named', '1');
  let data;
  for(let attempt=0;attempt<180;attempt++){
    data=await getJson(`/api/media/discovery/${kind}?${q}`);
    if(!data.loading)return data;
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw new Error('Catalogue is still preparing. Try again shortly.');
}
export async function loadSubjects(kind,getJson,limit=40) {
  return (await loadSubjectPage(kind,getJson,{limit})).items || [];
}
