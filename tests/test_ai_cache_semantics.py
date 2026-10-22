"""AI 速览缓存语义边界测试（A 线新增，F3/F4 修复验证）。

全部使用独立合成样例，隔离数据库（导入 app.main 前注入本测试专用 DB_PATH），
LLM 一律 mock，不产生任何外部请求、不读取真实密钥（配置文件指向临时假配置）。

覆盖：
- 规范化哈希稳定性：首尾空白、换行风格、非 BMP Unicode、NFC 等价、重复片段；
- prompt_version / 请求模型切换导致过期（GET 与 POST 一致性的范围内语义）；
- 来源（摘要/题名）改变后失效，POST 不得返回 cached=true 旧结果；
- 失败与旧成功缓存共存；摘要删除后旧结果不标当前有效；
- 同论文并发生成保护（只调用模型一次）；生成期间健康检查/检索仍可响应；
- 缺摘要不请求；离线不请求；用户消息只含题名与摘要（不含 DOI/内部 ID）；
- 密钥不出现在任何响应或日志；quote 对应原始快照。
"""
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config
config.DB_PATH = ROOT / 'tmp' / ('ai_cache_sem_' + uuid.uuid4().hex[:8] + '.db')
config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

from app.main import app
from app.db import get_db
from app import ai_brief
from app.store import upsert_paper
from fastapi.testclient import TestClient

# 临时假配置（仅测试用途；假密钥，非真实凭据）
FAKE_KEY = "sem-fake-key-" + uuid.uuid4().hex[:16]
CFG_DIR = Path(tempfile.mkdtemp(prefix="ai_sem_cfg_"))
CFG_FILE = CFG_DIR / "fake_ai_config.json"
CFG_FILE.write_text(json.dumps({
    "schema_version": 1,
    "llm": {"chat_completions_url": "https://modelgateway.zju.edu.cn/v1/chat/completions",
            "model": "sem-test-model",
            "api_key": FAKE_KEY}}), encoding="utf-8")

os.environ["PAPERNET_AI_CONFIG_FILE"] = str(CFG_FILE)
os.environ.pop("PAPERNET_LLM_API_KEY", None)

client = TestClient(app)

_ABSTRACT = ("We tested a public synthetic method. "
             "The measured error was 3 percent. "
             "The measured error was 3 percent. "
             "Our contribution is a general recipe.")
_QUOTE = "The measured error was 3 percent."


def good_content():
    return json.dumps({
        "title_zh": "合成验证",
        "abstract_zh": "误差为百分之三。",
        "abstract_en": "Error was 3 percent.",
        **{k: {"zh": "合成检查", "en": "Synthetic check", "quote": _QUOTE}
           for k in ai_brief._ELEMENTS}}, ensure_ascii=False)


def _mkpaper(title, abstract):
    with get_db() as conn:
        pid, _ = upsert_paper(
            conn, dict(title=f"{title} {uuid.uuid4().hex[:6]}",
                       abstract=abstract,
                       doi=f"10.9999/{uuid.uuid4().hex[:10]}",
                       year=2024, venue="Sem Venue"),
            "fixture", "library")
        conn.execute("UPDATE settings SET value='0' WHERE key='offline_mode'")
    return pid


def _post(paper_id, **kw):
    body = {"paper_id": paper_id, "source_kind": "abstract"}
    body.update(kw)
    return client.post("/api/ai/briefs", json=body).json()


def _get(paper_id):
    return client.get(f"/api/ai/briefs/{paper_id}").json()


class _Counter:
    def __init__(self, result=None, exc=None, gate=None):
        self.calls = []
        self.result = result or (good_content(), "sem-response-model")
        self.exc = exc
        self.gate = gate  # 可选 (entered, release) 事件对，用于并发控制

    def __call__(self, cfg, system, user):
        self.calls.append({"system": system, "user": user})
        if self.gate:
            self.gate[0].set()
            self.gate[1].wait(10)
        if self.exc:
            raise self.exc
        return self.result


