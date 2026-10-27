"""Local import jobs: readable progress, read-only Zotero source, short writes."""
import hashlib,json,threading,uuid
from datetime import datetime,timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from fastapi import APIRouter,HTTPException,Request,Query
from pydantic import BaseModel,Field
from . import importer,folders
from .db import get_db,get_setting
from .config import DATA_DIR
from .zotero_client import ZoteroClient,ZoteroError,KEY_RE

router=APIRouter(prefix='/api/import')
_queue_lock=threading.Lock();_start_lock=threading.Lock();ACTIVE={'queued','running','cancelling'}
def now():return datetime.now(timezone.utc).isoformat()

def init():
    with get_db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS import_jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS import_zotero_folders(collection_key TEXT PRIMARY KEY,folder_id INTEGER REFERENCES organizer_folders(id) ON DELETE SET NULL)')
        for row in c.execute('SELECT id,payload FROM import_jobs').fetchall():
            j=json.loads(row['payload'])
            if j['status'] in ACTIVE:
                j.update(status='interrupted',message='服务已重启，已入库的条目保留。可重新导入，重复文献会合并。');c.execute('UPDATE import_jobs SET payload=? WHERE id=?',(json.dumps(j,ensure_ascii=False),j['id']))

def save(j):
    with get_db() as c:
        previous=c.execute('SELECT payload FROM import_jobs WHERE id=?',(j['id'],)).fetchone()
        if previous and json.loads(previous[0])['status']=='cancelling' and j['status']=='running':j['status']='cancelling'
        c.execute('INSERT OR REPLACE INTO import_jobs VALUES(?,?)',(j['id'],json.dumps(j,ensure_ascii=False)))
def get(jid):
    with get_db() as c:row=c.execute('SELECT payload FROM import_jobs WHERE id=?',(jid,)).fetchone()
    if not row:raise HTTPException(404,'导入记录不存在')
    return json.loads(row[0])

class ImportIn(BaseModel):
    kind:Literal['doi','bibtex','ris','standard','pdf-folder','pdf','zotero']
    text:str=Field(default='',max_length=20_000_000)
    citations_text:str=Field(default='',max_length=20_000_000)
    venues_text:str=Field(default='',max_length=20_000_000)
    collection_key:str|None=Field(default=None,pattern=r'^[A-Za-z0-9]{1,32}$')
    item_keys:list[str]|None=Field(default=None,max_length=5000)
    include_children:bool=True
    preserve_folders:bool=True
    upload_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{64}$')
    destination_folder_ids:list[int]=Field(default_factory=list,max_length=50)

def validate_destination(c,ids):
    for fid in set(ids):
        if folders.get(c,fid)['scope']!='library':raise HTTPException(422,'导入位置必须是文献库文件夹，请重新选择。')

def apply_destination(c,paper_ids,folder_ids):
    validate_destination(c,folder_ids)
    if folder_ids and paper_ids:
        c.executemany('INSERT OR IGNORE INTO organizer_items(folder_id,paper_id) VALUES(?,?)',[(fid,pid) for fid in set(folder_ids) for pid in set(paper_ids)])
        folders.invalidate(c)

def list_items(collection_key=None,start=0,limit=50,query=''):
    if collection_key and not KEY_RE.fullmatch(collection_key):raise ValueError('集合标识无效')
    cli=ZoteroClient()
    # Local Zotero does not interpret a space-separated list of exclusions as
    # Boolean AND. Build a positive union from its own schema so attachments,
    # notes and annotations never consume a bibliographic page slot.
    _,_,schema=cli._request('GET','/api/itemTypes')
    kinds=sorted({x.get('itemType') for x in (schema or []) if isinstance(x,dict) and isinstance(x.get('itemType'),str) and importer.re.fullmatch(r'[A-Za-z]+',x['itemType']) and x['itemType'] not in ('attachment','note','annotation')})
    if not kinds:raise ValueError('无法读取 Zotero 文献类型，请重新连接')
    path='/api/users/0/'+('collections/'+collection_key+'/' if collection_key else '')+'items/top'
    params={'format':'json','start':start,'limit':limit,'itemType':' || '.join(kinds),'sort':'dateModified','direction':'desc'}
    doi=importer.norm_doi(query)
    if doi and importer.DOI_RE.fullmatch(doi):
        params.update(q=doi,qmode='everything',limit=100,start=0)
        matches=[]
        while True:
            _,headers,batch=cli._request('GET',path+'?'+urlencode(params))
            if not isinstance(batch,list):raise ValueError('Zotero 未返回文献列表')
            matches.extend(x for x in batch if importer.norm_doi((x.get('data') or {}).get('DOI'))==doi)
            if len(batch)<100:break
            params['start']+=len(batch)
        headers={k:v for k,v in headers.items() if k.lower()!='total-results'};headers['Total-Results']=str(len(matches))
        return matches[start:start+limit],headers
    if query:params.update(q=query,qmode='titleCreatorYear')
    _,headers,items=cli._request('GET',path+'?'+urlencode(params))
    if not isinstance(items,list):raise ValueError('Zotero 未返回文献列表，请重新连接')
    return items,headers

