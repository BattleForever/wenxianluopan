"""Bounded scholarly lookups with DOI identity and publisher evidence."""
import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import quote,urlparse,urljoin
import requests
from . import netclient,venues
from .store import norm_doi

NATURE_INDEX='https://www.nature.com/siteindex'
class IdentityConflict(ValueError):pass

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
    raw=netclient.cached_get_json(url)
    msg=(raw or {}).get('message') or {}
    if norm_doi(msg.get('DOI'))!=norm_doi(doi):raise IdentityConflict('DOI identity conflict')
    date=msg.get('published') or msg.get('published-print') or msg.get('published-online') or {}
    year=((date.get('date-parts') or [[None]])[0] or [None])[0]
    authors=[' '.join(filter(None,[a.get('given'),a.get('family')])) or a.get('name','') for a in msg.get('author',[]) if isinstance(a,dict)]
    return {'title':(msg.get('title') or [None])[0],'abstract':plain(msg.get('abstract')) or None,
            'authors':[a for a in authors if a],'year':year,'venue':(msg.get('container-title') or [None])[0],
            'doi':norm_doi(msg.get('DOI')),'type':msg.get('type'),'meta':{'issn':msg.get('ISSN') or []},
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
        if session is None:session=requests.Session();memo['_nature_session']=session
        current=url
        for _ in range(6):
            u=urlparse(current)
            if (u.scheme!='https' or u.hostname not in ('www.nature.com','nature.com','idp.nature.com')
                    or u.username or u.password or u.port not in (None,443)):
                raise ValueError('Unsupported publisher redirect')
            # Nature's public pages use an anonymous cookie handshake. Follow
            # only this publisher's fixed hosts, without any model/service key.
            r=session.get(current,headers={'User-Agent':'WenxianLuopan/0.9 (scholarly metadata)'},timeout=(5,15),allow_redirects=False)
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

def authorized_metrics(name):
    rec=venues.local_rank(name)
    cached=bool(rec and isinstance(rec.get('rank'),dict) and any(k in venues.rank_to_fields(rec['rank']) for k in ('impact_factor','cas_zone','jcr_zone')))
    if not cached:
        rec=None
        key=venues.load_key()
        if not key:return None
        rec,_=venues.online_rank(name,key)
    if not rec or not isinstance(rec.get('rank'),dict):return None
    if rec.get('name') and venues.norm_name(rec['name'])!=venues.norm_name(name):raise IdentityConflict('Journal identity conflict')
    out=venues.rank_to_fields(rec['rank']);out.update(_source='easyScholar 缓存' if cached else 'easyScholar',_url='https://www.easyscholar.cc/',
                                                  evidence={},metric_year=rec.get('metric_year'))
    for key in ('impact_factor','cas_zone','jcr_zone'):
        if out.get(key):out['evidence'][key]={'value':out[key],'year':rec.get('metric_year'),'queried_at':rec.get('at'),'url':out['_url']}
    return out
