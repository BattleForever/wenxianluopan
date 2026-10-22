# -*- coding: utf-8 -*-
"""V4R1 反例测试：AI 产物可审计元数据贯穿持久化。

要求：
- 新生成的 AI 字段内容登记 model / prompt_version / input_snapshot /
  generated_at（来源实际输入快照），origin=ai_with_source 且元数据完整；
- 新式提供者缺模型/版本元数据 → 拒绝登记为 ai_with_source（如实上报，
  不补造）；
- 旧式 str 提供者（历史契约）兼容：内容照常登记，元数据如实 NULL；
- 真实产品提供者（ai_brief._enrich_provider）路径元数据完整。

全部合成桩（call_llm/load_ai_config 替换），无真实外呼。
运行：python tests/test_v04r1_ai_metadata.py  （exit 0 = 通过）
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

from app import ai_brief as ab
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


def prov_row(conn, paper_id, field):
    """读取 provenance 行（旧库缺审计列时以 NULL 占位，不修改库）。"""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(content_provenance)")}
    if not cols:
        return None
    want = ["text", "origin", "source_hash", "model", "prompt_version",
            "input_snapshot", "generated_at"]
    sel = ", ".join(w if w in cols else f"NULL AS {w}" for w in want)
    return conn.execute(
        f"SELECT {sel} FROM content_provenance WHERE paper_id=? AND field=?",
        (paper_id, field)).fetchone()


class AIMetadataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="v04r1_meta_"))
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _db(self, papers, settings=None):
        self.n += 1
        return make_db(self._tmp / f"lib{self.n}.db", papers,
                       settings or {"zotero_enabled": "0",
                                    "offline_mode": "0"})

    def test_new_ai_metadata_persisted(self):
        """新式提供者（带元数据 dict）→ provenance 元数据完整。"""
        conn = self._db([{"id": 1, "title": "T", "abstract": "  Src abstract.  "}])
        ls.register_ai_provider("abstract_zh", lambda p, h: {
            "text": "AI 译文", "model": "synthetic-model",
            "prompt_version": "enrich_field_v1",
            "input_snapshot": "Src abstract.", "generated_at": "2026-01-01T00:00:00Z"})
        try:
            out = enr.enrich_paper(conn, 1, zotero=False, network=False, ai=True)
        finally:
            ls.unregister_ai_provider("abstract_zh")
        self.assertEqual(out["fields"]["abstract_zh"]["origin"], "ai_with_source")
        row = prov_row(conn, 1, "abstract_zh")
        self.assertIsNotNone(row)
        self.assertEqual(row["origin"], "ai_with_source")
        self.assertEqual(row["model"], "synthetic-model")
        self.assertEqual(row["prompt_version"], "enrich_field_v1")
        self.assertEqual(row["input_snapshot"], "Src abstract.",
                         "input_snapshot 必须是实际发给模型的输入快照")
        self.assertEqual(row["generated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["source_hash"],
                         compute_source_hash("T", "  Src abstract.  "))
        conn.close()

    def test_new_provider_missing_model_rejected(self):
        """新式提供者缺 model/prompt_version → 拒绝登记 ai_with_source。"""
        conn = self._db([{"id": 1, "title": "T", "abstract": "Abs."}])
        ls.register_ai_provider("abstract_zh", lambda p, h: {
            "text": "AI 译文（无元数据）"})
        try:
            out = enr.enrich_paper(conn, 1, zotero=False, network=False, ai=True)
        finally:
            ls.unregister_ai_provider("abstract_zh")
        codes = {e["code"] for e in out["errors"]}
        self.assertIn("ai_metadata_missing", codes)
        self.assertEqual(out["fields"]["abstract_zh"]["after"], "missing")
        self.assertIsNone(prov_row(conn, 1, "abstract_zh"),
                          "缺模型/版本的新 AI 内容不得登记为 ai_with_source")
        self.assertIsNone(conn.execute(
            "SELECT abstract_zh FROM papers WHERE id=1").fetchone()[0])
        conn.close()

    def test_legacy_str_provider_compat_metadata_null(self):
        """旧式 str 提供者（历史契约）：内容照常登记，元数据如实 NULL。"""
        conn = self._db([{"id": 1, "title": "T", "abstract": "Abs."}])
        ls.register_ai_provider("abstract_zh", lambda p, h: "旧契约 AI 译文")
        try:
            out = enr.enrich_paper(conn, 1, zotero=False, network=False, ai=True)
        finally:
            ls.unregister_ai_provider("abstract_zh")
        self.assertEqual(out["fields"]["abstract_zh"]["after"], "filled")
        row = prov_row(conn, 1, "abstract_zh")
        self.assertEqual(row["origin"], "ai_with_source")
        self.assertIsNone(row["model"], "旧契约未记录模型 → 如实 NULL，不补造")
        self.assertIsNone(row["prompt_version"])
        conn.close()

    def test_real_provider_path_full_metadata(self):
        """真实产品提供者（ai_brief._enrich_provider）：元数据完整、
        input_snapshot 与实际发送输入一致。"""
        conn = self._db([{"id": 1, "title": "Real T",
                          "abstract": "Real abstract body."}])
        fake_cfg = {"chat_url": "https://modelgateway.zju.edu.cn/v1/x",
                    "request_model": "synthetic-model", "api_key": "k"}
        ls.register_ai_provider("abstract_zh", ab._enrich_provider("abstract_zh"))
        try:
            with mock.patch.object(ab, "load_ai_config",
                                   return_value=fake_cfg), \
                    mock.patch.object(ab, "call_llm",
                                      return_value=("合成译文", "synthetic-resp")):
                out = enr.enrich_paper(conn, 1, zotero=False, network=False,
                                       ai=True)
        finally:
            ls.unregister_ai_provider("abstract_zh")
        self.assertEqual(out["fields"]["abstract_zh"]["after"], "filled")
        row = prov_row(conn, 1, "abstract_zh")
        self.assertEqual(row["model"], "synthetic-model",
                         "实际请求模型必须登记")
        self.assertEqual(row["prompt_version"], ab.ENRICH_FIELD_PROMPT_VERSION)
        self.assertEqual(row["input_snapshot"], "Real abstract body.")
        self.assertTrue(row["generated_at"])
        self.assertEqual(row["source_hash"],
                         compute_source_hash("Real T", "Real abstract body."))
        conn.close()

    def test_old_schema_columns_migrated(self):
        """旧库 content_provenance 缺审计列 → 自动 ALTER 补齐，不丢数据。"""
        conn = self._db([{"id": 1, "title": "T", "abstract": "Abs."}])
        conn.execute("DROP TABLE IF EXISTS content_provenance")
        conn.executescript(
            "CREATE TABLE content_provenance ("
            "paper_id INTEGER NOT NULL, field TEXT NOT NULL, text TEXT, "
            "origin TEXT NOT NULL, source_hash TEXT, model TEXT, "
            "prompt_version TEXT, recorded_by TEXT, "
            "created_at TEXT DEFAULT (datetime('now','localtime')), "
            "updated_at TEXT, PRIMARY KEY (paper_id, field))")
        conn.execute("INSERT INTO content_provenance(paper_id, field, text, "
                     "origin, recorded_by) VALUES(1,'title_zh','人工旧译文',"
                     "'library_human','human')")
        conn.commit()
        ls.register_ai_provider("abstract_zh", lambda p, h: {
            "text": "AI 译文", "model": "m", "prompt_version": "v1",
            "input_snapshot": "Abs.", "generated_at": "T"})
        try:
            enr.enrich_paper(conn, 1, zotero=False, network=False, ai=True)
        finally:
            ls.unregister_ai_provider("abstract_zh")
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(content_provenance)")}
        self.assertIn("input_snapshot", cols)
        self.assertIn("generated_at", cols)
        old = conn.execute("SELECT origin FROM content_provenance "
                           "WHERE paper_id=1 AND field='title_zh'").fetchone()
        self.assertEqual(old[0], "library_human", "旧记录不得被破坏")
        conn.close()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(AIMetadataTests))
    sys.exit(0 if result.wasSuccessful() else 1)