@router.get('/zotero/items')
def preview(collection_key:str|None=None,start:int=Query(0,ge=0),query:str=Query('',max_length=300),limit:int=Query(20,ge=1,le=100)):
    try:
        raw,headers=list_items(collection_key,start,limit,query)
        rows=[]
        for item in raw:
            p=importer._zotero_item_to_paper(item)
            if p:rows.append({'key':p['meta']['zotero_key'],'title':p['title'],'year':p['year'],'doi':p['doi'],'authors':p['authors'],'venue':p.get('venue'),'type':p.get('type')})
        total=next((int(v) for k,v in headers.items() if k.lower()=='total-results' and str(v).isdigit()),None)
        if total is None and len(raw)<limit:total=start+len(raw)
        from .import_locations import locations
        local=locations(rows)
        for row in rows:row['library']=local.get(row['key'])
        return {'items':rows,'start':start,'limit':limit,'has_more':start+len(raw)<total if total is not None else len(raw)==limit,'total':total}
    except (ZoteroError,ValueError):raise HTTPException(503,'无法读取 Zotero。请打开桌面端，并在“设置 → 高级”允许本机应用连接。')

def collection_rows():
    from .zotero_link import collection_rows as rows
    return rows(ZoteroClient())

class PreviewItem(BaseModel):
    key:str=Field(max_length=32)
    doi:str|None=Field(default=None,max_length=1024)
    title:str|None=Field(default=None,max_length=4000)
class LocationsIn(BaseModel):
    items:list[PreviewItem]=Field(max_length=100)
@router.post('/locations')
def refresh_locations(body:LocationsIn):
    from .import_locations import locations
    return {'locations':locations([x.model_dump() for x in body.items])}

def _folder(c,key,by,seen=None):
    seen=set(seen or ())
    if key in seen or key not in by or len(seen)>=folders.MAX_DEPTH:return None
    seen.add(key);row=by[key]
    old=c.execute('SELECT folder_id FROM import_zotero_folders WHERE collection_key=?',(key,)).fetchone()
    if old and old[0] and c.execute('SELECT 1 FROM organizer_folders WHERE id=?',(old[0],)).fetchone():return old[0]
    parent=_folder(c,row['parent'],by,seen) if row.get('parent') else None
    name=importer.re.sub(r'[\x00-\x1f\x7f/\\]',' ',row['name']).strip()[:80] or 'Zotero 集合'
    old=c.execute("SELECT id FROM organizer_folders WHERE scope='library' AND parent_id IS ? AND name_key=?",(parent,name.casefold())).fetchone()
    if old:fid=old[0]
    else:
        stamp=now();fid=c.execute("INSERT INTO organizer_folders(scope,parent_id,name,name_key,created_at,updated_at) VALUES('library',?,?,?,?,?)",(parent,name,name.casefold(),stamp,stamp)).lastrowid
    c.execute('INSERT OR REPLACE INTO import_zotero_folders VALUES(?,?)',(key,fid));return fid

