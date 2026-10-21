"""字段补全流水线（A 线 parallel_v04，接口与分工 §3.1/§3.2）。

核心函数：
    enrich_paper(conn, paper_id, *, zotero=True, network=True, ai=False)

按字段判定缺口（title/abstract 原文、title_zh/abstract_zh、summary/四要素
归纳为 summary），补全顺序 = 本地已有 → Zotero（启用时）→ 有来源网络书目
（白名单/离线开关/超时/预算/身份冲突保护）→ AI（仅当有原文依据且 ai=True）。

安全与来源语义：
- 已有人工/未知来源内容 → skipped：不覆盖、不重复生成；
- 补齐即持久化：papers 列更新（或 summary 登记 content_provenance）+
  content_provenance 登记 origin/recorded_by/source_hash（INSERT OR IGNORE，
  绝不覆盖已有记录）；
- 持久化后重复调用不得再次外呼（原文列已填 → 不再触发 Zotero/网络）；
- 补齐后经 zotero_sync.enqueue 登记同步待办（reason="enrich"）；
  zotero_enabled=0 时记录明确"未启用"状态而非静默（§3.2）。
- Zotero 五种错误分别上报：zotero_not_enabled / zotero_unreachable /
  zotero_not_found / zotero_auth_missing / zotero_config_invalid。
- 网络/AI 错误按契约错误码：network_disabled / network_failed /
  no_source_for_ai / ai_disabled。
- 身份保护：DOI 归一化后精确比较；请求 DOI 与命中条目 DOI 不同 → 不复用
  （按 zotero_not_found 上报冲突说明，绝不按题名补写）。

不发起任何未经调用方允许的真实联网/模型调用；网络与 AI 阶段均可被
调用方参数关闭。本模块只写应用库（papers/content_provenance/content_extra/
zotero_sync_queue），绝不直接写 zotero.sqlite。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from .library_migrate import compute_source_hash, norm_doi

log = logging.getLogger("papernet.enrich")

# 契约 3.1 字段集（备注/附件路径不在自动补全范围）
RAW_FIELDS = ("title", "abstract")          # 原文（Zotero/网络可补）
ZH_FIELDS = ("title_zh", "abstract_zh")     # 中文（AI 可补，需原文依据）
AI_FIELDS = ("title_zh", "abstract_zh", "summary")
FIELDS = ("title", "title_zh", "abstract", "abstract_zh", "summary")

RECORDED_BY = "enrich_v04"

ZOTERO_ERROR_CODES = ("zotero_not_enabled", "zotero_unreachable",
                      "zotero_not_found", "zotero_auth_missing",
                      "zotero_config_invalid")


# ------------------------------------------------------------------ 工具

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
        "SELECT field, text, origin, source_hash FROM content_provenance "
        "WHERE paper_id=?", (paper_id,)).fetchall()
    return {r[0]: {"text": r[1], "origin": r[2], "source_hash": r[3]}
            for r in rows}


def _load_paper(conn, paper_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        return None
    paper = {k: row[k] for k in row.keys()}
    try:
        paper["meta"] = json.loads(paper.get("meta_json") or "{}")
    except (ValueError, TypeError):
        paper["meta"] = {}
    return paper


def _has_text(v) -> bool:
    return bool(v and str(v).strip())


def _field_present(conn, paper: dict, prov: dict, field: str) -> tuple[bool, str | None]:
    """字段是否已有内容（papers 列或来源表），返回 (present, origin)。"""
    if field in ("summary",):
        p = prov.get(field)
        if p and _has_text(p.get("text")):
            return True, p.get("origin")
        if _table_exists(conn, "content_extra"):
            row = conn.execute(
                "SELECT text FROM content_extra WHERE paper_id=? AND field=?",
                (paper["id"], field)).fetchone()
            if row and _has_text(row[0]):
                return True, (p or {}).get("origin")
        return False, None
    if _has_text(paper.get(field)):
        p = prov.get(field)
        return True, (p or {}).get("origin")
    p = prov.get(field)
    if p and _has_text(p.get("text")):
        return True, p.get("origin")
    return False, None


def _persist(conn, paper_id: int, field: str, text: str, origin: str,
             source_hash: str, model: str | None = None,
             prompt_version: str | None = None,
             input_snapshot: str | None = None,
             generated_at: str | None = None) -> None:
    """补齐即持久化：papers 列（或 content_extra）+ 来源登记。

    绝不覆盖已有非空值（双重保护：调用方只在缺口字段上调用本函数，
    SQL 侧再加 WHERE 非空守卫 / INSERT OR IGNORE）。
    V4R1 审计契约：AI 内容（ai_with_source）的 model/prompt_version/
    input_snapshot/generated_at 随行登记；缺失时如实记 NULL，不补造。"""
    text = str(text).strip()
    if not text:
        return
    if field in ("title", "abstract", "title_zh", "abstract_zh"):
        conn.execute(
            f"UPDATE papers SET \"{field}\"=?, updated_at="
            f"datetime('now','localtime') WHERE id=? AND "
            f"(\"{field}\" IS NULL OR TRIM(\"{field}\")='')", (text, paper_id))
    elif field == "summary":
        if _table_exists(conn, "content_extra"):
            conn.execute(
                "INSERT INTO content_extra(paper_id, field, text, updated_at) "
                "VALUES(?,?,?,datetime('now','localtime')) "
                "ON CONFLICT(paper_id, field) DO NOTHING",
                (paper_id, field, text))
    _ensure_provenance_columns(conn)
    conn.execute(
        "INSERT OR IGNORE INTO content_provenance"
        "(paper_id, field, text, origin, source_hash, model, prompt_version, "
        " recorded_by, input_snapshot, generated_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
        (paper_id, field, text, origin, source_hash, model, prompt_version,
         RECORDED_BY, input_snapshot, generated_at))


def _ensure_provenance_columns(conn) -> None:
    """content_provenance 建表/补列（V4R1 审计元数据列；旧库 ALTER 补齐）。"""
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
                conn.execute(
                    f"ALTER TABLE content_provenance ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass


def _current_source_hash(conn, paper_id: int) -> str:
    """按当前 papers 行（题名+摘要）计算来源哈希（V4R1-05）。

    原文补齐会改变输入：登记来源哈希一律以「补齐后的最终实际输入」为准，
    不把原文补齐前的旧哈希赋给新结果。"""
    row = conn.execute("SELECT title, abstract FROM papers WHERE id=?",
                       (paper_id,)).fetchone()
    if row is None:
        return compute_source_hash(None, None)
    return compute_source_hash(row["title"], row["abstract"])


def _realign_provenance_hash(conn, paper_id: int, field: str, origin: str,
                             text: str, source_hash: str) -> None:
    """V4R1-05：把本轮登记行的 source_hash 校正为补齐后最终输入的哈希，
    使来源登记与保存的内容、字段依赖一致（只校正 text/origin 匹配的行，
    不改写其他历史记录）。"""
    conn.execute(
        "UPDATE content_provenance SET source_hash=? "
        "WHERE paper_id=? AND field=? AND origin=? AND text=?",
        (source_hash, paper_id, field, origin, text))


def _ai_gate(conn) -> tuple[str, str] | None:
    """V4R1-01 统一闸门（离线维度）：返回 (code, message) 或 None（允许）。

    只做本地 settings 读取。配置缺失的零外呼由提供者入口
    （ai_brief.ai_generation_blocked / load_ai_config）保证，未产出时
    _run_ai_stage 按当前配置状态上报 ai_not_configured / ai_disabled。"""
    try:
        from .ai_brief import ai_generation_blocked
    except Exception:  # noqa: BLE001 - ai_brief 不可用时交由 _run_ai_stage 上报
        return None
    try:
        blocked = ai_generation_blocked(conn, check_config=False)
    except Exception:  # noqa: BLE001 - 闸门故障不阻断流程（最底层仍有闸门）
        return None
    if blocked is None:
        return None
    code = "ai_offline" if blocked["code"] == "offline_mode" else blocked["code"]
    return (code, blocked["message"])


def _content_version(filled: dict) -> str:
    payload = json.dumps(filled, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ Zotero 阶段

def _zotero_fetch(conn, paper: dict) -> tuple[dict | None, tuple[str, str] | None]:
    """Zotero 只读查找（含身份冲突保护）。返回 (data|None, (code, message)|None)。

    五种错误分别上报（§3.1）；命中条目 DOI 与请求 DOI 不同 → 不复用。"""
    from .zotero_client import (ZoteroAuthRequired, ZoteroClient, ZoteroError,
                                ZoteroUnavailable)
    if _get_setting(conn, "zotero_enabled") not in (None, "1"):
        return None, ("zotero_not_enabled",
                      "Zotero 同步未启用（settings.zotero_enabled=0），跳过 Zotero 补全")
    client = ZoteroClient()
    try:
        client.ping()
    except ZoteroUnavailable as e:
        return None, ("zotero_unreachable", f"Zotero 本地服务不可达: {e}")
    except ZoteroAuthRequired as e:
        return None, ("zotero_auth_missing", str(e))
    except ZoteroError as e:
        return None, ("zotero_config_invalid", f"Zotero 配置/协议错误: {e}")
    item = None
    try:
        key = (paper.get("meta") or {}).get("zotero_key")
        if key:
            item = client.get_item(str(key))
        if not item and paper.get("doi"):
            item = client.find_by_identifier(doi=paper["doi"])
    except ZoteroUnavailable as e:
        return None, ("zotero_unreachable", f"Zotero 本地服务不可达: {e}")
    except ZoteroAuthRequired as e:
        return None, ("zotero_auth_missing", str(e))
    except ZoteroError as e:
        return None, ("zotero_config_invalid", f"Zotero 查询失败: {e}")
    if not item or not isinstance(item.get("data"), dict):
        return None, ("zotero_not_found", "Zotero 中未找到匹配条目")
    d = item["data"]
    # 身份保护（§3.3）：DOI 归一化后精确比较；不一致 → 冲突，绝不复用
    item_doi, req_doi = norm_doi(d.get("DOI")), norm_doi(paper.get("doi"))
    if req_doi and item_doi and item_doi != req_doi:
        return None, ("zotero_not_found",
                      f"Zotero 命中条目 DOI 冲突（请求 {req_doi}，条目 {item_doi}），"
                      "按身份保护不复用、不按题名补写")
    return {"zotero_key": item.get("key"),
            "title": d.get("title"),
            "abstract": d.get("abstractNote")}, None


# ------------------------------------------------------------------ 网络阶段

_NETWORK_TIMEOUT = (5, 15)   # (连接, 读取) 秒
_NETWORK_BUDGET = 1          # 单次 enrich 的网络书目请求预算（单篇 1 次）

def _network_fetch(conn, paper: dict, errors: list, missing: list[str]) -> dict | None:
    """有来源网络书目（Crossref works API，固定白名单 https 主机）。

    尊重离线开关（network_disabled）、白名单、超时与预算（§3.1）。
    失败返回 None 并按字段上报 network_failed，不编造。"""
    if _get_setting(conn, "offline_mode") == "1":
        for f in missing:
            errors.append({"field": f, "code": "network_disabled",
                           "message": "当前为离线模式（settings.offline_mode=1），"
                                      "跳过网络书目补全"})
        return None
    doi = paper.get("doi")
    if not doi:
        for f in missing:
            errors.append({"field": f, "code": "network_failed",
                           "message": "缺少 DOI，无法查询网络权威书目"})
        return None
    try:
        from .config import ALLOWED_HOSTS
        host = "api.crossref.org"
        if host not in ALLOWED_HOSTS:
            for f in missing:
                errors.append({"field": f, "code": "network_failed",
                               "message": f"目标主机 {host} 不在出站白名单内"})
            return None
    except Exception:  # noqa: BLE001 - 配置缺失时保持固定白名单主机
        pass
    if _NETWORK_BUDGET <= 0:
        return None
    import requests
    try:
        resp = requests.get(f"https://api.crossref.org/works/{doi}",
                            timeout=_NETWORK_TIMEOUT,
                            headers={"User-Agent": "PaperNet/0.1 (mailto:local)"})
        if resp.status_code != 200:
            for f in missing:
                errors.append({"field": f, "code": "network_failed",
                               "message": f"Crossref {doi} -> HTTP {resp.status_code}"})
            return None
        msg = (resp.json() or {}).get("message") or {}
        return {"title": (msg.get("title") or [None])[0],
                "abstract": msg.get("abstract")}
    except Exception as e:  # noqa: BLE001 - 网络失败不中断补全
        for f in missing:
            errors.append({"field": f, "code": "network_failed",
                           "message": f"网络书目查询失败（已脱敏）: "
                                      f"{type(e).__name__}"})
        return None


# ------------------------------------------------------------------ 主流程

def enrich_paper(conn: sqlite3.Connection, paper_id: int, *,
                 zotero: bool = True, network: bool = True,
                 ai: bool = False) -> dict:
    """字段补全核心（契约 3.1）。论文不存在抛 ValueError（路由转 404）。

    返回 {"paper_id", "fields": {field: {before, after, origin, text_len}},
          "queued", "errors": [{field, code, message}], "queue": {...}}。
    """
    paper = _load_paper(conn, paper_id)
    if paper is None:
        raise ValueError(f"论文 #{paper_id} 不存在")
    prov = _provenance_map(conn, paper_id)
    fields_out: dict[str, dict] = {}
    errors: list[dict] = []
    filled: dict[str, str] = {}

    # ---- 缺口判定（本地已有 → skipped，不覆盖不重复生成）
    state: dict[str, tuple[bool, str | None]] = {}
    for f in FIELDS:
        present, origin = _field_present(conn, paper, prov, f)
        state[f] = (present, origin)
        fields_out[f] = {"before": "present" if present else "missing",
                         "after": "skipped" if present else "missing",
                         "origin": origin,
                         "text_len": (len(str(paper.get(f) or (prov.get(f) or {}).get("text") or ""))
                                      if present else None)}

    source_hash = _current_source_hash(conn, paper_id)

    # ---- Zotero 阶段（仅原文 title/abstract，原文语种非翻译）
    missing_raw = [f for f in RAW_FIELDS if not state[f][0]]
    zotero_data = None
    if zotero and missing_raw:
        zotero_data, zerr = _zotero_fetch(conn, paper)
        if zerr:
            for f in missing_raw:
                errors.append({"field": f, "code": zerr[0], "message": zerr[1]})
        elif zotero_data:
            stage_filled: list[tuple[str, str]] = []
            for f in missing_raw:
                val = zotero_data.get(f)
                if _has_text(val):
                    _persist(conn, paper_id, f, str(val), "zotero", source_hash)
                    filled[f] = str(val)
                    stage_filled.append((f, str(val)))
                    fields_out[f].update({"after": "filled", "origin": "zotero",
                                          "text_len": len(str(val))})
            if stage_filled:
                # V4R1-05：原文补齐改变输入 → 按最终实际输入重算来源哈希，
                # 并校正本轮登记行（不把补齐前的旧哈希赋给新结果）
                source_hash = _current_source_hash(conn, paper_id)
                for f, txt in stage_filled:
                    _realign_provenance_hash(conn, paper_id, f, "zotero", txt,
                                             source_hash)

    # ---- 网络书目阶段（仅原文，仍有缺口才触发）
    still_missing_raw = [f for f in RAW_FIELDS
                         if fields_out[f]["after"] == "missing"]
    if network and still_missing_raw:
        net = _network_fetch(conn, paper, errors, still_missing_raw)
        if net:
            net_filled: list[tuple[str, str]] = []
            for f in still_missing_raw:
                val = net.get(f)
                if _has_text(val):
                    _persist(conn, paper_id, f, str(val), "network_biblio",
                             source_hash)
                    filled[f] = str(val)
                    net_filled.append((f, str(val)))
                    fields_out[f].update({"after": "filled",
                                          "origin": "network_biblio",
                                          "text_len": len(str(val))})
            if net_filled:
                source_hash = _current_source_hash(conn, paper_id)
                for f, txt in net_filled:
                    _realign_provenance_hash(conn, paper_id, f,
                                             "network_biblio", txt, source_hash)

    # ---- AI 阶段（仅当有原文依据且 ai=True；ai 默认 False）
    missing_ai = [f for f in AI_FIELDS if fields_out[f]["after"] == "missing"]
    if missing_ai:
        if not ai:
            for f in missing_ai:
                errors.append({"field": f, "code": "ai_disabled",
                               "message": "AI 补全未启用（allow_ai=false）"})
        else:
            gate = _ai_gate(conn)
            paper_now = _load_paper(conn, paper_id) or paper
            src_title = paper_now.get("title") or ""
            src_abstract = paper_now.get("abstract") or ""
            if not (_has_text(src_title) or _has_text(src_abstract)):
                for f in missing_ai:
                    errors.append({"field": f, "code": "no_source_for_ai",
                                   "message": "无原文依据（题名/摘要均缺失），"
                                              "AI 不依据题名猜测摘要内容"})
            elif gate is not None:
                # V4R1-01：离线时零模型请求，逐字段返回明确状态
                for f in missing_ai:
                    errors.append({"field": f, "code": gate[0],
                                   "message": gate[1]})
            else:
                # V4R1-05：AI 输入是补齐后的最终原文 → 用最终来源哈希登记
                source_hash = _current_source_hash(conn, paper_id)
                fields_from_ai = _run_ai_stage(conn, paper_id, missing_ai,
                                               errors)
                for f, meta in fields_from_ai.items():
                    text = str(meta.get("text") or "").strip()
                    if not text:
                        continue
                    legacy = meta.get("provider_contract") == "legacy_str"
                    if not legacy and (not meta.get("model") or
                                       not meta.get("prompt_version")):
                        # 新契约提供者必须自带模型/提示词版本元数据；
                        # 缺失时拒绝登记为 ai_with_source（如实上报，不补造）
                        errors.append(
                            {"field": f, "code": "ai_metadata_missing",
                             "message": "AI 提供者未返回模型/提示词版本元数据，"
                                        "拒绝登记为 ai_with_source"})
                        continue
                    _persist(conn, paper_id, f, text, "ai_with_source",
                             source_hash,
                             model=meta.get("model"),
                             prompt_version=meta.get("prompt_version"),
                             input_snapshot=meta.get("input_snapshot"),
                             generated_at=meta.get("generated_at"))
                    filled[f] = text
                    fields_out[f].update({"after": "filled",
                                          "origin": "ai_with_source",
                                          "text_len": len(text)})

    # ---- 保存→队列衔接（§3.2）：补齐即入队，未启用记录明确状态
    queue_info: dict = {"queued": False, "state": "nothing_filled"}
    queued = False
    if filled:
        queue_info, queued = _enqueue(conn, paper_id, filled)

    # ---- 收尾
    conn.commit()
    return {"paper_id": paper_id, "fields": fields_out,
            "queued": queued, "queue": queue_info, "errors": errors}


def _run_ai_stage(conn, paper_id: int, missing_ai: list[str],
                  errors: list[dict]) -> dict[str, dict]:
    """调用 AI 字段提供者（经 library_source 挂钩，§3.1）。

    返回 {field: {"text", "model"?, "prompt_version"?, "input_snapshot"?,
                  "generated_at"?}}：V4R1 审计契约下，新式提供者返回带
    元数据的 dict，原样透传给 _persist；旧式 str 提供者兼容返回
    {"text": ...}（元数据未知，如实记 NULL，不补造）。

    防御性导入：library_source 缺失/抛错 → 记 ai_disabled，不中断。
    get_existing_content(..., ai=True) 仅对缺失字段调用已注册提供者；
    enrich 负责把返回文本持久化并登记 ai_with_source 来源。"""
    out: dict[str, dict] = {}
    try:
        from .library_source import get_existing_content
    except Exception as e:  # noqa: BLE001
        for f in missing_ai:
            errors.append({"field": f, "code": "ai_disabled",
                           "message": f"AI 提供者不可用: {type(e).__name__}"})
        return out
    try:
        ec = get_existing_content(conn, paper_id, zotero=False,
                                  network=False, ai=True)
    except TypeError:
        # 兼容不带关键字参数的旧签名
        try:
            ec = get_existing_content(conn, paper_id)
        except Exception as e:  # noqa: BLE001
            for f in missing_ai:
                errors.append({"field": f, "code": "ai_disabled",
                               "message": f"AI 提供者失败: {type(e).__name__}"})
            return out
    except Exception as e:  # noqa: BLE001
        for f in missing_ai:
            errors.append({"field": f, "code": "ai_disabled",
                           "message": f"AI 提供者失败: {type(e).__name__}"})
        return out
    if not isinstance(ec, dict):
        for f in missing_ai:
            errors.append({"field": f, "code": "ai_disabled",
                           "message": "AI 提供者返回异常形状，已拒绝"})
        return out
    for f in missing_ai:
        info = ec.get(f) if isinstance(ec.get(f), dict) else {}
        text = info.get("text")
        if _has_text(text):
            contract_map = ec.get("ai_provider_contract") or {}
            entry = {"text": str(text)}
            for k in ("model", "prompt_version", "input_snapshot",
                      "generated_at"):
                if info.get(k) is not None:
                    entry[k] = info[k]
            if contract_map.get(f):
                entry["provider_contract"] = contract_map[f]
            out[f] = entry
        elif f in ZH_FIELDS or f == "summary":
            # 已请求但无提供者/未生成：按当前配置状态明确报告，不静默
            try:
                from .ai_brief import load_ai_config
                configured = load_ai_config() is not None
            except Exception:  # noqa: BLE001
                configured = False
            if not configured:
                errors.append({"field": f, "code": "ai_not_configured",
                               "message": "校内模型配置不可用，未产生模型请求，"
                                          "该字段未生成"})
            else:
                errors.append({"field": f, "code": "ai_disabled",
                               "message": "未注册该字段的 AI 提供者或提供者未产出内容"})
    return out


def _enqueue(conn, paper_id: int, filled: dict) -> tuple[dict, bool]:
    """补齐后登记 Zotero 同步待办（§3.2，reason="enrich"）。

    幂等（B 线 enqueue 语义）；zotero_enabled=0 → 记录明确"未启用"状态，
    绝不静默。任何失败记录明确状态，不中断补全结果返回。"""
    enabled = _get_setting(conn, "zotero_enabled")
    if enabled not in (None, "1"):
        log.info("Zotero 同步未启用（paper %d enrich 已保存，未入队）", paper_id)
        return {"queued": False, "state": "zotero_not_enabled",
                "message": "Zotero 同步未启用（settings.zotero_enabled=0），"
                           "补全内容已保存但未登记同步待办"}, False
    try:
        from . import zotero_sync
        payload = {"reason": "enrich",
                   "content_version": _content_version(filled)}
        res = zotero_sync.enqueue(conn, paper_id, payload)
        qid = res.get("queue_id") if isinstance(res, dict) else res
        return {"queued": True, "queue_id": qid, "state": "pending",
                "reason": "enrich"}, True
    except Exception as e:  # noqa: BLE001 - 入队失败不静默
        log.warning("enrich 入队失败（paper %d）: %s", paper_id, e)
        return {"queued": False, "state": "enqueue_failed",
                "message": f"入队失败: {type(e).__name__}"}, False
