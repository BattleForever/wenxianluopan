"""Recovery and expiry integration checks on a disposable SQLite library."""
import json,os,sys,tempfile,unittest,zipfile,time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from pydantic import ValidationError
from app.db import get_db,init_db
from app.store import upsert_paper
from app import folders as f,workspace as w,trash as t
init_db();w.init()

class Trash(unittest.TestCase):
 def setUp(self):
  with get_db() as c:
   for table in ('paper_trash','organizer_undo','organizer_items','organizer_legacy','organizer_folders','reading_list','citations','papers'):c.execute('DELETE FROM '+table)
   c.execute('UPDATE trash_settings SET days=30')
   self.pid=upsert_paper(c,{'title':'Recovery case','doi':'10.9999/recovery','year':2025},'test','library')[0]
  self.a=f.create(f.Create(scope='library',name='课题'))['folder']['id']
  self.b=f.create(f.Create(scope='library',name='方法',parent_id=self.a))['folder']['id']
  self.r=f.create(f.Create(scope='reading',name='本周精读'))['folder']['id']
  f.assign(f.Items(scope='library',ids=[self.pid],folder_ids=[self.b]))
  f.assign(f.Items(scope='reading',ids=[self.pid],folder_ids=[self.r]))
  w.reading(w.ReadingIn(ids=[self.pid],note='Keep this reading note'))
 def put(self,s='library'):return t.put(t.Put(scope=s,ids=[self.pid]))
 def search(self,s):return w.search(w.SearchIn(scope=s))['total']
 def age(self,days):
  with get_db() as c:c.execute('UPDATE paper_trash SET deleted_at=?',((t.now()-timedelta(days=days)).isoformat(),))
 def test_library_hides_every_view_but_preserves_rows_and_edges(self):
  self.put();self.assertEqual(self.search('library'),0);self.assertEqual(self.search('reading'),0);self.assertEqual(self.search('all'),0)
  with get_db() as c:self.assertIsNotNone(c.execute('SELECT * FROM papers WHERE id=?',(self.pid,)).fetchone())
  self.assertEqual(f.list_folders()['folders'][0]['count'],0)
 def test_reading_trash_does_not_remove_library(self):
  w.reading(w.ReadingIn(ids=[self.pid],action='remove'));self.assertEqual(self.search('reading'),0);self.assertEqual(self.search('library'),1)
  self.assertEqual(t.items('reading')['total'],1);self.assertEqual(t.items()['total'],0)
 def test_restore_preserves_note_and_nested_membership(self):
  self.put();t.restore(t.Restore(scope='library',ids=[self.pid]))
  p=w.search(w.SearchIn(scope='reading'))['papers'][0]
  self.assertEqual(p['reading_note'],'Keep this reading note');self.assertIn(self.b,[x['id'] for x in p['folders']])
 def test_restore_recreates_deleted_folder_tree(self):
  self.put();f.delete(self.a);t.restore(t.Restore(scope='library',ids=[self.pid]))
  rows=f.list_folders()['folders'];child=next(x for x in rows if x['name']=='方法');parent=next(x for x in rows if x['name']=='课题')
  self.assertEqual(child['parent_id'],parent['id']);self.assertEqual(child['count'],1)
 def test_restore_reuses_renamed_original_folder(self):
  self.put();f.edit(self.b,f.Edit(name='新名称'));t.restore(t.Restore(scope='library',ids=[self.pid]))
  self.assertEqual(len(f.list_folders()['folders']),2);self.assertEqual(f.get_folder(self.b)['name'],'新名称')
 def test_repeated_delete_does_not_reset_age(self):
  self.put();self.age(8);old=t.items()['items'][0]['deleted_at'];self.assertEqual(self.put()['affected'],0);self.assertEqual(t.items()['items'][0]['deleted_at'],old)
 def test_settings_independent_and_zero_means_keep(self):
  self.assertEqual(t.items()['days'],30);t.settings('library',t.Retention(days=0));self.put();self.age(100)
  with patch.object(t,'recycle_file') as recycle:self.assertEqual(t.sweep()['recycled'],0);recycle.assert_not_called()
  self.assertEqual(t.items('reading')['days'],30);self.assertIsNone(t.items()['items'][0]['due_at'])
 def test_setting_validation(self):
  for days in (-1,3651):
   with self.assertRaises(ValidationError):t.Retention(days=days)
 def test_expiry_does_not_run_before_day_thirty(self):
  self.put();self.age(29)
  with patch.object(t,'recycle_file') as recycle:t.sweep();recycle.assert_not_called()
 def test_expiry_sends_verified_recovery_bundle_and_retains_secondary_recovery(self):
  pdf=Path(T.name)/'original.pdf';pdf.write_bytes(b'%PDF-test-keep-original')
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(pdf),self.pid))
  self.put();self.age(31);bundles=[]
  def capture(path):
   with zipfile.ZipFile(path) as z:
    obj=json.loads(z.read('recovery.json'));self.assertEqual(obj['paper']['id'],self.pid);self.assertEqual(z.read('document.pdf'),pdf.read_bytes());self.assertNotIn('settings',obj['tables']);bundles.append(obj)
  with patch.object(t,'recycle_file',side_effect=capture):self.assertEqual(t.sweep()['recycled'],1)
  self.assertEqual(t.items()['total'],0);self.assertEqual(t.items(archived=True)['total'],1);self.assertEqual(pdf.read_bytes(),b'%PDF-test-keep-original')
  t.restore(t.Restore(scope='library',ids=[self.pid]));self.assertEqual(self.search('library'),1)
 def test_os_failure_keeps_everything_and_reports_retry(self):
  self.put();self.age(31)
  with patch.object(t,'recycle_file',side_effect=OSError('private path must not be logged')):self.assertEqual(t.sweep()['failed'],1)
  row=t.items()['items'][0];self.assertNotIn('private path',row['error']);self.assertEqual(row['state'],'pending')
  self.assertEqual(len(list((Path(T.name)/'recycle_staging').glob('*.zip')))>0,True)
 def test_reading_bundle_never_copies_or_deletes_shared_pdf(self):
  pdf=Path(T.name)/'reading.pdf';pdf.write_bytes(b'shared')
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(pdf),self.pid))
  self.put('reading')
  def capture(path):
   with zipfile.ZipFile(path) as z:self.assertNotIn('document.pdf',z.namelist())
  with patch.object(t,'recycle_file',side_effect=capture):self.assertEqual(t.recycle(t.Put(scope='reading',ids=[self.pid]))['recycled'],1)
  self.assertTrue(pdf.exists());self.assertEqual(self.search('library'),1)
 def test_invalid_batch_rolls_back(self):
  with self.assertRaises(HTTPException):t.put(t.Put(scope='library',ids=[self.pid,999999]))
  self.assertEqual(t.items()['total'],0)
 def test_trash_blocks_classification_and_invalidates_undo(self):
  result=f.assign(f.Items(scope='library',ids=[self.pid],folder_ids=[self.a]));self.put()
  with self.assertRaises(HTTPException):f.assign(f.Items(scope='library',ids=[self.pid],folder_ids=[self.a]))
  with self.assertRaises(HTTPException):f.undo(result['undo_token'])
 def test_reading_restore_requires_library_restore_first(self):
  self.put('reading');self.put()
  with self.assertRaises(HTTPException):t.restore(t.Restore(scope='reading',ids=[self.pid]))
  t.restore(t.Restore(scope='library',ids=[self.pid]));self.assertEqual(self.search('reading'),0)
  t.restore(t.Restore(scope='reading',ids=[self.pid]));self.assertEqual(self.search('reading'),1)
 def test_readding_reading_restores_note_and_folders(self):
  self.put('reading');w.reading(w.ReadingIn(ids=[self.pid]))
  paper=w.search(w.SearchIn(scope='reading'))['papers'][0];self.assertEqual(paper['reading_note'],'Keep this reading note');self.assertIn(self.r,[x['id'] for x in paper['folders']])
 def test_classifying_into_reading_restores_and_undo_retrashes_atomically(self):
  self.put('reading');other=f.create(f.Create(scope='reading',name='Later'))['folder']['id']
  result=f.assign(f.Items(scope='reading',ids=[self.pid],folder_ids=[other]))
  self.assertEqual(self.search('reading'),1);self.assertEqual(t.items('reading')['total'],0)
  f.undo(result['undo_token']);self.assertEqual(self.search('reading'),0);self.assertEqual(t.items('reading')['total'],1)
 def test_application_startup_catches_up_expired_items(self):
  self.put();self.age(31)
  from app.main import app
  from fastapi.testclient import TestClient
  with patch.object(t,'recycle_file') as recycle:
   with TestClient(app):
    deadline=time.monotonic()+3
    while t.items(archived=True)['total']!=1 and time.monotonic()<deadline:time.sleep(.02)
    self.assertEqual(t.items(archived=True)['total'],1);recycle.assert_called_once()
 def test_legacy_delete_route_soft_deletes_and_counts_only_active_edges(self):
  from app.main import app
  from fastapi.testclient import TestClient
  with get_db() as c:c.execute('INSERT INTO citations(from_id,to_id,src) VALUES(?,?,?)',(self.pid,self.pid,'test'))
  with TestClient(app) as client:
   response=client.delete('/api/papers/'+str(self.pid));self.assertEqual(response.status_code,200)
   health=client.get('/api/health').json();self.assertEqual(health['library'],0);self.assertEqual(health['edges'],0)
   self.assertEqual(client.get('/api/papers').json()['total'],0)
  with get_db() as c:
   self.assertIsNotNone(c.execute('SELECT * FROM papers WHERE id=?',(self.pid,)).fetchone());self.assertEqual(c.execute('SELECT COUNT(*) FROM citations').fetchone()[0],1)

if __name__=='__main__':unittest.main()
