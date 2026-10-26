import os,sys,tempfile,unittest,json,time
from pathlib import Path
from unittest.mock import patch,MagicMock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name;sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.main import app
from app import metric_catalog as catalog,venues,data_sources,connection_status as checks
from app.db import set_setting

class Catalog(unittest.TestCase):
 def setUp(self):venues._cache={'journal':{'name':'Test Journal','rank':{'sciif':'4.2','sciUp':'2区','sci':'Q1','private_key':'DO_NOT_EXPORT'},'metric_year':2024,'at':'2026-10-26','api_key':'DO_NOT_EXPORT'}}
 def test_exports_are_portable_no_credentials_and_do_not_replace_raw_cache(self):
  path=catalog.snapshot('jsonl');text=path.read_text(encoding='utf-8');self.assertNotIn('DO_NOT_EXPORT',text);self.assertEqual(json.loads(text)['metric_year'],2024);self.assertNotEqual(path,venues.CACHE_PATH)
  table=catalog.snapshot('csv');normalized=catalog.import_text(table.read_text(encoding='utf-8'));row=json.loads(normalized);self.assertEqual(row['rank']['sciif'],'4.2');self.assertEqual(row['metric_year'],2024)
 def test_import_csv_and_json_array_use_existing_validation(self):
  result=data_sources.import_metrics_cache({'text':'name,impact_factor,cas_zone,jcr_zone,metric_year\nNew Journal,6.1,2区,Q1,2025\nInvalid Journal,=run(),9区,Q9,2025'})
  self.assertEqual(result['imported'],1);self.assertEqual(result['rejected'],1)
  self.assertEqual(data_sources.import_metrics_cache({'text':json.dumps([{'name':'Array Journal','impact_factor':'3.5'}])})['imported'],1)
 def test_search_and_export_never_use_network(self):
  with patch('requests.Session',side_effect=AssertionError('network')):self.assertEqual(catalog.items(q='TEST',offset=0,limit=50)['total'],1)
 def test_native_open_requires_workspace_token(self):
  with TestClient(app) as client:
   self.assertEqual(client.post('/api/data-sources/metrics-catalog/open-file').status_code,403)
   token=client.get('/api/workspace/state').json()['token']
   with patch.object(catalog.os,'startfile',create=True) as opened:
    response=client.post('/api/data-sources/metrics-catalog/open-file',headers={'X-Workspace-Token':token});self.assertEqual(response.status_code,200);self.assertTrue(Path(opened.call_args.args[0]).is_file())
 def test_csv_cells_cannot_be_formulas(self):
  venues._cache={'x':{'name':'=HYPERLINK("x")','rank':{'sciif':'2'}}};text=catalog.snapshot('csv').read_text(encoding='utf-8');self.assertIn("'=HYPERLINK",text)

class Connections(unittest.TestCase):
 def setUp(self):set_setting('offline_mode','0');checks.FILE.unlink(missing_ok=True)
 def test_publisher_home_reachability_does_not_claim_entitlement(self):
  with patch.object(checks,'fetch_probe',return_value={'code':200,'body':b'<html>Welcome</html>','url':'https://www.nature.com/'}):result=checks.publisher_check('nature')
  self.assertEqual(result['state'],'success');self.assertEqual(result['access'],'not_checked');self.assertIn('权限未验证',result['message'])
 def test_pdf_link_is_scoped_to_that_link(self):
  with patch.object(checks,'fetch_probe',return_value={'code':206,'body':b'%PDF-1.7 fixture','url':'https://www.nature.com/x.pdf'}):result=checks.publisher_check('nature','https://www.nature.com/x.pdf')
  self.assertEqual(result['access'],'pdf_readable');self.assertEqual(result['scope'],'selected_link');self.assertIn('仅验证当前链接',result['message'])
 def test_blocked_and_empty_waf_response_are_not_green(self):
  for status,body in [(403,b'blocked'),(418,b'denied'),(202,b''),(200,b'')]:
   with patch.object(checks,'fetch_probe',return_value={'code':status,'body':body,'url':'https://ieeexplore.ieee.org/'}):self.assertEqual(checks.publisher_check('ieee')['state'],'warning')
 def test_untrusted_urls_and_signed_login_links_are_rejected(self):
  for url in ['https://127.0.0.1/x','http://www.nature.com/x','https://www.nature.com/x?token=private','https://www.nature.com@evil.example/x']:
   with self.assertRaises(HTTPException):checks.publisher_check('nature',url)
 def test_expired_results_do_not_become_fresh_when_another_target_is_checked(self):
  from app.private_settings import atomic_json
  from datetime import datetime,timezone
  atomic_json(checks.FILE,{'fingerprint':'same','time':time.time(),'results':{'ai':{'state':'success','checked_at':datetime.fromtimestamp(time.time()-1800,timezone.utc).isoformat()},'network':{'state':'success','checked_at':datetime.now(timezone.utc).isoformat()}}})
  with patch.object(checks,'fingerprint',return_value='same'):result=checks.saved_results()
  self.assertNotIn('ai',result);self.assertIn('network',result)
 def test_offline_checks_do_not_send_model_or_publisher_requests(self):
  set_setting('offline_mode','1')
  with patch.object(checks,'fetch_probe',side_effect=AssertionError('network')),patch.object(checks.ai_profiles,'test_profile',side_effect=AssertionError('model')):
   self.assertEqual(checks.check_target('ai')['state'],'paused');self.assertEqual(checks.check_target('nature')['state'],'paused')
 def test_jobs_require_token_and_return_only_bounded_status(self):
  with TestClient(app) as client:
   self.assertEqual(client.post('/api/connections/check',json={'targets':['network']}).status_code,403)
   token=client.get('/api/workspace/state').json()['token']
   with patch.object(checks,'check_target',return_value={'state':'success','message':'fixture'}):
    r=client.post('/api/connections/check',json={'targets':['network']},headers={'X-Workspace-Token':token});self.assertEqual(r.status_code,200);jid=r.json()['id']
    for _ in range(30):
     data=client.get('/api/connections/checks/'+jid).json()
     if data['status']=='completed':break
     time.sleep(.02)
   self.assertEqual(data['status'],'completed');self.assertNotIn('fingerprint',data);self.assertEqual(data['completed'],1)
if __name__=='__main__':unittest.main(verbosity=2)
