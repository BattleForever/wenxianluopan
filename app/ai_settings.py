"""Compatibility endpoints for the active named AI connection."""
from fastapi import APIRouter,HTTPException
from . import ai_brief,ai_profiles
router=APIRouter(prefix='/api/ai/config')
PROVIDERS=ai_profiles.PROVIDERS

def public_config():
    cfg=ai_brief.load_ai_config()
    if not cfg:return {'configured':False,'base_url':'','model':'','service':'custom'}
    base=cfg['chat_url'].removesuffix('/chat/completions')
    return {'configured':True,'base_url':base,'model':cfg['request_model'],'service':next((k for k,v in PROVIDERS.items() if v==base),'custom')}

@router.get('')
def get_config():return public_config()

@router.post('')
def save_config(body:dict):
    active=ai_profiles.list_profiles()['active_profile_id']
    value={**body,'activate':True}
    if active:value['id']=active
    ai_profiles.save_profile(value)
    return {'saved':True,**public_config()}

@router.post('/test')
def test_config():
    active=ai_profiles.list_profiles()['active_profile_id']
    if not active:
        blocked=ai_brief.ai_generation_blocked()
        return {'ok':False,'error':blocked or {'code':'not_configured','message':'请先保存模型设置'}}
    return ai_profiles.test_profile(active)
