"""Network policy and generic source recovery; no user credentials or data."""
import os,sys,tempfile,unittest,json,threading
from pathlib import Path
from unittest.mock import patch,MagicMock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import app
from app import network_settings as n,metadata_jobs as m,metadata_sources as sources,publisher_metadata as pub,netclient
from app.db import get_db,set_setting
from app.store import upsert_paper

class Policy(unittest.TestCase):
 def setUp(self):n.FILE.unlink(missing_ok=True)
 def test_default_ignores_environment(self):
  with patch.dict(os.environ,{'HTTPS_PROXY':'http://unwanted.invalid:8888','ALL_PROXY':'socks5://unwanted.invalid:9999'}):
   self.assertEqual(n.proxy_options('https://api.crossref.org')['proxies'],n.DIRECT)
  self.assertFalse(n.prepare_session(__import__('requests').Session()).trust_env)
 def test_custom_credentials_are_encoded_and_never_returned(self):
  with TestClient(app) as c:
   r=c.post('/api/network',json={'mode':'custom','address':'http://127.0.0.1:7890','username':'a@b','password':'fixture-secret !'});self.assertEqual(r.status_code,200)
   self.assertNotIn('fixture-secret',r.text+c.get('/api/network').text)
  self.assertIn('a%40b:fixture-secret%20%21@',n.proxy_options('https://api.crossref.org')['proxies']['https'])
 def test_inline_credentials_removed_from_public_address(self):
  n.save(n.NetworkIn(mode='custom',address='http://user:fixture-private@proxy.invalid:8888'))
  self.assertEqual(n.public()['address'],'http://proxy.invalid:8888');self.assertNotIn('fixture-private',json.dumps(n.public()))
 def test_loopback_always_direct(self):
  policy={'mode':'custom','address':'http://proxy.invalid:8888'}
  for url in ['http://127.0.0.1:23119/api','http://localhost:8367/v1','http://[::1]:8367/v1']:
   self.assertEqual(n.proxy_options(url,policy)['proxies'],n.DIRECT)
 def test_modes_can_switch_without_restart_or_losing_password(self):
  n.save(n.NetworkIn(mode='custom',address='http://proxy.invalid:8888',username='user',password='fixture-secret'))
  n.save(n.NetworkIn(mode='direct'));self.assertEqual(n.proxy_options('https://api.crossref.org')['proxies'],n.DIRECT)
  n.save(n.NetworkIn(mode='custom'));self.assertIn('user:fixture-secret@',n.proxy_options('https://api.crossref.org')['proxies']['https'])
  n.save(n.NetworkIn(mode='custom',clear_password=True));self.assertFalse(n.public()['password_saved'])
 def test_invalid_targets_do_not_save(self):
  for value in ['file:///tmp/test','http://proxy.invalid','http://proxy.invalid:0','http://proxy.invalid:99999','http://proxy.invalid:80/path','http://proxy.invalid:80/?token=fixture-secret']:
   with self.assertRaises(HTTPException) as e:n.save(n.NetworkIn(mode='custom',address=value))
   self.assertNotIn('fixture-secret',str(e.exception.detail))
  self.assertEqual(n.public()['mode'],'direct')
 def test_system_uses_current_system_config(self):
  with patch.object(n.urllib.request,'getproxies',return_value={'https':'http://system.invalid:8888'}),patch.object(n.urllib.request,'proxy_bypass',return_value=False):
   self.assertEqual(n.proxy_options('https://api.crossref.org',{'mode':'system'})['proxies']['https'],'http://system.invalid:8888')
 def test_browser_direct_and_custom_no_secret_arguments(self):
  self.assertEqual(n.browser_flags('https://www.sciencedirect.com/'),['--no-proxy-server'])
  n.save(n.NetworkIn(mode='custom',address='socks5h://proxy.invalid:8888'))
  self.assertIn('--proxy-server=socks5://proxy.invalid:8888',n.browser_flags('https://www.sciencedirect.com/'))
  n.save(n.NetworkIn(mode='custom',password='fixture-secret',username='user'))
  with self.assertRaises(ValueError) as e:n.browser_flags('https://www.sciencedirect.com/')
  self.assertNotIn('fixture-secret',str(e.exception))
 def test_test_request_does_not_save_and_redacts_errors(self):
  import requests
  set_setting('offline_mode','0')
  with patch.object(n.requests.Session,'get',side_effect=requests.exceptions.ProxyError('fixture-secret')):
   r=n.test(n.NetworkIn(mode='custom',address='http://proxy.invalid:8888',password='fixture-secret'))
  self.assertFalse(r['ok']);self.assertNotIn('fixture-secret',json.dumps(r));self.assertEqual(n.public()['mode'],'direct')
 def test_offline_never_tests_external(self):
  set_setting('offline_mode','1')
  with patch.object(n.requests,'Session',side_effect=AssertionError('external request')):self.assertFalse(n.test(n.NetworkIn())['ok'])
  set_setting('offline_mode','0')
 def test_shared_client_receives_route_options(self):
  n.save(n.NetworkIn(mode='custom',address='http://proxy.invalid:8888'))
  with patch.object(netclient._SESSION,'get',return_value=MagicMock(status_code=200)) as call:netclient._get_with_retry('https://api.crossref.org/works')
  self.assertEqual(call.call_args.kwargs['proxies']['https'],'http://proxy.invalid:8888')

