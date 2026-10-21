"""Bounded source and citation regressions; no external model or Zotero writes."""
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import fitz
TMP=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=TMP.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.db import init_db,get_db,set_setting
init_db()
from app import pdf_guide as pg
from app.store import upsert_paper
pg.init()
TEXT='This source describes a phased array and reports a controlled experiment.'
E={'page':1,'quote':'describes a phased array'}
DATA={'title_zh':'测试题名','overview':'中文概括','institutions':'测试单位','sections':[{'heading':'Introduction','explanation':'解释研究问题','evidence':E},{'heading':'Conclusion','explanation':'解释结论边界','evidence':E}],'figures':[{'number':1,'caption':'方法图','panels':[{'label':'a','explanation':'阵列方法'}],'evidence':E}],'conclusion':{'text':'测试结论','evidence':E}}
class TestPDFGuide(unittest.TestCase):
 def test_valid_quote_and_offset(self):
  d=pg.validate(DATA,[TEXT]);self.assertEqual(d['sections'][0]['evidence']['start'],TEXT.index(E['quote']))
 def test_fabricated_quote_rejected(self):
  d=copy.deepcopy(DATA);d['sections'][0]['evidence']['quote']='A fabricated experimental claim'
  with self.assertRaises(ValueError):pg.validate(d,[TEXT])
 def test_out_of_bounds_page_rejected(self):
  d=copy.deepcopy(DATA);d['sections'][0]['evidence']['page']=2
  with self.assertRaises(ValueError):pg.validate(d,[TEXT])
 def test_unreadable_pdf_and_no_ocr(self):
  f=Path(TMP.name)/'empty.pdf';doc=fitz.open();doc.new_page();doc.save(f);doc.close()
  with get_db() as c:pid,_=upsert_paper(c,{'title':'Scanned example','local_pdf_path':str(f)},source='test',status='library')
  with self.assertRaises(pg.HTTPException) as e:pg._source(pid)
  self.assertEqual(e.exception.status_code,422)
 def test_offline_post_never_calls_model(self):
  f=Path(TMP.name)/'text.pdf';d=fitz.open();p=d.new_page();p.insert_textbox(fitz.Rect(30,30,500,700),TEXT*8);d.save(f);d.close()
  with get_db() as c:pid,_=upsert_paper(c,{'title':'Readable example','local_pdf_path':str(f)},source='test',status='library')
  set_setting('offline_mode','1')
  with patch.object(pg.ai_brief,'call_llm',side_effect=AssertionError('network')):
   self.assertEqual(pg.generate(pid)['error']['code'],'offline_mode')
 def test_changed_pdf_marks_cache_stale(self):
  f=Path(TMP.name)/'stale.pdf';d=fitz.open();p=d.new_page();p.insert_textbox(fitz.Rect(30,30,500,700),TEXT*8);d.save(f);d.close()
  with get_db() as c:pid,_=upsert_paper(c,{'title':'Cache source','local_pdf_path':str(f)},source='test',status='library')
  p,_,pages,h=pg._source(pid);pg._save(pid,p,pages,h,pg.validate(DATA,pages),'test-model')
  self.assertFalse(pg.get_guide(pid)['stale'])
  with get_db() as c:c.execute('UPDATE papers SET title=? WHERE id=?',('New title',pid))
  self.assertTrue(pg.get_guide(pid)['stale'])
 def test_model_html_is_data(self):
  d=copy.deepcopy(DATA);d['overview']='<img src=x onerror=alert(1)>'
  self.assertEqual(pg.validate(d,[TEXT])['overview'],d['overview'])
if __name__=='__main__':unittest.main()
