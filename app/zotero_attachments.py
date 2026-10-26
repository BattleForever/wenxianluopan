"""Read actual attachment locations through Zotero, including managed linked files."""
from pathlib import Path
import hashlib
from .db import get_db

def find_native(p,expected_digest=None,parent_key=None):
    from .zotero_client import ZoteroClient,normalize_doi,KEY_RE
    from .pdf_access import validate_pdf,MAX_BYTES
    from . import zotero_pdf
    cli=ZoteroClient(timeout=3);doi=normalize_doi(p.get('doi'));title=(p.get('title') or '').strip().casefold()
    if not doi and not title:return None
    try:items=cli.search_items(doi or p['title'],limit=100)
    except Exception:return None
    for item in items:
        d=item.get('data') or {};parent=item.get('key') or d.get('key')
        if parent_key and parent!=parent_key:continue
        if not parent or not KEY_RE.fullmatch(parent) or d.get('deleted') or d.get('itemType') in (None,'note','attachment'):continue
        if doi:
            if normalize_doi(d.get('DOI'))!=doi:continue
        elif (d.get('title') or '').strip().casefold()!=title:continue
        try:children=cli.get_children(parent)
        except Exception:continue
        for child in children:
            a=child.get('data') or {};key=child.get('key') or a.get('key')
            if not key or not KEY_RE.fullmatch(key) or a.get('deleted') or a.get('itemType')!='attachment' or a.get('parentItem')!=parent:continue
            if a.get('contentType')!='application/pdf' and not (a.get('filename') or a.get('path') or '').lower().endswith('.pdf'):continue
            try:
                file=Path(cli.attachment_file(key))
                if not file.is_file() or file.stat().st_size>MAX_BYTES:continue
                raw=file.read_bytes();validate_pdf(raw)
                if expected_digest and hashlib.sha256(raw).hexdigest()!=expected_digest:continue
            except Exception:continue
            # The API resolves linked-file bases and plugin-managed directories.
            # Never infer a Zotero key from the containing folder's name.
            with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(file),p['id']))
            zotero_pdf.init();zotero_pdf.record(p['id'],'existing','已关联 Zotero 中的原文附件',key,hashlib.sha256(raw).hexdigest(),str(file))
            return file
    return None
