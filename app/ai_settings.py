"""Local-only model settings. Never return or log credentials."""
import json,os
from urllib.parse import urlparse
from fastapi import APIRouter,HTTPException
from . import ai_brief
from .config import DATA_DIR
from .db import set_setting
router=APIRouter(prefix='/api/ai/config')
PROVIDERS={'deepseek':'https://api.deepseek.com/v1','openai':'https://api.openai.com/v1'}

def public_config():
    cfg=ai_brief.load_ai_config()
    if not cfg:return {'configured':False,'base_url':'','model':'','service':'custom'}
    base=cfg['chat_url'].removesuffix('/chat/completions')
    return {'configured':True,'base_url':base,'model':cfg['request_model'],'service':next((k for k,v in PROVIDERS.items() if v==base),'custom')}

@router.get('')
def get_config():return public_config()

@router.post('')
def save_config(body:dict):
    service=body.get('service','custom');base=PROVIDERS.get(service,body.get('base_url',''))
    if not isinstance(base,str):raise HTTPException(400,'请填写模型服务地址')
    base=base.strip().rstrip('/').removesuffix('/chat/completions')
    u=urlparse(base)
    if u.scheme!='https' or u.hostname not in ai_brief.ALLOWED_LLM_HOSTS or u.username or u.password or u.query or u.fragment or u.port not in (None,443):
        raise HTTPException(400,'请选择支持的HTTPS模型服务地址（DeepSeek、OpenAI或浙江大学模型服务）')
    model=body.get('model','')
    if not isinstance(model,str) or not model.strip() or len(model)>200:raise HTTPException(400,'请填写服务商提供的模型名称')
    key=body.get('api_key','')
    if not isinstance(key,str) or len(key)>8192:raise HTTPException(400,'密钥格式不正确')
    key=key.strip()
    previous=ai_brief.load_ai_config()
    if not key:
        if previous and previous['chat_url'].removesuffix('/chat/completions')==base:key=previous['api_key']
        else:raise HTTPException(400,'首次设置或更换服务时，请填写API密钥')
    private=DATA_DIR/'private';private.mkdir(exist_ok=True)
    target=private/'ai_config.local.json';temp=target.with_suffix('.tmp')
    try:
        temp.write_text(json.dumps({'llm':{'base_url':base,'model':model.strip(),'api_key':key}},ensure_ascii=False),encoding='utf-8')
        os.replace(temp,target)
    except OSError:raise HTTPException(500,'本机配置保存失败，请检查数据目录权限')
    if body.get('enable_ai') is True:set_setting('offline_mode','0')
    return {'saved':True,**public_config()}
