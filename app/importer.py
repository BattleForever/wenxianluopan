"""导入器：DOI / BibTeX / RIS / PDF 文件夹 / Zotero 本地库同步。

与 citations.py 相同的两阶段约束：联网/读文件阶段不持有 SQLite 写事务，
全部写回放在最后单事务完成（netclient 缓存写入需要独立短事务）。
"""

import html
import hashlib
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

import bibtexparser

from .db import get_db, get_setting
from .netclient import (
    OfflineError,
    oa_search_title,
    oa_work_by_doi,
    oa_work_to_paper,
    zotero_get,
)
from .store import add_citation, find_existing, norm_doi, norm_title, upsert_paper, upsert_venue

log = logging.getLogger("papernet.importer")

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")


def _remember(stats,pid):
    ids=stats.setdefault('paper_ids',[])
    if pid not in ids:ids.append(pid)


def extract_dois(text):
    # Separate records by whitespace or a delimiter before the next DOI, never
    # split semicolons within a DOI suffix (older journal identifiers use them).
    value=re.sub(r'[,;，；](?=\s*(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.)','\n',text or '',flags=re.I)
    out=[]
    for match in DOI_RE.finditer(value):
        d=norm_doi(match[1].rstrip('.,;'))
        while d.endswith(')') and d.count(')')>d.count('('):d=d[:-1]
        if d not in out:out.append(d)
    return out


# ------------------------------------------------------------------ DOI

def import_dois(text: str):
    dois = extract_dois(text)
    if not dois:raise ValueError('未识别到 DOI。请粘贴 10. 开头的 DOI 或 doi.org 链接，每行一条。')
    stats = {"total": len(dois), "added": 0, "updated": 0, "not_found": [], "errors": []}

    pairs = []
    for doi in dois:
        # Reuse an existing local record even when the network is unavailable.
        with get_db() as conn:
            existing=find_existing(conn,doi=doi)
            if existing:
                conn.execute("UPDATE papers SET status='library' WHERE id=?",(existing['id'],))
                stats['updated']+=1
                _remember(stats,existing['id'])
                continue
        p=None
        try:
            work=oa_work_by_doi(doi)
            if work:p=oa_work_to_paper(work)
        except Exception:pass
        if not p:
            try:
                from .metadata_sources import crossref
                p=crossref(doi)
            except Exception:pass
        if p and norm_doi(p.get('doi'))==doi:pairs.append((doi,p))
        else:stats['not_found'].append(doi);stats['errors'].append(f'{doi}：未取得可核对的书目，请检查网络，或改用 BibTeX、RIS、PDF、Zotero 导入。')

    with get_db() as conn:  # 写回阶段
        for doi,p in pairs:
            try:
                pid,created=upsert_paper(conn,p,source='doi',status='library')
                stats['added' if created else 'updated']+=1
                _remember(stats,pid)
            except ValueError:stats['errors'].append(f'{doi}：与已有文献标识冲突，未合并。')
    return stats


# ------------------------------------------------------------------ BibTeX / RIS

def _clean(s):
    """导入文本清洗：反转义 HTML 实体（&amp; 等）并去掉首尾空白。
    实体残留会让 norm_venue 归一化后多出 'amp'，破坏期刊等级匹配。"""
    if not isinstance(s, str):
        return s
    return html.unescape(s).strip()


def _entry_to_paper(e: dict) -> dict:
    authors = []
    raw_authors = e.get("author") or e.get("authors") or ""
    for a in re.split(r"\s+and\s+|;", raw_authors):
        a = a.strip()
        if a:
            authors.append(a)
    year = e.get("year") or ""
    m = re.search(r"\d{4}", str(year))
    return {
        "doi": e.get("doi"),
        "title": _clean(e.get("title") or ""),
        "authors": authors,
        "year": int(m.group(0)) if m else None,
        "venue": _clean(e.get("journal") or e.get("booktitle") or e.get("journaltitle")
                        or e.get("publisher") or ""),
        "abstract": _clean(e.get("abstract") or e.get("summary") or ""),
        "tags": [k for k in (e.get("keywords") or "").split(";") if k.strip()],
    }


