"""Identity-safe import locations and exact DOI search through the local API."""
import os,sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db
from app import folders,import_jobs as jobs,importer,trash
from app.store import upsert_paper
from app.import_locations import locations
init_db();folders.init();trash.init();jobs.init()
class Locations(unittest.TestCase):
 def setUp(self):
  with get_db() as c:
   c.execute('DELETE FROM paper_trash');c.execute('DELETE FROM organizer_items');c.execute('DELETE FROM organizer_folders');c.execute('DELETE FROM papers')
 def paper(self,doi='10.1000/local',key='ITEM0001',status='library',title='Local fixture'):
  with get_db() as c:return upsert_paper(c,{'doi':doi,'title':title,'meta':{'zotero_key':key}},'fixture',status)[0]
 def test_existing_import_exposes_real_hierarchical_locations(self):
  pid=self.paper()
  with get_db() as c:
   stamp=folders.now();a=c.execute("INSERT INTO organizer_folders(scope,name,name_key,created_at,updated_at) VALUES('library','Research','research',?,?)",(stamp,stamp)).lastrowid
   b=c.execute("INSERT INTO organizer_folders(scope,parent_id,name,name_key,created_at,updated_at) VALUES('library',?,'PMUT','pmut',?,?)",(a,stamp,stamp)).lastrowid;c.execute('INSERT INTO organizer_items VALUES(?,?)',(b,pid))
  d=locations([{'key':'ITEM0001','doi':'https://doi.org/10.1000/LOCAL'}])['ITEM0001'];self.assertTrue(d['collected']);self.assertEqual(d['paper_id'],pid);self.assertEqual(d['paths'],['Research / PMUT'])
 def test_key_doi_conflict_is_not_labeled_as_imported(self):
  self.paper();d=locations([{'key':'ITEM0001','doi':'10.1000/different'}])['ITEM0001'];self.assertTrue(d['conflict']);self.assertFalse(d['collected']);self.assertIsNone(d['paper_id'])
 def test_different_known_doi_with_same_title_is_not_same_paper(self):
  self.paper();d=locations([{'key':'OTHER001','doi':'10.1000/another','title':'Local fixture'}])['OTHER001'];self.assertFalse(d['collected']);self.assertIsNone(d['paper_id'])
 def test_candidates_do_not_claim_library_membership(self):
  pid=self.paper(status='candidate');d=locations([{'key':'ITEM0001'}])['ITEM0001'];self.assertEqual(d['paper_id'],pid);self.assertFalse(d['collected'])
 def test_synced_item_map_without_meta_key_is_recognized(self):
  pid=self.paper(key='')
  with get_db() as c:
   c.execute('CREATE TABLE IF NOT EXISTS zotero_item_map(paper_id INTEGER,kind TEXT,zotero_key TEXT)');c.execute('INSERT INTO zotero_item_map VALUES(?,?,?)',(pid,'item','SYNC0001'))
  self.assertEqual(locations([{'key':'SYNC0001'}])['SYNC0001']['paper_id'],pid)
 def test_all_supported_import_results_include_local_ids(self):
  for fn,text in [(importer.import_bibtex,'@article{x,title={Imported Bib fixture}}'),(importer.import_ris,'TY  - JOUR\nTI  - Imported RIS fixture\nER  -'),(importer.import_standard,'{"title":"Imported standard fixture"}')]:
   r=fn(text);self.assertEqual(len(r['paper_ids']),1)
   with get_db() as c:self.assertTrue(c.execute('SELECT 1 FROM papers WHERE id=?',(r['paper_ids'][0],)).fetchone())
 def test_doi_search_matches_doi_not_fulltext_mentions_and_paginates_after_filter(self):
  calls=[]
  class Cli:
   def _request(self,method,path):
    if path=='/api/itemTypes':return 200,{},[{'itemType':'journalArticle'},{'itemType':'attachment'}]
    q=parse_qs(urlparse(path).query);calls.append(q);start=int(q['start'][0]);n=100 if start==0 else 1
    return 200,{'Total-Results':'101'},[{'key':f'ITEM{i+start:04}','data':{'itemType':'journalArticle','DOI':'10.1000/exact' if i==0 else '10.1000/other','title':'Fixture'}} for i in range(n)]
  with patch.object(jobs,'ZoteroClient',Cli):rows,headers=jobs.list_items(start=1,limit=10,query='https://doi.org/10.1000/EXACT')
  self.assertEqual(len(rows),1);self.assertEqual(rows[0]['key'],'ITEM0100');self.assertEqual(headers['Total-Results'],'2');self.assertEqual(len(calls),2);self.assertTrue(all(q['qmode']==['everything'] and q['q']==['10.1000/exact'] for q in calls))

if __name__=='__main__':unittest.main()
