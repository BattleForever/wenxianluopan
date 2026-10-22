"""Explicit PDF discovery/upload/download. No retrieval is triggered by GET."""
import hashlib
from pathlib import Path
from urllib.parse import urlparse,urljoin
import fitz
import requests
from fastapi import APIRouter,HTTPException,Request
from .db import get_db
from .config import PDF_DIR
from . import metadata_sources,pdf_guide
from .netclient import is_online

router=APIRouter(prefix='/api/papers')
MAX_BYTES=40*1024*1024
# Download only public scholarly/publisher hosts. Every redirect is checked
# before the request, and no caller-supplied arbitrary URL is accepted.
HOSTS={'www.nature.com','nature.com','idp.nature.com','media.springernature.com','link.springer.com',
       'arxiv.org','export.arxiv.org','pmc.ncbi.nlm.nih.gov','www.ncbi.nlm.nih.gov',
       'europepmc.org','www.biorxiv.org','www.medrxiv.org','journals.plos.org',
       'www.frontiersin.org','public-pages-files-2025.frontiersin.org',
       'www.mdpi.com','mdpi-res.com','www.science.org','onlinelibrary.wiley.com',
       'ieeexplore.ieee.org','www.sciencedirect.com','www.cell.com',
       'iopscience.iop.org','academic.oup.com','www.pnas.org'}

def paper(pid):
    with get_db() as c:r=c.execute('SELECT * FROM papers WHERE id=?',(pid,)).fetchone()
    if not r:raise HTTPException(404,'文献不存在')
    return dict(r)

def validate_pdf(raw):
    if not raw.startswith(b'%PDF-') or len(raw)>MAX_BYTES:raise HTTPException(422,'请选择真实PDF文件，大小不超过40 MB')
    try:
        with fitz.open(stream=raw,filetype='pdf') as d:
            if d.needs_pass:raise HTTPException(422,'PDF已加密，请先解除密码')
            if not len(d):raise ValueError()
            return len(d)
    except HTTPException:raise
    except Exception:raise HTTPException(422,'文件无法作为PDF读取，请重新选择')

def save_pdf(pid,raw):
    pages=validate_pdf(raw);target=PDF_DIR/(str(pid)+'_'+hashlib.sha256(raw).hexdigest()[:16]+'.pdf')
    PDF_DIR.mkdir(parents=True,exist_ok=True)
    # Content-addressed copies never overwrite a user's existing source file.
    if not target.exists():target.write_bytes(raw)
    with get_db() as c:c.execute('UPDATE papers SET local_pdf_path=? WHERE id=?',(str(target),pid))
    return {'ready':True,'pages':pages,'filename':target.name}

def public_source(p):
    f=Path(p.get('local_pdf_path') or '')
    if not f.is_file():return None
    try:
        with fitz.open(f) as d:
            return {'ready':not d.needs_pass,'filename':f.name,'pages':len(d)}
    except Exception:return None

@router.get('/{pid}/pdf-access')
def status(pid:int):
    p=paper(pid)
    return {**(public_source(p) or {'ready':False}), 'has_public_link':bool(p.get('oa_pdf_url')),
            'doi_url':'https://doi.org/'+p['doi'] if p.get('doi') else None}

@router.post('/{pid}/pdf-access/find')
def find(pid:int):
    p=paper(pid)
    try:
        path=pdf_guide._find_pdf(p)
        return {**status(pid),'message':'已关联可读PDF'}
    except HTTPException:pass
    if not is_online():return {**status(pid),'message':'未找到本机附件，可上传PDF或开启联网后查找公开全文'}
    found=None
    try:found=metadata_sources.openalex(p)
    except Exception:pass
    if found and found.get('oa_pdf_url'):
        with get_db() as c:c.execute('UPDATE papers SET oa_pdf_url=? WHERE id=?',(found['oa_pdf_url'],pid))
    return {**status(pid),'message':'已找到公开PDF地址，可点击下载' if found and found.get('oa_pdf_url') else '未找到公开PDF。可上传本机文件，或前往出版商获取后上传'}

@router.post('/{pid}/pdf-access/upload')
async def upload(pid:int,request:Request):
    paper(pid);raw=bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw)>MAX_BYTES:raise HTTPException(413,'PDF超过40 MB')
    return save_pdf(pid,bytes(raw))

def checked_url(url):
    try:u=urlparse(url);port=u.port
    except ValueError:raise HTTPException(422,'PDF地址格式错误')
    if u.scheme!='https' or u.hostname not in HOSTS or u.username or u.password or port not in (None,443):
        raise HTTPException(422,'此公开地址暂不支持直接下载，可在出版商页面下载后上传PDF')
    return url

@router.post('/{pid}/pdf-access/download')
def download(pid:int):
    p=paper(pid)
    existing=public_source(p)
    if existing and existing['ready']:return existing
    if not is_online():raise HTTPException(409,'请先开启联网，或上传本机PDF')
    url=p.get('oa_pdf_url')
    if not url:
        find(pid);url=paper(pid).get('oa_pdf_url')
    if not url:raise HTTPException(404,'未找到公开PDF，请前往出版商获取或上传本机文件')
    try:
        with requests.Session() as session:
            for _ in range(6):
                checked_url(url)
                with session.get(url,timeout=(8,25),allow_redirects=False,stream=True,headers={'User-Agent':'WenxianLuopan scholarly reader'}) as response:
                    if response.status_code in (301,302,303,307,308):
                        url=urljoin(url,response.headers.get('Location',''));continue
                    if response.status_code!=200:raise HTTPException(502,'公开全文本次无法下载，请稍后重试或上传本机PDF')
                    raw=bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw)>MAX_BYTES:raise HTTPException(413,'公开PDF超过40 MB，请手动下载后选择正文版本')
                    return save_pdf(pid,bytes(raw))
            raise HTTPException(502,'公开地址跳转过多，请从出版商获取PDF')
    except requests.RequestException:raise HTTPException(502,'PDF下载连接未成功，请重试或上传本机文件')
