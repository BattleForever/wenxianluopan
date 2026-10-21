"""长期本地库迁移（B 线 parallel_v03）。

迁移来源：原库一致性快照 evidence/library_snapshot/papernet_snapshot.db
         （backup API 生成，见 app/library_snapshot.py）。
迁移目标：长期数据目录（环境变量 PAPERNET_LIBRARY_DIR 指定，默认
         library/papernet.db；候选包不含具体私有路径）。

迁移内容：
- papers 全字段（含中文字段 title_zh/abstract_zh、local_pdf_path、meta_json
  内的 zotero_key/tags/related_ids/language/enrich_tried 等扩展 meta），
  保留原内部 ID；
- citations 引用边、collections/collection_items 集合、venues、settings；
- fetch_cache（可缓存数据，原样带入以保证迁移后与源库逐表一致）；
- 新增表：
  * content_provenance —— 字段级内容来源（origin 语义见 §3.1）；
    迁移时对已有 title_zh/abstract_zh 的行登记 origin='library_unknown_source'
    （原库未记录来源信息，不伪造模型/时间；原库 meta 键经全量盘点无任何
    来源标记，见 snapshot_report.json 的 papers_meta_key_counts）；
  * content_extra —— summary 等扩展内容字段（迁移时原库无该数据，空表）；
  * zotero_sync_queue —— Zotero 同步待办/重试队列（app/zotero_sync.py 使用）。

冲突处理：原库冲突只列出（evidence/conflicts.json），不自动修复、不模糊合并。
幂等性（parallel_v04 §3.5 安全重跑语义）：
- papers：目标已有行绝不覆盖（绝不 UPDATE 已有非空值），仅对目标为 NULL
  的列补缺；补缺的中文字段登记 provenance origin=library_unknown_source、
  recorded_by='migration_v04'；目标比源多的行/用户修改一律保留不回滚。
- 其余表：INSERT OR IGNORE 保守合并（已有行不动，仅补目标缺失的行）。
- content_provenance 已有记录（尤其 library_human）绝不覆盖（INSERT OR IGNORE）。
- 同一来源重复执行 = 安全 no-op（对已补齐目标）；冲突另列。

用法：
    python -m app.library_migrate                # 全流程（迁移两遍做幂等验证）
    python -m app.library_migrate --no-idempotency
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
from pathlib import Path

TRACK_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = TRACK_ROOT / "evidence" / "library_snapshot" / "papernet_snapshot.db"
# 长期库目录可用环境变量 PAPERNET_LIBRARY_DIR 覆盖（总控交付接缝：
# 候选包不含私有路径；本机运行时经环境变量注入真实目录）。
LIBRARY_DIR = Path(os.environ.get("PAPERNET_LIBRARY_DIR", "library"))
DEFAULT_TARGET = LIBRARY_DIR / "papernet.db"
DEFAULT_EVIDENCE_DIR = TRACK_ROOT / "evidence" / "migration"

# 与 app/db.py（冻结共享文件）保持一致的 v02 基础 schema，
# 外加 B 线新增表。此处独立成文，避免 import 冻结文件后与之耦合漂移；
# 逐列定义与 db.SCHEMA 完全相同。
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    openalex_id TEXT UNIQUE,
    doi TEXT UNIQUE,
    title TEXT,
    title_zh TEXT,
    norm_title TEXT,
    authors TEXT,
    year INTEGER,
    venue TEXT,
    abstract TEXT,
    abstract_zh TEXT,
    type TEXT,
    cited_by_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'candidate',
    oa_pdf_url TEXT,
    local_pdf_path TEXT,
    source TEXT,
    tags TEXT,
    meta_json TEXT,
    fetched_at TEXT,
    added_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_norm_title ON papers(norm_title);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);

CREATE TABLE IF NOT EXISTS citations (
    from_id INTEGER NOT NULL,
    to_id INTEGER NOT NULL,
    src TEXT,
    PRIMARY KEY (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_citations_to ON citations(to_id);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL,
    paper_id INTEGER NOT NULL,
    PRIMARY KEY (collection_id, paper_id)
);

CREATE TABLE IF NOT EXISTS fetch_cache (
    key TEXT PRIMARY KEY,
    url TEXT,
    status INTEGER,
    body TEXT,
    fetched_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS venues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_norm TEXT UNIQUE,
    kind TEXT,
    impact_factor TEXT,
    jcr_zone TEXT,
    cas_zone TEXT,
    cas_top TEXT,
    cas_sub TEXT,
    ccf_tier TEXT,
    source TEXT,
    extra_json TEXT,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_venues_norm ON venues(name_norm);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

B_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_provenance (
    paper_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    text TEXT,
    origin TEXT NOT NULL,
    source_hash TEXT,
    model TEXT,
    prompt_version TEXT,
    recorded_by TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT,
    PRIMARY KEY (paper_id, field)
);
CREATE TABLE IF NOT EXISTS content_extra (
    paper_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    text TEXT,
    updated_at TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (paper_id, field)
);
CREATE TABLE IF NOT EXISTS zotero_sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER,
    payload_json TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    zotero_key TEXT,
    last_error TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_zsq_state ON zotero_sync_queue(state);
CREATE INDEX IF NOT EXISTS idx_zsq_paper ON zotero_sync_queue(paper_id);
"""

