"""原库一致性快照（B 线 parallel_v03）。

只读红线：源库 papernet.db 仅允许通过 SQLite backup API 做一致性快照，
本模块以 `file:...?mode=ro` 只读方式打开源库并调用 `Connection.backup`，
绝不以可写方式打开源库，也不做文件级拷贝（避免读到中间状态）。

产出：
- evidence/library_snapshot/papernet_snapshot.db（快照库）
- evidence/library_snapshot/snapshot_report.json（快照 sha256、逐表计数、
  与验收盘点 local_library_inventory.json 的核对结果、全库 meta 键清单）
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

TRACK_ROOT = Path(__file__).resolve().parent.parent
# 源库路径可用环境变量 PAPERNET_SNAPSHOT_SOURCE 覆盖（总控交付接缝：
# 候选包不含私有路径；本机运行时经环境变量注入真实源库）。
DEFAULT_SOURCE = Path(os.environ.get(
    "PAPERNET_SNAPSHOT_SOURCE", "papernet.db"))
DEFAULT_TARGET = TRACK_ROOT / "evidence" / "library_snapshot" / "papernet_snapshot.db"
DEFAULT_EVIDENCE = TRACK_ROOT / "evidence" / "library_snapshot" / "snapshot_report.json"

# 验收盘点（local_library_inventory.json，sha256 398aeb5e...）中的逐表计数，
# 用于核对快照与盘点一致。
INVENTORY_COUNTS = {
    "papers": 15836,
    "citations": 13387,
    "collections": 57,
    "collection_items": 477,
    "venues": 2231,
}
INVENTORY_NONEMPTY = {
    "doi": 13508,
    "title": 15787,
    "abstract": 9578,
    "title_zh": 15782,
    "abstract_zh": 9573,
}
INVENTORY_META_ZOTERO_KEY = 394


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    for t in list_tables(conn):
        counts[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    return counts


def papers_field_stats(conn: sqlite3.Connection) -> dict:
    """papers 表逐字段非空计数（TEXT 字段排除空串，与盘点口径一致）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()]
    stats = {}
    for c in cols:
        n = conn.execute(
            f'SELECT COUNT(*) FROM papers WHERE "{c}" IS NOT NULL '
            f'AND TRIM(CAST("{c}" AS TEXT)) != \'\''
        ).fetchone()[0]
        stats[c] = n
    return stats


def meta_key_counts(conn: sqlite3.Connection) -> dict:
    """meta_json 全量键清单与出现次数（不做内容采样，避免导出私有内容）。"""
    keys: dict[str, int] = {}
    for (mj,) in conn.execute(
        "SELECT meta_json FROM papers WHERE meta_json IS NOT NULL AND meta_json != ''"
    ):
        try:
            m = json.loads(mj)
        except (ValueError, TypeError):
            keys["<invalid_json>"] = keys.get("<invalid_json>", 0) + 1
            continue
        if not isinstance(m, dict):
            keys["<non_dict_json>"] = keys.get("<non_dict_json>", 0) + 1
            continue
        for k in m:
            keys[k] = keys.get(k, 0) + 1
    return keys


def snapshot(source_db: Path = DEFAULT_SOURCE, target_db: Path = DEFAULT_TARGET,
             report_path: Path = DEFAULT_EVIDENCE) -> dict:
    """用 backup API 生成一致性快照并输出核对报告。"""
    source_db = Path(source_db)
    target_db = Path(target_db)
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if target_db.exists():
        target_db.unlink()

    src = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True, timeout=15)
    try:
        dst = sqlite3.connect(str(target_db))
        try:
            src.backup(dst)  # 一致性快照：处理在线库的页级一致拷贝
        finally:
            dst.close()
    finally:
        src.close()

    chk = sqlite3.connect(str(target_db))
    try:
        counts = table_counts(chk)
        field_stats = papers_field_stats(chk)
        meta_keys = meta_key_counts(chk)
        tables = list_tables(chk)
    finally:
        chk.close()

    report = {
        "source_db": str(source_db),
        "source_size": source_db.stat().st_size,
        "snapshot_db": str(target_db),
        "snapshot_size": target_db.stat().st_size,
        "snapshot_sha256": sha256_file(target_db),
        "method": "sqlite3.Connection.backup (backup API, source opened read-only)",
        "tables": tables,
        "counts": counts,
        "papers_field_nonempty": field_stats,
        "papers_meta_key_counts": meta_keys,
        "check_against_inventory": {
            "counts_match": {k: counts.get(k) == v for k, v in INVENTORY_COUNTS.items()},
            "nonempty_match": {k: field_stats.get(k) == v
                               for k, v in INVENTORY_NONEMPTY.items()},
            "zotero_key_count_match": meta_keys.get("zotero_key", 0) == INVENTORY_META_ZOTERO_KEY,
        },
    }
    report["all_checks_pass"] = all(
        report["check_against_inventory"]["counts_match"].values()
    ) and all(
        report["check_against_inventory"]["nonempty_match"].values()
    ) and report["check_against_inventory"]["zotero_key_count_match"]

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return report


if __name__ == "__main__":
    rep = snapshot()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if not rep["all_checks_pass"]:
        raise SystemExit("snapshot check FAILED")
    print("SNAPSHOT OK")
