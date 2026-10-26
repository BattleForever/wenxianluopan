"""Fresh citation caches remain fast; old caches refresh online and work offline."""
import os,sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch,Mock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name;os.environ.pop('OPENALEX_API_KEY',None)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db,set_setting
from app import netclient as n
init_db()
class Freshness(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0');self.url='https://api.openalex.org/works/W123'
  with get_db() as c:
   c.execute('DELETE FROM fetch_cache');c.execute('INSERT INTO fetch_cache(key,url,status,body,fetched_at) VALUES(?,?,?,?,?)',(n._cache_key(self.url,{}),self.url,200,'{"old":true}','2000-01-01 00:00:00'))
 def test_expired_online_refresh(self):
  r=Mock(status_code=200,text='{"new":true}')
  with patch.object(n._SESSION,'get',return_value=r) as call:self.assertEqual(n.cached_get_json(self.url,max_age=3600),{'new':True})
  self.assertEqual(call.call_count,1)
 def test_expired_offline_preserves_cache(self):
  set_setting('offline_mode','1')
  with patch.object(n._SESSION,'get',side_effect=AssertionError('network')):self.assertEqual(n.cached_get_json(self.url,max_age=3600),{'old':True})
 def test_unlimited_citation_cursor_fetches_all_pages(self):
  pages=[{'results':[{'id':'W1'}],'meta':{'next_cursor':'next'}},{'results':[{'id':'W2'}],'meta':{'next_cursor':None}}]
  with patch.object(n,'cached_get_json',side_effect=pages) as call:items=list(n.oa_cited_by('Wseed',None))
  self.assertEqual([x['id'] for x in items],['W1','W2']);self.assertEqual(call.call_count,2)
if __name__=='__main__':unittest.main()
