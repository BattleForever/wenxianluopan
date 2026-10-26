"""Bounded scholarly lookups with DOI identity and publisher evidence."""
import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import quote,urlparse,urljoin
import requests
from . import network_settings as network
from . import netclient,venues
from .store import norm_doi

NATURE_INDEX='https://www.nature.com/siteindex'
class IdentityConflict(ValueError):pass

def zotero_biblio(paper):
    """Read only; absent Zotero does not disable online discovery."""
    import json
    from .zotero_client import ZoteroClient
    client=ZoteroClient()
    meta=paper.get('meta') or {}
    if not meta:
        try:meta=json.loads(paper.get('meta_json') or '{}')
        except (ValueError,TypeError):meta={}
    item=client.get_item(meta['zotero_key']) if meta.get('zotero_key') else client.find_by_identifier(doi=paper['doi']) if paper.get('doi') else None
    if not item:return None
    d=item.get('data') or {}
    if paper.get('doi') and norm_doi(d.get('DOI'))!=norm_doi(paper['doi']):raise IdentityConflict('Zotero DOI mismatch')
    year=re.search(r'\b(19|20)\d{2}\b',d.get('date') or '')
    authors=[(x.get('name') or ' '.join(filter(None,[x.get('firstName'),x.get('lastName')]))) for x in d.get('creators',[]) if isinstance(x,dict)]
    return {'title':d.get('title'),'abstract':d.get('abstractNote'),'authors':authors,'year':int(year[0]) if year else None,'venue':d.get('publicationTitle'),
            '_source':'Zotero','_origin':'zotero','_url':None}

def zotero_pdf_paths(paper,memo):
    """One lock-safe attachment index snapshot per completion batch; no live DB queries."""
    from pathlib import Path
    import tempfile,sqlite3
    from .db import get_setting
    from .pdf_guide import _copy_zotero_index
    key='_zotero_pdf_index'
    if key not in memo:
        mapping={};root=Path(get_setting('zotero_data_dir') or Path.home()/'Zotero');db=root/'zotero.sqlite'
        if db.is_file():
            try:
                with tempfile.TemporaryDirectory(prefix='wenxian-metadata-') as td:
                    copied=_copy_zotero_index(db,Path(td))
                    with sqlite3.connect(copied) as c:
                        query='''SELECT v.value,a.path,i.key FROM itemAttachments a JOIN items i ON i.itemID=a.itemID
                          JOIN itemData d ON d.itemID=a.parentItemID JOIN fields f ON f.fieldID=d.fieldID
                          JOIN itemDataValues v ON v.valueID=d.valueID WHERE f.fieldName='DOI'
                          AND NOT EXISTS(SELECT 1 FROM deletedItems x WHERE x.itemID=a.parentItemID OR x.itemID=a.itemID)'''
                        for doi,path,item_key in c.execute(query):
                            if not path or not path.startswith('storage:'):continue
                            folder=root/'storage'/item_key;file=folder/path[8:]
                            if file.suffix.lower()=='.pdf' and file.is_file() and file.resolve().is_relative_to(folder.resolve()):mapping.setdefault(norm_doi(doi),[]).append(str(file))
            except (OSError,sqlite3.Error):pass
        memo[key]=mapping
    return memo[key].get(norm_doi(paper.get('doi')),[])[:5]

class TextHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.links=[];self.current=None;self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.skip+=1
        if tag=='a':self.current=[dict(attrs).get('href',''),[]]
        if tag in ('p','div','li','br','td','tr','h1','h2','h3'):self.parts.append(' ')
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
        if tag=='a' and self.current:self.links.append((self.current[0],''.join(self.current[1]).strip()));self.current=None
    def handle_data(self,data):
        if not self.skip:
            self.parts.append(data)
            if self.current:self.current[1].append(data)
    @property
    def text(self):return re.sub(r'\s+',' ',''.join(self.parts)).strip()

def plain(value):
    p=TextHTML();p.feed(str(value or ''));return p.text

def crossref(doi):
    if not norm_doi(doi):return None
    url='https://api.crossref.org/works/'+quote(norm_doi(doi),safe='')
    raw=netclient.cached_get_json(url,max_age=86400)
    msg=(raw or {}).get('message') or {}
    if norm_doi(msg.get('DOI'))!=norm_doi(doi):raise IdentityConflict('DOI identity conflict')
    date=msg.get('published') or msg.get('published-print') or msg.get('published-online') or {}
    year=((date.get('date-parts') or [[None]])[0] or [None])[0]
    authors=[' '.join(filter(None,[a.get('given'),a.get('family')])) or a.get('name','') for a in msg.get('author',[]) if isinstance(a,dict)]
    links=' '.join(x.get('URL','') for x in msg.get('link',[]) if isinstance(x,dict));pii=re.search(r'PII[:/]([A-Za-z0-9]+)',links,re.I)
    publisher_url='https://www.sciencedirect.com/science/article/abs/pii/'+pii[1] if pii and norm_doi(doi).startswith('10.1016/') else None
    if not publisher_url:
        from .publisher_metadata import checked_url,PublisherLookupError
        for candidate in [msg.get('URL'),*((msg.get('resource') or {}).get('primary') or {}).values()]:
            if not isinstance(candidate,str):continue
            try:publisher_url=checked_url(candidate);break
            except PublisherLookupError:continue
    return {'title':(msg.get('title') or [None])[0],'abstract':plain(msg.get('abstract')) or None,
            'authors':[a for a in authors if a],'year':year,'venue':(msg.get('container-title') or [None])[0],
            'doi':norm_doi(msg.get('DOI')),'type':msg.get('type'),'meta':{'issn':msg.get('ISSN') or [],'publisher_url':publisher_url},
            '_source':'Crossref','_url':url}

