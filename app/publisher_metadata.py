"""Bounded publisher abstract recovery, with explicit DOI/title identity.

A normal, isolated installed browser can render a public page when ordinary
HTTP cannot. No user browser profile, cookies, credentials or paywall bypass.
"""
import json,re,os,tempfile,subprocess
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlparse
import requests
from . import network_settings as network
from .store import norm_doi,norm_title
from .netclient import is_online

HOSTS={'www.sciencedirect.com','www.nature.com','link.springer.com'}
class PublisherLookupError(Exception):pass
class IdentityError(PublisherLookupError):pass

class Parser(HTMLParser):
 def __init__(self):super().__init__(convert_charrefs=True);self.meta={};self.dois=set();self.blocks=[];self.depth=0;self.capture=None;self.h1=False;self.title=[]
 def handle_starttag(self,tag,attrs):
  d=dict(attrs)
  if tag=='meta':self.meta[(d.get('name') or d.get('property') or '').lower()]=d.get('content','')
  if tag=='a' and (d.get('href') or '').startswith(('https://doi.org/','http://dx.doi.org/')):self.dois.add(norm_doi(d['href']))
  if tag=='h1':self.h1=True
  if self.capture is not None:self.depth+=tag not in ('br','hr','img','input','meta','link')
  elif tag in ('div','section') and ('abstract author' in d.get('class','') or d.get('id') in ('Abs1','abstract','en-abstract')):self.capture=[];self.depth=1
 def handle_endtag(self,tag):
  if tag=='h1':self.h1=False
  if self.capture is not None:
   self.depth-=1
   if self.depth<=0:self.blocks.append(' '.join(self.capture));self.capture=None
 def handle_data(self,data):
  if self.h1:self.title.append(data)
  if self.capture is not None:self.capture.append(data)

def parse(html,p,url):
 parser=Parser();parser.feed(html)
 title=parser.meta.get('citation_title') or ' '.join(parser.title)
 doi=norm_doi(parser.meta.get('citation_doi') or parser.meta.get('dc.identifier') or '')
 expected=norm_doi(p.get('doi'))
 if not expected or doi and doi!=expected or not doi and expected not in parser.dois:raise IdentityError('出版商页面 DOI 未匹配，未合并')
 if norm_title(title)!=norm_title(p.get('title')):raise IdentityError('出版商页面题名未匹配，未合并')
 abstract=parser.meta.get('citation_abstract') or parser.meta.get('dc.description') or next((b for b in parser.blocks if re.match(r'\s*Abstract\b',b,re.I)), '')
 abstract=re.sub(r'^\s*Abstract\s*','',abstract,flags=re.I).strip()
 if len(abstract)<100:return None
 return {'abstract':abstract[:30000],'_source':'出版商公开摘要','_url':url,'_origin':'network_biblio'}

def checked_url(url):
 u=urlparse(url)
 if u.scheme!='https' or u.hostname not in HOSTS or u.username or u.password or u.port not in (None,443) or u.query or u.fragment:raise PublisherLookupError('出版商地址暂不支持自动读取')
 return url

def render_public_page(url):
 checked_url(url)
 candidates=[Path(os.environ.get('PROGRAMFILES(X86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',Path(os.environ.get('PROGRAMFILES','C:/Program Files'))/'Google/Chrome/Application/chrome.exe']
 browser=next((p for p in candidates if p.is_file()),None)
 if not browser:return None
 with tempfile.TemporaryDirectory(prefix='wenxian-public-',ignore_cleanup_errors=True) as profile:
  try:
   process=subprocess.Popen([str(browser),'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--user-data-dir='+profile,'--virtual-time-budget=5000','--dump-dom',*network.browser_flags(url),url],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   try:stdout,_=process.communicate(timeout=25)
   except subprocess.TimeoutExpired:
    if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    else:process.kill()
    process.wait(timeout=5);return None
   if len(stdout)>3*1024*1024:return None
   return stdout.decode('utf-8','replace') if process.returncode==0 else None
  except (OSError,subprocess.TimeoutExpired,ValueError):return None


def lookup(p,memo):
 if not is_online():return None
 meta=p.get('meta') or {}
 if not meta:
  try:meta=json.loads(p.get('meta_json') or '{}')
  except (ValueError,TypeError):meta={}
 url=meta.get('publisher_url')
 if not url:return None
 checked_url(url)
 if url in memo:return memo[url]
 if memo.get('_publisher_count',0)>=4:raise PublisherLookupError('本批出版商页面已达处理上限，可单独重试')
 memo['_publisher_count']=memo.get('_publisher_count',0)+1
 html=None;blocked=False
 try:
  with requests.get(url,timeout=(5,12),allow_redirects=False,headers={'User-Agent':'WenxianLuopan scholarly metadata'},**network.proxy_options(url)) as response:
   blocked=response.status_code in (401,403,429)
   if response.status_code==200:
    if len(response.content)>3*1024*1024:raise PublisherLookupError('出版商页面过大，未处理')
    html=response.text
 except requests.RequestException:blocked=True
 if blocked:html=render_public_page(url)
 if not html:raise PublisherLookupError('出版商页面本次不可读取；HTTP或浏览器访问未取得公开摘要')
 result=parse(html,p,url);memo[url]=result;return result