COPY_TABLES = ["papers", "citations", "collections", "collection_items",
               "venues", "settings", "fetch_cache"]

# §3.5：迁移/补缺登记 provenance 的 recorded_by 标识（v04 起启用）
MIGRATION_RECORDED_BY = "migration_v04"


def norm_doi(doi):
    if not doi:
        return None
    d = str(doi).strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d).strip()
    return d or None


def norm_openalex_id(oid):
    if not oid:
        return None
    m = re.match(r"^(?:https?://openalex\.org/)?(W\d+[A-Za-z0-9]*)$",
                 str(oid).strip(), re.IGNORECASE)
    return m.group(1).upper() if m else str(oid).strip() or None


def compute_source_hash(title, abstract, prompt_version=""):
    """内容来源哈希：规范化题名+摘要（+prompt_version）的 sha256。

    ai_with_source 的判定依据：记录时的哈希必须与当前输入一致。
    """
    norm = lambda s: re.sub(r"\s+", "", (s or "")).lower()
    payload = json.dumps(
        {"title": norm(title), "abstract": norm(abstract),
         "prompt_version": prompt_version},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_library_schema(conn: sqlite3.Connection):
    conn.executescript(BASE_SCHEMA)
    conn.executescript(B_SCHEMA)


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> dict:
    """保守复制（§3.5）：目标已有行绝不覆盖（INSERT OR IGNORE），
    仅新增目标缺失的行；返回 {source_rows, inserted, skipped_existing}。"""
    cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")').fetchall()]
    dst_cols = {r[1] for r in dst.execute(f'PRAGMA table_info("{table}")').fetchall()}
    use = [c for c in cols if c in dst_cols]
    if not use:
        return {"source_rows": 0, "inserted": 0, "skipped_existing": 0}
    collist = ", ".join(f'"{c}"' for c in use)
    ph = ", ".join(["?"] * len(use))
    rows = src.execute(f'SELECT {collist} FROM "{table}"').fetchall()
    inserted = 0
    for row in rows:
        cur = dst.execute(
            f'INSERT OR IGNORE INTO "{table}" ({collist}) VALUES ({ph})', row)
        inserted += 1 if cur.rowcount else 0
    return {"source_rows": len(rows), "inserted": inserted,
            "skipped_existing": len(rows) - inserted}


def _merge_papers(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict:
    """papers 保守合并（§3.5 安全重跑核心）：
    - 目标缺失的行 → 整行新增（保留内部 ID）；
    - 目标已有的行 → 仅对目标为 NULL 的列补缺（绝不 UPDATE 已有非空值，
      用户修改/目标比源多的行不回滚）；
    - 补缺 title_zh/abstract_zh 时同步登记 provenance（无记录才登记）。
    返回统计 {inserted, skipped_existing, filled_null_columns, filled_cells}。"""
    src_cols = [r[1] for r in src.execute('PRAGMA table_info("papers")').fetchall()]
    dst_cols = [r[1] for r in dst.execute('PRAGMA table_info("papers")').fetchall()]
    dst_colset = set(dst_cols)
    use = [c for c in src_cols if c in dst_colset]
    non_id = [c for c in use if c != "id"]
    collist = ", ".join(f'"{c}"' for c in use)
    dst_ids = {r[0] for r in dst.execute("SELECT id FROM papers")}
    filled_cols: dict[str, int] = {}
    inserted = skipped = 0
    for row in src.execute(f'SELECT {collist} FROM "papers"').fetchall():
        rec = dict(zip(use, row))
        pid = rec["id"]
        if pid not in dst_ids:
            ph = ", ".join(["?"] * len(use))
            cur = dst.execute(
                f'INSERT OR IGNORE INTO "papers" ({collist}) VALUES ({ph})',
                [rec[c] for c in use])
            if cur.rowcount:
                inserted += 1
                _register_migrated_zh(dst, pid, rec)
            else:
                skipped += 1
            continue
        # 已有行：逐列比较，仅补目标为 NULL 的列
        dst_row = dst.execute(
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in non_id)} '
            f'FROM papers WHERE id=?', (pid,)).fetchone()
        dst_vals = dict(zip(non_id, dst_row))
        updates = {}
        for c in non_id:
            sv, dv = rec[c], dst_vals[c]
            if dv is None and sv is not None:
                updates[c] = sv
        if updates:
            sets = ", ".join(f'"{c}"=?' for c in updates)
            dst.execute(f'UPDATE papers SET {sets} WHERE id=?',
                        (*updates.values(), pid))
            for c in updates:
                filled_cols[c] = filled_cols.get(c, 0) + 1
            _register_migrated_zh(dst, pid, updates)
        else:
            skipped += 1
    return {"inserted": inserted, "skipped_existing": skipped,
            "filled_null_columns": filled_cols,
            "filled_cells": sum(filled_cols.values())}


