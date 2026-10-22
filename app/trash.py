"""Scoped, reversible deletion and expiry to an OS-recycled recovery bundle.

No bibliography, PDF, Zotero original or evidence row is hard-deleted here.
Archived rows are retained as a secondary local recovery index, independently
of Windows Recycle Bin retention. Only generated bundles enter the OS bin.
"""
import asyncio
import json
import logging
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .config import DATA_DIR
from .db import get_db
from .system_recycle import recycle_file

router = APIRouter(prefix='/api/trash')
Scope = Literal['library', 'reading']
_lock = threading.Lock()
log = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def init():
    with get_db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS paper_trash(
          id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
          paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
          deleted_at TEXT NOT NULL, snapshot TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',recycled_at TEXT,error TEXT,
          bundle_name TEXT, UNIQUE(scope,paper_id));
        CREATE INDEX IF NOT EXISTS trash_due ON paper_trash(state,deleted_at);
        CREATE TABLE IF NOT EXISTS trash_settings(scope TEXT PRIMARY KEY,days INTEGER NOT NULL);
        INSERT OR IGNORE INTO trash_settings VALUES('library',30),('reading',30);
        ''')


def hidden(c, scope=None):
    """Library trash is hidden in all views; reading trash only in that list."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trash'").fetchone():return set()
    scopes = ('library','reading') if scope == 'reading' else ('library',)
    return {r[0] for r in c.execute('SELECT paper_id FROM paper_trash WHERE scope IN ('+','.join('?'*len(scopes))+')', scopes)}


def ensure_active(c, ids, scope='library'):
    if set(ids) & hidden(c,scope):
        raise HTTPException(409,'文献已在回收站，请先恢复后再操作')


def folder_snapshot(c, pid, scope):
    result=[]
    for item in c.execute('SELECT f.* FROM organizer_items i JOIN organizer_folders f ON f.id=i.folder_id WHERE i.paper_id=? AND f.scope=?',(pid,scope)):
        chain=[]; node=dict(item); seen=set()
        while node and node['id'] not in seen:
            seen.add(node['id']);chain.insert(0,{k:node[k] for k in ('id','name','color')})
            parent=c.execute('SELECT * FROM organizer_folders WHERE id=?',(node['parent_id'],)).fetchone()
            node=dict(parent) if parent else None
        result.append(chain)
    return result


class Put(BaseModel):
    scope: Scope
    ids: list[int] = Field(min_length=1,max_length=500)


@router.post('/items')
def put(body:Put):
    from . import folders
    affected=0
    with get_db() as c:
        c.execute('BEGIN IMMEDIATE')
        ids=list(dict.fromkeys(body.ids))
        for pid in ids:
            if not c.execute('SELECT 1 FROM papers WHERE id=?',(pid,)).fetchone():
                raise HTTPException(404,'部分文献已不存在，请刷新后重试')
        for pid in ids:
            if c.execute('SELECT 1 FROM paper_trash WHERE scope=? AND paper_id=?',(body.scope,pid)).fetchone():continue
            reading=c.execute('SELECT * FROM reading_list WHERE paper_id=?',(pid,)).fetchone()
            if body.scope=='reading' and not reading:continue
            if body.scope=='reading':ensure_active(c,[pid])
            snap={'folders':folder_snapshot(c,pid,body.scope),'reading':dict(reading) if reading else None}
            c.execute('INSERT INTO paper_trash(scope,paper_id,deleted_at,snapshot) VALUES(?,?,?,?)',
                      (body.scope,pid,now().isoformat(),json.dumps(snap,ensure_ascii=False)))
            affected+=1
        if affected:folders.invalidate(c)
    return {'affected':affected,'scope':body.scope}


class Retention(BaseModel):
    days:int=Field(ge=0,le=3650)


@router.patch('/settings/{scope}')
def settings(scope:Scope, body:Retention):
    with get_db() as c:
        c.execute('UPDATE trash_settings SET days=? WHERE scope=?',(body.days,scope))
    return {'scope':scope,'days':body.days}


@router.get('/items')
def items(scope:Scope='library', archived:bool=False):
    with get_db() as c:
        days=c.execute('SELECT days FROM trash_settings WHERE scope=?',(scope,)).fetchone()[0]
        rows=[dict(r) for r in c.execute('SELECT t.*,p.title,p.title_zh,p.doi,p.year FROM paper_trash t JOIN papers p ON p.id=t.paper_id WHERE t.scope=? AND t.state=? ORDER BY t.deleted_at DESC,t.id DESC',(scope,'recycled' if archived else 'pending'))]
    for r in rows:
        snap=json.loads(r.pop('snapshot'));r['paths']=[' / '.join(x['name'] for x in chain) for chain in snap['folders']]
        due=datetime.fromisoformat(r['deleted_at'])+timedelta(days=days) if days else None
        r['due_at']=due.isoformat() if due else None
        r['remaining_days']=max(0,(due-now()).total_seconds()/86400) if due else None
    return {'scope':scope,'days':days,'items':rows,'total':len(rows),'archived':archived}


def restore_folders(c,pid,scope,chains):
    from . import folders
    for chain in chains:
        parent=None
        for part in chain:
            # Reuse a surviving original folder even if the user renamed/moved it.
            original=c.execute('SELECT * FROM organizer_folders WHERE id=? AND scope=?',(part['id'],scope)).fetchone()
            if original:
                parent=original['id'];continue
            existing=c.execute('SELECT id FROM organizer_folders WHERE scope=? AND parent_id IS ? AND name_key=?',(scope,parent,part['name'].casefold())).fetchone()
            if existing:parent=existing['id'];continue
            folders.validate_parent(c,scope,parent)
            stamp=now().isoformat()
            parent=c.execute('INSERT INTO organizer_folders(scope,parent_id,name,name_key,color,position,created_at,updated_at) VALUES(?,?,?,?,?,9999,?,?)',
                (scope,parent,part['name'],part['name'].casefold(),part['color'],stamp,stamp)).lastrowid
        if parent:c.execute('INSERT OR IGNORE INTO organizer_items VALUES(?,?)',(parent,pid))


class Restore(BaseModel):
    scope:Scope
    ids:list[int]=Field(min_length=1,max_length=500)


@router.post('/restore')
def restore(body:Restore):
    from . import folders
    with get_db() as c:
        c.execute('BEGIN IMMEDIATE')
        affected=restore_in(c,body.scope,body.ids)
        if affected:folders.invalidate(c)
    return {'restored':affected,'scope':body.scope}


def restore_in(c,scope,ids):
    affected=0
    for pid in set(ids):
        r=c.execute('SELECT * FROM paper_trash WHERE scope=? AND paper_id=?',(scope,pid)).fetchone()
        if not r:continue
        if scope=='reading':ensure_active(c,[pid])
        snap=json.loads(r['snapshot']);restore_folders(c,pid,scope,snap['folders'])
        if scope=='reading' and snap['reading']:
            old=snap['reading'];c.execute('INSERT OR IGNORE INTO reading_list(paper_id,note,added_at) VALUES(?,?,?)',(pid,old['note'],old['added_at']))
        c.execute('DELETE FROM paper_trash WHERE id=?',(r['id'],));affected+=1
    return affected


def bundle(c, row):
    """User content only: never include settings, API profiles or request logs."""
    pid=row['paper_id'];paper=dict(c.execute('SELECT * FROM papers WHERE id=?',(pid,)).fetchone())
    directory=DATA_DIR/'recycle_staging';directory.mkdir(parents=True,exist_ok=True)
    if row['bundle_name'] and Path(row['bundle_name']).name==row['bundle_name']:
        previous=directory/row['bundle_name']
        if previous.is_file():
            try:
                with zipfile.ZipFile(previous) as z:
                    saved=json.loads(z.read('recovery.json'))
                    if z.testzip() is None and saved['paper']==paper and saved['deleted_at']==row['deleted_at']:return previous
            except (OSError,ValueError,KeyError,zipfile.BadZipFile):pass
    content={'format':'wenxianluopan-recovery-1','scope':row['scope'],'paper':paper,
             'deleted_at':row['deleted_at'],'snapshot':json.loads(row['snapshot']),'tables':{}}
    names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('reading_list','content_extra','content_provenance','metadata_field_sources','ai_briefs','pdf_guides','pdf_guide_sources','pdf_guide_source_texts'):
        if table in names:
            content['tables'][table]=[dict(r) for r in c.execute('SELECT * FROM '+table+' WHERE paper_id=?',(pid,))]
    content['tables']['citations']=[dict(r) for r in c.execute('SELECT * FROM citations WHERE from_id=? OR to_id=?',(pid,pid))]
    target=directory/f'文献罗盘_{row["scope"]}_{pid}_{uuid.uuid4().hex}.wxl-trash.zip'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('recovery.json',json.dumps(content,ensure_ascii=False,indent=2))
        z.writestr('恢复说明.txt','在电脑回收站中还原本文件。软件仍保留恢复索引，可在对应回收站的“已转出记录”中恢复文献、原分类和阅读备注。原始 PDF 和 Zotero 原库未移动。此文件含个人阅读内容，请勿上传。')
        # Reading-list removal never copies/removes a shared PDF. Library bundles
        # include a copy if available, leaving every original untouched.
        source=Path(paper.get('local_pdf_path') or '')
        if row['scope']=='library' and source.is_file() and source.suffix.lower()=='.pdf':
            z.write(source,'document.pdf')
    with zipfile.ZipFile(target) as z:
        if z.testzip() is not None or json.loads(z.read('recovery.json'))['paper']['id']!=pid:
            raise OSError('恢复包校验失败，内容仍保留')
    return target


def sweep(force_scope=None, ids=None):
    """Called on startup/hourly or explicitly. Failures retain every record."""
    if not _lock.acquire(blocking=False):return {'recycled':0,'failed':0,'busy':True}
    result={'recycled':0,'failed':0}
    try:
        with get_db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("SELECT t.*,s.days FROM paper_trash t JOIN trash_settings s ON s.scope=t.scope WHERE state='pending'").fetchall()
            for row in rows:
                explicit=force_scope==row['scope'] and (ids is None or row['paper_id'] in ids)
                due=row['days']>0 and datetime.fromisoformat(row['deleted_at'])+timedelta(days=row['days'])<=now()
                if not explicit and not due:continue
                try:
                    path=bundle(c,row);c.execute('UPDATE paper_trash SET bundle_name=? WHERE id=?',(path.name,row['id']))
                    recycle_file(path)
                    c.execute("UPDATE paper_trash SET state='recycled',recycled_at=?,error=NULL WHERE id=?",(now().isoformat(),row['id']))
                    result['recycled']+=1
                except Exception:
                    # Never persist system exceptions or paths that may carry user information.
                    c.execute('UPDATE paper_trash SET error=? WHERE id=?',('转入电脑回收站未成功，文献及恢复包仍保留；可稍后重试',row['id']))
                    result['failed']+=1
    finally:_lock.release()
    return result


@router.post('/recycle')
def recycle(body:Put):
    return sweep(body.scope,set(body.ids))


async def maintenance():
    while True:
        try:await asyncio.to_thread(sweep)
        except Exception:log.warning('回收站到期检查未完成，将在下一轮重试')
        await asyncio.sleep(3600)
