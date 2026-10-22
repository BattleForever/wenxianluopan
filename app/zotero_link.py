"""Product-facing local authorization, collection picker and verified moves."""
import threading,uuid
from datetime import datetime,timezone
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from .zotero_client import ZoteroClient,ZoteroError,ZoteroAuthRequired,ZoteroUnavailable,KEY_RE
from .private_settings import atomic_json,read_json,LOCK
from .config import DATA_DIR
from .db import get_db
from . import zotero_sync,trash

router=APIRouter(prefix='/api/zotero-link')
_auth={};_guard=threading.Lock();_sync_guard=threading.Lock()

def init():
    with get_db() as c:c.execute('''CREATE TABLE IF NOT EXISTS zotero_destinations(
       paper_id INTEGER PRIMARY KEY,collection_key TEXT,path TEXT,item_key TEXT,verified_at TEXT)''')

def client():
    with LOCK:saved=read_json(DATA_DIR/'private'/'zotero.local.json')
    return ZoteroClient(api_key=saved.get('api_key'))

def collection_rows(cli):
    raw=cli.get_collections();rows=[];by={}
    for item in raw:
        d=item.get('data') or item;k=item.get('key') or d.get('key')
        if not k or not KEY_RE.fullmatch(k):continue
        by[k]={'key':k,'name':d.get('name') or '未命名集合','parent':d.get('parentCollection') or None}
    for k,item in by.items():
        names=[];seen=set();node=item
        while node and node['key'] not in seen:
            seen.add(node['key']);names.insert(0,node['name']);node=by.get(node['parent'])
        rows.append({**item,'path':' / '.join(names)})
    return sorted(rows,key=lambda r:r['path'].casefold())

@router.get('/status/{pid}')
def status(pid:int):
    with get_db() as c:
        row=c.execute('SELECT * FROM zotero_destinations WHERE paper_id=?',(pid,)).fetchone()
        sync=zotero_sync.sync_status(c,paper_id=pid,limit=1)
    cli=client()
    try:available=cli.ping()['ok']
    except ZoteroError:available=False
    return {'available':available,'authorized':bool(cli.api_key),'destination':dict(row) if row else None,
            'record':next(iter(sync['records']),None)}

@router.get('/collections')
def collections():
    try:return {'collections':collection_rows(client()),'root_label':'我的文献库（根目录）'}
    except ZoteroError:raise HTTPException(503,'未连接Zotero，请启动桌面端并允许本机应用连接')

@router.post('/authorize')
def authorize():
    with _guard:
        for jid,job in _auth.items():
            if job['state']=='waiting':return {'id':jid,**job}
        jid=uuid.uuid4().hex;_auth[jid]={'state':'waiting','message':'请在Zotero桌面弹窗中确认连接'}
        # Bounded descriptors; never retain any response/key in job status.
        while len(_auth)>10:_auth.pop(next(iter(_auth)))
    def work():
        try:
            cli=client();key=cli.authorize('文献罗盘',timeout=120)
            if not key:raise ZoteroAuthRequired()
            with LOCK:atomic_json(DATA_DIR/'private'/'zotero.local.json',{'api_key':key})
            outcome={'state':'authorized','message':'连接已授权，可以选择集合并同步'}
        except Exception:outcome={'state':'failed','message':'授权未完成，请确认Zotero正在运行并在弹窗中允许连接'}
        with _guard:_auth[jid]=outcome
    threading.Thread(target=work,daemon=True).start()
    return {'id':jid,'state':'waiting','message':'请在Zotero桌面弹窗中确认连接'}

@router.get('/authorize/{jid}')
def authorization(jid:str):
    with _guard:
        result=_auth.get(jid)
        if not result:raise HTTPException(404,'授权会话已结束，请重新连接')
        return {'id':jid,**result}

class Sync(BaseModel):
    paper_id:int=Field(gt=0)
    collection_key:str|None=Field(default=None,pattern=r'^[A-Za-z0-9]{1,32}$')
    move:bool=False

@router.post('/sync')
def sync(body:Sync):
    if not _sync_guard.acquire(blocking=False):raise HTTPException(409,'另一篇文献正在同步，请稍后重试')
    try:
        cli=client()
        try:rows=collection_rows(cli)
        except ZoteroError:raise HTTPException(503,'未连接Zotero，请先启动桌面端')
        destination=next((r for r in rows if r['key']==body.collection_key),None)
        if body.collection_key and not destination:raise HTTPException(409,'目标集合已不存在，请重新选择')
        if not cli.api_key:raise HTTPException(401,'请先点击“连接Zotero”并确认桌面授权')
        with get_db() as c:
            trash.ensure_active(c,[body.paper_id])
            old=c.execute('SELECT * FROM zotero_destinations WHERE paper_id=?',(body.paper_id,)).fetchone()
            result=zotero_sync.sync_one_paper(c,cli,body.paper_id,body.collection_key)
            if result.get('zotero_state')!='synced':
                return {**result,'destination':dict(old) if old else None,'message':'同步尚未完成；请核对连接或冲突提示后重试'}
            mapped=c.execute("SELECT zotero_key FROM zotero_item_map WHERE paper_id=? AND kind='item'",(body.paper_id,)).fetchone()
            if not mapped:raise HTTPException(502,'同步条目未返回可核对标识，请重试')
            key=mapped[0]
            # Remove only the previous app-managed membership. Other Zotero
            # collections, user notes and attachments remain untouched.
            if body.move and old and old['collection_key']!=body.collection_key and old['collection_key']:
                item=cli.get_item(key);d=item.get('data') or {};groups=list(d.get('collections') or [])
                if old['collection_key'] in groups:
                    groups.remove(old['collection_key']);cli.update_item(key,{'collections':groups},version=d.get('version'))
                got=cli.get_item(key);remaining=(got.get('data') or {}).get('collections') or []
                if old['collection_key'] in remaining or body.collection_key and body.collection_key not in remaining:
                    raise HTTPException(502,'目标已添加，但旧位置迁移未核验成功；原位置记录保留，可重试')
            path=destination['path'] if destination else '我的文献库（根目录）'
            c.execute('INSERT OR REPLACE INTO zotero_destinations VALUES(?,?,?,?,?)',(body.paper_id,body.collection_key,path,key,datetime.now(timezone.utc).isoformat()))
        return {**result,'destination':{'collection_key':body.collection_key,'path':path,'item_key':key},'message':'已同步并回读核验'}
    except ZoteroAuthRequired:raise HTTPException(401,'本地授权已失效，请重新连接Zotero')
    except ZoteroError:raise HTTPException(502,'Zotero未完成此操作，请刷新集合并重试；已有内容保留')
    finally:_sync_guard.release()
