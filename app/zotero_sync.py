"""Zotero 同步：持久化待办/重试队列 + 状态机 + 状态接口（§3.2 契约）。

V4R1（parallel_v04_r1，B 线）：
- V4R1-02：process_queue 调 update_item 改位置参数（item_key, patch, version），
  与 ZoteroClient 签名一致（修 unexpected keyword 'key'）；
- V4R1-03：回读实质核验——书目 itemType/key/DOI 归一化比对 + 写入字段
  核对；笔记 itemType/parentItem/应用标记/实际正文（按确定性可逆规范化规则
  重算内容哈希），正文错/错父项/错DOI/错类型/丢字段/回读失败 → 不同步；
- V4R1-04：崩溃窗口——远端已建而映射未落时，重试先按父项+应用标记+字段
  get_children 认领既有受控笔记，不盲 create；映射回读失败且查重未命中
  → pending 不盲建；
- map-first 强标识：复用映射条目前核验远端 DOI 与本地一致（normalize_doi），
  不一致 → 显式 conflict（mapped_item_doi_conflict），不写笔记不 synced；
- A 线接缝（总控修正）：选产物时经 artifact_validity 门控——stale 跳过并记
  原因（V4R1-05）；unverifiable 照写但笔记内如实标注"有效性未验证"（保留
  旧库/人工内容同步能力，绝不当作 verified）；kind 映射与依赖哈希传参见
  _validator_kind/_validity_gate 注释；
- V4R1-06：sync_one_paper 单篇同步入口（enqueue → 仅对该篇 flush）。

状态机（V4-04 扩展，§3.3）：
none（未入队）→ pending（待写/待重试）→ partial（书目已写而部分产物未完成）
→ synced | conflict。**synced 仅在「写入 + 回读核验（GET item/note key 比对
内容哈希）」通过后标记**；回读失败 → 保持 pending 并写 last_error；
书目成功而部分产物（中文题名/摘要、AI 四要素/总结等内容子笔记）未完成 →
partial。写请求被 Zotero 以冲突（409/412）拒绝 → conflict；Zotero 不可达/
缺凭据/网络失败 → 保持 pending 并记录 last_error，等下次 flush 重试
（队列持久化在应用库 zotero_sync_queue 表，重启后天然可重试）。

V4-04 内容子笔记（§3.3）：
- 书目条目保持 build_item_payload 原语义（对已有条目仅补缺失字段，绝不覆盖）；
- 翻译/四要素/总结/来源以子笔记写入，note 首行固定标识
  `<!-- wenxianluopan:<field>:<content_sha256_12> -->`，正文含来源哈希、模型、
  提示词版本、生成时间、校验状态；
- zotero_item_map 表记录 paper_id ↔ item/note key 与内容哈希（迁移/初始化
  函数建表，兼容旧库），同 note key 幂等更新，绝不触碰用户旧笔记；
- process_queue 结果按产物计数（biblio/notes/verified），不只报 synced=1。

队列持久化在应用库（schema 见下方 QUEUE_SCHEMA，与 app/library_migrate.py
B_SCHEMA 同款），绝不直接 SQL 写 zotero.sqlite。

§3.2 契约：GET /api/library/sync/status → 每条记录
{"paper_id": int, "local_saved": bool, "zotero_state": "none|pending|partial|synced|conflict",
 "last_error": str|null}。旧取值/形状不移除。RIS 导出与 Zotero 写入严格区分。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

try:
    from .zotero_client import (ZoteroAuthRequired, ZoteroClient, ZoteroConflict,
                                ZoteroError, ZoteroUnavailable, build_item_payload,
                                normalize_doi)
except ImportError:
    from zotero_client import (ZoteroAuthRequired, ZoteroClient, ZoteroConflict,
                               ZoteroError, ZoteroUnavailable, build_item_payload,
                               normalize_doi)

log = logging.getLogger("papernet.zotero_sync")

STATE_NONE = "none"
STATE_PENDING = "pending"
STATE_PARTIAL = "partial"
STATE_SYNCED = "synced"
STATE_CONFLICT = "conflict"
STATES = (STATE_NONE, STATE_PENDING, STATE_PARTIAL, STATE_SYNCED, STATE_CONFLICT)
OPEN_STATES = (STATE_PENDING, STATE_PARTIAL)  # 需要继续处理的未完成态

QUEUE_SCHEMA = """
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