def import_bibtex(text: str):
    db = bibtexparser.loads((text or '').lstrip('\ufeff'))
    if not db.entries:raise ValueError('未识别到 BibTeX 条目。请选择 .bib 文件，或粘贴以 @article、@inproceedings 等开头的完整内容。')
    stats = {"total": len(db.entries), "added": 0, "updated": 0, "errors": []}
    with get_db() as conn:
        for e in db.entries:
            try:
                if not e.get('title') and not e.get('doi'):raise ValueError('条目缺少题名和 DOI，未导入')
                pid, created = upsert_paper(conn, _entry_to_paper(e), source="bibtex",
                                          status="library")
                stats["added" if created else "updated"] += 1
                _remember(stats,pid)
            except Exception as ex:  # noqa: BLE001
                stats["errors"].append(str(ex))
    return stats


def _parse_ris(text: str):
    entries = []
    cur = None
    last_tag=None
    for line in (text or "").lstrip('\ufeff').splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s{2}-\s?(.*)$", line)
        if not m:
            if cur is not None and last_tag and line.strip():cur[last_tag][-1]+=' '+line.strip()
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "TY":
            if cur is not None:entries.append(cur)
            cur = defaultdict(list)
            last_tag=None
        elif tag == "ER":
            if cur is not None:
                entries.append(cur)
            cur = None
        elif cur is not None:
            cur[tag].append(val)
            last_tag=tag
    if cur is not None:entries.append(cur)
    papers = []
    for e in entries:
        title = (e.get("TI") or e.get("T1") or [""])[0]
        year_s = (e.get("PY") or e.get("Y1") or [""])[0]
        m = re.search(r"\d{4}", year_s)
        papers.append({
            "doi": (e.get("DO") or [None])[0],
            "title": _clean(title),
            "authors": (e.get("AU") or e.get("A1") or []),
            "year": int(m.group(0)) if m else None,
            "venue": _clean((e.get("JO") or e.get("JF") or e.get("JA") or e.get("T2") or [None])[0]),
            "abstract": _clean((e.get("AB") or e.get("N2") or [None])[0]),
            "tags": e.get("KW") or [],
        })
    return papers


def import_ris(text: str):
    papers = _parse_ris(text)
    if not papers:raise ValueError('未识别到 RIS 条目。请选择 .ris 文件，或粘贴包含 TY  - 与 TI  - 的内容。')
    stats = {"total": len(papers), "added": 0, "updated": 0, "errors": []}
    with get_db() as conn:
        for p in papers:
            try:
                if not p.get('title') and not p.get('doi'):raise ValueError('条目缺少题名和 DOI，未导入')
                pid, created = upsert_paper(conn, p, source="ris", status="library")
                stats["added" if created else "updated"] += 1
                _remember(stats,pid)
            except Exception as ex:  # noqa: BLE001
                stats["errors"].append(str(ex))
    return stats


# ------------------------------------------------------------------ PDF 文件夹

def _pdf_text_head(path: Path, pages=3) -> str:
    from .pdf_engine import PDFDocument
    with PDFDocument(path) as doc:
        return '\n'.join(doc.page_text(i) for i in range(min(pages,len(doc))))


def _match_paper(doi=None, title=None):
    """联网匹配 OpenAlex，返回 paper dict 或 None（只联网，不写库）。"""
    if doi:
        try:
            w = oa_work_by_doi(doi)
        except OfflineError:
            raise
        except Exception:  # noqa: BLE001
            w = None
        if w:
            return oa_work_to_paper(w)
    if title and len(title) > 15:
        try:
            results = oa_search_title(title, 3)
        except Exception:  # noqa: BLE001
            results = []
        import difflib
        for w in results:
            t1 = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (w.get("display_name") or "").lower())
            t2 = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (title or "").lower())
            if t1 and t2 and difflib.SequenceMatcher(None, t1, t2).ratio() >= 0.90:
                return oa_work_to_paper(w)
    return None


