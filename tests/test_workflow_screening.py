"""C 线新增回归：指标年份、合法零值/未知值、阈值筛选与选中导出。

使用隔离合成夹具库（每次运行新建，uuid 命名），不触碰真实数据库与历史 evidence。
运行:  .venv/Scripts/python tests/test_workflow_screening.py
注意: 与其他 test_*.py 一样按单文件运行（各文件在同一进程内会互相覆盖 config.DB_PATH）。
"""
import csv
import io
import json
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config
config.DB_PATH = ROOT / 'evidence' / ('workflow_screening_' + uuid.uuid4().hex[:8] + '.db')

from app.db import get_db, init_db
init_db()
from app.linkview import metric_year_from_extra
from app.store import add_citation, upsert_paper, upsert_venue
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

DOI_ZERO = '10.0000/wf-zero'
DOI_NUM = '10.0000/wf-numeric'
DOI_UNKNOWN = '10.0000/wf-unknown'
DOI_ESCAPE = '10.0000/wf-escape'

with get_db() as conn:
    p_zero, _ = upsert_paper(conn, dict(
        title='Zero impact factor venue paper', doi=DOI_ZERO, year=2020,
        venue='ZeroIF Journal', abstract='zero if venue fixture',
        cited_by_count=7), 'fixture', 'library')
    p_num, _ = upsert_paper(conn, dict(
        title='Numeric string venue paper', doi=DOI_NUM, year=2021,
        venue='NumericIF Journal', abstract='numeric string venue fixture',
        cited_by_count=3), 'fixture', 'library')
    p_unknown, _ = upsert_paper(conn, dict(
        title='Unknown metric venue paper', doi=DOI_UNKNOWN, year=2022,
        venue='Unknown Journal', abstract='unknown venue fixture',
        cited_by_count=5), 'fixture', 'library')
    p_escape, _ = upsert_paper(conn, dict(
        title='=cmd|\' /c calc!A0', doi=DOI_ESCAPE, year=2023,
        venue='Unknown Journal', abstract='formula-like title fixture\nsecond line',
        cited_by_count=1), 'fixture', 'library')
    # 合成指标仅用于测试，不代表真实期刊证据。
    # store.upsert_venue（R3 共享文件）不写 extra_json，这里直接补写以模拟
    # “现有 extra_json 已带可核对年份”的数据形态，不新增 schema。
    upsert_venue(conn, dict(name='ZeroIF Journal', impact_factor='0', cas_zone='4区',
                            source='synthetic_fixture'))
    upsert_venue(conn, dict(name='NumericIF Journal', impact_factor='5.0', cas_zone='3',
                            source='synthetic_fixture'))
    upsert_venue(conn, dict(name='Unknown Journal', impact_factor=None, cas_zone=None,
                            source='synthetic_fixture'))
    conn.execute("UPDATE venues SET extra_json=? WHERE name='ZeroIF Journal'",
                 (json.dumps({'rank': {'year': '2023'}}),))
    conn.execute("UPDATE venues SET extra_json='{}' WHERE name='NumericIF Journal'")
    conn.execute("UPDATE venues SET extra_json='not-json' WHERE name='Unknown Journal'")
    add_citation(conn, p_zero, p_num, 'fixture')


class MetricYearUnit(unittest.TestCase):
    def test_known_year_from_extra_rank(self):
        self.assertEqual(metric_year_from_extra(json.dumps({'rank': {'year': '2023'}})), 2023)

    def test_top_level_year_and_int(self):
        self.assertEqual(metric_year_from_extra(json.dumps({'metric_year': 2019})), 2019)

    def test_if_year_key(self):
        self.assertEqual(metric_year_from_extra(json.dumps({'if_year': '2022'})), 2022)

    def test_invalid_and_missing_stay_unknown(self):
        self.assertIsNone(metric_year_from_extra('not-json'))
        self.assertIsNone(metric_year_from_extra('{}'))
        self.assertIsNone(metric_year_from_extra(json.dumps({'year': '2023-10-20'})))  # 更新时间不是年份
        self.assertIsNone(metric_year_from_extra(json.dumps({'year': '99'})))
        self.assertIsNone(metric_year_from_extra(None))


