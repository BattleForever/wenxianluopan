"""Regression: Connector linked PDFs and duplicate exact-DOI parent records."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pdf_fixture as pdfgen
from fastapi import HTTPException
from app.main import app
from app import pdf_guide,pdf_access,zotero_pdf
from app.db import get_db,set_setting
from app.store import upsert_paper

class Attachments(unittest.TestCase):
 def setUp(self):
  self.doi='10.5555/'+self._testMethodName
  with get_db() as c:self.pid=upsert_paper(c,{'title':'A linked original saved by Connector','doi':self.doi},'fixture','library')[0]
  set_setting('zotero_data_dir',str(Path(T.name)/'unavailable_index'))
  self.file=Path(T.name)/(self._testMethodName+'.pdf')
  with pdfgen.document() as d:d.new_page().insert_text((30,30),self.doi);d.save(self.file)
  self.cli=MagicMock();self.cli.search_items.return_value=[{'key':'EMPTY','data':{'itemType':'journalArticle','DOI':self.doi}},{'key':'WITHPDF','data':{'itemType':'conferencePaper','DOI':self.doi.upper()}}]
  self.cli.get_children.side_effect=lambda key:[] if key=='EMPTY' else [{'key':'LINKED','data':{'itemType':'attachment','parentItem':'WITHPDF','contentType':'application/pdf','linkMode':'linked_file'}}]
  self.cli.attachment_file.return_value=self.file
 def test_find_reuses_connector_linked_pdf_on_another_exact_doi_parent(self):
  with patch('app.zotero_client.ZoteroClient',return_value=self.cli):f=pdf_guide._find_pdf(pdf_access.paper(self.pid))
  self.assertEqual(f,self.file);self.assertEqual(pdf_access.paper(self.pid)['local_pdf_path'],str(self.file));self.cli.create_items.assert_not_called();self.assertEqual(pdf_access.status(self.pid)['saved_to'],'zotero')
 def test_wrong_doi_and_deleted_records_never_supply_pdf(self):
  self.cli.search_items.return_value=[{'key':'WRONG','data':{'itemType':'journalArticle','DOI':'10.5555/other'}},{'key':'DELETED','data':{'itemType':'journalArticle','DOI':self.doi,'deleted':1}}]
  with patch('app.zotero_client.ZoteroClient',return_value=self.cli),self.assertRaises(HTTPException):pdf_guide._find_pdf(pdf_access.paper(self.pid))
  self.cli.get_children.assert_not_called()
 def test_existing_local_source_does_not_query_zotero(self):
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(self.file),self.pid))
  with patch('app.zotero_client.ZoteroClient',side_effect=AssertionError('unnecessary query')):self.assertEqual(pdf_guide._find_pdf(pdf_access.paper(self.pid)),self.file)
 def test_missing_linked_file_continues_other_attachments(self):
  self.cli.get_children.side_effect=None;self.cli.get_children.return_value=[{'key':'MISSING','data':{'itemType':'attachment','parentItem':'EMPTY','contentType':'application/pdf','linkMode':'linked_file'}},{'key':'VALID','data':{'itemType':'attachment','parentItem':'EMPTY','contentType':'application/pdf','linkMode':'linked_file'}}]
  self.cli.attachment_file.side_effect=lambda k:Path(T.name)/'does-not-exist.pdf' if k=='MISSING' else self.file
  with patch('app.zotero_client.ZoteroClient',return_value=self.cli):self.assertEqual(pdf_guide._find_pdf(pdf_access.paper(self.pid)),self.file)
 def test_attachment_manager_move_refreshes_status_by_key_without_search_or_write(self):
  import hashlib
  digest=hashlib.sha256(self.file.read_bytes()).hexdigest()
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(Path(T.name)/'old-storage'/'LINKED'/'paper.pdf'),self.pid))
  zotero_pdf.record(self.pid,'saved','verified','LINKED',digest,'old-storage')
  with patch('app.zotero_client.ZoteroClient',return_value=self.cli):result=pdf_access.status(self.pid)
  self.assertTrue(result['ready']);self.assertEqual(result['saved_to'],'zotero');self.cli.attachment_file.assert_called_once_with('LINKED');self.cli.search_items.assert_not_called();self.cli.create_items.assert_not_called()
 def test_moved_attachment_with_changed_content_is_not_silently_rebound(self):
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',('vanished.pdf',self.pid))
  zotero_pdf.record(self.pid,'saved','verified','LINKED','0'*64,'vanished.pdf')
  with patch('app.zotero_client.ZoteroClient',return_value=self.cli):self.assertFalse(pdf_access.status(self.pid)['ready'])
 def test_attachment_manager_replaces_key_recovered_by_paper_identity_and_hash(self):
  import hashlib
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',('old-imported-file.pdf',self.pid))
  zotero_pdf.record(self.pid,'saved','verified','OLDKEY',hashlib.sha256(self.file.read_bytes()).hexdigest(),'old-imported-file.pdf')
  def location(key):
   if key=='OLDKEY':raise ValueError('old imported attachment replaced')
   return self.file
  self.cli.attachment_file.side_effect=location
  with patch('app.zotero_client.ZoteroClient',return_value=self.cli):self.assertTrue(pdf_access.status(self.pid)['ready'])
  self.assertEqual(zotero_pdf.status(self.pid)['attachment_key'],'LINKED');self.cli.create_items.assert_not_called()
 def test_publisher_pdf_wrappers_and_citation_links_need_no_extension(self):
  from app.publisher_pdf import candidates
  html='<meta name="citation_pdf_url" content="/stamp/stamp.jsp?arnumber=123"><iframe src="/download/original.pdf"></iframe><object data="https://files.example/paper.pdf"></object><a href="https://127.0.0.1/private">other</a>'
  self.assertEqual(candidates(html,'https://publisher.example/article'),['https://publisher.example/stamp/stamp.jsp?arnumber=123','https://publisher.example/download/original.pdf','https://files.example/paper.pdf'])

if __name__=='__main__':unittest.main(verbosity=2)
