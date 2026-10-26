import os,sys,tempfile,unittest
from pathlib import Path
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name;sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.recommendation import rank
class Essential(unittest.TestCase):
 def test_beginner_view_covers_both_directions_without_removing_candidates(self):
  papers=[{'id':i,'title':'Ultrasonic gas flow measurement method '+str(i),'abstract':'Phased array ultrasound gas flow meter experiment','year':2020+i%6} for i in range(1,41)]
  edges=[{'from':1,'to':i} for i in range(2,22)]+[{'from':i,'to':1} for i in range(22,41)]
  result,_=rank(papers,edges,[1],year=2026)
  selected=[i for i,r in result.items() if 'essential' in r['roles']]
  self.assertTrue(selected);self.assertTrue(set(selected)&set(range(2,22)));self.assertTrue(set(selected)&set(range(22,41)));self.assertLess(len(selected),len(papers)-1);self.assertEqual(len(result),40)
 def test_common_foundation_is_not_lost_in_representative_selection(self):
  papers=[{'id':i,'title':'Research '+str(i),'year':2020} for i in range(1,10)]
  result,_=rank(papers,[{'from':1,'to':8},{'from':2,'to':8}],[1,2],year=2026)
  self.assertIn('essential',result[8]['roles'])
if __name__=='__main__':unittest.main(verbosity=2)
