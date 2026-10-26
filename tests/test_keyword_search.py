"""A search must narrow the current scope even with recommendation sorting."""
import os,sys,tempfile,unittest,json
from pathlib import Path
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db import init_db,get_db
from app import workspace,exploration
from app.store import upsert_paper
init_db();workspace.init();api=FastAPI();api.include_router(workspace.router);client=TestClient(api)
with get_db() as c:
 ids=[]
 for p in [dict(title='PMUT ultrasonic gas flowmeter',abstract='Measures gas flow in a small pipe.',doi='10.1234/gas.1',authors=['Alice Chen'],venue='Sensors'),dict(title='Optical airflow monitoring',abstract='Air flow and calibration.',title_zh='气体流量监测'),dict(title='Ultrasonic skin imaging',abstract='Medical tissue imaging.',authors=['Bob Stone']),dict(title='Gas absorption spectroscopy',abstract='Concentration sensing without flow measurement.')]:
  pid,_=upsert_paper(c,p,'fixture','library');ids.append(pid)
 # Last record intentionally contains "flow" only in a negative sentence;
 # keyword search is not a relevance or truth classifier.
 c.execute("UPDATE papers SET abstract='Concentration sensing.' WHERE id=?",(ids[3],))
exploration.save({'id':'scope_fixture','status':'completed','options':{'seed_ids':[ids[2]]},'results':[{'id':x} for x in ids[:3]],'created_at':'2026-01-01'})

class KeywordSearch(unittest.TestCase):
 def search(self,query,**kw):
  r=client.post('/api/research/search',json={'query':query,'query_mode':'filter','sort':'recommended','seed_ids':[ids[2]],'all_results':True,**kw});self.assertEqual(r.status_code,200);return r.json()
 def test_unrelated_query_returns_zero_in_recommendation_mode(self):self.assertEqual(self.search('zzzz_unrelated_981746')['total'],0)
 def test_filter_independent_of_sort_and_seed_not_forced_back(self):
  for sort in ('recommended','cited','year_desc','relevance'):
   d=self.search('gas flow',sort=sort,pin_seeds=True)
   self.assertEqual({p['id'] for p in d['papers']},{ids[0],ids[1]})
 def test_chinese_compound_query_can_find_english_metadata(self):
  d=self.search('气流流量');self.assertEqual({p['id'] for p in d['papers']},{ids[0],ids[1]});self.assertTrue(d['search']['expansions'])
 def test_multiple_terms_are_all_required(self):self.assertEqual([p['id'] for p in self.search('PMUT flow')['papers']],[ids[0]])
 def test_air_flow_alias_does_not_match_liquid_flow_alone(self):
  from app.keyword_search import prepare
  matches,_=prepare('air flow');self.assertFalse(matches({'title':'Liquid flowmeter'}));self.assertTrue(matches({'title':'Airflow monitoring'}))
 def test_doi_url_and_case_normalization(self):self.assertEqual([p['id'] for p in self.search('https://doi.org/10.1234/GAS.1')['papers']],[ids[0]])
 def test_author_and_journal_are_searchable(self):
  self.assertEqual([p['id'] for p in self.search('Alice Chen')['papers']],[ids[0]])
  self.assertEqual([p['id'] for p in self.search('Sensors')['papers']],[ids[0]])
 def test_quoted_phrase_is_literal_and_does_not_expand(self):
  d=self.search('"气流流量"');self.assertEqual(d['total'],0);self.assertEqual(d['search']['expansions'],[])
 def test_whitespace_restores_full_scope(self):self.assertEqual(self.search('  ')['total'],4)
 def test_scope_does_not_leak_other_matching_papers(self):
  d=self.search('gas',scope='task',job_id='scope_fixture');self.assertEqual({p['id'] for p in d['papers']},{ids[0],ids[1]})
 def test_explicit_rank_mode_keeps_candidates_with_clear_metadata(self):
  d=self.search('zzzz_unrelated_981746',query_mode='rank');self.assertEqual(d['total'],4);self.assertEqual(d['search']['mode'],'rank');self.assertEqual(d['search']['keyword_matches'],0)

if __name__=='__main__':unittest.main()
