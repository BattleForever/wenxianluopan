"""Draft connections, explicit loopback models and unified task summaries."""
import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,set_setting,get_db
init_db()
from app import ai_profiles as p,ai_brief,workspace,metadata_jobs,metric_jobs,pdf_guide
workspace.init();metadata_jobs.init();metric_jobs.init();pdf_guide.init()

class Connections(unittest.TestCase):
 def setUp(self):
  (Path(T.name)/'private/ai_config.local.json').unlink(missing_ok=True);set_setting('offline_mode','0')
 def test_local_openai_compatible_endpoint_is_valid(self):
  self.assertTrue(ai_brief.valid_model_base('http://127.0.0.1:8367/v1'))
  self.assertFalse(ai_brief.valid_model_base('http://192.168.1.2:8367/v1'))
  self.assertFalse(ai_brief.valid_model_base('http://169.254.169.254/v1'))
  self.assertFalse(ai_brief.valid_model_base('http://127.0.0.2:8367/v1'))
  self.assertFalse(ai_brief.valid_model_base('http://127.0.0.1:8765/api'))
 def test_save_draft_then_fetch_then_select(self):
  a=p.save_profile({'service':'custom','base_url':'http://127.0.0.1:8367/v1','api_key':'synthetic-local-model'})['profile']
  self.assertEqual(a['models'],[]);self.assertIsNone(ai_brief.load_ai_config())
  response=Mock(status_code=200);response.json.return_value={'data':[{'id':'test-local'}]}
  with patch('requests.get',return_value=response):r=p.fetch_models(a['id'])
  self.assertEqual(r['models'],['test-local'])
  p.select_model(a['id'],{'model':'test-local'});p.activate(a['id'])
  self.assertEqual(ai_brief.load_ai_config()['request_model'],'test-local')
  self.assertNotIn('synthetic-local-model',json.dumps(p.list_profiles()))
 def test_preview_does_not_need_saved_model(self):
  response=Mock(status_code=200);response.json.return_value={'data':[{'id':'model-a'}]}
  with patch('requests.get',return_value=response):
   self.assertEqual(p.preview_models({'base_url':'http://127.0.0.1:8367/v1','api_key':'synthetic-local'})['models'],['model-a'])
 def test_local_calls_do_not_follow_redirect(self):
  with patch('requests.get',return_value=Mock(status_code=302)) as call:
   with self.assertRaises(p.HTTPException):p.preview_models({'base_url':'http://127.0.0.1:8367/v1','api_key':'synthetic-local'})
   self.assertFalse(call.call_args.kwargs['allow_redirects'])
 def test_public_private_dns_still_blocked(self):
  with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('192.168.1.2',443))]):
   with self.assertRaises(ai_brief.LLMServiceError):ai_brief.check_model_destination('https://models.example.com/v1')

class Tasks(unittest.TestCase):
 def test_unified_tasks_are_recent_first_and_summary_only(self):
  from app import task_center
  with get_db() as c:
   c.execute('DELETE FROM research_jobs');c.execute('DELETE FROM enrichment_jobs');c.execute('DELETE FROM metric_jobs')
   c.execute('INSERT INTO research_jobs VALUES(?,?)',('research-a',json.dumps({'id':'research-a','status':'running','created_at':'2026-10-24T12:00:00+00:00','results':[{'id':1}],'processed':1,'fetches':1,'options':{'depth':2},'errors':[]})))
   c.execute('INSERT INTO enrichment_jobs VALUES(?,?)',('enrich-b',json.dumps({'id':'enrich-b','status':'completed','created_at':'2026-10-24T12:01:00+00:00','completed':1,'total':1,'fields_filled':2,'results':[{'do_not_echo':'synthetic-secret'}]})))
  r=task_center.recent_tasks()
  self.assertEqual(r['tasks'][0]['kind'],'enrichment');self.assertEqual(r['active_count'],1)
  self.assertNotIn('synthetic-secret',json.dumps(r));self.assertEqual(r['tasks'][1]['found'],1)
  self.assertTrue(task_center.recent_tasks(limit=1)['has_more'])

class Focus(unittest.TestCase):
 def test_relevant_frontier_gets_network_budget_first(self):
  from app import exploration
  with get_db() as c:
   c.execute('DELETE FROM citations');c.execute('DELETE FROM papers')
   for pid,title,count in [(1,'PMUT ultrasonic gas flowmeter',1),(2,'Global machine learning survey',10000),(3,'PMUT ultrasonic gas flowmeter arrays',1)]:
    c.execute('INSERT INTO papers(id,title,cited_by_count) VALUES(?,?,?)',(pid,title,count))
   c.execute("INSERT INTO citations(from_id,to_id,src) VALUES(1,2,'synthetic')")
   c.execute("INSERT INTO citations(from_id,to_id,src) VALUES(1,3,'synthetic')")
  fetched=[]
  def fetch(pid,*args):fetched.append(pid);return 0
  j=exploration.walk([1],direction='references',depth=2,per_direction=None,max_nodes=300,online=True,max_fetch=2,complete_seed=True,focused=True,fetcher=fetch)
  self.assertEqual(fetched,[1,3]);self.assertEqual({p['id'] for p in j['results']},{1,2,3})
 def test_result_budget_stops_unneeded_followup_requests(self):
  from app import exploration
  with get_db() as c:
   c.execute('DELETE FROM citations');c.execute('DELETE FROM papers')
   for i in range(1,7):c.execute('INSERT INTO papers(id,title) VALUES(?,?)',(i,'PMUT ultrasonic flow '+str(i)))
   for i in range(2,7):c.execute("INSERT INTO citations(from_id,to_id,src) VALUES(1,?,'synthetic')",(i,))
  fetched=[]
  j=exploration.walk([1],depth=2,max_nodes=3,max_fetch=20,online=True,complete_seed=True,focused=True,fetcher=lambda pid,*a:fetched.append(pid) or 0)
  self.assertEqual(fetched,[1]);self.assertEqual(len(j['results']),6);self.assertTrue(j['truncated'])

if __name__=='__main__':unittest.main()
