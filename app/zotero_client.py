"""Zotero 本地 API 客户端（B 线 parallel_v03）。

通道核查结论（2026-10 本机实测，Zotero 10.0.3，证据见
evidence/zotero_channel_check.md）：
- 读：GET http://127.0.0.1:23119/api/users/0/... 直接可用（3130 items）。
- 写：本地 API 写端点存在且受控开放，需两道凭据：
  1. `Zotero-Server-ID` 请求头（值来自任意响应头，防跨进程误写）；
  2. API key —— 经 `POST /api/local/authorize`（body {"appName": ...}）
     本地授权获得，Zotero 会在界面弹窗等待用户确认。
- 另存在 Connector 桥接 `POST /connector/saveItems`（无需 key），但只写入
  Zotero 当前选中集合、无法指定目标集合，不用于受控写回，仅作探测。
- 绝不直接 SQL 写 zotero.sqlite（硬红线）。

API key 只经环境变量 PAPERNET_ZOTERO_API_KEY（服务端注入）或内存中的
authorize() 流程获取，不写入源码/日志/JSON/交付。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

try:
    from .config import ZOTERO_BASE
except ImportError:  # 独立脚本场景
    ZOTERO_BASE = DEFAULT_BASE

API_KEY_ENV = "PAPERNET_ZOTERO_API_KEY"
KEY_RE = re.compile(r"^[A-Za-z0-9]+$")
CONNECTOR_API_VERSION = "3"

# 优先级0（parallel_v04_r1 凭据日志规则，真实事故：授权响应 key 落入
# probe5 日志）：授权端点在任何日志/调试/传输记录中只允许出现
# 「成功布尔 + 响应字段名」，禁止原始响应正文与任何字段值。
AUTHORIZE_PATH = "/api/local/authorize"

# 传输层/客户端日志（绝不含请求/响应正文；授权响应经 sanitize_authorize_response
# 脱敏后只记成功布尔与字段名）。测试可经 assertLogs 捕获验证无凭据泄漏。
log = logging.getLogger("papernet.zotero_client")
wire_log = logging.getLogger("papernet.zotero_wire")

# 敏感字段名（用于通用日志脱敏 redact_secrets：只保留是否非空的布尔，不留值）
SENSITIVE_FIELD_RE = re.compile(
    r"(api[_-]?key|(^|_)key($|_)|token|secret|credential|authorization|password)",
    re.IGNORECASE)

# V4-01（parallel_v04）：URL 协议修正。
# 基址不带 /api 尾巴：ping → GET {base}/connector/ping；
# 本地 API → {base}/api/users/0/...；authorize → POST {base}/api/local/authorize。
# （真实 Zotero 实测：/api/connector/ping 与 /api/api/... 均为 404。）
DEFAULT_BASE = "http://127.0.0.1:23119"


def normalize_base(base: str) -> str:
    """去掉基址尾部的 /api（历史配置 http://127.0.0.1:23119/api 兼容修正）。"""
    b = (base or DEFAULT_BASE).rstrip("/")
    if b.endswith("/api"):
        b = b[: -len("/api")]
    return b or DEFAULT_BASE


def normalize_doi(doi) -> str | None:
    """DOI 归一化：小写、去 https://doi.org/ / http://dx.doi.org/ / doi: 前缀。"""
    if not doi:
        return None
    d = str(doi).strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d).strip()
    return d or None


class ZoteroError(Exception):
    """Zotero 请求失败（含 HTTP 非 2xx）。"""


class ZoteroUnavailable(ZoteroError):
    """Zotero 本地服务不可达。"""


class ZoteroAuthRequired(ZoteroError):
    """写通道缺 API key / 授权（401）。只阻塞写入步骤。"""


class ZoteroConflict(ZoteroError):
    """版本冲突（409）或并发修改。"""


def _default_send(method, url, headers, data, timeout):
    """stdlib 传输层，测试可注入替换。"""
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read() if e.fp else b""
    except (urllib.error.URLError, OSError) as e:
        raise ZoteroUnavailable(f"Zotero 本地服务不可达: {e}") from e


