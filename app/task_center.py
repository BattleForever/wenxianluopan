"""Read-only summaries across persisted background jobs; no network on reads."""
import json
from datetime import datetime,timezone
from fastapi import APIRouter,Query
from .db import get_db
router=APIRouter(prefix='/api/tasks')
ACTIVE={'running','queued','cancelling','generating','awaiting_access'}

def time_key(value):
    try:
        t=datetime.fromisoformat(value.replace('Z','+00:00'))
        # Exploration historically used system local naive datetimes.
        return t.timestamp()
    except (ValueError,TypeError,AttributeError):return 0

@router.get('')
def recent_tasks(limit:int=Query(default=50,ge=1,le=500)):
    if not isinstance(limit,int):limit=50
    tasks=[]
    with get_db() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for kind,table,title in [('exploration','research_jobs','文献拓展'),('enrichment','enrichment_jobs','资料补全'),('metrics','metric_jobs','期刊指标'),('import','import_jobs','文献导入')]:
            if table not in tables:continue
            for r in c.execute('SELECT payload FROM '+table):
                j=json.loads(r[0]);active=j.get('status') in ACTIVE
                total=j.get('total',0);completed=j.get('completed',0)
                row={'id':j['id'],'kind':kind,'title':title,'status':j['status'],'active':active,'created_at':j.get('created_at',''),
                     'completed':completed,'total':total,'fields_filled':j.get('fields_filled',0),
                     'errors':len(j.get('errors',[])) if isinstance(j.get('errors'),list) else j.get('errors',0)}
                if kind=='import':row['added']=(j.get('result') or {}).get('added',0)
                if kind=='exploration':row.update(found=len(j.get('results',[])),processed=j.get('processed',0),fetches=j.get('fetches',0),round=j.get('round',0),depth=j.get('options',{}).get('depth',1),truncated=bool(j.get('truncated')),pending_branches=len(j.get('pending_fetch_ids',[])),retention=j.get('retention'),strategy=j.get('options',{}).get('strategy'))
                tasks.append(row)
        if 'browser_capture_jobs' in tables:
            for r in c.execute('SELECT payload FROM browser_capture_jobs'):
                j=json.loads(r['payload']);tasks.append({'id':j['id'],'kind':'capture','title':'原文获取','paper_id':j['paper_id'],'status':j['status'],'stage':j.get('stage'),'active':j['status'] in ('queued','running','awaiting_access'),'created_at':j.get('created_at'),'total':1,'completed':int(j['status']=='completed')})
        if 'pdf_guide_jobs' in tables:
            for r in c.execute('SELECT id,paper_id,status,stage,started_at FROM pdf_guide_jobs'):
                tasks.append({'id':str(r['id']),'kind':'guide','title':'全文解读','paper_id':r['paper_id'],'status':r['status'],'active':r['status'] in ACTIVE,'created_at':r['started_at'],'stage':r['stage']})
    tasks.sort(key=lambda x:(time_key(x['created_at']),str(x['id'])),reverse=True)
    return {'tasks':tasks[:limit],'total':len(tasks),'active_count':sum(t['active'] for t in tasks),'has_more':len(tasks)>limit}
