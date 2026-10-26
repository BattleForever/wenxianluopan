"""Retrieval retention, evidence ranking and counterexamples to citation cutoffs."""
import os,sys,tempfile,unittest,threading,time
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db
from app import exploration,recommendation
init_db();exploration.init()

def row(pid,title,year=2020,abstract='',cited=0):return {'id':pid,'title':title,'year':year,'abstract':abstract,'cited_by_count':cited}

class Ranking(unittest.TestCase):
 def fixture(self):
  papers=[row(1,'PMUT ultrasonic gas flowmeter'),row(2,'Optical microfluidic sensing'),
   row(3,'Phase delay inference',2001),row(4,'Ultrasonic piezoelectric flow sensing',2026,cited=0),
   row(5,'Generic machine learning overview',2010,cited=1000000),row(6,'Optical channel flow sensor'),
   row(7,'Unknown source',2024),row(100,'Propagation calibration'),row(101,'Transit time')] 
  edges=[{'from_id':a,'to_id':b} for a,b in [(1,100),(1,101),(3,100),(3,101),(4,1),(2,6),(7,2)]]
  return papers,edges
 def test_low_lexical_overlap_shared_refs_is_explained(self):
  papers,edges=self.fixture();r,m=recommendation.rank(papers,edges,[1,2],year=2026)
  self.assertEqual(r[3]['evidence']['shared_reference_count'],2)
  self.assertLess(r[3]['rank'],r[5]['rank']);self.assertTrue(any('共享' in t for t in r[3]['reasons']))
 def test_recent_low_cited_and_weak_seed_remain_visible(self):
  papers,edges=self.fixture();r,_=recommendation.rank(papers,edges,[1,2],year=2026)
  self.assertIn('recent',r[4]['roles']);self.assertIn(2,r[6]['evidence']['seed_coverage_ids'])
  self.assertEqual(set(r),{p['id'] for p in papers})
 def test_global_citations_and_journal_if_do_not_promote(self):
  papers,edges=self.fixture();a,_=recommendation.rank(papers,edges,[1,2],year=2026)
  papers[4]['cited_by_count']=9999999999;papers[4]['venue_info']={'impact_factor':999}
  b,_=recommendation.rank(papers,edges,[1,2],year=2026)
  self.assertEqual({k:v['rank'] for k,v in a.items()},{k:v['rank'] for k,v in b.items()})
 def test_large_popular_branch_does_not_bury_a_second_seed(self):
  papers=[row(1,'PMUT gas flow'),row(2,'Optical microfluidic sensing'),row(1000,'Photon channel calibration')]
  papers += [row(i,'PMUT gas flow related '+str(i),cited=100000) for i in range(3,103)]
  edges=[{'from_id':1,'to_id':i} for i in range(3,103)]+[{'from_id':2,'to_id':1000}]
  r,_=recommendation.rank(papers,edges,[1,2],year=2026)
  self.assertLessEqual(r[1000]['rank'],10)
 def test_duplicate_edges_and_input_order_cannot_inflate(self):
  papers,edges=self.fixture();a,_=recommendation.rank(papers,edges,[1,2],year=2026)
  b,_=recommendation.rank(list(reversed(papers)),list(reversed(edges+edges)),[2,1],year=2026)
  self.assertEqual(a,b)
 def test_missing_abstract_stays_in_review_not_deleted(self):
  papers,edges=self.fixture();r,_=recommendation.rank(papers,edges,[1],year=2026)
  self.assertIn('review',r[7]['roles']);self.assertIn('abstract',r[7]['missing'])
 def test_bridge_has_concrete_graph_witnesses(self):
  papers=[row(i,'Fixture graph '+str(i)) for i in range(1,13)]
  links=[(a,b) for group in [range(2,7),range(7,12)] for a in group for b in group if a<b]+[(1,2),(1,7),(12,3),(12,8)]
  r,_=recommendation.rank(papers,[{'from_id':a,'to_id':b} for a,b in links],[1],year=2026)
  self.assertIn('bridge',r[12]['roles']);self.assertGreaterEqual(len(r[12]['evidence']['bridge_witness_ids']),2)