def scan_pdf_folder(folder: str):
    root = Path(folder.strip().strip('"')).expanduser()
    if not root.is_dir():
        raise ValueError('文件夹不存在。请使用“选择文件夹”，或填写这台电脑上的完整路径。')
    pdfs = sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower()=='.pdf')
    if not pdfs:raise ValueError('这个文件夹及其子文件夹中没有 PDF 文件。')
    return import_pdf_files(pdfs)


def import_pdf_files(pdfs,title_override=None):
    with get_db() as conn:conn.execute('CREATE TABLE IF NOT EXISTS import_pdf_fingerprints(sha256 TEXT PRIMARY KEY,paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE)')
    stats = {"total": len(pdfs), "matched": 0, "title_only": 0, "failed": 0,
             "updated": 0, "errors": []}

    collected = []  # (paper_dict, kind)，联网阶段
    for pdf in pdfs:
        try:
            digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
            text = _pdf_text_head(pdf)
            m = DOI_RE.search(text)
            doi = norm_doi(m.group(1).rstrip(".,;)")) if m else None
            try:p = _match_paper(doi=doi) if doi else None
            except Exception:p=None
            if p:
                p["local_pdf_path"] = str(pdf)
                collected.append((p, "matched",digest))
                continue
            doc_title = ""
            try:
                from .pdf_engine import PDFDocument
                with PDFDocument(pdf) as doc:
                    doc_title = (doc.metadata or {}).get("title") or ""
            except Exception:  # noqa: BLE001
                pass
            meta_title = (title_override or pdf.stem or "").replace("_", " ").strip()
            best_title = doc_title.strip() if len(doc_title.strip()) > 15 else meta_title
            if best_title:
                collected.append(({"title": best_title,"doi":doi, "local_pdf_path": str(pdf)},
                                  "title_only",digest))
            else:
                stats["failed"] += 1
        except OfflineError:
            raise
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            stats["errors"].append(f"{pdf.name}: {e}")

    with get_db() as conn:  # 写回阶段
        for p, kind,digest in collected:
            existing=conn.execute('SELECT p.id FROM papers p JOIN import_pdf_fingerprints f ON p.id=f.paper_id WHERE f.sha256=?',(digest,)).fetchone()
            if existing:
                pid=existing[0];created=False;conn.execute("UPDATE papers SET status='library' WHERE id=?",(pid,))
            else:pid,created=upsert_paper(conn,p,source='pdf',status='library')
            conn.execute('INSERT OR IGNORE INTO import_pdf_fingerprints VALUES(?,?)',(digest,pid))
            _remember(stats,pid)
            if kind == "matched":
                stats["matched"] += 1 if created else 0
                if not created:
                    stats["updated"] += 1
            else:
                stats['title_only' if created else 'updated'] += 1
    return stats


# ------------------------------------------------------------------ 标准数据表（自采集 JSONL）

_PAPER_KEYS = (
    "doi", "title", "title_zh", "authors", "year", "venue", "abstract",
    "abstract_zh", "keywords", "tags", "cited_by_count", "type",
    "oa_pdf_url", "local_pdf_path", "openalex_id", "references", "status",
    "collections", "zotero_key", "language", "note", "venue_meta",
)

_VENUE_KEYS = ("name", "kind", "impact_factor", "jcr_zone", "cas_zone",
               "cas_top", "cas_sub", "ccf_tier", "source", "note")


def _parse_jsonl(text: str):
    """解析 JSONL，返回 (records, errors)；errors 为 (行号, 原因)。容忍首行 BOM。"""
    records, errors = [], []
    for i, line in enumerate((text or "").splitlines(), 1):
        line = line.strip().lstrip("\ufeff").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                errors.append((i, "不是 JSON 对象"))
            else:
                records.append((i, obj))
        except ValueError as e:
            errors.append((i, f"JSON 解析失败: {e}"))
    return records, errors


