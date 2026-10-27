"""Recovery and paper-lifecycle regressions on a disposable database."""
import json,os,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
import pdf_fixture as pdfgen
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.main import app
from app import metadata_jobs as m,pdf_access as pdf,pdf_guide,trash,exploration,zotero_link
from app.db import get_db,set_setting
from app.store import upsert_paper
from app.zotero_client import ZoteroClient
from app.private_settings import atomic_json
from fastapi import HTTPException

class Recovery(unittest.TestCase):
 def setUp(self):
  m._active.clear();exploration._cancel.clear();set_setting('offline_mode','0')
  with get_db() as c:self.pid=upsert_paper(c,{'title':self.id()},'test','library')[0]
 def test_interrupted_retry_includes_unprocessed_and_partial(self):
  job={'id':'interrupted','status':'interrupted','ids':[self.pid,999,1000],
       'results':[{'paper_id':self.pid,'filled':['title_zh'],'still_missing':['abstract_zh'],'errors':[]},
                  {'paper_id':999,'filled':[],'outcome':'already_complete','still_missing':[],'errors':[]}]}
  m.save(job);self.assertEqual(m.read(job['id'])['retry_ids'],[self.pid,1000])
 def test_recent_tasks_discoverable_without_browser_storage(self):
  job={'id':'discoverable','status':'interrupted','ids':[self.pid],'results':[],'total':1,'completed':0,'fields_filled':0}
  m.save(job);self.assertIn(job['id'],[r['id'] for r in m.recent()['jobs']])
 def test_completion_rejects_trashed_paper_before_thread(self):
  trash.put(trash.Put(ids=[self.pid],scope='library'))
  with patch.object(m.threading,'Thread') as start:
   with self.assertRaises(HTTPException) as e:m.start(m.Start(ids=[self.pid]))
  self.assertEqual(e.exception.status_code,409);start.assert_not_called()
 def test_pdf_source_rejects_trashed_paper(self):
  trash.put(trash.Put(ids=[self.pid],scope='library'))
  with pdfgen.document() as d:d.new_page();raw=d.tobytes()
  with self.assertRaises(HTTPException) as e:pdf.save_pdf(self.pid,raw)
  self.assertEqual(e.exception.status_code,409)
 def test_scanned_source_explains_ocr_before_generation(self):
  with pdfgen.document() as d:d.new_page();raw=d.tobytes()
  pdf.save_pdf(self.pid,raw);status=pdf.status(self.pid)
  self.assertTrue(status['ready']);self.assertFalse(status['reading_ready']);self.assertIn('OCR',status['reading_issue'])
 def test_find_does_not_claim_corrupt_attachment_is_ready(self):
  f=Path(T.name)/'corrupt.pdf';f.write_bytes(b'not a PDF')
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(f),self.pid))
  with patch.object(pdf.metadata_sources,'openalex',return_value=None):r=pdf.find(self.pid)
  self.assertFalse(r['ready']);self.assertNotEqual(r['message'],'已关联可读PDF')
 def test_legacy_zotero_client_reuses_desktop_authorization(self):
  atomic_json(Path(T.name)/'private/zotero.local.json',{'api_key':'synthetic-key'})
  with patch.dict(os.environ,{'PAPERNET_ZOTERO_API_KEY':''}):self.assertEqual(ZoteroClient().api_key,'synthetic-key')
 def test_unknown_paper_sync_never_contacts_zotero(self):
  with patch.object(zotero_link,'client') as client:
   with self.assertRaises(HTTPException) as e:zotero_link.sync(zotero_link.Sync(paper_id=999999))
  self.assertEqual(e.exception.status_code,404);client.assert_not_called()
 def test_failed_task_persistence_does_not_leave_active_lock(self):
  with patch.object(m,'save',side_effect=OSError('synthetic disk failure')):
   with self.assertRaises((OSError,HTTPException)):m.start(m.Start(ids=[self.pid]))
  self.assertFalse(m._active)
 def test_failed_exploration_thread_start_does_not_block_retry(self):
  with patch.object(exploration.threading.Thread,'start',side_effect=RuntimeError('synthetic thread failure')):
   with self.assertRaises(RuntimeError):exploration.create({'seed_ids':[self.pid],'online':False})
  self.assertFalse(exploration._cancel)
 def test_reading_list_does_not_decode_the_entire_library(self):
  from app import workspace as w
  with get_db() as c:c.execute('INSERT OR IGNORE INTO reading_list(paper_id) VALUES(?)',(self.pid,))
  with patch.object(w,'paper_dict',wraps=w.paper_dict) as decoded:
   result=w.search(w.SearchIn(scope='reading'))
  self.assertEqual(decoded.call_count,result['total'])

if __name__=='__main__':unittest.main()
