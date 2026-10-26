"""并行冲刺 A 线新增测试：可测网络计数（HTTP 次数 / 缓存命中 / 失败）与
超时/429/离线失败在 netclient 层的行为。

全部 mock _SESSION.get，不联网；数据库使用隔离 fixture。
运行:  <python> tests/test_graph_netstats.py
"""
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, Mock

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config
config.DB_PATH = ROOT / 'evidence' / ('graph_net_fixture_' + uuid.uuid4().hex[:8] + '.db')

from app import db, netclient
from app.db import get_db, init_db

init_db()


class MockResp(Mock):
    def __init__(self, status_code=200, payload=None, text=None):
        super().__init__()
        self.status_code = status_code
        self._text = text if text is not None else json.dumps(payload or {})
        self.url = 'https://api.openalex.org/mock'

    @property
    def text(self):
        return self._text


class NetStatsChecks(unittest.TestCase):
    """每个用例独立 fixture + 独立计数基线。"""

    def setUp(self):
        self.db_path = ROOT / 'evidence' / ('graph_net_fixture_' + uuid.uuid4().hex[:8] + '.db')
        db.DB_PATH = self.db_path
        init_db()
        self.baseline = netclient.net_stats_snapshot()

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            try:
                os.remove(str(self.db_path) + suffix)
            except OSError:
                pass

    def delta(self):
        cur = netclient.net_stats_snapshot()
        return {k: cur[k] - self.baseline[k] for k in cur}

    def set_offline(self, value):
        with get_db() as conn:
            conn.execute("UPDATE settings SET value=? WHERE key='offline_mode'",
                         (value,))

    def test_http_request_counted_once_then_cache_hit(self):
        resp = MockResp(200, {'id': 'https://openalex.org/W1', 'display_name': 'x'})
        with patch.object(netclient._SESSION, 'get', return_value=resp) as mock_get:
            first = netclient.cached_get_json('https://api.openalex.org/works/W1')
            second = netclient.cached_get_json('https://api.openalex.org/works/W1')
        self.assertEqual(first, second)
        self.assertEqual(mock_get.call_count, 1)
        d = self.delta()
        self.assertEqual(d['http_requests'], 1, '冷缓存恰好一次 HTTP 请求')
        self.assertEqual(d['cache_hits'], 1, '第二次应为缓存命中')
        self.assertEqual(d['http_errors'], 0)

    def test_timeout_counted_as_error_and_raises(self):
        with patch.object(netclient._SESSION, 'get',
                          side_effect=requests.exceptions.Timeout('mock timeout')):
            with self.assertRaises(requests.exceptions.Timeout):
                netclient.cached_get_json('https://api.openalex.org/works/Wslow',
                                          use_cache=False)
        d = self.delta()
        self.assertEqual(d['http_requests'], 1)
        self.assertEqual(d['http_errors'], 1)

    def test_429_raises_and_counted(self):
        with patch.object(netclient._SESSION, 'get', return_value=MockResp(429)):
            with self.assertRaises(RuntimeError) as ctx:
                netclient.cached_get_json('https://api.openalex.org/works/W429',
                                          use_cache=False)
        self.assertIn('429', str(ctx.exception))
        d = self.delta()
        self.assertEqual(d['http_requests'], 1)
        self.assertEqual(d['http_errors'], 1)

    def test_404_cached_without_error(self):
        with patch.object(netclient._SESSION, 'get', return_value=MockResp(404)):
            self.assertIsNone(netclient.cached_get_json(
                'https://api.openalex.org/works/W404', use_cache=False))
            # 404 也被缓存：再次查询直接命中缓存返回 None
            self.assertIsNone(netclient.cached_get_json(
                'https://api.openalex.org/works/W404'))
        d = self.delta()
        self.assertEqual(d['http_requests'], 1)
        self.assertEqual(d['http_errors'], 0)
        self.assertEqual(d['cache_hits'], 1)

    def test_offline_without_cache_raises(self):
        self.set_offline('1')
        with self.assertRaises(netclient.OfflineError):
            netclient.cached_get_json('https://api.openalex.org/works/Woff',
                                      use_cache=False)

    def test_forbidden_host_rejected(self):
        with self.assertRaises(netclient.ForbiddenHostError):
            netclient.cached_get_json('https://evil.example.org/works/W1',
                                      use_cache=False)

    def test_walk_reports_network_delta_separately_from_fetches(self):
        """节点抓取次数（fetches）与实际 HTTP 次数分开计数：一次真实链路的
        bounded_fetch 会产生 1 次单作品请求 + 1 次批量请求 + 1 次 cites 请求。"""
        from app.store import upsert_paper
        from app import exploration
        self.set_offline('0')
        with get_db() as conn:
            seed = upsert_paper(conn, dict(
                doi='10.0000/net-seed', title='Network stats seed paper'),
                'graph_net_fixture')[0]
        work = {'id': 'https://openalex.org/W9001',
                'doi': 'https://doi.org/10.0000/net-seed',
                'display_name': 'Network stats seed paper', 'cited_by_count': 3,
                'referenced_works': ['https://openalex.org/W9002'],
                'related_works': []}
        refw = {'id': 'https://openalex.org/W9002',
                'doi': 'https://doi.org/10.0000/net-ref',
                'display_name': 'Network stats reference', 'cited_by_count': 1,
                'referenced_works': [], 'related_works': []}
        responses = None  # URL 分发见 fake_get

        def fake_get(url, params=None, headers=None, timeout=None, proxies=None):
            self.assertEqual(proxies, netclient.network.DIRECT)
            if url.endswith('/works/doi:10.0000/net-seed') or url.endswith('/works/W9001'):
                return MockResp(200, work)
            if url.endswith('/works'):
                p = params or {}
                f = str(p.get('filter', ''))
                if f.startswith('openalex:'):
                    return MockResp(200, {'results': [refw], 'meta': {}})
                if f.startswith('cites:'):
                    return MockResp(200, {'results': [], 'meta': {}})
            raise AssertionError(f'unexpected url: {url} {params}')

        with patch.object(netclient._SESSION, 'get', side_effect=fake_get):
            job = exploration.walk([seed], direction='both', depth=1,
                                   online=True, max_fetch=2, per_direction=5)
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['fetches'], 1, '一个种子节点一次节点抓取')
        d = self.delta()
        self.assertEqual(d['http_requests'], job['network']['http_requests'])
        self.assertGreaterEqual(job['network']['http_requests'], 2,
                                '单作品 + 批量引用请求应分别计入 HTTP 次数')
        self.assertEqual(job['network']['cache_hits'], 0)
        self.assertEqual({p['id'] for p in job['results']} - {seed},
                         {p['id'] for p in job['results'] if p['depth'] == 1})
        with get_db() as conn:
            n = conn.execute('SELECT COUNT(*) c FROM papers').fetchone()['c']
        self.assertEqual(n, 2, 'max_nodes 限制任务结果，不限制库写入')


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromModule(sys.modules['__main__'])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