def _register_migrated_zh(dst: sqlite3.Connection, paper_id: int, values: dict) -> int:
    """迁移补缺的中文字段登记来源：仅当 (paper_id, field) 无任何记录时登记
    origin=library_unknown_source、recorded_by='migration_v04'（§3.5）。
    绝不覆盖已有记录（尤其 library_human）。"""
    n = 0
    for field in ("title_zh", "abstract_zh"):
        text = values.get(field)
        if text and str(text).strip():
            cur = dst.execute(
                "INSERT OR IGNORE INTO content_provenance"
                "(paper_id, field, text, origin, source_hash, model, "
                " prompt_version, recorded_by) "
                "VALUES(?,?,?,'library_unknown_source',NULL,NULL,NULL,?)",
                (paper_id, field, str(text), MIGRATION_RECORDED_BY))
            n += 1 if cur.rowcount else 0
    return n


def collect_conflicts(src: sqlite3.Connection) -> dict:
    """扫描原库冲突（只列出，不修复、不合并）。"""
    conflicts: dict[str, list] = {"duplicate_doi": [], "duplicate_openalex_id": [],
                                  "papers_missing_title": [],
                                  "citation_orphan_endpoint": [],
                                  "collection_item_orphan": [],
                                  "duplicate_venue_name_norm": []}
    seen: dict = {}
    for pid, doi in src.execute("SELECT id, doi FROM papers WHERE doi IS NOT NULL"):
        nd = norm_doi(doi)
        if nd:
            seen.setdefault(nd, []).append(pid)
    for nd, ids in seen.items():
        if len(ids) > 1:
            conflicts["duplicate_doi"].append({"normalized_doi": nd, "paper_ids": ids})
    seen = {}
    for pid, oid in src.execute(
            "SELECT id, openalex_id FROM papers WHERE openalex_id IS NOT NULL"):
        no = norm_openalex_id(oid)
        if no:
            seen.setdefault(no, []).append(pid)
    for no, ids in seen.items():
        if len(ids) > 1:
            conflicts["duplicate_openalex_id"].append(
                {"normalized_openalex_id": no, "paper_ids": ids})
    for (pid,) in src.execute(
            "SELECT id FROM papers WHERE title IS NULL OR TRIM(title)=''"):
        conflicts["papers_missing_title"].append({"paper_id": pid})
    for fid, tid in src.execute("SELECT from_id, to_id FROM citations"):
        for role, val in (("from_id", fid), ("to_id", tid)):
            if not src.execute("SELECT 1 FROM papers WHERE id=?", (val,)).fetchone():
                conflicts["citation_orphan_endpoint"].append(
                    {"from_id": fid, "to_id": tid, "missing": role, "paper_id": val})
    for cid, pid in src.execute("SELECT collection_id, paper_id FROM collection_items"):
        miss = []
        if not src.execute("SELECT 1 FROM collections WHERE id=?", (cid,)).fetchone():
            miss.append("collection_id")
        if not src.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
            miss.append("paper_id")
        if miss:
            conflicts["collection_item_orphan"].append(
                {"collection_id": cid, "paper_id": pid, "missing": miss})
    seen = {}
    for vid, nn in src.execute(
            "SELECT id, name_norm FROM venues WHERE name_norm IS NOT NULL"):
        seen.setdefault(nn, []).append(vid)
    for nn, ids in seen.items():
        if len(ids) > 1:
            conflicts["duplicate_venue_name_norm"].append({"name_norm": nn, "venue_ids": ids})
    counts = {k: len(v) for k, v in conflicts.items()}
    return {"conflicts": conflicts, "conflict_counts": counts,
            "total": sum(counts.values())}