# V4-04：paper_id ↔ zotero item/note key 映射（kind: 'item' | 'note:<field>'），
# 兼容旧库（CREATE IF NOT EXISTS，随 init_queue/初始化函数幂等创建）。
ITEM_MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS zotero_item_map (
    paper_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    zotero_key TEXT,
    content_hash TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (paper_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_zim_paper ON zotero_item_map(paper_id);
"""

NOTE_MARK_RE = re.compile(
    r"<!--\s*wenxianluopan:([A-Za-z0-9_]+):([0-9a-f]{12})\s*-->")

FIELD_LABELS = {
    "title_zh": "中文题名（翻译）",
    "abstract_zh": "中文摘要（译文）",
    "abstract_en": "英文摘要（概括）",
    "summary": "总结（不作为原文摘要）",
    "question": "研究问题",
    "method": "研究方法",
    "results": "主要结果",
    "contribution": "作者贡献",
}


def init_queue(conn: sqlite3.Connection):
    conn.executescript(QUEUE_SCHEMA)
    conn.executescript(ITEM_MAP_SCHEMA)


def enqueue(conn: sqlite3.Connection, paper_id: int,
            payload: dict | None = None) -> int:
    """登记同步待办（幂等：同一论文已有未完成记录（pending/partial/conflict）
    时更新负载，不产生重复队列项，返回同一 queue_id）。"""
    init_queue(conn)
    if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
        raise ValueError(f"论文 #{paper_id} 不存在于本地库，拒绝入队")
    row = conn.execute(
        "SELECT id, state FROM zotero_sync_queue WHERE paper_id=? ORDER BY id DESC",
        (paper_id,)).fetchone()
    pj = json.dumps(payload or {}, ensure_ascii=False)
    if row and row["state"] in (STATE_PENDING, STATE_PARTIAL, STATE_CONFLICT):
        conn.execute(
            "UPDATE zotero_sync_queue SET payload_json=?, state=?, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (pj, STATE_PENDING, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO zotero_sync_queue(paper_id, payload_json, state) "
        "VALUES(?,?,?)", (paper_id, pj, STATE_PENDING))
    return cur.lastrowid


def requeue(conn: sqlite3.Connection, queue_id: int) -> bool:
    """把 conflict/synced/partial 记录重新置为 pending（人工确认后重试）。"""
    cur = conn.execute(
        "UPDATE zotero_sync_queue SET state=?, last_error=NULL, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (STATE_PENDING, queue_id))
    return cur.rowcount == 1


def _paper_row(conn, paper_id: int):
    return conn.execute(
        "SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()


# ------------------------------------------------------------------ 映射表
def _map_get(conn, paper_id: int, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM zotero_item_map WHERE paper_id=? AND kind=?",
        (paper_id, kind)).fetchone()


def _map_put(conn, paper_id: int, kind: str, zotero_key: str | None,
             content_hash: str | None, verified: int | None):
    conn.execute(
        "INSERT INTO zotero_item_map(paper_id, kind, zotero_key, content_hash, "
        "verified, updated_at) VALUES(?,?,?,?,?,datetime('now','localtime')) "
        "ON CONFLICT(paper_id, kind) DO UPDATE SET zotero_key=excluded.zotero_key, "
        "content_hash=excluded.content_hash, verified=excluded.verified, "
        "updated_at=excluded.updated_at",
        (paper_id, kind, zotero_key, content_hash,
         0 if verified is None else int(verified)))


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


# ------------------------------------------------------------------ 产物
def content_hash12(text: str) -> str:
    """内容哈希（sha256 前 12 位），作为笔记标记与回读比对依据。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12]


def _content_artifacts(conn, paper_id: int, paper: dict) -> dict[str, dict]:
    """收集要写入内容子笔记的产物：中文题名/摘要（papers）→ 最新 ready
    AI brief（ai_briefs：题名/摘要译文、英文概括、四要素）→ 总结
    （content_provenance）。每个产物附来源哈希/模型/提示词版本/生成时间。
    """
    arts: dict[str, dict] = {}
    src_hash = None
    try:
        from .library_migrate import compute_source_hash
        src_hash = compute_source_hash(paper.get("title"), paper.get("abstract"))
    except Exception:  # noqa: BLE001  旧库缺该函数时不阻塞书目同步
        src_hash = None

    keys = set(paper.keys())
    for f in ("title_zh", "abstract_zh"):
        v = str(paper.get(f) or "").strip() if f in keys else ""
        if v:
            art = {"text": v, "source_hash": src_hash, "model": None,
                   "prompt_version": None, "generated_at": None,
                   "kind": "translation", "src": "papers"}
            # R2-01 同步侧元数据：存在对应 content_provenance 行时携带
            # 实际来源（origin）/模型/提示词版本/时间——已知 AI 生成的内容
            # 不得标注"library（非 AI 生成）"。有效性（stale 跳过/
            # unverifiable 标注）仍由 process_queue 的 _validity_gate
            # 统一判定，本处只如实携带元数据。
            if _table_exists(conn, "content_provenance"):
                try:
                    prow = conn.execute(
                        "SELECT origin, model, prompt_version, generated_at, "
                        "created_at FROM content_provenance "
                        "WHERE paper_id=? AND field=?", (paper_id, f)
                    ).fetchone()
                except sqlite3.OperationalError:
                    prow = None  # 旧库缺 generated_at 等列：按无记录处理
                if prow is not None:
                    art["origin"] = prow["origin"]
                    art["model"] = prow["model"]
                    art["prompt_version"] = prow["prompt_version"]
                    art["generated_at"] = prow["generated_at"] or \
                        prow["created_at"]
            arts[f] = art

    if _table_exists(conn, "ai_briefs"):
        row = conn.execute(
            "SELECT source_hash, request_model, response_model, prompt_version, "
            "brief_json, created_at FROM ai_briefs "
            "WHERE paper_id=? AND status='ready' ORDER BY id DESC LIMIT 1",
            (paper_id,)).fetchone()
        if row and row["brief_json"]:
            try:
                brief = json.loads(row["brief_json"]) or {}
            except (ValueError, TypeError):
                brief = {}
            model = row["response_model"] or row["request_model"]
            base = {"source_hash": row["source_hash"], "model": model,
                    "prompt_version": row["prompt_version"],
                    "generated_at": row["created_at"]}
            for f in ("title_zh", "abstract_zh", "abstract_en"):
                v = str(brief.get(f) or "").strip()
                if v and f not in arts:
                    arts[f] = {"text": v, "kind": "translation",
                               "src": "brief", **base}
            for name in ("question", "method", "results", "contribution"):
                el = (brief.get("elements") or {}).get(name) or {}
                zh = str(el.get("zh") or "").strip()
                if not zh:
                    continue
                text = zh
                if str(el.get("en") or "").strip():
                    text += "\n" + str(el["en"]).strip()
                if str(el.get("quote") or "").strip():
                    text += "\n原文依据: " + str(el["quote"]).strip()
                arts[name] = {"text": text, "kind": "element",
                              "src": "brief", **base}

    if _table_exists(conn, "content_provenance") and "summary" not in arts:
        row = conn.execute(
            "SELECT text, source_hash, model, prompt_version, created_at "
            "FROM content_provenance WHERE paper_id=? AND field='summary'",
            (paper_id,)).fetchone()
        if row and str(row["text"] or "").strip():
            arts["summary"] = {"text": str(row["text"]).strip(),
                               "source_hash": row["source_hash"],
                               "model": row["model"],
                               "prompt_version": row["prompt_version"],
                               "generated_at": row["created_at"],
                               "kind": "summary", "src": "provenance"}
    return arts


def build_note_html(field: str, artifact: dict) -> str:
    """内容子笔记 HTML：首行固定标识 + 正文 + 来源/模型/提示词版本/
    生成时间/校验状态（§3.3）。"""
    content = str(artifact.get("text") or "").strip()
    h12 = content_hash12(content)
    marker = f"<!-- wenxianluopan:{field}:{h12} -->"
    label = FIELD_LABELS.get(field, field)
    if artifact.get("validity") == "unverifiable":
        verify_state = ("有效性未验证（来源/依赖无法核实，历史内容如实同步；"
                        "未经核验，不得当作已验证内容）")
    else:
        verify_state = "回读核验：GET 本笔记 key，首行标记哈希与内容哈希一致"
    meta = [
        ("来源哈希", artifact.get("source_hash") or "-"),
        # R2-01：已知 AI 生成（provenance origin=ai_with_source）但模型
        # 未记录时如实标注，不得标"library（非 AI 生成）"
        ("模型", artifact.get("model")
         or ("AI 生成（模型未记录）"
             if artifact.get("origin") == "ai_with_source"
             else "library（非 AI 生成）")),
        ("提示词版本", artifact.get("prompt_version") or "-"),
        ("生成时间", artifact.get("generated_at") or "-"),
        ("校验状态", verify_state),
    ]
    meta_html = "".join(
        f"<li><b>{html.escape(k)}</b>: {html.escape(str(v))}</li>"
        for k, v in meta)
    return (f"{marker}\n"
            f"<p><b>文献罗盘 · {html.escape(label)}</b>（AI/翻译产物，"
            f"由文献罗盘维护；请勿手工编辑本块）</p>\n"
            f"<pre>{html.escape(content)}</pre>\n"
            f"<hr/>\n<ul>{meta_html}</ul>")


def parse_note_marker(note_text: str) -> tuple[str, str] | None:
    """从笔记首行解析 (field, hash12)；首行无标记时全文兜底查找。"""
    if not note_text:
        return None
    first = note_text.splitlines()[0]
    m = NOTE_MARK_RE.search(first) or NOTE_MARK_RE.search(note_text)
    return (m.group(1), m.group(2)) if m else None


# V4R1-03 回读实质核验：确定性规范化规则（确定且可逆——
# unescape(escape(text)) == text；规范化只做换行统一/首尾裁剪，不改内容）。
PRE_CONTENT_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)


