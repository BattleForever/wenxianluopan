"""Broad observed-candidate retention with resumable online investigation.

Network budget controls work, not admission to the candidate pool. Local known
edges in the requested depth/direction are traversed even when online work pauses.
"""
from collections import deque
import re
from .db import get_db
from .discovery_supports import SupportLedger
from .store import paper_dict
from . import recommendation
from .netclient import net_stats_snapshot

def walk(seed_ids,direction,depth,online,max_fetch,stop,on_progress,fetcher,
         fetched_ids=None,prior_errors=None,research_query='',**unused):
    fetched=set(fetched_ids or []);errors=list(prior_errors or []);ledger=SupportLedger()
    net0=net_stats_snapshot()
    seen={int(s):{'id':int(s),'seed':int(s),'parent':None,'depth':0,'relation':'seed','edge_source':None} for s in seed_ids}
    queue=deque(seen);processed=added=0;ordered_level=0
    with get_db() as c:
        from .trash import hidden
        removed=hidden(c)
    def snapshot():
        results=list(seen.values());ledger.attach(results)
        pending=sorted(pid for pid,r in seen.items() if r['depth']<depth and pid not in fetched and pid not in removed)
        return {'results':results,'processed':processed,'fetches':len(fetched),'fetched_ids':sorted(fetched),'pending_fetch_ids':pending,'new_records':added,'errors':errors,'retention':'all_observed'}
    while queue and not stop.is_set():
        if seen[queue[0]]['depth']>ordered_level:
            ordered_level=seen[queue[0]]['depth']
            if ordered_level<depth:
                with get_db() as c:
                    rows=[paper_dict(r) for r in c.execute('SELECT * FROM papers') if r['id'] not in removed]
                    edges=[dict(r) for r in c.execute('SELECT from_id,to_id FROM citations')]
                ranked,_=recommendation.rank(rows,edges,seed_ids,research_query)
                frontier=[p for p in queue if seen[p]['depth']==ordered_level]
                rest=[p for p in queue if seen[p]['depth']!=ordered_level]
                queue=deque(sorted(frontier,key=lambda p:(ranked.get(p,{}).get('rank',10**9),p))+rest)
        pid=queue.popleft();level=seen[pid]['depth']
        if level>=depth or pid in removed:continue
        if online and pid not in fetched and (len(fetched)<max_fetch or level==0):
            fetched.add(pid)
            if on_progress:on_progress({**snapshot(),'round':level+1,'message':'正在核查种子直接关系' if level==0 else '正在核查相关分支'})
            try:added+=fetcher(pid,direction,None,stop)
            except Exception as exc:
                from .private_settings import service_candidates
                message=re.sub(r'(?i)(api_key|secretKey|token)=([^&\s]+)',r'\1=[redacted]',str(exc))
                for secret in service_candidates('openalex')+service_candidates('easyscholar'):message=message.replace(secret,'[redacted]')
                errors.append({'paper_id':pid,'error':message[:240]})
        ways=['references','citing'] if direction=='both' else [direction]
        with get_db() as c:
            for way in ways:
                sql=('SELECT to_id id,src FROM citations WHERE from_id=?' if way=='references' else 'SELECT from_id id,src FROM citations WHERE to_id=?')
                for row in c.execute(sql+' ORDER BY id',(pid,)):
                    nid=row['id']
                    if nid in removed:continue
                    ledger.observe(nid,seed=seen[pid]['seed'],parent=pid,relation=way,depth=level+1,edge_source=row['src'],edge_from=pid if way=='references' else nid,edge_to=nid if way=='references' else pid)
                    if nid not in seen:
                        seen[nid]={'id':nid,'seed':seen[pid]['seed'],'parent':pid,'depth':level+1,'relation':way,'edge_source':row['src']};queue.append(nid)
        processed+=1
        if on_progress:on_progress({**snapshot(),'round':level+1})
    pending=sorted(pid for pid,r in seen.items() if r['depth']<depth and pid not in fetched and pid not in removed)
    failed=sorted({e['paper_id'] for e in errors if e.get('paper_id') is not None})
    status='cancelled' if stop.is_set() else 'paused' if online and pending else 'completed_with_errors' if errors else 'completed'
    net1=net_stats_snapshot()
    network={key:net1[key]-net0[key] for key in ('http_requests','http_errors','cache_hits')}
    return {**snapshot(),'pending_fetch_ids':pending,'failed_fetch_ids':failed,'network':network,
      'truncated':bool(pending or errors or stop.is_set()),'status':status,
      'coverage':{'basis':'current_source_and_local_known_edges','not_global_completeness':True,
         'pending_branches':len(pending),'failed_branches':len(failed),'observed_candidates':len(seen),'work_budget':max_fetch}}
