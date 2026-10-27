"""Owned capture-browser process. No daily browser profile, keys or cookies are copied."""
import json,sys,time,ipaddress,socket,os,subprocess
from pathlib import Path
from urllib.parse import urlparse,urljoin

def matches_pdf(raw,title,doi):
    import re
    from .pdf_engine import PDFDocument
    try:
        with PDFDocument(raw) as doc:
            if doc.encrypted:return False
            first=doc.page_text(0) if len(doc) else '';text=' '.join(doc.page_text(i) for i in range(min(2,len(doc))))
        norm=lambda s:re.sub(r'[^a-z0-9\u4e00-\u9fff]+','',(s or '').lower())
        return bool(doi and doi.lower() in first.lower() or len(norm(title))>=16 and norm(title) in norm(text))
    except Exception:return False

def remote_url(url):
    try:
        u=urlparse(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443):return False
        addresses=socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (ValueError,OSError):return False

def run(file):
    import requests
    from playwright.sync_api import sync_playwright
    config=json.loads(Path(file).read_text(encoding='utf-8'));Path(file).unlink(missing_ok=True)
    from . import network_settings
    session=requests.Session();session.trust_env=False
    base=config['callback']+'/api/pdf-capture/'+config['id'];headers={'X-Capture-Token':config['token']};cancel=Path(config['cancel'])
    host=None;attempt_count=0
    def stage(value,reason=None,status=None):
        try:session.post(base+'/stage',json={'stage':value,'reason':reason,'http_status':status,'page_host':host,'attempted':attempt_count},headers=headers,timeout=10)
        except requests.RequestException:pass
    def submit(raw,method):
        if len(raw)>40*1024*1024 or not raw.startswith(b'%PDF-'):return False
        if not matches_pdf(raw,config.get('title'),config.get('doi')):stage('access','identity_mismatch');return False
        try:
            response=session.post(base+'/file',data=raw,headers={**headers,'Content-Type':'application/pdf','X-Capture-Method':method},timeout=180)
            return response.status_code==200 and response.json().get('saved') is True
        except (requests.RequestException,ValueError):return False
    if not remote_url(config['url']):stage('failed');return
    stage('opening');downloaded=[];responses=[];focus=Path(config['cancel']).with_suffix('.focus')
    def download(d):
        try:
            path=d.path()
            if path and Path(path).stat().st_size<=40*1024*1024:downloaded.append(Path(path).read_bytes())
        except Exception:pass
    with sync_playwright() as pw:
        args=[];launch={'headless':bool(config.get('headless')),'channel':'chromium','accept_downloads':True,'viewport':{'width':1320,'height':900}}
        proxies=network_settings.proxy_options(config['url'])['proxies'];proxy=proxies.get('https') or proxies.get('all')
        if proxy:
            from urllib.parse import unquote
            u=urlparse(proxy);host='['+u.hostname+']' if ':' in u.hostname else u.hostname
            launch['proxy']={'server':u.scheme.replace('socks5h','socks5')+'://'+host+':'+str(u.port),'bypass':'localhost,127.0.0.1,[::1]'}
            if u.username:launch['proxy'].update(username=unquote(u.username),password=unquote(u.password or ''))
        else:args+=['--no-proxy-server']
        launch['args']=args
        if not Path(pw.chromium.executable_path).is_file():
            stage('preparing');install_env=dict(os.environ)
            for key in list(install_env):
                if key.lower() in ('http_proxy','https_proxy','all_proxy'):install_env.pop(key,None)
            if proxy:install_env.update(HTTPS_PROXY=proxy,HTTP_PROXY=proxy)
            installer=subprocess.Popen([sys.executable,'-m','playwright','install','chromium'],env=install_env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=time.monotonic()+300
            while installer.poll() is None and time.monotonic()<deadline and not cancel.exists():time.sleep(.5)
            if installer.poll() is None:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(installer.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                else:installer.terminate()
                stage('cancelled' if cancel.exists() else 'failed',None if cancel.exists() else 'browser_unavailable');return
            if installer.returncode:stage('failed','browser_unavailable');return
        try:context=pw.chromium.launch_persistent_context(config['profile'],**launch)
        except Exception:stage('failed','browser_unavailable');return
        try:
            def pdf_response(response):
                if 'application/pdf' in response.headers.get('content-type','').lower() and remote_url(response.url):responses.append(response)
            context.on('response',pdf_response)
            for existing in context.pages:existing.on('download',download)
            context.on('page',lambda p:p.on('download',download))
            page=context.new_page()
            try:
                navigation=page.goto(config['url'],wait_until='domcontentloaded',timeout=45000)
                host=urlparse(page.url).hostname
                if navigation and navigation.status in (401,403,418,429):stage('access','publisher_denied',navigation.status)
            except Exception:pass
            stage('identifying');attempted={};last_identified=0;started=time.monotonic();waiting=False
            discovered=[];clicked=set()
            while time.monotonic()-started<300 and context.pages and not cancel.exists():
                if page.is_closed():stage('cancelled');return
                if focus.exists():
                    focus.unlink(missing_ok=True)
                    try:page.bring_to_front()
                    except Exception:pass
                if responses:
                    response=responses.pop(0)
                    try:
                        if int(response.headers.get('content-length','0'))<=40*1024*1024 and submit(response.body(),'browser_download'):return
                    except Exception:pass
                if downloaded:
                    stage('downloading')
                    if submit(downloaded.pop(0),'browser_download'):return
                links=[]
                for current in context.pages:
                    try:
                        if current.is_closed() or not remote_url(current.url):continue
                        from .publisher_pdf import candidates
                        links.extend(candidates(current.content(),current.url))
                        # Publishers may generate a PDF tab/download only after a
                        # real click. Follow explicit PDF controls once per page.
                        if current.url not in clicked:
                            control=current.get_by_role('link',name=__import__('re').compile(r'^(?:View|Download|Full[ -]?Text|Get)?\s*(?:Full[ -]?Text\s*)?PDF(?:\s*\([^)]*\))?$',__import__('re').I)).first
                            if control.count() and control.is_visible():
                                clicked.add(current.url);control.click(timeout=2500,no_wait_after=True)
                    except Exception:pass
                for url in list(dict.fromkeys(links+discovered))[:24]:
                    url=urljoin(page.url,url)
                    if time.monotonic()-attempted.get(url,0)<35 or not remote_url(url):continue
                    attempted[url]=time.monotonic();attempt_count=len(attempted);host=urlparse(page.url).hostname;stage('downloading')
                    try:
                        response=context.request.get(url,headers={'Referer':page.url},timeout=18000)
                        size=int(response.headers.get('content-length','0'))
                        if response.status==200 and size<=40*1024*1024:
                            raw=response.body()
                            if submit(raw,'page_metadata'):return
                            if not raw.startswith(b'%PDF-'):
                                from .publisher_pdf import candidates
                                if len(raw)<=2*1024*1024:
                                    discovered.extend(u for u in candidates(raw.decode('utf-8','replace'),url) if u not in discovered)
                                stage('access','not_pdf',response.status)
                        elif response.status in (401,403,418,429):stage('access','publisher_denied',response.status)
                        response.dispose()
                    except Exception:pass
                    if cancel.exists():break
                if not waiting:stage('access');waiting=True
                try:page.wait_for_timeout(700)
                except Exception:break
            stage('cancelled' if cancel.exists() else 'failed',None if cancel.exists() else 'timeout')
        finally:focus.unlink(missing_ok=True);context.close()

if __name__=='__main__':
    try:run(sys.argv[1])
    except Exception:pass
