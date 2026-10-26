"""User-started browser acquisition; capture tokens are memory-only and never logged."""
import os,sys,json,uuid,threading,subprocess,hmac,time,importlib.util
from pathlib import Path
from typing import Literal
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,Field
from .config import DATA_DIR,PORT
from .db import get_db
from .private_settings import atomic_json
router=APIRouter(prefix='/api/pdf-capture');_guard=threading.RLock();_jobs={};_queue=[];_process=None
PRIVATE=DATA_DIR/'private'/'browser_capture'
ACTIVE={'queued','running','awaiting_access'}

def init():
    with get_db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS browser_capture_jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        for r in c.execute('SELECT id,payload FROM browser_capture_jobs').fetchall():
            j=json.loads(r['payload'])
            if j.get('status') in ACTIVE:
                j.update(status='interrupted',stage='采集已中断，已有原文保留，可重新获取');c.execute('UPDATE browser_capture_jobs SET payload=? WHERE id=?',(json.dumps(j,ensure_ascii=False),r['id']))

@router.get('/capabilities')
def capabilities():
    return {'browser_support':bool(importlib.util.find_spec('playwright')),'connector_available':False,'requires_extension':False}

def save(j):
    with _guard:public={k:v for k,v in j.items() if not k.startswith('_')}
    with get_db() as c:c.execute('INSERT OR REPLACE INTO browser_capture_jobs VALUES(?,?)',(j['id'],json.dumps(public,ensure_ascii=False)))
    return public

def read_job(jid):
    with get_db() as c:r=c.execute('SELECT payload FROM browser_capture_jobs WHERE id=?',(jid,)).fetchone()
    if not r:raise HTTPException(404,'采集任务不存在')
    j=json.loads(r['payload'])
    from datetime import datetime,timezone
    try:j['elapsed_seconds']=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(j.get('started_at') or j['created_at'])).total_seconds()))
    except (ValueError,KeyError,TypeError):j['elapsed_seconds']=0
    return j

class CaptureIn(BaseModel):
    paper_id:int=Field(gt=0)
    destination:str|None=Field(default=None,max_length=2048)
    sync_to_zotero:bool=False

@router.post('')
def start(body:CaptureIn,request:Request):
    from .workspace_data import authorize
    from .pdf_access import paper
    from .pdf_storage import policy
    from .netclient import is_online
    authorize(request);p=paper(body.paper_id)
    if not is_online():raise HTTPException(409,'请先开启联网，再使用浏览器获取原文')
    if not p.get('doi'):raise HTTPException(409,'请先补充 DOI，以核对网页和原文是否属于这篇论文')
    if not body.sync_to_zotero and policy().get('mode')=='ask' and not body.destination:raise HTTPException(409,'请先选择本次保存目录')
    if not importlib.util.find_spec('playwright'):raise HTTPException(503,'浏览器支持尚未安装，请运行程序更新依赖后重试')
    from datetime import datetime,timezone
    with _guard:
        for j in _jobs.values():
            if j['paper_id']==body.paper_id and j['status'] in ACTIVE:return save(j)
        if len(_queue)>=20:raise HTTPException(429,'浏览器采集任务较多，请稍后再试')
        jid=uuid.uuid4().hex;j={'id':jid,'paper_id':body.paper_id,'title':p.get('title'),'status':'queued','stage':'等待打开原文获取窗口','sync_to_zotero':body.sync_to_zotero,'created_at':datetime.now(timezone.utc).isoformat(),'_token':uuid.uuid4().hex+uuid.uuid4().hex,'_destination':body.destination,'_sync_to_zotero':body.sync_to_zotero}
        _jobs[jid]=j;_queue.append(jid);save(j);dispatch();return save(j)

