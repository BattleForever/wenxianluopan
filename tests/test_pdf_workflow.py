"""Failure, restart, source-identity and portable-export acceptance checks."""
import copy
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

TMP=tempfile.TemporaryDirectory()
os.environ['PAPERNET_DATA_DIR']=TMP.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fitz
from app.db import init_db,get_db,set_setting
from app.store import upsert_paper
from app import pdf_guide as pg
from app.pdf_structure import check_coverage,inventory

init_db();pg.init()
TEXT='This source describes a phased array and reports a controlled experiment. '
E={'page':1,'quote':'describes a phased array'}
DATA={'title_zh':'公开测试','overview':'中文概括','institutions':'测试单位',
      'sections':[{'heading':h,'explanation':'问题、方法和证据','evidence':E} for h in ('Introduction','Conclusion')],
      'figures':[{'number':1,'caption':'原理','panels':[{'label':'整体','explanation':'原理示意'}],'evidence':E}],
      'conclusion':{'text':'结论有范围','evidence':E}}
CFG={'chat_url':'https://api.deepseek.com/v1/chat/completions','request_model':'test-model','api_key':'fake-test-credential'}


class Workflow(unittest.TestCase):
    def setUp(self):
        pg._active.clear()
        set_setting('offline_mode','0')
        self.f=Path(TMP.name)/(self.id().split('.')[-1]+'.pdf')
        self.write_pdf(TEXT*9)
        with get_db() as c:
            self.pid,_=upsert_paper(c,{'title':self.id(),'local_pdf_path':str(self.f)},source='test',status='library')
        self.p,_,self.pages,self.digest=pg._source(self.pid)
        self.expected={'basis':'layout_detected','sections':[{'heading':'Introduction'},{'heading':'Conclusion'}],
                       'figures':[{'number':1,'page':1,'panels':[]}],'note':'测试版面清单'}

    def write_pdf(self,text):
        doc=fitz.open();p=doc.new_page();p.insert_textbox(fitz.Rect(30,30,500,700),text);doc.save(self.f);doc.close()

    def ready(self):
        pg._snapshot(self.pid,self.f,self.pages,self.digest)
        pg._save(self.pid,self.p,self.pages,self.digest,pg.validate(DATA,self.pages),'test-model','test-model')

    def job(self):
        with get_db() as c:
            return c.execute("INSERT INTO pdf_guide_jobs(paper_id,status,stage,source_hash,started_at) VALUES(?,'generating','generating',?,?)",(self.pid,self.digest,pg._now())).lastrowid

    def test_restart_persists_interruption_without_model_retry(self):
        self.ready();self.job()
        with patch.object(pg.ai_brief,'call_llm',side_effect=AssertionError('no retry')):
            pg.init();r=pg.get_guide(self.pid)
        self.assertEqual(r['error']['code'],'interrupted');self.assertIsNotNone(r['guide'])

    def test_failed_regeneration_preserves_last_ready(self):
        self.ready();j=self.job();pg._active.add(self.pid)
        with patch.object(pg.ai_brief,'call_llm',return_value=('{}','test-model')):
            pg._generate(self.pid,self.p,self.pages,self.digest,CFG,j,self.expected)
        r=pg.get_guide(self.pid)
        self.assertEqual(r['job']['status'],'failed');self.assertIsNotNone(r['guide']);self.assertNotIn(self.pid,pg._active)

    def test_changed_attachment_uses_original_text_and_image(self):
        self.ready();self.write_pdf('A completely different current attachment. '*20)
        self.assertTrue(pg.get_guide(self.pid)['stale'])
        old=pg.source_page(self.pid,1,source_hash=self.digest)
        self.assertIn(E['quote'],old['text'])
        self.assertTrue(pg.source_page(self.pid,1,image=True,source_hash=self.digest).body.startswith(b'\x89PNG'))
        self.assertNotEqual(pg.source_page(self.pid,1)['source_hash'],old['source_hash'])

    def test_corrupt_snapshot_fails_closed(self):
        self.ready();(pg.DATA_DIR/'private/pdf_sources'/(self.digest+'.pdf')).write_bytes(b'tampered')
        with self.assertRaises(pg.HTTPException) as e:pg.source_page(self.pid,1,source_hash=self.digest)
        self.assertEqual(e.exception.status_code,409)

    def test_old_cache_without_snapshot_is_not_current_pdf(self):
        pg._save(self.pid,self.p,self.pages,self.digest,pg.validate(DATA,self.pages),'old-model')
        with self.assertRaises(pg.HTTPException) as e:pg.source_page(self.pid,1,source_hash=self.digest)
        self.assertEqual(e.exception.status_code,409)

    def test_same_pdf_preserves_each_extraction_version(self):
        self.ready()
        old_id=pg.get_guide(self.pid)['guide_id']
        different=['A different extraction order. '+t for t in self.pages]
        with patch.object(pg,'VERSION','next-parser'):
            pg._snapshot(self.pid,self.f,different,self.digest)
            pg._save(self.pid,self.p,different,self.digest,pg.validate(DATA,different),'test-model','test-model')
            new_id=pg.get_guide(self.pid)['guide_id']
        old=pg.source_page(self.pid,1,source_hash=self.digest,guide_id=old_id)
        new=pg.source_page(self.pid,1,source_hash=self.digest,guide_id=new_id)
        self.assertEqual(old['text'],self.pages[0]);self.assertEqual(new['text'],different[0])

    def test_source_snapshot_is_scoped_to_paper(self):
        self.ready()
        with self.assertRaises(pg.HTTPException):pg.source_page(self.pid+100000,1,source_hash=self.digest)
        with self.assertRaises(pg.HTTPException):pg.source_page(self.pid,1,source_hash='../outside')

    def test_export_contains_relative_assets_and_no_runtime_paths(self):
        self.ready();r=pg.export_guide(self.pid,self.digest)
        with zipfile.ZipFile(io.BytesIO(r.body)) as z:
            self.assertEqual(set(z.namelist()),{'reading.md','figures/page_01.png'})
            md=z.read('reading.md').decode()
            self.assertIn('(figures/page_01.png)',md)
            for forbidden in ['localhost','127.0.0.1',TMP.name,CFG['api_key']]:self.assertNotIn(forbidden,md)
            self.assertTrue(z.read('figures/page_01.png').startswith(b'\x89PNG'))

    def test_missing_figures_and_invented_panels_rejected(self):
        for mutate in ('missing','invented','wrongpage'):
            d=copy.deepcopy(DATA)
            if mutate=='missing':d['figures']=[]
            if mutate=='invented':d['figures'][0]['panels'][0]['label']='a'
            if mutate=='wrongpage':d['figures'][0]['evidence']={'page':2,'quote':E['quote']}
            with self.assertRaises(ValueError):pg.validate(d,self.pages,self.expected)

    def test_missing_or_reordered_sections_rejected(self):
        for headings in [('Introduction','Other'),('Conclusion','Introduction')]:
            d=copy.deepcopy(DATA)
            for section,h in zip(d['sections'],headings):section['heading']=h
            with self.assertRaises(ValueError):pg.validate(d,self.pages,self.expected)

    def test_duplicate_panel_and_omitted_panel_rejected(self):
        expected=copy.deepcopy(self.expected);expected['figures'][0]['panels']=['a','b']
        for labels in (['a'],['a','a'],['b','a']):
            d=copy.deepcopy(DATA);d['figures'][0]['panels']=[{'label':l,'explanation':'测试'} for l in labels]
            with self.assertRaises(ValueError):pg.validate(d,self.pages,expected)

    def test_oversized_output_not_silently_truncated(self):
        d=copy.deepcopy(DATA);d['overview']='x'*16001
        with self.assertRaises(ValueError):pg.validate(d,self.pages)

    def test_too_many_detected_sections_rejected_before_model_call(self):
        expected={**self.expected,'sections':[{'heading':str(i)} for i in range(61)]}
        with patch.object(pg,'inventory',return_value=expected),patch.object(pg.ai_brief,'call_llm',side_effect=AssertionError('network')):
            with self.assertRaises(pg.HTTPException) as e:pg._snapshot(self.pid,self.f,self.pages,self.digest)
        self.assertEqual(e.exception.status_code,422)

    def test_offline_request_does_not_resolve_pdf_or_call_model(self):
        set_setting('offline_mode','1')
        with patch.object(pg,'_source',side_effect=AssertionError('no source read')):
            self.assertEqual(pg.generate(self.pid)['error']['code'],'offline_mode')

    def test_concurrency_limit_and_duplicate_do_not_start_threads(self):
        with patch.object(pg.ai_brief,'load_ai_config',return_value=CFG),patch.object(pg.threading,'Thread') as thread:
            pg._active.add(self.pid)
            self.assertEqual(pg.generate(self.pid)['status'],'generating')
            pg._active.clear();pg._active.update({-10,-11})
            with self.assertRaises(pg.HTTPException) as e:pg.generate(self.pid)
            self.assertEqual(e.exception.status_code,429);thread.assert_not_called()

    def test_thread_start_failure_is_persisted(self):
        with patch.object(pg.ai_brief,'load_ai_config',return_value=CFG),patch.object(pg.threading,'Thread') as thread:
            thread.return_value.start.side_effect=RuntimeError('internal path must not escape')
            with self.assertRaises(pg.HTTPException):pg.generate(self.pid)
        self.assertEqual(pg.get_guide(self.pid)['error']['code'],'start_failed');self.assertNotIn(self.pid,pg._active)

    def test_successful_product_job_creates_bound_snapshot(self):
        data=copy.deepcopy(DATA);data['figures']=[]
        with patch.object(pg.ai_brief,'load_ai_config',return_value=CFG),patch.object(pg.ai_brief,'call_llm',return_value=(json.dumps(data),'response-model')) as call:
            self.assertEqual(pg.generate(self.pid)['status'],'generating')
            deadline=time.monotonic()+5
            while self.pid in pg._active and time.monotonic()<deadline:time.sleep(.02)
            result=pg.get_guide(self.pid)
        self.assertEqual(result['job']['status'],'ready');self.assertEqual(call.call_count,1)
        text=pg.source_page(self.pid,1,source_hash=result['source_hash'],guide_id=result['guide_id'])
        self.assertEqual(text['text'],self.pages[0])

    def test_get_never_generates_and_model_change_marks_stale(self):
        self.ready()
        with patch.object(pg.ai_brief,'call_llm',side_effect=AssertionError('GET called model')),patch.object(pg.ai_brief,'load_ai_config',return_value={**CFG,'request_model':'other'}):
            self.assertIn('模型配置已变化',pg.get_guide(self.pid)['stale_reasons'])

    def test_layout_detects_real_caption_not_inline_reference(self):
        doc=fitz.open();p=doc.new_page()
        p.insert_text((40,80),'Introduction',fontname='hebo',fontsize=12)
        p.insert_textbox(fitz.Rect(40,100,500,300),TEXT*6,fontname='helv',fontsize=11)
        p.insert_text((40,350),'Fig. 1 Device layout. a Fabrication. b Measurement.',fontname='hebo',fontsize=9)
        result=inventory(doc);doc.close()
        self.assertEqual([f['number'] for f in result['figures']],[1])
        self.assertEqual(result['figures'][0]['panels'],['a','b'])
        self.assertNotIn(TEXT[:30],[s['heading'] for s in result['sections']])


