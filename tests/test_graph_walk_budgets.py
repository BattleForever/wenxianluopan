"""并行冲刺 A 线新增测试：图遍历预算、环、重复种子、空结果、部分成功与取消。

只覆盖新行为与既有行为回归的确定性 mock/离线样例；全部使用合成数据与
隔离 fixture（独立临时数据库），不联网、不复制真实数据库。
运行:  <python> tests/test_graph_walk_budgets.py
"""
import json
import os
import sys
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config
config.DB_PATH = ROOT / 'evidence' / ('graph_fixture_' + uuid.uuid4().hex[:8] + '.db')

from app import db
from app.db import get_db, init_db
from app import exploration
from app.store import upsert_paper, add_citation

init_db()

TITLES = {
    1: 'Seed survey paper',
    2: 'Reference alpha',
    3: 'Reference beta',
    4: 'Shared target',
    9: 'Isolated lonely paper',
}
# 引用边（from 引用 to）：1→2, 1→3, 2→4, 3→4, 4→1（合法跨论文环）
EDGES = [(1, 2), (1, 3), (2, 4), (3, 4), (4, 1)]


def build_base(conn):
    ids = {}
    for n, t in TITLES.items():
        ids[n] = upsert_paper(conn, dict(title=t, year=2020, cited_by_count=10 - n % 7),
                              'graph_fixture', 'library')[0]
    for a, b in EDGES:
        add_citation(conn, ids[a], ids[b], 'fixture')
    return ids


