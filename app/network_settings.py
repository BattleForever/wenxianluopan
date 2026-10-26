"""One explicit outbound route: direct by default, system, or custom proxy.

Credentials remain in the user's private data directory. Loopback Zotero and
local model connections never traverse a proxy. TLS verification is unchanged.
"""
import ipaddress,time,urllib.request
from urllib.parse import urlparse,urlunparse,quote,unquote
import requests
from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,Field
from typing import Literal
from .config import DATA_DIR
from .private_settings import LOCK,read_json,atomic_json

router=APIRouter(prefix='/api/network')
FILE=DATA_DIR/'private'/'network.local.json'
DIRECT={'http':'','https':'','all':''}
class NetworkIn(BaseModel):
 mode:Literal['direct','system','custom']='direct'
 address:str=Field(default='',max_length=2048)
 username:str=Field(default='',max_length=200)
 password:str=Field(default='',max_length=1000)
 clear_password:bool=False

def saved():
 with LOCK:return read_json(FILE,{'mode':'direct'})

def validated(value,old=None):
 data=value.model_dump(exclude_unset=True) if isinstance(value,NetworkIn) else dict(value);old=old or {};mode=data.get('mode','direct')
 address=data.get('address',old.get('address','')).strip();username=data.get('username',old.get('username','')).strip();password='' if data.get('clear_password') else data.get('password') or old.get('password','')
 if address:
  try:
   u=urlparse(address);port=u.port
   if u.scheme not in ('http','https','socks5','socks5h') or not u.hostname or port is None or not 1<=port<=65535 or u.path not in ('','/') or u.query or u.fragment:raise ValueError()
   if u.username is not None:username=unquote(u.username);password=unquote(u.password or '')
   host='['+u.hostname+']' if ':' in u.hostname else u.hostname;address=u.scheme+'://'+host+':'+str(port)
  except ValueError:raise HTTPException(422,'代理地址请填写协议、主机和端口，例如 http://127.0.0.1:7890')
 if mode=='custom' and not address:raise HTTPException(422,'请填写自定义代理地址')
 return {'mode':mode,'address':address,'username':username,'password':password}

@router.get('')
def public():
 d=saved();return {'mode':d.get('mode','direct'),'address':d.get('address',''),'username':d.get('username',''),'password_saved':bool(d.get('password'))}

@router.post('')
def save(body:NetworkIn):
 with LOCK:atomic_json(FILE,validated(body,saved()))
 return {'saved':True,**public()}

def local_destination(url):
 host=(urlparse(url).hostname or '').lower()
 if host=='localhost' or host.endswith('.localhost'):return True
 try:return ipaddress.ip_address(host).is_loopback
 except ValueError:return False

def proxy_options(url,policy=None):
 if local_destination(url):return {'proxies':dict(DIRECT)}
 d=policy or saved();mode=d.get('mode','direct')
 if mode=='direct':return {'proxies':dict(DIRECT)}
 if mode=='system':
  if urllib.request.proxy_bypass(urlparse(url).hostname or ''):return {'proxies':dict(DIRECT)}
  system=urllib.request.getproxies();return {'proxies':{k:system.get(k,'') for k in ('http','https','all')}}
 u=urlparse(d.get('address',''));auth=''
 if d.get('username'):auth=quote(d['username'],safe='')+':'+quote(d.get('password',''),safe='')+'@'
 target=urlunparse((u.scheme,auth+u.netloc,'','','',''))
 return {'proxies':{'http':target,'https':target,'all':target}}

def prepare_session(session):
 # System settings are resolved explicitly in proxy_options, so neither stale
 # Session state nor HTTP(S)_PROXY can silently override the selected mode.
 session.trust_env=False
 return session

def browser_flags(url):
 proxies=proxy_options(url)['proxies'];target=proxies.get('https') or proxies.get('all') or proxies.get('http')
 if not target:return ['--no-proxy-server']
 u=urlparse(target)
 if u.username or u.password:raise ValueError('认证代理暂不用于浏览器回退；接口请求仍可使用')
 target=target.replace('socks5h://','socks5://',1)
 return ['--proxy-server='+target,'--proxy-bypass-list=localhost;127.0.0.1;[::1]']

@router.post('/test')
def test(body:NetworkIn):
 from .db import get_bool_setting
 if get_bool_setting('offline_mode'):return {'ok':False,'message':'当前暂停联网，请先开启资料获取','results':[]}
 policy=validated(body,saved());results=[]
 for label,url in [('学术接口','https://api.crossref.org/works?rows=0'),('出版商','https://www.sciencedirect.com/')]:
  started=time.monotonic()
  try:
   with prepare_session(requests.Session()) as session:
    response=session.get(url,timeout=(5,10),allow_redirects=False,stream=True,**proxy_options(url,policy))
    status=response.status_code;response.close()
   results.append({'name':label,'reachable':True,'status':status,'milliseconds':round((time.monotonic()-started)*1000)})
  except requests.RequestException:results.append({'name':label,'reachable':False,'message':'连接未成功，请检查代理地址、端口或网络'})
 return {'ok':all(r['reachable'] for r in results),'mode':policy['mode'],'results':results,'message':'连接已测试；HTTP 403/429表示来源拒绝或限流，不代表已取得摘要或PDF'}
