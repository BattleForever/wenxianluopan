"""出站 HTTP 客户端。

安全约束（SSRF 防护）：
- 学术 API 仅允许白名单内的固定 https 主机（config.ALLOWED_HOSTS），
  发请求前校验 scheme 和 host；localhost/环回/私有/保留地址一律拒绝。
- Zotero 本地集成是唯一例外：固定基址 http://127.0.0.1:23119/api，
  只读，路径段仅接受 Zotero 返回的字母数字 item key，且需用户在设置中启用。
- 不提供任何"抓取任意 URL"的功能。
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import ALLOWED_HOSTS, ZOTERO_BASE
from .db import get_db, get_bool_setting

log = logging.getLogger("papernet.net")

UA = "PaperNet/0.1 (local literature tool)"
TIMEOUT = (5, 15)

# 网络不稳定环境下（TLS 重置/限流）的自动重试
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=1, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], respect_retry_after_header=False,
    allowed_methods=["GET"], raise_on_status=False)))
_SESSION.mount("http://", HTTPAdapter(max_retries=Retry(
    total=2, backoff_factor=1.0, allowed_methods=["GET"], raise_on_status=False)))


class OfflineError(Exception):
    """离线模式下请求被拦截。"""


class ForbiddenHostError(Exception):
    """目标主机不在白名单内。"""


# ---------------------------------------------------------------- 可测网络计数
# 目的：把"节点抓取次数"（exploration 层的 fetches）与"实际 HTTP 请求次数 /
# 缓存命中次数"区分开。只在本进程内累加，单任务运行（exploration 有全局互斥），
# 供任务结果记录与核对证据使用；不是监控接口。

_NET_STATS_LOCK = threading.Lock()
_NET_STATS = {"http_requests": 0, "http_errors": 0, "cache_hits": 0}


def _bump_net_stat(name):
    with _NET_STATS_LOCK:
        _NET_STATS[name] += 1


def net_stats_snapshot() -> dict:
    with _NET_STATS_LOCK:
        return dict(_NET_STATS)


def check_scholarly_url(url: str, extra_hosts=frozenset()):
    """校验学术 URL：仅 https + 白名单主机（可传额外主机集合，如 OA PDF 库）。"""
    u = urlparse(url)
    if u.scheme != "https":
        raise ForbiddenHostError(f"拒绝非 https 请求: {url}")
    host = (u.hostname or "").lower()
    if host not in ALLOWED_HOSTS and host not in extra_hosts:
        raise ForbiddenHostError(f"拒绝白名单外主机: {host}")


def is_online() -> bool:
    return not get_bool_setting("offline_mode")


def _cache_key(url: str, params) -> str:
    raw = url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_with_retry(url, params=None, timeout=TIMEOUT, attempts=1):
    last = None
    for i in range(attempts):
        _bump_net_stat("http_requests")  # 每个 HTTP 尝试都计数（与缓存命中区分）
        try:
            return _SESSION.get(url, params=params,
                                headers={"User-Agent": UA}, timeout=timeout)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            _bump_net_stat("http_errors")
            last = e
            log.warning("网络错误(第%d次): %s %s", i + 1, url, type(e).__name__)
            time.sleep(1.5 * (i + 1))
    raise last


def cached_get_json(url: str, params=None, use_cache=True):
    """GET 并缓存到本地 SQLite；离线模式下只允许命中缓存。返回 dict/list 或 None(404)。"""
    key = _cache_key(url, params)
    if use_cache:
        with get_db() as conn:
            row = conn.execute(
                "SELECT status, body FROM fetch_cache WHERE key=?", (key,)
            ).fetchone()
        if row is not None:
            _bump_net_stat("cache_hits")  # 缓存命中，未产生 HTTP 请求
            if row["status"] == 404:
                return None
            try:
                body = json.loads(row["body"])
                return body
            except (ValueError, TypeError):
                pass  # 缓存损坏则重新拉取
    if not is_online():
        raise OfflineError(f"离线模式且无本地缓存: {url}")
    check_scholarly_url(url)
    try:
        resp = _get_with_retry(url, params=params)
    except requests.exceptions.Timeout as e:
        _bump_net_stat("http_errors")
        raise
    if resp.status_code == 404:
        body = "null"
    elif resp.status_code != 200:
        _bump_net_stat("http_errors")
        raise RuntimeError(f"HTTP {resp.status_code}: {url}")
    else:
        body = resp.text
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_cache(key, url, status, body) VALUES(?,?,?,?)",
            (key, url, resp.status_code, body),
        )
    return json.loads(body) if body != "null" else None


# ---------------------------------------------------------------- OpenAlex

OA_BASE = "https://api.openalex.org"
OA_SELECT = (
    "id,doi,display_name,publication_year,cited_by_count,authorships,"
    "primary_location,type,related_works,best_oa_location,abstract_inverted_index"
)


def _oa_params(extra=None):
    p = dict(extra or {})
    mailto = None
    from .db import get_setting
    mailto = get_setting("mailto") or ""
    if mailto:
        p["mailto"] = mailto
    if os.environ.get('OPENALEX_API_KEY'):
        p['api_key'] = os.environ['OPENALEX_API_KEY']
    return p


def _abs_from_inverted(inv):
    if not inv:
        return None
    try:
        pos = {}
        for word, idxs in inv.items():
            for i in idxs:
                pos[i] = word
        return " ".join(pos[i] for i in sorted(pos))
    except Exception:
        return None


def oa_work_to_paper(w: dict) -> dict:
    """OpenAlex work -> paper 字段 dict。"""
    doi = w.get("doi") or ""
    if doi:
        doi = doi.replace("https://doi.org/", "").lower()
    venue = None
    loc = w.get("primary_location") or {}
    src = (loc.get("source") or {}) if isinstance(loc, dict) else {}
    venue = src.get("display_name")
    authors = []
    for a in (w.get("authorships") or []):
        name = (a.get("author") or {}).get("display_name")
        if name:
            authors.append(name)
    oa_pdf = None
    best = w.get("best_oa_location") or {}
    if isinstance(best, dict):
        oa_pdf = best.get("pdf_url") or (loc or {}).get("pdf_url")
    return {
        "openalex_id": w.get("id"),
        "doi": doi or None,
        "title": w.get("display_name"),
        "authors": authors,
        "year": w.get("publication_year"),
        "venue": venue,
        "abstract": _abs_from_inverted(w.get("abstract_inverted_index")),
        "type": w.get("type"),
        "cited_by_count": w.get("cited_by_count") or 0,
        "oa_pdf_url": oa_pdf,
        "related_works": w.get("related_works") or [],
    }


def oa_work_by_doi(doi: str):
    doi = doi.replace("https://doi.org/", "").strip().lower()
    return cached_get_json(f"{OA_BASE}/works/doi:{doi}", _oa_params())


def oa_work_by_id(wid: str):
    return cached_get_json(f"{OA_BASE}/works/{wid.rstrip('/').split('/')[-1]}", _oa_params())


def oa_works_by_ids(wids: list):
    """按 OpenAlex id 批量取，50 个/批。"""
    out = []
    for i in range(0, len(wids), 50):
        chunk = [w for w in wids[i:i + 50] if w]
        if not chunk:
            continue
        data = cached_get_json(
            f"{OA_BASE}/works",
            _oa_params({"filter": "openalex:" + "|".join(chunk),
                        "select": OA_SELECT, "per-page": 50}),
        )
        out.extend(data.get("results", []) if data else [])
    return out


def oa_works_by_dois(dois: list):
    out = []
    dois = [d for d in dois if d]
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        if not chunk:
            continue
        try:
            data = cached_get_json(
                f"{OA_BASE}/works",
                _oa_params({"filter": "doi:" + "|".join(chunk),
                            "select": OA_SELECT, "per-page": 50}),
            )
        except RuntimeError:
            chunk_ids = [d for d in chunk]  # 某些 doi 404 整批失败时逐个回退
            for d in chunk_ids:
                w = oa_work_by_doi(d)
                if w:
                    out.append(w)
            continue
        out.extend(data.get("results", []) if data else [])
    return out


def oa_search_title(title: str, limit=5):
    data = cached_get_json(
        f"{OA_BASE}/works",
        _oa_params({"search": title, "per-page": limit, "select": OA_SELECT}),
    )
    return data.get("results", []) if data else []


def oa_cited_by(wid: str, cap: int):
    """cursor 分页迭代引用 wid 的论文（cited_by 方向）。"""
    cursor = "*"
    got = 0
    while got < cap:
        per = min(200, cap - got)
        data = cached_get_json(
            f"{OA_BASE}/works",
            _oa_params({"filter": f"cites:{wid}", "cursor": cursor,
                        "per-page": per, "select": OA_SELECT}),
        )
        if not data:
            break
        results = data.get("results", [])
        if not results:
            break
        for w in results:
            yield w
        got += len(results)
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break


# ---------------------------------------------------------------- OpenCitations

OC_BASE = "https://opencitations.net/index/api/v2"


def _oc_pair(item: dict):
    citing = (item.get("citing") or "").replace("doi:", "").lower()
    cited = (item.get("cited") or "").replace("doi:", "").lower()
    return citing, cited


def oc_references(doi: str):
    """doi 所引用的文献（outgoing）。"""
    data = cached_get_json(f"{OC_BASE}/references/doi:{doi}", use_cache=True)
    if not data:
        return []
    out = []
    for it in data:
        if isinstance(it, dict):
            citing, cited = _oc_pair(it)
            if cited:
                out.append(cited)
    return out


def oc_citations(doi: str):
    """引用 doi 的文献（incoming）。"""
    data = cached_get_json(f"{OC_BASE}/citations/doi:{doi}", use_cache=True)
    if not data:
        return []
    out = []
    for it in data:
        if isinstance(it, dict):
            citing, cited = _oc_pair(it)
            if citing:
                out.append(citing)
    return out


# ---------------------------------------------------------------- Zotero 本地 API

def zotero_get(path: str, params=None):
    if not get_bool_setting("zotero_enabled"):
        raise RuntimeError("Zotero 集成未启用（设置中开启）")
    # 路径全部由本程序构造，item key 段在调用处已按 ^[A-Za-z0-9]+$ 校验
    url = ZOTERO_BASE + path
    resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Zotero 本地 API HTTP {resp.status_code}: {path} "
                           f"（请确认 Zotero 正在运行且设置中允许本地 API）")
    return resp
