"""Transfers preserve reading work; unsafe inputs and credential exports are rejected."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.main import app
from app import workspace_data as w
import launcher


class WorkspaceData(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.data=self.root/'data';self.data.mkdir();self.db=self.data/'papernet.db'
  self.client=TestClient(app);self.headers={'X-Workspace-Token':w.TOKEN}
  self.patches=[patch.object(w,'DATA_DIR',self.data),patch.object(w,'DB_PATH',self.db)]
  for p in self.patches:p.start();self.addCleanup(p.stop)
  with w.database(self.db) as c:
   c.executescript('''CREATE TABLE papers(id INTEGER PRIMARY KEY,local_pdf_path TEXT,title TEXT);
    CREATE TABLE citations(from_id INTEGER,to_id INTEGER);CREATE TABLE settings(key TEXT,value TEXT);
    CREATE TABLE reading_list(paper_id INTEGER,note TEXT);CREATE TABLE folders(id INTEGER,name TEXT);
    CREATE TABLE fetch_cache(key TEXT,body TEXT);CREATE TABLE research_jobs(id TEXT,payload TEXT);''')
   pdf=self.root/'external.pdf';pdf.write_bytes(b'%PDF-test-file')
   c.execute('INSERT INTO papers VALUES(1,?,?)',(str(pdf),'A paper'))
   c.execute('INSERT INTO papers VALUES(2,?,?)',(str(self.root/'missing.pdf'),'Missing'))
   c.execute("INSERT INTO reading_list VALUES(1,'keep this reading note')")
   c.execute("INSERT INTO folders VALUES(1,'My research topic')")
   c.execute("INSERT INTO settings VALUES('api_key','sentinel-secret')")
   c.execute("INSERT INTO fetch_cache VALUES('request','sentinel-secret')")
  (self.data/'private/pdf_sources').mkdir(parents=True)
  (self.data/'private/pdf_sources/source.pdf').write_bytes(b'%PDF-snapshot')
  (self.data/'private/ai_config.local.json').write_text('{"api_key":"sentinel-secret"}')
  (self.data/'private/relocation.local.json').write_text('{}')
  app.state.workspace_switching=False;app.state.workspace_requests=0
  if hasattr(app.state,'stop_server'):delattr(app.state,'stop_server')
 def pack(self,folder):
  f=io.BytesIO()
  with zipfile.ZipFile(f,'w',zipfile.ZIP_DEFLATED) as z:
   for p in folder.rglob('*'):
    if p.is_file():z.write(p,p.relative_to(folder).as_posix())
  f.seek(0);return f
 def test_portable_snapshot_keeps_library_notes_originals_and_sources(self):
  target=self.root/'snapshot';info=w.snapshot(target)
  self.assertEqual(info['papers'],2);self.assertEqual(info['linked_pdfs'],1);self.assertEqual(info['missing_pdf_ids'],[2])
  with w.database(target/'papernet.db') as db:
   value=db.execute('SELECT local_pdf_path FROM papers WHERE id=1').fetchone()[0]
   self.assertTrue((target/value).is_file());self.assertFalse(Path(value).is_absolute())
   self.assertEqual(db.execute('SELECT note FROM reading_list').fetchone()[0],'keep this reading note')
   self.assertEqual(db.execute('SELECT name FROM folders').fetchone()[0],'My research topic')
   self.assertEqual(db.execute('SELECT count(*) FROM fetch_cache').fetchone()[0],0)
   self.assertEqual(db.execute('SELECT count(*) FROM settings').fetchone()[0],0)
  self.assertFalse((target/'private/ai_config.local.json').exists());self.assertTrue((target/'private/pdf_sources/source.pdf').is_file())
  self.assertTrue((self.root/'external.pdf').is_file())
 def test_local_migration_keeps_connections_but_not_launch_pointer(self):
  target=self.root/'snapshot';w.snapshot(target,portable=False)
  self.assertTrue((target/'private/ai_config.local.json').exists());self.assertFalse((target/'private/relocation.local.json').exists())
 def test_archive_roundtrip_relocates_pdf_on_another_computer(self):
  target=self.root/'snapshot';w.snapshot(target);dest=self.root/'other-computer';dest.mkdir()
  self.assertEqual(w.unpack(self.pack(target),dest)['papers'],2);w.relocate_paths(dest)
  with w.database(dest/'papernet.db') as c:
   pdf=Path(c.execute('SELECT local_pdf_path FROM papers WHERE id=1').fetchone()[0]);self.assertTrue(pdf.is_relative_to(dest));self.assertTrue(pdf.is_file())
 def test_tampered_archive_rejected(self):
  folder=self.root/'snapshot';w.snapshot(folder);(folder/'pdfs'/next((folder/'pdfs').iterdir()).name).write_bytes(b'changed')
  with self.assertRaises(ValueError):w.unpack(self.pack(folder),self.root/'out')
 def test_zip_traversal_and_symlink_rejected(self):
  for name in ('../outside','/absolute','C:/outside','private\\outside'):
   f=io.BytesIO()
   with zipfile.ZipFile(f,'w') as z:z.writestr(name,'bad')
   f.seek(0)
   with self.assertRaises(ValueError):w.unpack(f,self.root/'out')
  f=io.BytesIO()
  with zipfile.ZipFile(f,'w') as z:
   e=zipfile.ZipInfo('symlink');e.external_attr=0o120777<<16;z.writestr(e,'target')
  f.seek(0)
  with self.assertRaises(ValueError):w.unpack(f,self.root/'out')
 def test_nonempty_and_overlapping_destinations_rejected(self):
  for path in (self.data,self.data/'inside',self.root):
   with self.assertRaises(ValueError):w.target_directory(str(path))
  with self.assertRaises(ValueError):w.target_directory('relative-directory')
 def test_mutations_require_own_origin_and_token(self):
  for headers in ({},{**self.headers,'Origin':'https://evil.invalid'}):
   self.assertEqual(self.client.post('/api/workspace/open-folder',headers=headers).status_code,403)
  self.assertEqual(self.client.get('/api/workspace/state',headers={'host':'evil.invalid'}).status_code,403)
 def test_export_download_is_zip_and_has_no_connection_credentials(self):
  r=self.client.post('/api/workspace/backup',headers=self.headers);self.assertEqual(r.status_code,200)
  with zipfile.ZipFile(io.BytesIO(r.content)) as z:
   self.assertIn('workspace.json',z.namelist());self.assertNotIn('private/ai_config.local.json',z.namelist())
   self.assertNotIn(b'sentinel-secret',z.read('papernet.db'))
  self.assertEqual(r.headers['X-Missing-PDFs'],'1')
 def test_running_jobs_block_transfer_and_exit(self):
  with w.database(self.db) as c:c.execute('INSERT INTO research_jobs VALUES(?,?)',('active',json.dumps({'status':'running'})))
  with patch.object(app.state,'stop_server',lambda:None,create=True),patch.object(w.threading,'Timer') as timer:
   self.assertEqual(self.client.post('/api/workspace/exit',headers=self.headers).status_code,409)
   self.assertEqual(self.client.post('/api/workspace/backup',headers=self.headers).status_code,409)
   timer.assert_not_called()
 def test_exit_only_uses_managed_service_callback(self):
  self.assertEqual(self.client.post('/api/workspace/exit',headers=self.headers).status_code,409)
  with patch.object(app.state,'stop_server',lambda:None,create=True),patch.object(w.threading,'Timer') as timer:
   self.assertEqual(self.client.post('/api/workspace/exit',headers=self.headers).status_code,200);timer.assert_called_once()
 def test_migration_copies_not_overwrites_and_next_launch_follows_pointer(self):
  (self.data/'private/relocation.local.json').unlink();dest=self.root/'migrated'
  with patch.object(app.state,'stop_server',lambda:None,create=True),patch.object(w.subprocess,'Popen'),patch.object(w.threading,'Timer'):
   r=self.client.post('/api/workspace/migrate',json={'path':str(dest)},headers=self.headers)
  self.assertEqual(r.status_code,200,r.text);self.assertTrue(self.db.exists());self.assertTrue((dest/'papernet.db').exists())
  self.assertEqual(launcher.resolve_workspace(self.data),dest)
  self.assertTrue((dest/'private/ai_config.local.json').exists())
 def test_failed_restart_does_not_switch_next_launch(self):
  (self.data/'private/relocation.local.json').unlink()
  with patch.object(app.state,'stop_server',lambda:None,create=True),patch.object(w.subprocess,'Popen',side_effect=OSError('test failure')):
   r=self.client.post('/api/workspace/migrate',json={'path':str(self.root/'dest')},headers=self.headers)
  self.assertEqual(r.status_code,400);self.assertEqual(launcher.resolve_workspace(self.data),self.data)
  self.assertFalse(app.state.workspace_switching)
 def test_import_does_not_modify_current_database(self):
  (self.data/'private/relocation.local.json').unlink();folder=self.root/'snapshot';w.snapshot(folder);archive=self.pack(folder).getvalue()
  before=hashlib.sha256(self.db.read_bytes()).hexdigest();dest=self.root/'imported'
  with patch.object(app.state,'stop_server',lambda:None,create=True),patch.object(w.subprocess,'Popen'),patch.object(w.threading,'Timer'):
   r=self.client.post('/api/workspace/restore',params={'path':str(dest)},content=archive,headers=self.headers)
  self.assertEqual(r.status_code,200,r.text);self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())
  self.assertFalse((dest/'private/ai_config.local.json').exists())
 def test_missing_or_circular_migration_does_not_create_empty_library(self):
  with self.assertRaises(RuntimeError):launcher.resolve_workspace(self.data)
  p=self.data/'private/relocation.local.json';p.write_text(json.dumps({'target':str(self.data),'data_id':w.data_identity(self.data)}))
  with self.assertRaises(RuntimeError):launcher.resolve_workspace(self.data)

if __name__=='__main__':unittest.main()
