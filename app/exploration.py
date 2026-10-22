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
        refs = oa_works_by_ids((record.get('referenced_works') or [])[:cap])
    if not stop.is_set() and direction in ('citing','both'):
        citing = list(oa_cited_by(wid,cap))
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
         online=False, max_fetch=20, stop=None, on_progress=None, fetcher=None):
    """Breadth-first traversal. Each result has one first-discovered seed path.

    P1：同时收集本次遍历实际观察到的去重发现依据（supports，唯一键
    seed,parent,id,relation），已见过的节点被其他父节点再次观察到时同样
    补充依据。network 字段区分节点抓取次数（fetches）与实际 HTTP 请求
    /缓存命中次数（netclient 计数器差值）。
    """
    stop = stop or threading.Event()
    fetcher = fetcher or bounded_fetch
    seen = {int(pid): {'id':int(pid),'depth':0,'parent':None,'seed':int(pid),'relation':'seed','edge_source':None} for pid in seed_ids}
    queue = deque(seen)
    processed = fetches = added = 0
    errors = []
    truncated = False
    ledger = SupportLedger()
    net0 = net_stats_snapshot()
    while queue and not stop.is_set():
        pid = queue.popleft()
        level = seen[pid]['depth']
        if level >= depth:
            continue
        if online:
            if fetches >= max_fetch:
                truncated = True
                break
            fetches += 1
            try:
                added += fetcher(pid,direction,per_direction,stop)
            except Exception as exc:
                errors.append({'paper_id':pid,'error':str(exc)[:240]})
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
                neighbors = conn.execute(sql+' ORDER BY p.cited_by_count DESC,p.id LIMIT ?', (pid,per_direction+1)).fetchall()
                if len(neighbors)>per_direction:
                    truncated = True
                for row in neighbors[:per_direction]:
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
                    if len(seen) >= max_nodes:
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
            raise ValueError('当前为离线模式，请先在设置中开启联网，或选择本地网络探索。')
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
        def run():
            try:
                job.update(status='running',message='按引用关系逐轮探索')
                save(job)
                def progress(values):
                    job.update(values)
                    save(job)
                result = walk(**job['options'],stop=event,on_progress=progress)
                job.update(result,message='已保留已发现文献；筛选后加入阅读清单。')
            except Exception as exc:
                job.update(status='failed',message=str(exc)[:300])
            finally:
                try:save(job)
                finally:
                    with _guard:_cancel.pop(job_id,None)
        try:threading.Thread(target=run,daemon=True).start()
        except Exception:
            _cancel.pop(job_id,None)
            job.update(status='failed',message='扩展任务未能启动，可重试。')
            try:save(job)
            except Exception:pass
            raise RuntimeError('扩展任务未能启动，可重试。')
        return job


def cancel(job_id):
    with _guard:
        event = _cancel.get(job_id)
        if event:
            event.set()
    return bool(event)