def _std_to_paper(obj: dict) -> dict:
    """标准表一行 → upsert_paper 的 paper dict。多余键忽略。"""
    authors = obj.get("authors")
    if isinstance(authors, str):
        authors = [a.strip() for a in re.split(r"[;；,，]| and ", authors) if a.strip()]
    tags = obj.get("tags") or obj.get("keywords")
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[;；,，]", tags) if t.strip()]
    meta = {}
    if obj.get("note"):
        meta["note"] = str(obj["note"])
    if obj.get("language"):
        meta["language"] = str(obj["language"])
    if obj.get("zotero_key"):
        meta["zotero_key"] = str(obj["zotero_key"])
    status = obj.get("status")
    status = status if status in ("library", "candidate") else "library"
    cols = obj.get("collections")
    if isinstance(cols, str):
        cols = [c.strip() for c in re.split(r"[;；,，]", cols) if c.strip()]
    vm = obj.get("venue_meta")
    vm = vm if isinstance(vm, dict) else None
    return {
        "doi": obj.get("doi"),
        "title": _clean(obj.get("title")),
        "title_zh": _clean(obj.get("title_zh")),
        "authors": authors or [],
        "year": obj.get("year"),
        "venue": _clean(obj.get("venue")),
        "abstract": _clean(obj.get("abstract")),
        "abstract_zh": _clean(obj.get("abstract_zh")),
        "tags": tags or [],
        "cited_by_count": obj.get("cited_by_count") or 0,
        "type": obj.get("type"),
        "oa_pdf_url": obj.get("oa_pdf_url"),
        "local_pdf_path": obj.get("local_pdf_path"),
        "openalex_id": obj.get("openalex_id"),
        "meta": meta,
        "_status": status,
        "_collections": [c for c in (cols or []) if c][:10],
        "_venue_meta": vm,
    }


def _std_venue(obj: dict) -> dict:
    """标准表一行 venue 记录 → upsert_venue 的 dict（多余键忽略）。"""
    v = {k: obj.get(k) for k in _VENUE_KEYS}
    note = v.pop("note", None)
    if note:
        v["extra_json"] = json.dumps({"note": str(note)}, ensure_ascii=False)
    return v


def _ref_to_key(ref):
    """references 里的一项 → ('doi'|'title', 值)。支持字符串或 {doi|title}。"""
    if isinstance(ref, dict):
        if ref.get("doi"):
            return ("doi", str(ref["doi"]))
        if ref.get("title"):
            return ("title", str(ref["title"]))
        return None
    s = str(ref).strip()
    if not s:
        return None
    if re.match(r"^10\.\d{4,9}/", norm_doi(s) or ""):
        return ("doi", s)
    return ("title", s)


