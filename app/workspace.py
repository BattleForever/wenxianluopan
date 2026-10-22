"""Research workflow API: exploration, ranking, screening and reading lists."""
import csv
import io
import json
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from . import exploration, relevance, folders, trash
from .db import get_db
from .graph import compute_clusters
from .linkview import number, venue_public, zone_number
from .store import norm_venue, paper_dict
from . import __version__

router = APIRouter(prefix='/api/research')


def init():
    exploration.init()
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS reading_list (paper_id INTEGER PRIMARY KEY, note TEXT DEFAULT '', added_at TEXT DEFAULT (datetime('now','localtime')))")
    folders.init()
    trash.init()


class ExploreIn(BaseModel):
    seed_ids: list[int] = Field(min_length=1,max_length=10)
    direction: Literal['references','citing','both'] = 'both'
    depth: int = Field(default=2,ge=1,le=3)
    per_direction: int = Field(default=20,ge=1,le=100)
    max_nodes: int = Field(default=200,ge=10,le=500)
    online: bool = False
    max_fetch: int = Field(default=20,ge=1,le=30)


class SearchIn(BaseModel):
    query: str = Field(default='',max_length=4000)
    seed_ids: list[int] = Field(default_factory=list,max_length=10)
    job_id: str = ''
    scope: Literal['all','library','reading','task'] = 'all'
    sort: Literal['cited','year_desc','year_asc','impact_factor','cas_zone','relevance','network','depth'] = 'cited'
    year_min: int | None = Field(default=None,ge=1500,le=2200)
    year_max: int | None = Field(default=None,ge=1500,le=2200)
    min_if: float | None = Field(default=None,ge=0,le=1000)
    cas_max: int | None = Field(default=None,ge=1,le=4)
    cas_missing: bool = False
    zone_scheme: Literal['cas','jcr'] = 'cas'
    limit: int = Field(default=100,ge=1,le=500)
    offset: int = Field(default=0,ge=0,le=1000000)
    folder_id: int | None = Field(default=None,gt=0)
    unfiled: bool = False
    folder_descendants: bool = True


class ReadingIn(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=500)
    action: Literal['add','remove'] = 'add'
    note: str | None = Field(default=None,max_length=2000)


class ExportIn(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=500)
    format: Literal['csv','ris','graphml'] = 'csv'
    job_id: str = ''


def load_papers(scope='all'):
    with get_db() as conn:
        extra={
            'library':" AND status='library'",
            'reading':" AND id IN (SELECT paper_id FROM reading_list WHERE paper_id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='reading'))",
        }.get(scope,'')
        rows = conn.execute("SELECT * FROM papers WHERE id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library')"+extra+" ORDER BY id").fetchall()
        venues = {r['name_norm']:dict(r) for r in conn.execute('SELECT * FROM venues')}
        reading = {r['paper_id']:dict(r) for r in conn.execute("SELECT * FROM reading_list WHERE paper_id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='reading')")}
        grouping = folders.memberships(conn)
    papers = []
    for row in rows:
        p = paper_dict(row)
        v = venues.get(norm_venue(p.get('venue') or ''))
        p['venue_info'] = venue_public(v) if v else None
        p['in_reading_list'] = p['id'] in reading
        p['reading_note'] = (reading.get(p['id']) or {}).get('note','')
        p['folders'] = grouping.get(p['id'],[])
        papers.append(p)
    return papers


@router.get('/state')
def state():
    with get_db() as conn:
        n = conn.execute('SELECT COUNT(*) n FROM reading_list r JOIN papers p ON p.id=r.paper_id WHERE r.paper_id NOT IN (SELECT paper_id FROM paper_trash)').fetchone()['n']
        jobs = [json.loads(r['payload']) for r in conn.execute('SELECT payload FROM research_jobs ORDER BY rowid DESC LIMIT 5')]
    return {'reading_count':n,'jobs':[{k:j[k] for k in ('id','status','created_at','options','processed','new_records')} for j in jobs],
            'version':__version__,'method':'本地 TF-IDF 与 LSA 文本排序及引用网络重叠推荐',
            'data_note':'数据来自已导入书目与当前拓展任务；来源与补全记录可在文献详情查看。'}


@router.post('/explore')
def explore(body:ExploreIn):
    try:
        return exploration.create(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))


@router.get('/jobs/{job_id}')
def job_detail(job_id:str):
    job = exploration.get(job_id)
    if not job: raise HTTPException(404,'扩展任务不存在。')
    return job


