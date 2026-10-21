"""图算法：图数据组装、Louvain 聚类、相似论文推荐、GraphML 导出。"""

import io

import networkx as nx

from .db import get_db
from .store import paper_dict


def _load_rows(include_candidates: bool, collection_id=None,
               year_min=None, year_max=None, candidate_cap=300):
    where = []
    params = []
    if not include_candidates:
        where.append("status='library'")
    if year_min:
        where.append("year >= ?")
        params.append(int(year_min))
    if year_max:
        where.append("year <= ?")
        params.append(int(year_max))
    if collection_id:
        where.append("id IN (SELECT paper_id FROM collection_items WHERE collection_id=?)")
        params.append(int(collection_id))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM papers {clause}", params).fetchall()]
        if include_candidates:
            libs = [r for r in rows if r["status"] == "library"]
            cands = [r for r in rows if r["status"] != "library"]
            if collection_id and libs:
                # 收藏集视图：候选 = 与成员有直接引用关系的一跳邻域文献
                member_ids = [r["id"] for r in libs]
                ph = ",".join("?" * len(member_ids))
                neigh = conn.execute(
                    f"""SELECT DISTINCT p.* FROM papers p
                        JOIN citations c ON p.id IN (c.from_id, c.to_id)
                        WHERE (c.from_id IN ({ph}) OR c.to_id IN ({ph}))
                          AND p.status != 'library'""",
                    member_ids + member_ids,
                ).fetchall()
                cands += [dict(r) for r in neigh]
            else:
                # 直接在 SQL 里按被引数截断，避免把上万候选整表载入内存
                cand_where = ["status!='library'"]
                cand_params = []
                if year_min:
                    cand_where.append("year >= ?")
                    cand_params.append(int(year_min))
                if year_max:
                    cand_where.append("year <= ?")
                    cand_params.append(int(year_max))
                cand_params.append(int(candidate_cap))
                cands = [dict(r) for r in conn.execute(
                    "SELECT * FROM papers WHERE " + " AND ".join(cand_where) +
                    " ORDER BY cited_by_count DESC, id LIMIT ?",
                    cand_params).fetchall()]
            # 候选按被引数截断，避免图过载
            seen, uniq = set(), []
            for r in cands:
                in_year = ((not year_min or (r["year"] is not None and r["year"] >= int(year_min)))
                           and (not year_max or (r["year"] is not None and r["year"] <= int(year_max))))
                if r["id"] not in seen and in_year:
                    seen.add(r["id"])
                    uniq.append(r)
            uniq.sort(key=lambda r: r["cited_by_count"] or 0, reverse=True)
            rows = libs + uniq[:int(candidate_cap)]
        # 只保留两端都在节点集内的边
        ids = {r["id"] for r in rows}
        edges = conn.execute("SELECT from_id, to_id, src FROM citations").fetchall()
        edges = [dict(e) for e in edges if e["from_id"] in ids and e["to_id"] in ids]
    return rows, edges


def build_graph(include_candidates=False, collection_id=None, year_min=None,
                year_max=None, candidate_cap=300):
    rows, edges = _load_rows(include_candidates, collection_id, year_min, year_max,
                             candidate_cap)
    nodes = []
    for r in rows:
        d = paper_dict(r)
        nodes.append({
            "id": d["id"],
            "title": d["title"] or "(无标题)",
            "doi": d["doi"],
            "openalex_id": d["openalex_id"],
            "authors": (d["authors"] or [])[:3],
            "year": d["year"],
            "venue": d["venue"],
            "cited_by_count": d["cited_by_count"] or 0,
            "status": d["status"],
            "has_pdf": bool(d["local_pdf_path"] or d["oa_pdf_url"]),
            "local_pdf": bool(d["local_pdf_path"]),
        })
    cluster = compute_clusters(nodes, edges)
    for n in nodes:
        n["cluster"] = cluster.get(n["id"], 0)
    return {
        "nodes": nodes,
        "edges": [{"from": e["from_id"], "to": e["to_id"]} for e in edges],
        "stats": {
            "papers": len(nodes),
            "library": sum(1 for n in nodes if n["status"] == "library"),
            "candidates": sum(1 for n in nodes if n["status"] != "library"),
            "edges": len(edges),
        },
    }


def compute_clusters(nodes, edges):
    """Louvain 社区检测，返回 {paper_id: cluster_index}。"""
    if len(nodes) < 3 or not edges:
        return {n["id"]: 0 for n in nodes}
    G = nx.Graph()
    G.add_nodes_from(n["id"] for n in nodes)
    G.add_edges_from((e["from_id"], e["to_id"]) for e in edges)
    try:
        comms = nx.community.louvain_communities(G, seed=42)
    except Exception:  # noqa: BLE001
        return {n["id"]: 0 for n in nodes}
    out = {}
    for i, c in enumerate(sorted(comms, key=len, reverse=True)):
        for nid in c:
            out[nid] = i
    return out


