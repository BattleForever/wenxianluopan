"""Real read/import seams, per-journal requests and persistent lookup cooldown."""
import json,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db,set_setting
init_db()
from app import metric_jobs as m,workspace,venues,data_sources,metadata_sources
from app.store import upsert_paper
workspace.init();m.completion.init();m.init()

class MetricBatch(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0');venues._cache={};m._active.clear()
  with get_db() as c:
   for table in ('papers','venues','metric_jobs','metric_lookups','metadata_field_sources'):c.execute('DELETE FROM '+table)
   self.ids=[upsert_paper(c,{'title':'Synthetic paper '+str(i),'venue':'Shared Journal','doi':'10.9999/m'+str(i)},'test','library')[0] for i in range(12)]
 def cache(self,**kw):
  rec={'name':'Shared Journal','rank':{'sciif':'8.1','sciUp':'1区','sci':'Q1'},'at':'2026-10-20',**kw}
  venues._cache={venues.norm_name(rec['name']):rec};return rec
 def value(self):return {'impact_factor':'8.1','cas_zone':'1区','jcr_zone':'Q1','_source':'authorized fixture','_url':None,'evidence':{}}
 def job(self):return {'id':'fixture','status':'queued','total':1,'completed':0,'fields_filled':0,'errors':0,'results':[]}
 def test_search_hydrates_every_paper_from_one_cached_journal_without_network(self):
  self.cache()
  with patch.object(venues,'online_rank',side_effect=AssertionError('read network')),patch.object(m.sources,'nature_metrics',side_effect=AssertionError('read network')):
   result=workspace.search(workspace.SearchIn(limit=100))
  self.assertEqual(result['facets']['if_known'],12);self.assertEqual(result['facets']['cas_known'],12)
  self.assertTrue(all(p['venue_info']['impact_factor']=='8.1' for p in result['papers']))
 def test_import_immediately_applies_to_all_existing_papers(self):
  result=data_sources.import_metrics_cache({'text':json.dumps(self.cache())})
  self.assertEqual(result['fields_filled'],3)
  self.assertFalse(m.completion.missing_metrics({'venue':'Shared Journal'}))
 def test_cached_hydration_uses_shared_read_and_write_connections(self):
  self.cache()
  with patch.object(m,'get_db',wraps=m.get_db) as connections:
   self.assertEqual(m.hydrate_cached(),3)
  self.assertEqual(connections.call_count,2)
 def test_missing_cache_cannot_escalate_to_online_on_read(self):
  venues._cache={venues.norm_name('Shared Journal'):{'name':'Shared Journal','rank':{}}}
  with patch.object(venues,'load_key',return_value='synthetic'),patch.object(venues,'online_rank',side_effect=AssertionError('network')):
   self.assertEqual(m.hydrate_cached(),0)
 def test_partial_cache_does_not_block_explicit_missing_rank_lookup(self):
  self.cache(rank={'sciif':'8.1'})
  rec={'name':'Shared Journal','rank':{'sciif':'8.1','sciUp':'2区','sci':'Q1'}}
  with patch.object(venues,'load_key',return_value='synthetic'),patch.object(venues,'online_rank',return_value=(rec,False)) as lookup:
   result=metadata_sources.authorized_metrics('Shared Journal',refresh=True)
  lookup.assert_called_once();self.assertEqual(result['cas_zone'],'2区')
 def test_batch_requests_journal_once_for_twelve_papers(self):
  j=self.job()
  with patch.object(m.sources,'authorized_metrics',return_value=self.value()) as fetch:
   m.worker(j,m.groups(self.ids),threading.Event())
  self.assertEqual(fetch.call_count,1);self.assertEqual(j['total'],j['completed']);self.assertEqual(j['fields_filled'],3)
 def test_cooldown_persists_and_prevents_repeat_missing_queries(self):
  j=self.job()
  with patch.object(m.sources,'authorized_metrics',return_value=None),patch.object(m.sources,'nature_metrics',return_value=None):m.worker(j,m.groups(),threading.Event())
  with patch.object(m.threading,'Thread',side_effect=AssertionError('repeat worker')):result=m.start(m.Start())
  self.assertEqual(result['total'],0)
 def test_explicit_retry_bypasses_cooldown(self):
  with get_db() as c:c.execute('INSERT INTO metric_lookups VALUES(?,?,?)',(venues.norm_name('Shared Journal'),time.time(),'partial'))
  with patch.object(m.threading,'Thread') as worker:result=m.start(m.Start(retry=True))
  self.assertEqual(result['total'],1);worker.return_value.start.assert_called_once()
 def test_offline_uses_cache_but_never_launches_network_worker(self):
  set_setting('offline_mode','1');self.cache()
  with patch.object(m.threading,'Thread',side_effect=AssertionError('offline worker')):result=m.start(m.Start())
  self.assertEqual(result['fields_filled'],3);self.assertEqual(result['total'],0)
 def test_existing_values_are_preserved(self):
  m.completion.apply_metrics(self.ids[0],{**self.value(),'impact_factor':'3.2'})
  self.cache();m.hydrate_cached()
  with get_db() as c:self.assertEqual(c.execute('SELECT impact_factor FROM venues').fetchone()[0],'3.2')
 def test_active_batch_is_shared(self):
  j=self.job();m.save(j);m._active[j['id']]=threading.Event()
  with patch.object(m.threading,'Thread',side_effect=AssertionError('duplicate worker')):result=m.start(m.Start())
  self.assertEqual(result['id'],j['id'])
 def test_trashed_paper_does_not_initiate_journal_lookup(self):
  with get_db() as c:
   for pid in self.ids:c.execute("INSERT INTO paper_trash(paper_id,scope,deleted_at,snapshot) VALUES(?, 'library', datetime('now'), '{}')",(pid,))
  self.assertEqual(m.groups(),[])
 def test_source_failure_is_terminal_and_cools_down(self):
  j=self.job()
  with patch.object(m.sources,'authorized_metrics',side_effect=RuntimeError('synthetic')):m.worker(j,m.groups(),threading.Event())
  self.assertEqual(j['status'],'completed');self.assertEqual(j['errors'],1);self.assertEqual(j['results'][0]['error'],'source_unavailable')
 def test_cancel_skips_requests(self):
  j=self.job();cancel=threading.Event();cancel.set()
  with patch.object(m.sources,'authorized_metrics',side_effect=AssertionError('cancelled')):m.worker(j,m.groups(),cancel)
  self.assertEqual(j['status'],'cancelled');self.assertEqual(j['completed'],0)
 def test_history_keeps_distinct_metric_years(self):
  records=[self.cache(metric_year=year) for year in (2023,2024)]
  venues.remember_records(records);venues.remember_records(records)
  with get_db() as c:years={r[0] for r in c.execute("SELECT metric_year FROM journal_metric_history WHERE name_norm=?",(venues.norm_name('Shared Journal'),))}
  self.assertTrue({2023,2024}<=years)
 def test_catalog_is_local_and_has_counts(self):
  self.cache();result=data_sources.metrics_catalog()
  self.assertEqual(result['journals'],1);self.assertEqual(result['cas_known'],1);self.assertEqual(result['storage'],'local')

if __name__=='__main__':unittest.main()