def normalize_note_html(note_html: str) -> str:
    """笔记 HTML 的确定性规范化：统一换行符、去首尾空白（不删除核验依据）。"""
    s = str(note_html or "").replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


def extract_note_content(note_html: str) -> str | None:
    """从 build_note_html 生成的笔记中提取正文（<pre> 块内文本，HTML 实体
    还原）。规则确定且可逆：写入时 html.escape，读回 html.unescape，
    内容逐字节还原，可据此重算内容哈希做回读比对。"""
    m = PRE_CONTENT_RE.search(normalize_note_html(note_html))
    if not m:
        return None
    return html.unescape(m.group(1))


def _client_can_readback(client: Any) -> bool:
    return hasattr(client, "get_item") and callable(client.get_item)


def _verify_item(client: Any, item_key: str, expected_doi: str | None = None,
                 expect_fields: dict | None = None) -> tuple[bool, str | None]:
    """V4R1-03 书目回读实质核验：itemType/key/DOI（归一化比对）与
    expect_fields（本次写入字段是否真实落库，防「丢字段仍 synced」）。"""
    try:
        got = client.get_item(item_key)
    except ZoteroError as e:
        return False, f"回读失败: {e}"[:300]
    got = got or {}
    d = got.get("data") or {}
    if not (d.get("key") or got.get("key")):
        return False, "回读未取到条目"
    if (d.get("itemType") or "") in ("", "note", "attachment"):
        return False, (f"回读 itemType 异常（期望书目条目）: "
                       f"{d.get('itemType')!r}")
    if expected_doi:
        remote = normalize_doi(d.get("DOI"))
        if remote != expected_doi:
            return False, (f"回读 DOI 不一致: 期望 {expected_doi}, "
                           f"实际 {remote}")[:300]
    for k, v in (expect_fields or {}).items():
        cur = d.get(k)
        if k == "collections":
            if not set(v or []) <= set(cur or []):
                return False, f"回读丢字段: collections 缺少 {v}"[:300]
        elif (cur or "") != (v or ""):
            return False, f"回读字段不一致/丢失: {k}"[:300]
    return True, None