class SourceRecovery(unittest.TestCase):
 def setUp(self):
  n.FILE.unlink(missing_ok=True);set_setting('offline_mode','0')
 def test_crossref_publisher_url_is_fixed_allowlisted_and_cache_has_expiry(self):
  raw={'message':{'DOI':'10.5555/example','title':['Paper'],'resource':{'primary':{'URL':'https://link.springer.com/article/10.5555/example'}}}}
  with patch.object(sources.netclient,'cached_get_json',return_value=raw) as get:d=sources.crossref('10.5555/example')
  self.assertEqual(d['meta']['publisher_url'],'https://link.springer.com/article/10.5555/example');self.assertEqual(get.call_args.kwargs['max_age'],86400)
  raw['message']['resource']['primary']['URL']='http://127.0.0.1/private'
  with patch.object(sources.netclient,'cached_get_json',return_value=raw):self.assertIsNone(sources.crossref('10.5555/example')['meta']['publisher_url'])
 def test_many_unseen_papers_missing_apis_recover_public_abstract(self):
  for i in range(3):
   title='Unseen controlled research '+str(i);doi='10.5555/unseen'+str(i);text='A public source describing a controlled experiment, its method and measured results. '*4
   with get_db() as c:pid=upsert_paper(c,{'title':title,'doi':doi,'year':2025,'authors':['Author'],'venue':'Example','meta':{'publisher_url':'https://www.sciencedirect.com/science/article/abs/pii/FIXTURE'+str(i)}},'fixture','library')[0]
   html='<meta name="citation_doi" content="'+doi+'"><meta name="citation_title" content="'+title+'"><meta name="citation_abstract" content="'+text+'">'
   response=MagicMock(status_code=403);response.__enter__.return_value=response
   with patch.object(sources,'zotero_biblio',return_value=None),patch.object(sources,'zotero_pdf_paths',return_value=[]),patch.object(sources,'crossref',return_value=None),patch.object(sources,'openalex',return_value=None),patch.object(pub.requests,'get',return_value=response),patch.object(pub,'render_public_page',return_value=html):
    result=m.complete_one(pid,{'allow_network':True,'allow_ai':False,'include_metrics':False},threading.Event(),{})
   self.assertIn('abstract',result['filled']);self.assertEqual(m.paper(pid)['abstract'],text.strip())
   with get_db() as c:self.assertEqual(c.execute("SELECT source FROM metadata_field_sources WHERE paper_id=? AND field='abstract'",(pid,)).fetchone()[0],'出版商公开摘要')
 def test_missing_abstract_recovers_from_acquired_pdf_and_rejects_other_paper(self):
  import fitz
  from app import pdf_resolver
  title='A controlled original for automatic abstract recovery';doi='10.5555/pdf-recovery'
  text='A controlled flow experiment compares two measurement channels and reports stable accuracy. '*4
  file=Path(T.name)/'recovery.pdf';doc=fitz.open();page=doc.new_page();page.insert_textbox(fitz.Rect(30,30,550,750),title+'\n'+doi+'\nAbstract: '+text+'\n1 Introduction\nBody text.',fontsize=11);doc.save(file);doc.close()
  with get_db() as c:pid=upsert_paper(c,{'title':title,'doi':doi,'year':2025,'authors':['Author'],'venue':'Example'},'fixture','library')[0]
  def acquire(pid):
   with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(file),pid))
   return {'ready':True}
  with patch.object(sources,'zotero_biblio',return_value=None),patch.object(sources,'zotero_pdf_paths',return_value=[]),patch.object(sources,'crossref',return_value=None),patch.object(sources,'openalex',return_value=None),patch.object(pub,'lookup',return_value=None),patch.object(pdf_resolver,'acquire',side_effect=acquire) as get:
   r=m.complete_one(pid,{'allow_network':True,'allow_ai':False,'include_metrics':False,'recover_abstract_from_pdf':True},threading.Event(),{})
  get.assert_called_once_with(pid);self.assertIn('abstract',r['filled']);self.assertIn('stable accuracy',m.paper(pid)['abstract'])
  self.assertIsNone(m.local_pdf_abstract({'local_pdf_path':str(file),'title':'Another entirely different experiment','doi':'10.5555/wrong'}))
 def test_completed_fields_do_not_retry_earlier_empty_source_errors(self):
  payload={'id':'recovered','status':'completed','ids':[7],'results':[{'paper_id':7,'still_missing':[],'outcome':'filled','errors':[{'code':'abstract_not_provided','source':'Earlier API'}]}]}
  with get_db() as c:c.execute('INSERT OR REPLACE INTO enrichment_jobs VALUES(?,?)',('recovered',json.dumps(payload)))
  self.assertEqual(m.read('recovered')['retry_ids'],[])
  self.assertTrue(m.Start(ids=[7]).recover_abstract_from_pdf)
 def test_quote_whitespace_variants_keep_original_literal_span(self):
  for space in ['\u00a0','\u202f','\t','\n','  ']:
   source='Measured range 20'+space+'m/s at 95.8'+space+'% accuracy.'
   q=m.exact_source_quote(source,'Measured range 20 m/s at 95.8 % accuracy.')
   self.assertEqual(q,source);self.assertIsNone(m.exact_source_quote(source,'Measured range 21 m/s at 95.8 % accuracy.'))
