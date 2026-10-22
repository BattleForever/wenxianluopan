"""Optional scholarly service credentials; public status only."""
from fastapi import APIRouter,HTTPException
from .config import DATA_DIR
from .private_settings import LOCK,read_json,atomic_json,service_key
router=APIRouter(prefix='/api/data-sources')
NAMES={'openalex','easyscholar'}

@router.get('')
def status():
    from .venues import load_key
    return {'openalex':{'configured':bool(service_key('openalex'))},'easyscholar':{'configured':bool(load_key())}}

@router.post('')
def save(body:dict):
    name=body.get('service')
    if not isinstance(name,str) or name not in NAMES:raise HTTPException(400,'不支持的数据源')
    key=body.get('api_key','')
    if not isinstance(key,str) or len(key)>8192 or any(ord(c)<32 or ord(c)==127 for c in key):raise HTTPException(400,'密钥不能包含换行或控制字符')
    if not key.strip() and body.get('clear') is not True:raise HTTPException(400,'请填写密钥，或选择删除已保存的配置')
    with LOCK:
        path=DATA_DIR/'private'/'data_sources.local.json';value=read_json(path)
        value[name]=None if body.get('clear') is True else key.strip()
        try:atomic_json(path,value)
        except OSError:raise HTTPException(500,'数据源设置保存失败')
    return {'saved':True,**status()}
