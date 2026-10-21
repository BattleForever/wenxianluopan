"""引用边抓取与缓存：OpenAlex（主）+ OpenCitations（补充）。

关键约束：绝不在外层写事务内发起联网请求（netclient 的缓存写入需要
独立短事务），否则 SQLite 写锁自嵌套会死锁。统一采用两阶段：
Phase A 读库 + 联网取数（内存暂存）→ Phase B 单事务写回。
"""

import json
import logging

from .db import get_db, get_bool_setting, get_setting
from .netclient import (
    OfflineError,
    oc_citations,
    oc_references,
    oa_cited_by,
    oa_work_by_doi,
    oa_work_by_id,
    oa_work_to_paper,
    oa_works_by_dois,
    oa_works_by_ids,
)
from .store import (add_citation, check_provider_identity, find_existing,
                    norm_doi, norm_openalex_id, upsert_paper)

log = logging.getLogger("papernet.citations")


def fetch_citations_for_paper(paper_id: int, citedby_cap=None):
    """为一篇论文抓取引用边。返回统计 dict。"""
    cap = int(citedby_cap or get_setting("citedby_cap") or 300)
    oc_on = get_bool_setting("opencitations_enabled", True)
    stats = {"paper_id": paper_id, "refs": 0, "cited_by": 0, "oc_edges": 0,
             "new_candidates": 0, "matched": False,
             "skipped_self_loops": 0, "duplicate_edges": 0,
             "invalid_endpoints": 0}

    # ---------- Phase A1：读库 ----------
    with get_db() as conn:
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise ValueError(f"paper {paper_id} 不存在")
        doi = row["doi"]
        wid = row["openalex_id"]
        try:
            meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
        except (ValueError, TypeError):
            meta = {}

    # ---------- Phase A2：联网（若缺 openalex_id 先匹配） ----------
    match_update = None  # (wid, cited, provider_doi)
    if not wid:
        if doi:
            w = oa_work_by_doi(doi)
            if w:
                p = oa_work_to_paper(w)
                wid = p.get("openalex_id")
                # 供应方 DOI 一并留存，供写回前与目标种子核验（02A-R2）
                match_update = (wid, p.get("cited_by_count") or 0, p.get("doi"))
        elif row["title"] and len(row["title"]) > 15:
            from .importer import _match_paper
            p = _match_paper(title=row["title"])
            if p:
                wid = p.get("openalex_id")
                match_update = (wid, p.get("cited_by_count") or 0, p.get("doi"))
    if not wid:
        stats["error"] = "无法匹配 OpenAlex（缺 DOI/标题或离线）"
        return stats
    stats["matched"] = True

    rec = oa_work_by_id(wid) or {}
    ref_ids = rec.get("referenced_works") or []
    related_ids = rec.get("related_works") or []
    ref_works = oa_works_by_ids(ref_ids) if ref_ids else []
    rel_works = oa_works_by_ids(related_ids[:100]) if related_ids else []

    cited_works = list(oa_cited_by(wid, cap))

    # OpenCitations 补充：references 有界（=论文参考文献数）；
    # citations 方向可能返回数万条，仅对小被引数论文启用，大论文交给 OpenAlex cursor+cap
    oc_ref_dois, oc_cite_dois, oc_known = [], [], {}
    if oc_on and doi:
        try:
            oc_ref_dois = [d for d in (norm_doi(x) for x in oc_references(doi)) if d]
            if (row["cited_by_count"] or 0) <= 1000:
                oc_cite_dois = [d for d in (norm_doi(x) for x in oc_citations(doi)) if d]
        except OfflineError:
            pass
    all_oc = list(dict.fromkeys(oc_ref_dois + oc_cite_dois))
    with get_db() as conn:
        for d in all_oc:
            r2 = find_existing(conn, doi=d)
            if r2:
                oc_known[d] = r2["id"]
        missing = [d for d in all_oc if d not in oc_known]
    oc_works = oa_works_by_dois(missing[:500]) if missing else []

    # ---------- Phase B：单事务写回 ----------
    with get_db() as conn:
        # 02A-R2：所有用于种子回写/建边的供应方响应，写入前逐一核验与目标
        # 种子自身身份的一致性——首次匹配响应（match_update）与随后按 ID
        # 获取、实际用于引用拓展的记录（rec）都要核对，不能只查其一。
        # 冲突时抛错整体回滚：不写部分身份更新、不建边、不改 fetched_at /
        # meta_json / 用户集合；错误经 API/批量任务结果可见。
        if match_update:
            conf = {}
            if not check_provider_identity(conn, paper_id,
                                           openalex_id=match_update[0],
                                           doi=match_update[2], conflict=conf):
                raise ValueError(
                    f"种子身份冲突，已停止抓取写回：{conf.get('detail')}")
        conf_rec = {}
        rec_obj = rec if isinstance(rec, dict) else {}
        if not check_provider_identity(
                conn, paper_id,
                openalex_id=rec_obj.get("id") or wid,
                doi=rec_obj.get("doi"), conflict=conf_rec):
            raise ValueError(
                f"种子身份冲突，已停止抓取写回：{conf_rec.get('detail')}")
        # 02A-R3：两次响应彼此一致性。种子某标识为空时，两个互相矛盾的
        # 响应可以分别通过上面的"与种子对照"校验，因此这里对当前种子、
        # 首次匹配响应、实际建边响应三方统一收集 DOI 与 OpenAlex ID 的
        # 非空规范化值：同类标识出现两个不同值即明确拒绝；只有一个值或
        # 缺失不判冲突（正常补全不受影响）。
        target = conn.execute(
            "SELECT doi, openalex_id FROM papers WHERE id=?", (paper_id,)
        ).fetchone()
        identities = {
            "doi": [row["doi"] if row else None,
                    match_update[2] if match_update else None,
                    rec_obj.get("doi")],
            "openalex": [row["openalex_id"] if row else None,
                         match_update[0] if match_update else None,
                         rec_obj.get("id") or wid],
        }
        cross = {}
        for kind, raw_values in identities.items():
            norm = norm_doi if kind == "doi" else norm_openalex_id
            distinct = {v for v in (norm(x) for x in raw_values if x) if v}
            if len(distinct) > 1:
                cross[kind] = sorted(distinct)
        if cross:
            detail = "；".join(
                f"{k.upper()} 出现多个不同值 {vals}" for k, vals in cross.items())
            raise ValueError(
                f"种子身份冲突，已停止抓取写回：供应方两次响应/种子身份不一致：{detail}")
        if match_update:
            conn.execute(
                "UPDATE papers SET openalex_id=?, "
                "cited_by_count=MAX(cited_by_count, ?), doi=COALESCE(doi, ?) WHERE id=?",
                (norm_openalex_id(match_update[0]), match_update[1],
                 match_update[2], paper_id),
            )

        def upsert_one(w, src):
            added, mapping = _upsert_candidates(conn, [w], src)
            stats["new_candidates"] += added
            return mapping.get(w.get("id"))

        def edge_stat(blank_reason_key, added, cause, pid, paper_id):
            if added:
                stats[blank_reason_key] += 1
            elif cause == "self_loop" or pid == paper_id:
                stats["skipped_self_loops"] += 1
            elif cause == "invalid_endpoint":
                stats["invalid_endpoints"] += 1
            else:
                stats["duplicate_edges"] += 1

        for w in ref_works:
            pid = upsert_one(w, "openalex")
            if pid:
                rs = {}
                edge_stat("refs", add_citation(conn, paper_id, pid, "openalex",
                                               reason=rs),
                          rs.get("cause"), pid, paper_id)

        for w in cited_works:
            pid = upsert_one(w, "openalex")
            if pid:
                rs = {}
                edge_stat("cited_by", add_citation(conn, pid, paper_id, "openalex",
                                                   reason=rs),
                          rs.get("cause"), pid, paper_id)

        for w in rel_works:  # related_works 只入候选池，不建边
            upsert_one(w, "openalex")

        for w in oc_works:
            pid = upsert_one(w, "opencitations")
            d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            if pid and d:
                oc_known[d] = pid

        for d in oc_ref_dois:
            if oc_known.get(d):
                rs = {}
                edge_stat("oc_edges",
                          add_citation(conn, paper_id, oc_known[d],
                                       "opencitations", reason=rs),
                          rs.get("cause"), oc_known[d], paper_id)
        for d in oc_cite_dois:
            if oc_known.get(d):
                rs = {}
                edge_stat("oc_edges",
                          add_citation(conn, oc_known[d], paper_id,
                                       "opencitations", reason=rs),
                          rs.get("cause"), oc_known[d], paper_id)

        meta["related_ids"] = related_ids[:100]
        conn.execute(
            "UPDATE papers SET meta_json=?, fetched_at=datetime('now','localtime') WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), paper_id),
        )
    return stats


def _upsert_candidates(conn, works, src):
    """批量把 OpenAlex work 写入 candidate。返回 (新增数, {wid: id})。"""
    mapping = {}
    added = 0
    for w in works:
        p = oa_work_to_paper(w)
        pid, created = upsert_paper(conn, p, source=src, status="candidate")
        if p.get("openalex_id"):
            mapping[p["openalex_id"]] = pid
        added += 1 if created else 0
    return added, mapping


def fetch_citations_batch(paper_ids, citedby_cap=None):
    results = []
    for pid in paper_ids:
        try:
            results.append(fetch_citations_for_paper(pid, citedby_cap))
        except OfflineError:
            raise
        except Exception as e:  # noqa: BLE001
            results.append({"paper_id": pid, "error": str(e)})
    return results