class ZoteroIdentity(unittest.TestCase):
    def test_locked_index_copy_retries_once_with_new_name(self):
        root=Path(TMP.name)/'copy-retry';root.mkdir(exist_ok=True)
        src=root/'source.sqlite';src.write_bytes(b'synthetic index')
        original=pg.shutil.copy2;targets=[]
        def copied(a,b):
            targets.append(b)
            if len(targets)==1:raise PermissionError('synthetic lock')
            return original(a,b)
        with patch.object(pg.shutil,'copy2',side_effect=copied):target=pg._copy_zotero_index(src,root)
        self.assertEqual(target.read_bytes(),b'synthetic index');self.assertEqual(len(targets),2)
        self.assertNotEqual(targets[0],targets[1])

    def test_conflicting_doi_never_falls_back_to_same_title(self):
        root=Path(TMP.name)/'fake-zotero';root.mkdir(exist_ok=True)
        db=root/'zotero.sqlite'
        with closing(sqlite3.connect(db)) as c:
            c.executescript('CREATE TABLE fields(fieldID INTEGER,fieldName TEXT); CREATE TABLE itemData(itemID INTEGER,fieldID INTEGER,valueID INTEGER); CREATE TABLE itemDataValues(valueID INTEGER,value TEXT);')
            c.executescript("INSERT INTO fields VALUES(1,'DOI'),(2,'title'); INSERT INTO itemData VALUES(1,1,1),(1,2,2); INSERT INTO itemDataValues VALUES(1,'10.1000/wrong'),(2,'Same title');")
            c.commit()
        set_setting('zotero_data_dir',str(root))
        with self.assertRaises(pg.HTTPException) as e:pg._find_pdf({'id':999,'title':'Same title','doi':'10.1000/right'})
        self.assertEqual(e.exception.status_code,404)


if __name__=='__main__':unittest.main()
