"""Readable on-disk projects preserve originals, edits and portable user documents."""
import contextlib,hashlib,io,json,os,sqlite3,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.main import app
from app import project_files as p,workspace_data as w,db
from app import background

class Projects(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.data=self.root/'data';self.data.mkdir();self.path=self.data/'papernet.db'
  with contextlib.closing(sqlite3.connect(Path(T.name)/'papernet.db')) as source,contextlib.closing(sqlite3.connect(self.path)) as target:source.backup(target)
  for change in (patch.object(db,'DB_PATH',self.path),patch.object(p,'DATA_DIR',self.data),patch.object(w,'DATA_DIR',self.data),patch.object(w,'DB_PATH',self.path)):
   change.start();self.addCleanup(change.stop)
  self.pdf=self.root/'original.pdf';self.pdf.write_bytes(b'%PDF-original-untouched')
  with db.get_db() as c:
   c.execute("INSERT INTO papers(id,title,status,local_pdf_path,year) VALUES(1,'Readable title','library',?,2025)",(str(self.pdf),))
   c.execute("INSERT INTO papers(id,title,status,year) VALUES(2,'Other paper','candidate',2024)")
   c.execute("INSERT INTO reading_list(paper_id,note) VALUES(1,'Keep my reading note')")
   c.execute("INSERT INTO organizer_folders(id,scope,name,name_key,created_at,updated_at) VALUES(1,'library','Flowmeters','flowmeters','2026-01-01','2026-01-01')")
   c.execute("INSERT INTO organizer_folders(id,scope,parent_id,name,name_key,created_at,updated_at) VALUES(2,'library',1,'Methods','methods','2026-01-01','2026-01-01')")
   c.execute('INSERT INTO organizer_items VALUES(2,1)')
  app.state.workspace_switching=False;app.state.workspace_requests=0;self.client=TestClient(app);self.headers={'X-Workspace-Token':w.TOKEN}
 def test_readable_classification_tree_and_reading_notes(self):
  stats=p.refresh();self.assertEqual(stats['papers'],2);self.assertEqual(stats['reading'],1)
  path=p.directory('papers')/'文献库'/'Flowmeters [1]'/'Methods [2]'/'Readable title [1].html'
  self.assertTrue(path.is_file());self.assertIn('Keep my reading note',path.read_text(encoding='utf-8'))
  self.assertTrue((p.directory('papers')/'阅读清单'/'未分类'/'Readable title [1].html').is_file())
  self.assertTrue((p.directory('papers')/'待筛选文献'/'未分类'/'Other paper [2].html').is_file())
 def test_pdf_shortcut_is_readable_and_original_unchanged(self):
  before=hashlib.sha256(self.pdf.read_bytes()).hexdigest();p.refresh()
  link=p.directory('originals')/'Readable title [1].url';self.assertIn(self.pdf.as_uri(),link.read_text(encoding='utf-8'))
  self.assertTrue(self.pdf.is_file());self.assertEqual(before,hashlib.sha256(self.pdf.read_bytes()).hexdigest())
 def test_repeated_refresh_does_not_create_false_user_edits(self):
  p.refresh();stats=p.refresh();self.assertEqual(stats['preserved_edits'],0)
 def test_edited_generated_page_is_preserved_before_refresh(self):
  p.refresh();path=next((p.directory('papers')/'文献库').rglob('*.html'));path.write_text('MY PERSONAL EDIT',encoding='utf-8')
  stats=p.refresh();self.assertEqual(stats['preserved_edits'],1)
  saved=list((p.directory('notes')/'保留的目录修改').glob('*.html'));self.assertEqual(saved[0].read_text(),'MY PERSONAL EDIT')
 def test_folder_rename_refreshes_indexes_without_removing_user_files(self):
  p.refresh();old=p.directory('papers')/'文献库'/'Flowmeters [1]'/'Methods [2]';(old/'user_notes.txt').write_text('mine')
  with db.get_db() as c:c.execute("UPDATE organizer_folders SET name='Experiments' WHERE id=2")
  p.refresh();self.assertTrue((old/'user_notes.txt').is_file());self.assertFalse((old/'Readable title [1].html').exists())
  self.assertTrue((old.parent/'Experiments [2]'/'Readable title [1].html').is_file())
 def test_html_content_is_escaped(self):
  with db.get_db() as c:c.execute("UPDATE papers SET title=?,abstract=? WHERE id=1",('<script>alert(1)</script>','<img src=x onerror=alert(1)>'))
  p.refresh();text=''.join(f.read_text(encoding='utf-8') for f in p.directory('papers').rglob('*.html'))
  self.assertNotIn('<script>alert(1)',text);self.assertIn('&lt;script&gt;',text);self.assertNotIn('<img src=x',text)
 def test_project_indexes_do_not_copy_private_connection_settings(self):
  (self.data/'private').mkdir();(self.data/'private/ai_config.local.json').write_text('{"api_key":"PRIVATE-CONNECTION-SENTINEL"}')
  p.refresh();text=b''.join(f.read_bytes() for f in p.root().rglob('*') if f.is_file());self.assertNotIn(b'PRIVATE-CONNECTION-SENTINEL',text)
 def test_exports_saved_in_the_named_export_directory(self):
  r=self.client.post('/api/research/export',json={'ids':[1],'format':'ris'});self.assertEqual(r.status_code,200)
  files=list(p.directory('exports').glob('*.ris'));self.assertEqual(len(files),1);self.assertEqual(files[0].read_bytes(),r.content)
 def test_backup_is_saved_locally_and_user_documents_are_portable(self):
  p.ensure_tree();(p.directory('notes')/'my_notes.md').write_text('my research notes');p.save_export('reading','csv','title\nPaper')
  r=self.client.post('/api/workspace/backup',headers=self.headers);self.assertEqual(r.status_code,200,r.text)
  self.assertEqual(len(list(p.directory('backups').glob('*.zip'))),1)
  with zipfile.ZipFile(io.BytesIO(r.content)) as z:
   self.assertIn('项目文件/个人资料/my_notes.md',z.namelist());self.assertTrue(any(n.startswith('项目文件/导出/') for n in z.namelist()))
   self.assertFalse(any('项目文件/备份/' in n for n in z.namelist()));self.assertFalse(any(n.startswith('项目文件/文献目录/') for n in z.namelist()))
  dest=self.root/'restored';dest.mkdir();w.unpack(io.BytesIO(r.content),dest);w.relocate_paths(dest)
  self.assertEqual((dest/'项目文件/个人资料/my_notes.md').read_text(),'my research notes')
 def test_open_folder_uses_selected_named_location(self):
  with patch.object(os,'startfile',create=True) as open_file:
   r=self.client.post('/api/workspace/open-folder',json={'kind':'exports'},headers=self.headers)
  self.assertEqual(r.status_code,200);self.assertEqual(Path(open_file.call_args.args[0]),p.directory('exports'))
 def test_unsupported_folder_name_cannot_open_arbitrary_paths(self):
  r=self.client.post('/api/workspace/open-folder',json={'kind':'../../other'},headers=self.headers);self.assertEqual(r.status_code,422)
 def test_background_does_not_fall_back_to_an_owned_process(self):
  error=PermissionError('restricted');error.winerror=5
  with patch.object(background.subprocess,'Popen',side_effect=error) as spawn:
   with self.assertRaises(RuntimeError):background.spawn(['dummy'])
  self.assertEqual(spawn.call_count,1)
 def test_windows_background_flags_detach_from_owner_job(self):
  if os.name!='nt':self.skipTest('Windows lifecycle')
  self.assertTrue(background.options()['creationflags'] & 0x01000000)

if __name__=='__main__':unittest.main()
