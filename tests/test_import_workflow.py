"""Import real supported formats, network failures and Zotero identity safely."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import importer
from app.db import init_db,get_db
from app.netclient import OfflineError
from tests.pdf_fixture import document as open_document
init_db()

class Imports(unittest.TestCase):
 def test_legacy_zotero_transport_adds_api_prefix(self):
  from app import netclient
  from unittest.mock import Mock
  with patch.object(netclient.requests,'get',return_value=Mock(status_code=200)) as read:
   netclient.zotero_get('/users/0/collections')
  self.assertEqual(read.call_args[0][0],'http://127.0.0.1:23119/api/users/0/collections')
 def test_invalid_text_does_not_report_success(self):
  for fn in (importer.import_dois,importer.import_bibtex,importer.import_ris):
   with self.subTest(fn=fn.__name__),self.assertRaises(ValueError):fn('not a bibliographic record')
 def test_doi_preserves_semicolon_and_parentheses(self):
  seen=[]
  def work(doi):seen.append(doi);return {'doi':'https://doi.org/'+doi,'display_name':'A DOI fixture '+str(len(seen)),'id':'https://openalex.org/W'+str(len(seen))}
  with patch.object(importer,'oa_work_by_doi',side_effect=work):
   d=importer.import_dois('doi:10.1000/abc;def\nhttps://doi.org/10.1000/test(1)')
  self.assertEqual(d['added'],2);self.assertEqual(seen,['10.1000/abc;def','10.1000/test(1)'])
 def test_doi_falls_back_after_provider_failure(self):
  with patch.object(importer,'oa_work_by_doi',side_effect=RuntimeError('upstream unavailable')),patch('app.metadata_sources.crossref',return_value={'doi':'10.1000/fallback','title':'Crossref fallback fixture','_source':'Crossref'}):
   self.assertEqual(importer.import_dois('10.1000/fallback')['added'],1)
 def test_pdf_still_imports_when_lookup_is_offline(self):
  file=Path(T.name)/'Readable PDF source.pdf'
  with open_document() as doc:
   page=doc.new_page();page.insert_text((40,40),'Readable PDF source');page.insert_text((40,80),'10.1000/offline');doc.save(file)
  with patch.object(importer,'_match_paper',side_effect=OfflineError()):r=importer.scan_pdf_folder(str(file.parent))
  self.assertGreaterEqual(r['title_only'],1)
  with get_db() as c:self.assertTrue(c.execute('SELECT 1 FROM papers WHERE local_pdf_path=?',(str(file),)).fetchone())
 def test_ris_bom_continuation_and_final_unterminated_record(self):
  d=importer.import_ris('\ufeffTY  - JOUR\nTI  - Imported long\n      title\nPY  - 2024\nDO  - 10.1000/ris-edge')
  self.assertEqual(d['added'],1)
  with get_db() as c:self.assertEqual(c.execute("SELECT title FROM papers WHERE doi='10.1000/ris-edge'").fetchone()[0],'Imported long title')
 def test_zotero_uppercase_doi_and_deleted_items(self):
  p=importer._zotero_item_to_paper({'key':'ABCDEFGH','data':{'key':'ABCDEFGH','itemType':'journalArticle','DOI':'10.1000/Zotero','title':'Zotero fixture'}})
  self.assertEqual(p['doi'],'10.1000/zotero')
  self.assertIsNone(importer._zotero_item_to_paper({'data':{'itemType':'journalArticle','deleted':1,'title':'Deleted'}}))
 def test_pdf_missing_folder_is_validation_error(self):
  with self.assertRaises(ValueError):importer.scan_pdf_folder(str(Path(T.name)/'missing'))

if __name__=='__main__':unittest.main()
