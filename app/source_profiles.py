"""Named academic credentials; multiple enabled keys form a failover pool."""
import uuid
from datetime import datetime,timezone
from fastapi import APIRouter,HTTPException
from . import network_settings as network
from .config import DATA_DIR
from .private_settings import LOCK,read_json,atomic_json
router=APIRouter(prefix='/api/data-sources/connections')
BASES={'openalex':'https://api.openalex.org','easyscholar':'https://www.easyscholar.cc'}
def path():return DATA_DIR/'private/data_sources.local.json'
def store():
 s=read_json(path())
 if not isinstance(s.get('connections'),list):
  from .private_settings import service_key
  from .venues import load_key
  existing={n:(load_key() if n=='easyscholar' else service_key(n)) for n in BASES}
  s['connections']=[{'id':'legacy-'+n,'service':n,'name':{'openalex':'OpenAlex','easyscholar':'easyScholar'}[n],'api_key':key,'enabled':True} for n,key in existing.items() if key]
 return s
def public(x):return {k:x.get(k) for k in ('id','service','name','enabled','last_test_at','last_test_ok')}|{'base_url':BASES[x['service']],'has_key':bool(x.get('api_key'))}
def write(s):
 for n in BASES:s[n]=None  # managed tombstones prevent legacy/environment resurrection
 atomic_json(path(),s)
@router.get('')
def list_connections():
 with LOCK:s=store()
 return {'connections':[public(x) for x in s['connections']]}
@router.post('')
def save_connection(body:dict):
 with LOCK:
  s=store();pid=body.get('id');old=next((x for x in s['connections'] if x['id']==pid),None)
  if pid and not old:raise HTTPException(404,'接口连接不存在')
  name=body.get('service',old.get('service') if old else None)
  if not isinstance(name,str) or name not in BASES:raise HTTPException(400,'请选择 OpenAlex 或 easyScholar')
  key=body.get('api_key','')
  if not isinstance(key,str) or len(key)>8192 or any(ord(c)<32 or ord(c)==127 for c in key):raise HTTPException(400,'密钥格式不正确')
  key=key.strip() or (old.get('api_key','') if old and old['service']==name else '')
  if not key:raise HTTPException(400,'请填写接口密钥')
  label=body.get('name') or name
  if not isinstance(label,str) or len(label)>100 or any(ord(c)<32 for c in label):raise HTTPException(400,'连接名称格式不正确')
  if not old and len(s['connections'])>=50:raise HTTPException(400,'最多保存50组接口')
  item={**(old or {}),'id':pid or uuid.uuid4().hex,'name':label.strip(),'service':name,'api_key':key,'enabled':body.get('enabled',True) is True}
  if not old or old.get('api_key')!=key:item.pop('last_test_ok',None);item.pop('last_test_at',None)
  s['connections']=[item if x['id']==pid else x for x in s['connections']] if old else s['connections']+[item]
  write(s)
 return {'connection':public(item),**list_connections()}
@router.delete('/{pid}')
def delete_connection(pid:str):
 with LOCK:
  s=store();n=len(s['connections']);s['connections']=[x for x in s['connections'] if x['id']!=pid]
  if n==len(s['connections']):raise HTTPException(404,'接口连接不存在')
  write(s)
 return list_connections()
@router.post('/{pid}/only')
def only_connection(pid:str):
 with LOCK:
  s=store();item=next((x for x in s['connections'] if x['id']==pid),None)
  if not item:raise HTTPException(404,'接口连接不存在')
  for x in s['connections']:
   if x['service']==item['service']:x['enabled']=x['id']==pid
  write(s)
 return list_connections()
@router.post('/{pid}/test')
def test_connection(pid:str):
 from .netclient import is_online
 import requests
 if not is_online():return {'ok':False,'message':'当前为离线模式'}
 with LOCK:item=next((x.copy() for x in store()['connections'] if x['id']==pid),None)
 if not item:raise HTTPException(404,'接口连接不存在')
 try:
  if item['service']=='openalex':
   r=requests.get(BASES['openalex']+'/works',params={'per-page':1,'select':'id','api_key':item['api_key']},timeout=(5,15),allow_redirects=False,**network.proxy_options(BASES[item['service']]))
   ok=r.status_code==200 and isinstance(r.json().get('results'),list)
  else:
   r=requests.get(BASES['easyscholar']+'/open/getPublicationRank',params={'secretKey':item['api_key'],'publicationName':'Nature'},timeout=(5,15),allow_redirects=False,**network.proxy_options(BASES[item['service']]))
   ok=r.status_code==200 and r.json().get('errCode') in (0,'0',None) and bool(r.json().get('data'))
 except (requests.RequestException,ValueError,AttributeError):ok=False
 with LOCK:
  s=store();current=next((x for x in s['connections'] if x['id']==pid),None)
  if current and current.get('api_key')==item['api_key']:
   current.update(last_test_at=datetime.now(timezone.utc).isoformat(),last_test_ok=ok);write(s)
 return {'ok':ok,'message':'连接成功' if ok else '连接未通过，请检查密钥、额度或网络'}
