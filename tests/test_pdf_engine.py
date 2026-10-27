"""Portable PDF runtime, historical evidence and native rendering regressions."""
import io, os, sys, tempfile, unittest, hashlib, json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from pypdf import PdfReader, PdfWriter
from PIL import Image
import pdf_fixture

TMP=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=TMP.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.pdf_engine import PDFDocument
from app.db import init_db,get_db
from app import pdf_guide as pg
from app.store import upsert_paper
init_db();pg.init()

def raw():
    doc=pdf_fixture.document();page=doc.new_page()
    page.insert_text((40,80),'Introduction',fontname='hebo',fontsize=12)
    page.insert_textbox(pdf_fixture.Rect(40,110,280,380),'Left column begins. '+('A controlled measurement reports experimental results. '*6),fontsize=10)
    page.insert_textbox(pdf_fixture.Rect(310,110,560,380),'Right column begins. '+('A separate discussion explains the observed results. '*6),fontsize=10)
    return doc.tobytes()

class PortablePDF(unittest.TestCase):
    def test_content_order_does_not_interleave_columns(self):
        with PDFDocument(raw()) as doc:
            text=doc.page_text(0)
            self.assertLess(text.index('Left column begins'),text.index('Right column begins'))
            self.assertLess(text.rfind('controlled measurement'),text.index('Right column begins'))
            self.assertEqual(len(doc),1)

    def test_render_rotated_page_bounded_and_concurrent(self):
        writer=PdfWriter();writer.append(PdfReader(io.BytesIO(raw())))
        writer.pages[0].rotate(90);stream=io.BytesIO();writer.write(stream);data=stream.getvalue()
        def render(_):
            with PDFDocument(data) as doc:return doc.render_png(0,max_side=500)
        with ThreadPoolExecutor(max_workers=4) as pool:images=list(pool.map(render,range(8)))
        for data in images:
            with Image.open(io.BytesIO(data)) as image:
                self.assertGreater(image.width,image.height)
                self.assertLessEqual(max(image.size),501)
        self.assertEqual(len({hashlib.sha256(x).hexdigest() for x in images}),1)

    def test_encrypted_file_requires_explicit_unlock(self):
        writer=PdfWriter();writer.append(PdfReader(io.BytesIO(raw())));writer.encrypt('secret')
        out=io.BytesIO();writer.write(out)
        with PDFDocument(out.getvalue()) as doc:
            self.assertTrue(doc.encrypted)
            with self.assertRaises(ValueError):doc.page_text(0)

    def test_corrupt_file_rejected_and_blank_has_no_text(self):
        with self.assertRaises(ValueError):PDFDocument(b'not a PDF')
        writer=PdfWriter();writer.add_blank_page(595,842);out=io.BytesIO();writer.write(out)
        with PDFDocument(out.getvalue()) as doc:self.assertEqual(doc.page_text(0),'')

    def test_legacy_snapshot_keeps_its_original_text_after_engine_change(self):
        file=Path(TMP.name)/'legacy.pdf';file.write_bytes(raw())
        with get_db() as c:pid,_=upsert_paper(c,{'title':'Legacy original','local_pdf_path':str(file)},'fixture','library')
        p,_,pages,digest=pg._source(pid)
        legacy_pages=[pages[0].replace(' ','  ')]
        folder=pg.DATA_DIR/'private'/'pdf_sources';folder.mkdir(parents=True,exist_ok=True)
        (folder/(digest+'.pdf')).write_bytes(raw())
        with patch.object(pg,'VERSION','pdf_guide_v3_ieee_layout'):
            pg._save(pid,p,legacy_pages,digest,{'overview':'Historical saved result'},'fixture-model')
        with get_db() as c:
            c.execute('INSERT INTO pdf_guide_source_texts VALUES(?,?,?,?)',(pid,digest,'pdf_guide_v3_ieee_layout',json.dumps(legacy_pages)))
            guide_id=c.execute('SELECT id FROM pdf_guides WHERE paper_id=?',(pid,)).fetchone()[0]
        response=pg.source_page(pid,1,source_hash=digest,guide_id=guide_id)
        self.assertEqual(response['text'],legacy_pages[0])
        self.assertNotEqual(response['text'],pages[0])
        with patch.object(pg.ai_brief,'load_ai_config',return_value=None):
            result=pg.get_guide(pid)
        self.assertTrue(result['stale']);self.assertEqual(result['guide']['overview'],'Historical saved result')
        self.assertIn('导读规则已变化',result['stale_reasons'])
        self.assertTrue(pg.source_page(pid,1,image=True,source_hash=digest,guide_id=guide_id).body.startswith(b'\x89PNG'))

if __name__=='__main__':unittest.main()
