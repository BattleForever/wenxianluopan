"""Zotero 同步队列与状态机测试（B 线 parallel_v03，unittest 风格）。

用本地桩客户端覆盖：本地成功但 Zotero 失败、重启后重试、重复提交、
已有项命中不重复、版本冲突、仅缺失字段补全、§3.2 状态接口形状。
真实写回不在此测试（见 evidence/ 联调留痕）。
"""

import json
import sqlite3
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import zotero_sync
from app.zotero_client import (ZoteroConflict, ZoteroUnavailable,
                               build_item_payload)
from app.zotero_sync import (STATE_CONFLICT, STATE_NONE, STATE_PENDING,
                             STATE_SYNCED, enqueue, init_queue, process_queue,
                             requeue, sync_status)

PAPERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    openalex_id TEXT, doi TEXT UNIQUE, title TEXT, title_zh TEXT, norm_title TEXT,
    authors TEXT, year INTEGER, venue TEXT, abstract TEXT, abstract_zh TEXT,
    type TEXT, cited_by_count INTEGER DEFAULT 0, status TEXT DEFAULT 'candidate',
    oa_pdf_url TEXT, local_pdf_path TEXT, source TEXT, tags TEXT, meta_json TEXT,
    fetched_at TEXT, added_at TEXT, updated_at TEXT
);
"""


def make_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(PAPERS_SCHEMA)
    conn.execute(
        "INSERT INTO papers(id, doi, title, abstract, authors, year, venue, tags) "
        "VALUES(1, '10.1038/nature14539', 'Deep learning', 'Abstract text', "
        "'[{\"name\":\"LeCun, Y.\"}]', 2015, 'Nature', '[\"ml\"]')")
    conn.commit()
    return conn


class MockZotero:
    """桩客户端：模拟本地 API 的查重/新建/更新行为。"""

    def __init__(self, existing=None, create_error=None, create_failed=None):
        self.existing = dict(existing or {})   # doi -> item dict
        self.create_error = create_error       # exception 或 None
        self.create_failed = create_failed     # {"0": reason} 或 None
        self.created = []
        self.updated = []

    def find_by_identifier(self, doi=None, title=None):
        if doi and doi.lower() in self.existing:
            return self.existing[doi.lower()]
        if title:
            for it in self.existing.values():
                if (it["data"].get("title") or "").strip() == title.strip():
                    return it
        return None

    def create_items(self, items):
        if self.create_error:
            raise self.create_error
        if self.create_failed:
            return {"success": {}, "failed": self.create_failed}
        self.created.extend(items)
        key = f"NEWKEY{len(self.created):04d}"
        return {"success": {"0": key}, "failed": {}}

    def update_item(self, key, patch, version=None):
        self.updated.append((key, patch, version))
        return {"ok": True}


class ZoteroSyncTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="b03_"))
        self.dbfile = self._tmp / "app.db"
        self.db = make_db(self.dbfile)
        init_queue(self.db)

    def tearDown(self):
        self.db.close()
        import shutil as _sh
        _sh.rmtree(self._tmp, ignore_errors=True)

    def _reopen(self):
        # “重启”：仅重新打开同一库文件，不重复插入种子数据
        conn = sqlite3.connect(str(self.dbfile))
        conn.row_factory = sqlite3.Row
        return conn

    def test_local_saved_even_when_zotero_fails(self):
        enqueue(self.db, 1, {})
        res = process_queue(self.db, MockZotero(create_error=ZoteroUnavailable("down")))
        self.assertEqual(res["stayed_pending"], 1)
        row = self.db.execute(
            "SELECT state, last_error, attempts FROM zotero_sync_queue").fetchone()
        self.assertEqual(row["state"], STATE_PENDING)
        self.assertIn("down", row["last_error"])
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM papers WHERE id=1").fetchone()[0], 1)
        rec = sync_status(self.db, paper_id=1)["records"][0]
        self.assertTrue(rec["local_saved"])
        self.assertEqual(rec["zotero_state"], STATE_PENDING)
        self.assertTrue(rec["last_error"])

    def test_retry_after_restart(self):
        enqueue(self.db, 1, {})
        process_queue(self.db, MockZotero(create_error=ZoteroUnavailable("down")))
        self.db.close()
        conn = self._reopen()  # “重启”：重新打开同一库文件，队列仍在
        self.assertEqual(
            conn.execute("SELECT state FROM zotero_sync_queue").fetchone()["state"],
            STATE_PENDING)
        res = process_queue(conn, MockZotero())
        self.assertEqual(res["synced"], 1)
        rec = sync_status(conn, paper_id=1)["records"][0]
        self.assertEqual(rec["zotero_state"], STATE_SYNCED)
        self.assertIsNone(rec["last_error"])
        conn.close()

    def test_duplicate_submit_single_queue_row(self):
        q1 = enqueue(self.db, 1, {"collection_key": "A"})
        q2 = enqueue(self.db, 1, {"collection_key": "B"})
        self.assertEqual(q1, q2)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM zotero_sync_queue").fetchone()[0], 1)
        pj = json.loads(self.db.execute(
            "SELECT payload_json FROM zotero_sync_queue").fetchone()[0])
        self.assertEqual(pj["collection_key"], "B")

    def test_existing_item_hit_no_duplicate(self):
        mock = MockZotero(existing={
            "10.1038/nature14539": {
                "key": "EXIST01",
                "data": {"title": "Deep learning", "DOI": "10.1038/nature14539",
                         "abstractNote": "Existing abstract", "version": 3,
                         "collections": []},
            }})
        enqueue(self.db, 1, {})
        res = process_queue(self.db, mock)
        self.assertEqual(res["synced"], 1)
        self.assertEqual(mock.created, [])   # 未新建
        rec = sync_status(self.db, paper_id=1)["records"][0]
        self.assertEqual(rec["zotero_state"], STATE_SYNCED)
        key = self.db.execute(
            "SELECT zotero_key FROM zotero_sync_queue").fetchone()["zotero_key"]
        self.assertEqual(key, "EXIST01")

    def test_fill_only_missing_fields(self):
        mock = MockZotero(existing={
            "10.1038/nature14539": {
                "key": "EXIST02",
                "data": {"title": "Deep learning", "DOI": "10.1038/nature14539",
                         "abstractNote": "", "version": 5, "collections": ["COL1"]},
            }})
        enqueue(self.db, 1, {"collection_key": "TARGET"})
        process_queue(self.db, mock)
        self.assertEqual(len(mock.updated), 1)
        key, patch, version = mock.updated[0]
        self.assertEqual(key, "EXIST02")
        self.assertEqual(version, 5)
        self.assertEqual(set(patch.keys()), {"abstractNote", "collections"})  # 仅缺失字段
        self.assertEqual(patch["abstractNote"], "Abstract text")
        self.assertEqual(patch["collections"], ["COL1", "TARGET"])  # 不覆盖已有集合

    def test_version_conflict_marks_conflict(self):
        class ConflictClient(MockZotero):
            def create_items(self, items):
                raise ZoteroConflict("version conflict 409")
        enqueue(self.db, 1, {})
        res = process_queue(self.db, ConflictClient())
        self.assertEqual(res["conflict"], 1)
        rec = sync_status(self.db, paper_id=1)["records"][0]
        self.assertEqual(rec["zotero_state"], STATE_CONFLICT)
        self.assertIn("409", rec["last_error"])
        qid = self.db.execute("SELECT id FROM zotero_sync_queue").fetchone()["id"]
        self.assertTrue(requeue(self.db, qid))
        res2 = process_queue(self.db, MockZotero())
        self.assertEqual(res2["synced"], 1)

    def test_zotero_write_rejected_marks_conflict(self):
        enqueue(self.db, 1, {})
        res = process_queue(self.db, MockZotero(create_failed={"0": "invalid item"}))
        self.assertEqual(res["conflict"], 1)
        rec = sync_status(self.db, paper_id=1)["records"][0]
        self.assertEqual(rec["zotero_state"], STATE_CONFLICT)
        self.assertIn("invalid item", rec["last_error"])

    def test_status_none_state_and_summary(self):
        conn2 = self._reopen()
        conn2.execute("INSERT INTO papers(id, doi, title) VALUES(2, '10.1/x', 'X')")
        conn2.commit()
        enqueue(conn2, 1, {})
        status = sync_status(conn2)
        by_id = {r["paper_id"]: r for r in status["records"]}
        self.assertEqual(by_id[2]["zotero_state"], STATE_NONE)
        self.assertTrue(by_id[2]["local_saved"])
        self.assertEqual(by_id[1]["zotero_state"], STATE_PENDING)
        self.assertEqual(set(by_id[1].keys()),
                         {"paper_id", "local_saved", "zotero_state", "last_error"})
        self.assertEqual(status["summary"][STATE_NONE], 1)
        self.assertEqual(status["summary"][STATE_PENDING], 1)
        conn2.close()

    def test_enqueue_rejects_unknown_paper(self):
        with self.assertRaises(ValueError):
            enqueue(self.db, 999, {})

    def test_sync_router_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        init_queue(self.db)
        enqueue(self.db, 1, {"collection_key": "K"})
        self.db.commit()

        app = FastAPI()
        app.include_router(zotero_sync.router)

        def _tmp_conn():
            c = sqlite3.connect(str(self.dbfile))
            c.row_factory = sqlite3.Row
            return c

        zotero_sync._conn = _tmp_conn  # 隔离：指向临时测试库
        client = TestClient(app)
        resp = client.get("/api/library/sync/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["records"][0]["zotero_state"], STATE_PENDING)
        self.assertTrue(data["records"][0]["local_saved"])
        self.assertIsNone(data["records"][0]["last_error"])
        resp2 = client.get("/api/library/sync/status", params={"state": "none"})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["records"], [])
        resp3 = client.get("/api/library/sync/status", params={"state": "bogus"})
        self.assertEqual(resp3.status_code, 400)

    def test_build_item_payload_no_private_files(self):
        paper = {"title": "T", "authors": ["Ann B Lee"], "abstract": "A",
                 "venue": "Nature", "doi": "10.1/x", "year": 2015,
                 "oa_pdf_url": "https://x/y.pdf", "tags": ["t1"],
                 "local_pdf_path": "/synthetic-private/a.pdf"}
        item = build_item_payload(paper, "COLL")
        self.assertEqual(item["itemType"], "journalArticle")
        self.assertEqual(item["creators"][0]["lastName"], "Lee")
        self.assertEqual(item["collections"], ["COLL"])
        self.assertNotIn("local_pdf_path", item)  # 不把本地 PDF 路径送进 Zotero payload
        self.assertNotIn("/synthetic-private", json.dumps(item))


if __name__ == "__main__":
    unittest.main()