def _zotero(body,j):
    rows=collection_rows();by={r['key']:r for r in rows};cli=ZoteroClient()
    stats={'total':0,'added':0,'updated':0,'with_pdf':0,'errors':[],'paper_ids':[]};j['result']=stats
    if body.collection_key and body.collection_key not in by:raise ValueError('集合已不存在，请刷新集合列表')
    selected=body.item_keys
    if selected is not None:
        if not selected or any(not KEY_RE.fullmatch(k) for k in selected):raise ValueError('请先勾选要导入的文献')
        selected=list(dict.fromkeys(selected));j['total']=len(selected);save(j)
        def chosen():
            for key in selected:
                if get(j['id'])['status']=='cancelling':return
                try:yield [cli.get_item(key)]
                except ZoteroError:
                    stats['total']+=1;j['completed']+=1;stats['errors'].append(key+'：未能读取此条目，其他文献继续导入。');save(j)
        batches=chosen()
    else:
        keys=[body.collection_key]
        if body.collection_key and body.include_children:
            for key in keys:
                keys.extend(r['key'] for r in rows if r['parent']==key and r['key'] not in keys)
        def source():
            for key in keys:
                start=0
                while True:
                    if get(j['id'])['status']=='cancelling':return
                    batch,_=list_items(key,start,100);yield batch
                    if len(batch)<100:break
                    start+=len(batch)
        batches=source()
    seen=set()
    for batch in batches:
        for item in batch:
            if get(j['id'])['status']=='cancelling':j['status']='cancelled';return
            p=importer._zotero_item_to_paper(item or {})
            if not p:continue
            key=p['meta']['zotero_key']
            if key in seen:continue
            seen.add(key);stats['total']+=1;j['total']=max(j['total'],stats['total']);j['message']='正在导入：'+(p['title'] or p['doi']);save(j)
            try:
                # Resolve actual linked/imported attachment paths via Zotero itself.
                file=importer._zotero_pdf_path(key,Path(get_setting('zotero_data_dir') or Path.home()/'Zotero'))
                if file:p['local_pdf_path']=file
                with get_db() as c:
                    pid,created=importer.upsert_paper(c,p,'zotero','library')
                    apply_destination(c,[pid],body.destination_folder_ids)
                    if body.preserve_folders:
                        for ck in (item.get('data') or {}).get('collections',[]):
                            fid=_folder(c,ck,by)
                            if fid:c.execute('INSERT OR IGNORE INTO organizer_items VALUES(?,?)',(fid,pid))
                        folders.invalidate(c)
                stats['added' if created else 'updated']+=1;stats['with_pdf']+=int(bool(file))
                if pid not in stats['paper_ids']:stats['paper_ids'].append(pid)
            except Exception:stats['errors'].append((p['title'] or key)+'：此条未导入，请检查来源或标识冲突后重试。')
            j['completed']+=1;save(j)
    j['total']=stats['total']

def work(jid,body):
    with _queue_lock:
        j=get(jid)
        if j['status']=='cancelling':j['status']='cancelled';save(j);return
        j.update(status='running',message='正在读取文献…');save(j)
        try:
            with get_db() as c:validate_destination(c,body.destination_folder_ids)
            if body.kind=='zotero':_zotero(body,j)
            else:
                if body.kind=='doi':records=importer.extract_dois(body.text)
                elif body.kind=='pdf-folder':
                    root=Path(body.text.strip().strip('"')).expanduser()
                    if not root.is_dir():raise ValueError('文件夹不存在，请重新选择。')
                    records=sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower()=='.pdf')
                else:records=[body.text]
                if not records:raise ValueError('没有可导入的 DOI 或 PDF 文件，请检查内容。')
                j['total']=len(records);combined={'added':0,'updated':0,'total':0,'errors':[],'paper_ids':[]};j['result']=combined
                for record in records:
                    if get(jid)['status']=='cancelling':j['status']='cancelled';break
                    try:
                        if body.kind=='standard':r=importer.import_standard(record,body.citations_text,body.venues_text)
                        elif body.kind=='pdf-folder':r=importer.import_pdf_files([record])
                        elif body.kind=='pdf':r=importer.import_pdf_files([DATA_DIR/'imports'/(body.upload_id+'.pdf')],title_override=body.text)
                        else:r={'doi':importer.import_dois,'bibtex':importer.import_bibtex,'ris':importer.import_ris}[body.kind](record)
                        if body.destination_folder_ids:
                            try:
                                with get_db() as c:apply_destination(c,r.get('paper_ids',[]),body.destination_folder_ids)
                            except HTTPException:r.setdefault('errors',[]).append('文献已导入，但所选文件夹已变更。请在导入结果中修改存放位置。')
                        combined['added']+=r.get('added',r.get('matched',0)+r.get('title_only',0));combined['updated']+=r.get('updated',0);combined['total']+=r.get('total',r.get('papers_total',0))
                        combined['errors'].extend(r.get('errors',[])+r.get('paper_errors',[]))
                        combined['paper_ids']=list(dict.fromkeys(combined['paper_ids']+r.get('paper_ids',[])))
                        if body.kind=='standard':combined.update(edges_added=r.get('edges_added',0),unresolved_total=r.get('unresolved_total',0),venues_added=r.get('venues_added',0))
                    except ValueError as e:combined['errors'].append(str(e))
                    j['completed']+=1;j['message']='已处理 '+str(j['completed'])+' / '+str(j['total']);save(j)
            if j['status']!='cancelled':
                errors=(j.get('result') or {}).get('errors',[])
                j['status']='completed_with_errors' if errors else 'completed'
            j['message']='导入已结束，已入库内容保留。'
        except ValueError as e:j.update(status='failed',message=str(e))
        except HTTPException:j.update(status='failed',message='所选导入位置已不存在或不可用，请重新选择后导入。')
        except ZoteroError:j.update(status='failed',message='Zotero 连接中断，已导入条目保留。请确认桌面端已启动后重新导入。')
        except Exception:j.update(status='failed',message='此次导入未完成，请检查内容后重试。已入库条目保留。')
        if get(jid)['status']=='cancelling':j['status']='cancelled'
        j['finished_at']=now();save(j)

