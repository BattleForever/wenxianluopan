"""Connection lifecycle, deletion tombstones and write-only credentials."""
import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
os.environ.pop('PAPERNET_LLM_API_KEY',None);os.environ.pop('PAPERNET_AI_CONFIG_FILE',None)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,set_setting
init_db()
from app import ai_profiles as p,ai_brief,data_sources

class Profiles(unittest.TestCase):
 def setUp(self):
  (Path(T.name)/'private/ai_config.local.json').unlink(missing_ok=True)
  set_setting('offline_mode','0')
 def create(self,name='Test',service='deepseek',activate=False):
  return p.save_profile({'name':name,'service':service,'model':'test-model','api_key':'synthetic-private-value','activate':activate})['profile']
 def test_create_list_edit_and_switch(self):
  a=self.create('A');b=self.create('B','openai');self.assertTrue(a['active']);self.assertFalse(b['active'])
  p.save_profile({'id':b['id'],'name':'B edited','service':'openai','model':'edited','api_key':''})
  p.activate(b['id']);cfg=ai_brief.load_ai_config();self.assertEqual(cfg['request_model'],'edited');self.assertIn('api.openai.com',cfg['chat_url'])
  text=json.dumps(p.list_profiles());self.assertNotIn('synthetic-private-value',text);self.assertNotIn('"api_key"',text)
 def test_delete_active_selects_next_then_disables(self):
  a=self.create('A');b=self.create('B');p.delete(a['id']);self.assertEqual(p.list_profiles()['active_profile_id'],b['id'])
  p.delete(b['id']);self.assertIsNone(ai_brief.load_ai_config());self.assertEqual(p.list_profiles()['profiles'],[])
 def test_deleted_last_does_not_restore_external_config(self):
  f=Path(T.name)/'external.json';f.write_text(json.dumps({'llm':{'base_url':'https://api.deepseek.com/v1','model':'external','api_key':'synthetic-external-key'}}))
  with patch.dict(os.environ,{'PAPERNET_AI_CONFIG_FILE':str(f)}):
   self.assertEqual(p.list_profiles()['active_profile_id'],'legacy');p.delete('legacy')
   self.assertIsNone(ai_brief.load_ai_config());self.assertEqual(p.list_profiles()['profiles'],[])
 def test_managed_profile_key_is_not_overridden_by_legacy_env(self):
  self.create()
  with patch.dict(os.environ,{'PAPERNET_LLM_API_KEY':'synthetic-env-key'}):self.assertEqual(ai_brief.load_ai_config()['api_key'],'synthetic-private-value')
 def test_individual_test_uses_selected_profile_and_persists(self):
  self.create('A');b=self.create('B','openai')
  with patch.object(ai_brief,'call_llm',return_value=('OK','response')) as call:
   r=p.test_profile(b['id'])
  self.assertTrue(r['ok']);self.assertIn('api.openai.com',call.call_args.args[0]['chat_url']);self.assertFalse(call.call_args.kwargs['retry'])
  self.assertTrue(next(x for x in p.list_profiles()['profiles'] if x['id']==b['id'])['last_test_ok'])
 def test_test_failure_is_sanitized_and_does_not_switch(self):
  a=self.create('A');b=self.create('B','openai')
  with patch.object(ai_brief,'call_llm',side_effect=ai_brief.LLMServiceError('auth_failed','鉴权失败')):
   r=p.test_profile(b['id'])
  self.assertFalse(r['ok']);self.assertEqual(p.list_profiles()['active_profile_id'],a['id']);self.assertNotIn('synthetic-private-value',json.dumps(r))
 def test_offline_individual_test_makes_no_model_call(self):
  a=self.create();set_setting('offline_mode','1')
  with patch.object(ai_brief,'call_llm',side_effect=AssertionError('network')):self.assertEqual(p.test_profile(a['id'])['error']['code'],'offline_mode')
 def test_changed_endpoint_requires_new_key(self):
  a=self.create()
  with self.assertRaises(p.HTTPException):p.save_profile({'id':a['id'],'service':'openai','model':'test','api_key':''})
 def test_unknown_profile_id_is_not_created(self):
  with self.assertRaises(p.HTTPException):p.save_profile({'id':'not-existing','service':'deepseek','model':'test','api_key':'synthetic'})
 def test_data_source_keys_never_echo_and_clear(self):
  value='synthetic-scholar-private-value'
  r=data_sources.save({'service':'easyscholar','api_key':value});self.assertTrue(r['easyscholar']['configured']);self.assertNotIn(value,json.dumps(r))
  r=data_sources.save({'service':'easyscholar','clear':True});self.assertFalse(r['easyscholar']['configured'])
 def test_key_change_invalidates_test(self):
  a=self.create()
  with patch.object(ai_brief,'call_llm',return_value=('OK','test')):p.test_profile(a['id'])
  r=p.save_profile({'id':a['id'],'service':'deepseek','model':'test-model','api_key':'synthetic-replaced-key'})
  self.assertIsNone(r['profile']['last_test_ok'])

if __name__=='__main__':unittest.main()
