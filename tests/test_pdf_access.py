"""Real PDF validation and bounded source acquisition on a disposable library."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import fitz
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import app
from app import pdf_access as a
from app.db import get_db,set_setting
from app.store import upsert_paper

def pdf():
 with fitz.open() as d:
  d.new_page().insert_text((60,60),'Readable test PDF');return d.tobytes()

class PDF(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0')
  with get_db() as c:self.pid=upsert_paper(c,{'title':self.id(),'doi':'10.9999/'+self.id(),'oa_pdf_url':'https://www.nature.com/test.pdf'},'test','library')[0]
 def test_read_does_not_acquire_or_call_network(self):
  with patch.object(a.requests,'Session',side_effect=AssertionError('network')):self.assertFalse(a.status(self.pid)['ready'])
 def test_upload_validated_and_original_retained(self):
  with TestClient(app) as c:r=c.post(f'/api/papers/{self.pid}/pdf-access/upload',content=pdf(),headers={'Content-Type':'application/pdf'})
  self.assertEqual(r.status_code,200);self.assertTrue(a.status(self.pid)['ready']);self.assertEqual(r.json()['pages'],1)
 def test_html_disguised_pdf_rejected(self):
  with self.assertRaises(HTTPException):a.save_pdf(self.pid,b'<html>challenge</html>')
  self.assertFalse(a.status(self.pid)['ready'])
 def test_upload_does_not_overwrite_existing_original(self):
  f=Path(T.name)/'original.pdf';f.write_bytes(pdf());old=f.read_bytes()
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(f),self.pid))
  a.save_pdf(self.pid,pdf());self.assertEqual(f.read_bytes(),old)
 def test_redirect_hosts_checked_before_second_request(self):
  response=unittest.mock.MagicMock();response.status_code=302;response.headers={'Location':'http://127.0.0.1/private'};response.__enter__.return_value=response
  session=unittest.mock.MagicMock();session.__enter__.return_value=session;session.get.return_value=response
  with patch.object(a.requests,'Session',return_value=session):
   with self.assertRaises(HTTPException):a.download(self.pid)
  self.assertEqual(session.get.call_count,1)
 def test_bounded_download_validates_pdf(self):
  response=unittest.mock.MagicMock();response.status_code=200;response.__enter__.return_value=response;response.iter_content.return_value=[pdf()]
  session=unittest.mock.MagicMock();session.__enter__.return_value=session;session.get.return_value=response
  with patch.object(a.requests,'Session',return_value=session):self.assertTrue(a.download(self.pid)['ready'])
 def test_offline_download_blocked(self):
  set_setting('offline_mode','1')
  with patch.object(a.requests,'Session',side_effect=AssertionError('network')):
   with self.assertRaises(HTTPException):a.download(self.pid)
 def test_invalid_identity_never_installs_public_link(self):
  with patch.object(a.pdf_guide,'_find_pdf',side_effect=HTTPException(404)),patch.object(a.metadata_sources,'openalex',side_effect=ValueError('mismatch')):
   r=a.find(self.pid)
  self.assertFalse(r['ready']);self.assertIn('未找到公开PDF',r['message'])
 def test_legacy_download_uses_the_same_pdf_validator(self):
  with patch.object(a,"download",return_value={"ready":True,"pages":1}) as download,TestClient(app) as client:
   response=client.post(f"/api/papers/{self.pid}/download-pdf")
  self.assertEqual(response.status_code,200);download.assert_called_once_with(self.pid)
 def test_arbitrary_url_rejected(self):
  for url in ('http://www.nature.com/a.pdf','https://www.nature.com@127.0.0.1/a','https://example.org/a.pdf','https://www.nature.com:1234/a'):
   with self.assertRaises(HTTPException):a.checked_url(url)

if __name__=='__main__':unittest.main()
