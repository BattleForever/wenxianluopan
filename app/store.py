"""论文入库与去重。"""

import difflib
import json
import re

from .db import get_db


def norm_doi(doi: str):
    if not doi:
        return None
    d = doi.strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d)
    d = d.strip()
    return d or None


def norm_openalex_id(oid):
    """规范化 OpenAlex ID 表示：URL 前缀与大小写差异归一为 Wxxxx 形式。"""
    if not oid:
        return None
    m = re.match(r"^(?:https?://openalex\.org/)?(W\d+[A-Za-z0-9]*)$",
                 str(oid).strip(), re.IGNORECASE)
    return m.group(1).upper() if m else str(oid).strip() or None


def norm_title(t: str):
    if not t:
        return ""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (t or "").lower())


def find_existing(conn, doi=None, openalex_id=None, title=None, conflict=None):
    """按 openalex_id > doi > 题名精确匹配 查找已有条目（阶段 02A 身份规则）。

    - 相同规范化 DOI / OpenAlex ID 视为同一论文；大小写、URL 前缀等表示
      差异不产生重复记录。
    - 稳定标识明确冲突（DOI 与 OpenAlex ID 命中不同记录、输入标识与命中
      记录的另一标识不一致、同一标识命中多条）时不猜测：返回 None，并把
      冲突详情写入 conflict 字典（上层必须可见地报告，不得静默合并）。
    - 题名精确匹配仅在候选唯一且无标识冲突时用于合并补全；候选具有不同
      的非空 DOI 时视为不同论文，不参与合并。
    - 题名模糊相似度不再触发自动合并；疑似重复请用 find_similar_titles
      提示，不得单独静默合并。
    """
    if conflict is None:
        conflict = {}
    n_oa = norm_openalex_id(openalex_id)
    n_doi = norm_doi(doi)

    oa_row = None
    if n_oa:
        # 读写两侧统一规范化等价（02A-R1）：库内可能是旧软件/旧路径写入的
        # URL 形式或大小写差异，先按后缀粗筛，再在 Python 侧规范化比对。
        # 同一规范化 ID 命中多条旧记录时明确报冲突，不自动合并或拆分旧库。
        cand_rows = conn.execute(
            "SELECT * FROM papers WHERE openalex_id LIKE '%' || ?", (n_oa,)
        ).fetchall()
        by_id = {}
        for r in cand_rows:
            if norm_openalex_id(r["openalex_id"]) == n_oa:
                by_id[r["id"]] = r
        rows = list(by_id.values())
        if len(rows) > 1:
            conflict["type"] = "openalex_multi"
            conflict["detail"] = (f"OpenAlex ID {n_oa}（规范化后）命中 "
                                  f"{len(rows)} 条记录，不自动合并")
            return None
        oa_row = rows[0] if rows else None

    doi_row = None
    if n_doi:
        rows = conn.execute("SELECT * FROM papers WHERE doi=?", (n_doi,)).fetchall()
        if len(rows) > 1:
            conflict["type"] = "doi_multi"
            conflict["detail"] = f"DOI {n_doi} 命中 {len(rows)} 条记录"
            return None
        doi_row = rows[0] if rows else None

    if oa_row and doi_row and oa_row["id"] != doi_row["id"]:
        conflict["type"] = "doi_vs_openalex"
        conflict["detail"] = (f"DOI {n_doi} 命中记录 #{doi_row['id']}，"
                              f"OpenAlex ID {n_oa} 命中记录 #{oa_row['id']}，两者不同")
        return None
    if oa_row:
        if n_doi and oa_row["doi"] and oa_row["doi"] != n_doi:
            conflict["type"] = "doi_mismatch_on_openalex_hit"
            conflict["detail"] = (f"OpenAlex ID {n_oa} 命中记录 #{oa_row['id']}"
                                  f"（DOI {oa_row['doi']}），与输入 DOI {n_doi} 不同")
            return None
        return oa_row
    if doi_row:
        if n_oa and doi_row["openalex_id"] and \
                norm_openalex_id(doi_row["openalex_id"]) != n_oa:
            conflict["type"] = "openalex_mismatch_on_doi_hit"
            conflict["detail"] = (f"DOI {n_doi} 命中记录 #{doi_row['id']}"
                                  f"（OpenAlex ID {doi_row['openalex_id']}），"
                                  f"与输入 OpenAlex ID {n_oa} 不同")
            return None
        return doi_row

    nt = norm_title(title)
    if nt:
        cands = conn.execute(
            "SELECT * FROM papers WHERE norm_title=? AND norm_title != ''", (nt,)
        ).fetchall()
        # 输入带非空稳定标识时，具有不同非空标识的同名候选是不同论文，不合并
        if n_doi:
            cands = [c for c in cands if not c["doi"] or c["doi"] == n_doi]
        if n_oa:
            cands = [c for c in cands if not c["openalex_id"]
                     or norm_openalex_id(c["openalex_id"]) == n_oa]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            conflict["type"] = "title_ambiguous"
            conflict["detail"] = f"题名精确匹配命中 {len(cands)} 条记录，不猜测"
            return None
    return None


