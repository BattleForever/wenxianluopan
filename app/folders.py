"""Independent library/reading collections, transactional moves and undo."""
import json,re,sqlite3,unicodedata,uuid
from datetime import datetime,timezone,timedelta
from typing import Literal
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from .db import get_db

router=APIRouter(prefix='/api/organizer')
Scope=Literal['library','reading']
MAX_DEPTH=8

def now():return datetime.now(timezone.utc).isoformat()

def init():
    with get_db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS organizer_folders(
          id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,
          parent_id INTEGER REFERENCES organizer_folders(id) ON DELETE CASCADE,
          name TEXT NOT NULL,name_key TEXT NOT NULL,color TEXT NOT NULL DEFAULT '#62b9ce',
          position INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS organizer_names ON organizer_folders(scope,COALESCE(parent_id,0),name_key);
        CREATE INDEX IF NOT EXISTS organizer_parent ON organizer_folders(scope,parent_id,position);
        CREATE TABLE IF NOT EXISTS organizer_items(
          folder_id INTEGER NOT NULL REFERENCES organizer_folders(id) ON DELETE CASCADE,
          paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
          PRIMARY KEY(folder_id,paper_id));
        CREATE INDEX IF NOT EXISTS organizer_paper ON organizer_items(paper_id,folder_id);
        CREATE TABLE IF NOT EXISTS organizer_legacy(collection_id INTEGER PRIMARY KEY,
          folder_id INTEGER REFERENCES organizer_folders(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS organizer_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
        INSERT OR IGNORE INTO organizer_meta VALUES('revision',0);
        CREATE TABLE IF NOT EXISTS organizer_undo(token TEXT PRIMARY KEY,revision INTEGER NOT NULL,
          scope TEXT NOT NULL,payload TEXT NOT NULL,expires_at TEXT NOT NULL);
        ''')
        migrate_legacy(c)

def clean_name(name):
    name=unicodedata.normalize('NFC',name.strip())
    if not name or len(name)>80 or re.search(r'[\x00-\x1f\x7f/\\]',name):raise HTTPException(400,'文件夹名称需为1–80字，不能包含斜杠或控制字符')
    return name,name.casefold()

def migrate_legacy(c):
    for row in c.execute('SELECT * FROM collections WHERE id NOT IN (SELECT collection_id FROM organizer_legacy) ORDER BY id').fetchall():
        base=(row['name'] or '导入分类').strip()[:70].replace('/','／').replace('\\','＼')
        base=re.sub(r'[\x00-\x1f\x7f]','',base) or '导入分类';name=base;index=2
        while c.execute("SELECT 1 FROM organizer_folders WHERE scope='library' AND parent_id IS NULL AND name_key=?",(name.casefold(),)).fetchone():name=f'{base} ({index})';index+=1
        position=c.execute("SELECT COALESCE(MAX(position),-1)+1 FROM organizer_folders WHERE scope='library' AND parent_id IS NULL").fetchone()[0]
        stamp=now();fid=c.execute("INSERT INTO organizer_folders(scope,name,name_key,position,created_at,updated_at) VALUES('library',?,?,?,?,?)",(name,name.casefold(),position,stamp,stamp)).lastrowid
        c.execute('INSERT INTO organizer_legacy VALUES(?,?)',(row['id'],fid))
        c.execute('INSERT OR IGNORE INTO organizer_items SELECT ?,i.paper_id FROM collection_items i JOIN papers p ON p.id=i.paper_id WHERE i.collection_id=?',(fid,row['id']))
        invalidate(c)

def get(c,fid):
    row=c.execute('SELECT * FROM organizer_folders WHERE id=?',(fid,)).fetchone()
    if not row:raise HTTPException(404,'文件夹不存在，请刷新后重试')
    return dict(row)

def descendants(c,fid):
    return [r[0] for r in c.execute('WITH RECURSIVE tree(id) AS (SELECT id FROM organizer_folders WHERE id=? UNION SELECT f.id FROM organizer_folders f JOIN tree t ON f.parent_id=t.id) SELECT id FROM tree',(fid,))]

def depth(c,fid):
    seen=set()
    while fid is not None:
        if fid in seen:raise HTTPException(409,'文件夹层级异常，请刷新后重试')
        seen.add(fid);fid=get(c,fid)['parent_id']
    return len(seen)

def validate_parent(c,scope,parent,fid=None):
    if parent is not None and get(c,parent)['scope']!=scope:raise HTTPException(400,'文件夹只能在同一工作区内移动')
    if fid is not None and parent in descendants(c,fid):raise HTTPException(400,'不能把文件夹移入自身或其子文件夹')
    height=max([depth(c,x)-depth(c,fid)+1 for x in descendants(c,fid)],default=1) if fid else 1
    if depth(c,parent)+height>MAX_DEPTH:raise HTTPException(400,f'文件夹最多支持{MAX_DEPTH}层')

def invalidate(c):
    c.execute("UPDATE organizer_meta SET value=value+1 WHERE key='revision'")

def snapshot(c,scope,ids=()):
    return {'folders':[dict(r) for r in c.execute('SELECT * FROM organizer_folders WHERE scope=?',(scope,))],
            'items':[dict(r) for r in c.execute('SELECT i.* FROM organizer_items i JOIN organizer_folders f ON f.id=i.folder_id WHERE f.scope=?',(scope,))],
            'legacy':[dict(r) for r in c.execute('SELECT l.* FROM organizer_legacy l JOIN organizer_folders f ON f.id=l.folder_id WHERE f.scope=?',(scope,))],
            'reading':{str(pid):dict(r) if (r:=c.execute('SELECT * FROM reading_list WHERE paper_id=?',(pid,)).fetchone()) else None for pid in ids} if scope=='reading' else {},
            'trash':{str(pid):dict(r) if (r:=c.execute('SELECT * FROM paper_trash WHERE paper_id=? AND scope=?',(pid,scope)).fetchone()) else None for pid in ids} if scope=='reading' else {}}

def remember(c,scope,before):
    invalidate(c);rev=c.execute("SELECT value FROM organizer_meta WHERE key='revision'").fetchone()[0]
    token=uuid.uuid4().hex
    c.execute('DELETE FROM organizer_undo WHERE expires_at<?',(now(),))
    c.execute('INSERT INTO organizer_undo VALUES(?,?,?,?,?)',(token,rev,scope,json.dumps(before,ensure_ascii=False),(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()))
    c.execute('DELETE FROM organizer_undo WHERE rowid NOT IN (SELECT rowid FROM organizer_undo ORDER BY rowid DESC LIMIT 20)')
    return token

def filtered_ids(c,scope,fid=None,unfiled=False,recursive=True):
    if fid is not None and unfiled:raise HTTPException(400,'文件夹和未分类不能同时筛选')
    if not fid and not unfiled:return None
    if scope not in ('library','reading'):raise HTTPException(400,'请先选择文献库或阅读清单')
    if unfiled:
        return {r[0] for r in c.execute('SELECT p.id FROM papers p WHERE NOT EXISTS(SELECT 1 FROM organizer_items i JOIN organizer_folders f ON f.id=i.folder_id WHERE i.paper_id=p.id AND f.scope=?)',(scope,))}
    if get(c,fid)['scope']!=scope:raise HTTPException(400,'文件夹不属于当前工作区')
    ids=descendants(c,fid) if recursive else [fid]
    return {r[0] for r in c.execute('SELECT DISTINCT paper_id FROM organizer_items WHERE folder_id IN ('+','.join('?'*len(ids))+')',ids)}

@router.get('/folders')
def list_folders(scope:Scope='library'):
    from . import trash
    with get_db() as c:
        migrate_legacy(c)
        rows=[dict(r) for r in c.execute('SELECT * FROM organizer_folders WHERE scope=? ORDER BY position,name_key,id',(scope,))]
        valid={r[0] for r in c.execute("SELECT id FROM papers WHERE status='library'")} if scope=='library' else {r[0] for r in c.execute('SELECT r.paper_id FROM reading_list r JOIN papers p ON p.id=r.paper_id')}
        valid-=trash.hidden(c,scope)
        direct={r['id']:set() for r in rows}
        for r in c.execute('SELECT i.* FROM organizer_items i JOIN organizer_folders f ON f.id=i.folder_id WHERE f.scope=?',(scope,)):
            if r['paper_id'] in valid:direct[r['folder_id']].add(r['paper_id'])
        totals={k:set(v) for k,v in direct.items()};lookup={r['id']:r for r in rows}
        for row in sorted(rows,key=lambda x:depth(c,x['id']),reverse=True):
            if row['parent_id'] in totals:totals[row['parent_id']].update(totals[row['id']])
        for r in rows:r.update(direct_count=len(direct[r['id']]),count=len(totals[r['id']]))
        assigned=set().union(*direct.values()) if direct else set()
        rev=c.execute("SELECT value FROM organizer_meta WHERE key='revision'").fetchone()[0]
        trash_count=c.execute("SELECT COUNT(*) FROM paper_trash WHERE scope=? AND state='pending'",(scope,)).fetchone()[0]
    return {'scope':scope,'folders':rows,'total':len(valid),'unfiled':len(valid-assigned),'revision':rev,'trash_count':trash_count}

class Create(BaseModel):
    scope:Scope
    name:str=Field(min_length=1,max_length=80)
    parent_id:int|None=Field(default=None,gt=0)
    color:str=Field(default='#62b9ce',pattern=r'^#[0-9a-fA-F]{6}$')

class Edit(BaseModel):
    name:str|None=Field(default=None,min_length=1,max_length=80)
    color:str|None=Field(default=None,pattern=r'^#[0-9a-fA-F]{6}$')
    parent_id:int|None=Field(default=None,gt=0)

@router.post('/folders')
def create(body:Create):
    name,key=clean_name(body.name)
    try:
        with get_db() as c:
            c.execute('BEGIN IMMEDIATE');validate_parent(c,body.scope,body.parent_id);before=snapshot(c,body.scope)
            pos=c.execute('SELECT COALESCE(MAX(position),-1)+1 FROM organizer_folders WHERE scope=? AND parent_id IS ?',(body.scope,body.parent_id)).fetchone()[0]
            stamp=now();fid=c.execute('INSERT INTO organizer_folders(scope,parent_id,name,name_key,color,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(body.scope,body.parent_id,name,key,body.color,pos,stamp,stamp)).lastrowid
            result=get(c,fid);token=remember(c,body.scope,before)
    except sqlite3.IntegrityError:raise HTTPException(409,'同一位置已有同名文件夹，请换一个名称')
    return {'folder':result,'undo_token':token}

@router.patch('/folders/{fid}')
def edit(fid:int,body:Edit):
    try:
        with get_db() as c:
            c.execute('BEGIN IMMEDIATE');f=get(c,fid);before=snapshot(c,f['scope'])
            name,key=clean_name(body.name) if body.name is not None else (f['name'],f['name_key'])
            parent=body.parent_id if 'parent_id' in body.model_fields_set else f['parent_id'];validate_parent(c,f['scope'],parent,fid)
            position=f['position']
            if parent!=f['parent_id']:position=c.execute('SELECT COALESCE(MAX(position),-1)+1 FROM organizer_folders WHERE scope=? AND parent_id IS ?',(f['scope'],parent)).fetchone()[0]
            c.execute('UPDATE organizer_folders SET name=?,name_key=?,color=?,parent_id=?,position=?,updated_at=? WHERE id=?',(name,key,body.color or f['color'],parent,position,now(),fid))
            result=get(c,fid);token=remember(c,f['scope'],before)
    except sqlite3.IntegrityError:raise HTTPException(409,'同一位置已有同名文件夹')
    return {'folder':result,'undo_token':token}

class Move(BaseModel):
    parent_id:int|None=Field(default=None,gt=0)
    before_id:int|None=Field(default=None,gt=0)

@router.post('/folders/{fid}/move')
def move(fid:int,body:Move):
    try:
        with get_db() as c:
            c.execute('BEGIN IMMEDIATE');f=get(c,fid);validate_parent(c,f['scope'],body.parent_id,fid)
            if body.before_id is not None:
                target=get(c,body.before_id)
                if target['scope']!=f['scope'] or target['parent_id']!=body.parent_id or body.before_id==fid:raise HTTPException(400,'排序目标无效')
            before=snapshot(c,f['scope'])
            c.execute('UPDATE organizer_folders SET parent_id=?,updated_at=? WHERE id=?',(body.parent_id,now(),fid))
            siblings=[r[0] for r in c.execute('SELECT id FROM organizer_folders WHERE scope=? AND parent_id IS ? AND id!=? ORDER BY position,id',(f['scope'],body.parent_id,fid))]
            siblings.insert(siblings.index(body.before_id) if body.before_id else len(siblings),fid)
            c.executemany('UPDATE organizer_folders SET position=? WHERE id=?',[(i,x) for i,x in enumerate(siblings)])
            token=remember(c,f['scope'],before)
    except sqlite3.IntegrityError:raise HTTPException(409,'目标位置已有同名文件夹')
    return {'folder':get_folder(fid),'undo_token':token}

def get_folder(fid):
    with get_db() as c:return get(c,fid)

@router.delete('/folders/{fid}')
def delete(fid:int,keep_children:bool=False):
    try:
        with get_db() as c:
            c.execute('BEGIN IMMEDIATE');f=get(c,fid);before=snapshot(c,f['scope'])
            affected=1 if keep_children else len(descendants(c,fid))
            if keep_children:
                # A uniqueness collision aborts the whole transaction.
                c.execute('UPDATE organizer_folders SET parent_id=? WHERE parent_id=?',(f['parent_id'],fid))
            c.execute('DELETE FROM organizer_folders WHERE id=?',(fid,));token=remember(c,f['scope'],before)
    except sqlite3.IntegrityError:raise HTTPException(409,'保留子文件夹会产生同名冲突；请先重命名或移动子文件夹')
    return {'deleted':affected,'papers_deleted':0,'undo_token':token}

class Items(BaseModel):
    ids:list[int]=Field(min_length=1,max_length=500)
    folder_ids:list[int]=Field(default_factory=list,max_length=50)
    scope:Scope
    action:Literal['add','move','remove']='add'
    source_folder_id:int|None=Field(default=None,gt=0)
    source_descendants:bool=False

@router.post('/items')
def assign(body:Items):
    from . import trash
    with get_db() as c:
        c.execute('BEGIN IMMEDIATE');targets=list(dict.fromkeys(body.folder_ids))
        trash.ensure_active(c,body.ids)
        if not targets:raise HTTPException(400,'请选择至少一个文件夹')
        for fid in targets:
            if get(c,fid)['scope']!=body.scope:raise HTTPException(400,'目标文件夹与工作区不一致')
        if body.source_folder_id is not None and get(c,body.source_folder_id)['scope']!=body.scope:raise HTTPException(400,'来源文件夹与工作区不一致')
        if body.action=='move' and body.source_folder_id is None:raise HTTPException(400,'请从具体文件夹执行移动；全部文献视图请使用添加')
        ids=list(dict.fromkeys(body.ids))
        for pid in ids:
            if not c.execute('SELECT 1 FROM papers WHERE id=?',(pid,)).fetchone():raise HTTPException(404,'部分文献已不存在，请刷新后重试')
        before=snapshot(c,body.scope,ids)
        if body.action in ('add','move'):
            if body.scope=='library':c.executemany("UPDATE papers SET status='library' WHERE id=?",[(pid,) for pid in ids])
            else:
                trash.restore_in(c,'reading',ids)
                c.executemany('INSERT OR IGNORE INTO reading_list(paper_id) VALUES(?)',[(pid,) for pid in ids])
        if body.action=='move':
            origins=descendants(c,body.source_folder_id) if body.source_descendants else [body.source_folder_id]
            c.executemany('DELETE FROM organizer_items WHERE folder_id=? AND paper_id=?',[(fid,pid) for fid in origins for pid in ids])
        if body.action=='remove':
            origins=set(x for fid in targets for x in (descendants(c,fid) if body.source_descendants else [fid]))
            c.executemany('DELETE FROM organizer_items WHERE folder_id=? AND paper_id=?',[(fid,pid) for fid in origins for pid in ids])
        else:c.executemany('INSERT OR IGNORE INTO organizer_items VALUES(?,?)',[(fid,pid) for fid in targets for pid in ids])
        token=remember(c,body.scope,before)
    return {'affected':len(ids),'undo_token':token}

@router.post('/undo/{token}')
def undo(token:str):
    with get_db() as c:
        c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM organizer_undo WHERE token=?',(token,)).fetchone()
        if not r or r['expires_at']<now():raise HTTPException(410,'此操作已过期，无法撤销')
        rev=c.execute("SELECT value FROM organizer_meta WHERE key='revision'").fetchone()[0]
        if rev!=r['revision']:raise HTTPException(409,'此后已发生其他整理操作，不能覆盖后续更改')
        data=json.loads(r['payload']);c.execute('DELETE FROM organizer_folders WHERE scope=?',(r['scope'],))
        pending=list(data['folders']);done=set()
        while pending:
            ready=[f for f in pending if f['parent_id'] is None or f['parent_id'] in done]
            if not ready:raise HTTPException(409,'历史文件夹结构无效')
            for f in ready:
                columns=list(f);c.execute('INSERT INTO organizer_folders('+','.join(columns)+') VALUES('+','.join('?'*len(columns))+')',[f[k] for k in columns]);done.add(f['id']);pending.remove(f)
        c.executemany('INSERT INTO organizer_items VALUES(?,?)',[(x['folder_id'],x['paper_id']) for x in data['items'] if c.execute('SELECT 1 FROM papers WHERE id=?',(x['paper_id'],)).fetchone()])
        for x in data['legacy']:c.execute('UPDATE organizer_legacy SET folder_id=? WHERE collection_id=?',(x['folder_id'],x['collection_id']))
        for pid,row in data['reading'].items():
            if not c.execute('SELECT 1 FROM papers WHERE id=?',(pid,)).fetchone():continue
            c.execute('DELETE FROM reading_list WHERE paper_id=?',(pid,))
            if row:c.execute('INSERT INTO reading_list(paper_id,note,added_at) VALUES(?,?,?)',(row['paper_id'],row['note'],row['added_at']))
        for pid,row in data.get('trash',{}).items():
            if not c.execute('SELECT 1 FROM papers WHERE id=?',(pid,)).fetchone():continue
            c.execute('DELETE FROM paper_trash WHERE scope=? AND paper_id=?',(r['scope'],pid))
            if row:
                keys=list(row);c.execute('INSERT INTO paper_trash('+','.join(keys)+') VALUES('+','.join('?'*len(keys))+')',[row[k] for k in keys])
        c.execute('DELETE FROM organizer_undo WHERE token=?',(token,));invalidate(c)
    return {'restored':True,'scope':r['scope']}

def reading_changed(c,ids,removed=False):
    if removed:
        c.executemany("DELETE FROM organizer_items WHERE paper_id=? AND folder_id IN (SELECT id FROM organizer_folders WHERE scope='reading')",[(pid,) for pid in ids])
    invalidate(c)

def memberships(c):
    result={}
    for row in c.execute('SELECT i.paper_id,f.id,f.name,f.scope,f.color FROM organizer_items i JOIN organizer_folders f ON f.id=i.folder_id ORDER BY f.position,f.id'):
        r=dict(row);pid=r.pop('paper_id');result.setdefault(pid,[]).append(r)
    return result
