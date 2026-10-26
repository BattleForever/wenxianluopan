from pathlib import Path
import requests,json
from datetime import datetime,timezone
from urllib.parse import urlparse
from . import network_settings as network
from .db import get_db
from .store import norm_doi
from . import pdf_access
from .netclient import is_online

SERVICE='https://services.zotero.org/oa/search'
def official_candidates(doi):
 doi=norm_doi(doi)
 if not doi or not is_online():return []
 key='zotero-oa:'+doi
 with get_db() as c:cached=c.execute('SELECT body,fetched_at FROM fetch_cache WHERE key=?',(key,)).fetchone()
 if cached:
  try:
   dt=datetime.fromisoformat(cached['fetched_at']).replace(tzinfo=timezone.utc)
   if (datetime.now(timezone.utc)-dt).total_seconds()<86400:return json.loads(cached['body'])
  except (ValueError,TypeError):pass
 r=requests.post(SERVICE,json={'doi':doi},headers={'Content-Type':'application/json'},timeout=(5,10),allow_redirects=False,**network.proxy_options(SERVICE))
 if r.status_code!=200:raise ValueError('官方公开全文查询本次未完成')
 rows=r.json()
 if not isinstance(rows,list):raise ValueError('官方查询未返回有效结果')
 urls=[]
 for row in rows[:6]:
  url=row.get('url') if isinstance(row,dict) else None
  if not url:continue
  try:pdf_access.checked_url(url)
  except Exception:continue
  if url not in urls:urls.append(url)
 with get_db() as c:c.execute('INSERT OR REPLACE INTO fetch_cache(key,url,status,body) VALUES(?,?,?,?)',(key,SERVICE,200,json.dumps(urls)))
 return urls

def acquire(pid):
 found=pdf_access.find(pid)
 if found.get('ready'):return found
 tried=[]
 if found.get('has_public_link'):
  try:return pdf_access.download(pid)
  except Exception:tried.append('已有公开地址未能下载')
 p=pdf_access.paper(pid)
 try:urls=official_candidates(p.get('doi'))
 except Exception:urls=[];tried.append('官方公开全文查询本次未完成')
 for url in urls:
  try:
   result=pdf_access.download_url(pid,url)
   with get_db() as c:c.execute('UPDATE papers SET oa_pdf_url=? WHERE id=?',(url,pid))
   return {**result,'resolver':'zotero_official_oa'}
  except Exception:tried.append('公开全文候选未能下载')
 return {**pdf_access.status(pid),'automatic_lookup':True,'message':'已自动查找，未取得可访问的PDF全文；书目与阅读内容仍保留','lookup_notes':list(dict.fromkeys(tried))}