def find_similar_titles(conn, title, limit=5):
    """题名模糊相似的疑似重复提示（仅提示，不参与合并决策）。"""
    nt = norm_title(title)
    prefix = nt[:16]
    if not prefix:
        return []
    cands = conn.execute(
        "SELECT * FROM papers WHERE norm_title LIKE ? || '%'", (prefix,)
    ).fetchall()
    out = []
    for c in cands:
        ratio = difflib.SequenceMatcher(None, nt, c["norm_title"] or "").ratio()
        if 0 < ratio < 1 and ratio >= 0.93:
            out.append({"id": c["id"], "title": c["title"], "doi": c["doi"],
                        "ratio": round(ratio, 4)})
    out.sort(key=lambda x: -x["ratio"])
    return out[:limit]


def upsert_paper(conn, p: dict, source: str, status: str = "library"):
    """插入或合并论文，返回 (id, created)。已存在时补全空缺字段。

    稳定标识明确冲突时抛出 ValueError（上层必须可见地报告），不猜测、
    不覆盖标识、不静默合并。
    """
    doi = norm_doi(p.get("doi"))
    title = (p.get("title") or "").strip() or None
    conflict = {}
    existing = find_existing(conn, doi=doi, openalex_id=p.get("openalex_id"),
                             title=title, conflict=conflict)
    if conflict:
        raise ValueError(
            f"稳定标识冲突，未合并：{conflict.get('detail') or conflict.get('type')}")
    authors = json.dumps(p.get("authors") or [], ensure_ascii=False)
    tags = json.dumps(p.get("tags") or [], ensure_ascii=False)
    meta = json.dumps(p.get("meta") or {}, ensure_ascii=False)

    fields = {
        "openalex_id": norm_openalex_id(p.get("openalex_id")),
        "doi": doi,
        "title": title,
        "title_zh": (p.get("title_zh") or "").strip() or None,
        "norm_title": norm_title(title),
        "authors": authors if p.get("authors") else None,
        "year": p.get("year"),
        "venue": p.get("venue"),
        "abstract": p.get("abstract"),
        "abstract_zh": (p.get("abstract_zh") or "").strip() or None,
        "type": p.get("type"),
        "cited_by_count": p.get("cited_by_count"),
        "oa_pdf_url": p.get("oa_pdf_url"),
        "local_pdf_path": p.get("local_pdf_path"),
        "source": source,
        "tags": tags if p.get("tags") else None,
        "meta_json": meta if p.get("meta") else None,
    }

    if existing:
        sets, vals = [], []
        for k, v in fields.items():
            if v is None or v == "":
                continue
            cur = existing[k]
            if k == "cited_by_count":
                if (v or 0) > (cur or 0):
                    sets.append("cited_by_count=?"); vals.append(v)
                continue
            if cur in (None, ""):
                sets.append(f"{k}=?"); vals.append(v)
        # Zotero 键合并
        if p.get("meta", {}).get("zotero_key") and existing["meta_json"]:
            try:
                m = json.loads(existing["meta_json"])
                m.setdefault("zotero_key", p["meta"]["zotero_key"])
                sets.append("meta_json=?")
                vals.append(json.dumps(m, ensure_ascii=False))
            except (ValueError, TypeError):
                pass
        if existing["status"] == "candidate" and status == "library":
            sets.append("status='library'")
        if sets:
            sets.append("updated_at=datetime('now','localtime')")
            conn.execute(
                f"UPDATE papers SET {', '.join(sets)} WHERE id=?", (*vals, existing["id"])
            )
        _ensure_venue_row(conn, p.get("venue"), source)
        return existing["id"], False

    cur = conn.execute(
        """INSERT INTO papers(openalex_id, doi, title, title_zh, norm_title, authors,
           year, venue, abstract, abstract_zh, type, cited_by_count, status,
           oa_pdf_url, local_pdf_path, source, tags, meta_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fields["openalex_id"], fields["doi"], fields["title"], fields["title_zh"],
            fields["norm_title"], fields["authors"], fields["year"], fields["venue"],
            fields["abstract"], fields["abstract_zh"], fields["type"],
            fields["cited_by_count"] or 0, status,
            fields["oa_pdf_url"], fields["local_pdf_path"], source,
            fields["tags"], fields["meta_json"],
        ),
    )
    _ensure_venue_row(conn, p.get("venue"), source)
    return cur.lastrowid, True


def _ensure_venue_row(conn, venue, source: str):
    """遇到新期刊/会议时在 venues 表登记占位（便于后续联网补等级）。
    INSERT 与 UPDATE 分支都要调用，否则后补 venue 的文献永远进不了刷新队列。"""
    vn = norm_venue(venue or "")
    if not vn:
        return
    exists = conn.execute(
        "SELECT 1 FROM venues WHERE name_norm=?", (vn,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT OR IGNORE INTO venues(name, name_norm, source) VALUES(?,?,?)",
            (venue, vn, f"auto:{source}"),
        )


def check_provider_identity(conn, paper_id, openalex_id=None, doi=None,
                            conflict=None):
    """抓取入口写回/建边前的一致性校验（阶段 02A-R1/R2）。

    使用统一规范化规则核对两类冲突：
    1. 目标自身（02A-R2）：读取目标内部 ID 的当前记录，供应方 DOI /
       OpenAlex ID 与目标已有同类标识都非空且规范化后不相等时拒绝——
       供应方标识未被其他记录占用也不能视为一致。
    2. 其他记录占用（02A-R1）：供应方标识规范化后已归属其他内部记录时
       拒绝（含旧 URL/大小写等价与语义重复）。
    任一方缺少某项标识不算冲突（缺失 ≠ 明确矛盾），身份一致的正常补全
    仍然可用。返回 False 时把详情写入 conflict；调用方必须停止写回、
    不产生部分更新，并让错误对 API/任务状态可见。
    """
    if conflict is None:
        conflict = {}
    row = conn.execute(
        "SELECT id, doi, openalex_id FROM papers WHERE id=?", (paper_id,)
    ).fetchone()
    if not row:
        conflict["type"] = "target_missing"
        conflict["detail"] = f"目标内部记录 #{paper_id} 不存在，无法核验身份"
        return False
    n_prov_oa = norm_openalex_id(openalex_id)
    n_prov_doi = norm_doi(doi)
    n_own_oa = norm_openalex_id(row["openalex_id"])
    n_own_doi = norm_doi(row["doi"])
    # 1) 供应方标识 vs 目标自身标识
    if n_prov_oa and n_own_oa and n_prov_oa != n_own_oa:
        conflict["type"] = "openalex_mismatch_with_target"
        conflict["detail"] = (f"供应方 OpenAlex ID {n_prov_oa} 与目标 "
                              f"#{paper_id} 现有 OpenAlex ID {n_own_oa} 不同，不写回")
        return False
    if n_prov_doi and n_own_doi and n_prov_doi != n_own_doi:
        conflict["type"] = "doi_mismatch_with_target"
        conflict["detail"] = (f"供应方 DOI {n_prov_doi} 与目标 #{paper_id} "
                              f"现有 DOI {n_own_doi} 不同，不写回")
        return False
    # 2) 供应方标识是否已归属其他记录
    if n_prov_oa:
        rows = [r for r in conn.execute(
            "SELECT id, doi, openalex_id FROM papers "
            "WHERE openalex_id LIKE '%' || ? AND id != ?",
            (n_prov_oa, paper_id)).fetchall()
            if norm_openalex_id(r["openalex_id"]) == n_prov_oa]
        if rows:
            r0 = rows[0]
            conflict["type"] = "openalex_owned_by_other"
            conflict["detail"] = (f"供应方 OpenAlex ID {n_prov_oa} 已归属记录 "
                                  f"#{r0['id']}（DOI {r0['doi']}），与目标 "
                                  f"#{paper_id} 不同，不写回")
            return False
    if n_prov_doi:
        r = conn.execute(
            "SELECT id FROM papers WHERE doi=? AND id != ?",
            (n_prov_doi, paper_id)).fetchone()
        if r:
            conflict["type"] = "doi_owned_by_other"
            conflict["detail"] = (f"供应方 DOI {n_prov_doi} 已归属记录 "
                                  f"#{r['id']}，与目标 #{paper_id} 不同，不写回")
            return False
    return True


def add_citation(conn, from_id: int, to_id: int, src: str, reason: dict = None):
    """写入引用边，返回是否新增。

    自引用（from_id == to_id）、空端点与库中不存在的端点均被拒绝，返回
    False 且不写库；reason 为可选 dict，用于向调用方区分跳过原因：
    'self_loop' / 'invalid_endpoint' / 'duplicate'，不得把无效端点当成
    重复边。同一有效边重复写入为 INSERT OR IGNORE，返回 False（零新增）。
    不同论文之间的合法引用环不受影响。
    """
    if from_id is None or to_id is None:
        if reason is not None:
            reason["cause"] = "invalid_endpoint"
        return False
    if int(from_id) == int(to_id):
        if reason is not None:
            reason["cause"] = "self_loop"
        return False
    if not conn.execute("SELECT 1 FROM papers WHERE id=?",
                        (from_id,)).fetchone() or \
            not conn.execute("SELECT 1 FROM papers WHERE id=?",
                             (to_id,)).fetchone():
        if reason is not None:
            reason["cause"] = "invalid_endpoint"
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO citations(from_id, to_id, src) VALUES(?,?,?)",
        (from_id, to_id, src),
    )
    if cur.rowcount == 1:
        return True
    if reason is not None:
        reason["cause"] = "duplicate"
    return False


def norm_venue(name: str):
    if not name:
        return ""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (name or "").lower())


def upsert_venue(conn, v: dict):
    """插入或合并期刊/会议等级记录。返回 (id, created)。已存在时空缺字段补全。"""
    name = (v.get("name") or "").strip()
    if not name:
        return None, False
    nn = norm_venue(name)
    existing = conn.execute(
        "SELECT * FROM venues WHERE name_norm=?", (nn,)
    ).fetchone()
    vals = (
        v.get("kind"), v.get("impact_factor"), v.get("jcr_zone"),
        v.get("cas_zone"), v.get("cas_top"), v.get("cas_sub"),
        v.get("ccf_tier"), v.get("source"),
    )
    if existing:
        conn.execute(
            """UPDATE venues SET
               kind=COALESCE(?, kind),
               impact_factor=COALESCE(?, impact_factor),
               jcr_zone=COALESCE(?, jcr_zone),
               cas_zone=COALESCE(?, cas_zone),
               cas_top=COALESCE(?, cas_top),
               cas_sub=COALESCE(?, cas_sub),
               ccf_tier=COALESCE(?, ccf_tier),
               source=COALESCE(?, source),
               updated_at=datetime('now','localtime')
               WHERE id=?""",
            (*vals, existing["id"]),
        )
        return existing["id"], False
    cur = conn.execute(
        """INSERT INTO venues(name, name_norm, kind, impact_factor, jcr_zone,
           cas_zone, cas_top, cas_sub, ccf_tier, source)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (name, nn, *vals),
    )
    return cur.lastrowid, True


def venue_dict(row) -> dict:
    import json as _json
    d = dict(row)
    try:
        d["extra"] = _json.loads(d.get("extra_json") or "{}")
    except (ValueError, TypeError):
        d["extra"] = {}
    d.pop("extra_json", None)
    d.pop("name_norm", None)
    return d


def paper_dict(row) -> dict:
    d = dict(row)
    for k in ("authors", "tags"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (ValueError, TypeError):
            d[k] = []
    try:
        d["meta"] = json.loads(d.get("meta_json") or "{}")
    except (ValueError, TypeError):
        d["meta"] = {}
    d.pop("meta_json", None)
    d.pop("norm_title", None)
    return d
