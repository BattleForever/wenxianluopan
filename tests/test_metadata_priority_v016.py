"""Missing fields: local PDF, Zotero, online; existing content is preserved."""
import os,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db,set_setting
from app.store import upsert_paper
from app import metadata_jobs as m,metadata_sources as sources,workspace
init_db();m.init();workspace.init()
class Priority(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0');set_setting('zotero_enabled','0');set_setting('zotero_data_dir',str(Path(T.name)/'no-zotero'))
  with get_db() as c:self.pid=upsert_paper(c,{'title':self.id(),'doi':'10.9999/'+self.id().split('.')[-1],'authors':['Local author'],'year':2025,'venue':'Local journal','type':'journal-article','oa_pdf_url':'https://example.org/paper.pdf'},'fixture','library')[0]
 def opts(self):return {'allow_network':True,'allow_ai':False,'include_metrics':False}
 def test_zotero_precedes_network_without_enable_switch(self):
  calls=[]
  def zotero(p):calls.append('zotero');return {'abstract':'Synthetic original abstract.','_source':'Zotero','_origin':'zotero','_url':None}
  with patch.object(m,'local_pdf_abstract',return_value=None),patch.object(sources,'zotero_biblio',side_effect=zotero),patch.object(sources,'crossref',side_effect=AssertionError('unnecessary network')),patch.object(sources,'openalex',side_effect=AssertionError('unnecessary network')):
   m.complete_one(self.pid,self.opts(),threading.Event(),{})
  self.assertEqual(calls,['zotero']);self.assertEqual(m.paper(self.pid)['abstract'],'Synthetic original abstract.')
 def test_local_pdf_precedes_zotero(self):
  with patch.object(m,'local_pdf_abstract',return_value={'abstract':'Local PDF abstract','_source':'PDF 正文摘要','_origin':'local_pdf','_url':None}),patch.object(sources,'zotero_biblio',side_effect=AssertionError('unnecessary Zotero')) as z,patch.object(sources,'crossref',side_effect=AssertionError('network')) as net:
   m.complete_one(self.pid,self.opts(),threading.Event(),{})
  z.assert_not_called();net.assert_not_called()
 def test_unavailable_zotero_falls_through_to_network(self):
  calls=[]
  def zotero(p):calls.append('zotero');raise RuntimeError('not running')
  def network(doi):calls.append('network');return {'abstract':'Network abstract','_source':'Crossref','_url':None}
  with patch.object(m,'local_pdf_abstract',return_value=None),patch.object(sources,'zotero_biblio',side_effect=zotero),patch.object(sources,'crossref',side_effect=network):m.complete_one(self.pid,self.opts(),threading.Event(),{})
  self.assertEqual(calls,['zotero','network'])
 def test_zotero_attachment_abstract_precedes_network(self):
  def extract(p):return {'abstract':'Attachment abstract','_source':'PDF','_url':None} if p.get('local_pdf_path') else None
  with patch.object(m,'local_pdf_abstract',side_effect=extract),patch.object(sources,'zotero_biblio',return_value=None),patch.object(sources,'zotero_pdf_paths',return_value=['synthetic-attachment.pdf']),patch.object(sources,'crossref',side_effect=AssertionError('unnecessary network')) as net:
   m.complete_one(self.pid,self.opts(),threading.Event(),{})
  self.assertEqual(m.paper(self.pid)['abstract'],'Attachment abstract');net.assert_not_called()
if __name__=='__main__':unittest.main()
