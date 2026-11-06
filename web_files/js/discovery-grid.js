/* Search the whole server catalogue; fetch only enough tiles to fill the visible box. */
class DiscoveryGrid {
  constructor({kind, grid, search, params, onPage, onState}) {
    Object.assign(this,{kind,grid,search,params,onPage,onState});
    this.items=[];this.offset=0;this.total=0;this.more=true;this.busy=false;this.started=false;this.version=0;
    this.button=document.createElement('button');this.button.type='button';this.button.className='btn btn-quiet discovery-more';
    this.button.textContent='Load more';grid.after(this.button);this.button.hidden=true;
    this.button.addEventListener('click',()=>{clearTimeout(this.retry);this.retry=null;this.load(this.columns()*2);});
    let frame=0;
    this.schedule=()=>{if(!frame)frame=requestAnimationFrame(()=>{frame=0;this.fill();});};
    window.addEventListener('scroll',this.schedule,{passive:true});window.addEventListener('resize',this.schedule);
    this.observer=new ResizeObserver(()=>this.schedule());this.observer.observe(grid);
    if(search){let timer;search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>this.reset(),180);});}
  }
  visible(){return this.grid.getClientRects().length>0;}
  columns(){return Math.max(1,getComputedStyle(this.grid).gridTemplateColumns.split(/\s+/).filter(Boolean).length);}
  target(){
    const columns=this.columns(),tile=this.grid.querySelector('.character-tile,.person-tile,.vr-subject');
    const gap=parseFloat(getComputedStyle(this.grid).rowGap)||12;
    const height=tile?.getBoundingClientRect().height || this.grid.clientWidth/columns+65;
    const parent=this.grid.closest('[data-discovery-scroll]');
    const bottom=parent ? Math.min(innerHeight,parent.getBoundingClientRect().bottom) : innerHeight;
    const space=Math.max(height,bottom-this.grid.getBoundingClientRect().top);
    return columns*(Math.ceil(space/(height+gap))+1);
  }
  signature(){return JSON.stringify([this.search?.value.trim()||'',this.params?.()||{}]);}
  open(reload=false){if(reload||!this.started||this.signature()!==this.querySignature||Date.now()-(this.at||0)>60000)return this.reset();this.schedule();return Promise.resolve();}
  reset(){
    this.version++;this.abort?.abort();clearTimeout(this.retry);this.retry=null;
    this.started=true;this.busy=false;this.more=true;this.items=[];this.offset=0;this.total=0;
    this.querySignature=this.signature();
    this.onPage([],false,this);this.onState('Loading…',this);return this.load(this.target());
  }
  fill(){if(this.started&&this.visible()&&this.more&&!this.busy&&!this.retry&&this.items.length<this.target())this.load(this.target()-this.items.length);}
  async load(wanted){
    if(this.busy||!this.more||!this.visible())return;
    const mine=this.version;this.busy=true;this.abort=new AbortController();this.grid.setAttribute('aria-busy','true');
    this.button.disabled=true;
    try{
      const q=new URLSearchParams({...this.params?.(),q:this.search?.value.trim()||'',offset:String(this.offset),limit:String(Math.min(200,Math.max(this.columns(),Math.ceil(wanted))))});
      const res=await fetch(`/api/media/discovery/${this.kind}?${q}`,{signal:this.abort.signal});
      if(!res.ok)throw new Error(res.status===401?'Sign in again to browse.':'Could not load subjects. Use Load more to retry.');
      const data=await res.json();if(mine!==this.version)return;
      if(data.loading){
        this.onState('Preparing the catalogue…',this);
        this.retry=setTimeout(()=>{this.retry=null;this.fill();},1000);return;
      }
      const rows=data.items||[],append=this.offset>0;
      this.items.push(...rows);this.offset=data.next_offset??this.offset+rows.length;this.total=data.total??this.items.length;
      this.at=Date.now();
      this.more=!!data.has_more;this.hiddenTotal=data.hidden_total||0;
      this.onPage(rows,append,this);this.onState('',this);
    }catch(e){if(mine===this.version&&e.name!=='AbortError'){this.onState(e.message,this);this.retry=-1;}}
    finally{if(mine===this.version){this.busy=false;this.grid.setAttribute('aria-busy','false');this.button.disabled=false;this.button.hidden=!this.more;this.schedule();}}
  }
}
window.DiscoveryGrid=DiscoveryGrid;