def import_standard(papers_text: str = "", citations_text: str = "",
                    venues_text: str = ""):
    """导入自采集标准数据表（JSONL），并按 DOI/标题解析引用边。

    papers.jsonl 每行一篇文献；citations.jsonl 每行 {"citing": ..., "cited": ...}；
    venues.jsonl 每行一个期刊/会议等级记录。引用两端支持 DOI（推荐）或与 papers
    表一致的标题。
    """
    stats = {"papers_total": 0, "added": 0, "updated": 0, "paper_errors": [],
             "citation_lines": 0, "edges_added": 0, "unresolved": [],
             "unresolved_total": 0, "errors": [], "venues_total": 0,
             "venues_added": 0, "venues_updated": 0,
             "skipped_self_loops": 0, "identity_conflicts": [],
             "invalid_endpoints": 0, "rejected_refs_skipped": 0}

    # ---- Phase 1：纯解析，不碰数据库 ----
    paper_recs, paper_errs = _parse_jsonl(papers_text)
    for i, why in paper_errs:
        stats["paper_errors"].append(f"第{i}行: {why}")
    parsed = []
    for i, obj in paper_recs:
        unknown = [k for k in obj if k not in _PAPER_KEYS]
        p = _std_to_paper(obj)
        refs = [_ref_to_key(r) for r in (obj.get("references") or [])]
        parsed.append((p, [r for r in refs if r], unknown))

    cit_recs, cit_errs = _parse_jsonl(citations_text)
    for i, why in cit_errs:
        stats["errors"].append(f"citations 第{i}行: {why}")
    cit_pairs = []
    for i, obj in cit_recs:
        stats["citation_lines"] += 1
        a = _ref_to_key(obj.get("citing"))
        b = _ref_to_key(obj.get("cited"))
        if a and b:
            cit_pairs.append((a, b))
        else:
            stats["errors"].append(f"citations 第{i}行: 缺少 citing/cited")
    stats["papers_total"] = len(parsed)

    venue_recs, venue_errs = _parse_jsonl(venues_text)
    for i, why in venue_errs:
        stats["errors"].append(f"venues 第{i}行: {why}")
    venue_objs = []
    for i, obj in venue_recs:
        unknown_v = [k for k in obj if k not in _VENUE_KEYS]
        if unknown_v:
            stats["errors"].append(f"venues 第{i}行: 忽略未知字段 {unknown_v[:5]}")
        venue_objs.append(_std_venue(obj))
    stats["venues_total"] = len(venue_objs)

    # ---- Phase 2：单事务写回 ----
    with get_db() as conn:
        doi_map = {}
        col_cache = {}
        # 期刊/会议等级先入库（ papers 循环里行内 venue_meta 也会汇到这里）
        for v in venue_objs:
            try:
                _, v_created = upsert_venue(conn, v)
                stats["venues_added" if v_created else "venues_updated"] += 1
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"venue {str(v.get('name'))[:30]}: {e}")
        resolution = []  # 与 parsed 一一对应：是否成功入库、唯一内部 ID、可否建边
        for p, refs, unknown in parsed:
            try:
                if not p.get('title') and not p.get('doi'):raise ValueError('条目缺少题名和 DOI，未导入')
                status = p.pop("_status", "library") or "library"
                collections = p.pop("_collections", [])
                vm = p.pop("_venue_meta", None)
                pid, created = upsert_paper(conn, p, source="standard",
                                            status=status)
                if isinstance(vm, dict):
                    vm = dict(vm)
                    vm.setdefault("name", p.get("venue"))
                    if vm.get("name"):
                        try:
                            _, v_created = upsert_venue(conn, vm)
                            stats["venues_added" if v_created else "venues_updated"] += 1
                        except Exception as e:  # noqa: BLE001
                            stats["errors"].append(f"venue_meta: {e}")
                # 自动建收藏集并入集（最多 10 个/篇）
                for cname in collections:
                    cid = col_cache.get(cname)
                    if cid is None:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO collections(name) VALUES(?)",
                            (cname,),
                        )
                        row = conn.execute(
                            "SELECT id FROM collections WHERE name=?", (cname,)
                        ).fetchone()
                        if not row:
                            continue
                        cid = row["id"]
                        col_cache[cname] = cid
                    conn.execute(
                        "INSERT OR IGNORE INTO collection_items(collection_id, "
                        "paper_id) VALUES(?, ?)", (cid, pid),
                    )
                stats["added" if created else "updated"] += 1
                _remember(stats,pid)
                if p.get("doi"):
                    doi_map[norm_doi(p["doi"])] = pid
                # 题名缓存已移除（02A-R1）：同批同名/跨批同名一律走数据库
                # 层的 title_ambiguous 歧义检查，结果不依赖输入顺序。
                resolution.append({"ok": True, "paper_id": pid, "reason": None})
                if unknown:
                    stats["errors"].append(
                        f"{(p.get('title') or '')[:30]}: 忽略未知字段 {unknown[:5]}")
            except Exception as e:  # noqa: BLE001
                stats["paper_errors"].append(f"{(p.get('title') or '')[:40]}: {e}")
                resolution.append({"ok": False, "paper_id": None, "reason": str(e)})

        def resolve(key):
            kind, val = key
            conf = {}
            if kind == "doi":
                d = norm_doi(val)
                if d in doi_map:
                    return doi_map[d]
                row = find_existing(conn, doi=d, conflict=conf)
            else:
                # 题名端点不使用批内缓存（02A-R1）：同批与跨批共用数据库
                # 局的歧义检查，不任选同名记录。
                row = find_existing(conn, title=val, conflict=conf)
            if conf:
                stats["identity_conflicts"].append(
                    f"{kind}:{str(val)[:50]}: {conf.get('detail', conf.get('type', ''))}")
            if row:
                if kind == "doi":
                    doi_map[norm_doi(val)] = row["id"]
                return row["id"]
            return None

        def add_edge(src_id, tgt_id, src_name):
            """统一引文边写入：自引用/无效端点显式分类计数，不报成新增。"""
            if src_id == tgt_id:
                stats["skipped_self_loops"] += 1
                return False
            rs = {}
            ok = add_citation(conn, src_id, tgt_id, src_name, reason=rs)
            if not ok and rs.get("cause") == "invalid_endpoint":
                stats["invalid_endpoints"] += 1
            return ok

        for (p, refs, _), res in zip(parsed, resolution):
            if not res.get("ok"):
                # 身份冲突/被拒条目：其内嵌引用不参与建边，也不得再按
                # DOI/题名重新解析起点（02A-R1）。跳过原因可核对。
                stats["rejected_refs_skipped"] += len(refs)
                stats["errors"].append(
                    f"{(p.get('title') or '')[:30]}: 条目未入库"
                    f"（{(res.get('reason') or '')[:80]}），"
                    f"跳过 {len(refs)} 条内嵌引用建边")
                continue
            # 已成功入库条目直接使用其唯一内部 ID 作为边的起点
            src_id = res["paper_id"]
            for key in refs:
                tgt = resolve(key)
                if tgt:
                    stats["edges_added"] += int(add_edge(src_id, tgt, "standard"))
                else:
                    stats["unresolved"].append(
                        f"{(p.get('title') or '')[:30]} -> {key[1][:60]}")

        for (a, b) in cit_pairs:
            src_id, dst_id = resolve(a), resolve(b)
            if src_id and dst_id:
                stats["edges_added"] += int(add_edge(src_id, dst_id, "standard"))
            else:
                stats["unresolved"].append(f"{a[1][:50]} -> {b[1][:50]}")

        # 每条解析数据是否成功、其唯一内部 ID、是否可参与建边（截断留存）
        stats["paper_link_status_total"] = len(resolution)
        stats["paper_link_status"] = [
            {"title": (pp[0].get("title") or "")[:40],
             "paper_id": r.get("paper_id"),
             "can_link": bool(r.get("ok")),
             "reason": (r.get("reason") or "")[:100]}
            for pp, r in zip(parsed, resolution)][:50]

    stats["unresolved_total"] = len(stats["unresolved"])
    stats["unresolved"] = stats["unresolved"][:20]
    stats["identity_conflicts_total"] = len(stats["identity_conflicts"])
    stats["identity_conflicts"] = stats["identity_conflicts"][:20]
    return stats