@router.post('/jobs')
def start(body:ImportIn):
    if body.kind not in ('zotero','pdf') and not body.text.strip():raise HTTPException(422,'请先选择文件或填写内容。')
    if body.kind=='pdf' and (not body.upload_id or not (DATA_DIR/'imports'/(body.upload_id+'.pdf')).is_file()):raise HTTPException(422,'PDF 尚未上传，请重新选择文件。')
    with _start_lock:
        with get_db() as c:
            validate_destination(c,body.destination_folder_ids)
            active=sum(json.loads(r[0])['status'] in ACTIVE for r in c.execute('SELECT payload FROM import_jobs'))
        if active>=10:raise HTTPException(429,'已有十项导入任务，请等待完成后继续。')
        j={'id':uuid.uuid4().hex[:12],'status':'queued','kind':'import','source':body.kind,'created_at':now(),'completed':0,'total':0,'message':'正在排队','result':{},'destination_folder_ids':body.destination_folder_ids}
        save(j);threading.Thread(target=work,args=(j['id'],body),daemon=True).start()
    return j

@router.get('/jobs/{jid}')
def status(jid:str):return get(jid)
@router.post('/jobs/{jid}/cancel')
def cancel(jid:str):
    j=get(jid)
    if j['status'] in ACTIVE:j['status']='cancelling';save(j)
    return j

@router.post('/pdf-upload')
async def upload(request:Request,filename:str=Query('paper.pdf',max_length=255),folder_ids:list[int]=Query(default=[],alias='folder_id',max_length=50)):
    from .pdf_access import validate_pdf,MAX_BYTES
    with get_db() as c:validate_destination(c,folder_ids)
    chunks=[];size=0
    async for chunk in request.stream():
        size+=len(chunk)
        if size>MAX_BYTES:raise HTTPException(413,'PDF 文件过大，请改用本地文件夹导入。')
        chunks.append(chunk)
    raw=b''.join(chunks)
    try:validate_pdf(raw)
    except Exception:raise HTTPException(422,'文件不是可读取的 PDF，请检查文件是否完整或已解除密码。')
    digest=hashlib.sha256(raw).hexdigest();target=DATA_DIR/'imports'/(digest+'.pdf');target.parent.mkdir(exist_ok=True)
    if not target.exists():target.write_bytes(raw)
    # Keep the useful original filename as PDF metadata fallback, without paths.
    from .pdf_engine import PDFDocument
    with PDFDocument(target) as doc:
        title=(doc.metadata or {}).get('title') or ''
        if not title.strip() or title.casefold() in ('untitled','document','none'):title=Path(filename).stem
    # Import by content, not by an untrusted browser-supplied local filesystem path.
    body=ImportIn(kind='pdf',upload_id=digest,text=title,destination_folder_ids=folder_ids)
    return start(body)
