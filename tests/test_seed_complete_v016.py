"""Seed neighborhoods stay intact before bounded follow-up expansion."""
import tempfile,os,sys,threading,unittest
from pathlib import Path
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db
from app.store import upsert_paper,add_citation
from app import exploration
init_db()
class SeedComplete(unittest.TestCase):
 def setUp(self):
  with get_db() as c:
   c.execute('DELETE FROM citations');c.execute('DELETE FROM papers')
   self.ids=[upsert_paper(c,{'title':'synthetic seed '+str(i)},'fixture','library')[0] for i in range(15)]
   for n in self.ids[1:]:add_citation(c,self.ids[0],n,'fixture')
 def test_all_seed_neighbors_override_followup_cap(self):
  r=exploration.walk([self.ids[0]],depth=1,per_direction=2,max_nodes=10,complete_seed=True)
  self.assertEqual(len(r['results']),15);self.assertFalse(r['truncated'])
 def test_online_seed_fetch_receives_unlimited_cap(self):
  seen=[]
  def fetch(pid,direction,cap,stop):seen.append(cap);return 0
  r=exploration.walk([self.ids[0]],depth=1,per_direction=2,complete_seed=True,online=True,fetcher=fetch)
  self.assertEqual(seen,[None]);self.assertEqual(len(r['results']),15)
 def test_exhausted_network_budget_still_reads_known_edges(self):
  with get_db() as c:
   extra=upsert_paper(c,{'title':'extra second level'},'fixture','library')[0]
   add_citation(c,self.ids[1],extra,'fixture')
  r=exploration.walk([self.ids[0]],depth=2,per_direction=None,max_nodes=100,max_fetch=1,complete_seed=True,online=True,fetcher=lambda *a:0)
  self.assertIn(extra,[p['id'] for p in r['results']]);self.assertTrue(r['truncated'])
if __name__=='__main__':unittest.main()
