"""Queue admission and per-connection concurrency; isolated synthetic work."""
import os,sys,tempfile,threading,time,unittest,json
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db,set_setting
from app import metadata_jobs as m,ai_profiles as profiles
from app.store import upsert_paper
init_db();m.init()
class QueueTests(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0')
  profiles._target().unlink(missing_ok=True)
  self.releases=[]
 def tearDown(self):
  for e in self.releases:e.set()
  end=time.time()+3
  while m._active and time.time()<end:time.sleep(.02)
  self.assertFalse(m._active)
 def papers(self,n):
  with get_db() as c:return [upsert_paper(c,{'title':self.id()+' '+str(i)},'fixture','library')[0] for i in range(n)]
 def profile(self,limit=1,name='Fixture connection'):
  return profiles.save_profile({'name':name,'base_url':'https://api.openai.com/v1','service':'custom','model':'fixture-model','api_key':'fixture-token','max_concurrency':limit,'activate':True})['profile']
 def test_configured_parallel_limit_two_and_no_secret_in_jobs(self):
  self.profile(2);ids=self.papers(3);release=threading.Event();self.releases.append(release);two=threading.Event();lock=threading.Lock();state={'active':0,'peak':0}
  def complete(pid,opts,cancel,memo):
   with lock:
    state['active']+=1;state['peak']=max(state['peak'],state['active'])
    if state['active']==2:two.set()
   release.wait(3)
   with lock:state['active']-=1
   return {'paper_id':pid,'filled':[],'still_missing':[],'errors':[]}
  with patch.object(m,'complete_one',side_effect=complete):
   jobs=[m.start(m.Start(ids=[pid],allow_network=False,include_metrics=False)) for pid in ids]
   self.assertTrue(two.wait(1));self.assertEqual(m.read(jobs[2]['id'])['status'],'queued');self.assertNotIn('fixture-token',json.dumps(jobs));release.set()
   end=time.time()+4
   while m._active and time.time()<end:time.sleep(.02)
  self.assertEqual(state['peak'],2)
 def test_cancel_queued_job_does_not_process_it(self):
  ids=self.papers(2);release=threading.Event();self.releases.append(release);started=threading.Event();seen=[]
  def complete(pid,*args):
   seen.append(pid);started.set();release.wait(3);return {'paper_id':pid,'filled':[],'still_missing':[],'errors':[]}
  with patch.object(m,'complete_one',side_effect=complete):
   first=m.start(m.Start(ids=[ids[0]],allow_network=False));self.assertTrue(started.wait(1));second=m.start(m.Start(ids=[ids[1]],allow_network=False));m.stop(second['id']);self.assertEqual(m.read(second['id'])['status'],'cancelled');release.set()
   end=time.time()+3
   while m._active and time.time()<end:time.sleep(.02)
  self.assertEqual(seen,[ids[0]])
 def test_same_paper_is_serial_even_when_connection_limit_is_two(self):
  self.profile(2);pid=self.papers(1)[0];release=threading.Event();self.releases.append(release);started=threading.Event()
  def complete(*args):
   started.set();release.wait(3);return {'paper_id':pid,'filled':[],'still_missing':[],'errors':[]}
  with patch.object(m,'complete_one',side_effect=complete):
   first=m.start(m.Start(ids=[pid],allow_network=False));self.assertTrue(started.wait(1));second=m.start(m.Start(ids=[pid],allow_network=False));self.assertEqual(second['status'],'queued');release.set()
   end=time.time()+3
   while m._active and time.time()<end:time.sleep(.02)
 def test_model_selection_is_pinned_across_active_connection_switch(self):
  a=self.profile(1,'First fixture');reference=profiles.active_reference();b=profiles.save_profile({'name':'Second fixture','service':'deepseek','model':'other-fixture','api_key':'second-fixture-token','activate':True})['profile']
  token=m._model_ref.set(reference)
  try:self.assertEqual(m.job_ai_config()['request_model'],'fixture-model')
  finally:m._model_ref.reset(token)
  self.assertNotEqual(a['id'],b['id']);self.assertNotIn('fixture-token',json.dumps(reference))
 def test_model_concurrency_configuration_is_validated(self):
  for n in [0,9,True,'2']:
   with self.assertRaises(Exception):self.profile(n)
  self.assertEqual(self.profile(3)['max_concurrency'],3)
 def test_model_slots_share_a_bound_and_release_on_error(self):
  from app import model_limits,ai_brief
  cfg={'chat_url':'https://api.openai.com/v1/chat/completions','api_key':'fixture-slot-key','request_model':'fixture','max_concurrency':2};release=threading.Event();self.releases.append(release);two=threading.Event();state={'active':0,'peak':0};lock=threading.Lock()
  def generate(*args,**kwargs):
   with lock:
    state['active']+=1;state['peak']=max(state['peak'],state['active'])
    if state['active']==2:two.set()
   release.wait(3)
   with lock:state['active']-=1
   return 'OK','fixture'
  with patch.object(ai_brief,'_call_llm',side_effect=generate):
   threads=[threading.Thread(target=lambda:ai_brief.call_llm(cfg,'fixture','fixture')) for _ in range(4)]
   for t in threads:t.start()
   self.assertTrue(two.wait(1));release.set()
   for t in threads:t.join(3)
  self.assertEqual(state['peak'],2);self.assertFalse(model_limits._counts)
  with patch.object(ai_brief,'_call_llm',side_effect=RuntimeError('fixture')):
   with self.assertRaises(RuntimeError):ai_brief.call_llm(cfg,'fixture','fixture')
  self.assertFalse(model_limits._counts)
 def test_cancel_while_waiting_for_api_slot_does_not_send_request(self):
  from app import ai_brief
  cfg={'chat_url':'https://api.openai.com/v1/chat/completions','api_key':'fixture-cancel-key','request_model':'fixture','max_concurrency':1};release=threading.Event();self.releases.append(release);entered=threading.Event();cancel=threading.Event();errors=[];calls=[]
  def generate(*args,**kwargs):calls.append(1);entered.set();release.wait(3);return 'OK','fixture'
  def waiting():
   try:ai_brief.call_llm({**cfg,'_cancel_event':cancel},'fixture','fixture')
   except ai_brief.LLMServiceError as e:errors.append(e.code)
  with patch.object(ai_brief,'_call_llm',side_effect=generate):
   first=threading.Thread(target=lambda:ai_brief.call_llm(cfg,'fixture','fixture'));first.start();self.assertTrue(entered.wait(1));second=threading.Thread(target=waiting);second.start();cancel.set();second.join(1);release.set();first.join(3)
  self.assertEqual(errors,['cancelled']);self.assertEqual(len(calls),1)
 def test_another_paper_queues_and_runs_after_first(self):
  with get_db() as c:ids=[upsert_paper(c,{'title':'Queue fixture '+str(i)},'fixture','library')[0] for i in range(2)]
  release=threading.Event();entered=threading.Event();seen=[]
  def complete(pid,opts,cancel,memo):
   seen.append(pid);entered.set();release.wait(3);return {'paper_id':pid,'filled':[],'still_missing':[],'errors':[],'outcome':'already_complete'}
  try:
   with patch.object(m,'complete_one',side_effect=complete):
    first=m.start(m.Start(ids=[ids[0]],allow_ai=True,allow_network=False,include_metrics=False));self.assertTrue(entered.wait(1))
    second=m.start(m.Start(ids=[ids[1]],allow_ai=True,allow_network=False,include_metrics=False));self.assertEqual(second['status'],'queued')
    self.assertGreaterEqual(m.read(second['id'])['queue_position'],1);release.set()
    end=time.time()+5
    while time.time()<end and m.read(second['id'])['status']!='completed':time.sleep(.03)
    self.assertEqual(seen,ids);self.assertEqual(m.read(second['id'])['status'],'completed')
  finally:release.set();time.sleep(.1);m._active.clear()
if __name__=='__main__':unittest.main()
