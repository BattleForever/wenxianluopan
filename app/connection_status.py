"""Explicit connection checks; publisher reachability is not subscription entitlement."""
import hashlib,json,time,threading,uuid
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor,as_completed
from urllib.parse import urlparse,urljoin,parse_qs
import requests
from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,Field
from .config import DATA_DIR
from .private_settings import LOCK,read_json,atomic_json
from . import network_settings as network,ai_profiles
router=APIRouter(prefix='/api/connections');FILE=DATA_DIR/'private/connection_checks.local.json';guard=threading.RLock();jobs={}
def public_job(job):
    with guard:return {**job,'results':{key:dict(value) for key,value in job['results'].items()}}
PUBLISHERS={
 'nature':('Springer Nature','https://www.nature.com/',{'www.nature.com','nature.com','link.springer.com','www.springer.com','idp.nature.com','media.springernature.com'}),
 'elsevier':('Elsevier / ScienceDirect','https://www.sciencedirect.com/',{'www.sciencedirect.com','www.cell.com','www.elsevier.com'}),
 'ieee':('IEEE Xplore','https://ieeexplore.ieee.org/',{'ieeexplore.ieee.org'}),
 'wiley':('Wiley','https://onlinelibrary.wiley.com/',{'onlinelibrary.wiley.com'}),
 'acs':('ACS','https://pubs.acs.org/',{'pubs.acs.org'}),
 'taylor':('Taylor & Francis','https://www.tandfonline.com/',{'www.tandfonline.com'}),
 'oup':('Oxford Academic','https://academic.oup.com/',{'academic.oup.com'})}

def fingerprint():
    from .zotero_link import client
    from .db import get_bool_setting
    with LOCK:
        store=ai_profiles._store();active=next((p for p in store['profiles'] if p['id']==store['active_profile_id']),{})
    # This digest never leaves the private cache and is not a credential export.
    return hashlib.sha256(json.dumps([network.saved(),get_bool_setting('offline_mode'),active.get('id'),active.get('base_url'),active.get('model'),active.get('api_keys') or active.get('api_key'),client().api_key],sort_keys=True).encode()).hexdigest()

def saved_results():
    with LOCK:cache=read_json(FILE)
    if cache.get('fingerprint')!=fingerprint():return {}
    try:
        if time.time()-cache.get('time',0)>900:return {}
    except TypeError:return {}
    fresh={}
    for key,value in cache.get('results',{}).items():
        try:
            if -60<=time.time()-datetime.fromisoformat(value['checked_at']).timestamp()<=900:fresh[key]=value
        except (KeyError,ValueError,TypeError):pass
    return fresh

@router.get('')
def summary():
    public=ai_profiles.list_profiles();active=next((p for p in public['profiles'] if p['active']),None)
    return {'results':saved_results(),'ai_configured':bool(active and active.get('has_key') and active.get('model')),'active_model':active.get('model') if active else None,'network_mode':network.public()['mode'],'publishers':[{'id':k,'name':v[0],'url':v[1]} for k,v in PUBLISHERS.items()]}

def allowed(url,hosts):
    try:u=urlparse(url);port=u.port
    except ValueError:return False
    return u.scheme=='https' and u.hostname in hosts and port in (None,443) and not u.username and not u.password and not any(k.lower() in ('token','key','api_key','secret','ticket','auth','code','password') for k in parse_qs(u.query))

def fetch_probe(url,hosts):
    started=time.monotonic()
    with network.prepare_session(requests.Session()) as session:
        for _ in range(5):
            if not allowed(url,hosts):raise ValueError('outside selected publisher')
            remaining=18-(time.monotonic()-started)
            if remaining<=0:raise requests.Timeout()
            with session.get(url,timeout=(min(3,remaining),min(7,remaining)),allow_redirects=False,stream=True,headers={'User-Agent':'Mozilla/5.0 (compatible; WenxianLuopan connection check)'},**network.proxy_options(url)) as response:
                code=response.status_code
                if code in (301,302,303,307,308):
                    target=urljoin(url,response.headers.get('Location',''))
                    if not allowed(target,hosts):return {'code':code,'body':b'','url':url,'login_redirect':True}
                    url=target;continue
                data=bytearray()
                for chunk in response.iter_content(8192):
                    data.extend(chunk)
                    if len(data)>=131072 or data.startswith(b'%PDF-') and len(data)>=4096:break
                return {'code':code,'body':bytes(data),'url':url}
    return {'code':0,'body':b'','url':url}

