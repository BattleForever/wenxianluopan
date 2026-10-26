"""Attach validated PDF originals to an existing verified Zotero parent.

Uses Zotero's documented local full-file upload. No direct Zotero DB writes,
no linked-only substitute, and no changes to existing user attachments.
"""
import hashlib
from pathlib import Path
from datetime import datetime,timezone
from fastapi import HTTPException
from .db import get_db
from . import pdf_access,pdf_resolver
from .zotero_client import KEY_RE

MARKER='wenxianluopan-pdf'

def init():
    with get_db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS zotero_pdf_state(
       paper_id INTEGER PRIMARY KEY,state TEXT,attachment_key TEXT,sha256 TEXT,message TEXT,updated_at TEXT)""")
        if 'native_path' not in {r['name'] for r in c.execute('PRAGMA table_info(zotero_pdf_state)')}:
            c.execute('ALTER TABLE zotero_pdf_state ADD COLUMN native_path TEXT')

def status(pid):
    with get_db() as c:r=c.execute('SELECT * FROM zotero_pdf_state WHERE paper_id=?',(pid,)).fetchone()
    return dict(r) if r else {'state':'not_checked','message':'PDF尚未同步'}

def record(pid,state,message,key=None,digest=None,native_path=None):
    with get_db() as c:c.execute('INSERT OR REPLACE INTO zotero_pdf_state(paper_id,state,attachment_key,sha256,message,updated_at,native_path) VALUES(?,?,?,?,?,?,?)',(pid,state,key,digest,message,datetime.now(timezone.utc).isoformat(),native_path))
    return status(pid)

def checked_file(path):
    f=Path(path)
    if not f.is_file() or f.stat().st_size>pdf_access.MAX_BYTES:raise ValueError('PDF不可读')
    raw=f.read_bytes();pdf_access.validate_pdf(raw);return raw

def refresh_location(pid):
    """Attachment managers can move a verified file after the upload returns."""
    old=status(pid);key=old.get('attachment_key')
    if old.get('state') not in ('existing','saved') or not key or not KEY_RE.fullmatch(key):return False
    try:
        from .zotero_client import ZoteroClient
        file=ZoteroClient(timeout=3).attachment_file(key);raw=checked_file(file)
        if old.get('sha256') and hashlib.sha256(raw).hexdigest()!=old['sha256']:return False
        # A custom copy remains independent; refresh only a vanished native binding.
        p=pdf_access.paper(pid)
        if Path(p.get('local_pdf_path') or '').is_file():return False
        with get_db() as c:
            c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(file),pid))
            c.execute('UPDATE zotero_pdf_state SET native_path=? WHERE paper_id=?',(str(file),pid))
        return True
    except Exception:
        # Some attachment managers replace the imported attachment with a new
        # linked-file item. Recover by strict paper identity AND the saved hash.
        try:
            from .zotero_attachments import find_native
            return bool(find_native(pdf_access.paper(pid),expected_digest=old.get('sha256')))
        except Exception:return False

def sync_pdf(pid,parent,cli):
    attachment=None;stage='check'
    try:
        prior=status(pid)
        requested=pdf_access.paper(pid);requested_path=requested.get('local_pdf_path')
        try:requested_raw=checked_file(requested_path) if requested_path else None
        except Exception:requested_raw=None
        requested_digest=hashlib.sha256(requested_raw).hexdigest() if requested_raw else None
        children=cli.get_children(parent)
        for item in children:
            d=item.get('data') or item;key=item.get('key') or d.get('key')
            if not key or not KEY_RE.fullmatch(key) or d.get('deleted') or d.get('itemType')!='attachment' or d.get('parentItem')!=parent:continue
            if d.get('contentType')!='application/pdf' and not (d.get('filename') or '').lower().endswith('.pdf'):continue
            try:
                native_path=cli.attachment_file(key);raw=checked_file(native_path)
                if key==prior.get('attachment_key') and prior.get('state')=='pending' and prior.get('sha256') and (requested_digest is None or requested_digest==prior['sha256']) and hashlib.sha256(raw).hexdigest()!=prior['sha256']:
                    return record(pid,'pending','现有附件与待保存文件不同，请核对后重试；未覆盖附件',key,prior['sha256'])
                if requested_digest and hashlib.sha256(raw).hexdigest()!=requested_digest:continue
                from .pdf_storage import bind_zotero
                bind_zotero(pid,native_path)
                return record(pid,'existing','PDF已在Zotero中，已核对文件',key,hashlib.sha256(raw).hexdigest(),str(native_path))
            except Exception:pass
            tags=[t.get('tag') for t in d.get('tags',[]) if isinstance(t,dict)]
            if (key==prior.get('attachment_key') or MARKER in tags) and d.get('linkMode') in ('imported_file','imported_url') and (requested_digest is None or key==prior.get('attachment_key') and prior.get('sha256') in (None,requested_digest)):attachment=(key,d)
        # Prefer an explicitly associated local file, then lock-safe Zotero
        # attachment lookup, then an accessible public full-text source.
        source=pdf_access.paper(pid);path=source.get('local_pdf_path')
        try:raw=checked_file(path) if path else None
        except Exception:raw=None
        if raw is None:
            stage='acquire'
            found=pdf_resolver.acquire(pid)
            source=pdf_access.paper(pid);path=source.get('local_pdf_path')
            if not found.get('ready') or not path:
                return record(pid,'missing','书目已保存；已自动查找，未取得可访问的PDF全文',attachment[0] if attachment else None)
            raw=checked_file(path)
        filename='paper-'+str(pid)+'.pdf';digest=hashlib.sha256(raw).hexdigest()
        stage='create'
        if attachment:key,data=attachment
        else:
            result=cli.create_items([{'itemType':'attachment','parentItem':parent,'linkMode':'imported_file','title':'全文 PDF','contentType':'application/pdf','filename':filename,'tags':[{'tag':MARKER}]}])
            key=(result.get('success') or {}).get('0')
            if not key or not KEY_RE.fullmatch(key):raise ValueError('附件条目未创建')
            data=(cli.get_item(key) or {}).get('data') or {}
        if data.get('parentItem')!=parent or data.get('itemType')!='attachment':raise ValueError('附件归属未核对')
        record(pid,'pending','书目已保存；PDF正在传输或等待重试',key,digest)
        stage='upload';cli.upload_pdf_attachment(key,raw,filename,Path(path).stat().st_mtime_ns//1000000,data.get('md5'))
        stage='readback'
        try:
            actual=cli.get_item(key);got=(actual or {}).get('data') or {}
            if got.get('parentItem')!=parent or got.get('contentType')!='application/pdf':raise ValueError('附件归属未核对')
            native_path=cli.attachment_file(key);readback=checked_file(native_path)
            if hashlib.sha256(readback).hexdigest()!=digest:raise ValueError('附件内容未核对')
        except Exception:
            # A Zotero attachment manager can replace the just-uploaded key.
            # Recover only under this exact parent and with identical bytes.
            from .zotero_attachments import find_native
            recovered=find_native(source,expected_digest=digest,parent_key=parent)
            if not recovered:raise
            return status(pid)
        from .pdf_storage import bind_zotero
        bind_zotero(pid,native_path)
        return record(pid,'saved','PDF已保存并回读核对',key,digest,str(native_path))
    except Exception:
        # Do not put transport bodies, upload tickets or private paths in status.
        old=status(pid)
        messages={'check':'暂时无法核对 Zotero 附件，请检查本机连接后重试','acquire':'原文获取未完成，可重新获取或上传已有 PDF','create':'Zotero 附件条目创建未完成，请检查写入授权后重试','upload':'PDF 传输未完成，本地原文保留，可重新同步','readback':'PDF 已传输，尚未完成回读核对，请稍后重新同步'}
        return record(pid,'pending','书目已保存；'+messages[stage],old.get('attachment_key'),old.get('sha256'))