# ------------------------------------------------------------------ Zotero

def _zotero_item_to_paper(item: dict):
    data = item.get("data", {})
    if data.get('deleted') or data.get("itemType") in ("attachment", "note", "annotation"):
        return None
    if not data.get('title') and not data.get('DOI'):return None
    doi = norm_doi(data.get("DOI") or data.get('doi'))
    date = str(data.get("date") or "")
    m = re.search(r"\d{4}", date)
    authors = []
    for c in data.get("creators", []):
        if c.get("creatorType") in ("author", "inventor"):
            name = " ".join(x for x in [c.get("firstName"), c.get("lastName")] if x)
            if not name:
                name = c.get("name", "")
            if name:
                authors.append(name)
    key = data.get("key") or item.get('key','')
    if key and not re.match(r"^[A-Za-z0-9]+$", key):
        key = ""
    return {
        "doi": doi,
        "title": data.get("title"),
        "authors": authors,
        "year": int(m.group(0)) if m else None,
        "venue": data.get("publicationTitle") or data.get("proceedingsTitle"),
        "abstract": data.get("abstractNote"),
        "tags": [t.get("tag") for t in data.get("tags", []) if isinstance(t, dict)],
        "type": data.get("itemType"),
        "meta": {"zotero_key": key},
    }


def _zotero_pdf_path(item_key: str, zotero_dir: Path):
    """返回该条目第一个 PDF 附件的本地路径（只读取，不改 Zotero 数据）。"""
    try:
        resp = zotero_get(f"/users/0/items/{item_key}/children", {"format": "json"})
        children = resp.json()
    except Exception:  # noqa: BLE001
        return None
    for ch in children:
        d = ch.get("data", {})
        if d.get("itemType") != "attachment":
            continue
        if "pdf" not in (d.get("contentType") or "") and not str(
                d.get("filename", "")).lower().endswith(".pdf"):
            continue
        try:
            from .zotero_client import ZoteroClient
            file=ZoteroClient().attachment_file(ch.get('key') or d.get('key'))
            if file.is_file() and file.suffix.lower()=='.pdf':return str(file)
        except Exception:pass
        if d.get("linkMode") == "imported_file" and d.get("filename"):
            p = zotero_dir / "storage" / ch.get("key", "") / d["filename"]
            if p.is_file():
                return str(p)
        if d.get("linkMode") == "linked_file" and d.get("path"):
            p = Path(d["path"].replace("attachments:", "", 1))
            if p.is_file():
                return str(p)
    return None


