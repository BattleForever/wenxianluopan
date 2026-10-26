import os,sys,tempfile,sqlite3,unittest,json
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name;sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.main import app
from app import pdf_guide as pg,folders
from app.db import get_db,set_setting
from app.store import upsert_paper

class Recovery(unittest.TestCase):
 def setUp(self):
  self.client=TestClient(app,raise_server_exceptions=False);self.client.__enter__()
  with get_db() as c:self.pid,_=upsert_paper(c,{'title':'Fixture '+self.id(),'doi':'10.9999/'+self.id().split('.')[-1]},'fixture','candidate')
 def tearDown(self):self.client.__exit__(None,None,None)
 def test_cleanup_failure_does_not_erase_found_pdf_or_modify_zotero(self):
  root=Path(T.name)/self._testMethodName;root.mkdir();db=root/'zotero.sqlite';key='FIXTURE1';folder=root/'storage'/key;folder.mkdir(parents=True);pdf=folder/'paper.pdf';pdf.write_bytes(b'%PDF-1.4 test fallback only')
  with sqlite3.connect(db) as c:
   c.executescript('CREATE TABLE fields(fieldID INTEGER,fieldName TEXT);CREATE TABLE itemData(itemID INTEGER,fieldID INTEGER,valueID INTEGER);CREATE TABLE itemDataValues(valueID INTEGER,value TEXT);CREATE TABLE items(itemID INTEGER,key TEXT);CREATE TABLE itemAttachments(parentItemID INTEGER,itemID INTEGER,path TEXT);')
   c.execute('INSERT INTO fields VALUES(1,?)',('DOI',));c.execute('INSERT INTO itemData VALUES(1,1,1)');c.execute('INSERT INTO itemDataValues VALUES(1,?)',(pg._paper(self.pid)['doi'],));c.execute('INSERT INTO items VALUES(2,?)',(key,));c.execute('INSERT INTO itemAttachments VALUES(1,2,?)',('storage:paper.pdf',))
  before=db.read_bytes();set_setting('zotero_data_dir',str(root))
  with patch('app.zotero_attachments.find_native',return_value=None),patch.object(pg.shutil,'rmtree',side_effect=PermissionError('fixture antivirus lock')):
   self.assertEqual(pg._find_pdf(pg._paper(self.pid)),pdf)
  self.assertEqual(before,db.read_bytes())
 def test_reading_location_is_independent_of_library_status_and_folders(self):
  self.client.post('/api/organizer/items',json={'ids':[self.pid],'folder_ids':[],'scope':'reading','action':'replace'}).raise_for_status()
  reading=self.client.get('/api/organizer/locations/'+str(self.pid)+'?scope=reading').json();library=self.client.get('/api/organizer/locations/'+str(self.pid)).json()
  self.assertTrue(reading['collected']);self.assertFalse(library['collected'])
  a=self.client.post('/api/organizer/folders',json={'scope':'reading','name':'Fixture Reading','color':'#f0b765'}).json()['folder']['id']
  self.client.post('/api/organizer/items',json={'ids':[self.pid],'folder_ids':[a],'scope':'reading','action':'replace'}).raise_for_status()
  self.assertEqual(self.client.get('/api/organizer/locations/'+str(self.pid)+'?scope=reading').json()['paths'],['Fixture Reading']);self.assertEqual(self.client.get('/api/organizer/locations/'+str(self.pid)).json()['folder_ids'],[])
 def test_unexpected_server_error_returns_safe_json_and_private_reference(self):
  def failed():raise PermissionError('DO_NOT_EXPOSE_EXCEPTION_CONTENT')
  from fastapi.routing import APIRoute
  route=APIRoute('/api/fixture-failure',failed,methods=['GET']);app.router.routes.insert(0,route)
  try:
   r=self.client.get('/api/fixture-failure');self.assertEqual(r.status_code,500);d=r.json();self.assertIn('error_id',d);self.assertNotIn('DO_NOT_EXPOSE',r.text)
   log=Path(T.name)/'private'/'errors.jsonl';rows=[json.loads(x) for x in log.read_text(encoding='utf-8').splitlines()];self.assertEqual(rows[-1]['id'],d['error_id']);self.assertNotIn('DO_NOT_EXPOSE',log.read_text(encoding='utf-8'))
  finally:app.router.routes.remove(route)

if __name__=='__main__':unittest.main(verbosity=2)
