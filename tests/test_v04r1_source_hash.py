# -*- coding: utf-8 -*-
"""V4R1-05 反例测试：来源哈希与最终实际输入一致。

反例（修复前）：enrich 在缺口判定阶段用「补齐前」的题名/摘要计算
source_hash；Zotero/网络补齐原文后，content_provenance 里登记的仍是旧
哈希（Codex 反例 raw_fill_provenance.hash_matches_current=false），并且
AI 字段也拿到旧哈希。

修复后：原文补齐改变输入 → 按补齐后的最终实际输入重算来源哈希并校正
本轮登记行；AI 字段以最终哈希生成与登记；重复调用不重复外呼。
全部合成数据（Zotero/网络 mock），无真实外呼。

运行：python tests/test_v04r1_source_hash.py  （exit 0 = 通过）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import shutil
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import enrich as enr
from app import library_source as ls
from app.library_migrate import compute_source_hash, create_library_schema


def make_db(path, papers, settings=None):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_library_schema(conn)
    for p in papers:
        conn.execute(
            "INSERT INTO papers(id, doi, title, title_zh, abstract, "
            "abstract_zh, meta_json, status) VALUES(?,?,?,?,?,?,?,?)",
            (p["id"], p.get("doi"), p.get("title"), p.get("title_zh"),
             p.get("abstract"), p.get("abstract_zh"),
             json.dumps(p.get("meta") or {}, ensure_ascii=False),
             p.get("status", "library")))
    for k, v in (settings or {}).items():
        conn.execute("INSERT INTO settings(key, value) VALUES(?,?)", (k, v))
    conn.commit()
    return conn


class SourceHashFinalInputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="v04r1_hash_"))
        self.dbfile = self._tmp / "lib.db"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _db(self, papers, settings=None):
        return make_db(self.dbfile.with_name(f"{self.dbfile.name}_{id(papers)}"),
                       papers,
                       settings or {"zotero_enabled": "1", "offline_mode": "0"})

    def test_raw_fill_provenance_hash_matches_final_input(self):
        """修复前反例：Zotero 补齐后 provenance 哈希必须是最终输入哈希。"""
        conn = self._db([{"id": 1, "doi": "10.1/a", "title": None}])
        pre_hash = compute_source_hash(None, None)

        def fake_fetch(c, paper):
            return {"zotero_key": "Z1", "title": "Fresh Title",
                    "abstract": "Fresh Abstract."}, None

        with mock.patch.object(enr, "_zotero_fetch", side_effect=fake_fetch):
            out = enr.enrich_paper(conn, 1, zotero=True, network=False)
        final_hash = compute_source_hash("Fresh Title", "Fresh Abstract.")
        self.assertNotEqual(pre_hash, final_hash,
                            "补齐前后输入哈希必须不同（否则用例无效）")
        for field, origin in (("title", "zotero"), ("abstract", "zotero")):
            row = conn.execute(
                "SELECT source_hash FROM content_provenance "
                "WHERE paper_id=1 AND field=?", (field,)).fetchone()
            self.assertEqual(row[0], final_hash,
                             f"{field} 的来源登记哈希必须等于补齐后的最终输入哈希"
                             "（不得把补齐前的旧哈希赋给新结果）")
            self.assertNotEqual(row[0], pre_hash)
        self.assertEqual(out["fields"]["abstract"]["origin"], "zotero")
        conn.close()

    def test_ai_fields_registered_with_post_fill_hash(self):
        """原文补齐后生成 AI 字段 → 提供者拿到并登记最终哈希。"""
        conn = self._db([{"id": 2, "doi": "10.1/b", "title": "AI Title"}])
        seen = []

        def provider(paper, source_hash):
            seen.append(source_hash)
            return {"text": "AI 译文", "model": "synthetic-model",
                    "prompt_version": "enrich_field_v1",
                    "input_snapshot": "Fresh Abstract.", "generated_at": "T"}

        ls.register_ai_provider("abstract_zh", provider)
        try:
            with mock.patch.object(
                    enr, "_zotero_fetch",
                    side_effect=lambda c, p: (
                        {"zotero_key": "Z2", "title": "AI Title",
                         "abstract": "Fresh Abstract."}, None)):
                enr.enrich_paper(conn, 2, zotero=True, network=False, ai=True)
        finally:
            ls.unregister_ai_provider("abstract_zh")
        final_hash = compute_source_hash("AI Title", "Fresh Abstract.")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], final_hash,
                         "AI 提供者必须拿到补齐后的最终来源哈希")
        row = conn.execute(
            "SELECT source_hash, model, prompt_version FROM content_provenance "
            "WHERE paper_id=2 AND field='abstract_zh'").fetchone()
        self.assertEqual(row[0], final_hash)
        self.assertEqual(row[1], "synthetic-model")
        conn.close()

    def test_network_fill_hash_final(self):
        """网络书目补齐 → 来源登记哈希 = 最终（题名+摘要）哈希。"""
        conn = self._db([{"id": 3, "doi": "10.1/c", "title": None}],
                        settings={"zotero_enabled": "0", "offline_mode": "0"})
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"message": {"title": ["Net Title"],
                                              "abstract": "Net Abstract."}}
        with mock.patch("requests.get", return_value=resp):
            enr.enrich_paper(conn, 3, zotero=False, network=True)
        final_hash = compute_source_hash("Net Title", "Net Abstract.")
        for field in ("title", "abstract"):
            row = conn.execute(
                "SELECT source_hash, origin FROM content_provenance "
                "WHERE paper_id=3 AND field=?", (field,)).fetchone()
            self.assertEqual(row[0], final_hash)
            self.assertEqual(row[1], "network_biblio")
        conn.close()

    def test_repeat_call_no_refill_and_hash_stable(self):
        """补齐持久化后重复调用：不重复外呼，哈希保持一致。"""
        conn = self._db([{"id": 4, "doi": "10.1/d", "title": "T4"}])
        calls = []

        def fake_fetch(c, paper):
            calls.append(1)
            return {"zotero_key": "Z4", "title": "T4",
                    "abstract": "Abstract Four."}, None

        with mock.patch.object(enr, "_zotero_fetch", side_effect=fake_fetch):
            enr.enrich_paper(conn, 4, zotero=True, network=False)
        h1 = conn.execute(
            "SELECT source_hash FROM content_provenance "
            "WHERE paper_id=4 AND field='abstract'").fetchone()[0]
        with mock.patch.object(enr, "_zotero_fetch", side_effect=fake_fetch):
            out2 = enr.enrich_paper(conn, 4, zotero=True, network=False)
        self.assertEqual(len(calls), 1, "持久化后重复调用不得再次外呼")
        self.assertEqual(out2["fields"]["abstract"]["after"], "skipped")
        h2 = conn.execute(
            "SELECT source_hash FROM content_provenance "
            "WHERE paper_id=4 AND field='abstract'").fetchone()[0]
        self.assertEqual(h1, h2)
        self.assertEqual(h1, compute_source_hash("T4", "Abstract Four."))
        conn.close()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(
            SourceHashFinalInputTests))
    sys.exit(0 if result.wasSuccessful() else 1)