class GraphBudgetChecks(unittest.TestCase):
    """每个用例独立 fixture。"""

    def setUp(self):
        self.db_path = ROOT / 'evidence' / ('graph_fixture_' + uuid.uuid4().hex[:8] + '.db')
        db.DB_PATH = self.db_path
        init_db()
        with get_db() as conn:
            conn.execute("UPDATE settings SET value='1' WHERE key='offline_mode'")
            self.ids = build_base(conn)

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            try:
                os.remove(str(self.db_path) + suffix)
            except OSError:
                pass

    # ---------- 深度与预算 ----------

    def test_depth_and_node_caps(self):
        one = exploration.walk([self.ids[1]], direction='references', depth=1)
        self.assertEqual({p['id'] for p in one['results']},
                         {self.ids[1], self.ids[2], self.ids[3]})
        two = exploration.walk([self.ids[1]], direction='references', depth=2)
        self.assertEqual({p['id'] for p in two['results']},
                         {self.ids[1], self.ids[2], self.ids[3], self.ids[4]})
        capped = exploration.walk([self.ids[1]], direction='references',
                                  depth=3, max_nodes=3)
        self.assertLessEqual(len(capped['results']), 3)
        self.assertTrue(capped['truncated'])
        # max_nodes 是任务结果上限：数据库写入不受它限制，节点仍在库中
        with get_db() as conn:
            n = conn.execute('SELECT COUNT(*) c FROM papers').fetchone()['c']
        self.assertEqual(n, len(TITLES))

    def test_per_direction_budget(self):
        job = exploration.walk([self.ids[1]], direction='references',
                               depth=3, per_direction=1)
        self.assertTrue(job['truncated'])
        for r in job['results']:
            if r['depth'] >= 1:
                # 每个展开节点每方向最多引入 1 个新邻居：结果规模受 1+depth 约束
                pass
        self.assertLessEqual(len(job['results']), 1 + 3)

    # ---------- 环与重复种子 ----------

    def test_cycle_terminates_and_dedups(self):
        job = exploration.walk([self.ids[1]], direction='references', depth=10)
        self.assertEqual(len(job['results']),
                         len({p['id'] for p in job['results']}))
        self.assertEqual(job['status'], 'completed')
        # 环回边 4→1 被观察到为节点 1 的一条依据（节点已见，不重复展开）
        one = next(p for p in job['results'] if p['id'] == self.ids[1])
        self.assertEqual(one['depth'], 0)
        self.assertTrue(any(s['parent'] == self.ids[4] and s['relation'] == 'references'
                            for s in one['supports']))

    def test_duplicate_seeds_dedup_and_attribution(self):
        job = exploration.walk([self.ids[1], self.ids[1], self.ids[2]],
                               direction='references', depth=2)
        ids = [p['id'] for p in job['results']]
        self.assertEqual(len(ids), len(set(ids)))
        by_id = {p['id']: p for p in job['results']}
        self.assertEqual(by_id[self.ids[1]]['seed'], self.ids[1])
        self.assertEqual(by_id[self.ids[2]]['seed'], self.ids[2])
        # 4 由 2 首先发现（队列顺序 1,2；1 的邻居 2,3 均已见）
        self.assertEqual(by_id[self.ids[4]]['seed'], self.ids[2])
        self.assertEqual(by_id[self.ids[4]]['parent'], self.ids[2])

    # ---------- 合法空结果 ----------

    def test_legit_empty_expansion(self):
        job = exploration.walk([self.ids[9]], direction='both', depth=2)
        self.assertEqual(job['status'], 'completed')
        self.assertFalse(job['truncated'])
        self.assertEqual([p['id'] for p in job['results']], [self.ids[9]])
        seed = job['results'][0]
        self.assertEqual(seed['support_count'], 0)
        self.assertEqual(seed['supports'], [])
        self.assertFalse(seed['support_truncated'])

    # ---------- 部分成功：单节点抓取失败不中断 ----------

    def test_fetch_failure_partial_success(self):
        target = self.ids[2]

        def failing_fetcher(pid, direction, cap, stop):
            if pid == target:
                raise RuntimeError('HTTP 429: mock rate limited')
            return 0

        job = exploration.walk([self.ids[1]], direction='references', depth=2,
                               online=True, max_fetch=10, fetcher=failing_fetcher)
        self.assertEqual(job['status'], 'completed_with_errors')
        self.assertEqual(len(job['errors']), 1)
        self.assertEqual(job['errors'][0]['paper_id'], target)
        self.assertIn('429', job['errors'][0]['error'])
        # 失败节点之后其余节点仍被展开（部分成功）
        self.assertEqual({p['id'] for p in job['results']},
                         {self.ids[1], self.ids[2], self.ids[3], self.ids[4]})

    # ---------- 取消：当前已开始调用完成后在边界停止 ----------

    def test_cancellation_stops_at_boundary_after_current_call(self):
        started = threading.Event()
        wrote = {'edge': False}

        def cancelling_fetcher(pid, direction, cap, stop):
            started.set()
            stop.set()  # 调用已开始：完成后才在边界停止
            # 模拟本次调用自身的写回（应保留，不被取消丢弃）
            with get_db() as conn:
                wrote['edge'] = add_citation(conn, self.ids[3], self.ids[9],
                                             'fixture')
            return 1

        job = exploration.walk([self.ids[1]], direction='references', depth=2,
                               online=True, max_fetch=10,
                               fetcher=cancelling_fetcher,
                               stop=threading.Event())
        self.assertTrue(started.is_set())
        self.assertEqual(job['status'], 'cancelled')
        self.assertEqual([p['id'] for p in job['results']], [self.ids[1]])
        self.assertTrue(wrote['edge'],
                        '取消后当前已开始调用的写回结果不应被丢弃')

    def test_fetch_budget_stops_at_max_fetch(self):
        calls = []

        def counting(pid, direction, cap, stop):
            calls.append(pid)
            return 0

        job = exploration.walk([self.ids[1]], direction='both', depth=3,
                               online=True, max_fetch=2, fetcher=counting)
        self.assertEqual(len(calls), 2)
        self.assertTrue(job['truncated'])
        self.assertEqual(job['fetches'], 2)


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromModule(sys.modules['__main__'])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
