"""Small persistent discovery snapshots; one refresh per key, outside HTTP requests."""
import json, os, threading, time, unicodedata

def search_text(value):
    return unicodedata.normalize('NFKD', str(value or '')).casefold().replace('_', ' ')

class Catalogue:
    def __init__(self, directory, ttl=600):
        self.directory=directory; self.ttl=ttl; self.lock=threading.RLock()
        self.entries={}; self.running=set(); self.errors={}; self.epoch=0

    def get(self, key, build):
        with self.lock:
            if key not in self.entries:
                try:
                    with open(os.path.join(self.directory,key+'.json'),encoding='utf-8') as f:entry=json.load(f)
                    if isinstance(entry,dict) and entry.get('version')==2 and isinstance(entry.get('at'),(int,float)) and entry.get('data') is not None:self.entries[key]=entry
                except (OSError,ValueError,TypeError):pass
            entry=self.entries.get(key)
            if (not entry or time.time()-entry['at']>self.ttl) and key not in self.running:
                self.running.add(key);epoch=self.epoch
                def refresh():
                    try:
                        value=build();result={'version':2,'at':time.time(),'data':value}
                        with self.lock:
                            if epoch!=self.epoch:return
                            try:
                                os.makedirs(self.directory,exist_ok=True)
                                target=os.path.join(self.directory,key+'.json');temp=target+'.tmp'
                                with open(temp,'w',encoding='utf-8') as f:json.dump(result,f,separators=(',',':'))
                                os.replace(temp,target)
                            except OSError as e:print('[Warning] discovery snapshot: '+str(e),flush=True)
                            self.entries[key]=result;self.errors.pop(key,None)
                    except Exception as e:
                        with self.lock:self.errors[key]=str(e)
                        print('[Warning] discovery refresh '+key+': '+str(e),flush=True)
                    finally:
                        with self.lock:self.running.discard(key)
                threading.Thread(target=refresh,daemon=True,name='discovery-'+key).start()
            return entry['data'] if entry else None

    def invalidate(self):
        with self.lock:
            self.epoch+=1
            for key,entry in self.entries.items():entry['at']=0
            for key in ('characters','people'):
                try:os.unlink(os.path.join(self.directory,key+'.json'))
                except FileNotFoundError:pass

    def status(self,key):
        with self.lock:return {'refreshing':key in self.running,'retry':key in self.errors}

def character_matches(row, query):
    text=' '.join(str(row.get(k) or '') for k in ('tag','label','series'))
    text+=' '+' '.join(str(v.get('tag','')) for v in row.get('variants',[]))
    return search_text(query) in search_text(text)

def people_matches(row, guess, query):
    text=' '.join([str(row.get('name') or ''),str(guess.get('name') or ''), 'Person '+str(row['id'])])
    text+=' '+' '.join(str(v) for v in guess.get('handles',[]))
    return search_text(query) in search_text(text)

def page_info(rows, offset, limit):
    return {'total':len(rows),'offset':offset,'next_offset':offset+min(limit,max(0,len(rows)-offset)),
            'has_more':offset+limit<len(rows)}
