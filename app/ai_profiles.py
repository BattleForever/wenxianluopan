"""Named model connections. Only public descriptors leave this module."""
import re
import uuid
from datetime import datetime,timezone
from fastapi import APIRouter,HTTPException
from . import ai_brief
from .config import DATA_DIR
from .db import set_setting
from .private_settings import LOCK,read_json,atomic_json

router=APIRouter(prefix='/api/ai/profiles')
PROVIDERS={'deepseek':'https://api.deepseek.com/v1','openai':'https://api.openai.com/v1','zju':'https://modelgateway.zju.edu.cn/v1'}
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
    value={**store,'llm':{k:active[k] for k in ('base_url','model','api_key')} if active else {}}
    try:atomic_json(_target(),value)
    except OSError:raise HTTPException(500,'连接设置保存失败，请检查数据目录权限')

def public(p,active):
    base=p.get('base_url','')
    return {k:p.get(k) for k in ('id','name','model','last_test_at','last_test_ok','last_test_code')}|{
        'base_url':base,'service':next((k for k,v in PROVIDERS.items() if v==base),'custom'),
        'active':p['id']==active,'has_key':bool(p.get('api_key'))}

@router.get('')
def list_profiles():
    with LOCK:s=_store()
    return {'active_profile_id':s['active_profile_id'],'profiles':[public(p,s['active_profile_id']) for p in s['profiles']]}

def _validate(body,previous=None):
    service=body.get('service','custom')
    if not isinstance(service,str):raise HTTPException(400,'请选择模型服务')
    base=PROVIDERS.get(service,body.get('base_url',''))
    if not isinstance(base,str):raise HTTPException(400,'请填写模型服务地址')
    base=base.strip().rstrip('/').removesuffix('/chat/completions')
    if not ai_brief.valid_llm_url(base):raise HTTPException(400,'请选择支持的 HTTPS 模型服务地址（DeepSeek、OpenAI 或浙江大学模型服务）')
    model=body.get('model','');name=body.get('name') or model
    if not isinstance(model,str) or not model.strip() or len(model)>200 or any(ord(ch)<32 for ch in model):raise HTTPException(400,'请填写正确的模型名称')
    if not isinstance(name,str) or len(name)>100 or any(ord(ch)<32 for ch in name):raise HTTPException(400,'连接名称最多 100 字，不能包含控制字符')
    key=body.get('api_key','')
    if not isinstance(key,str) or len(key)>8192 or any(ord(ch)<32 or ord(ch)==127 for ch in key):raise HTTPException(400,'密钥格式不正确，请勿包含换行或控制字符')
    key=key.strip()
    if not key:
        if previous and previous.get('base_url')==base:key=previous.get('api_key','')
        else:raise HTTPException(400,'新建连接或更换服务地址时，请填写密钥')
    if not key:raise HTTPException(400,'请填写密钥')
    return {'name':name.strip(),'base_url':base,'model':model.strip(),'api_key':key}

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
    blocked=ai_brief.ai_generation_blocked(check_config=False)
    if blocked:return {'ok':False,'error':blocked}
    cfg={'chat_url':p['base_url']+'/chat/completions','request_model':p['model'],'api_key':p['api_key']}
    try:
        _,model=ai_brief.call_llm(cfg,'You are a connection test. Reply OK only.','Reply OK.',max_tokens=256,timeout=(10,30),retry=False)
        result={'ok':True,'model':model or p['model'],'message':'连接成功'}
    except ai_brief.LLMServiceError as e:result={'ok':False,'error':{'code':e.code,'message':e.message}}
    with LOCK:
        current=_store();item=next((x for x in current['profiles'] if x['id']==pid),None)
        if item and all(item.get(k)==p.get(k) for k in ('base_url','model','api_key')):
            item.update(last_test_at=datetime.now(timezone.utc).isoformat(),last_test_ok=result['ok'],last_test_code=None if result['ok'] else result['error']['code']);_write(current)
    return result
