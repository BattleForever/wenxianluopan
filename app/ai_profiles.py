"""Named model connections. Only public descriptors leave this module."""
import re
import uuid
from urllib.parse import urlparse
from datetime import datetime,timezone
from fastapi import APIRouter,HTTPException
from . import network_settings as network
from . import ai_brief
from .config import DATA_DIR
from .db import set_setting
from .private_settings import LOCK,read_json,atomic_json

router=APIRouter(prefix='/api/ai/profiles')
PROVIDERS={'deepseek':'https://api.deepseek.com/v1','openai':'https://api.openai.com/v1',
 'qwen':'https://dashscope.aliyuncs.com/compatible-mode/v1','moonshot':'https://api.moonshot.cn/v1',
 'zhipu':'https://open.bigmodel.cn/api/paas/v4','siliconflow':'https://api.siliconflow.cn/v1',
 'gemini':'https://generativelanguage.googleapis.com/v1beta/openai'}
def _target():return DATA_DIR/'private'/'ai_config.local.json'

def _store():
    raw=read_json(ai_brief._config_path())
    if isinstance(raw.get('profiles'),list):
        return {'schema_version':2,'active_profile_id':raw.get('active_profile_id'),
                'profiles':[p for p in raw['profiles'] if isinstance(p,dict)]}
    llm=raw.get('llm')
    if not isinstance(llm,dict) or not ai_brief.load_ai_config():return {'schema_version':2,'active_profile_id':None,'profiles':[]}
    cfg=ai_brief.load_ai_config()
    p={'id':'legacy','name':'已有模型连接','base_url':cfg['chat_url'].removesuffix('/chat/completions'),
       'model':cfg['request_model'],'api_key':cfg['api_key']}
    return {'schema_version':2,'active_profile_id':'legacy','profiles':[p]}

def _write(store):
    active=next((p for p in store['profiles'] if p['id']==store['active_profile_id']),None)
    # Keep the legacy active section for old readers. Empty llm is an explicit
    # tombstone, so deleting the last profile cannot resurrect an external key.
    value={**store,'llm':{k:active[k] for k in ('base_url','model','api_key','api_keys') if k in active} if active else {}}
    try:atomic_json(_target(),value)
    except OSError:raise HTTPException(500,'连接设置保存失败，请检查数据目录权限')

def safe_concurrency(p):
    try:return max(1,min(8,int(p.get('max_concurrency',1))))
    except (ValueError,TypeError):return 1

def public(p,active):
    base=p.get('base_url','')
    return {k:p.get(k) for k in ('id','name','model','last_test_at','last_test_ok','last_test_code')}|{
        'base_url':base,'service':next((k for k,v in PROVIDERS.items() if v==base),'custom'),
        'name':urlparse(base).hostname if p.get('name')=='已有模型连接' else p.get('name'),
        'active':p['id']==active,'has_key':bool(p.get('api_key')),'models':p.get('models') or ([p['model']] if p.get('model') else []),
        'max_concurrency':safe_concurrency(p),'key_count':len(p.get('api_keys') or ([p['api_key']] if p.get('api_key') else []))}

@router.get('')
def list_profiles():
    with LOCK:s=_store()
    return {'active_profile_id':s['active_profile_id'],'profiles':[public(p,s['active_profile_id']) for p in s['profiles']]}