def publisher_check(key,url=None):
    label,base,hosts=PUBLISHERS[key];url=url or base
    if not allowed(url,hosts):raise HTTPException(422,'请填写所选出版商的 HTTPS 论文或 PDF 地址，不含登录令牌')
    result={'name':label,'state':'unchecked','access':'not_checked','message':'尚未检测','scope':'selected_link' if url!=base else 'site'}
    try:
        got=fetch_probe(url,hosts);code=got['code'];result['http_status']=code
        if got.get('login_redirect'):return {**result,'state':'warning','access':'login_required','message':'跳转登录或其他站点，需在浏览器中验证'}
        if code in (401,403,418,429) or code==202 and not got['body']:return {**result,'state':'warning','access':'blocked','message':'站点要求登录、验证或限制了本次访问'}
        if code not in (200,206):return {**result,'state':'error','message':'站点本次未返回可用页面'}
        if not got['body']:return {**result,'state':'warning','access':'unverified','message':'站点返回空页面，尚未确认访问情况'}
        result.update(state='success',message='站点可达；全文权限未验证')
        if got['body'].startswith(b'%PDF-'):return {**result,'access':'pdf_readable','message':'该原文链接可读取；仅验证当前链接'}
        if url!=base:
            from .publisher_pdf import candidates
            for pdf in candidates(got['body'].decode('utf-8','replace'),got['url'])[:2]:
                if not allowed(pdf,hosts):continue
                try:
                    original=fetch_probe(pdf,hosts)
                    if original['code'] in (200,206) and original['body'].startswith(b'%PDF-'):return {**result,'access':'pdf_readable','message':'该篇原文链接可读取；不代表全站订阅权限'}
                except (requests.RequestException,ValueError):pass
            result.update(access='unverified',message='论文页面可达，未验证可读取的原文')
        return result
    except (requests.RequestException,ValueError):return {**result,'state':'error','message':'连接未成功，请检查当前网络方式'}

def check_target(target,url=None):
    from .db import get_bool_setting
    if target=='zotero':
        from .zotero_link import client
        cli=client()
        try:ok=bool(cli.ping().get('ok'))
        except Exception:ok=False
        authorized=False
        if ok and cli.api_key:
            try:cli._request('GET','/api/users/0/items?limit=1&format=json',auth=True);authorized=True
            except Exception:pass
        return {'name':'Zotero','state':'success' if ok else 'error','message':('已连接 · 凭据有效' if authorized else '已连接 · 写入待授权') if ok else '未连接，请启动 Zotero','write_authorized':authorized}
    if get_bool_setting('offline_mode'):return {'name':target,'state':'paused','message':'当前暂停联网'}
    if target=='ai':
        ref=ai_profiles.active_reference()
        if not ref.get('profile_id') or not ref.get('model'):return {'name':'AI 模型','state':'unchecked','message':'请先配置并选择模型'}
        r=ai_profiles.test_profile(ref['profile_id'])
        return {'name':'AI 模型','state':'success' if r.get('ok') else 'error','message':'当前模型连接成功' if r.get('ok') else (r.get('error') or {}).get('message','测试未通过')}
    if target=='network':
        try:
            r=fetch_probe('https://api.crossref.org/works?rows=0',{'api.crossref.org'})
            return {'name':'网络','state':'success' if r['code']==200 else 'warning','message':'学术网络可用' if r['code']==200 else '网络已响应，来源拒绝或限制访问','http_status':r['code']}
        except (requests.RequestException,ValueError):return {'name':'网络','state':'error','message':'学术网络连接未成功'}
    return publisher_check(target,url)

class CheckIn(BaseModel):
    targets:list[str]=Field(default_factory=lambda:['ai','zotero','network'],max_length=10)
    urls:dict[str,str]=Field(default_factory=dict)

@router.post('/check')
def start(body:CheckIn,request:Request):
    from .workspace_data import authorize
    authorize(request);targets=list(dict.fromkeys(body.targets))
    if not targets or any(k not in {'ai','zotero','network',*PUBLISHERS} for k in targets):raise HTTPException(422,'请选择支持的连接')
    for key,url in body.urls.items():
        if key not in targets or key not in PUBLISHERS or len(url)>2048 or not allowed(url,PUBLISHERS[key][2]):raise HTTPException(422,'论文地址必须属于所选出版商，不含登录令牌')
    config=fingerprint()
    with guard:
        running=next((j for j in jobs.values() if j['status']=='running'),None)
        if running:return public_job(running)
        while len(jobs)>=24:jobs.pop(next(iter(jobs)))
        jid=uuid.uuid4().hex;job={'id':jid,'status':'running','total':len(targets),'completed':0,'results':{}};jobs[jid]=job
    def work():
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(check_target,key,body.urls.get(key)):key for key in targets}
            for future in as_completed(futures):
                key=futures[future]
                try:result=future.result()
                except Exception:result={'name':key,'state':'error','message':'本次检查未完成，请重试'}
                result['checked_at']=datetime.now(timezone.utc).isoformat()
                with guard:job['results'][key]=result;job['completed']+=1
        try:
            with LOCK:
                prior=read_json(FILE);results=prior.get('results',{}) if prior.get('fingerprint')==config else {}
                results.update(job['results']);atomic_json(FILE,{'fingerprint':config,'time':time.time(),'results':results})
        except OSError:
            with guard:job['cache_saved']=False
        with guard:job['status']='completed'
    threading.Thread(target=work,daemon=True).start();return public_job(job)

@router.get('/checks/{jid}')
def check_status(jid:str):
    with guard:
        if jid not in jobs:raise HTTPException(404,'检查记录已结束，可重新检查')
        return public_job(jobs[jid])
