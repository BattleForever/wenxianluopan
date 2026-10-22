# -*- coding: utf-8 -*-
"""V4R1-01 反例测试：离线约束——所有模型/外部网络入口统一闸门。

反例（修复前，baseline_v04 复现）：offline_mode=1 + POST /api/enrich/missing
allow_ai=true 时，enrich 的 AI 阶段经 library_source AI 挂钩调用
ai_brief._enrich_provider 三个字段提供者（title_zh/abstract_zh/summary），
各自仍发出 1 次模型请求（共 3 次）——旧版只有 /api/ai/briefs 有离线检查。

修复后：ai_brief.ai_generation_blocked 统一闸门 + call_llm 最底层传输
闸门 + enrich AI 阶段入口闸门 + library_source AI 挂钩闸门（纵深防御），
离线时零模型请求、零网络请求，且返回明确状态（ai_offline）；
已有内容/缓存照常可读。

证明方式：在底层传输（requests.post / requests.get）注入请求计数器，
断言计数器为 0，而不是只检查响应文案。全部使用合成桩，无真实外呼。

运行：python tests/test_v04r1_offline_gate.py  （exit 0 = 通过）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TRACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TRACK))
sys.path.insert(0, str(TRACK / "tests"))

# ---- 进程级隔离：必须在导入 app 前设置数据目录（本文件独立进程运行） ----
_TMP = Path(tempfile.mkdtemp(prefix="v04r1_offline_"))
os.environ["PAPERNET_DATA_DIR"] = str(_TMP / "data")
os.environ.pop("PAPERNET_AI_CONFIG_FILE", None)  # 未配置状态（零模型请求路径）

from _v04_runtime import build_runtime  # noqa: E402

# 修复前基线 = parallel_v04 integrated（V4R1 起点；baseline_readonly 无 enrich）
BASELINE = Path(os.environ.get('PAPERNET_TEST_BASELINE') or TRACK / 'tests/fixtures/optional_baseline')

# 修复前反例探针：对指定 app 根（baseline 或修复后源码）构建运行时并在
# 子进程执行同一场景（offline_mode=1 + allow_ai=true），底层传输计数。
_PROBE_SRC = r'''
import json, os, sqlite3, sys
from pathlib import Path
import unittest.mock as mock
import requests

RT = Path(sys.argv[1]); OUT = Path(sys.argv[2])
os.environ["PAPERNET_DATA_DIR"] = str(RT / "data")
cfg_path = RT / "fake_ai_config.json"
cfg_path.write_text(json.dumps({"llm": {
    "chat_completions_url": "https://modelgateway.zju.edu.cn/v1/chat/completions",
    "api_key": "synthetic-test-only-key", "model": "synthetic-model"}}),
    encoding="utf-8")
os.environ["PAPERNET_AI_CONFIG_FILE"] = str(cfg_path)
sys.path.insert(0, str(RT))

CALLS = {"post": 0, "get": 0}

def fake_post(url, **kw):
    CALLS["post"] += 1
    r = mock.Mock(status_code=200)
    content = json.dumps({
        "title_zh": "合成译名", "abstract_zh": "合成摘要译文",
        "abstract_en": "Synthetic summary.",
        "question": {"zh": "问题", "en": "Q", "quote": ""},
        "method": {"zh": "方法", "en": "M", "quote": ""},
        "results": {"zh": "结果", "en": "R", "quote": ""},
        "contribution": {"zh": "贡献", "en": "C", "quote": ""}},
        ensure_ascii=False)
    r.json.return_value = {"model": "synthetic-model",
                           "choices": [{"message": {"content": content}}]}
    return r

def fake_get(url, **kw):
    CALLS["get"] += 1
    r = mock.Mock(status_code=200)
    r.json.return_value = {"message": {"title": ["Net Title"],
                                       "abstract": "Net abstract."}}
    return r

with mock.patch.object(requests, "post", fake_post), \
        mock.patch.object(requests, "get", fake_get):
    from app import main as appmain
    from app import db as appdb
    appdb.init_db()
    from fastapi.testclient import TestClient
    client = TestClient(appmain.app)
    conn = sqlite3.connect(str(RT / "data" / "papernet.db"))
    conn.execute("INSERT INTO papers(id, doi, title, abstract, status) "
                 "VALUES(1, '10.1000/demo', 'Demo Title', "
                 "'Demo abstract text for the offline counterexample.', "
                 "'library')")
    conn.execute("INSERT INTO papers(id, doi, title, abstract, status) "
                 "VALUES(2, '10.1000/demo2', 'Demo Title 2', "
                 "'Demo abstract text 2.', 'library')")
    conn.execute("INSERT INTO settings(key, value) VALUES('offline_mode','1') "
                 "ON CONFLICT(key) DO UPDATE SET value='1'")
    conn.commit(); conn.close()
    r_enrich = client.post("/api/enrich/missing",
                           json={"paper_id": 1, "allow_network": True,
                                 "allow_ai": True})
    r_brief_post = client.post("/api/ai/briefs", json={"paper_id": 2})
    r_brief_get = client.get("/api/ai/briefs/2")

result = {
    "enrich_http": r_enrich.status_code,
    "model_calls": CALLS["post"],
    "network_calls": CALLS["get"],
    "enrich_fields": r_enrich.json().get("fields"),
    "enrich_errors": r_enrich.json().get("errors"),
    "brief_post_status": r_brief_post.json().get("status"),
    "brief_post_error": r_brief_post.json().get("error"),
    "brief_get_status": r_brief_get.json().get("status"),
}
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
               encoding="utf-8")
print("PROBE_JSON=" + json.dumps(result, ensure_ascii=False))
'''


def run_probe(app_root: Path, tag: str) -> dict:
    """构建运行时副本并在子进程跑探针，返回结果 JSON。"""
    import subprocess
    rt = build_runtime(app_root=app_root)
    probe = rt / "v04r1_probe_offline.py"
    probe.write_text(_PROBE_SRC, encoding="utf-8")
    out = rt / f"probe_{tag}.json"
    p = subprocess.run([sys.executable, str(probe), str(rt), str(out)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    if p.returncode != 0:
        raise AssertionError(
            f"探针运行失败({tag}) exit={p.returncode}\n{p.stdout}\n{p.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def _seed_paper(paper_id: int, title: str, abstract: str) -> None:
    from app import db as appdb
    with appdb.get_db() as conn:
        conn.execute(
            "INSERT INTO papers(id, doi, title, abstract, status) "
            "VALUES(?,?,?,?, 'library')",
            (paper_id, f"10.1000/p{paper_id}", title, abstract))


class OfflineGateHTTPTests(unittest.TestCase):
    """修复后（lane_A 源码）：离线零外呼 + 明确状态 + 历史/缓存可读。"""

    @classmethod
    def setUpClass(cls):
        from app import main as appmain
        from app import db as appdb
        appdb.init_db()
        cls.app = appmain.app
        cls.appdb = appdb
        cls.client = None

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(self.app)
        self.counter = {"post": 0, "get": 0}

        def fake_post(url, **kw):
            self.counter["post"] += 1
            r = mock.Mock(status_code=200)
            r.json.return_value = {"model": "synthetic-model", "choices": [
                {"message": {"content": json.dumps({
                    "title_zh": "合成译名", "abstract_zh": "合成摘要译文",
                    "abstract_en": "Synthetic summary.",
                    "question": {"zh": "问", "en": "Q", "quote": ""},
                    "method": {"zh": "法", "en": "M", "quote": ""},
                    "results": {"zh": "果", "en": "R", "quote": ""},
                    "contribution": {"zh": "献", "en": "C", "quote": ""}},
                    ensure_ascii=False)}}]}
            return r

        def fake_get(url, **kw):
            self.counter["get"] += 1
            r = mock.Mock(status_code=200)
            r.json.return_value = {"message": {"title": ["Net Title"],
                                               "abstract": "Net abstract."}}
            return r

        self.patches = [
            mock.patch("requests.post", fake_post),
            mock.patch("requests.get", fake_get),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _set_offline(self, on: bool):
        with self.appdb.get_db() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('offline_mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if on else "0"))

    def test_offline_enrich_allow_ai_zero_external_calls(self):
        """反例修复证明：offline=1 + allow_ai=true → 底层传输计数=0。"""
        with self.appdb.get_db() as conn:
            conn.execute(
                "INSERT INTO papers(id, doi, title, abstract, status) "
                "VALUES(31, '10.1000/p31', 'Offline Paper', "
                "'Offline abstract body.', 'library')")
        self._set_offline(True)
        try:
            r = self.client.post("/api/enrich/missing",
                                 json={"paper_id": 31, "allow_network": True,
                                       "allow_ai": True})
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(self.counter["post"], 0,
                             "离线模式下不得发出任何模型请求（计数器必须为 0）")
            self.assertEqual(self.counter["get"], 0,
                             "离线模式下不得发出任何网络书目请求")
            codes = {e["code"] for e in data["errors"]}
            self.assertIn("ai_offline", codes,
                          f"必须返回明确状态 ai_offline，实际 {codes}")
            for f in ("title_zh", "abstract_zh", "summary"):
                self.assertEqual(data["fields"][f]["after"], "missing",
                                 "离线时不得填充 AI 字段")
        finally:
            self._set_offline(False)

    def test_offline_brief_post_unavailable_and_get_readable(self):
        """离线时 AI 速览不外呼（unavailable 明确状态），已有缓存仍可读。"""
        with self.appdb.get_db() as conn:
            conn.execute(
                "INSERT INTO papers(id, doi, title, abstract, status) "
                "VALUES(32, '10.1000/p32', 'Brief Paper', "
                "'Brief abstract body.', 'library')")
        self._set_offline(True)
        try:
            r = self.client.post("/api/ai/briefs", json={"paper_id": 32})
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual(self.counter["post"], 0, "离线生成必须零模型请求")
            self.assertEqual(data["status"], "unavailable")
            self.assertEqual((data.get("error") or {}).get("code"),
                             "offline_mode")
            # 已有内容/缓存照常可读：GET 绝不联网
            g = self.client.get("/api/ai/briefs/32")
            self.assertEqual(g.status_code, 200)
            self.assertEqual(self.counter["post"], 0)
            self.assertEqual(self.counter["get"], 0)
            self.assertEqual(g.json()["status"], "not_generated")
        finally:
            self._set_offline(False)

    def test_unconfigured_allow_ai_zero_model_calls(self):
        """配置缺失 + allow_ai=true → 零模型请求 + 明确状态 ai_not_configured。"""
        with self.appdb.get_db() as conn:
            conn.execute(
                "INSERT INTO papers(id, doi, title, abstract, status) "
                "VALUES(33, '10.1000/p33', 'NoCfg Paper', "
                "'NoCfg abstract body.', 'library')")
        self._set_offline(False)
        r = self.client.post("/api/enrich/missing",
                             json={"paper_id": 33, "allow_ai": True})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(self.counter["post"], 0,
                         "配置缺失时不得发出任何模型请求")
        codes = {e["code"] for e in data["errors"]}
        self.assertIn("ai_not_configured", codes,
                      f"配置缺失必须返回明确状态，实际 {codes}")
        b = self.client.post("/api/ai/briefs", json={"paper_id": 33})
        self.assertEqual(self.counter["post"], 0)
        self.assertEqual(b.json()["status"], "unavailable")
        self.assertEqual((b.json().get("error") or {}).get("code"),
                         "not_configured")


class BaselineCounterexampleTests(unittest.TestCase):
    """修复前证据：baseline_v04 上复现 V4R1-01（模型仍被调用 3 次）。

    需要 baseline_readonly 目录；缺失时跳过（不阻塞套件）。"""

    def test_baseline_offline_enrich_still_calls_model(self):
        if not BASELINE.is_dir():
            raise unittest.SkipTest(
                f"baseline_readonly 不存在: {BASELINE}")
        result = run_probe(BASELINE, "baseline")
        print("[baseline 反例] model_calls=%s network_calls=%s "
              "brief_post_status=%s" % (result["model_calls"],
                                        result["network_calls"],
                                        result["brief_post_status"]))
        self.assertEqual(result["model_calls"], 3,
                         "baseline 反例：offline+allow_ai 应仍产生 3 次模型调用")
        codes = {e["code"] for e in (result["enrich_errors"] or [])}
        self.assertNotIn("ai_offline", codes,
                         "baseline 无离线 AI 闸门（不应出现 ai_offline）")


class FixedLaneProbeTests(unittest.TestCase):
    """修复后（lane_A 源码运行时副本）：同一探针场景零外呼。"""

    def test_fixed_offline_enrich_zero_model_calls(self):
        result = run_probe(TRACK, "fixed")
        print("[fixed] model_calls=%s network_calls=%s brief_post_status=%s"
              % (result["model_calls"], result["network_calls"],
                 result["brief_post_status"]))
        self.assertEqual(result["model_calls"], 0,
                         "修复后离线 enrich 必须零模型请求")
        self.assertEqual(result["network_calls"], 0,
                         "修复后离线 enrich 必须零网络请求")
        codes = {e["code"] for e in (result["enrich_errors"] or [])}
        self.assertIn("ai_offline", codes)
        self.assertEqual(result["brief_post_status"], "unavailable")
        self.assertEqual((result["brief_post_error"] or {}).get("code"),
                         "offline_mode")
        # 离线时已有内容/缓存读取不受影响
        self.assertIn(result["brief_get_status"],
                      ("not_generated", "ready", "missing_source"))


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
