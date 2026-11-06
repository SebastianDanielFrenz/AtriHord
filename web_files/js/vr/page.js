/* Standalone discovery and the same immersive viewer used by desktop previews. */
import { startVr } from './app.js';
import { contextFromQuery, hasQueryContext, loadVrState, normalizeContext, sourceTitle } from './logic.js';
import { discoverySpec, subjectItems, characterSource, HOME_LINKS } from './discovery.js';
import { makeGetJson } from './sources.js';
const $ = id => document.getElementById(id);
let storage = null;
try { storage = sessionStorage; } catch (_) { /* optional */ }
const saved = storage ? loadVrState(storage) : null;
let context = hasQueryContext(location.search) ? contextFromQuery(location.search) : normalizeContext({view:'home'});
let running = false, kind = 'characters';
let pager = null;
const getJson = makeGetJson();
const enter = $('vrEnter'), preview = $('vrPreview'), grid = $('vrSubjectGrid');
function paintStart() {
  const c = context;
  $('vrStart').textContent = `Starts at: ${c.view === 'item' ? c.item.name : c.view === 'wall' ? sourceTitle(c.source) : 'Home'}`;
  for (const btn of $('vrDestinations').children) btn.setAttribute('aria-pressed', String(btn._kind === (c.source?.kind || (c.view === 'home' ? 'home' : ''))));
}
function choose(next) { context = normalizeContext(next); paintStart(); }
function destination(type, name, description, next) {
  const b = document.createElement('button'); b.type = 'button'; b.className = 'vr-destination'; b._kind = type;
  const strong = document.createElement('strong'); strong.textContent = name;
  const sub = document.createElement('span'); sub.textContent = description; b.append(strong,sub);
  b.addEventListener('click', () => { choose(next); if (['people','characters'].includes(type)) browse(type); });
  $('vrDestinations').appendChild(b);
}
destination('home','Discover','Characters, faces and your library',{view:'home'});
for (const [type,name,description] of HOME_LINKS) destination(type,name,description,{view:'wall',source:discoverySpec(type)});
if (saved?.context) destination('resume','Resume last visit','Return to where you left off',saved.context);
function renderSubjects(rows,append) {
  if(!append)grid.replaceChildren();
  for (const item of subjectItems(kind,rows)) {
    const card = document.createElement('div'); card.className='vr-subject-card';
    const b = document.createElement('button'); b.type='button'; b.className='vr-subject';
    b.setAttribute('aria-label', `Browse ${item.name}, ${item.count} files`);
    const art=document.createElement('span'); art.className='vr-subject-art';
    if (item.thumb) { const img=document.createElement('img');img.src=item.thumb;img.alt='';img.loading='lazy';art.appendChild(img); }
    const label=document.createElement('span');label.className='vr-subject-name';label.textContent=item.name;
    const count=document.createElement('span');count.className='vr-subject-count';count.textContent=`${item.count.toLocaleString()} files`;
    b.append(art,label,count);
    const pick = variant => {
      const source = kind === 'characters' && item.variants?.length
        ? characterSource(item.source, variant?.tag || '', item.source)
        : { ...item.source };
      choose({view:'wall',source});
      for (const c of grid.querySelectorAll('.vr-subject')) c.setAttribute('aria-pressed',String(c===b));
    };
    b.addEventListener('click',()=>pick(null));
    card.appendChild(b);
    if (kind === 'characters' && Array.isArray(item.variants) && item.variants.length > 1) {
      const variants=document.createElement('div'); variants.className='vr-variant-list';
      const all=document.createElement('button'); all.type='button'; all.className='vr-variant'; all.textContent='All variants';
      all.addEventListener('click',()=>pick(null)); variants.appendChild(all);
      for (const variant of item.variants) {
        if (!variant || !variant.tag) continue;
        const v=document.createElement('button'); v.type='button'; v.className='vr-variant';
        v.textContent=variant.label || variant.name || variant.tag.replace(/_/g,' ');
        v.setAttribute('aria-label',`Browse ${item.name}, ${v.textContent}`);
        v.addEventListener('click',()=>pick(variant)); variants.appendChild(v);
      }
      card.appendChild(variants);
    }
    grid.appendChild(card);
  }
}
async function browse(next) {
  kind=next;
  $('vrCharacters').setAttribute('aria-pressed',String(kind==='characters'));$('vrPeople').setAttribute('aria-pressed',String(kind==='people'));
  $('vrPeopleOptions').hidden=kind!=='people';
  if(!pager)pager=new DiscoveryGrid({kind,grid,search:$('vrSubjectSearch'),
    params:()=>kind==='people'?{show:$('vrPeopleShow').value,named:$('vrPeopleNamedOnly').checked?'1':'0'}:{show:'real'},
    onPage:renderSubjects,
    onState:(message,state)=>{$('vrSubjectNote').textContent=message||`${state.items.length.toLocaleString()} of ${state.total.toLocaleString()} ${kind==='people'?'people':'characters'}${kind==='people'&&state.hiddenTotal?` · ${state.hiddenTotal.toLocaleString()} hidden`:''} · select one, then enter VR`;}
  });
  pager.kind=kind;return pager.reset();
}
async function begin(opts) {
  if(running)return;running=true;enter.disabled=preview.disabled=true;$('vrMessage').textContent='';
  try { const h=await startVr(context,{onStatus:t=>{$('vrMessage').textContent=t||'';},...opts});const r=await h.ended;context=h.context;paintStart();if(r?.error)$('vrMessage').textContent=r.error; }
  catch(e){$('vrMessage').textContent=e.message||'Could not start VR.';}
  finally {running=false;enter.disabled=!navigator.xr;preview.disabled=false;}
}
enter.addEventListener('click',()=>begin({}));preview.addEventListener('click',()=>begin({preview:true}));
$('vrCharacters').addEventListener('click',()=>browse('characters'));$('vrPeople').addEventListener('click',()=>browse('people'));
$('vrPeopleNamedOnly').addEventListener('change',()=>{if(kind==='people'&&pager)pager.reset();});
$('vrPeopleShow').addEventListener('change',()=>{if(kind==='people'&&pager)pager.reset();});

