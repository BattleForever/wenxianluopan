"""Explainable local literature ranking. Scores rank evidence, not paper quality.

Independent implementation of shared-reference/co-citation retrieval, personalized
graph proximity and rank fusion. No external model calls, journal-IF threshold or
global-citation threshold. Every supplied candidate remains in the output.
"""
from collections import defaultdict,deque
from datetime import datetime
import hashlib,json,math,threading
import networkx as nx
from . import relevance

VERSION='evidence_diverse_v3'
LABELS={'essential':'入门重点','references':'研究基础','citing':'后续进展','recent':'新近相关','bridge':'桥接线索','review':'待核查'}
_cache={};_lock=threading.Lock()

def rank(papers,edges,seed_ids,query='',year=None,candidate_ids=None):
    papers=sorted(papers,key=lambda p:p['id']);seeds=set(seed_ids);year=year or datetime.now().year
    edges=sorted({(e.get('from_id',e.get('from')),e.get('to_id',e.get('to'))) for e in edges})
    digest=hashlib.sha256(json.dumps([VERSION,year,sorted(seeds),query,sorted(candidate_ids) if candidate_ids is not None else None,
        [(p['id'],relevance.document(p)[:40000],p.get('year')) for p in papers],edges],ensure_ascii=False).encode()).hexdigest()
    with _lock:
        if _cache.get('key')==digest:return _cache['value']
    rows={p['id']:p for p in papers};present=set(rows);seeds&=present
    refs=defaultdict(set);citers=defaultdict(set)
    for a,b in edges:
        if a is None or b is None or a==b:continue
        refs[a].add(b);citers[b].add(a)
    n=max(1,len(rows))
    def overlap(a,b,inverse):
        common=a&b
        def weight(i):return 1+math.log((n+1)/(1+min(n,len(inverse[i]))))
        denom=math.sqrt(sum(weight(x) for x in a)*sum(weight(x) for x in b))
        return (sum(weight(x) for x in common)/denom if denom else 0),common
    text,text_model=relevance.rank(papers,query,list(seeds))
    g=nx.Graph();g.add_nodes_from(rows)
    g.add_edges_from((a,b) for a,b in edges if a in present and b in present and a!=b)
    if seeds:
        try:proximity=nx.pagerank(g,alpha=.75,personalization={x:1/len(seeds) if x in seeds else 0 for x in rows},max_iter=150)
        except nx.PowerIterationFailedConvergence:proximity={}
    else:proximity={}
    communities={}
    h=g.subgraph(present-seeds)
    if h.number_of_edges():
        for i,group in enumerate(nx.community.louvain_communities(h,seed=42)):
            for pid in group:communities[pid]=i
    nearest=set(seeds)
    for sid in seeds:nearest|=(refs[sid]|citers[sid])&present
    byseed={s:{} for s in sorted(seeds)};result={}
    for pid,p in rows.items():
        shared_refs=set();shared_citers=set();covered=set();direct_refs=[];direct_citers=[];bc=cc=0.
        for sid in sorted(seeds-{pid}):
            b,rr=overlap(refs[pid],refs[sid],citers);c,co=overlap(citers[pid],citers[sid],refs)
            bc=max(bc,b);cc=max(cc,c);shared_refs|=rr;shared_citers|=co
            if pid in refs[sid]:direct_refs.append(sid)
            if pid in citers[sid]:direct_citers.append(sid)
            signal=max(b,c)+(1 if sid in direct_refs or sid in direct_citers else 0)
            if signal>0:covered.add(sid);byseed[sid][pid]=signal
        groups=defaultdict(list)
        for neighbor in g.neighbors(pid):
            if neighbor in communities:groups[communities[neighbor]].append(neighbor)
        bridge=len(groups)>=2 and proximity.get(pid,0)>1e-9
        witnesses=[min(ids) for _,ids in sorted(groups.items())][:6] if bridge else []
        thematic=float(text.get(pid,{}).get('relevance',0))
        related=bool(direct_refs or direct_citers or shared_refs or shared_citers or thematic>=.07)
        recent=bool(p.get('year') and year-2<=p['year']<=year and related)
        foundation=len(citers[pid]&nearest)
        roles=[];reasons=[]
        if direct_refs:roles.append('references');reasons.append('种子直接参考文献')
        elif foundation>=2 and p.get('year') and any(rows[s].get('year') and p['year']<=rows[s]['year'] for s in seeds):
            roles.append('references');reasons.append(f'被种子邻域 {foundation} 篇文献引用的基础工作')
        if direct_citers:roles.append('citing');reasons.append('直接引用种子的后续工作')
        if shared_refs:reasons.append(f'与种子共享 {len(shared_refs)} 篇参考文献')
        if shared_citers:reasons.append(f'与种子被 {len(shared_citers)} 篇文献共同引用')
        if recent:roles.append('recent');reasons.append('近三年相关工作；不要求已有高被引')
        if bridge:roles.append('bridge');reasons.append(f'连接 {len(groups)} 组已知引文邻域')
        missing=[]
        if not p.get('abstract'):missing.append('abstract')
        if not refs[pid] and not citers[pid]:missing.append('citation_context')
        if missing:roles.append('review')
        if thematic>=.07:reasons.append('题名或摘要与研究主题匹配')
        if not reasons:reasons.append('当前依据有限，保留候选供核查')
        result[pid]={'version':VERSION,'roles':roles,'reasons':reasons,'missing':missing,
          'evidence':{'direct_reference_seeds':direct_refs,'direct_citing_seeds':direct_citers,
             'shared_reference_count':len(shared_refs),'shared_reference_ids':sorted(shared_refs)[:12],
             'co_citation_count':len(shared_citers),'co_citing_ids':sorted(shared_citers)[:12],
             'bridge_group_count':len(groups) if bridge else 0,'bridge_witness_ids':witnesses,
             'seed_coverage_ids':sorted(covered),'local_foundation_citations':foundation},
          'signals':{'text':thematic,'coupling':bc,'co_citation':cc,'proximity':proximity.get(pid,0),
             'foundation':foundation,'recent':1/max(1,year-(p.get('year') or 0)+1) if recent else 0,'bridge':len(groups) if bridge else 0}}
    candidates=(present if candidate_ids is None else present&set(candidate_ids))-seeds
    channels={}
    for name in ('text','coupling','co_citation','proximity','foundation','recent','bridge'):
        eligible=[pid for pid in candidates if result[pid]['signals'][name]>1e-9]
        channels[name]=sorted(eligible,key=lambda pid:(-result[pid]['signals'][name],pid))
    fused=defaultdict(float)
    for order in channels.values():
        for i,pid in enumerate(order,1):fused[pid]+=1/(30+i)
    core=sorted(candidates,key=lambda pid:(-fused[pid],pid))
    # Adapt the entry view to each evidence pool, rather than selecting only two
    # per direction. Round-robin known communities before filling by fused rank.
    essentials=set()
    entry_paths={}
    for sid in sorted(seeds):
        for pid,path in nx.single_source_shortest_path(g,sid,cutoff=2).items():
            if pid not in entry_paths or (len(path),path)<(len(entry_paths[pid]),entry_paths[pid]):entry_paths[pid]=path
    priority={pid:i for i,pid in enumerate(core)}
    for sid in sorted(seeds):
        buckets=[
            [pid for pid in core if sid in result[pid]['evidence']['direct_reference_seeds']],
            [pid for pid in core if sid in result[pid]['evidence']['direct_citing_seeds']],
            [pid for pid in core if sid in result[pid]['evidence']['seed_coverage_ids']
             and not result[pid]['evidence']['direct_reference_seeds'] and not result[pid]['evidence']['direct_citing_seeds']]]
        for bucket in buckets:
            reachable=[pid for pid in bucket if pid in entry_paths]
            quota=min(len(reachable),max(4,math.ceil(2*math.log2(1+len(reachable)))))
            groups=defaultdict(deque)
            for pid in sorted(reachable,key=lambda pid:priority[pid]):groups[communities.get(pid,-1)].append(pid)
            chosen=[]
            while len(chosen)<quota:
                progressed=False
                for queue in groups.values():
                    if queue and len(chosen)<quota:chosen.append(queue.popleft());progressed=True
                if not progressed:break
            essentials.update(chosen)
        if not any(sid in result[pid]['evidence']['seed_coverage_ids'] for pid in essentials):
            essentials.update([pid for pid in core if result[pid]['signals']['text']>=.07 and g.degree(pid)==0][:4])
    essentials.update(pid for pid in core if len(result[pid]['evidence']['direct_reference_seeds'])>=2)
    for pid in essentials:
        result[pid]['evidence']['entry_path_ids']=entry_paths.get(pid,[])
        result[pid]['roles'].insert(0,'essential')
        result[pid]['reasons'].insert(0,'入门代表：兼顾研究基础、后续工作与关联证据；非全部重要文献')
    queues=[deque(core),deque(core)]
    queues += [deque(sorted(byseed[sid],key=lambda pid:(-byseed[sid][pid],pid))) for sid in sorted(seeds)]
    queues += [deque(channels[name]) for name in ('recent','bridge','foundation','text')]
    queues += [deque(pid for pid in core if 'citing' in result[pid]['roles'])]
    selected=set();order=[]
    while len(selected)<len(candidates):
        changed=False
        for queue in queues:
            while queue and (queue[0] in selected or queue[0] not in candidates):queue.popleft()
            if queue:
                pid=queue.popleft();selected.add(pid);order.append(pid);changed=True
        if not changed:break
    for i,pid in enumerate(sorted(seeds)+order,1):result[pid]['rank']=i
    model={'status':'ok' if seeds or query else 'no_query','method':'引文证据与主题联合排序','version':VERSION,
      'hint':'排序综合共享参考、共同施引和本地文本匹配，并兼顾新近工作与不同引文邻域；不删除低排名候选。',
      'text_model':text_model['method'],'scope':'current_known_citation_graph','corpus_size':len(rows),
      'quality_probability':False}
    value=(result,model)
    with _lock:_cache.clear();_cache.update(key=digest,value=value)
    return value
