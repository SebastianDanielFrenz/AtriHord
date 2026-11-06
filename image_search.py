"""Private, multimodal image search. Uploads stay in memory; only tiny visual descriptors are cached.
Visual descriptors combine a difference hash, spatial colour layout and colour histograms.
Text/character/face signals reuse media-dl's existing caption, tag and ArcFace index.
"""
from __future__ import annotations
import base64, heapq, io, json, math, os, re, secrets, sqlite3, threading, time, warnings
from collections import Counter
import numpy as np
from PIL import Image, ImageOps

PHOTO_TAGS = ("realistic", "photo_background", "lips", "depth_of_field")  # same photographic gate as media_faces
MAX_UPLOAD = 8 * 1024 * 1024
FEATURE_SIZE = 248
IMAGE_EXTS = {'.jpg','.jpeg','.jfif','.png','.webp','.bmp','.gif','.tif','.tiff'}
GENERIC_TAGS = {'1girl','1boy','solo','looking_at_viewer','long_hair','short_hair','smile','breasts','highres','absurdres'}
STOP = set('a an the and or of to in on at with for from by is are was this that it its as has have wearing standing sitting image picture photo photograph illustration showing girl woman man person young background beautiful looking camera one two very'.split())

def words(text):
    return {t for t in re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}",str(text).lower()) if t not in STOP}

def tag_map(raw, category):
    rows = (raw or {}).get(category, [])
    return {str(e['tag']):float(e.get('p') or 0) for e in rows if isinstance(e,dict) and e.get('tag')}

def overlap(a,b):
    if not a or not b:return 0.0
    return len(a & b) / math.sqrt(len(a)*len(b))

def prepare_image(blob):
    if not blob or len(blob)>MAX_UPLOAD:raise ValueError('Choose an image up to 8 MB.')
    with warnings.catch_warnings():
        warnings.simplefilter('error',Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(blob)) as source:
            if source.width*source.height > 40_000_000:raise ValueError('Choose an image smaller than 40 megapixels.')
            image=ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((2048,2048),Image.Resampling.LANCZOS)
            output=io.BytesIO();image.save(output,format='JPEG',quality=92)
            return output.getvalue(),descriptor(image)

