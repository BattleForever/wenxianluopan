"""v04_r2 R2-01 来源链修复回归：过期 AI 译文不得被重新包装为当前有效。

背景（Codex independent_r1.py stale_translation_reuse 反例，parallel_v04_r1
验收 R2-01/P1）：保存带来源哈希的 AI 中文题名/摘要译文后修改原文，
artifact_validity 正确判 stale，但读取/生成入口未共用该判定——旧译文经
get_existing_content 降级为 library_unknown_source 后被缺口感知生成复用，
新 brief 仍标 verified。

本套件用 fastapi.testclient 走真实路由，覆盖：
1. 题名+摘要都变 / 仅题名变：旧译文不得进入新 brief；新 brief 不得
   "verified 且含旧输入译文"；
2. model / prompt_version 变化：stale 且同样不复用；
3. 人工内容（library_human）在原文变化后保留且不被 stale 逻辑破坏；
4. 历史 ai_briefs 行仍可读（含旧输出，不删除历史）；
5. 同步侧：papers 直出中文携带 provenance 元数据（配合
   deliverables/zotero_sync_r2.patch，lane 内自测）；stale 不同步。

运行约束：独立进程运行（与本仓库其他套件一致）；隔离临时库、全部
mock，无真实模型调用、无真实 Zotero 写入。
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="r2stale_"))
config.DB_PATH = _TMP / "r2stale.db"

from app.main import app  # noqa: E402
from app import ai_brief, db, library_migrate, library_source  # noqa: E402
from app.db import get_db  # noqa: E402
from app.store import upsert_paper  # noqa: E402
from app import zotero_sync  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CFG = {"request_model": "fixture",
       "chat_url": "https://modelgateway.zju.edu.cn/v1/chat/completions",
       "api_key": "TEST-ONLY-NOT-A-REAL-KEY"}
OLD_TITLE = "Old source title"
OLD_ABS = "We measured an accuracy of 82 percent."
NEW_TITLE = "New source title"
NEW_ABS = "New source reports a completely different result."
OLD_TZ = "旧题名的AI译文"
OLD_AZ = "旧摘要的AI译文"

client = TestClient(app)


def _model_content(with_title_zh=True, with_abstract_zh=None):
    """受控模型输出。with_title_zh/with_abstract_zh=False 时不含对应
    中文字段（缺口感知场景，与 Codex 反例一致）。"""
    if with_abstract_zh is None:
        with_abstract_zh = with_title_zh
    quote = "We measured an accuracy of 82 percent."
    data = {"abstract_en": "Fresh English summary.",
            **{k: {"zh": f"新来源的{name}要素", "en": "Fresh element",
                   "quote": quote} for k, name in
               zip(ai_brief._ELEMENTS, ("研究问题", "研究方法", "研究结果",
                                        "研究贡献"))}}
    if with_title_zh:
        data["title_zh"] = "新来源题名的全新译文"
    if with_abstract_zh:
        data["abstract_zh"] = "新来源摘要的全新译文。"
    return json.dumps(data, ensure_ascii=False)


class _StubZotero:
    """最小 Zotero 桩（duck-typing，与既有套件一致；只记录不外发）。"""

    def __init__(self, item):
        self.item = item
        self.note_writes = []
        self._seq = 0

    def find_by_identifier(self, doi=None, title=None):
        return dict(self.item)

    def create_items(self, items):
        out = {}
        for i, it in enumerate(items):
            self._seq += 1
            key = f"K{self._seq:04d}"
            if it.get("itemType") == "note":
                self.note_writes.append({"key": key, "note": it["note"]})
            out[str(i)] = key
        return {"success": out}

    def update_item(self, key, patch, version=None):
        data = self.item.setdefault("data", {})
        for k, v in (patch or {}).items():
            if k == "collections":
                data[k] = list(data.get(k) or []) + [
                    x for x in v if x not in (data.get(k) or [])]
            else:
                data[k] = v
        return {"key": key}

    def get_item(self, key):
        if key == self.item["key"]:
            return dict(self.item)
        raise zotero_sync.ZoteroError("条目不存在（模拟）")


class StaleReuseBase(unittest.TestCase):
    def setUp(self):
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(config.DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        db.init_db()
        ai_brief.init()
        with get_db() as c:
            c.execute("UPDATE settings SET value='1' "
                      "WHERE key='zotero_enabled'")
            c.execute("UPDATE settings SET value='0' "
                      "WHERE key='offline_mode'")

    # ---- 夹具 -------------------------------------------------------
    def make_paper(self, title=OLD_TITLE, abstract=OLD_ABS,
                   doi="10.9999/r2-stale"):
        with get_db() as conn:
            pid, _ = upsert_paper(
                conn, {"title": title, "abstract": abstract, "doi": doi},
                "fixture", "library")
        return pid

    def seed_ai(self, pid, title=OLD_TITLE, abstract=OLD_ABS,
                model="fixture", pv=None, texts=(OLD_TZ, OLD_AZ)):
        """保存带 model/prompt_version/来源哈希 的 AI 中文题名/摘要译文。"""
        pv = pv if pv is not None else ai_brief.ENRICH_FIELD_PROMPT_VERSION
        h = library_migrate.compute_source_hash(title, abstract)
        with get_db() as c:
            library_source._ensure_provenance_columns(c)
            for (f, t) in zip(("title_zh", "abstract_zh"), texts):
                c.execute(f"UPDATE papers SET {f}=? WHERE id=?", (t, pid))
                c.execute(
                    "INSERT OR REPLACE INTO content_provenance(paper_id,field,"
                    "text,origin,source_hash,model,prompt_version) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (pid, f, t, "ai_with_source", h, model, pv))
        return h

    def seed_human(self, pid, text="人工校对题名"):
        with get_db() as c:
            library_source._ensure_provenance_columns(c)
            c.execute("UPDATE papers SET title_zh=? WHERE id=?", (text, pid))
            c.execute(
                "INSERT OR REPLACE INTO content_provenance(paper_id,field,"
                "text,origin) VALUES(?,?,?,?)",
                (pid, "title_zh", text, "library_human"))
        return text

    def change_source(self, pid, title=NEW_TITLE, abstract=NEW_ABS):
        with get_db() as c:
            c.execute("UPDATE papers SET title=?, abstract=? WHERE id=?",
                      (title, abstract, pid))

    def generate(self, pid, content, cfg=CFG):
        with mock.patch.object(ai_brief, "load_ai_config",
                               return_value=cfg):
            with mock.patch.object(ai_brief, "call_llm",
                                   return_value=(content, "fixture")):
                generated = client.post(
                    "/api/ai/briefs",
                    json={"paper_id": pid,
                          "source_kind": "abstract"}).json()
                current = client.get(f"/api/ai/briefs/{pid}").json()
        return generated, current


class TestBothChanged(StaleReuseBase):
    """原文（题名+摘要）都变：旧译文不得进入新 brief。"""

    def test_gap_output_does_not_fill_old_translations(self):
        """模型未产出中文字段（缺口场景）时，绝不回填旧输入译文：
        如实按结构错误拒绝，而不是把旧译文冒充当前译文。"""
        pid = self.make_paper()
        self.seed_ai(pid)
        self.change_source(pid)
        with mock.patch.object(ai_brief, "load_ai_config",
                               return_value=CFG):
            with get_db() as c:
                v = library_source.artifact_validity(c, pid, "title_zh")
        self.assertEqual(v["validity"], "stale")
        self.assertIn("source_changed", v["reasons"])
        generated, current = self.generate(pid, _model_content(with_title_zh=False, with_abstract_zh=False))
        brief = generated.get("brief") or {}
        self.assertNotEqual(brief.get("title_zh"), OLD_TZ)
        self.assertNotEqual(brief.get("abstract_zh"), OLD_AZ)
        self.assertNotIn(OLD_TZ, json.dumps(generated, ensure_ascii=False))
        self.assertNotIn(OLD_AZ, json.dumps(generated, ensure_ascii=False))
        # 新 brief 有效性如实：不存在 "verified/非 stale 且含旧输入译文"
        self.assertFalse(
            current.get("validity") == "verified"
            and current.get("stale") is False
            and (OLD_TZ in json.dumps(current.get("brief") or {},
                                      ensure_ascii=False)))

    def test_regeneration_uses_fresh_translations(self):
        """模型正常产出时：新 brief 为全新译文，旧译文不出现，且新 brief
        如实标 verified（不含旧输入译文）。"""
        pid = self.make_paper()
        self.seed_ai(pid)
        self.change_source(pid)
        generated, current = self.generate(pid, _model_content(with_title_zh=True))
        self.assertEqual(generated.get("status"), "ready")
        brief = generated.get("brief") or {}
        self.assertEqual(brief.get("title_zh"), "新来源题名的全新译文")
        self.assertEqual(brief.get("abstract_zh"), "新来源摘要的全新译文。")
        self.assertNotIn(OLD_TZ, json.dumps(brief, ensure_ascii=False))
        self.assertNotIn(OLD_AZ, json.dumps(brief, ensure_ascii=False))
        self.assertEqual(current.get("validity"), "verified")
        self.assertFalse(current.get("stale"))
        self.assertNotIn(OLD_TZ, json.dumps(current.get("brief") or {},
                                            ensure_ascii=False))

    def test_getter_keeps_ai_identity_with_stale_reasons(self):
        """读取接口保持 AI 身份：origin 不降级，附 validity/stale_reasons。"""
        pid = self.make_paper()
        self.seed_ai(pid)
        self.change_source(pid)
        with get_db() as c:
            ec = library_source.get_existing_content(
                c, pid, zotero=False, network=False, ai=False)
        for f in ("title_zh", "abstract_zh"):
            self.assertEqual(ec[f]["origin"], "ai_with_source")
            self.assertEqual(ec[f]["validity"], "stale")
            self.assertIn("source_changed", ec[f]["stale_reasons"])
            self.assertEqual(ec[f]["text"], OLD_TZ if f == "title_zh"
                             else OLD_AZ)  # 历史文本保留，仅历史展示
        self.assertIn("title_zh", ec["stale_fields"])
        self.assertIn("abstract_zh", ec["stale_fields"])


class TestTitleOnlyChanged(StaleReuseBase):
    """仅题名变化：同样语义。"""

    def test_title_change_not_reused(self):
        pid = self.make_paper()
        self.seed_ai(pid)
        self.change_source(pid, title=NEW_TITLE, abstract=OLD_ABS)
        with mock.patch.object(ai_brief, "load_ai_config",
                               return_value=CFG):
            with get_db() as c:
                v = library_source.artifact_validity(c, pid, "abstract_zh")
        self.assertEqual(v["validity"], "stale")
        generated, current = self.generate(pid, _model_content(with_title_zh=False, with_abstract_zh=False))
        brief = generated.get("brief") or {}
        self.assertNotEqual(brief.get("title_zh"), OLD_TZ)
        self.assertNotEqual(brief.get("abstract_zh"), OLD_AZ)
        self.assertNotIn(OLD_TZ, json.dumps(generated, ensure_ascii=False))
        self.assertNotIn(OLD_AZ, json.dumps(generated, ensure_ascii=False))


class TestMetadataChange(StaleReuseBase):
    """model / prompt_version 变化：stale 且同样不复用。"""

    def test_model_change_stale_and_not_reused(self):
        pid = self.make_paper()
        self.seed_ai(pid, model="old-model")
        with mock.patch.object(ai_brief, "load_ai_config", return_value=CFG):
            with get_db() as c:
                v = library_source.artifact_validity(c, pid, "title_zh")
                self.assertEqual(v["validity"], "stale")
                self.assertIn("request_model_changed", v["reasons"])
                ec = library_source.get_existing_content(
                    c, pid, zotero=False, network=False, ai=False)
        self.assertEqual(ec["title_zh"]["origin"], "ai_with_source")
        self.assertEqual(ec["title_zh"]["validity"], "stale")
        self.assertIn("title_zh", ec["stale_fields"])
        generated, _ = self.generate(pid, _model_content(with_title_zh=False, with_abstract_zh=False))
        self.assertNotIn(OLD_TZ, json.dumps(generated, ensure_ascii=False))
        self.assertNotIn(OLD_AZ, json.dumps(generated, ensure_ascii=False))

    def test_prompt_version_change_stale_and_not_reused(self):
        pid = self.make_paper()
        self.seed_ai(pid, pv="enrich_field_v0（旧提示词）")
        with mock.patch.object(ai_brief, "load_ai_config", return_value=CFG):
            with get_db() as c:
                v = library_source.artifact_validity(c, pid, "title_zh")
            self.assertEqual(v["validity"], "stale")
            self.assertIn("prompt_version_changed", v["reasons"])
        generated, _ = self.generate(pid, _model_content(with_title_zh=False, with_abstract_zh=False))
        self.assertNotIn(OLD_TZ, json.dumps(generated, ensure_ascii=False))
        self.assertNotIn(OLD_AZ, json.dumps(generated, ensure_ascii=False))


class TestHumanContent(StaleReuseBase):
    """人工内容（library_human）保留，不被 stale 逻辑破坏。"""

    def test_human_kept_and_used_after_source_change(self):
        pid = self.make_paper()
        human = self.seed_human(pid)
        self.change_source(pid)
        with get_db() as c:
            ec = library_source.get_existing_content(
                c, pid, zotero=False, network=False, ai=False)
        # 人工内容保持 library_human 身份，不标 stale、不进 stale_fields
        self.assertEqual(ec["title_zh"],
                         {"text": human, "origin": "library_human"})
        self.assertNotIn("title_zh", ec["stale_fields"])
        # 数据库列不被删除/覆盖
        with get_db() as c:
            self.assertEqual(c.execute(
                "SELECT title_zh FROM papers WHERE id=?",
                (pid,)).fetchone()[0], human)
        # 缺口感知生成：人工译文作为当前内容参与（abstract_zh 缺失由模型生成）
        generated, current = self.generate(pid, _model_content(with_title_zh=False, with_abstract_zh=True))
        brief = generated.get("brief") or {}
        self.assertEqual(brief.get("title_zh"), human)
        self.assertIn("title_zh", brief.get("reused_fields") or [])
        self.assertNotIn(OLD_AZ, json.dumps(brief, ensure_ascii=False))

    def test_ai_history_row_not_deleted(self):
        """旧原始 AI 输出（papers 列 + content_provenance 行）不被覆盖删除。"""
        pid = self.make_paper()
        self.seed_ai(pid)
        self.change_source(pid)
        self.generate(pid, _model_content(with_title_zh=True))
        with get_db() as c:
            rows = c.execute(
                "SELECT field, text, origin FROM content_provenance "
                "WHERE paper_id=? ORDER BY field", (pid,)).fetchall()
            by_field = {r["field"]: (r["text"], r["origin"]) for r in rows}
        self.assertEqual(by_field["title_zh"], (OLD_TZ, "ai_with_source"))
        self.assertEqual(by_field["abstract_zh"], (OLD_AZ, "ai_with_source"))
        with get_db() as c:
            self.assertEqual(c.execute(
                "SELECT title_zh FROM papers WHERE id=?",
                (pid,)).fetchone()[0], OLD_TZ)


class TestHistoryReadable(StaleReuseBase):
    """历史 ai_briefs 行仍可查（含旧输出）。"""

    def test_old_brief_rows_preserved_and_queryable(self):
        pid = self.make_paper()
        self.seed_ai(pid)
        # 旧来源下先生成一份 brief（含旧译文——由受控模型输出提供）
        old_content = json.dumps(
            {"title_zh": OLD_TZ, "abstract_zh": OLD_AZ,
             "abstract_en": "Old summary.",
             **{k: {"zh": "旧要素", "en": "Old element",
                    "quote": "We measured an accuracy of 82 percent."}
                for k in ai_brief._ELEMENTS}},
            ensure_ascii=False)
        generated_old, _ = self.generate(pid, old_content)
        self.assertEqual(generated_old.get("status"), "ready")
        # 来源变化后再生成新 brief
        self.change_source(pid)
        generated_new, current = self.generate(
            pid, _model_content(with_title_zh=True))
        self.assertEqual(generated_new.get("status"), "ready")
        with get_db() as c:
            rows = c.execute(
                "SELECT id, source_hash, brief_json FROM ai_briefs "
                "WHERE paper_id=? AND status='ready' ORDER BY id",
                (pid,)).fetchall()
        self.assertEqual(len(rows), 2)
        old_brief = json.loads(rows[0]["brief_json"])
        new_brief = json.loads(rows[1]["brief_json"])
        # 历史行完整保留旧输出（供历史展示），未被删除/改写
        self.assertEqual(old_brief.get("title_zh"), OLD_TZ)
        self.assertEqual(old_brief.get("abstract_zh"), OLD_AZ)
        # 当前 GET 返回新 brief，含新输入译文，如实 verified
        self.assertEqual(current.get("validity"), "verified")
        self.assertEqual((current.get("brief") or {}).get("title_zh"),
                         "新来源题名的全新译文")
        self.assertNotIn(OLD_TZ, json.dumps(new_brief, ensure_ascii=False))


class TestSyncMetadata(StaleReuseBase):
    """同步侧：papers 直出中文携带 provenance 元数据；stale 跳过。"""

    def _queue_paper(self, pid, doi="10.9999/r2-stale"):
        with get_db() as c:
            zotero_sync.enqueue(c, pid, {"collection_key": "TESTCOL1"})

    def test_papers_direct_zh_carries_provenance_metadata(self):
        pid = self.make_paper()
        with get_db() as c:
            library_source._ensure_provenance_columns(c)
            c.execute("UPDATE papers SET title_zh=? WHERE id=?",
                      (OLD_TZ, pid))
            c.execute(
                "INSERT OR REPLACE INTO content_provenance(paper_id,field,"
                "text,origin,source_hash,model,prompt_version,generated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (pid, "title_zh", OLD_TZ, "ai_with_source",
                 library_migrate.compute_source_hash(OLD_TITLE, OLD_ABS),
                 "fixture-model", ai_brief.ENRICH_FIELD_PROMPT_VERSION,
                 "2026-10-21T00:00:00Z"))
            paper = dict(c.execute("SELECT * FROM papers WHERE id=?",
                                   (pid,)).fetchone())
            arts = zotero_sync._content_artifacts(c, pid, paper)
        art = arts["title_zh"]
        self.assertEqual(art["origin"], "ai_with_source")
        self.assertEqual(art["model"], "fixture-model")
        self.assertEqual(art["prompt_version"],
                         ai_brief.ENRICH_FIELD_PROMPT_VERSION)
        self.assertEqual(art["generated_at"], "2026-10-21T00:00:00Z")
        # 无 provenance 行的字段仍如实标 NULL（不补造）：本夹具 abstract_zh
        # 为空且无 brief，故不产生该产物
        self.assertNotIn("abstract_zh", arts)

    def test_known_ai_not_labeled_library_in_note(self):
        pid = self.make_paper()
        self.seed_ai(pid, model="fixture")  # 哈希与当前一致
        with get_db() as c:
            paper = dict(c.execute("SELECT * FROM papers WHERE id=?",
                                   (pid,)).fetchone())
            arts = zotero_sync._content_artifacts(c, pid, paper)
        note = zotero_sync.build_note_html("title_zh", arts["title_zh"])
        self.assertIn("fixture", note)
        self.assertNotIn("library（非 AI 生成）", note)
        # AI 但模型未记录：如实标注，不冒充 library
        art = dict(arts["title_zh"], model=None)
        note2 = zotero_sync.build_note_html("title_zh", art)
        self.assertIn("AI 生成（模型未记录）", note2)
        self.assertNotIn("library（非 AI 生成）", note2)

    def test_stale_translation_not_synced(self):
        """stale 不得被当当前内容同步（r1 策略：跳过并记原因）。"""
        pid = self.make_paper()
        self.seed_ai(pid)
        self.change_source(pid)  # 来源已变 → 全部 stale
        self._queue_paper(pid)
        stub = _StubZotero({"key": "ITEM1", "data": {
            "key": "ITEM1", "title": NEW_TITLE, "DOI": "10.9999/r2-stale",
            "abstractNote": NEW_ABS, "collections": [],
            "itemType": "journalArticle", "version": 1}})
        with mock.patch.object(ai_brief, "load_ai_config",
                               return_value=CFG):
            conn = db_get_conn()
            try:
                summary = zotero_sync.process_queue(conn, stub)
                rec = conn.execute(
                    "SELECT state, last_error FROM zotero_sync_queue "
                    "WHERE paper_id=?", (pid,)).fetchone()
            finally:
                conn.close()
        self.assertEqual(stub.note_writes, [])
        self.assertGreaterEqual(summary.get("artifacts_skipped", 0), 2)
        self.assertIn("stale", rec["last_error"] or "")

    def test_unverifiable_synced_with_honest_note(self):
        """unverifiable 可同步但笔记内明确标注"有效性未验证"。"""
        pid = self.make_paper()
        # ai_with_source 但无 model/prompt_version 元数据 → unverifiable
        h = library_migrate.compute_source_hash(OLD_TITLE, OLD_ABS)
        with get_db() as c:
            library_source._ensure_provenance_columns(c)
            c.execute("UPDATE papers SET title_zh=? WHERE id=?",
                      (OLD_TZ, pid))
            c.execute(
                "INSERT OR REPLACE INTO content_provenance(paper_id,field,"
                "text,origin,source_hash) VALUES(?,?,?,?,?)",
                (pid, "title_zh", OLD_TZ, "ai_with_source", h))
        self._queue_paper(pid)
        stub = _StubZotero({"key": "ITEM1", "data": {
            "key": "ITEM1", "title": OLD_TITLE, "DOI": "10.9999/r2-stale",
            "abstractNote": OLD_ABS, "collections": [],
            "itemType": "journalArticle", "version": 1}})
        conn = db_get_conn()
        try:
            summary = zotero_sync.process_queue(conn, stub)
        finally:
            conn.close()
        notes = [w["note"] for w in stub.note_writes
                 if "title_zh" in w["note"].splitlines()[0]]
        self.assertEqual(len(notes), 1)
        self.assertIn("有效性未验证", notes[0])
        self.assertIn(OLD_TZ, notes[0])


def db_get_conn():
    """process_queue 需要一个长连接（内部 commit）；与既有套件一致的
    独立连接方式。"""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    unittest.main(verbosity=2)