def _collection_and_descendants(collection_key: str):
    """返回分类自身与其全部后代子分类的 key 列表。"""
    from .zotero_link import collection_rows
    from .zotero_client import ZoteroClient
    rows=collection_rows(ZoteroClient())
    if not any(r['key']==collection_key for r in rows):raise ValueError('集合已不存在，请刷新')
    out = [collection_key]
    for key in out:
        out.extend(r['key'] for r in rows if r['parent']==key and r['key'] not in out)
    return out


def zotero_collections():
    """列出本地 Zotero 的分类（只读）。"""
    from .zotero_link import collection_rows
    from .zotero_client import ZoteroClient
    return collection_rows(ZoteroClient())


def zotero_sync(enrich=True, collection_key=None):
    zotero_dir = Path(get_setting("zotero_data_dir") or (Path.home() / "Zotero"))
    stats = {"total": 0, "added": 0, "updated": 0, "with_pdf": 0, "errors": [],
             "enriched": 0, "collections": 0}

    # 确定要同步的分类集合：指定分类时递归包含其全部子分类
    keys = [None]
    if collection_key:
        if not re.match(r"^[A-Za-z0-9]+$", collection_key):
            raise ValueError("非法分类 key")
        keys = _collection_and_descendants(collection_key)
        stats["collections"] = len(keys)

    collected = []  # 联网/读文件阶段
    seen_keys = set()
    for ck in keys:
        base = f"/users/0/collections/{ck}/items" if ck else "/users/0/items"
        start = 0
        while True:
            params = {"format": "json", "limit": 100, "start": start,
                      "itemType": "-attachment -note -annotation"}
            resp = zotero_get(base, params)
            items = resp.json()
            if not items:
                break
            for item in items:
                data = item.get("data", {})
                zkey = data.get("key", "")
                if zkey and zkey in seen_keys:
                    continue  # 同一条目可能同时出现在父子分类
                if zkey:
                    seen_keys.add(zkey)
                p = _zotero_item_to_paper(item)
                if not p:
                    continue
                stats["total"] += 1
                if zkey:
                    pdf = _zotero_pdf_path(zkey, zotero_dir)
                    if pdf:
                        p["local_pdf_path"] = pdf
                        stats["with_pdf"] += 1
                if enrich and not p.get("doi") and p.get("title"):
                    try:
                        matched = _match_paper(title=p.get("title"))
                        if matched:
                            p["openalex_id"] = matched.get("openalex_id")
                            p["doi"] = matched.get("doi") or p.get("doi")
                            p["cited_by_count"] = matched.get("cited_by_count", 0)
                            p.setdefault("oa_pdf_url", matched.get("oa_pdf_url"))
                            stats["enriched"] += 1
                    except OfflineError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass
                collected.append(p)
            start += len(items)
            if len(items) < 100:
                break

    with get_db() as conn:  # 写回阶段
        for p in collected:
            try:
                pid, created = upsert_paper(conn, p, source="zotero", status="library")
                stats["added" if created else "updated"] += 1
                _remember(stats,pid)
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(str(e))
    return stats