def seed_provenance(dst: sqlite3.Connection) -> dict:
    """为迁移过来的中文字段登记来源。原库未记录来源信息 → 统一
    library_unknown_source（不伪造模型/时间）；后续人工/AI 写入时更新。
    §3.5：已有记录（尤其 library_human）绝不覆盖 —— INSERT OR IGNORE，
    仅补 (paper_id, field) 无记录的缺口。"""
    rows = dst.execute(
        "SELECT id, title_zh, abstract_zh FROM papers "
        "WHERE (title_zh IS NOT NULL AND TRIM(title_zh) != '') "
        "   OR (abstract_zh IS NOT NULL AND TRIM(abstract_zh) != '')"
    ).fetchall()
    registered = skipped_existing = 0
    for pid, tzh, azh in rows:
        for field, text in (("title_zh", tzh), ("abstract_zh", azh)):
            if text and str(text).strip():
                cur = dst.execute(
                    "INSERT OR IGNORE INTO content_provenance"
                    "(paper_id, field, text, origin, source_hash, model, "
                    " prompt_version, recorded_by) "
                    "VALUES(?,?,?,?,NULL,NULL,NULL,?)",
                    (pid, field, text, "library_unknown_source",
                     MIGRATION_RECORDED_BY))
                if cur.rowcount:
                    registered += 1
                else:
                    skipped_existing += 1
    return {"registered": registered, "skipped_existing": skipped_existing}


def referential_integrity(dst: sqlite3.Connection) -> dict:
    pids = {r[0] for r in dst.execute("SELECT id FROM papers")}
    cids = {r[0] for r in dst.execute("SELECT id FROM collections")}
    edges = dst.execute("SELECT from_id, to_id FROM citations").fetchall()
    items = dst.execute("SELECT collection_id, paper_id FROM collection_items").fetchall()
    bad_edges = sum(1 for a, b in edges if a not in pids or b not in pids)
    bad_items = sum(1 for c, p in items if c not in cids or p not in pids)
    return {
        "citations_total": len(edges),
        "citations_invalid_endpoints": bad_edges,
        "collection_items_total": len(items),
        "collection_items_invalid": bad_items,
        "provenance_rows": dst.execute(
            "SELECT COUNT(*) FROM content_provenance").fetchone()[0],
        "provenance_origin_counts": {
            r[0]: r[1] for r in dst.execute(
                "SELECT origin, COUNT(*) FROM content_provenance "
                "GROUP BY origin")},
    }


def sample_check(src: sqlite3.Connection, dst: sqlite3.Connection,
                 n_each: int = 10, seed: int = 2026) -> list[dict]:
    """抽样内容核对（≥20 条）：逐列比较源/目标，只记录 id、布尔结果与
    内容哈希，不导出私有内容。样本 = 前 10 + 后 10 + 随机 10（固定种子）。"""
    ids = [r[0] for r in src.execute("SELECT id FROM papers ORDER BY id")]
    if not ids:
        return []
    head = ids[:n_each]
    tail = ids[-n_each:] if len(ids) > n_each else []
    mid = ids[n_each:-n_each] if len(ids) > 2 * n_each else []
    rng = random.Random(seed)
    rnd = rng.sample(mid, min(n_each, len(mid)))
    sample_ids = sorted(set(head) | set(tail) | set(rnd))
    cols = [r[1] for r in src.execute("PRAGMA table_info(papers)").fetchall()]
    src.row_factory = sqlite3.Row
    dst.row_factory = sqlite3.Row
    out = []
    for pid in sample_ids:
        s = src.execute(
            f'SELECT {", ".join(cols)} FROM papers WHERE id=?', (pid,)).fetchone()
        d = dst.execute(
            f'SELECT {", ".join(cols)} FROM papers WHERE id=?', (pid,)).fetchone()
        if s is None or d is None:
            out.append({"paper_id": pid, "equal": False, "error": "row missing"})
            continue
        mismatches = [c for c in cols if s[c] != d[c]]
        blob = json.dumps(list(s), ensure_ascii=False, default=str)
        out.append({"paper_id": pid,
                    "equal": not mismatches,
                    "mismatched_fields": mismatches,
                    "source_content_sha256":
                        hashlib.sha256(blob.encode("utf-8")).hexdigest()})
    return out


def table_counts(conn: sqlite3.Connection) -> dict:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in sorted(tables)}


def _snapshot_all(conn: sqlite3.Connection) -> dict:
    """目标库全部用户数据的逐表逐行快照（用于安全重跑 no-op 校验）。"""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    snap = {}
    for t in sorted(tables):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
        rows = conn.execute(
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} '
            f'FROM "{t}"').fetchall()
        snap[t] = [tuple(r) for r in rows]
    return snap