def descriptor(image):
    image=image.convert('RGB')
    gray=np.asarray(image.resize((9,8),Image.Resampling.LANCZOS).convert('L'),dtype=np.uint8)
    bits=np.packbits((gray[:,1:]>gray[:,:-1]).reshape(-1)).tobytes()
    spatial=np.asarray(image.resize((8,8),Image.Resampling.LANCZOS),dtype=np.uint8).tobytes()
    small=np.asarray(image.resize((64,64),Image.Resampling.BILINEAR),dtype=np.uint8)
    hist=[]
    for ch in range(3):
        h=np.bincount((small[:,:,ch]//16).reshape(-1),minlength=16).astype(float)
        hist.extend(np.rint(h/h.sum()*255).clip(0,255).astype(np.uint8))
    return bits+spatial+bytes(hist)

def visual_scores(query, blobs):
    q=np.frombuffer(query,dtype=np.uint8)
    m=np.frombuffer(b''.join(blobs),dtype=np.uint8).reshape(-1,FEATURE_SIZE)
    dh=1-np.unpackbits(m[:,:8]^q[:8],axis=1).mean(axis=1)
    spatial=(1-np.sqrt(np.mean((m[:,8:200].astype(np.float32)-q[8:200])**2,axis=1))/120).clip(0,1)
    colour=np.minimum(m[:,200:].astype(np.float32),q[200:]).sum(axis=1)/765
    return (.5*dh+.3*spatial+.2*colour).clip(0,1)

class ImageSearch:
    def __init__(self, cache_dir, ai_connect, paths, resolve, analyze):
        self.cache_dir=cache_dir;self.ai_connect=ai_connect;self.paths=paths;self.resolve=resolve;self.analyze=analyze
        self.lock=threading.RLock();self.jobs={};self.slots=threading.BoundedSemaphore(2)
        self.indexing=False;self.index_total={};self.index_error=None;self.index_source=None
        self.local=threading.local();self.cache_initialized=False;self.pending_owners=set();self.hits={}
    def cache(self):
        if not getattr(self.local,'cache',None):
            with self.lock:
                os.makedirs(self.cache_dir,exist_ok=True)
                c=sqlite3.connect(os.path.join(self.cache_dir,'image_search.sqlite3'),timeout=20)
                c.execute('PRAGMA busy_timeout=20000')
                if not self.cache_initialized:
                    c.execute('PRAGMA journal_mode=WAL')
                    c.execute('CREATE TABLE IF NOT EXISTS features(root TEXT,path TEXT,size INTEGER,mtime_ns INTEGER,feature BLOB,PRIMARY KEY(root,path)) WITHOUT ROWID')
                    c.commit();self.cache_initialized=True
                c.execute('PRAGMA synchronous=NORMAL')
                self.local.cache=c
        return self.local.cache
    def feature(self,root,path):
        full=self.resolve(root,path)
        if not full:return None
        try:stat=os.stat(full)
        except OSError:return None
        if not stat.st_size:return None
        c=self.cache();row=c.execute('SELECT size,mtime_ns,feature FROM features WHERE root=? AND path=?',(root,path)).fetchone()
        if row and row[0]==stat.st_size and row[1]==stat.st_mtime_ns:return row[2]
        feature=None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',Image.DecompressionBombWarning)
                with Image.open(full) as image:
                    if image.width*image.height>40_000_000:raise ValueError('Image too large')
                    image.draft('RGB',(256,256));feature=descriptor(ImageOps.exif_transpose(image))
        except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning):pass
        c.execute('INSERT OR REPLACE INTO features VALUES(?,?,?,?,?)',(root,path,stat.st_size,stat.st_mtime_ns,feature));c.commit()
        return feature
    def start_index(self):
        snapshot=self.paths()
        with self.lock:
            if self.indexing or self.index_source is snapshot:return
            self.indexing=True;self.index_error=None;self.index_source=snapshot
        def run():
            try:
                rows=[(row[0],row[1]) for row in snapshot if not row[2] and os.path.splitext(row[1])[1].lower() in IMAGE_EXTS]
                with self.lock:self.index_total=dict(Counter(r for r,p in rows))
                for root,path in rows:
                    self.feature(root,path);time.sleep(.002)
            except Exception as e:
                with self.lock:self.index_error=str(e)[:160]
            finally:
                with self.lock:self.indexing=False
        threading.Thread(target=run,daemon=True,name='image-search-index').start()
    def coverage(self,allowed):
        marks=','.join('?' for _ in allowed)
        n=self.cache().execute(f'SELECT COUNT(*) FROM features WHERE feature IS NOT NULL AND root IN ({marks})',tuple(allowed)).fetchone()[0] if allowed else 0
        with self.lock:return {'indexed':n,'total':sum(self.index_total.get(r,0) for r in allowed),'building':self.indexing}
    def nearest(self,query,allowed,limit=400):
        if not allowed:return []
        marks=','.join('?' for _ in allowed);heap=[]
        cur=self.cache().execute(f'SELECT root,path,feature FROM features WHERE feature IS NOT NULL AND root IN ({marks})',tuple(allowed))
        while True:
            rows=cur.fetchmany(2048)
            if not rows:break
            rows=[r for r in rows if len(r[2])==FEATURE_SIZE]
            if not rows:continue
            for row,score in zip(rows,visual_scores(query,[r[2] for r in rows])):
                if score<.76:continue
                item=(float(score),row[0],row[1])
                if len(heap)<limit:heapq.heappush(heap,item)
                elif item>heap[0]:heapq.heapreplace(heap,item)
        return sorted(heap,reverse=True)
    def start(self,owner,allowed,blob,source=None):
        now=time.time()
        with self.lock:
            self.jobs={k:j for k,j in self.jobs.items() if now-j['created']<900}
            self.hits={u:[t for t in hit if now-t<60] for u,hit in self.hits.items() if any(now-t<60 for t in hit)}
            if len(self.hits.get(owner,[]))>=10:raise RuntimeError('Please wait before starting another image search.')
            if owner in self.pending_owners or any(j['owner']==owner and j['state']=='working' for j in self.jobs.values()):raise RuntimeError('An image search is already running. Please wait.')
            if len(self.jobs)>=40 or not self.slots.acquire(False):raise RuntimeError('Image search is busy. Try again shortly.')
            self.pending_owners.add(owner);self.hits.setdefault(owner,[]).append(now)
        try:normalized,feature=prepare_image(blob)
        except Exception:
            with self.lock:self.pending_owners.discard(owner)
            self.slots.release();raise
        with self.lock:
            key=secrets.token_urlsafe(18)
            job={'owner':owner,'allowed':set(allowed),'source':source,'created':now,'state':'working','phase':'Reading the reference image','feature':feature,'scores':{},'warnings':[],'refresh':0}
            self.jobs[key]=job;self.pending_owners.discard(owner)
        try:
            self.start_index()
            threading.Thread(target=self.run,args=(job,normalized),daemon=True,name='image-search-query').start()
        except Exception:
            with self.lock:self.jobs.pop(key,None)
            self.slots.release();raise RuntimeError('The library index is temporarily unavailable. Try again shortly.')
        return key
    def reference(self,job,blob):
        c=self.ai_connect();source=job['source'];row=None
        if source:row=c.execute('SELECT id,caption,tags FROM media WHERE root=? AND path=? AND trashed IS NULL',source).fetchone()
        if row and (row['caption'] or row['tags']):
            try:tags=json.loads(row['tags'] or '{}')
            except ValueError:tags={}
            faces=[{'embedding_b64':base64.b64encode(f['embedding']).decode(),'cluster_id':f['cluster_id']} for f in c.execute('SELECT embedding,cluster_id FROM faces WHERE media_id=? AND embedding IS NOT NULL',(row['id'],))]
            return {'caption':row['caption'] or '', 'tags':tags,'faces':faces}
        try:return self.analyze(blob)
        except Exception as e:
            job['warnings'].append('AI analysis is temporarily unavailable; visual matching still works.')
            return {'caption':'','tags':{},'faces':[]}
    def semantic(self,job,reference):
        c=self.ai_connect();allowed=sorted(job['allowed']);marks=','.join('?' for _ in allowed)
        if not allowed:return {}
        candidates={};scores={};tags=reference.get('tags') or {}
        chars=tag_map(tags,'character');chars={k:v for k,v in chars.items() if v>=.1}
        try:variants=dict(c.execute("SELECT tag,group_key FROM tag_variants WHERE category='character'"))
        except sqlite3.OperationalError:variants={}
        groups={variants.get(tag,tag) for tag in chars}
        for tag,group in variants.items():
            if group in groups:chars.setdefault(tag,max(chars.values(),default=1))
        general=tag_map(tags,'general');general={k:v for k,v in general.items() if v>=.25 and k not in GENERIC_TAGS}
        tokens=words(reference.get('caption',''))
        def put(row,kind,value):
            pair=(row['root'],row['path']);scores.setdefault(pair,{})[kind]=max(scores.get(pair,{}).get(kind,0),float(value))
        # Indexed tag lookups avoid scanning the 20-million-row tag table.
        for category,values in [('character',chars),('general',general)]:
            for tag in sorted(values,key=values.get,reverse=True)[:(32 if category=='character' else 16)]:
                # Bound common-tag reads before sorting: a general tag can have
                # hundreds of thousands of hits and has no confidence index.
                order=" ORDER BY p DESC" if category=='character' else ''
                sql=f"SELECT m.id,m.root,m.path,m.caption,m.tags FROM (SELECT media_id,p FROM media_tags WHERE category=? AND tag=?{order} LIMIT 2000) t JOIN media m ON m.id=t.media_id WHERE m.kind='image' AND m.trashed IS NULL AND m.root IN ({marks}) ORDER BY t.p DESC LIMIT 400"
                for row in c.execute(sql,(category,tag,*allowed)):candidates[row['id']]=row
        if tokens:
            terms=sorted(tokens,key=lambda t:(-len(t),t))[:16]
            expr='caption : ('+' OR '.join('"'+t.replace('"','')+'"' for t in terms)+')'
            try:
                sql=f"SELECT m.id,m.root,m.path,m.caption,m.tags FROM media_fts f JOIN media m ON m.id=f.rowid WHERE media_fts MATCH ? AND m.kind='image' AND m.trashed IS NULL AND m.root IN ({marks}) LIMIT 800"
                for row in c.execute(sql,(expr,*allowed)):candidates[row['id']]=row
            except sqlite3.OperationalError:pass
        for row in candidates.values():
            try:other=json.loads(row['tags'] or '{}')
            except ValueError:other={}
            character=overlap(groups,{variants.get(tag,tag) for tag in tag_map(other,'character')})
            other_tokens=words(row['caption'] or '');other_general=set(tag_map(other,'general'))-GENERIC_TAGS
            text_similarity=overlap(tokens,other_tokens);tag_similarity=overlap(set(general),other_general)
            description=.7*text_similarity+.3*tag_similarity
            if not text_similarity and (len(set(general)&other_general)<3 or tag_similarity<.35):description=0
            if character>.15:put(row,'characters',character)
            if description>.14:put(row,'description',description)
        reference_faces=[];clusters=set()
        raw_general=tag_map(tags,'general')
        photographic=not tags or any(raw_general.get(tag,0)>=.5 for tag in PHOTO_TAGS)
        for face in ((reference.get('faces') or [])[:5] if photographic else []):
            try:
                v=np.frombuffer(base64.b64decode(face['embedding_b64'],validate=True),dtype='<f4')
                if len(v)!=512 or not np.isfinite(v).all() or np.linalg.norm(v)<1e-8:continue
                reference_faces.append(v/np.linalg.norm(v))
                if face.get('cluster_id') is not None:clusters.add(face['cluster_id'])
            except (ValueError,KeyError,TypeError):continue
        if reference_faces:
            query=np.stack(reference_faces)
            sql=f"SELECT m.root,m.path,f.embedding,f.cluster_id FROM faces f JOIN media m ON m.id=f.media_id WHERE f.embedding IS NOT NULL AND m.kind='image' AND m.trashed IS NULL AND m.root IN ({marks}) AND (m.tags IS NULL OR m.tags='' OR EXISTS(SELECT 1 FROM media_tags pt WHERE pt.media_id=m.id AND pt.tag IN ('realistic','photo_background','lips','depth_of_field') AND pt.p>=.5))"
            for row in c.execute(sql,allowed):
                v=np.frombuffer(row['embedding'],dtype='<f4')
                if len(v)!=512 or not np.isfinite(v).all():continue
                norm=float(np.linalg.norm(v))
                if norm<1e-8:continue
                sim=float((query@(v/norm)).max())
                if row['cluster_id'] in clusters:sim=max(sim,.99)
                if sim>=.55:put(row,'faces',sim)
        return scores
    def refresh_visual(self,job):
        found=self.nearest(job['feature'],job['allowed'])
        with self.lock:
            for score,root,path in found:job['scores'].setdefault((root,path),{})['visual']=score
            job['refresh']=time.time()
    def run(self,job,blob):
        try:
            reference=self.reference(job,blob)
            with self.lock:job['phase']='Matching descriptions, characters and faces'
            scores=self.semantic(job,reference)
            # Seed optical matches from semantic candidates before the full background sweep finishes.
            with self.lock:job['phase']='Comparing visual appearance'
            for root,path in list(scores)[:96]:self.feature(root,path)
            with self.lock:job['scores']=scores;job['caption']=str(reference.get('caption') or '')[:2000];job['characters']=sorted(tag_map(reference.get('tags') or {},'character'),key=lambda t:-tag_map(reference.get('tags') or {},'character')[t])[:8]
            self.refresh_visual(job)
            with self.lock:job['state']='ready';job['phase']='Ready'
        except Exception as e:
            with self.lock:job['state']='error';job['error']='Image search failed: '+str(e)[:200]
        finally:self.slots.release()
    def get(self,key,owner,current_allowed=None):
        with self.lock:
            job=self.jobs.get(key)
            if not job or job['owner']!=owner or time.time()-job['created']>900:return None
            ready=job['state']=='ready';refresh=ready and ((self.indexing and time.time()-job['refresh']>15) or (not self.indexing and not job.get('final_visual')))
        if refresh:
            self.refresh_visual(job)
            if not self.indexing:job['final_visual']=True
        with self.lock:
            scores={pair:dict(values) for pair,values in job['scores'].items() if pair!=job['source']}
            return {'state':job['state'],'phase':job['phase'],'error':job.get('error'),'source':job['source'],
                'caption':job.get('caption',''),'characters':list(job.get('characters',[])),
                'warnings':list(job['warnings']),'scores':scores,'coverage':self.coverage(job['allowed'] if current_allowed is None else job['allowed'] & set(current_allowed))}
