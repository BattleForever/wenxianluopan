import json,os,sys,tempfile,unittest
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
if __name__=='__main__':unittest.main()
