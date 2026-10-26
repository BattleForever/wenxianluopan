"""Bounded citation traversal, persisted tasks, explicit provenance and cancellation."""
from collections import deque
from datetime import datetime
import json
import threading
import uuid

from .db import get_db
from .discovery_supports import SupportLedger
from .netclient import is_online, net_stats_snapshot

_guard = threading.Lock()
_cancel = {}


def init():
    with get_db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS research_jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        # Interrupted jobs are not silently resumed with different network state.
        for row in conn.execute('SELECT id,payload FROM research_jobs').fetchall():
            job = json.loads(row['payload'])
            if job['status'] in ('queued','running','cancelling'):
                job.update(status='interrupted', message='上次运行被中断，可重新建立任务。')
                conn.execute('UPDATE research_jobs SET payload=? WHERE id=?', (json.dumps(job,ensure_ascii=False),row['id']))


def save(job):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO research_jobs(id,payload) VALUES(?,?)', (job['id'],json.dumps(job,ensure_ascii=False)))


def get(job_id):
    with get_db() as conn:
        row = conn.execute('SELECT payload FROM research_jobs WHERE id=?',(job_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def bounded_fetch(paper_id, direction, cap, stop):
    """Fetch only requested directions. Cap applies per direction, not the whole field."""
    from .netclient import oa_work_by_doi, oa_work_by_id, oa_works_by_ids, oa_cited_by, oa_work_to_paper
    from .store import upsert_paper, add_citation, check_provider_identity, norm_openalex_id
    with get_db() as conn:
        paper = conn.execute('SELECT * FROM papers WHERE id=?',(paper_id,)).fetchone()
    wid = paper['openalex_id']
    record = oa_work_by_id(wid) if wid else oa_work_by_doi(paper['doi']) if paper['doi'] else None
    if not record:
        raise ValueError('缺少可匹配的 OpenAlex ID 或 DOI；请补全后重试。')
    wid = record.get('id')
    refs, citing = [], []
    if not stop.is_set() and direction in ('references','both'):
        ids=(record.get('referenced_works') or []) if cap is None else (record.get('referenced_works') or [])[:cap]
        for offset in range(0,len(ids),50):
            if stop.is_set():break
            refs.extend(oa_works_by_ids(ids[offset:offset+50]))
    if not stop.is_set() and direction in ('citing','both'):
        for work in oa_cited_by(wid,cap):
            if stop.is_set():break
            citing.append(work)
    added = 0
    with get_db() as conn:
        # Retain source identity so repeated fetches resolve to the same record.
        # 02A-R2：实际用于建边/回写的完整供应方记录（ID 与 DOI）都必须先与
        # 目标种子自身身份核验；COALESCE 保留旧 ID 不能替代关系归属校验。
        # 冲突时抛错回滚，不写部分更新、不建边；walk 将错误记入任务状态。
        conf = {}
        if not check_provider_identity(
                conn, paper_id,
                openalex_id=record.get("id") or wid,
                doi=record.get("doi"), conflict=conf):
            raise ValueError(f"种子身份冲突，已停止探索写回：{conf.get('detail')}")
        conn.execute("UPDATE papers SET openalex_id=COALESCE(openalex_id,?), fetched_at=datetime('now','localtime') WHERE id=?",(norm_openalex_id(wid) or wid,paper_id))
        for works,is_ref in ((refs,True),(citing,False)):
            for work in works:
                pid,new = upsert_paper(conn,oa_work_to_paper(work),'openalex',status='candidate')
                if pid != paper_id:
                    add_citation(conn,paper_id if is_ref else pid,pid if is_ref else paper_id,'openalex')
                added += bool(new)
    return added


def walk(seed_ids, direction='both', depth=2, per_direction=20, max_nodes=200,
         online=False, max_fetch=20, stop=None, on_progress=None, fetcher=None,complete_seed=False,focused=False,strategy='legacy',fetched_ids=None,prior_errors=None,research_query=''):
    """Breadth-first traversal. Each result has one first-discovered seed path.

    P1：同时收集本次遍历实际观察到的去重发现依据（supports，唯一键
    seed,parent,id,relation），已见过的节点被其他父节点再次观察到时同样
    补充依据。network 字段区分节点抓取次数（fetches）与实际 HTTP 请求
    /缓存命中次数（netclient 计数器差值）。
    """
    stop = stop or threading.Event()
    fetcher = fetcher or bounded_fetch
    if strategy=='balanced':
        from .balanced_discovery import walk as balanced
        return balanced(seed_ids,direction,depth,online,max_fetch,stop,on_progress,fetcher,fetched_ids,prior_errors,research_query=research_query)
    seen = {int(pid): {'id':int(pid),'depth':0,'parent':None,'seed':int(pid),'relation':'seed','edge_source':None} for pid in seed_ids}
    queue = deque(seen)
    processed = fetches = added = 0
    errors = []
    truncated = False
    ledger = SupportLedger()
    net0 = net_stats_snapshot()
    ranked_level=-1
    import re,math
    from collections import Counter
    def terms(text):
        ignored={'the','and','for','with','from','using','based','study','research','this','that','into','through','these','which','their','were','have','been','paper','method','results','analysis','development'}
        return Counter(t for t in re.findall(r'[a-z][a-z0-9-]{2,}|[\u4e00-\u9fff]{2,}',(text or '').lower()) if t not in ignored)
    with get_db() as c:
        seed_text=' '.join((r['title'] or '')+' '+(r['abstract'] or '') for r in c.execute('SELECT title,abstract FROM papers WHERE id IN ('+','.join('?' for _ in seed_ids)+')',seed_ids))
    seed_terms=terms(seed_text)
    def focus_score(pid):
        with get_db() as c:r=c.execute('SELECT title,abstract,cited_by_count FROM papers WHERE id=?',(pid,)).fetchone()
        v=terms((r['title'] or '')+' '+(r['abstract'] or ''));dot=sum(min(n,v.get(w,0)) for w,n in seed_terms.items())
        return dot/math.sqrt(max(1,sum(seed_terms.values())*sum(v.values())))

    while queue and not stop.is_set():
        if focused and queue and seen[queue[0]]['depth']>0 and seen[queue[0]]['depth']!=ranked_level:
            ranked_level=seen[queue[0]]['depth']
            same=[pid for pid in queue if seen[pid]['depth']==ranked_level]
            later=[pid for pid in queue if seen[pid]['depth']!=ranked_level]
            queue=deque(sorted(same,key=lambda pid:(-focus_score(pid),pid))+later)
        pid = queue.popleft()
        level = seen[pid]['depth']
        cap=None if complete_seed and level==0 else per_direction
        if level >= depth:
            continue
        if complete_seed and level>0 and len(seen)>=max_nodes:
            truncated=True
            break
        if online:
            if fetches >= max_fetch and not (complete_seed and level==0):
                truncated = True
                if not complete_seed:break
            else:
                fetches += 1
                try:
                    added += fetcher(pid,direction,cap,stop)
                except Exception as exc:
                    # Never persist credential-bearing exception URLs in a task.
                    import re
                    message=re.sub(r'(?i)(api_key|secretKey|token)=([^&\s]+)',r'\1=[redacted]',str(exc))
                    from .private_settings import service_candidates
                    for secret in service_candidates('openalex')+service_candidates('easyscholar'):message=message.replace(secret,'[redacted]')
                    errors.append({'paper_id':pid,'error':message[:240]})
        if stop.is_set():
            break
        directions = ['references','citing'] if direction == 'both' else [direction]
        with get_db() as conn:
            from .trash import hidden
            removed=hidden(conn)
            if pid in removed:continue
            for way in directions:
                sql = ('SELECT p.id,c.src FROM citations c JOIN papers p ON p.id=c.to_id WHERE c.from_id=?'
                       if way == 'references' else 'SELECT p.id,c.src FROM citations c JOIN papers p ON p.id=c.from_id WHERE c.to_id=?')
                # This is a bounded sample, sorted by database citation count. Not exhaustive.
                ordered=sql+' ORDER BY p.cited_by_count DESC,p.id'
                neighbors = conn.execute(ordered if cap is None else ordered+' LIMIT ?', (pid,) if cap is None else (pid,cap+1)).fetchall()
                if cap is not None and len(neighbors)>cap:
                    truncated = True
                for row in neighbors if cap is None else neighbors[:cap]:
                    nid = row['id']
                    if nid in removed:continue
                    # P1：实际观察到的依据一律记录（含已见节点被另一父节点
                    # 再次观察的情形），去重键 seed,parent,id,relation。
                    # 只记录实际使用的 per_direction 条样本；用于检测截断的
                    # 多余一行不记依据。
                    ledger.observe(nid, seed=seen[pid]['seed'], parent=pid,
                                   relation=way, depth=level+1,
                                   edge_source=row['src'],
                                   edge_from=pid if way=='references' else nid,
                                   edge_to=nid if way=='references' else pid)
                    if nid in seen:
                        continue
                    if len(seen) >= max_nodes and not (complete_seed and level==0):
                        truncated = True
                        break
                    seen[nid] = {'id':nid,'depth':level+1,'parent':pid,'seed':seen[pid]['seed'],
                                 'relation':way,'edge_source':row['src']}
                    queue.append(nid)
        processed += 1
        if on_progress:
            snapshot = list(seen.values())
            ledger.attach(snapshot)
            on_progress({'results':snapshot,'processed':processed,'fetches':fetches,
                         'new_records':added,'errors':errors,'round':level+1,'truncated':truncated})
    net1 = net_stats_snapshot()
    network = {'http_requests': net1['http_requests'] - net0['http_requests'],
               'http_errors': net1['http_errors'] - net0['http_errors'],
               'cache_hits': net1['cache_hits'] - net0['cache_hits']}
    results = list(seen.values())
    ledger.attach(results)
    return {'results':results,'processed':processed,'fetches':fetches,
            'new_records':added,'errors':errors,'truncated':truncated,
            'network':network,
            'status':'cancelled' if stop.is_set() else 'completed_with_errors' if errors else 'completed'}


def create(options):
    with _guard:
        if _cancel:
            raise RuntimeError('已有扩展任务正在运行，请等待完成或先取消。')
        seed_ids = list(dict.fromkeys(options['seed_ids']))
        with get_db() as conn:
            actual = {r['id'] for r in conn.execute('SELECT id FROM papers WHERE id IN ('+','.join('?' for _ in seed_ids)+')',seed_ids)}
            from .trash import hidden
            actual-=hidden(conn)
        if set(seed_ids) != actual:
            raise ValueError('种子文献不存在。')
        if options['online'] and not is_online():
            options={**options,'online':False}
        job_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        _cancel[job_id] = event
        job = {'id':job_id,'created_at':datetime.now().isoformat(timespec='seconds'),
               'options':{**options,'seed_ids':seed_ids},'status':'queued','results':[],
               'processed':0,'fetches':0,'new_records':0,'errors':[],'round':0,
               'message':'准备扩展','truncated':False}
        try:save(job)
        except Exception:
            _cancel.pop(job_id,None)
            raise RuntimeError('任务未能保存，请检查数据目录后重试。')
        _launch(job,event)
        return job


def cancel(job_id):
    with _guard:
        event = _cancel.get(job_id)
        if event:
            event.set()
    return bool(event)


def _launch(job,event):
    def run():
        try:
            previous_added=job.get('new_records',0) if job.get('fetched_ids') else 0
            previous_results={r['id']:r for r in job.get('results',[])} if job.get('fetched_ids') else {}
            job.update(status='running',message='按引文与主题证据探索；保留全部已发现候选')
            save(job)
            def progress(values):
                if previous_results and 'results' in values:
                    values={**values,'results':list((previous_results|{r['id']:r for r in values['results']}).values())}
                job.update(values,new_records=previous_added+values.get('new_records',0));save(job)
            result=walk(**job['options'],stop=event,on_progress=progress,
                        fetched_ids=job.get('fetched_ids'),prior_errors=job.get('errors'))
            result['new_records']+=previous_added
            job.update(result,message='已保留全部已发现候选；待查分支可继续探索。' if result.get('pending_fetch_ids') else '本轮已结束；来源覆盖仍有边界。')
        except Exception:
            job.update(status='failed',message='探索未完成，已保存结果保留，可重新打开任务。')
        finally:
            try:save(job)
            finally:
                with _guard:_cancel.pop(job['id'],None)
    try:threading.Thread(target=run,daemon=True).start()
    except Exception:
        _cancel.pop(job['id'],None);job.update(status='failed',message='扩展任务未能启动，可重试。');save(job)
        raise RuntimeError('扩展任务未能启动，可重试。')


def resume(job_id,additional_fetch=12):
    with _guard:
        if _cancel:raise RuntimeError('已有扩展任务正在运行，请稍候。')
        j=get(job_id)
        if not j:raise ValueError('扩展任务不存在。')
        if j['options'].get('strategy')!='balanced':raise ValueError('旧任务可查看推荐结果；继续发现请按新策略建立任务。')
        if not j.get('pending_fetch_ids'):raise ValueError('当前没有待查分支；失败来源可在补全后重新建立探索。')
        if not is_online():raise ValueError('当前为离线模式，请在设置中关闭离线后继续。')
        j['options']['max_fetch']=len(j.get('fetched_ids',[]))+additional_fetch
        j['options']['online']=is_online()
        event=threading.Event();_cancel[job_id]=event
        j.update(status='queued',message='继续核查尚未查询的分支；已有候选保留')
        try:save(j)
        except Exception:
            _cancel.pop(job_id,None)
            raise RuntimeError('任务未能保存，请检查数据目录后重试。')
        _launch(j,event)
        return j
