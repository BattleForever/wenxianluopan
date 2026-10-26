"""Publisher page and PDF-wrapper discovery, independent of browser extensions."""
from html.parser import HTMLParser
from urllib.parse import urljoin,urlparse
import re

class Links(HTMLParser):
    def __init__(self,base):super().__init__();self.base=base;self.urls=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);value=None
        if tag=='meta' and (a.get('name') or a.get('property') or '').lower() in ('citation_pdf_url','wkhealth_pdf_url','bepress_citation_pdf_url'):value=a.get('content')
        elif tag in ('iframe','embed'):value=a.get('src')
        elif tag=='object':value=a.get('data')
        elif tag=='a' and (a.get('type')=='application/pdf' or re.search(r'\.pdf(?:[?#]|$)|/pdfft|/stamp(?:PDF)?/',a.get('href',''),re.I)):value=a.get('href')
        if not value:return
        url=urljoin(self.base,value);u=urlparse(url)
        if u.scheme=='https' and not u.username and not u.password and url not in self.urls and len(self.urls)<12:self.urls.append(url)

def candidates(html,base):
    parser=Links(base);parser.feed(html[:2*1024*1024]);return parser.urls
