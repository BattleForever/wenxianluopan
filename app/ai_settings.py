"""Local-only model settings. Never return or log credentials."""
import json,os,tempfile,threading
from pathlib import Path
from fastapi import APIRouter,HTTPException
from . import ai_brief
from .config import DATA_DIR
from .db import set_setting
router=APIRouter(prefix='/api/ai/config')
PROVIDERS={'deepseek':'https://api.deepseek.com/v1','openai':'https://api.openai.com/v1',
           'zju':'https://modelgateway.zju.edu.cn/v1'}
_save_lock=threading.Lock()

def public_config():
    cfg=ai_brief.load_ai_config()
    if not cfg:return {'configured':False,'base_url':'','model':'','service':'custom'}
    base=cfg['chat_url'].removesuffix('/chat/completions')
    return {'configured':True,'base_url':base,'model':cfg['request_model'],'service':next((k for k,v in PROVIDERS.items() if v==base),'custom')}

@router.get('')
def get_config():return public_config()

@router.post('')
def save_config(body:dict):
    service=body.get('service','custom')
    if not isinstance(service,str):raise HTTPException(400,'请选择模型服务')
    base=PROVIDERS.get(service,body.get('base_url',''))
    if not isinstance(base,str):raise HTTPException(400,'请填写模型服务地址')
    base=base.strip().rstrip('/').removesuffix('/chat/completions')
    if not ai_brief.valid_llm_url(base):
        raise HTTPException(400,'请选择支持的HTTPS模型服务地址（DeepSeek、OpenAI或浙江大学模型服务）')
    model=body.get('model','')
    if not isinstance(model,str) or not model.strip() or len(model)>200 or any(ord(ch)<32 for ch in model):raise HTTPException(400,'请填写服务商提供的模型名称')
    key=body.get('api_key','')
    if not isinstance(key,str) or len(key)>8192 or any(ord(ch)<32 or ord(ch)==127 for ch in key):raise HTTPException(400,'密钥格式不正确，请勿包含换行或控制字符')
    key=key.strip()
    with _save_lock:
        previous=ai_brief.load_ai_config()
        if not key:
            if previous and previous['chat_url'].removesuffix('/chat/completions')==base:key=previous['api_key']
            else:raise HTTPException(400,'首次设置或更换服务时，请填写API密钥')
        name=None
        try:
            private=DATA_DIR/'private';private.mkdir(parents=True,exist_ok=True)
            target=private/'ai_config.local.json'
            fd,name=tempfile.mkstemp(dir=private,suffix='.tmp')
            with os.fdopen(fd,'w',encoding='utf-8') as out:
                json.dump({'llm':{'base_url':base,'model':model.strip(),'api_key':key}},out,ensure_ascii=False)
            os.replace(name,target)
        except OSError:raise HTTPException(500,'本机配置保存失败，请检查数据目录权限')
        finally:
            if name:Path(name).unlink(missing_ok=True)
    if body.get('enable_ai') is True:set_setting('offline_mode','0')
    return {'saved':True,**public_config()}


@router.post('/test')
def test_config():
    blocked=ai_brief.ai_generation_blocked()
    if blocked:return {'ok':False,'error':blocked}
    cfg=ai_brief.load_ai_config()
    if not cfg:return {'ok':False,'error':{'code':'not_configured','message':'请先保存模型设置'}}
    try:
        # Explicit small request only; no papers, PDF text or private file paths.
        _,model=ai_brief.call_llm(cfg,'You are a connection test. Reply OK only.','Reply OK.',max_tokens=256)
        return {'ok':True,'model':model or cfg['request_model'],'message':'连接成功，可以使用 AI 辅助阅读。'}
    except ai_brief.LLMServiceError as e:
        return {'ok':False,'error':{'code':e.code,'message':e.message}}
