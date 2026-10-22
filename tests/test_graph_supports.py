"""并行冲刺 A 线新增测试：P1 发现依据（supports）行为与跨进程持久化。

规则来源：parallel_v01/00_并行规则与接口.md A→C 节。
- 最小行为样例 S→A、S→B、A→C、B→C 在 depth=2 references 下 C 只出现
  一次但有两条不同父节点依据；重复响应不再累加；循环不无限扩展；
  每篇最多 50 条并明确截断；重启后依据不丢。
全部使用合成数据与隔离 fixture；重启持久化通过真实子进程验证。
运行:  <python> tests/test_graph_supports.py
"""
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config
config.DB_PATH = ROOT / 'evidence' / ('graph_sup_fixture_' + uuid.uuid4().hex[:8] + '.db')

from app import db
from app.db import get_db, init_db
from app import exploration
from app.discovery_supports import SupportLedger
from app.store import upsert_paper, add_citation

init_db()

TITLES = {1: 'Support seed S', 2: 'Ref A', 3: 'Ref B', 4: 'Ref C', 5: 'Unrelated'}
EDGES = [(1, 2), (1, 3), (2, 4), (3, 4)]  # S→A, S→B, A→C, B→C


class SupportsUnitChecks(unittest.TestCase):
    """SupportLedger 纯单元样例。"""

    def test_dedup_same_support_not_accumulated(self):
        led = SupportLedger()
        self.assertTrue(led.observe(4, seed=1, parent=2, relation='references',
                                    depth=2, edge_source='openalex',
                                    edge_from=2, edge_to=4))
        for _ in range(5):
            self.assertFalse(led.observe(4, seed=1, parent=2,
                                         relation='references', depth=2,
                                         edge_source='openalex',
                                         edge_from=2, edge_to=4))
        results = [{'id': 4, 'depth': 2, 'parent': 2, 'seed': 1,
                    'relation': 'references', 'edge_source': 'openalex'}]
        led.attach(results)
        self.assertEqual(results[0]['support_count'], 1)
        self.assertEqual(len(results[0]['supports']), 1)

    def test_cap_50_and_truncated_flag(self):
        led = SupportLedger()
        for i in range(60):
            led.observe(9, seed=1, parent=100 + i, relation='references',
                        depth=1)
        results = [{'id': 9}]
        led.attach(results)
        r = results[0]
        self.assertEqual(r['support_count'], 50)
        self.assertEqual(len(r['supports']), 50)
        self.assertTrue(r['support_truncated'])
        # 上限之后重复已知依据不改变计数；新依据也不存储，仅维持截断标记
        led.observe(9, seed=1, parent=100, relation='references', depth=1)
        led.observe(9, seed=1, parent=999, relation='references', depth=1)
        results = [{'id': 9}]
        led.attach(results)
        self.assertEqual(results[0]['support_count'], 50)
        self.assertTrue(results[0]['support_truncated'])

    def test_negative_cap_rejected(self):
        with self.assertRaises(ValueError):
            SupportLedger(cap=-1)