def _verify_note(client: Any, note_key: str, field: str, h12: str,
                 parent_key: str | None = None) -> tuple[bool, str | None]:
    """V4R1-03 笔记回读实质核验：itemType=note / parentItem / 应用标记 /
    实际正文（按确定性规范化规则重算内容哈希）。任何一项不符 → 核验失败。"""
    try:
        got = client.get_item(note_key)
    except ZoteroError as e:
        return False, f"回读失败: {e}"[:300]
    got = got or {}
    d = got.get("data") or {}
    if (d.get("itemType") or "") != "note":
        return False, (f"回读核验失败: itemType 非笔记 "
                       f"({d.get('itemType')!r})")[:300]
    if parent_key and (d.get("parentItem") or "") != parent_key:
        return False, (f"回读核验失败: parentItem 不一致 "
                       f"(期望 {parent_key}, 实际 {d.get('parentItem')!r})")[:300]
    note_text = d.get("note") or ""
    marker = parse_note_marker(note_text)
    if marker != (field, h12):
        return False, (f"回读核验失败: 首行标记与预期不一致 "
                       f"(期望 {field}:{h12}, 实际 {marker})")[:300]
    content = extract_note_content(note_text)
    if content is None:
        return False, "回读核验失败: 笔记缺少 <pre> 正文块（正文损坏）"[:300]
    actual = content_hash12(content)
    if actual != h12:
        return False, (f"回读核验失败: 正文哈希不一致 (期望 {h12}, "
                       f"实际 {actual})")[:300]
    return True, None


def _claim_orphan_note(client: Any, parent_key: str, field: str) -> str | None:
    """V4R1-04 崩溃窗口认领：远端已建子笔记而映射未落（响应丢失/重启）时，
    按父项 + 应用标识（wenxianluopan 标记）+ 字段名在 get_children 中找
    本应用维护的既有受控笔记，返回其 key。绝不触碰无标识的用户笔记。"""
    if not hasattr(client, "get_children") or not callable(client.get_children):
        return None
    try:
        children = client.get_children(parent_key)
    except ZoteroError:
        return None
    for ch in children or []:
        d = (ch.get("data") or {}) if isinstance(ch, dict) else {}
        if (d.get("itemType") or "") != "note":
            continue
        if (d.get("parentItem") or "") != parent_key:
            continue
        marker = parse_note_marker(d.get("note") or "")
        if marker and marker[0] == field:
            return ch.get("key")
    return None


def _validator_kind(field: str, art: dict) -> str:
    """把同步产物映射到 A 线 artifact_validity 支持的 kind。

    - summary → "summary"（content_provenance 字段产物）；
    - papers 列直出的 title_zh/abstract_zh → 字段名本身；
    - brief 行派生的译文/英文摘要/四要素 → "brief"（其有效性即所属
      brief 产物的有效性：同一输入依赖与模型/提示词版本）。
    """
    src = art.get("src")
    if art.get("kind") == "summary":
        return "summary"
    if src == "brief":
        return "brief"
    if field in ("title_zh", "abstract_zh"):
        return field
    return "brief"