def openalex(paper):
    raw=netclient.oa_work_by_doi(paper['doi']) if paper.get('doi') else netclient.oa_work_by_id(paper['openalex_id']) if paper.get('openalex_id') else None
    if not raw:return None
    if paper.get('doi') and norm_doi(raw.get('doi'))!=norm_doi(paper['doi']):raise IdentityConflict('DOI identity conflict')
    if not paper.get('doi') and paper.get('openalex_id') and str(raw.get('id','')).rstrip('/').split('/')[-1]!=str(paper['openalex_id']).rstrip('/').split('/')[-1]:raise IdentityConflict('OpenAlex identity conflict')
    result=netclient.oa_work_to_paper(raw);loc=raw.get('primary_location') or {};src=loc.get('source') or {}
    result.update(meta={'source_id':src.get('id'),'issn':src.get('issn') or []},_source='OpenAlex',_url=raw.get('id'))
    return result

def publisher_page(url,memo):
    u=urlparse(url)
    if u.scheme!='https' or u.hostname not in ('www.nature.com','nature.com') or u.username or u.query or u.fragment:
        raise ValueError('Unsupported publisher URL')
    if url not in memo:
        session=memo.get('_nature_session')
        if session is None:session=network.prepare_session(requests.Session());memo['_nature_session']=session
        current=url
        for _ in range(6):
            u=urlparse(current)
            if (u.scheme!='https' or u.hostname not in ('www.nature.com','nature.com','idp.nature.com')
                    or u.username or u.password or u.port not in (None,443)):
                raise ValueError('Unsupported publisher redirect')
            # Nature's public pages use an anonymous cookie handshake. Follow
            # only this publisher's fixed hosts, without any model/service key.
            r=session.get(current,headers={'User-Agent':'WenxianLuopan/0.9 (scholarly metadata)'},timeout=(5,15),allow_redirects=False,**network.proxy_options(current))
            if r.status_code not in (301,302,303,307,308):break
            location=r.headers.get('Location')
            if not location:raise RuntimeError('Publisher redirect missing')
            current=urljoin(current,location)
        if urlparse(current).hostname not in ('www.nature.com','nature.com'):raise RuntimeError('Publisher page not available')
        if r.status_code!=200:raise RuntimeError('Publisher unavailable')
        if len(r.content)>2_000_000:raise ValueError('Publisher page too large')
        p=TextHTML();p.feed(r.text);memo[url]=p
    return memo[url]

def nature_metrics(name,memo):
    # Resolve exact journal names through the publisher's own index; no
    # hard-coded IF, journal-name -> score, or third-party metric substitution.
    index=publisher_page(NATURE_INDEX,memo)
    links=[href for href,title in index.links if venues.norm_name(title)==venues.norm_name(name)]
    for href in links:
        parsed=urlparse(urljoin(NATURE_INDEX,href));parts=[x for x in parsed.path.split('/') if x]
        if parsed.hostname not in ('www.nature.com','nature.com') or not parts:continue
        if not re.fullmatch(r'[a-z][a-z0-9-]{1,45}',parts[0]):continue
        url='https://www.nature.com/'+parts[0]+'/journal-information'
        page=publisher_page(url,memo)
        for match in re.finditer(r'\bJournal Impact Factor\s*:?\s*(\d+(?:\.\d+)?)\s*\((20\d{2})\)',page.text,re.I):
            if re.search(r'5[- ]year\s*$',page.text[max(0,match.start()-12):match.start()],re.I):continue
            value=match[1];year=int(match[2]);proof=match[0]
            return {'impact_factor':value,'_source':'Nature Portfolio','_url':url,'metric_year':year,
                    'evidence':{'impact_factor':{'value':value,'year':year,'quote':proof,'url':url}},
                    'source_hash':hashlib.sha256(page.text.encode()).hexdigest()}
    return None

def authorized_metrics(name,allow_network=True,refresh=False):
    rec=venues.local_rank(name)
    cached=bool(rec and isinstance(rec.get('rank'),dict) and any(k in venues.rank_to_fields(rec['rank']) for k in ('impact_factor','cas_zone','jcr_zone')))
    if not cached or (refresh and allow_network):
        previous=rec if cached else None
        key=venues.load_key() if allow_network else None
        if key:
            fresh,_=venues.online_rank(name,key)
            if fresh:rec=fresh;cached=False
            else:rec=previous
        else:rec=previous
    if not rec or not isinstance(rec.get('rank'),dict):return None
    if rec.get('name') and venues.norm_name(rec['name'])!=venues.norm_name(name):raise IdentityConflict('Journal identity conflict')
    out=venues.rank_to_fields(rec['rank']);out.update(_source='easyScholar 缓存' if cached else 'easyScholar',_url='https://www.easyscholar.cc/',
                                                  evidence={},metric_year=rec.get('metric_year'))
    for key in ('impact_factor','cas_zone','jcr_zone'):
        if out.get(key):out['evidence'][key]={'value':out[key],'year':rec.get('metric_year'),'queried_at':rec.get('at'),'url':out['_url']}
    return out
