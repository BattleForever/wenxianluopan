"""Exercise HTTP import jobs, real parser/upload, cancellation and Zotero reads."""
import os,sys,tempfile,time,unittest,threading,json
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import import_jobs as jobs,folders
from app.db import init_db,get_db,set_setting
from tests.pdf_fixture import document
init_db();folders.init();jobs.init()
api=FastAPI();api.include_router(jobs.router);client=TestClient(api)
def wait(j):
 for _ in range(150):
  r=client.get('/api/import/jobs/'+j['id']).json()
  if r['status'] not in jobs.ACTIVE:return r
  time.sleep(.02)
 raise AssertionError('Import did not finish')
class Jobs(unittest.TestCase):
 def target(self,scope='library'):
  with get_db() as c:
   stamp=folders.now();name='Target '+str(time.time_ns());return c.execute('INSERT INTO organizer_folders(scope,name,name_key,created_at,updated_at) VALUES(?,?,?,?,?)',(scope,name,name.lower(),stamp,stamp)).lastrowid
 def test_confirmed_destination_is_assigned_during_import(self):
  fid=self.target();j=wait(client.post('/api/import/jobs',json={'kind':'bibtex','text':'@article{dst,title={Confirmed destination import}}','destination_folder_ids':[fid]}).json())
  self.assertEqual(j['status'],'completed');pid=j['result']['paper_ids'][0]
  with get_db() as c:self.assertTrue(c.execute('SELECT 1 FROM organizer_items WHERE folder_id=? AND paper_id=?',(fid,pid)).fetchone())
 def test_invalid_destination_creates_neither_job_nor_paper(self):
  with get_db() as c:before=[c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('import_jobs','papers')]
  r=client.post('/api/import/jobs',json={'kind':'ris','text':'TY  - JOUR\nTI  - Invalid target should not import\nER  -','destination_folder_ids':[9999999]});self.assertEqual(r.status_code,404)
  with get_db() as c:self.assertEqual(before,[c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('import_jobs','papers')])
 def test_reading_folder_cannot_be_used_as_library_destination(self):
  fid=self.target('reading');r=client.post('/api/import/jobs',json={'kind':'doi','text':'10.1000/no-import','destination_folder_ids':[fid]});self.assertEqual(r.status_code,422)
 def test_pdf_destination_is_checked_before_file_is_written(self):
  from app.config import DATA_DIR
  before=set((DATA_DIR/'imports').glob('*.pdf'))
  with document() as doc:doc.new_page().insert_text((40,50),'Invalid target PDF fixture');raw=doc.tobytes()
  r=client.post('/api/import/pdf-upload?filename=test.pdf&folder_id=9999999',content=raw);self.assertEqual(r.status_code,404);self.assertEqual(before,set((DATA_DIR/'imports').glob('*.pdf')))
 def test_preview_honors_page_size_offset_and_total(self):
  item={'key':'PREVIEW1','data':{'key':'PREVIEW1','itemType':'journalArticle','title':'Preview fixture','publicationTitle':'Fixture Journal'}}
  with patch.object(jobs,'list_items',return_value=([item],{'Total-Results':'21'})) as source:
   r=client.get('/api/import/zotero/items?start=10&limit=10&query=pmut').json()
  source.assert_called_once_with(None,10,10,'pmut');self.assertEqual(r['limit'],10);self.assertEqual(r['total'],21);self.assertTrue(r['has_more']);self.assertEqual(r['items'][0]['venue'],'Fixture Journal')
 def test_preview_last_page_infers_total_without_header(self):
  with patch.object(jobs,'list_items',return_value=([],{})):r=client.get('/api/import/zotero/items?start=20&limit=20').json()
  self.assertEqual(r['total'],20);self.assertFalse(r['has_more'])
 def test_preview_rejects_excessive_page_size(self):
  for size in (0,101):self.assertEqual(client.get('/api/import/zotero/items?limit='+str(size)).status_code,422)
 def test_text_job_real_bibtex_and_empty_validation(self):
  self.assertEqual(client.post('/api/import/jobs',json={'kind':'bibtex','text':''}).status_code,422)
  j=wait(client.post('/api/import/jobs',json={'kind':'bibtex','text':'@article{test,title={Job imported fixture},year={2026}}'}).json())
  self.assertEqual(j['status'],'completed');self.assertEqual(j['result']['added'],1)
 def test_wrong_format_is_visible_failure(self):
  j=wait(client.post('/api/import/jobs',json={'kind':'ris','text':'not RIS'}).json())
  self.assertEqual(j['status'],'completed_with_errors');self.assertTrue(j['result']['errors']);self.assertEqual(j['result']['added'],0)
 def test_standard_missing_identity_does_not_create_empty_paper(self):
  j=wait(client.post('/api/import/jobs',json={'kind':'standard','text':'{}'}).json())
  self.assertEqual(j['result']['added'],0);self.assertTrue(j['result']['errors'])
 def test_upload_validates_pdf_then_saves_named_source_offline(self):
  self.assertEqual(client.post('/api/import/pdf-upload?filename=bad.pdf',content=b'not pdf').status_code,422)
  with document() as doc:
   doc.new_page().insert_text((40,50),'Upload fixture without DOI');raw=doc.tobytes()
  with patch('app.importer._match_paper',return_value=None):j=wait(client.post('/api/import/pdf-upload?filename=Named%20upload%20fixture.pdf',content=raw).json())
  self.assertEqual(j['result']['added'],1)
  with get_db() as c:r=c.execute("SELECT title,local_pdf_path FROM papers WHERE title='Named upload fixture'").fetchone()
  self.assertIsNotNone(r);self.assertEqual(Path(r['local_pdf_path']).read_bytes(),raw)
  # Same content with another filename stays one record, including folder import.
  other=Path(T.name)/'Another filename for the same paper.PDF';other.write_bytes(raw)
  j=wait(client.post('/api/import/jobs',json={'kind':'pdf-folder','text':T.name}).json())
  self.assertEqual(j['result']['added'],0);self.assertGreaterEqual(j['result']['updated'],1)
 def test_zotero_read_preserves_doi_and_folder_tree(self):
  item={'key':'ITEM0001','data':{'key':'ITEM0001','itemType':'journalArticle','title':'Read only Zotero source','DOI':'10.1000/zotero-job','collections':['CHILD001']}}
  class Cli:
   def get_item(self,key):return item
  rows=[{'key':'PARENT01','name':'Research','parent':None},{'key':'CHILD001','name':'Papers','parent':'PARENT01'}]
  with patch.object(jobs,'ZoteroClient',Cli),patch.object(jobs,'collection_rows',return_value=rows),patch('app.importer._zotero_pdf_path',return_value=None):
   for attempt in range(2):
    j=wait(client.post('/api/import/jobs',json={'kind':'zotero','item_keys':['ITEM0001']}).json())
    self.assertEqual(j['status'],'completed');self.assertEqual(j['result']['added' if attempt==0 else 'updated'],1)
  with get_db() as c:
   self.assertEqual(c.execute("SELECT COUNT(*) FROM papers WHERE doi='10.1000/zotero-job'").fetchone()[0],1)
   folders=c.execute("SELECT name,parent_id FROM organizer_folders WHERE name IN ('Research','Papers')").fetchall();self.assertEqual(len(folders),2);self.assertTrue(next(r['parent_id'] for r in folders if r['name']=='Papers'))
 def test_nonroot_item_pagination_uses_current_local_api(self):
  calls=[]
  class Cli:
   def _request(self,method,path):
    if path=='/api/itemTypes':return 200,{},[{'itemType':k} for k in ['journalArticle','book','note','annotation','attachment']]
    calls.append((method,path));return 200,{},[]
  with patch.object(jobs,'ZoteroClient',Cli):jobs.list_items('COLLECT1',100,100,'term')
  self.assertEqual(calls[0][0],'GET');self.assertTrue(calls[0][1].startswith('/api/users/0/collections/COLLECT1/items/top?'));self.assertIn('start=100',calls[0][1])
  from urllib.parse import urlparse,parse_qs
  kinds=parse_qs(urlparse(calls[0][1]).query)['itemType'][0]
  self.assertEqual(kinds,'book || journalArticle');self.assertNotIn('attachment',kinds);self.assertNotIn('note',kinds)
 def test_cancellation_survives_current_request_and_next_job_runs(self):
  entered=threading.Event();release=threading.Event()
  def slow(text):entered.set();release.wait(3);return {'added':1,'updated':0,'total':1,'errors':[]}
  with patch('app.importer.import_dois',side_effect=slow):
   j=client.post('/api/import/jobs',json={'kind':'doi','text':'10.1000/cancel-a\n10.1000/cancel-b'}).json();self.assertTrue(entered.wait(2));client.post('/api/import/jobs/'+j['id']+'/cancel');release.set();done=wait(j)
  self.assertEqual(done['status'],'cancelled');self.assertEqual(done['completed'],1)
  next_job=wait(client.post('/api/import/jobs',json={'kind':'ris','text':'TY  - JOUR\nTI  - Next queued import\nER  -'}).json());self.assertEqual(next_job['status'],'completed')
 def test_connection_error_is_actionable_and_contains_no_wire_body(self):
  from app.zotero_client import ZoteroUnavailable
  with patch.object(jobs,'list_items',side_effect=ZoteroUnavailable('private wire content')):
   r=client.get('/api/import/zotero/items');self.assertEqual(r.status_code,503);self.assertNotIn('private wire content',r.text)
 def test_running_import_blocks_workspace_migration_or_backup(self):
  from app.workspace_data import job_count
  before=job_count();j={'id':'workspace_guard_test','status':'queued','created_at':'2026-01-01'};jobs.save(j)
  self.assertEqual(job_count(),before+1)
  j['status']='completed';jobs.save(j);self.assertEqual(job_count(),before)

if __name__=='__main__':unittest.main()