class SupportsWalkChecks(unittest.TestCase):
    """每个用例独立 fixture。"""

    def setUp(self):
        self.db_path = ROOT / 'evidence' / ('graph_sup_fixture_' + uuid.uuid4().hex[:8] + '.db')
        db.DB_PATH = self.db_path
        init_db()
        with get_db() as conn:
            conn.execute("UPDATE settings SET value='1' WHERE key='offline_mode'")
            self.ids = {}
            for n, t in TITLES.items():
                self.ids[n] = upsert_paper(conn, dict(title=t, year=2020,
                                                      cited_by_count=5),
                                           'graph_sup_fixture', 'library')[0]
            for a, b in EDGES:
                add_citation(conn, self.ids[a], self.ids[b], 'openalex')

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            try:
                os.remove(str(self.db_path) + suffix)
            except OSError:
                pass

    def test_minimal_sample_two_parents_one_result(self):
        """S→A、S→B、A→C、B→C，references、depth=2：C 只出现一次但有
        两条不同父节点依据；首条路径保持原含义（parent=首次发现的 A）。"""
        job = exploration.walk([self.ids[1]], direction='references', depth=2)
        by_id = {p['id']: p for p in job['results']}
        self.assertEqual(len(job['results']), 4)
        c = by_id[self.ids[4]]
        self.assertEqual(c['parent'], self.ids[2])  # 首次发现路径（A）
        self.assertEqual(c['depth'], 2)
        self.assertEqual(c['relation'], 'references')
        self.assertEqual(c['edge_source'], 'openalex')
        self.assertEqual(c['support_count'], 2)
        parents = {s['parent'] for s in c['supports']}
        self.assertEqual(parents, {self.ids[2], self.ids[3]})
        for s in c['supports']:
            self.assertEqual(s['id'], self.ids[4])
            self.assertEqual(s['relation'], 'references')
            self.assertEqual(s['seed'], self.ids[1])
            self.assertEqual(s['edge_from'], s['parent'])
            self.assertEqual(s['edge_to'], self.ids[4])
        # A、B 各自只有一条依据（来自 S）
        for n in (2, 3):
            node = by_id[self.ids[n]]
            self.assertEqual(node['support_count'], 1)
            self.assertEqual(node['supports'][0]['parent'], self.ids[1])
        self.assertFalse(c['support_truncated'])

    def test_repeated_response_does_not_accumulate(self):
        """重复探索同一图：每个任务独立账本，同键依据不跨任务/重复累加。"""
        first = exploration.walk([self.ids[1]], direction='references', depth=2)
        second = exploration.walk([self.ids[1]], direction='references', depth=2)
        c1 = next(p for p in first['results'] if p['id'] == self.ids[4])
        c2 = next(p for p in second['results'] if p['id'] == self.ids[4])
        self.assertEqual(c1['support_count'], 2)
        self.assertEqual(c2['support_count'], 2)

    def test_both_direction_keeps_distinct_relations(self):
        """补充边 4→2 后，both 遍历（种子 2，depth=1）中节点 4 被 references
        与 citing 两个方向各观察到一次：同一节点的两类依据是不同键，不互相
        覆盖，且数据库引用方向不被反转。"""
        with get_db() as conn:
            add_citation(conn, self.ids[4], self.ids[2], 'openalex')  # 4→2
        job = exploration.walk([self.ids[2]], direction='both', depth=1)
        four = next(p for p in job['results'] if p['id'] == self.ids[4])
        relations = {s['relation'] for s in four['supports']}
        self.assertEqual(relations, {'references', 'citing'})
        self.assertEqual(four['support_count'], 2)
        # references 依据 = 2 引用 4（edge_from=2, edge_to=4）；
        # citing 依据 = 4 引用 2（edge_from=4, edge_to=2）。方向不反转。
        ref = next(s for s in four['supports'] if s['relation'] == 'references')
        cit = next(s for s in four['supports'] if s['relation'] == 'citing')
        self.assertEqual((ref['edge_from'], ref['edge_to']),
                         (self.ids[2], self.ids[4]))
        self.assertEqual((cit['edge_from'], cit['edge_to']),
                         (self.ids[4], self.ids[2]))
        self.assertEqual(ref['parent'], self.ids[2])
        self.assertEqual(cit['parent'], self.ids[2])

    def test_multi_seed_records_observed_attribution(self):
        """多种子：每个节点的依据按实际观察到的归属记录（不声称穷尽）。
        节点 4 首次由种子 2 经边 2→4 发现；节点 3（由种子 1 发现）展开时
        又观察到 3→4，因此 4 另有一条 seed=1 的实际观察依据。"""
        job = exploration.walk([self.ids[1], self.ids[2]],
                               direction='references', depth=2)
        c = next(p for p in job['results'] if p['id'] == self.ids[4])
        self.assertEqual(c['seed'], self.ids[2])  # 首次发现归属
        self.assertEqual(c['parent'], self.ids[2])
        seeds = {s['seed'] for s in c['supports']}
        self.assertEqual(seeds, {self.ids[1], self.ids[2]})
        parents = {s['parent'] for s in c['supports']}
        self.assertEqual(parents, {self.ids[2], self.ids[3]})
        self.assertEqual(c['support_count'], 2)

    def test_persistence_across_process_restart(self):
        """服务重启后先 GET 旧任务：结果、路径与依据从 research_jobs 持久化恢复。"""
        db_path = ROOT / 'evidence' / ('graph_sup_restart_' + uuid.uuid4().hex[:8] + '.db')
        # 子进程以 worker 模式运行本文件：独立进程内建库、建任务并等任务完成
        proc = subprocess.run(
            [sys.executable, __file__, '--worker', str(db_path)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0,
                         f'worker 失败: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}')
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(info['status'], 'completed')
        # “重启”后（本进程内只切库、不经内存状态）先 GET 旧任务
        db.DB_PATH = db_path
        try:
            old = exploration.get(info['id'])
            self.assertIsNotNone(old)
            self.assertEqual(old['status'], 'completed')
            c = next(p for p in old['results'] if p['id'] and
                     p['parent'] is not None and p['depth'] == 2)
            self.assertEqual(c['support_count'], 2)
            self.assertEqual(len(c['supports']), 2)
            parents = {s['parent'] for s in c['supports']}
            self.assertEqual(len(parents), 2)
            # 路径字段完整保留
            for p in old['results']:
                for k in ('depth', 'parent', 'seed', 'relation', 'edge_source'):
                    self.assertIn(k, p)
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(str(db_path) + suffix)
                except OSError:
                    pass


def _run_worker(db_path_str: str) -> None:
    """子进程入口：独立进程内建库、建离线任务并等完成，输出 JSON 供父进程断言。"""
    import time
    from app.db import get_db
    from app.store import upsert_paper, add_citation
    db.DB_PATH = db_path_str
    init_db()
    exploration.init()  # research_jobs 表由 workspace.init/exploration.init 创建
    with get_db() as conn:
        conn.execute("UPDATE settings SET value='1' WHERE key='offline_mode'")
        ids = {}
        titles = {1: 'Support seed S', 2: 'Ref A', 3: 'Ref B', 4: 'Ref C'}
        edges = [(1, 2), (1, 3), (2, 4), (3, 4)]
        for n, t in titles.items():
            ids[n] = upsert_paper(conn, dict(title=t, year=2020, cited_by_count=5),
                                  'graph_sup_fixture', 'library')[0]
        for a, b in edges:
            add_citation(conn, ids[a], ids[b], 'openalex')
    job = exploration.create({'seed_ids': [ids[1]], 'direction': 'references',
                              'depth': 2, 'per_direction': 5, 'max_nodes': 30,
                              'online': False, 'max_fetch': 2})
    status = 'queued'
    for _ in range(200):
        j = exploration.get(job['id'])
        status = j['status']
        if status not in ('queued', 'running'):
            break
        time.sleep(0.02)
    print(json.dumps({'id': job['id'], 'status': status}))


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--worker':
        _run_worker(sys.argv[2])
        sys.exit(0)
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromModule(sys.modules['__main__'])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
