"""PDF destination policy; Zotero manages its own storage, never written directly."""
import os,sys,json,subprocess,hashlib,re,tempfile,time
from pathlib import Path
from typing import Literal
from fastapi import APIRouter,Request,HTTPException
from pydantic import BaseModel,Field
from .private_settings import LOCK,read_json,atomic_json
from .config import DATA_DIR
from .db import get_db,get_setting
router=APIRouter(prefix='/api/pdf-storage')
FILE=DATA_DIR/'private'/'pdf_storage.local.json'

def policy():
    with LOCK:return read_json(FILE,{'mode':'zotero','directory':''})

class StorageIn(BaseModel):
    mode:Literal['zotero','custom','ask']='zotero'
    directory:str=Field(default='',max_length=2048)

def managed_root():return (Path(get_setting('zotero_data_dir') or Path.home()/'Zotero')/'storage').resolve()

def checked_directory(value):
    p=Path(value).expanduser()
    if not p.is_absolute():raise HTTPException(422,'请选择完整的本机目录')
    p=p.resolve()
    if p==managed_root() or p.is_relative_to(managed_root()):raise HTTPException(422,'Zotero管理目录请使用默认方式；指定目录请选择普通文件夹')
    return p

@router.get('')
def settings():return policy()

@router.post('')
def save_settings(body:StorageIn,request:Request):
    from .workspace_data import authorize
    authorize(request);value=body.model_dump()
    if body.directory:
        value['directory']=str(checked_directory(body.directory))
    if body.mode=='custom' and not value['directory']:raise HTTPException(422,'请选择保存目录')
    with LOCK:atomic_json(FILE,value)
    return {'saved':True,**value}

@router.post('/choose-folder')
def choose_folder(request:Request):
    from .workspace_data import authorize
    authorize(request)
    if os.name!='nt':raise HTTPException(400,'请填写完整的本机目录')
    script="Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='选择文献原文的保存目录'; if($d.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Output $d.SelectedPath}"
    try:r=subprocess.run(['powershell.exe','-NoProfile','-STA','-WindowStyle','Hidden','-Command',script],capture_output=True,timeout=180,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:raise HTTPException(408,'未选择保存目录，已取消')
    return {'path':r.stdout.decode('utf-8-sig').strip()}

def bind_zotero(pid,path):
    if policy().get('mode')!='zotero':return
    from .pdf_access import validate_pdf,MAX_BYTES
    f=Path(path)
    if not f.is_file() or f.stat().st_size>MAX_BYTES:raise ValueError('Attachment not readable')
    validate_pdf(f.read_bytes())
    with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(f.resolve()),pid))

def describe(pid):
    from .pdf_access import paper
    p=paper(pid);path=Path(p.get('local_pdf_path') or '')
    if not path.is_file():return {'saved_to':None,'saved_directory':None,'can_open_folder':False,'storage_mode':policy().get('mode','zotero')}
    # No URL or synthetic collection path is presented as an actual file.
    with get_db() as c:
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='zotero_pdf_state'").fetchone()
        r=c.execute('SELECT * FROM zotero_pdf_state WHERE paper_id=?',(pid,)).fetchone() if exists else None
    native_path=r['native_path'] if r and 'native_path' in r.keys() else None
    native=bool(native_path and Path(native_path).resolve()==path.resolve() or r and r['attachment_key'] and path.parent.name==r['attachment_key'] or path.resolve().is_relative_to(managed_root()))
    cache=path.resolve().is_relative_to((DATA_DIR/'pdfs').resolve())
    return {'saved_to':'zotero' if native else 'cache' if cache else 'custom','saved_directory':str(path.parent.resolve()),'can_open_folder':True,'storage_mode':policy().get('mode','zotero')}

def finalize(pid,destination=None):
    from . import pdf_access
    p=pdf_access.paper(pid);f=Path(p.get('local_pdf_path') or '')
    if not f.is_file():return pdf_access.status(pid)
    setting=policy();mode=setting.get('mode','zotero')
    if mode in ('custom','ask'):
        folder=destination if mode=='ask' else setting.get('directory')
        if not folder:raise HTTPException(409,'请先选择本次保存目录')
        target=checked_directory(folder)
        raw=f.read_bytes();pdf_access.validate_pdf(raw);digest=hashlib.sha256(raw).hexdigest()[:12]
        name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',p.get('title') or 'paper').strip(' .')[:85] or 'paper'
        try:
            target.mkdir(parents=True,exist_ok=True);file=target/(name+'_'+digest+'.pdf')
            if not file.exists():
                fd,tmp=tempfile.mkstemp(dir=target,suffix='.tmp')
                try:
                    with os.fdopen(fd,'wb') as stream:stream.write(raw)
                    os.replace(tmp,file)
                finally:Path(tmp).unlink(missing_ok=True)
            elif hashlib.sha256(file.read_bytes()).hexdigest()!=hashlib.sha256(raw).hexdigest():raise OSError('Content conflict')
        except OSError:raise HTTPException(409,'此目录暂时无法保存，已下载的原文保留，可选择其他目录')
        with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(file.resolve()),pid))
        return {**pdf_access.status(pid),**describe(pid),'message':'PDF已保存，可打开所在文件夹'}
    try:
        from . import zotero_link
        if not zotero_link.client().api_key:raise ValueError('Zotero authorization unavailable')
        with get_db() as c:
            old=c.execute('SELECT collection_key FROM zotero_destinations WHERE paper_id=?',(pid,)).fetchone()
            recent=c.execute('SELECT collection_key FROM zotero_destinations ORDER BY verified_at DESC LIMIT 1').fetchone()
        key=old['collection_key'] if old else recent['collection_key'] if recent else None
        deadline=time.monotonic()+30
        while True:
            try:
                result=zotero_link.sync(zotero_link.Sync(paper_id=pid,collection_key=key,include_pdf=True));break
            except HTTPException as error:
                if error.status_code!=409 or '另一篇文献正在同步' not in str(error.detail) or time.monotonic()>=deadline:raise
                time.sleep(.25)
        complete=result.get('pdf',{}).get('state') in ('existing','saved')
        message='PDF已保存到Zotero，可打开所在文件夹' if complete else 'PDF已下载，Zotero保存尚未完成；原文已暂存，可重新保存'
    except Exception:message='PDF已下载，Zotero暂未连接或保存未完成；原文已暂存，可连接后重试'
    return {**pdf_access.status(pid),**describe(pid),'message':message}

@router.post('/open-folder/{pid}')
def open_folder(pid:int,request:Request):
    from .workspace_data import authorize
    from .pdf_access import paper,validate_pdf,MAX_BYTES
    authorize(request);f=Path(paper(pid).get('local_pdf_path') or '')
    if not f.is_file() or f.stat().st_size>MAX_BYTES:raise HTTPException(404,'尚未保存可打开的原文')
    validate_pdf(f.read_bytes());folder=str(f.parent.resolve())
    if os.name=='nt':os.startfile(folder)
    elif sys.platform=='darwin':subprocess.Popen(['open',folder])
    else:subprocess.Popen(['xdg-open',folder])
    return {'opened':True}
