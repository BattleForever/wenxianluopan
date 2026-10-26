"""PDF attachment flow with isolated files and native-API transport fixtures."""
import os,sys,tempfile,unittest,json,hashlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fitz
from app.main import app
from app.db import get_db
from app.store import upsert_paper
from app import zotero_pdf as z,pdf_access
from app.zotero_client import ZoteroClient,ZoteroError

def pdf(text='Readable test original'):
 with fitz.open() as d:d.new_page().insert_text((60,60),text);return d.tobytes()
class Fake:
 def __init__(self):self.children=[];self.creates=0;self.uploads=0;self.fail=False;self.raw=None
 def get_children(self,parent):return self.children
 def create_items(self,items):
  self.creates+=1;self.children.append({'key':'ATTACH1','data':dict(items[0])});return {'success':{'0':'ATTACH1'}}
 def get_item(self,key):return next(x for x in self.children if x['key']==key)
 def attachment_file(self,key):
  if self.raw is None:raise ZoteroError('not stored')
  path=Path(T.name)/('stored-'+key+'.pdf');path.write_bytes(self.raw);return path
 def upload_pdf_attachment(self,key,raw,*args):
  self.uploads+=1
  if self.fail:raise ZoteroError('temporary fixture error')
  self.raw=raw
class PDFSync(unittest.TestCase):
 def setUp(self):
  self.raw=pdf();self.f=Path(T.name)/(self.id().split('.')[-1]+'.pdf');self.f.write_bytes(self.raw);self.cli=Fake()
  with get_db() as c:self.pid=upsert_paper(c,{'title':self.id(),'local_pdf_path':str(self.f)},'fixture','library')[0]
 def test_upload_creates_child_and_compares_stored_bytes(self):
  d=z.sync_pdf(self.pid,'PARENT',self.cli);self.assertEqual(d['state'],'saved');self.assertEqual(d['sha256'],hashlib.sha256(self.raw).hexdigest());self.assertEqual(self.cli.creates,1)
  self.assertEqual(self.cli.children[0]['data']['linkMode'],'imported_file');self.assertEqual(self.cli.children[0]['data']['parentItem'],'PARENT')
 def test_existing_pdf_is_reused_without_creating_or_downloading(self):
  self.cli.children=[{'key':'USERPDF','data':{'itemType':'attachment','parentItem':'PARENT','contentType':'application/pdf','linkMode':'linked_file'}}];self.cli.raw=self.raw
  with patch.object(pdf_access,'find',side_effect=AssertionError('unnecessary download')):d=z.sync_pdf(self.pid,'PARENT',self.cli)
  self.assertEqual(d['state'],'existing');self.assertEqual(self.cli.creates,0);self.assertEqual(self.cli.uploads,0)
 def test_retry_reuses_pending_attachment_and_preserves_source(self):
  self.cli.fail=True;self.assertEqual(z.sync_pdf(self.pid,'PARENT',self.cli)['state'],'pending')
  self.cli.fail=False;self.assertEqual(z.sync_pdf(self.pid,'PARENT',self.cli)['state'],'saved');self.assertEqual(self.cli.creates,1);self.assertEqual(self.f.read_bytes(),self.raw)
 def test_upload_readback_mismatch_never_reports_saved(self):
  def upload(*args):self.cli.raw=pdf('Different PDF bytes')
  self.cli.upload_pdf_attachment=upload;self.assertEqual(z.sync_pdf(self.pid,'PARENT',self.cli)['state'],'pending');self.assertEqual(z.sync_pdf(self.pid,'PARENT',self.cli)['state'],'pending');self.assertEqual(self.cli.creates,1)
 def test_missing_original_is_independent_of_bibliographic_success(self):
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=NULL WHERE id=?',(self.pid,))
  with patch.object(pdf_access,'find',return_value={'ready':False,'has_public_link':False}):d=z.sync_pdf(self.pid,'PARENT',self.cli)
  self.assertEqual(d['state'],'missing');self.assertEqual(self.cli.creates,0)
 def test_public_pdf_acquisition_is_used_when_local_original_missing(self):
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=NULL WHERE id=?',(self.pid,))
  def downloaded(pid):
   with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(self.f),pid))
   return {'ready':True}
  with patch.object(pdf_access,'find',return_value={'ready':False,'has_public_link':True}),patch.object(pdf_access,'download',side_effect=downloaded) as dl:d=z.sync_pdf(self.pid,'PARENT',self.cli)
  self.assertEqual(d['state'],'saved');dl.assert_called_once_with(self.pid)
 def test_pdf_from_wrong_parent_not_claimed_as_existing(self):
  self.cli.children=[{'key':'OTHER','data':{'itemType':'attachment','parentItem':'UNRELATED','contentType':'application/pdf'}}];self.cli.raw=self.raw
  d=z.sync_pdf(self.pid,'PARENT',self.cli);self.assertEqual(d['state'],'saved');self.assertEqual(d['attachment_key'],'ATTACH1')
 def test_html_or_oversize_file_never_becomes_attachment(self):
  self.f.write_bytes(b'<html>not a PDF</html>')
  with patch.object(pdf_access,'find',return_value={'ready':False,'has_public_link':False}):d=z.sync_pdf(self.pid,'PARENT',self.cli)
  self.assertEqual(d['state'],'missing');self.assertEqual(self.cli.creates,0)