def dispatch():
    global _process
    if _process is not None or not _queue:return
    jid=_queue.pop(0);j=_jobs[jid]
    from .pdf_access import paper
    p=paper(j['paper_id']);PRIVATE.mkdir(parents=True,exist_ok=True)
    meta=json.loads(p.get('meta_json') or '{}');url=meta.get('publisher_url') or 'https://doi.org/'+p['doi']
    from urllib.parse import urlparse
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443):url='https://doi.org/'+p['doi']
    # Page discovery and attachment retrieval run without an installed extension.
    extension=None;file=PRIVATE/(jid+'.json')
    atomic_json(file,{'id':jid,'paper_id':p['id'],'title':p.get('title'),'doi':p['doi'],'url':url,'callback':'http://127.0.0.1:'+str(PORT), 'token':j['_token'],'profile':str(PRIVATE/'profile'),'extension':str(extension) if extension else None,'cancel':str(PRIVATE/(jid+'.cancel')),'headless':os.environ.get('PAPERNET_CAPTURE_HEADLESS')=='1'})
    from datetime import datetime,timezone
    j.update(status='running',phase='opening',started_at=datetime.now(timezone.utc).isoformat(),stage='正在打开原文获取窗口');save(j)
    try:
        env=dict(os.environ,PAPERNET_DATA_DIR=str(DATA_DIR));env['TEMP']=str(PRIVATE/'temp');env['TMP']=env['TEMP'];Path(env['TEMP']).mkdir(exist_ok=True)
        _process=subprocess.Popen([sys.executable,'-m','app.browser_capture_worker',str(file)],cwd=Path(__file__).resolve().parent.parent,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        process=_process
    except OSError:
        j.update(status='failed',stage='采集浏览器未能启动，请重新打开');save(j);file.unlink(missing_ok=True);_process=None;dispatch();return
    def wait():
        global _process
        process.wait()
        with _guard:
            if j['status'] in ACTIVE:j.update(status='failed',stage='浏览器已关闭，本次尚未取得匹配的 PDF，可重新获取');save(j)
            j.pop('_token',None);file.unlink(missing_ok=True);(PRIVATE/(jid+'.cancel')).unlink(missing_ok=True);_process=None;dispatch()
    threading.Thread(target=wait,daemon=True).start()

@router.get('/{jid}')
def status(jid:str):return read_job(jid)

def authorize_capture(jid,request):
    j=_jobs.get(jid);token=request.headers.get('X-Capture-Token','')
    if not j or not j.get('_token') or not hmac.compare_digest(token,j['_token']):raise HTTPException(403,'采集任务验证失败')
    if j['status'] not in ACTIVE:raise HTTPException(409,'采集任务已经结束')
    return j

class Stage(BaseModel):
    stage:Literal['preparing','opening','identifying','downloading','access','failed','cancelled']
    reason:Literal['publisher_denied','not_pdf','identity_mismatch','network','timeout','browser_unavailable']|None=None
    http_status:int|None=Field(default=None,ge=100,le=599)
    page_host:str|None=Field(default=None,max_length=255,pattern=r'^[a-zA-Z0-9.-]+$')
    attempted:int|None=Field(default=None,ge=0,le=1000)

@router.post('/{jid}/stage')
def stage(jid:str,body:Stage,request:Request):
    with _guard:
        j=authorize_capture(jid,request)
        messages={'preparing':'首次准备浏览器，完成后会自动打开','opening':'正在打开出版商页面','identifying':'正在识别原文下载入口','downloading':'正在下载并核对 PDF','access':'等待在原文获取窗口中完成访问','failed':'本次未取得原文，请查看原因后重试或上传文件','cancelled':'原文获取已停止'}
        if body.reason:
            labels={'publisher_denied':'网站拒绝了自动访问，可在浏览器中完成访问后重试','not_pdf':'来源返回的内容不是 PDF','identity_mismatch':'取得的 PDF 与当前文献未匹配，未自动关联','network':'本次连接未成功','timeout':'本次未在限定时间内取得原文','browser_unavailable':'采集浏览器未就绪，可重新获取'}
            issue={'code':body.reason,'message':labels[body.reason],'http_status':body.http_status}
            if issue not in j.setdefault('issues',[]):j['issues'].append(issue)
            PRIVATE.mkdir(parents=True,exist_ok=True)
            with (PRIVATE/(jid+'.audit.jsonl')).open('a',encoding='utf-8') as stream:stream.write(json.dumps({'stage':body.stage,**issue},ensure_ascii=False)+'\n')
        if body.page_host:j['page_host']=body.page_host
        if body.attempted is not None:j['attempted']=body.attempted
        j.update(status='awaiting_access' if body.stage=='access' else body.stage if body.stage in ('failed','cancelled') else 'running',phase=body.stage,stage=messages[body.stage]);return save(j)

@router.post('/{jid}/focus')
def focus(jid:str,request:Request):
    from .workspace_data import authorize
    authorize(request)
    with _guard:
        j=_jobs.get(jid)
        if not j or j['status'] not in ACTIVE:raise HTTPException(409,'获取窗口已关闭，请重新获取原文')
        PRIVATE.mkdir(parents=True,exist_ok=True);(PRIVATE/(jid+'.focus')).touch()
    return {'requested':True,'message':'已请求显示原文获取窗口；请在该窗口查看访问提示或点击 PDF 下载'}

@router.post('/{jid}/file')
async def receive(jid:str,request:Request):
    j=authorize_capture(jid,request)
    from .pdf_access import MAX_BYTES,validate_pdf,save_pdf,paper
    raw=bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw)>MAX_BYTES:raise HTTPException(413,'PDF超过40 MB')
    validate_pdf(bytes(raw))
    from .browser_capture_worker import matches_pdf
    if not matches_pdf(bytes(raw),j.get('title'),paper(j['paper_id']).get('doi')):raise HTTPException(422,'PDF题名或 DOI 未匹配，未关联到当前文献')
    save_pdf(j['paper_id'],bytes(raw))
    from starlette.concurrency import run_in_threadpool
    result=await run_in_threadpool(finish_source,j)
    with _guard:
        method=request.headers.get('X-Capture-Method')
        j.update(status='cancelled' if j['status']=='cancelled' else 'completed',stage=result.get('message') or 'PDF已获取并保存',source_method=method if method in ('connector','connector_attachment','connector_native','page_metadata','browser_download') else 'browser_download');save(j)
    return {'saved':True,'paper_id':j['paper_id'],'source':result}

