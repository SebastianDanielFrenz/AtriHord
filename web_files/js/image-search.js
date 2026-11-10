/* Uploads are private reference queries; matches use the ordinary media preview. */
(() => {
  const $ = id => document.getElementById(id);
  const dialog=$('imageSearchDialog'), grid=$('imageSearchGrid');
  const labels={visual:'Visual',description:'Description',characters:'Character',faces:'Face'};
  let job=null, category='all', items=[], more=false, timer=null, generation=0, objectUrl=null, limit=40;
  const status=text=>{$('imageSearchStatus').textContent=text;};
  const stop=()=>{clearTimeout(timer);timer=null;};
  async function json(response) { const body=await response.json(); if(!response.ok)throw new Error(body.error||'Image search could not be loaded.');return body; }
  function render(data) {
    items=data.items||[];more=!!data.has_more;grid.replaceChildren();
    for(const file of items) {
      const tile=createFileTile(file,items), reasons=document.createElement('div');reasons.className='image-match-reasons';
      for(const kind of file.match_types||[]) {const badge=document.createElement('span');badge.textContent=labels[kind]||kind;reasons.appendChild(badge);}
      tile.appendChild(reasons);grid.appendChild(tile);
    }
    if(typeof scheduleGalleryLayout==='function')scheduleGalleryLayout(grid);
    $('imageSearchMore').hidden=!more;
    $('imageSearchCaption').textContent=data.caption||'';
    $('imageSearchWarning').textContent=(data.warnings||[]).join(' ');
    const coverage=data.coverage||{};
    const progress=coverage.building?` · Visual index building: ${(coverage.indexed||0).toLocaleString()} pictures ready`:'';
    status(`${(data.total||0).toLocaleString()} matches${progress}${!data.total&&coverage.building?' — new visual matches will appear as indexing progresses.':''}`);
    for(const button of $('imageSearchTypes').querySelectorAll('button')) {
      const kind=button.dataset.match;button.textContent=kind==='all'?'All':`${labels[kind]==='Description'?'Descriptions':labels[kind]==='Character'?'Characters':labels[kind]==='Face'?'Faces':'Visual'} (${data.counts?.[kind]||0})`;
    }
  }
  async function poll(seq) {
    if(!job||seq!==generation||!dialog.open)return;
    const id=job, kind=category;
    try {
      let data, found=[];
      while(found.length<limit) {
        data=await json(await fetch(`/api/media/image-search/${encodeURIComponent(id)}?${new URLSearchParams({match:kind,offset:String(found.length),limit:String(Math.min(100,limit-found.length))})}`));
        if(data.state!=='ready')break;
        found.push(...(data.items||[]));
        if(!data.has_more||!data.items?.length)break;
      }
      if(data?.state==='ready')data.items=found;
      if(seq!==generation||id!==job||kind!==category||!dialog.open)return;
      if(data.state==='error')throw new Error(data.error||'Image search failed.');
      if(data.state==='working'){status(data.phase||'Searching…');timer=setTimeout(()=>poll(seq),1500);return;}
      render(data);
      if(data.coverage?.building)timer=setTimeout(()=>poll(seq),15000);
    } catch(error){if(seq===generation)status(error.message);}
  }
  function show() { if(!dialog.open)dialog.showModal(); }
  async function search(body,headers) {
    const seq=++generation;stop();job=null;items=[];limit=40;grid.replaceChildren();$('imageSearchMore').hidden=true;
    $('imageSearchWarning').textContent='';$('imageSearchCaption').textContent='';status('Reading the reference image…');
    try {
      const data=await json(await fetch('/api/media/image-search',{method:'POST',body,headers}));
      if(seq!==generation)return;job=data.id;poll(seq);
    } catch(error){if(seq===generation)status(error.message);}
  }
  function reference(url) {const image=$('imageSearchReference');image.src=url;image.hidden=false;}
  function open(file) {
    show();
    if(file) {reference(file.url);search(JSON.stringify({root:file.root||'',path:file.path}),{'Content-Type':'application/json'});}
    else if(job)poll(generation);
  }
  $('imageSearchBtn').addEventListener('click',()=>open());
  $('imageSearchClose').addEventListener('click',()=>dialog.close());dialog.addEventListener('close',stop);
  function useFile(file) {
    if(!file)return;
    if(file.type&&!file.type.startsWith('image/')){status('That is not a picture.');return;}
    if(file.size>8*1024*1024){status('Choose an image up to 8 MB.');return;}
    if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=URL.createObjectURL(file);reference(objectUrl);
    const body=new FormData();body.append('image',file,file.name||'pasted-image');search(body);
  }
  const firstImage=list=>Array.from(list||[]).find(f=>f&&f.type&&f.type.startsWith('image/'));
  $('imageSearchFile').addEventListener('change',event=>{
    const file=event.target.files?.[0];if(!file)return;event.target.value='';useFile(file);
  });
  // Paste (Ctrl+V) a copied picture while the dialog is open (user, 2026-11-09).
  document.addEventListener('paste',event=>{
    if(!dialog.open)return;
    const items=Array.from(event.clipboardData?.items||[]).filter(i=>i.kind==='file'&&i.type.startsWith('image/'));
    const file=items.length?items[0].getAsFile():firstImage(event.clipboardData?.files);
    if(!file){status('The clipboard holds no picture. Copy an image, then paste.');return;}
    event.preventDefault();useFile(file);
  });
  // Drop a picture file anywhere on the dialog.
  const hasFiles=event=>Array.from(event.dataTransfer?.types||[]).includes('Files');
  dialog.addEventListener('dragover',event=>{if(!hasFiles(event))return;event.preventDefault();event.dataTransfer.dropEffect='copy';dialog.classList.add('is-dropping');});
  dialog.addEventListener('dragleave',event=>{if(event.target===dialog||!dialog.contains(event.relatedTarget))dialog.classList.remove('is-dropping');});
  dialog.addEventListener('drop',event=>{
    dialog.classList.remove('is-dropping');if(!hasFiles(event))return;event.preventDefault();
    const file=firstImage(event.dataTransfer.files);
    if(!file){status('Drop a picture file.');return;}
    useFile(file);
  });
  $('imageSearchTypes').addEventListener('click',event=>{
    const button=event.target.closest('[data-match]');if(!button)return;category=button.dataset.match;limit=40;stop();
    for(const other of $('imageSearchTypes').children)other.setAttribute('aria-pressed',String(other===button));
    if(job)poll(generation);
  });
  $('imageSearchMore').addEventListener('click',()=>{limit+=40;stop();poll(generation);});
  // Leave the top-layer dialog before the ordinary media preview opens.
  grid.addEventListener('click',event=>{if(event.target.closest('.media-file')&&!event.target.closest('button'))dialog.close();},true);
  grid.addEventListener('contextmenu',event=>{if(event.target.closest('.media-file'))dialog.close();},true);
  window.ImageSearch={open};
})();