// The launch page mirrors the desktop library's text search and common media filters.
$('vrGlobalSearch').addEventListener('submit', event => {
  event.preventDefault();
  const query=$('vrSearchQuery').value.trim();
  const filters=new URLSearchParams();
  for (const [key,id] of [['kind','vrFilterKind'],['orientation','vrFilterOrientation'],['min_res','vrFilterResolution'],
    ['min_hres','vrFilterWidth'],['min_vres','vrFilterHeight'],['min_fps','vrFilterMinFps'],['max_fps','vrFilterMaxFps']]) {
    const value=$(id).value; if(value) filters.set(key,value);
  }
  const content=[...$('vrFilterContent').querySelectorAll('input[type="checkbox"]:checked')].map(input=>input.value);
  if(content.length) filters.set('content',content.join(','));
  const favorites=$('vrFilterFavorites').checked;
  if (!query && !filters.toString() && !favorites) {
    $('vrMessage').textContent='Add a search term or choose a filter.';
    return;
  }
  const mode=$('vrSearchMode').value || 'filename';
  const source={kind:favorites?'favorites':'search',root:'',path:'',query:query || (favorites?'':'*'),filters:filters.toString(),
    sort:{key:'',dir:''},offset:0,mode};
  choose({view:'wall',source});
  $('vrMessage').textContent='';
});

function fillSelect(id, values) {
  const select=$(id), first=select.options[0]; select.replaceChildren(first);
  for(const value of values) {
    const option=document.createElement('option'); option.value=String(value.key ?? value.fps);
    option.textContent=value.label || `${value.fps} fps`; select.appendChild(option);
  }
}
function fillContent(values) {
  const box=$('vrFilterContent'), legend=box.querySelector('legend'); box.replaceChildren(legend);
  for(const value of values || []) {
    const label=document.createElement('label'); label.className='vr-filter-content-option';
    const input=document.createElement('input'); input.type='checkbox'; input.value=value.key;
    const text=document.createElement('span'); text.textContent=value.label;
    label.append(input,text); box.appendChild(label);
  }
}
const filterRequest=getJson('/api/media/filters').then(options=>{
  const standards=options.standards || [];
  for(const id of ['vrFilterResolution','vrFilterWidth','vrFilterHeight']) {
    const select=$(id), first=select.options[0]; select.replaceChildren(first);
    for(const value of standards) { const opt=document.createElement('option');opt.value=value.key;opt.textContent=value.label;select.appendChild(opt); }
  }
  const rates=options.frame_rates || {};
  fillSelect('vrFilterMinFps',rates.min || []); fillSelect('vrFilterMaxFps',rates.max || []);
  fillContent(options.contents || []);
}).catch(()=>{});

const initialSource=context.source;
if(initialSource) {
  $('vrSearchQuery').value=initialSource.query==='*'?'':initialSource.query||'';
  $('vrSearchMode').value=initialSource.mode||'filename';
  const activeFilters=new URLSearchParams(initialSource.filters||'');
  for(const [key,id] of [['kind','vrFilterKind'],['orientation','vrFilterOrientation'],['min_res','vrFilterResolution'],
    ['min_hres','vrFilterWidth'],['min_vres','vrFilterHeight'],['min_fps','vrFilterMinFps'],['max_fps','vrFilterMaxFps']]) {
    $(id).value=activeFilters.get(key)||'';
  }
  $('vrFilterFavorites').checked=initialSource.kind==='favorites';
  filterRequest.then(()=>{
    for(const [key,id] of [['kind','vrFilterKind'],['orientation','vrFilterOrientation'],['min_res','vrFilterResolution'],
      ['min_hres','vrFilterWidth'],['min_vres','vrFilterHeight'],['min_fps','vrFilterMinFps'],['max_fps','vrFilterMaxFps']]) {
      $(id).value=activeFilters.get(key)||'';
    }
    for(const value of (activeFilters.get('content')||'').split(',').filter(Boolean)) {
      const checkbox=$('vrFilterContent').querySelector(`input[value="${CSS.escape(value)}"]`);
      if(checkbox) checkbox.checked=true;
    }
  });
}

$('vrSubjectMore').hidden=true;
enter.disabled=!navigator.xr;
$('vrStatus').textContent=navigator.xr?'Connect your headset, choose a starting point, and enter VR.':'WebXR is unavailable in this browser. You can still explore with Desktop preview.';
$('vrHint').textContent='';paintStart();browse('characters');