class Retention(unittest.TestCase):
 def setUp(self):
  with get_db() as c:
   c.execute('DELETE FROM citations');c.execute('DELETE FROM papers')
   for i in range(1,422):c.execute('INSERT INTO papers(id,title,year) VALUES(?,?,2025)',(i,'PMUT ultrasonic '+str(i)))
   c.execute("INSERT INTO citations(from_id,to_id,src) VALUES(1,2,'fixture')")
   for i in range(3,422):c.execute("INSERT INTO citations(from_id,to_id,src) VALUES(2,?,'fixture')",(i,))
 def test_observed_candidates_are_not_cut_at_300(self):
  j=exploration.walk([1],depth=2,per_direction=None,max_nodes=10,online=False,complete_seed=True,strategy='balanced')
  self.assertEqual(len(j['results']),421);self.assertEqual(j['retention'],'all_observed')
 def test_pending_frontier_can_be_resumed_without_requery(self):
  calls=[]
  def fetch(pid,*a):calls.append(pid);return 0
  a=exploration.walk([1],depth=2,per_direction=None,max_nodes=10,max_fetch=1,online=True,complete_seed=True,strategy='balanced',fetcher=fetch)
  self.assertEqual(calls,[1]);self.assertIn(2,a['pending_fetch_ids']);self.assertEqual(a['status'],'paused')
  b=exploration.walk([1],depth=2,per_direction=None,max_nodes=10,max_fetch=2,online=True,complete_seed=True,strategy='balanced',fetched_ids=a['fetched_ids'],fetcher=fetch)
  self.assertEqual(calls,[1,2]);self.assertEqual(b['pending_fetch_ids'],[]);self.assertEqual(len(b['results']),421)
 def test_cancel_retains_relations_observed_during_last_fetch(self):
  event=threading.Event()
  def fetch(pid,*a):event.set();return 0
  j=exploration.walk([1],depth=2,max_fetch=1,online=True,strategy='balanced',stop=event,fetcher=fetch)
  self.assertEqual(j['status'],'cancelled');self.assertIn(2,[r['id'] for r in j['results']])
 def test_api_resume_keeps_identity_candidates_and_cumulative_counts(self):
  from fastapi import FastAPI
  from fastapi.testclient import TestClient
  from app import workspace
  api=FastAPI();api.include_router(workspace.router);client=TestClient(api);calls=[]
  ready=threading.Event();permit=threading.Event()
  def fetch(pid,*a):
   calls.append(pid)
   if pid==2:ready.set();permit.wait(10)
   return 2
  def wait_done(jid):
   until=time.monotonic()+15
   while time.monotonic()<until:
    j=exploration.get(jid)
    with exploration._guard:active=jid in exploration._cancel
    if not active:return exploration.get(jid)
    time.sleep(.02)
   self.fail('Worker did not finish')
  with patch.object(exploration,'is_online',return_value=True),patch.object(exploration,'bounded_fetch',side_effect=fetch):
   response=client.post('/api/research/explore',json={'seed_ids':[1],'depth':2,'strategy':'balanced','max_fetch':1})
   self.assertEqual(response.status_code,200);jid=response.json()['id'];a=wait_done(jid)
   self.assertEqual(a['new_records'],2);self.assertEqual(a['status'],'paused')
   response=client.post('/api/research/jobs/'+jid+'/continue',json={'additional_fetch':1})
   try:
    self.assertEqual(response.status_code,200);self.assertTrue(ready.wait(10))
    live=exploration.get(jid)
    self.assertEqual({r['id'] for r in a['results']},{r['id'] for r in live['results']})
   finally:permit.set()
   b=wait_done(jid)
  self.assertEqual(calls,[1,2]);self.assertEqual(b['new_records'],4);self.assertEqual(b['id'],jid)
  self.assertEqual({r['id'] for r in a['results']},{r['id'] for r in b['results']});self.assertEqual(b['pending_fetch_ids'],[])
 def test_resume_save_failure_releases_worker_guard(self):
  j={'id':'save_failure','options':{'strategy':'balanced'},'pending_fetch_ids':[2],'fetched_ids':[1]}
  with patch.object(exploration,'get',return_value=j),patch.object(exploration,'is_online',return_value=True),patch.object(exploration,'save',side_effect=OSError('fixture')):
   with self.assertRaises(RuntimeError):exploration.resume('save_failure',1)
  self.assertNotIn('save_failure',exploration._cancel)
 def test_source_failure_retains_candidates_and_separate_error_boundary(self):
  def fail(*a):raise ValueError('Fixture source unavailable')
  j=exploration.walk([1],depth=2,max_fetch=2,online=True,strategy='balanced',fetcher=fail)
  self.assertEqual(len(j['results']),421);self.assertEqual(j['failed_fetch_ids'],[1,2])
  self.assertEqual(j['status'],'completed_with_errors');self.assertTrue(j['coverage']['not_global_completeness'])

class Search(unittest.TestCase):
 def test_recommended_search_and_lenses_keep_complete_pool(self):
  from app import workspace
  workspace.init()
  with get_db() as c:
   c.execute('DELETE FROM citations');c.execute('DELETE FROM papers')
   for pid,title,year,cited in [(1,'PMUT ultrasonic flowmeter',2024,10),(2,'Time propagation calibration',2001,0),(3,'PMUT gas sensing',2026,0),(4,'Unrelated generic review',2015,999999)]:
    c.execute("INSERT INTO papers(id,title,year,cited_by_count,status) VALUES(?,?,?,?,'library')",(pid,title,year,cited))
   c.execute("INSERT INTO citations(from_id,to_id,src) VALUES(1,2,'fixture')")
   c.execute("INSERT INTO citations(from_id,to_id,src) VALUES(3,1,'fixture')")
  full=workspace.search(workspace.SearchIn(seed_ids=[1],sort='recommended',all_results=True))
  self.assertEqual(full['candidate_pool_total'],4);self.assertEqual(full['shown'],4)
  newer=workspace.search(workspace.SearchIn(seed_ids=[1],sort='recommended',recommendation_group='recent',all_results=True))
  self.assertIn(3,[p['id'] for p in newer['papers']]);self.assertEqual(newer['candidate_pool_total'],4)
  self.assertTrue(all('recommendation' in p for p in full['papers']))

if __name__=='__main__':unittest.main()