class ScreeningAndExport(unittest.TestCase):
    def test_zero_sorts_above_unknown_missing_last(self):
        data = client.post('/api/research/search', json={'sort': 'impact_factor', 'limit': 500}).json()
        factors = [(p['venue_info'] or {}).get('impact_factor') for p in data['papers']
                   if p['doi'] in {DOI_ZERO, DOI_NUM, DOI_UNKNOWN}]
        self.assertEqual(factors, ['5.0', '0', None])  # 数值字符串、合法零值、未知依次排列

    def test_zero_if_shown_not_hidden(self):
        data = client.post('/api/research/search', json={'sort': 'impact_factor'}).json()
        row = next(p for p in data['papers'] if p['doi'] == DOI_ZERO)
        self.assertEqual((row['venue_info'] or {}).get('impact_factor'), '0')  # 合法零值不当作未知

    def test_metric_year_exposed_and_unknown_kept(self):
        data = client.post('/api/research/search', json={}).json()
        rows = {p['doi']: p for p in data['papers']}
        self.assertEqual((rows[DOI_ZERO]['venue_info'] or {}).get('metric_year'), 2023)
        self.assertIsNone((rows[DOI_UNKNOWN]['venue_info'] or {}).get('metric_year'))
        self.assertTrue('指标年份未知' in data['metric_note'])

    def test_min_if_zero_keeps_zero_excludes_unknown(self):
        data = client.post('/api/research/search', json={'min_if': 0, 'limit': 500}).json()
        dois = {p['doi'] for p in data['papers']}
        self.assertIn(DOI_ZERO, dois)      # 合法零值满足 IF>=0
        self.assertIn(DOI_NUM, dois)
        self.assertNotIn(DOI_UNKNOWN, dois)  # 阈值筛选明确排除未知值

    def test_cas_max_excludes_unknown_zone(self):
        data = client.post('/api/research/search', json={'cas_max': 4, 'limit': 500}).json()
        dois = {p['doi'] for p in data['papers']}
        self.assertIn(DOI_ZERO, dois)        # '4区' 解析为 4
        self.assertIn(DOI_NUM, dois)         # 纯数字 '3' 解析为 3
        self.assertNotIn(DOI_UNKNOWN, dois)

    def test_csv_contains_metric_year_and_source(self):
        export = client.post('/api/research/export', json={'ids': [p_zero, p_unknown], 'format': 'csv'})
        self.assertEqual(export.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(export.text)))
        self.assertIn('metric_year', rows[0])
        self.assertIn('metric_source', rows[0])
        self.assertIn('metric_updated_at', rows[0])
        by_doi = {r['doi']: r for r in rows}
        self.assertEqual(by_doi[DOI_ZERO]['metric_year'], '2023')
        self.assertEqual(by_doi[DOI_UNKNOWN]['metric_year'], '指标年份未知')
        self.assertEqual(by_doi[DOI_ZERO]['metric_source'], 'synthetic_fixture')

    def test_export_contains_only_selected(self):
        export = client.post('/api/research/export', json={'ids': [p_zero], 'format': 'csv'})
        self.assertNotIn(DOI_NUM.encode(), export.content)
        self.assertNotIn(DOI_UNKNOWN.encode(), export.content)
        self.assertIn(DOI_ZERO.encode(), export.content)

    def test_csv_formula_prefix_and_ris_newline_cleaned(self):
        export = client.post('/api/research/export', json={'ids': [p_escape], 'format': 'csv'})
        self.assertIn(b",'=cmd", export.content)  # 防公式注入前缀
        ris = client.post('/api/research/export', json={'ids': [p_escape], 'format': 'ris'})
        self.assertNotIn(b'\r', ris.content)
        ab = next(line for line in ris.text.splitlines() if line.startswith('AB  - '))
        self.assertNotIn('\n', ab)
        self.assertIn('second line', ab)

    def test_export_rejects_empty_selection(self):
        self.assertEqual(client.post('/api/research/export', json={'ids': [999999], 'format': 'csv'}).status_code, 400)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules['__main__'])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'file': 'test_workflow_screening.py', 'run': result.testsRun,
              'passed': result.testsRun - len(result.failures) - len(result.errors),
              'failures': [(str(t), v) for t, v in result.failures],
              'errors': [(str(t), v) for t, v in result.errors],
              'fixture': config.DB_PATH.name,
              'scope': '合成指标仅用于测试，不代表真实期刊证据。'}
    (ROOT / 'evidence' / 'workflow_screening.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)
