"""Translation adds Chinese fields without replacing the original or human text."""
import os,sys,tempfile,json,threading,unittest
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name;sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db,set_setting
from app.store import upsert_paper
from app import metadata_jobs as m,workspace
from fastapi import FastAPI
from fastapi.testclient import TestClient
init_db();workspace.init();m.init();api=FastAPI();api.include_router(m.router);client=TestClient(api)
TITLE='Fixture PMUT ultrasonic flowmeter';ABSTRACT='This fixture measures gas flow using two ultrasonic travel times.'
CFG={'chat_url':'https://api.deepseek.com/v1/chat/completions','request_model':'fixture-model','api_key':'synthetic-key'}

class TranslationOnly(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0');m._active.clear()
  with get_db() as c:self.pid,_=upsert_paper(c,{'title':TITLE+' '+self.id(),'abstract':ABSTRACT},'fixture','library')
  self.options={'purpose':'translate','allow_ai':True,'allow_network':False,'include_metrics':False}
 def generated(self,cfg,system,prompt,**kw):
  d=json.loads(prompt);self.assertNotIn('summary',d['fields'])
  return json.dumps({f:{'text':'夹具中文题名' if f=='title_zh' else '夹具中文摘要，保留原始事实。','quote':d['title'] if f=='title_zh' else d['abstract']} for f in d['fields']},ensure_ascii=False),'fixture-model'
 def run_translation(self):
  with patch.object(m.ai_brief,'ai_generation_blocked',return_value=None),patch.object(m.ai_brief,'load_ai_config',return_value=CFG),patch.object(m.ai_brief,'call_llm',side_effect=self.generated) as call,patch.object(m.sources,'zotero_biblio',side_effect=AssertionError('translation should not query bibliography')) as zotero,patch.object(m.sources,'zotero_pdf_paths',return_value=[]):
   result=m.complete_one(self.pid,self.options,threading.Event(),{});zotero.assert_not_called();return result,call
 def test_chinese_added_and_original_unchanged(self):
  before=m.paper(self.pid);r,_=self.run_translation();after=m.paper(self.pid)
  self.assertEqual(after['title'],before['title']);self.assertEqual(after['abstract'],before['abstract']);self.assertEqual(set(r['filled']),{'title_zh','abstract_zh'})
  self.assertTrue(after['title_zh']);self.assertTrue(after['abstract_zh'])
 def test_existing_human_translation_retained(self):
  with get_db() as c:c.execute('UPDATE papers SET title_zh=? WHERE id=?',('人工题名',self.pid))
  r,call=self.run_translation();self.assertEqual(r['filled'],['abstract_zh']);self.assertEqual(m.paper(self.pid)['title_zh'],'人工题名')
 def test_complete_translation_does_not_call_model_for_missing_summary(self):
  with get_db() as c:c.execute('UPDATE papers SET title_zh=?,abstract_zh=? WHERE id=?',('人工题名','人工摘要',self.pid))
  r,call=self.run_translation();call.assert_not_called();self.assertEqual(r['outcome'],'already_complete')
 def test_no_original_abstract_does_not_invent_translation(self):
  with get_db() as c:c.execute('UPDATE papers SET abstract=NULL WHERE id=?',(self.pid,))
  r,_=self.run_translation();self.assertEqual(r['filled'],['title_zh']);self.assertIn('abstract_zh',r['still_missing']);self.assertFalse(m.paper(self.pid)['abstract_zh'])
 def test_translation_endpoint_disables_unrelated_fetches(self):
  with patch.object(m,'worker',return_value=None):
   response=client.post('/api/research/enrichment',json={'ids':[self.pid],'purpose':'translate','allow_ai':True,'allow_network':True,'include_metrics':True})
  self.assertEqual(response.status_code,200);options=response.json()['options'];self.assertFalse(options['allow_network']);self.assertFalse(options['include_metrics'])
 def test_translation_explicitly_requires_ai_permission(self):
  response=client.post('/api/research/enrichment',json={'ids':[self.pid],'purpose':'translate','allow_ai':False});self.assertEqual(response.status_code,400)

if __name__=='__main__':unittest.main()