def _validate(body,previous=None):
    service=body.get('service','custom')
    if not isinstance(service,str):raise HTTPException(400,'请选择模型服务')
    base=PROVIDERS.get(service,body.get('base_url',''))
    if service=='zju':base='https://modelgateway.zju.edu.cn/v1'  # legacy import only, not a product provider
    if not isinstance(base,str):raise HTTPException(400,'请填写模型服务地址')
    base=base.strip().rstrip('/').removesuffix('/chat/completions')
    if not ai_brief.valid_model_base(base):raise HTTPException(400,'请填写 HTTPS 基础地址，或本机接口 http://127.0.0.1:端口/v1')
    model=body.get('model','');name=body.get('name') or urlparse(base).hostname
    if not isinstance(model,str) or len(model)>200 or any(ord(ch)<32 for ch in model):raise HTTPException(400,'模型名称格式不正确')
    if not isinstance(name,str) or len(name)>100 or any(ord(ch)<32 for ch in name):raise HTTPException(400,'连接名称最多 100 字，不能包含控制字符')
    if not name.strip():name=urlparse(base).hostname
    keys=body.get('api_keys');key=body.get('api_key','')
    if keys is not None:
        if not isinstance(keys,list) or not 1<=len(keys)<=20 or any(not isinstance(k,str) or not k.strip() or len(k)>8192 or any(ord(c)<32 or ord(c)==127 for c in k) for k in keys):raise HTTPException(400,'请每行填写一个有效密钥，最多20个')
        keys=list(dict.fromkeys(k.strip() for k in keys));key=keys[0]
    if not isinstance(key,str) or len(key)>8192 or any(ord(ch)<32 or ord(ch)==127 for ch in key):raise HTTPException(400,'密钥格式不正确，请勿包含换行或控制字符')
    key=key.strip()
    if not key:
        if previous and previous.get('base_url')==base:key=previous.get('api_key','')
        else:raise HTTPException(400,'新建连接或更换服务地址时，请填写密钥')
    if not key:raise HTTPException(400,'请填写密钥')
    models=body.get('models',previous.get('models',[]) if previous else [])
    if not isinstance(models,list) or len(models)>500 or any(not isinstance(m,str) or not m.strip() or len(m)>200 or any(ord(c)<32 for c in m) for m in models):raise HTTPException(400,'模型列表格式不正确，最多500项')
    models=list(dict.fromkeys(m for m in [model.strip(),*[m.strip() for m in models]] if m))
    retained=(previous.get('api_keys') or [previous['api_key']]) if previous and previous.get('base_url')==base and not body.get('api_key') else []
    if 'keep_key_indexes' in body:
        indexes=body['keep_key_indexes']
        if not isinstance(indexes,list) or any(not isinstance(i,int) or isinstance(i,bool) or i<0 or i>=len(retained) for i in indexes):raise HTTPException(400,'已保存密钥选择无效')
        retained=[retained[i] for i in dict.fromkeys(indexes)]
    combined=list(dict.fromkeys((retained+keys) if keys and body.get('append_keys') else keys or retained or ([] if previous and 'keep_key_indexes' in body else [key])))
    if not combined:raise HTTPException(400,'至少保留一个密钥，或填入新的密钥')
    if len(combined)>20:raise HTTPException(400,'每组最多保存20个密钥')
    limit=body.get('max_concurrency',previous.get('max_concurrency',1) if previous else 1)
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=8:raise HTTPException(400,'并发数请填写1到8的整数')
    return {'name':name.strip(),'base_url':base,'model':model.strip(),'models':models,'api_key':combined[0],'api_keys':combined,'max_concurrency':limit}

