"""Connection inventories, model selection and quota failover with synthetic keys."""
import os,sys,tempfile,json,unittest
from pathlib import Path
from unittest.mock import patch,Mock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
os.environ.pop('PAPERNET_AI_CONFIG_FILE',None);os.environ.pop('PAPERNET_LLM_API_KEY',None)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,set_setting
init_db()
from app import ai_profiles as ai,ai_brief
class Connections(unittest.TestCase):
 def setUp(self):
  for name in ('ai_config.local.json','data_sources.local.json'):(Path(T.name)/'private'/name).unlink(missing_ok=True)
  set_setting('offline_mode','0')
 def test_domain_name_and_model_inventory(self):
  p=ai.save_profile({'service':'deepseek','name':'','model':'a','models':['a','b'],'api_key':'synthetic-one'})['profile']
  self.assertEqual(p['name'],'api.deepseek.com');self.assertEqual(p['models'],['a','b'])
  ai.select_model(p['id'],{'model':'b'});self.assertEqual(ai_brief.load_ai_config()['request_model'],'b')
 def test_upstream_models_and_no_credential_echo(self):
  p=ai.save_profile({'service':'deepseek','model':'a','api_key':'synthetic-secret'})['profile']
  response=Mock(status_code=200);response.json.return_value={'data':[{'id':'b'},{'id':'a'},{'id':'b'}]}
  with patch('requests.get',return_value=response) as call:r=ai.fetch_models(p['id'])
  self.assertEqual(r['models'],['a','b']);self.assertNotIn('synthetic-secret',json.dumps(r))
  self.assertFalse(call.call_args.kwargs['allow_redirects'])
 def test_model_key_failover(self):
  p=ai.save_profile({'service':'deepseek','model':'a','api_keys':['synthetic-one','synthetic-two']})['profile']
  self.assertEqual(p['key_count'],2);self.assertNotIn('synthetic-two',json.dumps(ai.list_profiles()))
  cfg=ai_brief.load_ai_config();a=Mock(status_code=429);b=Mock(status_code=200);b.json.return_value={'choices':[{'message':{'content':'OK'}}]}
  with patch('requests.post',side_effect=[a,b]) as call:self.assertEqual(ai_brief.call_llm(cfg,'test','test',retry=False)[0],'OK')
  self.assertEqual(call.call_count,2)
 def test_source_multiple_keys_and_deletion_tombstone(self):
  from app import source_profiles as sp
  from app.private_settings import service_candidates,service_key
  a=sp.save_connection({'service':'openalex','name':'A','api_key':'synthetic-source-a'})['connection']
  b=sp.save_connection({'service':'openalex','name':'B','api_key':'synthetic-source-b'})['connection']
  self.assertEqual(len(service_candidates('openalex')),2)
  sp.save_connection({'id':b['id'],'service':'openalex','name':'B','enabled':False})
  self.assertEqual(service_key('openalex'),'synthetic-source-a')
  sp.delete_connection(a['id']);sp.delete_connection(b['id'])
  with patch.dict(os.environ,{'OPENALEX_API_KEY':'synthetic-env'}):self.assertIsNone(service_key('openalex'))
 def test_scholarly_quota_failover(self):
  from app import source_profiles as sp,netclient
  sp.save_connection({'service':'openalex','api_key':'synthetic-source-a'})
  sp.save_connection({'service':'openalex','api_key':'synthetic-source-b'})
  a=Mock(status_code=429);b=Mock(status_code=200,text='{"results":[]}')
  with patch.object(netclient._SESSION,'get',side_effect=[a,b]) as call:
   self.assertEqual(netclient.cached_get_json('https://api.openalex.org/works',{'filter':'cites:W123'},use_cache=False),{'results':[]})
  self.assertEqual(call.call_count,2)
 def test_existing_environment_key_survives_pool_migration(self):
  from app import source_profiles as sp
  from app.private_settings import service_key
  with patch.dict(os.environ,{'OPENALEX_API_KEY':'synthetic-environment-source'}):
   before=sp.list_connections();self.assertNotIn('synthetic-environment-source',json.dumps(before))
   sp.save_connection({'service':'easyscholar','api_key':'synthetic-scholar'})
   self.assertEqual(service_key('openalex'),'synthetic-environment-source')
if __name__=='__main__':unittest.main()
