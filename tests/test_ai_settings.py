import json,os,sys,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db
init_db()
from app import ai_settings as cfg
class ConfigTest(unittest.TestCase):
 def test_save_and_get_never_echo_key(self):
  value='unit-test-credential-not-a-real-key'
  out=cfg.save_config({'service':'deepseek','model':'unit-test-model','api_key':value})
  self.assertTrue(out['configured']);self.assertNotIn(value,json.dumps(out));self.assertNotIn(value,json.dumps(cfg.get_config()))
  file=Path(T.name)/'private/ai_config.local.json'
  self.assertEqual(json.loads(file.read_text())['llm']['api_key'],value)
 def test_blank_preserves_saved_key(self):
  cfg.save_config({'service':'deepseek','model':'unit-test-model','api_key':'unit-test-private'})
  self.assertTrue(cfg.save_config({'service':'deepseek','model':'unit-test-model','api_key':''})['saved'])
 def test_private_and_http_endpoints_rejected(self):
  for url in ['http://127.0.0.1:9000','https://127.0.0.1','https://not-approved.invalid/v1']:
   with self.assertRaises(cfg.HTTPException):cfg.save_config({'service':'custom','base_url':url,'model':'x','api_key':'never-transmitted'})
 def test_malformed_port_and_credential_urls_rejected(self):
  for url in ['https://api.deepseek.com:bad/v1','https://user:pass@api.deepseek.com/v1','https://api.deepseek.com/v1?api_key=secret','https://api.deepseek.com/v1#secret','https://api.deepseek.com/v1\nother']:
   with self.assertRaises(cfg.HTTPException) as e:cfg.save_config({'service':'custom','base_url':url,'model':'x','api_key':'fake-credential'})
   self.assertEqual(e.exception.status_code,400)
 def test_control_characters_and_invalid_types_rejected(self):
  for field,value in [('api_key','fake\nheader'),('api_key',['bad']),('model','bad\nmodel'),('service',[])]:
   data={'service':'deepseek','model':'x','api_key':'fake-credential'};data[field]=value
   with self.assertRaises(cfg.HTTPException):cfg.save_config(data)
 def test_config_loader_rejects_invalid_file_without_logging_contents(self):
  target=Path(T.name)/'private/ai_config.local.json'
  target.parent.mkdir(exist_ok=True)
  for value in [{'llm':{'base_url':[],'model':'m','api_key':'fake'}},{'llm':{'base_url':'https://api.deepseek.com:bad','model':'m','api_key':'fake'}},{'llm':{'base_url':'https://api.deepseek.com','model':'m','api_key':'fake\nheader'}}]:
   target.write_text(json.dumps(value),encoding='utf-8');self.assertIsNone(cfg.ai_brief.load_ai_config())
 def test_connection_test_is_explicit_small_and_sanitized(self):
  from app.db import set_setting
  cfg.save_config({'service':'deepseek','model':'test-model','api_key':'fake-credential','enable_ai':True})
  with patch.object(cfg.ai_brief,'call_llm',return_value=('OK','test-model')) as call:
   out=cfg.test_config();self.assertTrue(out['ok']);self.assertNotIn('fake-credential',json.dumps(out))
   self.assertEqual(call.call_args.kwargs['max_tokens'],256)
  set_setting('offline_mode','1')
  with patch.object(cfg.ai_brief,'call_llm',side_effect=AssertionError('network')):
   self.assertEqual(cfg.test_config()['error']['code'],'offline_mode')
 def test_provider_switch_requires_new_key(self):
  cfg.save_config({'service':'deepseek','model':'test','api_key':'fake-credential'})
  with self.assertRaises(cfg.HTTPException):cfg.save_config({'service':'openai','model':'test','api_key':''})
if __name__=='__main__':unittest.main()