@router.post('')
def save_profile(body:dict):
    with LOCK:
        s=_store();pid=body.get('id')
        if pid is not None and (not isinstance(pid,str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}',pid)):raise HTTPException(400,'连接标识无效')
        previous=next((p for p in s['profiles'] if p['id']==pid),None)
        if pid and not previous:raise HTTPException(404,'连接不存在')
        if not previous and len(s['profiles'])>=20:raise HTTPException(400,'最多保存 20 个连接，请先删除不使用的连接')
        cfg=_validate(body,previous)
        p={**(previous or {}),**cfg,'id':pid or uuid.uuid4().hex}
        # Credential or endpoint edits invalidate a previous successful test.
        if not previous or any(previous.get(k)!=cfg[k] for k in cfg if k!='name'):
            for key in ('last_test_at','last_test_ok','last_test_code'):p.pop(key,None)
        s['profiles']=[p if x['id']==p['id'] else x for x in s['profiles']] if previous else s['profiles']+[p]
        if body.get('activate') is True or s['active_profile_id'] is None:s['active_profile_id']=p['id']
        _write(s)
    if body.get('enable_ai') is True:set_setting('offline_mode','0')
    from . import metadata_jobs
    with metadata_jobs._lock:metadata_jobs.dispatch()
    return {'saved':True,'profile':public(p,s['active_profile_id']),**list_profiles()}

@router.post('/{pid}/activate')
def activate(pid:str):
    with LOCK:
        s=_store()
        if not any(p['id']==pid for p in s['profiles']):raise HTTPException(404,'连接不存在')
        s['active_profile_id']=pid;_write(s)
    return list_profiles()

@router.delete('/{pid}')
def delete(pid:str):
    with LOCK:
        s=_store()
        if not any(p['id']==pid for p in s['profiles']):raise HTTPException(404,'连接不存在')
        s['profiles']=[p for p in s['profiles'] if p['id']!=pid]
        if s['active_profile_id']==pid:s['active_profile_id']=s['profiles'][0]['id'] if s['profiles'] else None
        _write(s)
    return {'deleted':True,**list_profiles()}

@router.post('/{pid}/test')
def test_profile(pid:str):
    with LOCK:
        s=_store();p=next((p for p in s['profiles'] if p['id']==pid),None)
    if not p:raise HTTPException(404,'连接不存在')
    if not p.get('model'):return {'ok':False,'error':{'code':'missing_model','message':'请先获取或填写模型，并选择一个模型'}}
    blocked=ai_brief.ai_generation_blocked(check_config=False)
    if blocked:return {'ok':False,'error':blocked}
    cfg={'chat_url':p['base_url']+'/chat/completions','request_model':p['model'],'api_key':p['api_key'],'api_keys':p.get('api_keys') or [p['api_key']]}
    try:
        _,model=ai_brief.call_llm(cfg,'You are a connection test. Reply OK only.','Reply OK.',max_tokens=256,timeout=(10,60),retry=False)
        result={'ok':True,'model':model or p['model'],'message':'连接成功'}
    except ai_brief.LLMServiceError as e:result={'ok':False,'error':{'code':e.code,'message':e.message}}
    with LOCK:
        current=_store();item=next((x for x in current['profiles'] if x['id']==pid),None)
        if item and all(item.get(k)==p.get(k) for k in ('base_url','model','api_key')):
            item.update(last_test_at=datetime.now(timezone.utc).isoformat(),last_test_ok=result['ok'],last_test_code=None if result['ok'] else result['error']['code']);_write(current)
    return result

@router.post('/{pid}/model')
def select_model(pid:str,body:dict):
    with LOCK:
        s=_store();p=next((x for x in s['profiles'] if x['id']==pid),None)
        if not p:raise HTTPException(404,'连接不存在')
        model=body.get('model')
        if not isinstance(model,str) or model not in (p.get('models') or [p['model']]):raise HTTPException(400,'请从该连接的模型列表选择')
        p['model']=model;p.pop('last_test_ok',None);_write(s)
    return list_profiles()

def _models_request(p):
    import requests
    try:ai_brief.check_model_destination(p['base_url'])
    except ai_brief.LLMServiceError as e:raise HTTPException(400,e.message)
    for key in p.get('api_keys') or [p['api_key']]:
        try:r=requests.get(p['base_url']+'/models',headers={'Authorization':'Bearer '+key},timeout=(5,20),allow_redirects=False,
                           **({'proxies':dict(network.DIRECT)} if ai_brief.local_model_base(p['base_url']) else network.proxy_options(p['base_url'])))
        except requests.RequestException:raise HTTPException(502,'模型列表连接失败，可手动填写模型')
        if r.status_code in (401,402,403,429):continue
        if r.status_code!=200:raise HTTPException(502,'上游未提供模型列表，可手动填写模型')
        try:rows=r.json().get('data',[])
        except (ValueError,AttributeError):raise HTTPException(502,'上游模型列表格式不受支持')
        ids=[x.get('id') for x in rows if isinstance(x,dict)] if isinstance(rows,list) else []
        return sorted(set(m for m in ids if isinstance(m,str) and 0<len(m)<=200 and not any(ord(c)<32 for c in m)))[:500]
    raise HTTPException(502,'模型列表读取失败，请检查密钥与额度')

@router.post('/models-preview')
def preview_models(body:dict):
    if ai_brief.ai_generation_blocked(check_config=False):raise HTTPException(400,'当前为离线模式')
    with LOCK:previous=next((p for p in _store()['profiles'] if p['id']==body.get('id')),None)
    cfg=_validate(body,previous)
    return {'models':_models_request(cfg)}

@router.post('/{pid}/models')
def fetch_models(pid:str):
    if ai_brief.ai_generation_blocked(check_config=False):raise HTTPException(400,'当前为离线模式')
    with LOCK:p=next((x.copy() for x in _store()['profiles'] if x['id']==pid),None)
    if not p:raise HTTPException(404,'连接不存在')
    models=_models_request(p)
    with LOCK:
        s=_store();current=next((x for x in s['profiles'] if x['id']==pid),None)
        if current and current.get('base_url')==p['base_url']:
            current['models']=list(dict.fromkeys(m for m in [current['model'],*models] if m));_write(s)
    return {'models':models,**list_profiles()}


def active_reference():
    """Persistable model choice; no credentials or private response bodies."""
    with LOCK:
        store=_store();p=next((x for x in store['profiles'] if x['id']==store['active_profile_id']),None)
        return {'profile_id':p['id'],'model':p.get('model'),'name':p.get('name')} if p else {'profile_id':None,'model':None}

def concurrency_for(reference):
    with LOCK:
        p=next((x for x in _store()['profiles'] if x['id']==(reference or {}).get('profile_id')),None)
        return safe_concurrency(p or {})

def config_for(reference):
    with LOCK:
        p=next((x for x in _store()['profiles'] if x['id']==reference.get('profile_id')),None)
        if not p or not reference.get('model') or reference['model'] not in (p.get('models') or [p.get('model')]):return None
        return {'profile_id':p['id'],'chat_url':p['base_url']+'/chat/completions','request_model':reference['model'],'api_key':p['api_key'],'api_keys':list(p.get('api_keys') or [p['api_key']]),'max_concurrency':safe_concurrency(p)}
