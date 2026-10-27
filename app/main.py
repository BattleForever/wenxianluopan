"""PaperNet — 本地文献引用网络与发现工具。"""

import json
import logging
import re
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import graph as graphmod
from . import __version__
from .startup import data_identity
from . import importer, citations as citationsmod
from .config import DATA_DIR, DB_PATH, HOST, PDF_DIR, PORT, PDF_EXTRA_HOSTS, STATIC_DIR
from .db import get_db, get_setting, init_db, set_setting
from .netclient import OfflineError, ForbiddenHostError, is_online
from .store import norm_venue, paper_dict, upsert_paper

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("papernet")

init_db()

app = FastAPI(title="文献罗盘", version=__version__, docs_url=None, redoc_url=None)

from . import workspace_data,network_settings,pdf_storage,browser_capture
app.include_router(workspace_data.router)
app.include_router(network_settings.router)
app.include_router(pdf_storage.router)
browser_capture.init()
app.include_router(browser_capture.router)

@app.middleware('http')
async def workspace_transfer_guard(request, call_next):
    api_request = request.url.path.startswith('/api/')
    exempt = request.url.path in ('/api/health', '/api/workspace/state')
    if api_request and not exempt:
        if workspace_data.BUSY.is_set() or getattr(app.state, 'workspace_switching', False):
            return JSONResponse(status_code=503, content={'detail': '工作空间正在备份或切换，请稍候。'})
        app.state.workspace_requests = getattr(app.state, 'workspace_requests', 0) + 1
    try:
        return await call_next(request)
    except Exception as error:
        from .request_errors import record
        identifier=record(request,error)
        return JSONResponse(status_code=500,content={'detail':'本次操作未完成，请重试；已保留本地诊断记录。','error_id':identifier})
    finally:
        if api_request and not exempt:
            app.state.workspace_requests -= 1


# ------------------------------------------------------------------ models

class TextIn(BaseModel):
    text: str


class PathIn(BaseModel):
    path: str


class ZoteroSyncIn(BaseModel):
    collection_key: str | None = None


class StandardIn(BaseModel):
    papers_text: str = ""
    citations_text: str = ""
    venues_text: str = ""


class PaperEditIn(BaseModel):
    title_zh: str | None = None
    abstract_zh: str | None = None
    title: str | None = None
    abstract: str | None = None


class IDsIn(BaseModel):
    ids: list[int]


class CollectionIn(BaseModel):
    name: str


class CollectionItemsIn(BaseModel):
    paper_ids: list[int]


class SettingsIn(BaseModel):
    values: dict


def _err(e: Exception):
    if isinstance(e,ValueError):return JSONResponse(status_code=422,content={'error':str(e)})
    if isinstance(e, OfflineError):
        return JSONResponse(status_code=503, content={
            "error": "当前为离线模式且本地无缓存，请联网后重试或在设置中关闭离线模式"})
    if isinstance(e, ForbiddenHostError):
        return JSONResponse(status_code=400, content={"error": str(e)})
    log.exception("请求失败")
    return JSONResponse(status_code=500, content={"error": str(e)})


# ------------------------------------------------------------------ 基础

@app.get("/api/health")
def health():
    with get_db() as conn:
        n_lib = conn.execute("SELECT COUNT(*) c FROM papers WHERE status='library' AND id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library')").fetchone()["c"]
        n_cand = conn.execute("SELECT COUNT(*) c FROM papers WHERE status!='library' AND id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library')").fetchone()["c"]
        n_edge = conn.execute("SELECT COUNT(*) c FROM citations WHERE from_id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library') AND to_id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library')").fetchone()["c"]
    return {"ok": True, "version": __version__, "online": is_online(), "library": n_lib,
            "candidates": n_cand, "edges": n_edge,
            "data_dir": str(DATA_DIR), "app_id": "wenxianluopan",
            "data_id": data_identity(DATA_DIR)}


@app.get("/api/settings")
def read_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    d = {r["key"]: r["value"] for r in rows}
    d.pop("zotero_enabled", None)
    from .db import get_bool_setting
    d["zotero_enabled"] = "1" if get_bool_setting("zotero_enabled") else "0"
    return d


@app.post("/api/settings")
def write_settings(body: SettingsIn):
    allowed = {"offline_mode", "mailto", "citedby_cap", "opencitations_enabled",
               "zotero_enabled", "zotero_data_dir", "candidate_cap", "build_batch_size"}
    for k, v in body.values.items():
        if k in allowed:
            set_setting(k, v)
    return {"ok": True}