def _validity_gate(conn: sqlite3.Connection, paper_id: int, kind: str,
                   content_hash: str | None) -> dict | None:
    """A 线接缝（parallel_v04_r1 §7）：library_source.artifact_validity(
    conn, paper_id, kind, content_hash) -> {"validity": "verified|unverifiable|
    stale", "reasons": [...]}。lane_B 未集成该实现（或不可用）时返回 None
    （不阻塞基线行为）；集成时以 A 线为准。返回非 verified → 跳过该产物。"""
    try:
        from .library_source import artifact_validity
    except ImportError:
        try:
            from library_source import artifact_validity  # 独立脚本场景
        except ImportError:
            return None
    try:
        res = artifact_validity(conn, paper_id, kind, content_hash=content_hash)
    except Exception as e:  # noqa: BLE001  校验器自身故障按不可核验处理
        return {"validity": "unverifiable",
                "reasons": [f"artifact_validity 调用失败: {e}"][:3]}
    if not isinstance(res, dict) or not res.get("validity"):
        return {"validity": "unverifiable",
                "reasons": ["artifact_validity 返回形状异常"]}
    return res


# ------------------------------------------------------------------ 主流程
def process_queue(conn: sqlite3.Connection, client: Any, limit: int = 50,
                  paper_id: int | None = None) -> dict:
    """处理 pending/partial 队列。client 需实现（duck-typing，测试可注入桩）：
    find_by_identifier(doi=, title=), create_items(items),
    update_item(key, patch, version)；支持 get_item 时进行回读核验。
    返回按产物计数的摘要（biblio/notes/verified 等）。本地库失败绝不静默：
    last_error 落库可见。"""
    init_queue(conn)
    where = "state IN ('pending','partial')"  # 未完成态都参与重试
    args: list = []
    if paper_id is not None:
        where += " AND paper_id=?"
        args.append(paper_id)
    rows = conn.execute(
        f"SELECT * FROM zotero_sync_queue WHERE {where} ORDER BY id LIMIT ?",
        (*args, limit)).fetchall()
    summary = {"scanned": len(rows), "synced": 0, "partial": 0, "conflict": 0,
               "stayed_pending": 0, "deduped": 0, "updated_existing": 0,
               "biblio": 0, "notes": 0, "notes_unchanged": 0, "verified": 0,
               "artifacts_skipped": 0}
    can_readback = _client_can_readback(client)
    for row in rows:
        qid = row["id"]
        pid = row["paper_id"]
        paper = _paper_row(conn, pid)
        if paper is None:
            conn.execute(
                "UPDATE zotero_sync_queue SET state=?, last_error=?, "
                "attempts=attempts+1, updated_at=datetime('now','localtime') "
                "WHERE id=?", (STATE_CONFLICT, "本地论文记录已不存在", qid))
            summary["conflict"] += 1
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
            collection_key = payload.get("collection_key")
            doi = paper["doi"]
            local_doi = normalize_doi(doi)
            # 总控接缝（真实联调实测 2026-10-21）：优先复用本应用此前写回的
            # 条目（zotero_item_map）。Zotero 本地搜索索引在写入后短暂滞后，
            # 仅靠 find_by_identifier 可能在索引就绪前重复建条。
            existing = None
            mapped_fetch_failed = False
            mapped_item = _map_get(conn, pid, "item")
            if mapped_item is not None and mapped_item["zotero_key"] \
                    and hasattr(client, "get_item"):
                try:
                    cur = client.get_item(mapped_item["zotero_key"])
                    if cur and ((cur.get("data") or {}).get("itemType")
                                not in (None, "note", "attachment")):
                        existing = cur
                    else:
                        mapped_fetch_failed = True
                except ZoteroError:
                    # V4R1（map-first 响应丢失/索引滞后窗口）：映射指向的条目
                    # 本次回读失败——远端可能已建（崩溃窗口）→ 不得盲建
                    mapped_fetch_failed = True
            if existing is None:
                try:
                    existing = client.find_by_identifier(
                        doi=doi, title=paper["title"])
                except ZoteroError as e:
                    # 查重失败绝不盲写（避免重复条目）：保持 pending 待重试
                    _mark(conn, qid, STATE_PENDING,
                          last_error=f"查重失败: {e}"[:500])
                    summary["stayed_pending"] += 1
                    continue
            if isinstance(existing, dict) and existing.get("conflict"):
                # V4-02：强标识冲突（题名相同但 DOI 不一致）→ 显式 conflict，
                # 绝不按题名复用/补写
                _mark(conn, qid, STATE_CONFLICT, last_error=(
                    f"身份冲突: {existing.get('reason')} "
                    f"requested_doi={existing.get('requested_doi')} "
                    f"found_doi={existing.get('found_doi')}")[:500])
                summary["conflict"] += 1
                continue
            if existing is None and mapped_fetch_failed:
                # V4R1-04：映射存在但远端回读失败且查重未命中——可能是响应
                # 丢失后条目已建/索引滞后，绝不盲建新条目，保持 pending
                _mark(conn, qid, STATE_PENDING, last_error=(
                    "映射条目回读失败且查重未命中（疑似崩溃窗口/索引滞后），"
                    "保持 pending 待重试，未盲建新条目")[:500])
                summary["stayed_pending"] += 1
                continue
            if existing is not None:
                # V4R1（map-first 强标识核验）：复用任何已有条目前，远端 DOI
                # 必须与本地归一化一致；不一致 → 显式 conflict，不写笔记
                remote_doi = normalize_doi((existing.get("data") or {}).get("DOI"))
                if local_doi and remote_doi and remote_doi != local_doi:
                    _mark(conn, qid, STATE_CONFLICT, last_error=(
                        f"mapped_item_doi_conflict: 映射/复用条目远端 DOI 与本地"
                        f"不一致 (本地 {local_doi}, 远端 {remote_doi}, "
                        f"item_key={existing.get('key')})")[:500])
                    summary["conflict"] += 1
                    continue
            # ---- 书目条目（原语义：仅补缺失字段，绝不覆盖）
            if existing is not None:
                item_key = existing.get("key")
                data = existing.get("data") or {}
                patch = {}
                if not (data.get("abstractNote") or "").strip() and \
                        (paper["abstract"] or "").strip():
                    patch["abstractNote"] = paper["abstract"]
                if not (data.get("title") or "").strip() and \
                        (paper["title"] or "").strip():
                    patch["title"] = paper["title"]
                if collection_key and collection_key not in (data.get("collections") or []):
                    patch["collections"] = list(data.get("collections") or []) + [collection_key]
                if patch:
                    # V4R1-02：update_item 的首参是 item_key（位置参数），
                    # 与 ZoteroClient.update_item(item_key, patch, version) 一致
                    client.update_item(item_key, patch,
                                       version=data.get("version"))
                    summary["updated_existing"] += 1
                else:
                    summary["deduped"] += 1
            else:
                resp = client.create_items(
                    [build_item_payload(dict(paper), collection_key)])
                item_key = (resp.get("success") or {}).get("0")
                if not item_key:
                    _mark(conn, qid, STATE_CONFLICT,
                          last_error=f"Zotero 写入被拒绝: {resp.get('failed')}"[:500])
                    summary["conflict"] += 1
                    continue
            summary["biblio"] += 1
            _map_put(conn, pid, "item", item_key, None, None)

            # ---- 书目回读核验（V4R1-03：itemType/key/DOI 归一化 + 写入字段）
            if can_readback:
                expect = dict(patch) if (existing is not None and patch) else None
                ok, err = _verify_item(client, item_key, expected_doi=local_doi,
                                       expect_fields=expect)
                if not ok:
                    _mark(conn, qid, STATE_PENDING, zotero_key=item_key,
                          last_error=f"条目回读核验失败: {err}"[:500])
                    summary["stayed_pending"] += 1
                    continue
                summary["verified"] += 1

            # ---- 内容子笔记（title_zh/abstract_zh/abstract_en/summary/四要素）
            arts = _content_artifacts(conn, pid, dict(paper))
            failed_fields: list[str] = []
            skipped_fields: list[str] = []
            unverified_fields: list[str] = []
            for field in sorted(arts):
                art = arts[field]
                h12 = content_hash12(art["text"])
                # A 线接缝（总控修正 2026-10-21）：
                # 1) kind 映射——A 的 artifact_validity 只接受
                #    brief/abstract/title_zh/abstract_zh/summary；
                #    papers 列直出的题名/摘要按字段名校验，brief 行派生的
                #    译文/四要素/英文摘要统一按其 brief 产物校验；
                # 2) content_hash 参数语义=调用方所持"依赖哈希"（source_hash），
                #    不是笔记内容哈希 h12（原实现传 h12 导致全量误判 stale）。
                # 策略：stale → 跳过并记原因（V4R1-05 核心要求）；
                # unverifiable → 照写但笔记内如实标注"有效性未验证"，
                # 绝不当作 verified（保留旧库/人工内容可同步的基线行为）。
                gate = _validity_gate(conn, pid,
                                      _validator_kind(field, art),
                                      art.get("source_hash"))
                g_validity = (gate or {}).get("validity")
                if g_validity == "stale":
                    skipped_fields.append(
                        f"{field}(stale: "
                        f"{'; '.join(gate.get('reasons') or [])})")
                    continue
                if g_validity == "unverifiable":
                    art = dict(art)
                    art["validity"] = "unverifiable"
                    unverified_fields.append(field)
                mapped = _map_get(conn, pid, f"note:{field}")
                if mapped and mapped["content_hash"] == h12 and mapped["verified"]:
                    # 同内容且此前回读已核验：幂等跳过，不重复写
                    summary["notes_unchanged"] += 1
                    continue
                note_html = build_note_html(field, art)
                try:
                    if mapped and mapped["zotero_key"]:
                        version = None
                        try:
                            cur = client.get_item(mapped["zotero_key"])
                            version = ((cur or {}).get("data") or {}).get("version")
                        except ZoteroError:
                            version = None
                        client.update_item(mapped["zotero_key"],
                                           {"note": note_html}, version=version)
                        note_key = mapped["zotero_key"]
                    else:
                        # V4R1-04 崩溃窗口：远端可能已建而映射未落（响应丢失/
                        # 重启）→ 先按父项+应用标记+字段认领既有受控笔记
                        claimed = _claim_orphan_note(client, item_key, field)
                        if claimed:
                            version = None
                            try:
                                cur = client.get_item(claimed)
                                version = ((cur or {}).get("data") or {}).get(
                                    "version")
                            except ZoteroError:
                                version = None
                            client.update_item(claimed, {"note": note_html},
                                               version=version)
                            note_key = claimed
                        else:
                            resp = client.create_items(
                                [{"itemType": "note", "parentItem": item_key,
                                  "note": note_html}])
                            note_key = (resp.get("success") or {}).get("0")
                    if not note_key:
                        failed_fields.append(f"{field}(写入被拒绝)")
                        continue
                except ZoteroConflict:
                    raise  # 版本冲突（409/412/428）→ 外层如实记 conflict
                except ZoteroError as e:
                    failed_fields.append(f"{field}({str(e)[:120]})")
                    continue
                summary["notes"] += 1
                _map_put(conn, pid, f"note:{field}", note_key, h12, 0)
                if can_readback:
                    ok, err = _verify_note(client, note_key, field, h12,
                                           parent_key=item_key)
                    _map_put(conn, pid, f"note:{field}", note_key, h12,
                             1 if ok else 0)
                    if ok:
                        summary["verified"] += 1
                    else:
                        failed_fields.append(f"{field}({err})")

            if skipped_fields:
                summary["artifacts_skipped"] += len(skipped_fields)
            if unverified_fields:
                summary["artifacts_unverified"] = \
                    summary.get("artifacts_unverified", 0) + len(unverified_fields)
            note_err_parts = []
            if skipped_fields:
                note_err_parts.append("跳过产物(已失效stale): "
                                      + ", ".join(skipped_fields))
            if unverified_fields:
                note_err_parts.append("未验证产物(如实标注写入): "
                                      + ", ".join(unverified_fields))
            if failed_fields:
                _mark(conn, qid, STATE_PARTIAL, zotero_key=item_key,
                      last_error=("部分产物未完成: "
                                  + ", ".join(failed_fields))[:500])
                summary["partial"] += 1
            else:
                note_err = ("; ".join(note_err_parts))[:500] \
                    if note_err_parts else None
                _mark(conn, qid, STATE_SYNCED, zotero_key=item_key,
                      last_error=note_err)
                summary["synced"] += 1
        except (ZoteroConflict, ZoteroAuthRequired, ZoteroUnavailable,
                ZoteroError) as e:
            state = STATE_CONFLICT if isinstance(e, ZoteroConflict) else STATE_PENDING
            _mark(conn, qid, state, last_error=str(e)[:500])
            summary["conflict" if state == STATE_CONFLICT else "stayed_pending"] += 1
        except Exception as e:  # noqa: BLE001  单条异常不中断整批，但绝不吞错
            _mark(conn, qid, STATE_PENDING, last_error=f"unexpected: {e}"[:500])
            summary["stayed_pending"] += 1
    conn.commit()
    return summary


