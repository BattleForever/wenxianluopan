"""字段级读取顺序服务（B 线 parallel_v03，§3.1 契约实现）。

读取顺序：长期库 → Zotero（本地只读 API）→ 网络权威书目/公开来源 →
有来源条件下的 AI 翻译/总结（仅挂钩，A 线经 register_ai_provider 接入）。

origin 语义（严格）：
- library_human          人工译文/总结（来源表明确记录为人工）
- library_unknown_source 已有内容，来源信息未记录（不伪造模型/时间）
- ai_with_source         记录了来源哈希等审计元数据的 AI 内容（读取时经
                          artifact_validity 同一判定：verified/unverifiable
                          可作当前内容展示；stale 时保留 AI 身份与失效原因，
                          仅供历史展示，绝不降级为来源未知后冒充当前内容）
- zotero                 字段值取自 Zotero（原文语种，非翻译）
- library/zotero/network 原文 title/abstract 的取值位置
- None                   缺失（明确缺失，绝不从模型常识编造）

网络失败 / Zotero 不可达时保留已有内容并在 errors 中说明，不抛出中断。
Zotero 查询走本地只读 API（app/zotero_client.py），禁止 SQL 写 zotero.sqlite。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from .library_migrate import compute_source_hash

ZH_FIELDS = ("title_zh", "abstract_zh")
AI_FIELDS = ("title_zh", "abstract_zh", "summary")

# A 线挂钩：field -> callable(paper: dict, source_hash: str) -> str | None
# 仅在字段缺失且存在来源（原文题名+摘要）时被调用。
_AI_PROVIDERS: dict[str, Callable[..., str | None]] = {}


def register_ai_provider(field: str, fn: Callable[..., str | None]) -> None:
    """注册 AI 字段提供者（A 线接入点）。fn(paper, source_hash) -> 文本或 None。"""
    if field not in AI_FIELDS:
        raise ValueError(f"不支持的 AI 字段: {field}（合法值 {AI_FIELDS}）")
    _AI_PROVIDERS[field] = fn


def unregister_ai_provider(field: str) -> None:
    _AI_PROVIDERS.pop(field, None)


def _open_db(db_path_or_conn) -> sqlite3.Connection:
    if isinstance(db_path_or_conn, sqlite3.Connection):
        return db_path_or_conn
    path = str(db_path_or_conn)
    if path.startswith("file:"):
        return sqlite3.connect(path, uri=True)
    return sqlite3.connect(path)


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone() is not None


def _get_setting(conn, key: str):
    if not _table_exists(conn, "settings"):
        return None
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _provenance_map(conn, paper_id: int) -> dict:
    if not _table_exists(conn, "content_provenance"):
        return {}
    rows = conn.execute(
        "SELECT field, text, origin, source_hash, model, prompt_version, "
        "recorded_by, created_at FROM content_provenance WHERE paper_id=?",
        (paper_id,)).fetchall()
    return {r[0]: dict(zip(("text", "origin", "source_hash", "model",
                            "prompt_version", "recorded_by", "created_at"),
                           r[1:])) for r in rows}


def _zotero_lookup(paper: dict) -> dict | None:
    """Zotero 只读查找：优先 meta 里的 zotero_key，其次 DOI 检索。
    返回 {"zotero_key", "title", "abstract"} 或 None；不可达返回 None
    并由调用方在 errors 里记录原因。"""
    from .zotero_client import ZoteroError, ZoteroClient
    client = ZoteroClient()
    try:
        client.ping()
    except ZoteroError as e:
        raise ZoteroLookupError(f"Zotero 不可达: {e}")
    meta = paper.get("meta") or {}
    key = meta.get("zotero_key")
    try:
        if key:
            item = client.get_item(str(key))
            if item and item.get("data"):
                d = item["data"]
                return {"zotero_key": key, "title": d.get("title"),
                        "abstract": d.get("abstractNote")}
        if paper.get("doi"):
            found = client.find_by_identifier(doi=paper["doi"])
            if found:
                d = found.get("data") or {}
                return {"zotero_key": found.get("key"),
                        "title": d.get("title"),
                        "abstract": d.get("abstractNote")}
    except ZoteroError as e:
        raise ZoteroLookupError(f"Zotero 查询失败: {e}")
    return None


class ZoteroLookupError(Exception):
    """Zotero 侧不可用/失败（保留已有内容，不中断）。"""


def _network_lookup(paper: dict, conn: sqlite3.Connection) -> dict | None:
    """网络权威书目（Crossref works API，白名单内 https）。仅补缺失的
    原文题名/摘要；失败返回 None，不编造。"""
    doi = paper.get("doi")
    if not doi:
        return None
    if (_get_setting(conn, "offline_mode") or "0") == "1":
        raise ZoteroLookupError("offline_mode=1，跳过网络补全")
    import requests
    try:
        resp = requests.get(f"https://api.crossref.org/works/{doi}",
                            timeout=(5, 15),
                            headers={"User-Agent": "PaperNet/0.1 (mailto:local)"})
        if resp.status_code != 200:
            raise ZoteroLookupError(f"Crossref {doi} -> HTTP {resp.status_code}")
        msg = (resp.json() or {}).get("message") or {}
        return {"title": (msg.get("title") or [None])[0],
                "abstract": msg.get("abstract")}
    except ZoteroLookupError:
        raise
    except Exception as e:
        raise ZoteroLookupError(f"网络书目查询失败: {e}")


def get_existing_content(db_path_or_conn, paper_id: int, *,
                         zotero: bool = True, network: bool = False,
                         ai: bool = False) -> dict | None:
    """§3.1 契约接口。返回 dict（见下）或 None（论文不存在）。

    返回结构（契约键 + 补充键，补充键见交付说明）：
    {
      "paper_id": int,
      "title_zh":     {"text": str|None, "origin": "library_human"|
                       "library_unknown_source"|"ai_with_source"|None},
      "abstract_zh":  {...同上...},
      "summary":      {...同上...},
      "title":    {"text": str|None, "origin": "library"|"zotero"|"network"|None},
      "abstract": {"text": str|None, "origin": "library"|"zotero"|"network"|None},
      "source_hash": str|None,      # 当前（题名+摘要规范化）来源哈希
      "stale_fields": [...],        # 经统一有效性判定为 stale 的 ai_with_source 字段
      "provenance": {field: {...}}, # 来源表原始记录（诊断用）
      "errors": [...],              # Zotero/网络失败说明（保留已有内容）
    }
    """
    conn = _open_db(db_path_or_conn)
    own = not isinstance(db_path_or_conn, sqlite3.Connection)
    try:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        row = cur.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if row is None:
            return None
        cols = row.keys()
        paper = {c: row[c] for c in cols}
        if "meta_json" in paper:
            try:
                paper["meta"] = json.loads(paper.get("meta_json") or "{}")
            except (ValueError, TypeError):
                paper["meta"] = {}
        errors: list[str] = []
        stale: list[str] = []
        prov = _provenance_map(conn, paper_id)
        source_hash = compute_source_hash(paper.get("title"), paper.get("abstract"))

        # ---- 原文字段：长期库 → Zotero → 网络
        out: dict[str, Any] = {"paper_id": paper_id}
        zotero_data = None
        network_data = None
        need_raw = any(not (paper.get(f) or "").strip() for f in ("title", "abstract"))
        if need_raw and zotero and \
                (_get_setting(conn, "zotero_enabled") in (None, "1")):
            try:
                zotero_data = _zotero_lookup(paper)
            except ZoteroLookupError as e:
                errors.append(str(e))
        if need_raw and network:
            try:
                network_data = _network_lookup(paper, conn)
            except ZoteroLookupError as e:
                errors.append(str(e))

        for field, sources in (
                ("title", (("library", paper.get("title")),
                           ("zotero", (zotero_data or {}).get("title")),
                           ("network", (network_data or {}).get("title")))),
                ("abstract", (("library", paper.get("abstract")),
                              ("zotero", (zotero_data or {}).get("abstract")),
                              ("network", (network_data or {}).get("abstract"))))):
            text, origin = None, None
            for org, val in sources:
                if val and str(val).strip():
                    text, origin = val, org
                    break
            out[field] = {"text": text, "origin": origin}

        # ---- 中文/总结字段：长期库（来源表）→ AI 挂钩
        # R2-01：读取与统一有效性判定（artifact_validity）共用
        # _field_artifact_validity 的同一判定结果。
        for field in AI_FIELDS:
            p = prov.get(field)
            text = None
            if field in ZH_FIELDS:
                val = paper.get(field)
                text = val if (val and str(val).strip()) else None
            if p and p.get("text") and str(p["text"]).strip() and text is None:
                text = p["text"]
            origin = None
            entry: dict[str, Any] = {}
            if text is not None:
                if p and p.get("origin") == "library_human":
                    # 人工内容保留（R2-01）：不因来源重算/配置变化失效，
                    # 不参与 stale 排除，绝不被删除/覆盖。
                    origin = "library_human"
                elif p and p.get("origin") == "ai_with_source":
                    vres = _field_artifact_validity(cur, paper_id, field,
                                                    None)
                    if vres.get("validity") == "stale":
                        # 已知 stale 的 AI 内容保持其身份（R2-01）：
                        # origin 仍为 ai_with_source，附失效原因仅供
                        # 历史展示；绝不降级为 library_unknown_source
                        # 伪装成未知内容复用。
                        origin = "ai_with_source"
                        entry["validity"] = "stale"
                        entry["stale_reasons"] = \
                            list(vres.get("reasons") or [])
                        stale.append(field)
                    else:
                        origin = "ai_with_source"
                else:
                    origin = "library_unknown_source"
            entry["text"] = text
            entry["origin"] = origin
            out[field] = entry

        # ---- AI 挂钩（仅缺失字段 + 有来源条件；V4R1-01 统一闸门）
        if ai:
            src_title = out["title"]["text"]
            src_abstract = out["abstract"]["text"]
            if not (src_title or src_abstract):
                errors.append("无来源（题名/摘要均缺失），AI 挂钩未触发")
            elif (_get_setting(conn, "offline_mode") or "0") == "1":
                # V4R1-01：离线时 AI 挂钩一律不触发（纵深防御之一层，
                # 最底层闸门在 ai_brief.call_llm），零模型请求、明确记录。
                errors.append("offline_mode=1，AI 生成挂钩未触发"
                              "（离线模式零模型请求）")
            else:
                contract_map: dict[str, str] = \
                    out.setdefault("ai_provider_contract", {})
                for field in AI_FIELDS:
                    if out[field]["text"] is None:
                        fn = _AI_PROVIDERS.get(field)
                        if fn is None:
                            continue
                        try:
                            generated = fn(paper, source_hash)
                        except Exception as e:
                            errors.append(f"AI provider[{field}] 失败: {e}")
                            continue
                        text_val, meta = generated, None
                        if isinstance(generated, dict):
                            # V4R1 审计契约：提供者可返回带元数据的 dict
                            text_val = generated.get("text")
                            meta = {k: generated[k] for k in
                                    ("model", "response_model", "prompt_version",
                                     "input_snapshot", "generated_at")
                                    if generated.get(k) is not None}
                        if text_val and str(text_val).strip():
                            entry: dict[str, Any] = {"text": str(text_val),
                                                     "origin": "ai_with_source"}
                            if isinstance(generated, dict):
                                if meta:
                                    entry.update(meta)
                                contract_map[field] = "v04r1_dict"
                            else:
                                # 旧式 str 提供者（历史契约）：元数据未知，
                                # 由 enrich 如实记 NULL，不补造、不拒绝
                                contract_map[field] = "legacy_str"
                            out[field] = entry

        out["source_hash"] = source_hash
        out["stale_fields"] = stale
        out["provenance"] = prov
        out["errors"] = errors
        return out
    finally:
        if own:
            conn.close()


def _ensure_provenance_columns(conn) -> None:
    """content_provenance 建表/补列（V4R1 审计元数据：input_snapshot/
    generated_at；旧库 ALTER 补齐，已存在时忽略）。"""
    if not _table_exists(conn, "content_provenance"):
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS content_provenance ("
            "paper_id INTEGER NOT NULL, field TEXT NOT NULL, text TEXT, "
            "origin TEXT NOT NULL, source_hash TEXT, model TEXT, "
            "prompt_version TEXT, recorded_by TEXT, input_snapshot TEXT, "
            "generated_at TEXT, "
            "created_at TEXT DEFAULT (datetime('now','localtime')), "
            "updated_at TEXT, PRIMARY KEY (paper_id, field))")
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(content_provenance)")}
    for col in ("input_snapshot", "generated_at"):
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE content_provenance ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass


def record_ai_content(db_path_or_conn, paper_id: int, field: str, text: str,
                      source_hash: str, model: str | None = None,
                      prompt_version: str | None = None,
                      input_snapshot: str | None = None,
                      generated_at: str | None = None) -> None:
    """A 线生成内容落库入口：登记 ai_with_source 来源（含来源哈希与
    可审计元数据）。调用方负责在写回前重核当前输入哈希（§3.3 有效性
    语义由 A 定义）。model/prompt_version/时间缺失时如实记 NULL（未知），
    绝不补造。"""
    if field not in AI_FIELDS:
        raise ValueError(f"不支持的 AI 字段: {field}")
    conn = _open_db(db_path_or_conn)
    own = not isinstance(db_path_or_conn, sqlite3.Connection)
    try:
        _ensure_provenance_columns(conn)
        conn.execute(
            "INSERT INTO content_provenance"
            "(paper_id, field, text, origin, source_hash, model, prompt_version, "
            " recorded_by, input_snapshot, generated_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(paper_id, field) DO UPDATE SET text=excluded.text, "
            "origin=excluded.origin, source_hash=excluded.source_hash, "
            "model=excluded.model, prompt_version=excluded.prompt_version, "
            "input_snapshot=excluded.input_snapshot, "
            "generated_at=excluded.generated_at, updated_at=excluded.updated_at",
            (paper_id, field, text, "ai_with_source", source_hash, model,
             prompt_version, "ai_module", input_snapshot, generated_at))
        conn.commit()
    finally:
        if own:
            conn.close()


# ------------------------------------------------------------------ 统一有效性判定（V4R1 冻结接口）

def artifact_validity(conn, paper_id: int, kind: str,
                      content_hash: str | None = None) -> dict:
    """冻结契约（parallel_v04_r1 A→B，详见 lane_A/contract_v04r1.md）。

    判定某个已保存产物当前是否可作为"当前内容"（供已有内容复用 / AI 面板 /
    同步产物选择共用）。B 线经此接口决定哪些产物可同步，不盲取最新 ready。

    kind:
      - "brief" / "abstract"      → AI 速览产物（ai_briefs 最新 ready 行）
      - "title_zh"/"abstract_zh"/"summary" → 字段级产物（content_provenance 行）

    返回 {"validity": "verified|unverifiable|stale",
          "reasons": [str, ...],          # 只含内容性原因（依赖不匹配）
          "content_hash": str|None}       # 产物记录的内容依赖哈希（未知为 None）

    语义：
    - verified     输入依赖（原文/题名/提示词/模型）与当前一致，可作当前内容；
    - unverifiable 无法核实（产物不存在 / 来源未记录 / 模型配置不可读 /
      缺模型元数据）：历史仍可读，但不得当作"已验证有效"；
    - stale        依赖已变化（source_changed / title_changed /
      prompt_version_changed / request_model_changed / content_hash_mismatch）：
      保留原输入历史与失效原因，不标当前有效、不自动复用、不重标来源未知。
    - origin=library_human 的人工内容默认保留（verified），不因来源重算失效。
    - content_hash 参数 = 调用方所持依赖哈希；与产物记录不一致 →
      stale("content_hash_mismatch")（记录了哈希才比较）。
    """
    c = _open_db(conn)
    own = not isinstance(conn, sqlite3.Connection)
    try:
        cur = c.cursor()
        cur.row_factory = sqlite3.Row
        if kind in ("brief", "abstract"):
            return _brief_artifact_validity(cur, paper_id, content_hash)
        if kind in AI_FIELDS:
            return _field_artifact_validity(cur, paper_id, kind, content_hash)
        raise ValueError(f"不支持的产物 kind: {kind}")
    finally:
        if own:
            c.close()


def _brief_artifact_validity(cur, paper_id: int,
                             content_hash: str | None) -> dict:
    try:
        from . import ai_brief
    except Exception:  # noqa: BLE001
        return {"validity": "unverifiable",
                "reasons": ["ai_brief_module_unavailable"], "content_hash": None}
    try:
        row = cur.execute(
            "SELECT * FROM ai_briefs WHERE paper_id=? AND source_kind='abstract' "
            "AND status='ready' ORDER BY id DESC LIMIT 1",
            (paper_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return {"validity": "unverifiable", "reasons": ["not_generated"],
                "content_hash": None}
    prow = cur.execute("SELECT title, abstract FROM papers WHERE id=?",
                       (paper_id,)).fetchone()
    abstract_norm = ai_brief.normalize_source((prow["abstract"] if prow else "") or "")
    title_norm = ai_brief.normalize_source((prow["title"] if prow else "") or "")
    try:
        cfg = ai_brief.load_ai_config()
    except Exception:  # noqa: BLE001
        cfg = None
    reasons = list(ai_brief._validity_reasons(
        row, abstract_norm, title_norm,
        cfg["request_model"] if cfg else None))
    if content_hash is not None and content_hash != row["source_hash"]:
        reasons.append("content_hash_mismatch")
    content_reasons = [r for r in reasons if r != "request_model_unverifiable"]
    if content_reasons:
        validity = "stale"
    elif reasons:
        validity = "unverifiable"
    else:
        validity = "verified"
    return {"validity": validity, "reasons": reasons,
            "content_hash": row["source_hash"]}


def _field_artifact_validity(cur, paper_id: int, field: str,
                             content_hash: str | None) -> dict:
    try:
        from . import ai_brief
        enrich_field_prompt_version = ai_brief.ENRICH_FIELD_PROMPT_VERSION
        metadata_fill_prompt_version = ai_brief.METADATA_FILL_PROMPT_VERSION
    except Exception:  # noqa: BLE001
        enrich_field_prompt_version = "enrich_field_v1"
        metadata_fill_prompt_version = "metadata_fill_v1"
    if not _table_exists(cur, "content_provenance"):
        return {"validity": "unverifiable", "reasons": ["not_recorded"],
                "content_hash": None}
    row = cur.execute(
        "SELECT text, origin, source_hash, model, prompt_version "
        "FROM content_provenance WHERE paper_id=? AND field=?",
        (paper_id, field)).fetchone()
    if row is None or not (row["text"] or "").strip():
        return {"validity": "unverifiable", "reasons": ["not_recorded"],
                "content_hash": None}
    origin = row["origin"]
    recorded_hash = row["source_hash"]
    if origin == "library_human":
        # 人工内容默认保留：不因来源重算/配置变化失效
        reasons = []
        if recorded_hash and content_hash is not None and \
                content_hash != recorded_hash:
            reasons.append("content_hash_mismatch")
        return {"validity": "stale" if reasons else "verified",
                "reasons": reasons, "content_hash": recorded_hash}
    if origin != "ai_with_source":
        return {"validity": "unverifiable", "reasons": ["origin_unrecorded"],
                "content_hash": recorded_hash}
    # ai_with_source：逐项核对内容依赖
    content_reasons: list[str] = []
    if not recorded_hash:
        return {"validity": "unverifiable", "reasons": ["source_hash_unrecorded"],
                "content_hash": None}
    prow = cur.execute("SELECT title, abstract FROM papers WHERE id=?",
                       (paper_id,)).fetchone()
    current_hash = compute_source_hash(prow["title"] if prow else None,
                                       prow["abstract"] if prow else None)
    if recorded_hash != current_hash:
        content_reasons.append("source_changed")
    recorded_pv = row["prompt_version"]
    accepted_versions={enrich_field_prompt_version,metadata_fill_prompt_version}
    if recorded_pv is not None and recorded_pv not in accepted_versions:
        content_reasons.append("prompt_version_changed")
    try:
        from . import ai_brief as _ab
        cfg = _ab.load_ai_config()
    except Exception:  # noqa: BLE001
        cfg = None
    if row["model"] and cfg is not None and \
            row["model"] != cfg["request_model"]:
        content_reasons.append("request_model_changed")
    if content_hash is not None and content_hash != recorded_hash:
        content_reasons.append("content_hash_mismatch")
    if content_reasons:
        return {"validity": "stale", "reasons": content_reasons,
                "content_hash": recorded_hash}
    # 信息性「无法核实」：元数据/配置未知 → unverifiable（如实未知，不补造）
    unverifiable_reasons: list[str] = []
    if recorded_pv is None:
        unverifiable_reasons.append("prompt_version_unrecorded")
    if not row["model"]:
        unverifiable_reasons.append("model_unrecorded")
    elif cfg is None:
        unverifiable_reasons.append("request_model_unverifiable")
    if unverifiable_reasons:
        return {"validity": "unverifiable", "reasons": unverifiable_reasons,
                "content_hash": recorded_hash}
    return {"validity": "verified", "reasons": [],
            "content_hash": recorded_hash}
