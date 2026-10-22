"""Identity, provenance, offline gates and cancellation using synthetic data."""
import json,os,sys,tempfile,threading,unittest,copy
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db,set_setting
from app.store import upsert_paper
init_db()
from app import metadata_jobs as m,metadata_sources as s,ai_brief,workspace
m.init();workspace.init()
TEXT='A phased array sends two acoustic beams and measures both travel times.'
CFG={'chat_url':'https://api.deepseek.com/v1/chat/completions','request_model':'test','api_key':'synthetic-key'}
class Completion(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0');m._active.clear()
  with get_db() as c:self.pid,_=upsert_paper(c,{'title':self.id(),'doi':'10.9999/'+self.id().split('.')[-1],'abstract':TEXT,'authors':[]},'test','library')
 def opts(self,**kw):return {'allow_network':True,'allow_ai':False,'include_metrics':False,**kw}
 def test_existing_content_not_overwritten_and_empty_authors_filled(self):
  d={'title':'Wrong replacement','abstract':'Not original','authors':['Test Author'],'year':2025,'venue':'Test Journal','_source':'Crossref','_url':'https://api.crossref.org/works/test'}
  fields=m.apply_biblio(self.pid,d);p=m.paper(self.pid)
  self.assertNotIn('title',fields);self.assertEqual(p['abstract'],TEXT);self.assertEqual(json.loads(p['authors']),['Test Author']);self.assertEqual(p['year'],2025)
 def test_wrong_doi_crossref_is_rejected(self):
  with patch.object(s.netclient,'cached_get_json',return_value={'message':{'DOI':'10.9999/wrong','title':['Wrong']}}):
   with self.assertRaises(s.IdentityConflict):s.crossref('10.9999/right')
 def test_wrong_doi_openalex_is_rejected(self):
  with patch.object(s.netclient,'oa_work_by_doi',return_value={'doi':'https://doi.org/10.9999/wrong'}):
   with self.assertRaises(s.IdentityConflict):s.openalex({'doi':'10.9999/right'})
 def test_wrong_openalex_id_without_doi_is_rejected(self):
  with patch.object(s.netclient,'oa_work_by_id',return_value={'id':'https://openalex.org/W999'}):
   with self.assertRaises(s.IdentityConflict):s.openalex({'openalex_id':'https://openalex.org/W123'})
 def test_rank_response_for_other_journal_is_rejected(self):
  with patch.object(s.venues,'local_rank',return_value={'name':'Other journal','rank':{'sciif':'8.8'}}):
   with self.assertRaises(s.IdentityConflict):s.authorized_metrics('Requested journal')
 def test_no_source_text_never_calls_ai(self):
  p=m.paper(self.pid);p.update(title=None,abstract=None)
  with patch.object(m,'paper',return_value=p),patch.object(ai_brief,'call_llm',side_effect=AssertionError('no source')):
   fields,errors=m.ai_fill(self.pid,threading.Event())
  self.assertFalse(fields);self.assertEqual(errors[0]['code'],'missing_source')
 def test_worker_persistence_failure_is_terminal(self):
  j={'id':'save-failure','status':'queued','ids':[self.pid],'results':[],'completed':0,'total':1,'fields_filled':0,'options':self.opts()}
  with patch.object(m,'save',side_effect=[RuntimeError('disk busy'),None]):m.worker(j,threading.Event())
  self.assertEqual(j['status'],'failed');self.assertIn('已补全内容保留',j['stage'])
 def test_offline_zero_external_calls(self):
  set_setting('offline_mode','1')
  with patch.object(s,'crossref',side_effect=AssertionError('network')),patch.object(s,'openalex',side_effect=AssertionError('network')),patch.object(s,'nature_metrics',side_effect=AssertionError('network')),patch.object(ai_brief,'call_llm',side_effect=AssertionError('model')):
   r=m.complete_one(self.pid,self.opts(allow_ai=True),threading.Event(),{})
  self.assertTrue(any(e['code']=='offline_mode' for e in r['errors']))
 def test_metrics_have_year_and_evidence_not_current_date(self):
  m.apply_biblio(self.pid,{'venue':'Metric journal','_source':'test','_url':None})
  d={'impact_factor':'7.5','metric_year':2024,'_source':'Publisher','_url':'https://www.nature.com/test/journal-information','evidence':{'impact_factor':{'quote':'Journal Impact Factor: 7.5 (2024)','year':2024}}}
  m.apply_metrics(self.pid,d)
  found=next(p for p in workspace.load_papers() if p['id']==self.pid)['venue_info']
  self.assertEqual(found['metric_year'],2024);self.assertIsNone(found['cas_zone']);self.assertIn('impact_factor',found['metric_sources'])
 def test_complete_metrics_do_not_repeat_external_lookup(self):
  m.apply_biblio(self.pid,{'venue':'Complete journal','_source':'test','_url':None})
  m.apply_metrics(self.pid,{'impact_factor':'7.5','cas_zone':'1区','jcr_zone':'Q1','_source':'authorized fixture','_url':None})
  with patch.object(s,'authorized_metrics',side_effect=AssertionError('unnecessary call')),patch.object(s,'nature_metrics',side_effect=AssertionError('unnecessary call')):
   result=m.complete_one(self.pid,self.opts(allow_network=False,include_metrics=True),threading.Event(),{})
  self.assertFalse(result['errors'])
 def test_unknown_metric_placeholders_can_be_completed(self):
  m.apply_biblio(self.pid,{'venue':'Placeholder journal','_source':'test','_url':None})
  with get_db() as c:c.execute("UPDATE venues SET impact_factor='未知',cas_zone='N/A' WHERE name='Placeholder journal'")
  fields=m.apply_metrics(self.pid,{'impact_factor':'8.8','cas_zone':'1区','_source':'authorized fixture','_url':None})
  self.assertEqual(set(fields),{'impact_factor','cas_zone'})
 def test_metric_sources_shared_with_other_papers_in_same_journal(self):
  m.apply_biblio(self.pid,{'venue':'Shared journal','_source':'test','_url':None})
  m.apply_metrics(self.pid,{'cas_zone':'1区','_source':'easyScholar 缓存','_url':None,'evidence':{'cas_zone':{'year':None,'queried_at':'2026-09-26'}}})
  with get_db() as c:other,_=upsert_paper(c,{'title':self.id()+' other','venue':'Shared journal'},'test','library')
  source=next(r for r in m.field_sources(other)['sources'] if r['field']=='cas_zone')
  self.assertEqual(source['scope'],'venue');self.assertIsNone(json.loads(source['evidence'])['year']);self.assertIn('2026-09-26',source['evidence'])
 def test_completion_enqueues_only_without_external_write(self):
  d={'authors':['Test'],'year':2025,'venue':'Test','_source':'Crossref','_url':None}
  with patch.object(s,'crossref',return_value=d),patch.object(m.enrich,'_enqueue',return_value=({'queued':True,'state':'pending','queue_id':1},True)) as enqueue:
   r=m.complete_one(self.pid,self.opts(),threading.Event(),{})
  enqueue.assert_called_once();self.assertEqual(r['sync']['state'],'pending')
 def test_ai_fills_only_missing_fields_with_provenance(self):
  p=m.paper(self.pid);response={f:{'text':'测试中文内容','quote':p['title'] if f=='title_zh' else TEXT[:30]} for f in m.AI}
  with patch.object(ai_brief,'load_ai_config',return_value=CFG),patch.object(ai_brief,'call_llm',return_value=(json.dumps(response),'actual-model')):
   fields,errors=m.ai_fill(self.pid,threading.Event())
  self.assertEqual(set(fields),set(m.AI));self.assertFalse(errors)
  with get_db() as c:r=dict(c.execute("SELECT * FROM content_provenance WHERE paper_id=? AND field='abstract_zh'",(self.pid,)).fetchone())
  self.assertEqual(r['input_snapshot'],TEXT);self.assertEqual(r['model'],CFG['request_model']);self.assertEqual(r['prompt_version'],'metadata_fill_v1')
  from app.library_source import artifact_validity
  with get_db() as c,patch.object(ai_brief,'load_ai_config',return_value=CFG):
   self.assertEqual(artifact_validity(c,self.pid,'abstract_zh')['validity'],'verified')
 def test_ai_fabricated_quote_saves_nothing(self):
  response={f:{'text':'测试内容','quote':'invented non-source quotation'} for f in m.AI}
  with patch.object(ai_brief,'load_ai_config',return_value=CFG),patch.object(ai_brief,'call_llm',return_value=(json.dumps(response),'model')):
   with self.assertRaises(ValueError):m.ai_fill(self.pid,threading.Event())
  self.assertIsNone(m.paper(self.pid)['title_zh'])
 def test_one_invalid_ai_field_does_not_discard_verified_other_fields(self):
  p=m.paper(self.pid);response={f:{'text':'测试内容','quote':p['title'] if f=='title_zh' else TEXT[:30]} for f in m.AI}
  response['summary']['quote']='fabricated quote not present'
  with patch.object(ai_brief,'load_ai_config',return_value=CFG),patch.object(ai_brief,'call_llm',return_value=(json.dumps(response),'model')):
   fields,errors=m.ai_fill(self.pid,threading.Event())
  self.assertEqual(set(fields),{'title_zh','abstract_zh'});self.assertEqual(errors[0]['field'],'summary')
  self.assertTrue(m.paper(self.pid)['title_zh'])
 def test_still_missing_accounts_for_requested_metrics_and_ai(self):
  fields=m.requested_missing(self.pid,self.opts(allow_ai=True,include_metrics=True))
  self.assertIn('abstract_zh',fields);self.assertIn('cas_zone',fields)
 def test_long_verbatim_evidence_is_not_rejected_as_fabricated(self):
  text=TEXT*8
  with get_db() as c:c.execute('UPDATE papers SET abstract=? WHERE id=?',(text,self.pid))
  p=m.paper(self.pid);response={f:{'text':'测试内容','quote':p['title'] if f=='title_zh' else text} for f in m.AI}
  with patch.object(ai_brief,'load_ai_config',return_value=CFG),patch.object(ai_brief,'call_llm',return_value=(json.dumps(response),'actual-model')):
   fields,errors=m.ai_fill(self.pid,threading.Event())
  self.assertEqual(set(fields),set(m.AI));self.assertFalse(errors)
 def test_source_change_during_ai_does_not_write_result(self):
  p=m.paper(self.pid);response={f:{'text':'测试内容','quote':p['title'] if f=='title_zh' else TEXT[:30]} for f in m.AI}
  def generate(*a,**kw):
   with get_db() as c:c.execute('UPDATE papers SET abstract=? WHERE id=?',('Changed source',self.pid))
   return json.dumps(response),'model'
  with patch.object(ai_brief,'load_ai_config',return_value=CFG),patch.object(ai_brief,'call_llm',side_effect=generate):
   with self.assertRaises(ValueError):m.ai_fill(self.pid,threading.Event())
  self.assertIsNone(m.paper(self.pid)['title_zh'])
 def test_restart_marks_job_interrupted_without_retry(self):
  j={'id':'old','status':'running','stage':'working','results':[{'paper_id':self.pid,'filled':['year']}],'completed':1,'total':2,'fields_filled':1};m.save(j)
  with patch.object(ai_brief,'call_llm',side_effect=AssertionError('network')):m.init();r=m.read('old')
  self.assertEqual(r['status'],'interrupted');self.assertEqual(r['fields_filled'],1)
 def test_cancellation_stops_before_second_paper(self):
  event=threading.Event();j={'id':'cancel','status':'queued','stage':'','ids':[self.pid,self.pid],'total':2,'completed':0,'fields_filled':0,'results':[],'options':self.opts()}
  def one(*a):event.set();return {'paper_id':self.pid,'filled':['year'],'errors':[]}
  with patch.object(m,'complete_one',side_effect=one) as call:m.worker(j,event)
  self.assertEqual(call.call_count,1);self.assertEqual(m.read('cancel')['status'],'cancelled')
 def test_jcr_and_missing_partition_filters(self):
  m.apply_biblio(self.pid,{'venue':'Partition journal','_source':'test','_url':None});m.apply_metrics(self.pid,{'jcr_zone':'Q1','_source':'test','_url':None})
  r=workspace.search(workspace.SearchIn(zone_scheme='jcr',cas_max=1,query=self.id()))
  self.assertEqual(r['total'],1);self.assertEqual(r['facets']['jcr_known'],1)
  r=workspace.search(workspace.SearchIn(cas_missing=True,query=self.id()));self.assertEqual(r['total'],1)
 def test_publisher_redirect_cannot_escape_fixed_hosts(self):
  class R:status_code=302;headers={'Location':'https://outside.invalid/private'}
  with patch.object(s.requests,'Session') as session:
   session.return_value.get.return_value=R()
   with self.assertRaises(ValueError):s.publisher_page('https://www.nature.com/siteindex',{})
   self.assertEqual(session.return_value.get.call_count,1)
 def test_publisher_metric_not_confused_with_five_year_value(self):
  index=s.TextHTML();index.feed('<a href="/example/">Test Journal</a>');page=s.TextHTML();page.feed('5-year Journal Impact Factor: 9.9 (2024) Journal Impact Factor: 7.5 (2024)')
  with patch.object(s,'publisher_page',side_effect=[index,page]):r=s.nature_metrics('Test Journal',{})
  self.assertEqual(r['impact_factor'],'7.5');self.assertEqual(r['metric_year'],2024)

if __name__=='__main__':unittest.main()