def paper_id_of(row) -> int:
    return row["paper_id"]


def _mark(conn, qid: int, state: str, *, zotero_key: str | None = None,
          last_error: str | None = None):
    conn.execute(
        "UPDATE zotero_sync_queue SET state=?, "
        "zotero_key=COALESCE(?, zotero_key), last_error=?, attempts=attempts+1, "
        "updated_at=datetime('now','localtime') WHERE id=?",
        (state, zotero_key, last_error, qid))


def sync_status(conn: sqlite3.Connection, paper_id: int | None = None,
                state: str | None = None, limit: int = 500) -> dict:
    """§3.2 状态接口数据源。返回 {"records": [...], "summary": {...}}。"""
    init_queue(conn)
    where, args = [], []
    if paper_id is not None:
        where.append("p.id=?")
        args.append(paper_id)
    if state:
        if state == STATE_NONE:
            where.append("q.id IS NULL")
        else:
            where.append("q.state=?")
            args.append(state)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        """SELECT p.id AS paper_id,
                  (SELECT COUNT(*) FROM papers pp WHERE pp.id=p.id) > 0 AS local_saved,
                  COALESCE(q.state, 'none') AS zotero_state,
                  q.last_error AS last_error
           FROM papers p LEFT JOIN zotero_sync_queue q
                ON q.paper_id = p.id
                AND q.id = (SELECT MAX(id) FROM zotero_sync_queue
                            WHERE paper_id = p.id)
           """ + wsql + " ORDER BY p.id LIMIT ?",
        (*args, limit)).fetchall()
    records = [{"paper_id": r["paper_id"], "local_saved": bool(r["local_saved"]),
                "zotero_state": r["zotero_state"],
                "last_error": r["last_error"]} for r in rows]
    counts = {s: 0 for s in STATES}
    for (st, n) in conn.execute(
            "SELECT COALESCE(state, 'none'), COUNT(*) FROM zotero_sync_queue "
            "GROUP BY state"):
        counts[st] = n
    counts[STATE_NONE] = conn.execute(
        "SELECT COUNT(*) FROM papers p WHERE NOT EXISTS "
        "(SELECT 1 FROM zotero_sync_queue q WHERE q.paper_id=p.id)").fetchone()[0]
    return {"records": records, "summary": counts}