_SIM_CACHE = {"ver": None, "G": None, "status_map": None, "openalex_map": None}


def _sim_graph():
    """全库引用有向图（含候选），按 (论文数, 边数) 做失效判断的内存缓存，
    避免每次相似推荐都全量重建 networkx 图。"""
    with get_db() as conn:
        rows = conn.execute("SELECT id, status, openalex_id FROM papers ORDER BY id").fetchall()
        edges = conn.execute("SELECT from_id, to_id FROM citations ORDER BY from_id,to_id").fetchall()
    ver = (tuple(tuple(r) for r in rows), tuple(tuple(e) for e in edges))
    if _SIM_CACHE["ver"] == ver and _SIM_CACHE["G"] is not None:
        return _SIM_CACHE["G"], _SIM_CACHE["status_map"], _SIM_CACHE["openalex_map"]
    G = nx.DiGraph()
    G.add_nodes_from(r["id"] for r in rows)
    G.add_edges_from((e["from_id"], e["to_id"]) for e in edges)
    status_map = {r["id"]: r["status"] for r in rows}
    openalex_map = {r["id"]: r["openalex_id"] for r in rows}
    _SIM_CACHE.update(ver=ver, G=G, status_map=status_map, openalex_map=openalex_map)
    return G, status_map, openalex_map


def similar_papers(paper_id: int, limit=20):
    """基于引用网络的相似论文推荐：
    共参考文献 (bibliographic coupling) + 共被引 (co-citation) 的重叠系数加权。
    覆盖库内全部论文与候选，用于"按引用拓展新文献"。"""
    G, status_map, openalex_map = _sim_graph()
    with get_db() as conn:
        seed = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if not seed:
        raise ValueError(f"paper {paper_id} 不存在")

    sid = seed["id"]
    succ_s = set(G.successors(sid))
    pred_s = set(G.predecessors(sid))
    related_ids = set()
    if seed["openalex_id"]:
        try:
            import json
            m = json.loads(seed["meta_json"]) if seed["meta_json"] else {}
            related_ids = set(m.get("related_ids") or [])
        except (ValueError, TypeError):
            pass

    scored = []
    for nid in G.nodes:
        if nid == sid:
            continue
        succ_c = set(G.successors(nid))
        pred_c = set(G.predecessors(nid))
        co = len(succ_s & succ_c)
        bc = len(pred_s & pred_c)
        is_related = openalex_map.get(nid) in related_ids
        if co == 0 and bc == 0 and not is_related:
            continue
        j_co = co / max(1, min(len(succ_s), len(succ_c))) if succ_s and succ_c else 0
        j_bc = bc / max(1, min(len(pred_s), len(pred_c))) if pred_s and pred_c else 0
        score = 0.65 * j_co + 0.35 * j_bc
        if is_related:
            score += 0.05
        if score <= 0:
            continue
        scored.append({
            "id": nid,
            "score": round(score, 4),
            "shared_refs": co,
            "shared_citers": bc,
            "in_library": status_map.get(nid) == "library",
            "openalex_id": openalex_map.get(nid),
            "related_signal": is_related,
        })
    scored.sort(key=lambda x: (x["score"], x["shared_refs"] + x["shared_citers"]),
                reverse=True)
    top = scored[:limit]
    # 补充元数据
    with get_db() as conn:
        for item in top:
            r = conn.execute(
                "SELECT id, title, authors, year, venue, cited_by_count, status, doi, "
                "local_pdf_path, oa_pdf_url FROM papers WHERE id=?", (item["id"],)
            ).fetchone()
            if r:
                d = paper_dict(r)
                item.update({
                    "title": d["title"], "authors": (d["authors"] or [])[:3],
                    "year": d["year"], "venue": d["venue"],
                    "cited_by_count": d["cited_by_count"], "doi": d["doi"],
                    "has_pdf": bool(d["local_pdf_path"] or d["oa_pdf_url"]),
                })
    return top


def export_graphml(include_candidates=False, collection_id=None):
    rows, edges = _load_rows(include_candidates, collection_id, None, None, 10**9)
    G = nx.DiGraph()
    for r in rows:
        d = paper_dict(r)
        G.add_node(r["id"], **{
            "label": (d["title"] or "")[:200],
            "doi": d["doi"] or "",
            "year": d["year"] or 0,
            "venue": (d["venue"] or "")[:120],
            "status": d["status"],
            "cited_by_count": d["cited_by_count"] or 0,
        })
    for e in edges:
        G.add_edge(e["from_id"], e["to_id"])
    buffer = io.BytesIO()
    nx.write_graphml(G, buffer, named_key_ids=True)
    return buffer.getvalue()
