"""Comfortable graph pages retain the seed without losing any ranked papers."""
import os,sys,tempfile,unittest
from pathlib import Path
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.main import app
from app.db import get_db

class GraphPaging(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.client=TestClient(app)
  with get_db() as c:
   for i in range(1,81):c.execute('INSERT INTO papers(id,title,status,cited_by_count,year) VALUES(?,?,?,?,?)',(i,'Paper '+str(i),'library',i,2000 if i==1 else 2025))
 def query(self,**kwargs):
  r=self.client.post('/api/research/search',json={'seed_ids':[1],'pin_seeds':True,'limit':40,**kwargs});self.assertEqual(r.status_code,200);return r.json()
 def test_low_cited_seed_is_present_on_every_page_with_no_missing_papers(self):
  all_ids=set();first=self.query();self.assertEqual(first['page_count'],3)
  for page in range(first['page_count']):
   data=self.query(offset=page*40);ids=[p['id'] for p in data['papers']]
   self.assertEqual(ids[0],1);self.assertLessEqual(len(ids),40);self.assertEqual(len(ids),len(set(ids)))
   self.assertEqual(data['has_more'],page<2);all_ids.update(ids)
  self.assertEqual(all_ids,set(range(1,81)))
 def test_filtered_out_seed_is_not_forced_back_into_results(self):
  data=self.query(year_min=2020);self.assertEqual(data['anchor_count'],0);self.assertEqual(data['total'],79);self.assertNotIn(1,[p['id'] for p in data['papers']])
 def test_normal_search_retains_original_order_and_page_size(self):
  data=self.query(pin_seeds=False);self.assertEqual(data['anchor_count'],0);self.assertEqual(data['page_count'],2)
  self.assertEqual([p['id'] for p in data['papers']],list(range(80,40,-1)))
 def test_limit_one_still_makes_forward_progress(self):
  data=self.query(limit=1);self.assertEqual(data['anchor_count'],0);self.assertEqual(data['page_count'],80)
  self.assertNotEqual(data['papers'][0]['id'],self.query(limit=1,offset=1)['papers'][0]['id'])
 def test_all_results_exceeds_the_paged_api_limit_without_losing_records(self):
  with get_db() as db:
   for i in range(1000,1620):db.execute('INSERT INTO papers(id,title,status,cited_by_count,year) VALUES(?,?,?,?,?)',(i,'Extra '+str(i),'library',i,2025))
  try:
   data=self.query(all_results=True,limit=40)
   self.assertEqual(data['total'],700);self.assertEqual(data['shown'],700);self.assertEqual(data['limit'],700)
   self.assertFalse(data['has_more']);self.assertEqual(data['page_count'],1)
   self.assertEqual(len({p['id'] for p in data['papers']}),700)
  finally:
   with get_db() as db:db.execute('DELETE FROM papers WHERE id>=1000')

if __name__=='__main__':unittest.main()
