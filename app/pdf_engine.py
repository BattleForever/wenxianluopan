"""Local PDF extraction and rendering without a strong-copyleft runtime.

Text and layout come from pypdf; PDFium is used only for page images. All
PDFium calls share one lock because the native library is not thread-safe.
"""
from __future__ import annotations
import io
import math
import threading
from pathlib import Path
from pypdf import PdfReader, mult

EXTRACTOR_VERSION = 'pypdf_content_order_v1'
_render_lock = threading.RLock()


class PDFDocument:
    def __init__(self, source):
        self.raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        if not self.raw.startswith(b'%PDF-'):
            raise ValueError('Not a PDF')
        self.stream = io.BytesIO(self.raw)
        try:
            self.reader = PdfReader(self.stream, strict=False)
            self.encrypted = self.reader.is_encrypted
            if self.encrypted:
                # Do not silently decrypt even empty-password documents: the
                # existing product contract asks users to supply unlocked PDFs.
                self.metadata = {}
            else:
                self.metadata = {str(k).lstrip('/').lower(): str(v) for k,v in (self.reader.metadata or {}).items()}
        except Exception:
            self.stream.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.stream.close()

    def __len__(self):
        self._readable()
        return len(self.reader.pages)

    def _readable(self):
        if self.encrypted:
            raise ValueError('Encrypted PDF')

    def page_text(self, index):
        self._readable()
        # Plain extraction preserves content-stream reading order instead of
        # interleaving columns by sorting all glyphs geometrically.
        return self.reader.pages[index].extract_text(extraction_mode='plain') or ''

    def page_layout(self, index):
        self._readable()
        page = self.reader.pages[index]
        height = float(page.mediabox.height)
        fragments = []

        def width_of(text, font, size):
            widths = (font or {}).get('/Widths')
            first = int((font or {}).get('/FirstChar',0))
            if widths:
                return sum(float(widths[ord(ch)-first]) if 0<=ord(ch)-first<len(widths) else 500 for ch in text)*size/1000
            return len(text)*size*.5

        def visitor(text, cm, tm, font, size):
            if not text.strip():
                return
            matrix = mult(tm, cm)
            effective_size = abs(float(size)) * (math.hypot(matrix[0], matrix[1]) or 1)
            font_name = str((font or {}).get('/BaseFont', 'Unknown')).lstrip('/')
            x, baseline = matrix[4], matrix[5]
            for line_no, line in enumerate(text.splitlines()):
                if not line.strip():
                    continue
                top = height - baseline - effective_size + line_no * effective_size * 1.2
                fragments.append({'spans': [{'text':line, 'font':font_name, 'size':effective_size}],
                                  'bbox': [x, top, x+width_of(line,font,effective_size), top+effective_size]})

        page.extract_text(extraction_mode='plain', visitor_text=visitor)
        # Word/letter fragments often use separate text operators. Join only
        # adjacent fragments on the same baseline, never sort across columns.
        lines=[]
        for fragment in fragments:
            previous=lines[-1] if lines else None
            size=fragment['spans'][0]['size'];box=fragment['bbox']
            gap=box[0]-previous['bbox'][2] if previous else 0
            if (previous and abs(box[3]-previous['bbox'][3])<=max(size*.35,1)
                    and box[0]>=previous['bbox'][0]-1 and -size<=gap<=size*3):
                before=previous['spans'][-1]['text'];after=fragment['spans'][0]['text']
                if gap>size*.2 and before and after and not before[-1].isspace() and not after[0].isspace():
                    previous['spans'][-1]['text']+=' '
                previous['spans'].extend(fragment['spans'])
                previous['bbox']=[min(previous['bbox'][0],box[0]),min(previous['bbox'][1],box[1]),max(previous['bbox'][2],box[2]),max(previous['bbox'][3],box[3])]
            else:lines.append(fragment)
        blocks=[]
        for line in lines:
            previous=blocks[-1]['lines'][-1] if blocks else None
            span=line['spans'][0];old=previous['spans'][0] if previous else None
            if (previous and 0<line['bbox'][1]-previous['bbox'][1]<=span['size']*1.8
                    and abs(line['bbox'][0]-previous['bbox'][0])<16
                    and abs(span['size']-old['size'])<.6
                    and span['font']==old['font']):
                blocks[-1]['lines'].append(line)
            else:blocks.append({'lines':[line]})
        return blocks, height

    def render_png(self, index, max_side=1800, max_scale=1.4):
        self._readable()
        if not 0 <= index < len(self):
            raise IndexError('PDF page out of range')
        if max_side <= 0 or max_scale <= 0:
            raise ValueError('Invalid render dimensions')
        import pypdfium2
        with _render_lock:
            document = pypdfium2.PdfDocument(self.raw)
            try:
                page = document[index]
                try:
                    width, height = page.get_size()
                    scale = min(max_scale, max_side/max(width,height))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            output = io.BytesIO()
                            image.save(output, format='PNG')
                            return output.getvalue()
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()