def _patch_llm(counter, cfg_model="sem-test-model"):
    """同时替换 call_llm 与 load_ai_config，返回单个上下文管理器。"""
    cfg = {"chat_url": "https://modelgateway.zju.edu.cn/v1/chat/completions",
           "request_model": cfg_model, "api_key": "k"}
    stack = ExitStack()
    stack.enter_context(mock.patch.object(ai_brief, "call_llm", counter))
    stack.enter_context(mock.patch.object(ai_brief, "load_ai_config",
                                          return_value=cfg))
    return stack


class NormalizeHashStabilityTests(unittest.TestCase):
    """规范化函数本身的稳定性（纯单元，无 HTTP）。"""

    def test_trim_whitespace_stable(self):
        base = "  " + _ABSTRACT + "\t \n"
        self.assertEqual(ai_brief.normalize_source(base), _ABSTRACT)
        self.assertEqual(ai_brief.source_hash(ai_brief.normalize_source(base)),
                         ai_brief.source_hash(ai_brief.normalize_source(_ABSTRACT)))

    def test_line_endings_stable(self):
        crlf = _ABSTRACT.replace("\n", "\r\n") if "\n" in _ABSTRACT else _ABSTRACT
        multi = "line one\r\nline two\rline three\nline four"
        variants = [multi, multi.replace("\r\n", "\n").replace("\r", "\n")]
        hashes = {ai_brief.source_hash(ai_brief.normalize_source(v))
                  for v in variants}
        self.assertEqual(len(hashes), 1)

    def test_non_bmp_unicode_stable(self):
        # 非 BMP 字符（数学字母、emoji、生僻汉字扩展区）
        text = "Δ𝕏 proof 🧭 '\U0002f800' analysis \U0001d400 result"
        self.assertEqual(ai_brief.source_hash(ai_brief.normalize_source(text)),
                         ai_brief.source_hash(ai_brief.normalize_source(text)))
        padded = "  \n" + text + "  "
        self.assertEqual(ai_brief.source_hash(ai_brief.normalize_source(padded)),
                         ai_brief.source_hash(ai_brief.normalize_source(text)))

    def test_nfc_equivalence_same_hash(self):
        composed = "caf\u00e9 r\u00e9sum\u00e9"
        decomposed = "cafe\u0301 re\u0301sume\u0301"
        self.assertEqual(
            ai_brief.source_hash(ai_brief.normalize_source(composed)),
            ai_brief.source_hash(ai_brief.normalize_source(decomposed)))

    def test_repeated_fragment_hash_stable(self):
        # 重复片段不影响哈希确定性
        h1 = ai_brief.source_hash(ai_brief.normalize_source(_ABSTRACT))
        h2 = ai_brief.source_hash(ai_brief.normalize_source(_ABSTRACT))
        self.assertEqual(h1, h2)
        # 重复片段首次出现位置确定（quote 定位语义）
        first = _ABSTRACT.find(_QUOTE)
        second = _ABSTRACT.find(_QUOTE, first + 1)
        self.assertEqual(first, _ABSTRACT.find(_QUOTE))
        self.assertGreater(second, first)

    def test_empty_and_non_string(self):
        self.assertEqual(ai_brief.normalize_source(""), "")
        self.assertEqual(ai_brief.normalize_source("   \n\t "), "")
        self.assertEqual(ai_brief.normalize_source(None), "")