def finish_source(j):
    from .pdf_storage import finalize
    if not j.get('_sync_to_zotero'):return finalize(j['paper_id'],j.get('_destination'))
    from . import zotero_link
    from .pdf_access import status
    with get_db() as c:r=c.execute('SELECT collection_key FROM zotero_destinations WHERE paper_id=?',(j['paper_id'],)).fetchone()
    try:
        result=zotero_link.sync(zotero_link.Sync(paper_id=j['paper_id'],collection_key=r['collection_key'] if r else None))
        return {**status(j['paper_id']),'message':result['message']}
    except Exception:return {**status(j['paper_id']),'message':'PDF已获取；Zotero保存暂未完成，可在原位置重试'}

@router.post('/{jid}/cancel')
def cancel(jid:str,request:Request):
    from .workspace_data import authorize
    authorize(request)
    with _guard:
        j=_jobs.get(jid)
        if not j:return read_job(jid)
        if j['status'] not in ACTIVE:return save(j)
        if jid in _queue:_queue.remove(jid)
        PRIVATE.mkdir(parents=True,exist_ok=True);(PRIVATE/(jid+'.cancel')).touch()
        j.update(status='cancelled',stage='已停止浏览器采集');return save(j)


def active_count():
    with _guard:return sum(j['status'] in ACTIVE for j in _jobs.values())

def stop_all():
    with _guard:
        _queue.clear();process=_process
        for j in _jobs.values():
            if j['status'] in ACTIVE:
                PRIVATE.mkdir(parents=True,exist_ok=True);(PRIVATE/(j['id']+'.cancel')).touch();j.update(status='cancelled',stage='软件退出，浏览器采集已停止');save(j)
    if process:
        try:process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            else:process.terminate()


@router.post('/{jid}/open-browser')
def open_existing_browser(jid:str,request:Request):
    from .workspace_data import authorize
    from .pdf_access import paper
    import webbrowser
    authorize(request);j=read_job(jid);p=paper(j['paper_id'])
    url='https://doi.org/'+p['doi']
    webbrowser.open(url,new=2)
    return {'opened':True,'message':'已打开文献页面；可下载原文后上传，或在采集浏览器中完成访问'}

@router.post('/{jid}/adopt')
def adopt_connector_file(jid:str,request:Request):
    from .workspace_data import authorize
    from .pdf_access import paper,validate_pdf,MAX_BYTES,status
    from . import pdf_guide,zotero_pdf,pdf_storage
    from .browser_capture_worker import matches_pdf
    import hashlib
    authorize(request)
    with _guard:
        j=_jobs.get(jid)
        if not j or j['status'] not in ACTIVE:return read_job(jid)
        pid=j['paper_id'];p=paper(pid)
    try:
        file=pdf_guide._find_pdf({**p,'local_pdf_path':None})
        if file.stat().st_size>MAX_BYTES:return save(j)
        raw=file.read_bytes();validate_pdf(raw)
        if not matches_pdf(raw,p.get('title'),p.get('doi')):return save(j)
        if j.get('_sync_to_zotero'):result=finish_source(j)
        elif pdf_storage.policy().get('mode')=='zotero':pdf_storage.bind_zotero(pid,file);result={**status(pid),'message':'已关联 Zotero 原文，可打开所在文件夹'}
        else:result=pdf_storage.finalize(pid,j.get('_destination'))
        with _guard:
            if j['status'] not in ACTIVE:return save(j)
            j.update(status='completed',stage=result['message'],source_method='zotero_attachment');(PRIVATE/(jid+'.cancel')).touch();return save(j)
    except Exception:return save(j)
