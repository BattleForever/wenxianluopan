"""Research workflow API: exploration, ranking, screening and reading lists."""
import csv
import io
import json
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from . import exploration, relevance, folders, trash, recommendation, keyword_search
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
    depth: int = Field(default=1,ge=1,le=3)
    per_direction: int|None = Field(default=None,ge=1,le=1000)
    complete_seed: bool = True
    focused: bool = True
    strategy: Literal['balanced','legacy'] = 'legacy'
    research_query: str = Field(default='',max_length=4000)
    max_nodes: int = Field(default=300,ge=10,le=50000)
    online: bool = True
    max_fetch: int = Field(default=12,ge=1,le=5000)


class SearchIn(BaseModel):
    query: str = Field(default='',max_length=4000)
    # Legacy callers can still pass query as ranking context. The search bar
    # explicitly uses filter; its behaviour must not depend on selected sort.
    query_mode: Literal['filter','rank'] = 'rank'
    seed_ids: list[int] = Field(default_factory=list,max_length=10)
    job_id: str = ''
    scope: Literal['all','library','reading','task'] = 'all'
    sort: Literal['recommended','cited','year_desc','year_asc','impact_factor','cas_zone','relevance','network','depth'] = 'cited'
    recommendation_group: Literal['all','essential','references','citing','recent','bridge','review'] = 'all'
    year_min: int | None = Field(default=None,ge=1500,le=2200)
    year_max: int | None = Field(default=None,ge=1500,le=2200)
    min_if: float | None = Field(default=None,ge=0,le=1000)
    cas_max: int | None = Field(default=None,ge=1,le=4)
    cas_missing: bool = False
    zone_scheme: Literal['cas','jcr'] = 'cas'
    limit: int = Field(default=100,ge=1,le=500)
    offset: int = Field(default=0,ge=0,le=1000000)
    pin_seeds: bool = False
    all_results: bool = False
    folder_id: int | None = Field(default=None,gt=0)
    unfiled: bool = False
    folder_descendants: bool = True


class ReadingIn(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=500)
    action: Literal['add','remove'] = 'add'
    note: str | None = Field(default=None,max_length=2000)


class ExportIn(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=50000)
    format: Literal['csv','ris','graphml'] = 'csv'
    job_id: str = ''


def load_papers(scope='all'):
    from .metric_jobs import hydrate_cached
    hydrate_cached()
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


class ContinueIn(BaseModel):
    additional_fetch:int=Field(default=12,ge=1,le=500)

@router.post('/jobs/{job_id}/continue')
def continue_job(job_id:str,body:ContinueIn):
    try:return exploration.resume(job_id,body.additional_fetch)
    except ValueError as e:raise HTTPException(400,str(e))
    except RuntimeError as e:raise HTTPException(409,str(e))


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
    papers = load_papers('all' if body.sort in ('relevance','recommended') else body.scope)
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
    pool_ids=set(paths) if body.scope=='task' else {p['id'] for p in papers if (body.scope!='library' or p['status']=='library') and (body.scope!='reading' or p['in_reading_list'])}
    rec,rec_model=recommendation.rank(papers,edges,body.seed_ids,body.query,candidate_ids=pool_ids if allowed is None else pool_ids&allowed) if body.sort=='recommended' or body.recommendation_group!='all' else ({},{})
    if body.sort=='recommended':model=rec_model
    rec_facets={k:0 for k in recommendation.LABELS}
    pool_total=0

    output = []
    facets={'total':0,'if_known':0,'cas_known':0,'jcr_known':0,'cas_zones':{str(i):0 for i in range(1,5)},'jcr_zones':{str(i):0 for i in range(1,5)}}
    query = body.query.casefold().strip()
    matches,expansions=keyword_search.prepare(body.query)
    filter_query=bool(query) and (body.query_mode=='filter' or body.sort not in ('recommended','relevance'))
    scope_total=keyword_matches=0
    for p in papers:
        pid = p['id']
        if allowed is not None and pid not in allowed:continue
        if body.scope=='library' and p['status']!='library': continue
        if body.scope=='reading' and not p['in_reading_list']: continue
        if body.scope=='task' and pid not in paths: continue
        scope_total+=1
        matched=not query or matches(p)
        if matched:keyword_matches+=1
        if filter_query and not matched:continue
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
        pool_total+=1
        info=rec.get(pid)
        if info and pid not in seeds:
            for group in info['roles']:rec_facets[group]+=1
        if body.recommendation_group!='all' and (pid in seeds or not info or body.recommendation_group not in info['roles']):continue
        if info:p['recommendation']=info
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
        value=((p.get('recommendation') or {}).get('rank') if body.sort=='recommended' else number(v.get('impact_factor')) if body.sort=='impact_factor' else zone_number(v.get('cas_zone')) if body.sort=='cas_zone'
               else p.get('year') if body.sort.startswith('year') else p.get('relevance') if body.sort=='relevance'
               else p.get('network_score') if body.sort=='network' else (p.get('discovery') or {}).get('depth') if body.sort=='depth' else p.get('cited_by_count'))
        return (value is None,(value if body.sort in ('cas_zone','year_asc','depth','recommended') else -value) if value is not None else 0,-(p.get('year') or 0),p['id'])
    output.sort(key=key)
    anchors=[p for p in output if p['is_seed']][:max(0,body.limit-1)] if body.pin_seeds and not body.all_results else []
    anchor_ids={p['id'] for p in anchors}
    remaining=[p for p in output if p['id'] not in anchor_ids]
    capacity=body.limit-len(anchors)
    page=body.offset//body.limit
    page_count=max(1,(len(remaining)+capacity-1)//capacity)
    visible=output if body.all_results else anchors+remaining[page*capacity:(page+1)*capacity]
    if body.all_results:page=0;page_count=1
    # Group membership and pagination count matches only. Seeds are extra graph
    # endpoints, even when a year/keyword filter excludes them from the matches.
    context=[]
    if body.pin_seeds and body.recommendation_group!='all' and visible:
        visible_ids={p['id'] for p in visible}
        context_ids=set(seeds)
        if body.recommendation_group=='essential':
            for p in visible:context_ids.update((p.get('recommendation') or {}).get('evidence',{}).get('entry_path_ids',[]))
        context=[dict(p,is_seed=p['id'] in seeds,is_context=True,context_reason='引用路径') for p in papers if p['id'] in context_ids-visible_ids]
    ids={p['id'] for p in visible+context}
    ge=[e for e in edges if e['from_id'] in ids and e['to_id'] in ids]
    clusters=compute_clusters(visible+context,ge)
    for p in visible+context: p['cluster']=clusters.get(p['id'],0)
    return {'total':len(output),'shown':len(visible),'papers':visible,'context_papers':context,'context_count':len(context),'model':model,'facets':facets,
            'offset':page*body.limit,'limit':max(1,len(output)) if body.all_results else body.limit,'has_more':page+1<page_count,
            'all_results':body.all_results,'candidate_pool_total':pool_total,'recommendation_group':body.recommendation_group,'recommendation_groups':rec_facets,'recommendation_method':rec_model,
            'page_count':page_count,'anchor_count':len(anchors),
            'search':{'query':body.query.strip(),'mode':'filter' if filter_query or not query else 'rank',
                      'scope':body.scope,'scope_total':scope_total,'keyword_matches':keyword_matches,
                      'expansions':expansions if filter_query else [],'uses_network':False},
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
    from .project_files import save_export
    save_export('文献导出',ext,content)
    return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="papernet_selected.{ext}"'})