# ------------------------------------------------------------------ 导入

@app.post("/api/import/doi")
def api_import_doi(body: TextIn):
    try:
        return importer.import_dois(body.text)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.post("/api/import/bibtex")
def api_import_bibtex(body: TextIn):
    try:
        return importer.import_bibtex(body.text)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.post("/api/import/ris")
def api_import_ris(body: TextIn):
    try:
        return importer.import_ris(body.text)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.post("/api/import/standard")
def api_import_standard(body: StandardIn):
    """自采集标准数据表导入（JSONL：papers + citations + venues），支持纯中文文献。"""
    try:
        return importer.import_standard(body.papers_text, body.citations_text,
                                        body.venues_text)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.post("/api/papers/{paper_id}/edit")
def api_paper_edit(paper_id: int, body: PaperEditIn):
    """手动补充/修正文献信息（如中文标题、中文摘要）。只更新提供的字段。
    title_zh/abstract_zh 允许置空（清除译文）；title/abstract 空值跳过，防止误清原文。"""
    changed = 0
    with get_db() as conn:
        if body.title_zh is not None:
            v = body.title_zh.strip()
            conn.execute("UPDATE papers SET title_zh=? WHERE id=?",
                         (v if v else None, paper_id))
            changed += 1
        if body.abstract_zh is not None:
            v = body.abstract_zh.strip()
            conn.execute("UPDATE papers SET abstract_zh=? WHERE id=?",
                         (v if v else None, paper_id))
            changed += 1
        if body.title is not None and body.title.strip():
            conn.execute("UPDATE papers SET title=? WHERE id=?",
                         (body.title.strip(), paper_id))
            changed += 1
        if body.abstract is not None and body.abstract.strip():
            conn.execute("UPDATE papers SET abstract=? WHERE id=?",
                         (body.abstract.strip(), paper_id))
            changed += 1
    return {"ok": True, "changed": changed}


@app.post("/api/import/pdf-folder")
def api_import_pdf_folder(body: PathIn):
    try:
        return importer.scan_pdf_folder(body.path)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/zotero/collections")
def api_zotero_collections():
    try:
        return {"collections": importer.zotero_collections()}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.post("/api/zotero/sync")
def api_zotero_sync(body: ZoteroSyncIn | None = None):
    try:
        ckey = (body.collection_key if body else None) or None
        return importer.zotero_sync(enrich=False, collection_key=ckey)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ------------------------------------------------------------------ 文献库

@app.get("/api/papers")
def api_papers(query: str = "", status: str = "", limit: int = 200, offset: int = 0):
    limit = max(1, min(limit, 1000))  # SQLite 负 LIMIT 语义是"不限"，必须夹紧
    with get_db() as conn:
        sql = "SELECT * FROM papers WHERE id NOT IN (SELECT paper_id FROM paper_trash WHERE scope='library')"
        params: list = []
        if status in ("library", "candidate"):
            sql += " AND status=?"
            params.append(status)
        if query:
            sql += " AND (title LIKE ? OR title_zh LIKE ? OR doi LIKE ? OR norm_title LIKE ?)"
            q = f"%{query}%"
            params += [q, q, q, q.replace(" ", "")]
        where_tail = sql[len("SELECT * FROM papers "):]
        total = conn.execute(
            "SELECT COUNT(*) c FROM papers " + where_tail, params
        ).fetchone()["c"]
        sql += " ORDER BY cited_by_count DESC, id DESC LIMIT ? OFFSET ?"
        params += [min(limit, 1000), offset]
        rows = conn.execute(sql, params).fetchall()
    return {"total": total, "papers": [paper_dict(r) for r in rows]}


