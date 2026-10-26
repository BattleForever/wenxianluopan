"""PDF destination policies, browser callbacks and native-file preservation."""
import os,sys,tempfile,unittest,json,hashlib
from pathlib import Path
from unittest.mock import patch,MagicMock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.main import app
from app import pdf_storage as storage,pdf_access as pdf,browser_capture as capture,zotero_link,zotero_pdf
from app.browser_capture_worker import matches_pdf,remote_url
from app.db import get_db
from app.store import upsert_paper
import fitz

def document(title,doi):
 with fitz.open() as d:
  page=d.new_page();page.insert_textbox(fitz.Rect(30,30,540,700),title+'\n'+doi+'\nAbstract\nA controlled experiment measures flow through two paths. '*3,fontsize=10)
  return d.tobytes()
class Storage(unittest.TestCase):
 def setUp(self):
  storage.FILE.unlink(missing_ok=True)
  self.title='Verified original for PDF destination testing';self.doi='10.5555/'+self._testMethodName
  with get_db() as c:self.pid=upsert_paper(c,{'title':self.title,'doi':self.doi},'fixture','library')[0]
  self.raw=document(self.title,self.doi);pdf.save_pdf(self.pid,self.raw)
 def test_custom_copy_is_readable_reused_and_does_not_move_source(self):
  original=Path(pdf.paper(self.pid)['local_pdf_path']);target=Path(T.name)/'chosen'
  with patch.object(storage,'policy',return_value={'mode':'custom','directory':str(target)}):
   first=storage.finalize(self.pid);second=storage.finalize(self.pid)
  self.assertTrue(first['ready']);self.assertEqual(first['saved_directory'],str(target.resolve()));self.assertEqual(len(list(target.glob('*.pdf'))),1);self.assertEqual(original.read_bytes(),self.raw);self.assertEqual(first['filename'],second['filename'])
 def test_ask_without_selection_never_starts_download(self):
  with patch.object(storage,'policy',return_value={'mode':'ask'}),patch.object(pdf,'download',side_effect=AssertionError('network')):
   with self.assertRaises(HTTPException):pdf.download_request(self.pid)
 def test_native_binding_uses_confirmed_zotero_file_and_preserves_staging_copy(self):
  original=Path(pdf.paper(self.pid)['local_pdf_path']);folder=Path(T.name)/'Zotero'/'storage'/'PDFKEY';folder.mkdir(parents=True);native=folder/'native.pdf';native.write_bytes(self.raw)
  def sync(body):
   self.assertTrue(body.include_pdf);storage.bind_zotero(self.pid,native);zotero_pdf.record(self.pid,'saved','checked','PDFKEY',hashlib.sha256(self.raw).hexdigest());return {'pdf':{'state':'saved'}}
  with patch.object(zotero_link,'client',return_value=MagicMock(api_key='fixture')),patch.object(zotero_link,'sync',side_effect=sync):r=storage.finalize(self.pid)
  self.assertEqual(r['saved_to'],'zotero');self.assertEqual(r['saved_directory'],str(folder.resolve()));self.assertEqual(original.read_bytes(),self.raw);self.assertEqual(native.read_bytes(),self.raw)
 def test_new_pdf_version_is_saved_without_replacing_existing_native_original(self):
  folder=Path(T.name)/'native_versions';folder.mkdir(exist_ok=True);old=folder/'old.pdf';old.write_bytes(self.raw);new=folder/'new.pdf'
  updated=document(self.title,self.doi+' updated version');pdf.save_pdf(self.pid,updated)
  parent='PARENT';cli=MagicMock();cli.get_children.return_value=[{'key':'OLDPDF','data':{'itemType':'attachment','parentItem':parent,'contentType':'application/pdf','linkMode':'imported_file','filename':'old.pdf'}}]
  cli.attachment_file.side_effect=lambda key:old if key=='OLDPDF' else new
  cli.create_items.return_value={'success':{'0':'NEWPDF'}};cli.get_item.return_value={'key':'NEWPDF','data':{'itemType':'attachment','parentItem':parent,'contentType':'application/pdf','linkMode':'imported_file'}}
  cli.upload_pdf_attachment.side_effect=lambda key,raw,*args:new.write_bytes(raw)
  r=zotero_pdf.sync_pdf(self.pid,parent,cli)
  self.assertEqual(r['state'],'saved');self.assertEqual(old.read_bytes(),self.raw);self.assertEqual(new.read_bytes(),updated);self.assertEqual(Path(pdf.paper(self.pid)['local_pdf_path']),new.resolve())
 def test_zotero_unavailable_is_not_reported_as_saved_to_zotero(self):
  with patch.object(zotero_link,'client',return_value=MagicMock(api_key=None)):
   result=storage.finalize(self.pid)
  self.assertTrue(result['ready']);self.assertEqual(result['saved_to'],'cache');self.assertIn('暂存',result['message'])
 def test_storage_configuration_requires_local_token_and_valid_path(self):
  with TestClient(app) as c:
   self.assertEqual(c.post('/api/pdf-storage',json={'mode':'custom','directory':str(Path(T.name)/'target')}).status_code,403)
   headers={'X-Workspace-Token':c.get('/api/workspace/state').json()['token']}
   self.assertEqual(c.post('/api/pdf-storage',headers=headers,json={'mode':'custom','directory':'relative/path'}).status_code,422)
   self.assertEqual(c.post('/api/pdf-storage',headers=headers,json={'mode':'ask'}).status_code,200);self.assertEqual(c.get('/api/pdf-storage').json()['mode'],'ask')
 def test_open_folder_uses_saved_file_parent_not_arbitrary_client_path(self):
  with TestClient(app) as c,patch.object(storage.os,'startfile',create=True) as open_folder:
   headers={'X-Workspace-Token':c.get('/api/workspace/state').json()['token']};r=c.post('/api/pdf-storage/open-folder/'+str(self.pid),headers=headers)
  self.assertEqual(r.status_code,200);open_folder.assert_called_once_with(str(Path(pdf.paper(self.pid)['local_pdf_path']).parent.resolve()))
 def test_browser_pdf_identity_rejects_unrelated_and_non_pdf_bytes(self):
  self.assertTrue(matches_pdf(self.raw,self.title,self.doi));self.assertFalse(matches_pdf(self.raw,'Unrelated completely different paper','10.5555/unrelated'));self.assertFalse(matches_pdf(b'<html>denied</html>',self.title,self.doi))
 def test_zotero_identifier_lookup_searches_doi_fields_not_just_title(self):
  from app.zotero_client import ZoteroClient
  client=ZoteroClient();calls=[]
  def request(method,path,*args,**kwargs):
   calls.append(path)
   return 200,{},[{'key':'EXACTDOI','data':{'itemType':'journalArticle','title':'An alternate original Zotero title','DOI':self.doi}}] if 'qmode=everything' in path else []
  with patch.object(client,'_request',side_effect=request):result=client.find_by_identifier(doi=self.doi,title=self.title)
  self.assertEqual(result['key'],'EXACTDOI');self.assertIn('qmode=everything',calls[0])
 def test_browser_targets_reject_loopback_and_non_https(self):
  self.assertFalse(remote_url('http://example.org/a.pdf'));self.assertFalse(remote_url('https://127.0.0.1/a.pdf'));self.assertFalse(remote_url('https://user:pass@example.org/a.pdf'))
 def test_capture_secret_never_leaves_public_job_and_queue_cancels(self):
  with TestClient(app) as c,patch.object(capture,'dispatch'):
   headers={'X-Workspace-Token':c.get('/api/workspace/state').json()['token']}
   j=c.post('/api/pdf-capture',json={'paper_id':self.pid},headers=headers).json();self.assertEqual(j['status'],'queued')
   secret=capture._jobs[j['id']]['_token'];self.assertNotIn(secret,json.dumps(j));self.assertNotIn('_token',json.dumps(c.get('/api/pdf-capture/'+j['id']).json()))
   r=c.post('/api/pdf-capture/'+j['id']+'/cancel',headers=headers);self.assertEqual(r.json()['status'],'cancelled');self.assertNotIn(j['id'],capture._queue)
 def test_authenticated_capture_callback_saves_only_matching_pdf(self):
  target=Path(T.name)/'browser_output'
  with TestClient(app) as c,patch.object(capture,'dispatch'),patch.object(storage,'policy',return_value={'mode':'custom','directory':str(target)}):
   token={'X-Workspace-Token':c.get('/api/workspace/state').json()['token']};j=c.post('/api/pdf-capture',json={'paper_id':self.pid},headers=token).json();url='/api/pdf-capture/'+j['id']+'/file'
   self.assertEqual(c.post(url,content=self.raw).status_code,403)
   headers={'X-Capture-Token':capture._jobs[j['id']]['_token'],'Content-Type':'application/pdf'}
   self.assertEqual(c.post(url,content=document('Another unrelated publication','10.5555/other'),headers=headers).status_code,422)
   r=c.post(url,content=self.raw,headers=headers);self.assertEqual(r.status_code,200);self.assertTrue(r.json()['saved']);self.assertEqual(c.get('/api/pdf-capture/'+j['id']).json()['status'],'completed');self.assertEqual(len(list(target.glob('*.pdf'))),1)
if __name__=='__main__':unittest.main()