# ------------------------------------------------------------------ 路由
class EnqueueIn(BaseModel):
    paper_id: int
    collection_key: str | None = None


class FlushIn(BaseModel):
    """V4-03：POST /api/library/sync/flush 请求体（可省略）。"""
    paper_id: int | None = None
    limit: int = 20


router = APIRouter(prefix="/api/library/sync", tags=["library-sync"])


def _conn():
    from .db import get_db  # 冻结共享文件的隔离入口（经 config.DB_PATH）
    return get_db()


@router.get("/status")
def api_sync_status(paper_id: int | None = None, state: str | None = None,
                    limit: int = 500):
    if state and state not in STATES:
        raise HTTPException(400, f"非法状态: {state}（合法值 {STATES}）")
    with _conn() as conn:
        return sync_status(conn, paper_id=paper_id, state=state, limit=min(limit, 2000))


@router.post("/enqueue")
def api_sync_enqueue(body: EnqueueIn):
    with _conn() as conn:
        try:
            qid = enqueue(conn, body.paper_id,
                          {"collection_key": body.collection_key})
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"queued": True, "queue_id": qid, "state": STATE_PENDING}


@router.post("/flush")
def api_sync_flush(body: FlushIn | None = None):
    """V4-03：触发真实写回（只 flush 指定 paper 或全部 pending/partial）。

    返回按产物计数（biblio/notes/verified）与可见错误清单；写通道凭据
    未就绪时返回明确原因（队列保持 pending），绝不伪造“同步成功”。"""
    limit = max(1, min((body.limit if body and body.limit else 20), 200))
    only_pid = body.paper_id if body else None
    client = ZoteroClient()
    if not client.write_ready():
        return {
            "flushed": False,
            "biblio": 0, "notes": 0, "verified": 0,
            "reason": "Zotero 本地 API 写通道缺 API key 或服务不可达：需在 "
                      "Zotero 界面确认本地授权（POST /api/local/authorize）"
                      "或经环境变量 PAPERNET_ZOTERO_API_KEY 注入；队列保持 "
                      "pending，未写入。",
            "errors": [],
        }
    with _conn() as conn:
        result = process_queue(conn, client, limit=limit, paper_id=only_pid)
        err_sql = ("SELECT paper_id, state, last_error FROM zotero_sync_queue "
                   "WHERE last_error IS NOT NULL")
        err_args: list = []
        if only_pid is not None:
            err_sql += " AND paper_id=?"
            err_args.append(only_pid)
        err_sql += " ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 50"
        errors = [{"paper_id": r["paper_id"], "state": r["state"],
                   "last_error": r["last_error"]}
                  for r in conn.execute(err_sql, err_args).fetchall()]
    return {"flushed": True,
            "biblio": result["biblio"], "notes": result["notes"],
            "verified": result["verified"],
            "result": result, "errors": errors}


