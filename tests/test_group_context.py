"""Group counts stay accurate while the graph retains seed endpoints as context."""
import os,sys,tempfile,unittest
from pathlib import Path
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.main import app
from app.db import get_db
class GroupContext(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.client=TestClient(app)
  with get_db() as c:
   for i,y in [(1,2019),(2,2025),(3,2026),(4,2018),(5,2024)]:c.execute('INSERT INTO papers(id,title,status,cited_by_count,year,abstract) VALUES(?,?,?,?,?,?)',(i,'Ultrasonic sensor '+str(i),'library',i,y,'Research in ultrasonic sensing.'))
   for a,b in [(2,1),(3,1),(1,4),(5,4)]:c.execute('INSERT INTO citations(from_id,to_id,src) VALUES(?,?,?)',(a,b,'fixture'))
 def query(self,**kw):
  r=self.client.post('/api/research/search',json={'seed_ids':[1],'pin_seeds':True,'sort':'recommended','recommendation_group':'citing','limit':1,**kw});self.assertEqual(r.status_code,200);return r.json()
 def test_seed_context_connects_every_citing_page_without_inflating_counts(self):
  seen=set()
  for offset in [0,1]:
   d=self.query(offset=offset);self.assertEqual(d['total'],2);self.assertEqual(d['shown'],1);self.assertEqual(d['context_count'],1);self.assertEqual(d['page_count'],2)
   self.assertEqual([p['id'] for p in d['context_papers']],[1]);self.assertTrue(d['context_papers'][0]['is_context']);self.assertEqual(len(d['edges']),1)
   p=d['papers'][0];seen.add(p['id']);self.assertEqual(d['edges'][0]['from'],p['id']);self.assertEqual(d['edges'][0]['to'],1)
   self.assertIn(1,p['recommendation']['evidence']['direct_citing_seeds'])
  self.assertEqual(seen,{2,3})
 def test_filter_excluded_seed_is_explicit_context_not_a_match(self):
  d=self.query(year_min=2025,all_results=True);self.assertEqual(d['total'],2);self.assertEqual({p['id'] for p in d['papers']},{2,3});self.assertEqual(d['context_papers'][0]['id'],1);self.assertEqual(len(d['edges']),2)
 def test_empty_group_does_not_show_a_lonely_seed(self):
  d=self.query(query='no-such-paper-xyz',query_mode='filter');self.assertEqual(d['total'],0);self.assertEqual(d['context_papers'],[]);self.assertEqual(d['edges'],[])
 def test_context_opt_out_and_regular_search_unchanged(self):
  self.assertEqual(self.query(pin_seeds=False)['context_papers'],[])
  d=self.query(recommendation_group='all',limit=5);self.assertEqual(d['total'],5);self.assertEqual(d['context_papers'],[])
 def test_seed_is_always_context_not_a_group_match(self):
  d=self.query(recommendation_group='recent',all_results=True);self.assertNotIn(1,[p['id'] for p in d['papers']]);self.assertEqual(d['context_count'],1)
 def test_non_direct_related_work_is_not_mislabelled_as_citing(self):
  d=self.query(all_results=True);self.assertNotIn(5,[p['id'] for p in d['papers']])
if __name__=='__main__':unittest.main()