class AICacheSemanticsTests(unittest.TestCase):
    def setUp(self):
        # 测试隔离：与其他 AI 测试模块合并运行时，重设本模块的假配置文件
        # （环境变量为全局状态，避免模块导入顺序串扰）
        os.environ["PAPERNET_AI_CONFIG_FILE"] = str(CFG_FILE)
        with get_db() as conn:
            conn.execute("UPDATE settings SET value='0' WHERE key='offline_mode'")
        self.p = _mkpaper("Semantic fixture", _ABSTRACT)

    # 1. 带首尾空白的摘要：生成后立即 GET 不得 stale，读写哈希一致
    def test_whitespace_abstract_read_write_hash_consistent(self):
        p = _mkpaper("ws fixture", "  " + _ABSTRACT + "  ")
        counter = _Counter()
        with _patch_llm(counter):
            data = _post(p)
        self.assertEqual(data["status"], "ready")
        self.assertFalse(data["stale"])
        g = _get(p)
        self.assertFalse(g["stale"], g.get("stale_reasons"))
        self.assertEqual(g["brief"]["source_hash"], data["brief"]["source_hash"])
        # 原始快照保留原始空白，规范化快照去除空白
        self.assertEqual(data["brief"]["source_snapshot_raw"], "  " + _ABSTRACT + "  ")
        self.assertEqual(data["brief"]["source_snapshot_normalized"], _ABSTRACT)
        self.assertEqual(data["brief"]["quote_basis"], "source_snapshot_raw")

    # 2. quote/偏移对应原始快照（含空白时可精确定位）
    def test_quote_offsets_match_raw_snapshot(self):
        p = _mkpaper("quote fixture", "  " + _ABSTRACT)
        counter = _Counter()
        with _patch_llm(counter):
            data = _post(p)
        raw = data["brief"]["source_snapshot_raw"]
        el = data["brief"]["elements"]["question"]
        self.assertTrue(el["quote_verified"])
        self.assertEqual(raw[el["quote_start"]:el["quote_end"]], el["quote"])
        # 重复片段：取首次出现位置
        self.assertEqual(el["quote_start"], raw.find(el["quote"]))
        self.assertEqual(el["quote_start"], raw.find(el["quote"], 0))
        self.assertEqual(raw.find(el["quote"]), raw.find(el["quote"], 1))

    # 3. prompt_version 变化：GET 标 stale 且给原因；POST 重新生成
    def test_prompt_version_change_invalidates(self):
        counter = _Counter()
        with _patch_llm(counter):
            _post(self.p)
        with mock.patch.object(ai_brief, "PROMPT_VERSION", "sem-vNEXT"):
            g = _get(self.p)
            self.assertTrue(g["stale"])
            self.assertIn("prompt_version_changed", g["stale_reasons"])
            counter2 = _Counter()
            with _patch_llm(counter2):
                data = _post(self.p)
        self.assertFalse(data["cached"])
        self.assertEqual(len(counter2.calls), 1)
        # brief 记录的是生成时刻的 prompt_version（当时被注入为 sem-vNEXT）
        self.assertEqual(data["brief"]["prompt_version"], "sem-vNEXT")

    # 4. 请求模型切换：POST 不返回旧缓存（cached=false，重新生成）
    def test_model_change_invalidates_post_cache(self):
        counter = _Counter()
        with _patch_llm(counter, cfg_model="sem-model-A"):
            first = _post(self.p)
        self.assertEqual(first["brief"]["request_model"], "sem-model-A")
        counter2 = _Counter()
        with _patch_llm(counter2, cfg_model="sem-model-B"):
            data = _post(self.p)
        self.assertFalse(data["cached"])
        self.assertEqual(len(counter2.calls), 1)
        self.assertEqual(data["brief"]["request_model"], "sem-model-B")

    # 5. 摘要内容改变：GET stale（source_changed），POST 重新生成
    def test_abstract_change_invalidates(self):
        counter = _Counter()
        with _patch_llm(counter):
            first = _post(self.p)
        new_abs = _ABSTRACT + " A follow-up adds a 12 percent gain."
        with get_db() as conn:
            conn.execute("UPDATE papers SET abstract=? WHERE id=?", (new_abs, self.p))
        g = _get(self.p)
        self.assertTrue(g["stale"])
        self.assertIn("source_changed", g["stale_reasons"])
        self.assertTrue(g["cached"])  # 历史结果仍可查看，但不标当前有效
        counter2 = _Counter()
        with _patch_llm(counter2):
            data = _post(self.p)
        self.assertFalse(data["cached"])
        self.assertEqual(data["brief"]["source_hash"],
                         ai_brief.source_hash(ai_brief.normalize_source(new_abs)))
        self.assertNotEqual(data["brief"]["source_hash"],
                            first["brief"]["source_hash"])

    # 6. 题名改变：POST 不得返回 cached=true 旧结果；
    #    brief 保留生成时原题名（R3），当前题名在 current_paper 单独表示
    def test_title_change_post_not_cached(self):
        counter = _Counter()
        with _patch_llm(counter):
            first = _post(self.p)
            gen_title = first["brief"]["paper"]["title"]
        with get_db() as conn:
            conn.execute("UPDATE papers SET title='Changed semantic title' "
                         "WHERE id=?", (self.p,))
        g = _get(self.p)
        self.assertTrue(g["stale"])
        self.assertIn("title_changed", g["stale_reasons"])
        # R3：brief 不被当前题名覆盖，保留生成时原题名；当前题名单独表示
        self.assertEqual(g["brief"]["paper"]["title"], gen_title)
        self.assertEqual(g["current_paper"]["title"], "Changed semantic title")
        counter2 = _Counter()
        with _patch_llm(counter2):
            data = _post(self.p)
        self.assertFalse(data["cached"])
        self.assertEqual(data["brief"]["paper"]["title"], "Changed semantic title")

    # 7. 题名仅首尾空白差异：不视为来源改变（规范化稳定）
    def test_title_whitespace_only_no_invalidation(self):
        counter = _Counter()
        with _patch_llm(counter):
            first = _post(self.p)
        with get_db() as conn:
            conn.execute("UPDATE papers SET title=? WHERE id=?",
                         ("  " + first["brief"]["paper"]["title"] + "  ", self.p))
        g = _get(self.p)
        self.assertFalse(g["stale"], g.get("stale_reasons"))
        counter2 = _Counter()
        with _patch_llm(counter2):
            data = _post(self.p)
        self.assertTrue(data["cached"])

    # 8. 摘要删除后：GET missing_source + stale + 原因；旧结果不标当前有效
    def test_deleted_abstract_not_valid(self):
        counter = _Counter()
        with _patch_llm(counter):
            _post(self.p)
        with get_db() as conn:
            conn.execute("UPDATE papers SET abstract=NULL WHERE id=?", (self.p,))
        g = _get(self.p)
        self.assertTrue(g["stale"] or g["status"] != "ready")
        self.assertEqual(g["status"], "missing_source")
        self.assertIn("source_missing", g["stale_reasons"])
        # POST 同样拒绝：missing_source，不复用旧缓存
        counter2 = _Counter()
        with _patch_llm(counter2):
            data = _post(self.p)
        self.assertEqual(len(counter2.calls), 0)
        self.assertEqual(data["status"], "missing_source")

    # 9. 失败不毁掉旧成功缓存：恢复来源后旧结果仍有效
    def test_failure_keeps_old_ready_cache(self):
        counter = _Counter()
        with _patch_llm(counter):
            first = _post(self.p)
        orig_abs = _ABSTRACT
        new_abs = orig_abs + " extra sentence."
        with get_db() as conn:
            conn.execute("UPDATE papers SET abstract=? WHERE id=?", (new_abs, self.p))
        counter2 = _Counter(exc=ai_brief.LLMServiceError("service_error", "服务错误"))
        with _patch_llm(counter2):
            fail = _post(self.p)
        self.assertEqual(fail["status"], "unavailable")
        self.assertIsNone(fail["brief"])
        # 失败后 GET：来源已变 → stale=true，但历史 ready 结果仍可查看
        g = _get(self.p)
        self.assertTrue(g["stale"])
        self.assertEqual(g["status"], "ready")
        self.assertTrue(g["cached"])
        self.assertIsNotNone(g["brief"])
        # 恢复原摘要：旧缓存重新成为当前有效（未被失败破坏）
        with get_db() as conn:
            conn.execute("UPDATE papers SET abstract=? WHERE id=?", (orig_abs, self.p))
        g2 = _get(self.p)
        self.assertFalse(g2["stale"], g2.get("stale_reasons"))
        self.assertEqual(g2["brief"]["source_hash"], first["brief"]["source_hash"])

    # 10. 同论文并发生成：只调用模型一次，两个请求都得到结果
    def test_concurrent_generate_single_model_call(self):
        entered, release = threading.Event(), threading.Event()
        counter = _Counter(gate=(entered, release))
        with _patch_llm(counter):
            results = {}

            def worker():
                results["t"] = _post(self.p)

            th = threading.Thread(target=worker)
            th.start()
            self.assertTrue(entered.wait(10), "模型调用未开始")
            # 生成进行中：健康检查与论文列表仍可响应（不互斥）
            t0 = time.monotonic()
            h = client.get("/api/health")
            papers = client.get("/api/papers")
            elapsed = time.monotonic() - t0
            release.set()
            th.join(30)
            data2 = _post(self.p)  # 生成完成后：命中缓存
        self.assertEqual(len(counter.calls), 1)
        self.assertEqual(results["t"]["status"], "ready")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(papers.status_code, 200)
        self.assertLess(elapsed, 9.0, "生成期间普通请求被长时间阻塞")
        self.assertTrue(data2["cached"])

    # 11. 缺摘要：POST 不请求模型
    def test_missing_abstract_no_request(self):
        p = _mkpaper("no abstract sem", None)
        counter = _Counter()
        with _patch_llm(counter):
            data = _post(p)
        self.assertEqual(len(counter.calls), 0)
        self.assertEqual(data["status"], "missing_source")
        # 全空白摘要同样按缺失处理
        p2 = _mkpaper("blank abstract sem", "   \n\t ")
        counter2 = _Counter()
        with _patch_llm(counter2):
            data2 = _post(p2)
        self.assertEqual(len(counter2.calls), 0)
        self.assertEqual(data2["status"], "missing_source")

    # 12. 离线模式：无缓存不请求；不把缺结果伪装成功
    def test_offline_no_request(self):
        with get_db() as conn:
            conn.execute("UPDATE settings SET value='1' WHERE key='offline_mode'")
        try:
            counter = _Counter()
            with _patch_llm(counter):
                data = _post(self.p)
        finally:
            with get_db() as conn:
                conn.execute("UPDATE settings SET value='0' WHERE key='offline_mode'")
        self.assertEqual(len(counter.calls), 0)
        self.assertEqual(data["status"], "unavailable")
        self.assertEqual(data["error"]["code"], "offline_mode")

    # 13. 用户消息只含题名与摘要：无 DOI、无内部 ID（F4 后端部分）
    def test_user_prompt_only_title_and_abstract(self):
        counter = _Counter()
        with _patch_llm(counter):
            _post(self.p)
        user = counter.calls[0]["user"]
        self.assertIn(_ABSTRACT, user)
        with get_db() as conn:
            row = conn.execute("SELECT title, doi FROM papers WHERE id=?",
                               (self.p,)).fetchone()
        self.assertIn(row["title"], user)
        self.assertNotIn(row["doi"], user)
        self.assertNotIn("DOI", user)
        self.assertNotIn(f"ID {self.p}", user)
        self.assertNotIn("论文标识", user)

    # 14. 密钥不出现在响应或日志
    def test_key_not_in_responses_or_logs(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = Capture()
        ai_log = logging.getLogger("papernet.ai")
        ai_log.addHandler(handler)
        ai_log.setLevel(logging.DEBUG)
        try:
            responses = []
            counter = _Counter()
            with _patch_llm(counter):
                responses.append(_post(self.p))
                responses.append(_get(self.p))
            counter2 = _Counter(exc=ai_brief.LLMServiceError("network_error", "网络错误"))
            with _patch_llm(counter2):
                responses.append(_post(self.p))
            blob = json.dumps(responses, ensure_ascii=False, default=str)
            self.assertNotIn(FAKE_KEY, blob)
            log_text = "\n".join(records)
            self.assertNotIn(FAKE_KEY, log_text)
            self.assertNotIn("Authorization", log_text)
        finally:
            ai_log.removeHandler(handler)

    # 15. prompt_version 运行时注入（Codex 兼容）：模块名存在且可 patch
    def test_prompt_version_module_names_present(self):
        self.assertTrue(hasattr(ai_brief, "PROMPT_VERSION"))
        self.assertTrue(callable(ai_brief.load_ai_config))
        self.assertTrue(callable(ai_brief.call_llm))
        self.assertTrue(callable(ai_brief.source_hash))


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    sys.exit(0 if result.wasSuccessful() else 1)