class SyncPaperIn(BaseModel):
    """V4R1-06：POST /api/library/sync/paper 请求体。"""
    paper_id: int
    collection_key: str | None = None


def sync_one_paper(conn: sqlite3.Connection, client: Any, paper_id: int,
                   collection_key: str | None = None) -> dict:
    """V4R1-06 单篇同步入口：单篇意图 → enqueue（队列真实存在，
    queue_count>0）→ 仅对该 paper flush。一次只同步一篇。

    - 无凭据/服务不可达：如实返回 flushed=False、zotero_state=pending，
      且队列真实存在（不伪造成功），附可操作提示；
    - 论文不存在：queued=False，不入队。
    """
    init_queue(conn)
    if conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone() is None:
        return {"queued": False, "queue_count": 0, "flushed": False,
                "paper_id": paper_id, "zotero_state": None,
                "reason": f"论文 #{paper_id} 不存在于本地库，未入队"}
    enqueue(conn, paper_id, {"collection_key": collection_key})
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM zotero_sync_queue WHERE paper_id=?",
        (paper_id,)).fetchone()["c"]
    if not client.write_ready():
        return {
            "queued": True, "queue_count": n, "flushed": False,
            "paper_id": paper_id, "zotero_state": STATE_PENDING,
            "result": None,
            "reason": "Zotero 本地 API 写通道缺 API key 或服务不可达：请在 "
                      "Zotero 界面确认本地授权（应用内发起 authorize，弹窗确认）"
                      "或经环境变量 PAPERNET_ZOTERO_API_KEY 注入后重试；"
                      "队列已登记（pending），未写入。",
        }
    result = process_queue(conn, client, paper_id=paper_id)
    row = conn.execute(
        "SELECT state, last_error FROM zotero_sync_queue WHERE paper_id=? "
        "ORDER BY id DESC LIMIT 1", (paper_id,)).fetchone()
    return {"queued": True, "queue_count": n, "flushed": True,
            "paper_id": paper_id,
            "zotero_state": row["state"] if row else None,
            "last_error": row["last_error"] if row else None,
            "result": result}


def init():
    """应用启动时确保队列表/映射表存在（main.py 已注册路由并调用）。"""
    with _conn() as conn:
        init_queue(conn)
        conn.commit()