class NativeUpload(unittest.TestCase):
 def run_upload(self,remote=False,exists=False,reject=False,relative=False,alias=False):
  calls=[];raw=pdf();token='LOCALUPLOAD1'
  def send(method,url,headers,body,timeout):
   calls.append((method,url,headers,body))
   if '/local/uploads/' in url:return 201,{},b''
   form=parse_qs(body.decode())
   if 'upload' in form:return 204,{},b''
   if reject:return 500,{},b'{"uploadKey":"do-not-log-this"}'
   if exists:return 200,{},b'{"exists":1}'
   payload={'url':('https://remote.invalid' if remote else 'http://127.0.0.1:23119')+'/api/local/uploads/'+token,'uploadKey':token,'prefix':'','suffix':'','contentType':'application/octet-stream'}
   if relative:payload['url']='/api/local/uploads/'+token
   if alias:payload['url']='http://localhost:23119/api/local/uploads/'+token
   return 200,{},json.dumps(payload).encode()
  cli=ZoteroClient(api_key='fixture-credential',send=send);cli._server_id='fixture-server'
  cli.upload_pdf_attachment('ATTACH1',raw,'paper.pdf',1234567890)
  return calls,raw
 def test_official_three_phase_protocol_and_auth_boundaries(self):
  calls,raw=self.run_upload();self.assertEqual(len(calls),3)
  form=parse_qs(calls[0][3].decode());self.assertEqual(form['md5'],[hashlib.md5(raw).hexdigest()]);self.assertEqual(calls[0][2]['If-None-Match'],'*')
  self.assertIn('Zotero-API-Key',calls[0][2]);self.assertNotIn('Zotero-API-Key',calls[1][2]);self.assertEqual(calls[1][3],raw);self.assertIn('Zotero-API-Key',calls[2][2])
 def test_relative_upload_url_is_bound_to_the_same_server(self):self.assertEqual(len(self.run_upload(relative=True)[0]),3)
 def test_loopback_alias_never_changes_the_upload_destination(self):self.assertIn('http://127.0.0.1:23119/api/local/uploads/',self.run_upload(alias=True)[0][1][1])
 def test_existing_file_skips_binary_transfer(self):self.assertEqual(len(self.run_upload(exists=True)[0]),1)
 def test_external_upload_target_is_rejected(self):
  with self.assertRaises(ZoteroError):self.run_upload(remote=True)
 def test_transfer_failure_does_not_expose_upload_ticket(self):
  with self.assertRaises(ZoteroError) as e:self.run_upload(reject=True)
  self.assertNotIn('do-not-log-this',str(e.exception))
if __name__=='__main__':unittest.main()
