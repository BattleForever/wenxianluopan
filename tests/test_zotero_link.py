"""Product authorization and destination contracts, with no real library writes."""
import os,sys,tempfile,unittest,time,json
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.main import app
from app import zotero_link as z,zotero_sync as sync
from app.db import get_db
from app.store import upsert_paper
from app.zotero_client import ZoteroClient, ZoteroError
from fastapi import HTTPException
class Fake:
 api_key='synthetic-local-only'
 def __init__(self):self.groups=['OLD','USER'];self.updates=[]
 def get_collections(self):return [{'key':'PARENT','data':{'name':'Parent'}},{'key':'OLD','data':{'name':'Old'}},{'key':'NEW','data':{'name':'New','parentCollection':'PARENT'}}]
 def ping(self):return {'ok':True}
 def get_item(self,key):return {'key':key,'data':{'collections':self.groups,'version':1}}
 def update_item(self,key,body,version=None):self.groups=body['collections'];self.updates.append(body)
 def authorize(self,*a,**kw):return self.api_key
class Link(unittest.TestCase):
 def setUp(self):
  self.cli=Fake()
  with get_db() as c:self.pid=upsert_paper(c,{'title':self.id(),'doi':'10.9999/'+self.id()},'test','library')[0]
 def test_collection_paths_preserve_nesting(self):
  rows=z.collection_rows(self.cli);self.assertEqual(next(x for x in rows if x['key']=='NEW')['path'],'Parent / New')
 def test_invalid_collection_fails_without_write(self):
  with patch.object(z,'client',return_value=self.cli),patch.object(sync,'sync_one_paper') as writer:
   with self.assertRaises(HTTPException):z.sync(z.Sync(paper_id=self.pid,collection_key='ABSENT'))
  writer.assert_not_called()
 def test_no_key_provides_actionable_authorization(self):
  self.cli.api_key=None
  with patch.object(z,'client',return_value=self.cli):
   with self.assertRaises(HTTPException) as e:z.sync(z.Sync(paper_id=self.pid,collection_key='NEW'))
  self.assertEqual(e.exception.status_code,401)
 def test_migration_removes_only_prior_managed_membership(self):
  with get_db() as c:c.execute('INSERT INTO zotero_destinations VALUES(?,?,?,?,?)',(self.pid,'OLD','Old','ITEM','before'))
  def writer(c,cli,pid,collection):
   cli.groups.append(collection);c.execute('INSERT INTO zotero_item_map VALUES(?,?,?,?,?,?)',(pid,'item','ITEM',None,1,'test'));return {'zotero_state':'synced'}
  with patch.object(z,'client',return_value=self.cli),patch.object(sync,'sync_one_paper',side_effect=writer):result=z.sync(z.Sync(paper_id=self.pid,collection_key='NEW',move=True))
  self.assertEqual(set(self.cli.groups),{'NEW','USER'});self.assertEqual(result['destination']['path'],'Parent / New')
 def test_failed_sync_does_not_replace_verified_path(self):
  with get_db() as c:c.execute('INSERT INTO zotero_destinations VALUES(?,?,?,?,?)',(self.pid,'OLD','Old','ITEM','before'))
  with patch.object(z,'client',return_value=self.cli),patch.object(sync,'sync_one_paper',return_value={'zotero_state':'pending'}):result=z.sync(z.Sync(paper_id=self.pid,collection_key='NEW',move=True))
  self.assertEqual(result['destination']['collection_key'],'OLD');self.assertFalse(self.cli.updates)
 def test_authorize_response_and_status_never_expose_key(self):
  with patch.object(z,'client',return_value=self.cli):
   job=z.authorize();deadline=time.monotonic()+3
   while z.authorization(job['id'])['state']=='waiting' and time.monotonic()<deadline:time.sleep(.01)
   state=z.authorization(job['id']);self.assertEqual(state['state'],'authorized');self.assertNotIn(self.cli.api_key,json.dumps(state))
  saved=Path(T.name)/'private/zotero.local.json';self.assertEqual(json.loads(saved.read_text())['api_key'],self.cli.api_key)
 def test_failed_authorization_does_not_expose_response_body(self):
  for status in (401,403,409,412,500):
   cli=ZoteroClient(send=lambda *args:(status,{},b'{"key":"synthetic-sensitive-value"}'))
   with self.assertRaises(ZoteroError) as e:cli._request("POST","/api/local/authorize")
   self.assertNotIn("synthetic-sensitive-value",str(e.exception))
 def test_collection_pagination(self):
  calls=[]
  def send(method,url,headers,body,timeout):
   calls.append(url);items=[{'key':str(i),'data':{'name':str(i)}} for i in range(100)] if 'start=0' in url else []
   return 200,{},json.dumps(items).encode()
  self.assertEqual(len(ZoteroClient(send=send).get_collections()),100);self.assertEqual(len(calls),2)

if __name__=='__main__':unittest.main()