def papers_field_stats(conn: sqlite3.Connection) -> dict:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()]
    return {c: conn.execute(
        f'SELECT COUNT(*) FROM papers WHERE "{c}" IS NOT NULL '
        f'AND TRIM(CAST("{c}" AS TEXT)) != \'\'').fetchone()[0] for c in cols}


def migrate_once(src_path: Path, dst_path: Path) -> dict:
    """执行一遍迁移（§3.5 安全重跑语义）。

    - papers：目标已有行仅补 NULL 列，绝不覆盖已有非空值；目标缺失行整行
      新增；目标比源多的行/用户修改不回滚。
    - 其余表：INSERT OR IGNORE 保守合并。
    - content_provenance：已有记录不覆盖，仅补缺。
    - 冲突另列：返回值带 conflict_counts（完整清单由 run_migration 写入
      evidence/conflicts.json）。
    同一来源重复执行 = 对已补齐目标是安全 no-op。
    """
    src_path, dst_path = Path(src_path), Path(dst_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dst_path))
    try:
        dst.execute("PRAGMA foreign_keys=ON")
        create_library_schema(dst)
        copied = {}
        for t in COPY_TABLES:
            if t == "papers":
                copied[t] = _merge_papers(src, dst)
            else:
                copied[t] = _copy_table(src, dst, t)
        seeded = seed_provenance(dst)
        conflicts = collect_conflicts(src)
        dst.commit()
        return {"copied_rows": copied,
                "provenance_rows_seeded": seeded,
                "conflict_counts": conflicts["conflict_counts"],
                "rerun_semantics": "conservative_null_fill_v04"}
    finally:
        src.close()
        dst.close()


def run_migration(source: Path = DEFAULT_SOURCE, target: Path = DEFAULT_TARGET,
                  evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
                  idempotency: bool = True) -> dict:
    """完整迁移流程：迁移 + 统计 + 冲突 + 抽样核对 +（可选）幂等验证。"""
    source, target, evidence_dir = Path(source), Path(target), Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    first = migrate_once(source, target)

    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        conflicts = collect_conflicts(src)
        sample = sample_check(src, dst)
        stats = {
            "migration_source": str(source),
            "migration_target": str(target),
            "target_is_candidate": True,
            "run_1": first,
            "counts_after_run_1": table_counts(dst),
            "papers_field_nonempty_after": papers_field_stats(dst),
            "referential_integrity": referential_integrity(dst),
            "conflict_counts": conflicts["conflict_counts"],
            "sample_check": {"size": len(sample),
                             "all_equal": all(s.get("equal") for s in sample),
                             "samples": sample},
        }
        conflicts_path = evidence_dir / "conflicts.json"
        conflicts_path.write_text(json.dumps(conflicts, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

        idem = None
        if idempotency:
            # §3.5：二次迁移前快照目标全部用户数据，二次迁移后必须逐表逐行
            # 完全一致（安全 no-op：既不覆盖用户修改，也不改变任何已有值）。
            before = _snapshot_all(dst)
            second = migrate_once(source, target)
            counts_after_2 = table_counts(dst)
            idem = {
                "counts_after_run_2": counts_after_2,
                "counts_identical": counts_after_2 == stats["counts_after_run_1"],
                "target_unchanged_by_rerun": before == _snapshot_all(dst),
                "run_2": second,
            }
            stats["idempotency"] = idem
        (evidence_dir / "migration_stats.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        return stats
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="长期库迁移（原库快照 → wenxianluopan_library）")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--target", default=str(DEFAULT_TARGET))
    ap.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR))
    ap.add_argument("--no-idempotency", action="store_true")
    args = ap.parse_args()
    stats = run_migration(Path(args.source), Path(args.target), Path(args.evidence_dir),
                          idempotency=not args.no_idempotency)
    print(json.dumps({k: stats[k] for k in
                      ("counts_after_run_1", "conflict_counts", "referential_integrity",
                       "sample_check") if k in stats},
                     ensure_ascii=False, indent=2)[:4000])
    ok = (stats["sample_check"]["all_equal"] and stats["sample_check"]["size"] >= 20
          and (not stats.get("idempotency")
               or (stats["idempotency"]["counts_identical"]
                   and stats["idempotency"].get("target_unchanged_by_rerun", True))))
    print("MIGRATION OK" if ok else "MIGRATION CHECK FAILED")
    if not ok:
        raise SystemExit(1)
