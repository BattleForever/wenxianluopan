import os,sys,tempfile,unittest,threading
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name;sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.main import app
from app import metadata_jobs as m,folders,workspace,recommendation
from app.db import get_db,set_setting
from app.store import upsert_paper

class Workflow(unittest.TestCase):
 def setUp(self):
  self.client=TestClient(app);self.client.__enter__();set_setting('offline_mode','0')
  with get_db() as c:self.pid,_=upsert_paper(c,{'title':'Fixture '+self.id(),'doi':'10.0000/'+self.id().split('.')[-1]},'fixture','candidate')
 def tearDown(self):self.client.__exit__(None,None,None)
 def test_plan_reports_actual_missing_and_dependencies_without_network(self):
  with patch.object(m.sources,'crossref',side_effect=AssertionError('plan must not fetch')):
   r=self.client.post('/api/research/enrichment/plan',json={'ids':[self.pid]})
  self.assertEqual(r.status_code,200);data=r.json();by={f['field']:f for f in data['fields']}
  self.assertNotIn('title',by);self.assertIn('abstract',by);self.assertIn('abstract',by['abstract_zh']['requires']);self.assertEqual(data['papers'][0]['paper_id'],self.pid)
 def test_selected_field_does_not_write_other_returned_fields(self):
  opts={'fields':['authors'],'allow_network':True,'allow_ai':False,'include_metrics':False,'recover_abstract_from_pdf':False}
  supplied={'authors':['Fixture Author'],'abstract':'Unselected original abstract','year':2024,'_source':'fixture','meta':{'unselected':'not retained'}}
  with patch.object(m.sources,'zotero_biblio',return_value=supplied),patch.object(m.sources,'zotero_pdf_paths',return_value=[]),patch.object(m.publisher_metadata,'lookup',side_effect=AssertionError('abstract not selected')):
   result=m.complete_one(self.pid,opts,threading.Event(),{})
  self.assertEqual(result['filled'],['authors']);p=m.paper(self.pid);self.assertIsNone(p['abstract']);self.assertIsNone(p['year']);self.assertNotIn('unselected',p['meta_json'])
 def test_location_replace_is_atomic_undoable_and_scope_limited(self):
  a=self.client.post('/api/organizer/folders',json={'name':'Fixture A','scope':'library'}).json()['folder']['id']
  b=self.client.post('/api/organizer/folders',json={'name':'Fixture B','scope':'library','parent_id':a}).json()['folder']['id']
  self.client.post('/api/organizer/items',json={'ids':[self.pid],'scope':'library','folder_ids':[a],'action':'add'}).raise_for_status()
  r=self.client.post('/api/organizer/items',json={'ids':[self.pid],'scope':'library','folder_ids':[b],'action':'replace'});self.assertEqual(r.status_code,200)
  locations=self.client.get('/api/organizer/locations/'+str(self.pid)).json();self.assertEqual(locations['folder_ids'],[b]);self.assertEqual(locations['paths'],['Fixture A / Fixture B'])
  self.client.post('/api/organizer/undo/'+r.json()['undo_token']).raise_for_status();self.assertEqual(self.client.get('/api/organizer/locations/'+str(self.pid)).json()['folder_ids'],[a])
  self.client.post('/api/organizer/items',json={'ids':[self.pid],'scope':'library','folder_ids':[],'action':'replace'}).raise_for_status();self.assertEqual(self.client.get('/api/organizer/locations/'+str(self.pid)).json()['folder_ids'],[])
 def test_beginner_is_adaptive_and_exposes_real_path_context(self):
  rows=[{'id':i,'title':'Ultrasonic gas flow meter study '+str(i),'year':2024,'abstract':'Ultrasonic gas measurement'} for i in range(1,90)]
  edges=[{'from':1,'to':i} for i in range(2,30)]+[{'from':i,'to':1} for i in range(30,36)]+[{'from':i,'to':2} for i in range(36,90)]
  rank,_=recommendation.rank(rows,edges,[1]);selected=[r for r in rank.values() if 'essential' in r['roles']]
  self.assertGreater(len(selected),6);self.assertLess(len(selected),89)
  for r in selected:self.assertIn('entry_path_ids',r['evidence'])
 def test_capture_wait_reports_reason_and_focus_is_authorized(self):
  from app import browser_capture as capture
  with patch.object(capture,'dispatch'):
   token={'X-Workspace-Token':self.client.get('/api/workspace/state').json()['token']}
   job=self.client.post('/api/pdf-capture',json={'paper_id':self.pid},headers=token).json();jid=job['id']
   try:
    self.assertEqual(self.client.post('/api/pdf-capture/'+jid+'/focus').status_code,403)
    self.assertEqual(self.client.post('/api/pdf-capture/'+jid+'/focus',headers=token).status_code,200)
    callback={'X-Capture-Token':capture._jobs[jid]['_token']}
    stage=self.client.post('/api/pdf-capture/'+jid+'/stage',headers=callback,json={'stage':'access','reason':'publisher_denied','http_status':403,'page_host':'www.sciencedirect.com','attempted':2})
    self.assertEqual(stage.status_code,200);value=self.client.get('/api/pdf-capture/'+jid).json();self.assertEqual(value['status'],'awaiting_access');self.assertEqual(value['phase'],'access');self.assertEqual(value['issues'][0]['http_status'],403);self.assertIn('elapsed_seconds',value);self.assertNotIn('_token',str(value))
   finally:self.client.post('/api/pdf-capture/'+jid+'/cancel',headers=token)
 def test_pdf_recovery_is_limited_to_uploaded_parent_and_hash(self):
  import pdf_fixture as pdfgen; import hashlib
  from unittest.mock import MagicMock
  from app import zotero_pdf as z
  from app.zotero_client import ZoteroError
  f=Path(T.name)/'original.pdf'
  with pdfgen.document() as doc:doc.new_page().insert_text((30,30),'Fixture original');doc.save(f)
  with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(f),self.pid))
  cli=MagicMock();cli.get_children.return_value=[];cli.create_items.return_value={'success':{'0':'OLDKEY'}};cli.get_item.side_effect=[{'data':{'itemType':'attachment','parentItem':'PARENT'}},ZoteroError('attachment replaced')]
  digest=hashlib.sha256(f.read_bytes()).hexdigest()
  def recover(p,expected_digest,parent_key):
   self.assertEqual(parent_key,'PARENT');self.assertEqual(expected_digest,digest);z.record(self.pid,'existing','verified','NEWKEY',digest,str(f));return f
  with patch('app.zotero_attachments.find_native',side_effect=recover):result=z.sync_pdf(self.pid,'PARENT',cli)
  self.assertEqual(result['state'],'existing');self.assertEqual(result['attachment_key'],'NEWKEY');self.assertEqual(cli.upload_pdf_attachment.call_count,1)

if __name__=='__main__':unittest.main(verbosity=2)