@router.post('/jobs/{job_id}/cancel')
def cancel(job_id:str):
    if not exploration.get(job_id): raise HTTPException(404,'扩展任务不存在。')
    return {'ok':True,'requested':exploration.cancel(job_id),'message':'将在当前请求结束后停止；已发现的记录会保留。'}


@router.post('/search')
def search(body:SearchIn):
    if body.year_min and body.year_max and body.year_min>body.year_max:
        raise HTTPException(400,'开始年份应不晚于结束年份。')
    # Relevance keeps the full corpus for seed/query context. Other views only
    # decode the relevant records, especially small reading lists in large libraries.
    papers = load_papers('all' if body.sort=='relevance' else body.scope)
    task = exploration.get(body.job_id) if body.job_id else None
    if body.scope=='task' and not task: raise HTTPException(400,'请先建立或选择扩展任务。')
    paths = {p['id']:p for p in (task or {}).get('results',[])}
    scores,model = {},{'status':'not_requested','method':'未启用文本排序'}
    if body.sort=='relevance': scores,model = relevance.rank(papers,body.query,body.seed_ids)
    with get_db() as conn:
        edges = [dict(r) for r in conn.execute('SELECT from_id,to_id,src FROM citations')]
        allowed = folders.filtered_ids(conn,body.scope,body.folder_id,body.unfiled,body.folder_descendants)
    refs,citers = {},{}
    for e in edges:
        refs.setdefault(e['from_id'],set()).add(e['to_id'])
        citers.setdefault(e['to_id'],set()).add(e['from_id'])
    seeds = set(body.seed_ids)
    output = []
    facets={'total':0,'if_known':0,'cas_known':0,'jcr_known':0,'cas_zones':{str(i):0 for i in range(1,5)},'jcr_zones':{str(i):0 for i in range(1,5)}}
    query = body.query.casefold().strip()
    for p in papers:
        pid = p['id']
        if allowed is not None and pid not in allowed:continue
        if body.scope=='library' and p['status']!='library': continue
        if body.scope=='reading' and not p['in_reading_list']: continue
        if body.scope=='task' and pid not in paths: continue
        if query and body.sort!='relevance' and query not in ' '.join(str(p.get(k) or '') for k in ('title','title_zh','doi','abstract','abstract_zh')).casefold(): continue
        v=p['venue_info'] or {}
        year,impact,cas,jcr = p.get('year'),number(v.get('impact_factor')),zone_number(v.get('cas_zone')),zone_number(v.get('jcr_zone'))
        if body.year_min is not None and (year is None or year<body.year_min): continue
        if body.year_max is not None and (year is None or year>body.year_max): continue
        if body.min_if is not None and (impact is None or impact<body.min_if): continue
        facets['total']+=1
        if impact is not None:facets['if_known']+=1
        for key,value in [('cas',cas),('jcr',jcr)]:
            if value is not None:
                facets[key+'_known']+=1;facets[key+'_zones'][str(value)]+=1
        zone=cas if body.zone_scheme=='cas' else jcr
        if body.cas_missing and zone is not None:continue
        if body.cas_max is not None and (zone is None or zone>body.cas_max): continue
        sr=sc=0
        network_score=0.
        for sid in seeds-{pid}:
            a,b=refs.get(sid,set()),refs.get(pid,set())
            c,d=citers.get(sid,set()),citers.get(pid,set())
            rr,cc=len(a&b),len(c&d)
            sr+=rr; sc+=cc
            network_score=max(network_score,.65*rr/max(1,min(len(a),len(b)))+.35*cc/max(1,min(len(c),len(d))))
        p.update(scores.get(pid,{}))
        p.update(is_seed=pid in seeds,discovery=paths.get(pid),shared_refs=sr,shared_citers=sc,network_score=round(network_score,4))
        output.append(p)
    def key(p):
        v=p['venue_info'] or {}
        value=(number(v.get('impact_factor')) if body.sort=='impact_factor' else zone_number(v.get('cas_zone')) if body.sort=='cas_zone'
               else p.get('year') if body.sort.startswith('year') else p.get('relevance') if body.sort=='relevance'
               else p.get('network_score') if body.sort=='network' else (p.get('discovery') or {}).get('depth') if body.sort=='depth' else p.get('cited_by_count'))
        return (value is None,(value if body.sort in ('cas_zone','year_asc','depth') else -value) if value is not None else 0,-(p.get('year') or 0),p['id'])
    output.sort(key=key)
    visible=output[body.offset:body.offset+body.limit]
    ids={p['id'] for p in visible}
    ge=[e for e in edges if e['from_id'] in ids and e['to_id'] in ids]
    clusters=compute_clusters(visible,ge)
    for p in visible: p['cluster']=clusters.get(p['id'],0)
    return {'total':len(output),'shown':len(visible),'papers':visible,'model':model,'facets':facets,
            'offset':body.offset,'limit':body.limit,'has_more':body.offset+len(visible)<len(output),
            'edges':[{'from':e['from_id'],'to':e['to_id'],'source':e['src']} for e in ge],
            'metric_note':'IF 和分区属于期刊指标，不是论文质量分；未知值显示未知并排在最后，不按 0 处理；'
                          '设置 IF/分区/年份阈值时，该指标未知的文献会被排除（分区阈值同样排除未知分区）；'
                          '更新时间不等于指标所属年份，无法核定时显示"指标年份未知"。'}


