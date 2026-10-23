"""Journal-level metric hydration and bounded, shared background lookups."""
import json
import threading
import time
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .db import get_db, get_bool_setting
from . import metadata_jobs as completion, metadata_sources as sources, venues
from .store import norm_venue
from .linkview import number,zone_number

router=APIRouter(prefix='/api/research/metrics')
_lock=threading.Lock()
_active={}
COOLDOWN=6*60*60

def init():
    with get_db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS metric_jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS metric_lookups(name_norm TEXT PRIMARY KEY,checked_at REAL NOT NULL,status TEXT NOT NULL)')
        for row in c.execute('SELECT id,payload FROM metric_jobs').fetchall():
            j=json.loads(row['payload'])
            if j['status'] in ('queued','running'):
                j.update(status='interrupted',message='上次指标查询已中断，已保存结果保留')
                c.execute('UPDATE metric_jobs SET payload=? WHERE id=?',(json.dumps(j,ensure_ascii=False),j['id']))

def groups(ids=None):
    wanted=set(ids) if ids is not None else None
    result={}
    with get_db() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'papers' not in tables:return []
        exclude=" AND id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library')" if 'paper_trash' in tables else ''
        rows=c.execute('SELECT id,venue FROM papers WHERE venue IS NOT NULL'+exclude).fetchall()
        known={r['name_norm']:dict(r) for r in c.execute('SELECT * FROM venues')}
    for row in rows:
        if wanted is not None and row['id'] not in wanted:continue
        name=row['venue'].strip()
        if name:
            key=norm_venue(name);v=known.get(key,{})
            missing=[f for f in ('impact_factor','cas_zone','jcr_zone') if (number(v.get(f)) if f=='impact_factor' else zone_number(v.get(f))) is None]
            result.setdefault(key,{'name':name,'paper_id':row['id'],'papers':0,'missing':missing})['papers']+=1
    return list(result.values())

def cached_value(name):
    rec=venues.local_rank(name)
    if not rec or not isinstance(rec.get('rank'),dict):return None
    # This path must never turn a partial/empty cache into a network request.
    if not any(k in venues.rank_to_fields(rec['rank']) for k in ('impact_factor','cas_zone','jcr_zone')):return None
    return sources.authorized_metrics(name,allow_network=False)

def hydrate_cached(ids=None):
    updated=0;pending=[]
    for group in groups(ids):
        if not group['missing']:continue
        value=cached_value(group['name'])
        if value:pending.append((group['paper_id'],value))
    if pending:
        with get_db() as c:
            for pid,value in pending:updated+=len(completion.apply_metrics(pid,value,connection=c))
    return updated

def save(job):
    with get_db() as c:c.execute('INSERT OR REPLACE INTO metric_jobs VALUES(?,?)',(job['id'],json.dumps(job,ensure_ascii=False)))

class Start(BaseModel):
    ids:list[int]|None=Field(default=None,max_length=50000)
    allow_network:bool=True
    retry:bool=False

def worker(job,selected,cancel):
    memo={};values={}
    try:
        job.update(status='running',message='按期刊集中读取指标');save(job)
        for group in selected:
            if cancel.is_set():break
            name=group['name'];key=venues.norm_name(name)
            missing=completion.missing_metrics({'venue':name})
            if not missing:
                job['completed']+=1;save(job);continue
            results=[];error=None
            try:
                if key not in values:
                    value=sources.authorized_metrics(name,allow_network=True,refresh=True)
                    if value:results.append(value)
                    if 'impact_factor' in missing and not any(v.get('impact_factor') for v in results):
                        value=sources.nature_metrics(name,memo)
                        if value:results.append(value)
                    values[key]=results
                results=values[key]
                fields=[]
                for value in results:fields+=completion.apply_metrics(group['paper_id'],value)
                job['fields_filled']+=len(fields)
            except Exception:
                error='source_unavailable'
                job['errors']+=1
            remaining=completion.missing_metrics({'venue':name})
            with get_db() as c:c.execute('INSERT OR REPLACE INTO metric_lookups VALUES(?,?,?)',(key,time.time(),error or ('partial' if remaining else 'complete')))
            job['results'].append({'venue':name,'papers':group['papers'],'still_missing':remaining,'error':error})
            job['completed']+=1;job['message']='已查询 '+str(job['completed'])+' / '+str(job['total'])+' 个期刊';save(job)
        job.update(status='cancelled' if cancel.is_set() else 'completed',message='已停止；已取得指标保留' if cancel.is_set() else '指标读取完成；未覆盖项保留缺失标记');save(job)
    except Exception:
        job.update(status='failed',message='指标查询中断，已保存结果保留');save(job)
    finally:
        if memo.get('_nature_session'):memo['_nature_session'].close()
        with _lock:_active.pop(job['id'],None)

@router.post('')
def start(body:Start):
    with _lock:
        if _active:
            jid=next(iter(_active))
            return read(jid)
        fields=hydrate_cached(body.ids)
        selected=groups(body.ids)
        selected=[g for g in selected if g['missing']]
        online=body.allow_network and not get_bool_setting('offline_mode')
        if online and not body.retry:
            with get_db() as c:recent={r['name_norm']:r['checked_at'] for r in c.execute('SELECT * FROM metric_lookups')}
            selected=[g for g in selected if time.time()-recent.get(venues.norm_name(g['name']),0)>=COOLDOWN]
        if not online:selected=[]
        job={'id':uuid.uuid4().hex,'status':'queued' if selected else 'completed','total':len(selected),'completed':0,
             'fields_filled':fields,'errors':0,'results':[],'message':'正在准备指标查询' if selected else '已匹配本机缓存；没有需要立即查询的期刊',
             'created_at':completion.now()}
        save(job)
        if selected:
            cancel=threading.Event();_active[job['id']]=cancel
            try:threading.Thread(target=worker,args=(job,selected,cancel),daemon=True).start()
            except Exception:
                _active.pop(job['id'],None);job.update(status='failed',message='查询未能启动，可重试');save(job)
                raise HTTPException(503,'指标查询未能启动')
        return dict(job)

@router.get('/{jid}')
def read(jid:str):
    with get_db() as c:r=c.execute('SELECT payload FROM metric_jobs WHERE id=?',(jid,)).fetchone()
    if not r:raise HTTPException(404,'指标查询不存在')
    return json.loads(r['payload'])

@router.post('/{jid}/cancel')
def stop(jid:str):
    read(jid)
    with _lock:
        if jid in _active:_active[jid].set()
    return {'requested':True}