def parse_json_body(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None


def sanitize_authorize_response(data) -> dict:
    """优先级0：授权响应的日志安全形状。

    只输出「成功布尔 + 响应字段名」，绝不输出任何字段值/原始正文：
      {"authorized": bool, "response_fields": [字段名...]}
    authorized 仅在响应含非空、合法形状的 key 字段（apiKey/key）时为 True。
    """
    if not isinstance(data, dict):
        return {"authorized": False, "response_fields": []}
    authorized = False
    for f in ("apiKey", "key"):
        v = data.get(f)
        if isinstance(v, str) and v and KEY_RE.match(v):
            authorized = True
            break
    return {"authorized": authorized,
            "response_fields": sorted(str(k) for k in data.keys())}


def redact_secrets(obj):
    """通用日志脱敏：递归遍历 dict/list，敏感字段名（key/token/secret 等）
    只保留「是否非空」的占位布尔，绝不保留值。供日志/调试/传输记录使用。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and SENSITIVE_FIELD_RE.search(k):
                out[k] = "<redacted:nonempty>" if v else "<redacted:empty>"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [redact_secrets(x) for x in obj]
    return obj


def sanitize_wire_record(method: str, path: str, status: int) -> dict:
    """传输层日志的安全形状：只有方法/路径/状态码，绝无请求或响应正文。"""
    return {"method": method, "path": path, "status": status}


class ZoteroClient:
    """最小可用 Zotero 本地 API 客户端（读 + 受控写）。"""

    def __init__(self, base: str = ZOTERO_BASE, api_key: str | None = None,
                 timeout: float = 8.0, send=None):
        # V4-01：兼容历史基址（/api 尾巴），统一归一化
        self.base = normalize_base(base)
        self.api_key = api_key or os.environ.get(API_KEY_ENV) or None
        self.timeout = timeout
        self._send = send or _default_send
        self._server_id: str | None = None

    # ------------------------------------------------------------- 基础请求
    def _request(self, method: str, path: str, *, json_body=None,
                 extra_headers: dict | None = None, auth: bool = False):
        url = f"{self.base}{path}"
        headers = {"Accept": "application/json"}
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            # 写请求：需要 Server-ID + API key（读请求不需要）
            self._ensure_server_id()
            if self._server_id:
                headers["Zotero-Server-ID"] = self._server_id
            if not self.api_key:
                raise ZoteroAuthRequired(
                    "缺少 Zotero 本地 API key：请运行 authorize() 并在 Zotero "
                    "界面确认，或经环境变量 PAPERNET_ZOTERO_API_KEY 注入")
            headers["Zotero-API-Key"] = self.api_key
        if extra_headers:
            headers.update(extra_headers)
        status, resp_headers, raw = self._send(method, url, headers, body, self.timeout)
        sid = resp_headers.get("Zotero-Server-ID")
        if sid:
            self._server_id = sid
        if status == 401:
            raise ZoteroAuthRequired("Zotero 本地 API 要求 API key（401）；"
                                     "请先本地授权获取 key")
        if status in (409, 428):
            # 409 版本冲突；428 并发/预条件失败（真实 Zotero 本地 API 观测值）
            raise ZoteroConflict(f"版本冲突（{status}）："
                                 f"{parse_json_body(raw) or raw[:200]}")
        if status == 412:
            raise ZoteroConflict(f"预条件失败（412，版本过期）："
                                 f"{parse_json_body(raw) or raw[:200]}")
        if status >= 400:
            raise ZoteroError(f"Zotero {method} {path} -> {status}: "
                              f"{parse_json_body(raw) or raw[:200]}")
        parsed = parse_json_body(raw)
        # 优先级0：传输层日志只有 method/path/status；授权端点只记成功布尔
        # 与响应字段名（绝无原始响应正文/字段值）。
        if wire_log.isEnabledFor(logging.DEBUG):
            wire_log.debug("zotero wire %s",
                           json.dumps(sanitize_wire_record(method, path, status),
                                      ensure_ascii=False))
        if path == AUTHORIZE_PATH:
            info = sanitize_authorize_response(parsed)
            log.info("Zotero 本地授权: authorized=%s response_fields=%s",
                     info["authorized"], info["response_fields"])
        return status, resp_headers, parsed

    # ------------------------------------------------------------- 读端点
    def _ensure_server_id(self) -> str | None:
        """确保已取得 Zotero-Server-ID：先 ping；ping 响应未携带时（本机
        实测 /connector/ping 无该头）再经一次只读 /api 请求捕获。"""
        if self._server_id:
            return self._server_id
        self.ping()
        if self._server_id:
            return self._server_id
        self._request("GET", "/api/users/0/items?limit=1")
        return self._server_id

    def ping(self) -> dict:
        try:
            status, headers, _ = self._request("GET", "/connector/ping")
        except ZoteroUnavailable:
            raise
        return {"ok": status == 200,
                "server_id": headers.get("Zotero-Server-ID"),
                "version": headers.get("X-Zotero-Version")}

    def get_item(self, item_key: str) -> dict | None:
        if not KEY_RE.match(item_key or ""):
            raise ZoteroError(f"非法 item key: {item_key!r}")
        _, _, data = self._request("GET", f"/api/users/0/items/{item_key}")
        return data

    def get_children(self, item_key: str) -> list[dict]:
        """读取条目的子项（笔记/附件），用于回读核验与孤儿笔记认领。"""
        if not KEY_RE.match(item_key or ""):
            raise ZoteroError(f"非法 item key: {item_key!r}")
        _, _, data = self._request(
            "GET", f"/api/users/0/items/{item_key}/children")
        return data or []

    def search_items(self, query: str, limit: int = 20) -> list[dict]:
        from urllib.parse import quote
        _, _, data = self._request(
            "GET", f"/api/users/0/items?q={quote(query)}&limit={limit}"
                   "&itemType=-attachment&format=json")
        return data or []

    def get_collections(self) -> list[dict]:
        _, _, data = self._request(
            "GET", "/api/users/0/collections?limit=100&format=json")
        return data or []

    def find_by_identifier(self, doi: str | None = None,
                           title: str | None = None) -> dict | None:
        """按标识查找已有条目（V4-02 身份保护，§3.3）。

        - DOI 归一化（小写、去 https://doi.org/ 前缀）后精确比较；
        - 请求 DOI 与题名相同候选条目的 DOI 不同 → 返回显式冲突对象
          {"conflict": True, ...}（调用方记 conflict，绝不按题名复用/补写）；
        - 无强标识（或 DOI 检索未命中）时的题名匹配保守：题名完全相等，
          且对方无 DOI 或对方 DOI 与请求 DOI 归一化后相等。
        """
        req_doi = normalize_doi(doi)
        req_title = (title or "").strip()
        if req_doi:
            conflict_item = None
            for it in self.search_items(doi, limit=20):
                d = it.get("data") or {}
                item_doi = normalize_doi(d.get("DOI"))
                if item_doi == req_doi:
                    return it
                if req_title and (d.get("title") or "").strip() == req_title:
                    conflict_item = it
            if conflict_item is not None:
                cd = conflict_item.get("data") or {}
                return {"conflict": True,
                        "reason": "题名相同但 DOI 不一致，拒绝按题名复用",
                        "requested_doi": req_doi,
                        "found_doi": normalize_doi(cd.get("DOI")),
                        "item_key": conflict_item.get("key"),
                        "item": conflict_item}
        if req_title:
            for it in self.search_items(title, limit=20):
                d = it.get("data") or {}
                if (d.get("title") or "").strip() != req_title:
                    continue
                item_doi = normalize_doi(d.get("DOI"))
                if item_doi is None or (req_doi and item_doi == req_doi):
                    return it
                if req_doi:
                    return {"conflict": True,
                            "reason": "题名相同但 DOI 不一致，拒绝按题名复用",
                            "requested_doi": req_doi, "found_doi": item_doi,
                            "item_key": it.get("key"), "item": it}
                # 请求无 DOI 而候选有 DOI：不匹配（保守）
        return None

    # ------------------------------------------------------------- 写端点
    def authorize(self, app_name: str, timeout: float = 120.0) -> str | None:
        """发起本地授权；Zotero 界面弹窗确认后返回 API key。

        V4-01：POST /api/local/authorize 前先取 Zotero-Server-ID，
        并在请求头携带（防跨进程误写）。用户拒绝/超时/取消 → 返回
        None（不伪造成功）。key 只保留在内存。
        """
        self._ensure_server_id()
        extra = {"Zotero-Server-ID": self._server_id} if self._server_id else None
        saved = self.timeout
        self.timeout = timeout
        try:
            _, _, data = self._request(
                "POST", "/api/local/authorize",
                json_body={"appName": app_name}, auth=False,
                extra_headers=extra)
        finally:
            self.timeout = saved
        if isinstance(data, dict):
            key = data.get("apiKey") or data.get("key")
            if key and KEY_RE.match(str(key)):
                self.api_key = str(key)
                return self.api_key
        return None

    def create_collection(self, name: str) -> str | None:
        _, _, data = self._request(
            "POST", "/api/users/0/collections", auth=True,
            json_body=[{"name": name}])
        return _extract_key(data, 0)

    def create_items(self, items: list[dict]) -> dict:
        """批量新建条目，返回 {"success": {idx: key}, "failed": {idx: reason}}。"""
        _, _, data = self._request(
            "POST", "/api/users/0/items", auth=True, json_body=items)
        return _normalize_write_response(data)

    def update_item(self, item_key: str, patch: dict, version: int | None = None):
        """PATCH 更新条目（仅补缺失字段用）；带版本号供冲突检测。"""
        if version is not None:
            patch = dict(patch)
            patch["version"] = version
        _, _, data = self._request(
            "PATCH", f"/api/users/0/items/{item_key}", auth=True, json_body=patch)
        return data

    def write_ready(self) -> bool:
        """写通道是否具备凭据（不真正写）。"""
        if not self.api_key:
            return False
        try:
            self.ping()
            return True
        except ZoteroUnavailable:
            return False


# ------------------------------------------------------------------ 工具
def _extract_key(write_response, idx: int):
    if not isinstance(write_response, dict):
        return None
    success = write_response.get("success") or {}
    k = success.get(str(idx))
    if k:
        return k
    successful = write_response.get("successful") or {}
    item = successful.get(str(idx)) or {}
    return item.get("key")


def _normalize_write_response(data) -> dict:
    out = {"success": {}, "failed": {}}
    if not isinstance(data, dict):
        out["failed"]["0"] = f"unexpected response: {data!r}"[:300]
        return out
    success = data.get("success") or {}
    if isinstance(success, dict):
        out["success"] = {k: (v if isinstance(v, str) else (v or {}).get("key"))
                          for k, v in success.items()}
    successful = data.get("successful") or {}
    if isinstance(successful, dict):
        for k, v in successful.items():
            if k not in out["success"] and isinstance(v, dict):
                out["success"][k] = v.get("key")
    failed = data.get("failed") or {}
    if isinstance(failed, dict):
        out["failed"] = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)[:300])
                         for k, v in failed.items()}
    return out


def build_item_payload(paper: dict, collection_key: str | None = None) -> dict:
    """把应用内 papers 记录映射为 Zotero item（不包含本地 PDF/笔记内容）。"""
    authors = paper.get("authors") or []
    if isinstance(authors, str):
        try:
            authors = json.loads(authors)
        except (ValueError, TypeError):
            authors = [authors]
    creators = []
    for a in authors:
        if isinstance(a, dict):
            if a.get("name"):
                creators.append({"creatorType": "author",
                                 "name": str(a["name"])[:300]})
            elif a.get("first") or a.get("last"):
                creators.append({"creatorType": "author",
                                 "firstName": str(a.get("first") or "")[:200],
                                 "lastName": str(a.get("last") or "")[:200]})
        elif isinstance(a, str) and a.strip():
            parts = a.strip().rsplit(" ", 1)
            if len(parts) == 2:
                creators.append({"creatorType": "author",
                                 "firstName": parts[0], "lastName": parts[1]})
            else:
                creators.append({"creatorType": "author", "name": a.strip()})
    tags = paper.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (ValueError, TypeError):
            tags = [tags]
    item = {
        "itemType": "journalArticle",
        "title": (paper.get("title") or "")[:2000],
        "creators": creators[:50],
        "abstractNote": (paper.get("abstract") or "")[:20000],
        "publicationTitle": (paper.get("venue") or "")[:500],
        "DOI": paper.get("doi") or "",
        "url": paper.get("oa_pdf_url") or "",
        "date": str(paper["year"]) if paper.get("year") else "",
        "tags": [{"tag": str(t)[:200]} for t in tags if t][:20],
    }
    if collection_key:
        item["collections"] = [collection_key]
    return item