@router.post('/reading')
def reading(body:ReadingIn):
    if body.action=='remove':
        result=trash.put(trash.Put(ids=body.ids,scope='reading'))
        return {'ok':True,'affected':result['affected'],'trashed':True}
    # Explicitly adding a previously removed item is a restoration, not a new
    # record: preserve its existing note and folder memberships.
    trash.restore(trash.Restore(ids=body.ids,scope='reading'))
    with get_db() as conn:
        trash.ensure_active(conn,body.ids)
        ids=[pid for pid in set(body.ids) if conn.execute('SELECT 1 FROM papers WHERE id=?',(pid,)).fetchone()]
        for pid in ids:
            if body.action=='add':
                conn.execute('INSERT OR IGNORE INTO reading_list(paper_id) VALUES(?)',(pid,))
                if body.note is not None: conn.execute('UPDATE reading_list SET note=? WHERE paper_id=?',(body.note,pid))
            else: conn.execute('DELETE FROM reading_list WHERE paper_id=?',(pid,))
        folders.reading_changed(conn,ids,removed=body.action=='remove')
    return {'ok':True,'affected':len(ids)}


def csv_cell(value):
    text=str(value) if value is not None else ''
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text


@router.post('/export')
def export(body:ExportIn):
    selected=set(body.ids)
    papers=[p for p in load_papers() if p['id'] in selected]
    if not papers: raise HTTPException(400,'请选择需要导出的文献。')
    task=exploration.get(body.job_id) if body.job_id else None
    paths={p['id']:p for p in (task or {}).get('results',[])}
    if body.format=='csv':
        buf=io.StringIO(newline=''); writer=csv.writer(buf)
        writer.writerow(['id','title','title_zh','doi','year','venue','cited_by_count','impact_factor','cas_zone','metric_source','metric_updated_at','metric_year','paper_source','depth','parent_id','seed_id','relation','edge_source','reading_note'])
        for p in papers:
            v=p['venue_info'] or {}; path=paths.get(p['id'],{})
            writer.writerow([csv_cell(x) for x in [p['id'],p.get('title'),p.get('title_zh'),p.get('doi'),p.get('year'),p.get('venue'),p.get('cited_by_count'),v.get('impact_factor'),v.get('cas_zone'),v.get('source'),v.get('updated_at'),v.get('metric_year') or '指标年份未知',p.get('source'),path.get('depth'),path.get('parent'),path.get('seed'),path.get('relation'),path.get('edge_source'),p.get('reading_note')]])
        content='\ufeff'+buf.getvalue(); mime='text/csv; charset=utf-8'; ext='csv'
    elif body.format=='ris':
        lines=[]
        def clean(value): return str(value or '').replace('\r',' ').replace('\n',' ')
        for p in papers:
            lines+=['TY  - JOUR','TI  - '+clean(p.get('title'))]
            lines+=['AU  - '+clean(a) for a in p.get('authors',[])]
            for tag,k in [('PY','year'),('DO','doi'),('JO','venue'),('AB','abstract')]:
                if p.get(k): lines.append(tag+'  - '+clean(p[k]))
            lines+=['ER  - ','']
        content='\n'.join(lines); mime='application/x-research-info-systems'; ext='ris'
    else:
        import networkx as nx
        g=nx.DiGraph()
        for p in papers: g.add_node(str(p['id']),title=p.get('title') or '',doi=p.get('doi') or '',year=p.get('year') or 0)
        with get_db() as conn:
            for e in conn.execute('SELECT * FROM citations'):
                if str(e['from_id']) in g and str(e['to_id']) in g: g.add_edge(str(e['from_id']),str(e['to_id']),source=e['src'] or '')
        content='\n'.join(nx.generate_graphml(g)); mime='application/xml'; ext='graphml'
    return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="papernet_selected.{ext}"'})