class Transport(unittest.TestCase):
 def test_real_direct_custom_system_transports_and_loopback(self):
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  import requests
  hits=[]
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):
    hits.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'fixture')
   def log_message(self,*args):pass
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  address='http://127.0.0.1:'+str(server.server_port)
  try:
   with n.prepare_session(requests.Session()) as session:
    with patch.dict(os.environ,{'HTTP_PROXY':'http://127.0.0.1:1','ALL_PROXY':'http://127.0.0.1:1','NO_PROXY':''}):
     self.assertEqual(session.get(address+'/direct',**n.proxy_options(address,{'mode':'direct'}),timeout=2).status_code,200)
    policy={'mode':'custom','address':address}
    session.get('http://fixture.invalid/article',**n.proxy_options('http://fixture.invalid/article',policy),timeout=2)
    session.get(address+'/local',**n.proxy_options(address,policy),timeout=2)
    with patch.object(n.urllib.request,'getproxies',return_value={'http':address}),patch.object(n.urllib.request,'proxy_bypass',return_value=False):
     session.get('http://fixture.invalid/system',**n.proxy_options('http://fixture.invalid/system',{'mode':'system'}),timeout=2)
   self.assertEqual(hits,['/direct','http://fixture.invalid/article','/local','http://fixture.invalid/system'])
  finally:server.shutdown();server.server_close();thread.join()
if __name__=='__main__':unittest.main()