@app.get("/api/papers/{paper_id}")
def api_paper_detail(paper_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise HTTPException(404, "未找到该文献")
        in_deg = conn.execute("SELECT COUNT(*) c FROM citations WHERE to_id=?",
                              (paper_id,)).fetchone()["c"]
        out_deg = conn.execute("SELECT COUNT(*) c FROM citations WHERE from_id=?",
                               (paper_id,)).fetchone()["c"]
        venue_row = None
        vn = norm_venue(row["venue"] or "")
        if vn:
            venue_row = conn.execute(
                "SELECT name, kind, impact_factor, jcr_zone, cas_zone, cas_top, "
                "cas_sub, ccf_tier, source, extra_json, updated_at FROM venues WHERE name_norm=?",
                (vn,)).fetchone()
    d = paper_dict(row)
    d["edges_in"] = in_deg
    d["edges_out"] = out_deg
    from .linkview import venue_public
    d["venue_info"] = venue_public(dict(venue_row)) if venue_row else None
    return d


@app.delete("/api/papers/{paper_id}")
def api_paper_delete(paper_id: int):
    return {'ok':True,**trash.put(trash.Put(ids=[paper_id],scope='library'))}


@app.post("/api/papers/promote")
def api_promote(body: IDsIn):
    with get_db() as conn:
        trash.ensure_active(conn,body.ids)
        cur = conn.executemany(
            "UPDATE papers SET status='library' WHERE id=? AND status!='library'",
            [(i,) for i in body.ids],
        )
        promoted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return {"ok": True, "promoted": promoted}


@app.post("/api/papers/{paper_id}/download-pdf")
def api_download_pdf(paper_id: int):
    """Compatibility endpoint using the validated PDF acquisition path."""
    from .pdf_access import download, status
    existed = status(paper_id)["ready"]
    return {"ok": True, "downloaded": not existed, **download(paper_id)}


@app.get("/api/papers/{paper_id}/pdf")
def api_serve_pdf(paper_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT local_pdf_path FROM papers WHERE id=?",
                           (paper_id,)).fetchone()
    if not row or not row["local_pdf_path"]:
        raise HTTPException(404, "未关联本地 PDF")
    p = Path(row["local_pdf_path"])
    if not p.is_file():
        raise HTTPException(404, f"文件不存在: {row['local_pdf_path']}")
    return FileResponse(p, media_type="application/pdf",
                        filename=p.name, content_disposition_type="inline")


@app.get("/api/papers/{paper_id}/citations-view")
def api_citations_view(paper_id: int):
    with get_db() as conn:
        citing = conn.execute(
            """SELECT p.id, p.title, p.year, p.status, p.cited_by_count
               FROM citations c JOIN papers p ON p.id=c.from_id WHERE c.to_id=?
               ORDER BY p.cited_by_count DESC LIMIT 200""", (paper_id,)).fetchall()
        refs = conn.execute(
            """SELECT p.id, p.title, p.year, p.status, p.cited_by_count
               FROM citations c JOIN papers p ON p.id=c.to_id WHERE c.from_id=?
               ORDER BY p.cited_by_count DESC LIMIT 200""", (paper_id,)).fetchall()
    keys = ("id", "title", "year", "status", "cited_by_count")
    return {"citing": [dict(zip(keys, [r[k] for k in keys])) for r in citing],
            "references": [dict(zip(keys, [r[k] for k in keys])) for r in refs]}


# ------------------------------------------------------------------ 引用抓取

@app.post("/api/citations/fetch/{paper_id}")
def api_citations_fetch(paper_id: int, citedby_cap: int | None = None):
    try:
        return citationsmod.fetch_citations_for_paper(paper_id, citedby_cap)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ------------------------------------------------------------------ 图谱 / 推荐 / 导出

@app.get("/api/graph")
def api_graph(include_candidates: bool = False, collection_id: int | None = None,
              year_min: int | None = None, year_max: int | None = None):
    try:
        cap = int(get_setting("candidate_cap") or 300)
        return graphmod.build_graph(include_candidates, collection_id, year_min,
                                    year_max, cap)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/papers/{paper_id}/similar")
def api_similar(paper_id: int, limit: int = 20):
    try:
        return {"results": graphmod.similar_papers(paper_id, limit)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@app.get("/api/export/graphml")
def api_export_graphml(include_candidates: bool = False):
    try:
        content = graphmod.export_graphml(include_candidates)
        return Response(content, media_type="application/xml",
                        headers={"Content-Disposition":'attachment; filename="papernet.graphml"'})
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ------------------------------------------------------------------ 数据库浏览（只读）
# 安全约定：每条 SQL 都是 conn.execute 调用处的内联字面量；搜索值全部参数绑定；
# 表名经白名单分支分发，标识符不来自用户输入。

_DB_TABLES = ("papers", "citations", "collections", "collection_items",
              "venues", "settings", "fetch_cache")

_DB_COLUMNS = {
    "papers": ["id", "openalex_id", "doi", "title", "title_zh", "authors",
               "year", "venue", "abstract", "abstract_zh", "type",
               "cited_by_count", "status", "oa_pdf_url", "local_pdf_path",
               "source", "tags", "meta_json", "fetched_at", "added_at",
               "updated_at"],
    "citations": ["from_id", "citing_title", "citing_doi", "to_id",
                  "cited_title", "cited_doi", "src"],
    "collections": ["id", "name", "created_at"],
    "collection_items": ["collection_id", "paper_id"],
    "venues": ["id", "name", "kind", "impact_factor", "jcr_zone", "cas_zone",
               "cas_top", "cas_sub", "ccf_tier", "source", "updated_at"],
    "settings": ["key", "value"],
    "fetch_cache": ["key", "url", "status", "fetched_at"],
}


@app.get("/api/db/tables")
def api_db_tables():
    out = []
    with get_db() as conn:
        for name in _DB_TABLES:
            if name == "papers":
                n = conn.execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]
            elif name == "citations":
                n = conn.execute("SELECT COUNT(*) c FROM citations").fetchone()["c"]
            elif name == "collections":
                n = conn.execute("SELECT COUNT(*) c FROM collections").fetchone()["c"]
            elif name == "collection_items":
                n = conn.execute(
                    "SELECT COUNT(*) c FROM collection_items").fetchone()["c"]
            elif name == "venues":
                n = conn.execute("SELECT COUNT(*) c FROM venues").fetchone()["c"]
            elif name == "settings":
                n = conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"]
            else:
                n = conn.execute("SELECT COUNT(*) c FROM fetch_cache").fetchone()["c"]
            out.append({"name": name, "rows": n, "columns": _DB_COLUMNS[name]})
    return {"tables": out, "db_path": str(DB_PATH), "data_dir": str(DATA_DIR)}


@app.get("/api/db/rows")
def api_db_rows(table: str, q: str = "", limit: int = 50, offset: int = 0):
    if table not in _DB_TABLES:
        raise HTTPException(400, "未知数据表")
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    like = "%" + q.strip() + "%"
    columns = _DB_COLUMNS[table]
    has_q = bool(q.strip())
    rows, total = [], 0

    with get_db() as conn:
        if table == "papers":
            if has_q:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM papers WHERE title LIKE ? "
                    "OR title_zh LIKE ? OR doi LIKE ? OR venue LIKE ? "
                    "OR authors LIKE ?",
                    (like, like, like, like, like)).fetchone()["c"]
                raw = conn.execute(
                    "SELECT * FROM papers WHERE title LIKE ? "
                    "OR title_zh LIKE ? OR doi LIKE ? OR venue LIKE ? "
                    "OR authors LIKE ? ORDER BY id LIMIT ? OFFSET ?",
                    (like, like, like, like, like, limit, offset)).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM papers").fetchone()["c"]
                raw = conn.execute(
                    "SELECT * FROM papers ORDER BY id LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
        elif table == "citations":
            if has_q:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM citations WHERE src LIKE ?",
                    (like,)).fetchone()["c"]
                raw = conn.execute(
                    "SELECT c.from_id, p1.title AS citing_title, p1.doi AS citing_doi, "
                    "c.to_id, p2.title AS cited_title, p2.doi AS cited_doi, c.src "
                    "FROM citations c "
                    "LEFT JOIN papers p1 ON p1.id = c.from_id "
                    "LEFT JOIN papers p2 ON p2.id = c.to_id "
                    "WHERE c.src LIKE ? ORDER BY c.from_id, c.to_id LIMIT ? OFFSET ?",
                    (like, limit, offset)).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM citations").fetchone()["c"]
                raw = conn.execute(
                    "SELECT c.from_id, p1.title AS citing_title, p1.doi AS citing_doi, "
                    "c.to_id, p2.title AS cited_title, p2.doi AS cited_doi, c.src "
                    "FROM citations c "
                    "LEFT JOIN papers p1 ON p1.id = c.from_id "
                    "LEFT JOIN papers p2 ON p2.id = c.to_id "
                    "ORDER BY c.from_id, c.to_id LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
        elif table == "collections":
            if has_q:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM collections WHERE name LIKE ?",
                    (like,)).fetchone()["c"]
                raw = conn.execute(
                    "SELECT * FROM collections WHERE name LIKE ? "
                    "ORDER BY id LIMIT ? OFFSET ?", (like, limit, offset)).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM collections").fetchone()["c"]
                raw = conn.execute(
                    "SELECT * FROM collections ORDER BY id LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
        elif table == "collection_items":
            total = conn.execute(
                "SELECT COUNT(*) c FROM collection_items").fetchone()["c"]
            raw = conn.execute(
                "SELECT * FROM collection_items ORDER BY collection_id, paper_id "
                "LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        elif table == "venues":
            if has_q:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM venues WHERE name LIKE ? "
                    "OR cas_sub LIKE ? OR ccf_tier LIKE ?",
                    (like, like, like)).fetchone()["c"]
                raw = conn.execute(
                    "SELECT id, name, kind, impact_factor, jcr_zone, cas_zone, "
                    "cas_top, cas_sub, ccf_tier, source, updated_at FROM venues "
                    "WHERE name LIKE ? OR cas_sub LIKE ? OR ccf_tier LIKE ? "
                    "ORDER BY name LIMIT ? OFFSET ?",
                    (like, like, like, limit, offset)).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM venues").fetchone()["c"]
                raw = conn.execute(
                    "SELECT id, name, kind, impact_factor, jcr_zone, cas_zone, "
                    "cas_top, cas_sub, ccf_tier, source, updated_at FROM venues "
                    "ORDER BY name LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        elif table == "settings":
            if has_q:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM settings WHERE key LIKE ? "
                    "OR value LIKE ?", (like, like)).fetchone()["c"]
                raw = conn.execute(
                    "SELECT * FROM settings WHERE key LIKE ? OR value LIKE ? "
                    "ORDER BY key LIMIT ? OFFSET ?",
                    (like, like, limit, offset)).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM settings").fetchone()["c"]
                raw = conn.execute(
                    "SELECT * FROM settings ORDER BY key LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()
        else:  # fetch_cache
            if has_q:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM fetch_cache WHERE url LIKE ?",
                    (like,)).fetchone()["c"]
                raw = conn.execute(
                    "SELECT key, url, status, fetched_at FROM fetch_cache "
                    "WHERE url LIKE ? ORDER BY fetched_at DESC LIMIT ? OFFSET ?",
                    (like, limit, offset)).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) c FROM fetch_cache").fetchone()["c"]
                raw = conn.execute(
                    "SELECT key, url, status, fetched_at FROM fetch_cache "
                    "ORDER BY fetched_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)).fetchall()

    rows = [[r[c] for c in columns] for r in raw]
    return {"table": table, "columns": columns, "total": total, "rows": rows,
            "limit": limit, "offset": offset}


def _csv_bytes(header, data_rows) -> bytes:
    import csv
    import io
    # 公式注入防护：Excel 会把 = + @ 开头（含制表符前缀）的单元格当公式执行
    def _safe(v):
        if isinstance(v, str) and v[:1] in ("=", "+", "@", "\t", "\r"):
            return "'" + v
        return v
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows([[_safe(c) for c in row] for row in data_rows])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # UTF-8 BOM，Excel 直开


@app.get("/api/db/export/papers")
def api_db_export_papers():
    header = ["id", "doi", "title", "title_zh", "authors", "year", "venue",
              "abstract", "abstract_zh", "type", "cited_by_count", "status",
              "source", "tags", "local_pdf_path", "oa_pdf_url", "openalex_id",
              "added_at"]
    with get_db() as conn:
        raw = conn.execute(
            "SELECT id, doi, title, title_zh, authors, year, venue, abstract, "
            "abstract_zh, type, cited_by_count, status, source, tags, "
            "local_pdf_path, oa_pdf_url, openalex_id, added_at FROM papers "
            "ORDER BY id").fetchall()
    data_rows = [[r[c] for c in header] for r in raw]
    return Response(content=_csv_bytes(header, data_rows),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=papernet_papers.csv"})


@app.get("/api/db/export/citations")
def api_db_export_citations():
    header = ["from_id", "citing_doi", "citing_title", "to_id", "cited_doi",
              "cited_title", "src"]
    with get_db() as conn:
        raw = conn.execute(
            "SELECT c.from_id, p1.doi AS citing_doi, p1.title AS citing_title, "
            "c.to_id, p2.doi AS cited_doi, p2.title AS cited_title, c.src "
            "FROM citations c "
            "LEFT JOIN papers p1 ON p1.id = c.from_id "
            "LEFT JOIN papers p2 ON p2.id = c.to_id ORDER BY c.from_id"
        ).fetchall()
    data_rows = [[r[c] for c in header] for r in raw]
    return Response(content=_csv_bytes(header, data_rows),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=papernet_citations.csv"})


@app.get("/api/db/export/collections")
def api_db_export_collections():
    header = ["id", "name", "created_at", "paper_count"]
    with get_db() as conn:
        raw = conn.execute(
            "SELECT c.id, c.name, c.created_at, "
            "(SELECT COUNT(*) FROM collection_items i WHERE i.collection_id = c.id) "
            "AS paper_count FROM collections c ORDER BY c.id").fetchall()
    data_rows = [[r[c] for c in header] for r in raw]
    return Response(content=_csv_bytes(header, data_rows),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=papernet_collections.csv"})


@app.get("/api/db/export/venues")
def api_db_export_venues():
    header = ["id", "name", "kind", "impact_factor", "jcr_zone", "cas_zone",
              "cas_top", "cas_sub", "ccf_tier", "source", "updated_at"]
    with get_db() as conn:
        raw = conn.execute(
            "SELECT id, name, kind, impact_factor, jcr_zone, cas_zone, cas_top, "
            "cas_sub, ccf_tier, source, updated_at FROM venues ORDER BY name"
        ).fetchall()
    data_rows = [[r[c] for c in header] for r in raw]
    return Response(content=_csv_bytes(header, data_rows),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=papernet_venues.csv"})


@app.post("/api/db/clear-cache")
def api_db_clear_cache():
    """清空联网抓取缓存（fetch_cache）。只影响缓存：清空后离线模式下
    之前查过的接口需联网重新拉取；文献、引用边、期刊等级均不受影响。"""
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM fetch_cache").fetchone()["c"]
        mb = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(body)),0)/1048576.0 mb "
            "FROM fetch_cache").fetchone()["mb"]
        conn.execute("DELETE FROM fetch_cache")
    # VACUUM 不能在事务内执行，用独立连接在无事务状态下重建库文件回收空间
    with get_db() as conn:
        conn.execute("VACUUM")
    return {"ok": True, "cleared_rows": n, "freed_mb": round(mb, 1)}


class EnrichMissingIn(BaseModel):
    """契约 3.1 请求体：{"paper_id": N, "allow_network": true, "allow_ai": false}"""
    paper_id: int
    allow_network: bool = True
    allow_ai: bool = False


@app.post("/api/enrich/missing")
def api_enrich_missing(body: EnrichMissingIn):
    """字段补全产品入口（parallel_v04 §3.1 契约，A 线 app/enrich.py）。

    按字段判定缺口（title/abstract 原文、title_zh/abstract_zh、summary）；
    补全顺序 = 本地已有 → Zotero（启用时）→ 有来源网络书目（白名单/
    离线开关/超时/预算/身份冲突保护）→ AI（仅 allow_ai 且有原文依据）。
    补齐即持久化（papers 列 + content_provenance 来源登记）并经
    zotero_sync.enqueue 登记同步待办（reason="enrich"）；重复调用不得
    再次外呼；已有内容一律 skipped，不覆盖、不重复生成。
    Zotero 五种错误分别上报（errors.code），网络/AI 错误按契约错误码。
    """
    from .enrich import enrich_paper
    with get_db() as conn:
        try:
            return enrich_paper(conn, body.paper_id, zotero=True,
                                network=body.allow_network,
                                ai=body.allow_ai)
        except ValueError:
            raise HTTPException(404, "未找到该文献")


@app.get("/api/papers/{paper_id}/references-full")
def api_references_full(paper_id: int, limit: int = 200):
    """某篇文献的参考文献列表（含双语标题、摘要、期刊/会议等级）。"""
    limit = max(1, min(limit, 300))
    with get_db() as conn:
        raw = conn.execute(
            "SELECT p.id, p.doi, p.title, p.title_zh, p.year, p.venue, p.authors, "
            "p.abstract, p.abstract_zh, p.cited_by_count, p.status "
            "FROM citations c JOIN papers p ON p.id = c.to_id "
            "WHERE c.from_id = ? ORDER BY p.cited_by_count DESC, p.id LIMIT ?",
            (paper_id, limit)).fetchall()
        # 期刊/会议等级：按归一化名称逐个查询（有索引，量级为单篇参考文献数）
        venue_cache = {}
        out = []
        for r in raw:
            vn = norm_venue(r["venue"] or "")
            vi = None
            if vn:
                if vn in venue_cache:
                    vi = venue_cache[vn]
                else:
                    vrow = conn.execute(
                        "SELECT kind, impact_factor, jcr_zone, cas_zone, cas_top, "
                        "cas_sub, ccf_tier FROM venues WHERE name_norm=?",
                        (vn,)).fetchone()
                    vi = dict(vrow) if vrow else None
                    venue_cache[vn] = vi
            out.append({
                "id": r["id"], "doi": r["doi"], "title": r["title"],
                "title_zh": r["title_zh"], "year": r["year"], "venue": r["venue"],
                "abstract": (r["abstract"] or "")[:200],
                "abstract_zh": (r["abstract_zh"] or "")[:200],
                "authors": r["authors"], "cited_by_count": r["cited_by_count"],
                "status": r["status"], "venue_info": vi,
            })
    return {"paper_id": paper_id, "count": len(out), "references": out}


class VenueRefreshIn(BaseModel):
    limit: int = 100
    stale_days: int = 180


@app.post("/api/venues/refresh")
def api_venues_refresh(body: VenueRefreshIn):
    """更新期刊/会议等级：本地缓存优先，缺失才联网（easyScholar）。
    每条记录都标注更新时间（updated_at），由用户手动触发，每次最多 limit 条。
    两阶段：联网查询结果先攒在内存（不持有写事务），最后单事务写回。"""
    from . import venues as ven

    limit = max(1, min(body.limit, 500))
    stale_days = max(7, min(body.stale_days, 3650))
    if not is_online():
        return JSONResponse(status_code=503, content={
            "error": "当前为离线模式，无法联网更新期刊信息"})
    key = ven.load_key()
    if not key:
        return JSONResponse(status_code=400, content={
            "error": "未找到 easyScholar secretKey（请将密钥文件放入运行目录或检查 easyScholar 配置）"})

    stale_param = f"-{stale_days} days"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM venues WHERE "
            "(impact_factor IS NULL OR impact_factor = '' OR cas_zone IS NULL OR cas_zone = '') "
            "OR updated_at IS NULL OR updated_at <= datetime('now', ?) "
            "ORDER BY updated_at IS NOT NULL, id LIMIT ?",
            (stale_param, limit)).fetchall()
        total_venues = conn.execute("SELECT COUNT(*) c FROM venues").fetchone()["c"]

    # Phase B1：逐个查等级（本地缓存 / 联网限速查询），结果攒在内存
    updated, not_found, errors = 0, 0, 0
    results = []  # (venue_id, rank_fields|None)；None 表示确认无数据仅刷时间戳
    for row in rows:
        try:
            rec = ven.local_rank(row["name"])
            nf = False
            if not rec:
                rec, nf = ven.online_rank(row["name"], key)
            if rec and rec.get("rank"):
                results.append((row["id"], ven.rank_to_fields(rec["rank"])))
            elif nf:
                results.append((row["id"], None))
            else:
                errors += 1
        except Exception:  # noqa: BLE001
            errors += 1

    # Phase B2：单事务写回
    with get_db() as conn:
        for vid, fields in results:
            if fields is not None:
                conn.execute(
                    "UPDATE venues SET impact_factor=COALESCE(?, impact_factor), "
                    "jcr_zone=COALESCE(?, jcr_zone), cas_zone=COALESCE(?, cas_zone), "
                    "cas_top=COALESCE(?, cas_top), cas_sub=COALESCE(?, cas_sub), "
                    "source=?, updated_at=datetime('now','localtime') WHERE id=?",
                    (fields.get("impact_factor"), fields.get("jcr_zone"),
                     fields.get("cas_zone"), fields.get("cas_top"),
                     fields.get("cas_sub"), "easyscholar", vid))
                updated += 1
            else:
                conn.execute(
                    "UPDATE venues SET updated_at=datetime('now','localtime') WHERE id=?",
                    (vid,))
                not_found += 1
    return {"ok": True, "checked": len(rows), "updated": updated,
            "not_found": not_found, "errors": errors,
            "total_venues": total_venues,
            "hint": f"剩余待更新可再次点击继续（每轮最多 {limit} 条）"}


# ------------------------------------------------------------------ 收藏集

@app.get("/api/collections")
def api_collections():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.created_at,
               (SELECT COUNT(*) FROM collection_items i WHERE i.collection_id=c.id) n
               FROM collections c ORDER BY c.id""").fetchall()
    return {"collections": [dict(r) for r in rows]}


@app.post("/api/collections")
def api_collection_create(body: CollectionIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    with get_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO collections(name) VALUES(?)", (name,))
        row = conn.execute("SELECT id FROM collections WHERE name=?", (name,)).fetchone()
    return {"ok": True, "id": row["id"]}


@app.delete("/api/collections/{collection_id}")
def api_collection_delete(collection_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
        conn.execute("DELETE FROM collection_items WHERE collection_id=?",
                     (collection_id,))
    return {"ok": True}


@app.post("/api/collections/{collection_id}/items")
def api_collection_add(collection_id: int, body: CollectionItemsIn):
    with get_db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO collection_items(collection_id, paper_id) VALUES(?,?)",
            [(collection_id, i) for i in body.paper_ids],
        )
    return {"ok": True}


@app.delete("/api/collections/{collection_id}/items/{paper_id}")
def api_collection_remove(collection_id: int, paper_id: int):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM collection_items WHERE collection_id=? AND paper_id=?",
            (collection_id, paper_id),
        )
    return {"ok": True}


# ------------------------------------------------------------------ 前端

@app.get("/api/papers/{paper_id}/linked")
def api_paper_linked(paper_id: int, direction: str = "references", sort: str = "cited", limit: int = 300):
    from .linkview import linked_papers
    try:
        return linked_papers(paper_id, direction, sort, limit)
    except ValueError as e:
        raise HTTPException(400, str(e))

from . import ai_brief as ai_brief_module  # B 线：AI 速览模块初始化与路由注册
ai_brief_module.init()
app.include_router(ai_brief_module.router)

from . import ai_settings
app.include_router(ai_settings.router)
from . import ai_profiles
app.include_router(ai_profiles.router)
from . import folders
app.include_router(folders.router)
from . import data_sources
app.include_router(data_sources.router)
from . import metric_catalog
app.include_router(metric_catalog.router)
from . import connection_status
app.include_router(connection_status.router)

from . import import_jobs
import_jobs.init()
app.include_router(import_jobs.router)
from . import source_profiles
app.include_router(source_profiles.router)
from . import metadata_jobs
metadata_jobs.init()
app.include_router(metadata_jobs.router)
from . import metric_jobs
metric_jobs.init()
app.include_router(metric_jobs.router)

from . import pdf_guide
pdf_guide.init()
app.include_router(pdf_guide.router)
from . import pdf_access
app.include_router(pdf_access.router)

from . import workspace
workspace.init()
app.include_router(workspace.router)
from . import task_center
app.include_router(task_center.router)

from . import trash
app.include_router(trash.router)

@app.on_event('startup')
async def start_trash_maintenance():
    import asyncio
    app.state.trash_task=asyncio.create_task(trash.maintenance())

@app.on_event('shutdown')
async def stop_trash_maintenance():
    import asyncio
    from contextlib import suppress
    task=getattr(app.state,'trash_task',None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):await task

from . import zotero_sync as zotero_sync_module  # B 线（parallel_v03）：Zotero 同步队列与状态接口
zotero_sync_module.init()
app.include_router(zotero_sync_module.router)  # 必须在 app.mount("/", StaticFiles...) 之前注册
from . import zotero_link
zotero_link.init()
app.include_router(zotero_link.router)


@app.post("/api/library/sync/paper")
def api_library_sync_paper(body: zotero_sync_module.SyncPaperIn):
    """V4R1-06 单篇同步入口：单篇意图 → enqueue（队列真实存在）→ 仅对该
    paper flush。一次只同步一篇；无凭据如实 pending 且队列真实存在；
    论文不存在返回 queued=False。"""
    from .db import get_db
    with get_db() as conn:
        return zotero_sync_module.sync_one_paper(
            conn, zotero_sync_module.ZoteroClient(), body.paper_id,
            body.collection_key)


@app.get('/api/papers/{paper_id}/existing')
def api_paper_existing_content(paper_id: int):
    """parallel_v03 总控接缝：已有内容/来源展示端点（C 线 existing_content.js 消费）。

    B 线 library_source.get_existing_content 的 HTTP 包装；仅本地读取
    （Zotero 只读查询，不触发网络/AI 补全）。论文不存在返回 404，
    前端按约定对 404 降级隐藏区块。
    """
    from .library_source import get_existing_content
    with get_db() as conn:
        data = get_existing_content(conn, paper_id, zotero=True, network=False)
    if data is None:
        raise HTTPException(404, "未找到该文献")
    return data

@app.get('/')
def research_workspace():
    return FileResponse(STATIC_DIR / 'workspace.html', headers={'Cache-Control':'no-cache'})

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _open_browser():
    if not __import__("os").environ.get("PAPERNET_NO_BROWSER"):
        from .startup import open_ready
        try:open_ready(f"http://{HOST}:{PORT}", DATA_DIR, __version__)
        except RuntimeError as error:logging.getLogger(__name__).warning('%s', error)




@app.on_event('shutdown')
def stop_capture_browser():
    browser_capture.stop_all()

if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    from .lifecycle import record
    def stop_managed_server():
        record('stop_requested',getattr(app.state,'stop_reason','user_exit'))
        server.should_exit = True
    app.state.stop_server = stop_managed_server
    record('starting')
    try:
        server.run()
    except BaseException as error:
        record('exception',type(error).__name__)
        raise
    finally:
        record('stopped',getattr(app.state,'stop_reason','terminal_or_system'))
