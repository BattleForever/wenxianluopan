"""Layer-adjacent backend behaviour: source evidence, folders and automatic PDF lookup."""
import os,sys,tempfile,unittest,threading,json
from pathlib import Path
from unittest.mock import patch,MagicMock
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from fastapi import HTTPException
from app.main import app
from app import metadata_jobs as m,metadata_sources as sources,publisher_metadata as pub,zotero_link as z,pdf_resolver as resolver,pdf_access
from app.store import upsert_paper
from app.db import get_db,set_setting
TEXT='This controlled synthetic abstract reports a validated ultrasonic flow experiment with two measurement channels. '*4
class Publisher(unittest.TestCase):
 def setUp(self):
  set_setting('offline_mode','0')
  with get_db() as c:self.pid=upsert_paper(c,{'title':self.id(),'doi':'10.5555/fixture','authors':['Author'],'year':2025,'venue':'Journal'},'fixture','library')[0]
 def markup(self,doi='10.5555/fixture',title=None):return '<meta name="citation_doi" content="'+doi+'"><meta name="citation_title" content="'+(title or m.paper(self.pid)['title'])+'"><div class="abstract author"><h2>Abstract</h2><p>'+TEXT+'</p></div>'
 def test_matching_publisher_abstract_extracted(self):self.assertEqual(pub.parse(self.markup(),m.paper(self.pid),'https://www.sciencedirect.com/test')['abstract'],TEXT.strip())
 def test_wrong_doi_rejected(self):
  with self.assertRaises(pub.IdentityError):pub.parse(self.markup(doi='10.5555/wrong'),m.paper(self.pid),'https://www.sciencedirect.com/test')
 def test_wrong_title_rejected(self):
  with self.assertRaises(pub.IdentityError):pub.parse(self.markup(title='Different research'),m.paper(self.pid),'https://www.sciencedirect.com/test')
 def test_missing_api_abstract_uses_publisher_and_records_source(self):
  data={'abstract':TEXT,'_source':'出版商公开摘要','_url':'https://www.sciencedirect.com/test'}
  with patch.object(sources,'zotero_biblio',return_value=None),patch.object(sources,'zotero_pdf_paths',return_value=[]),patch.object(sources,'crossref',return_value={'_source':'Crossref'}),patch.object(sources,'openalex',return_value={'_source':'OpenAlex'}),patch.object(pub,'lookup',return_value=data):r=m.complete_one(self.pid,{'allow_network':True,'allow_ai':False,'include_metrics':False},threading.Event(),{})
  self.assertIn('abstract',r['filled']);self.assertTrue(any(e['code']=='abstract_not_provided' for e in r['errors']));self.assertEqual(m.paper(self.pid)['abstract'],TEXT)
  with get_db() as c:self.assertEqual(c.execute("SELECT source FROM metadata_field_sources WHERE paper_id=? AND field='abstract'",(self.pid,)).fetchone()[0],'出版商公开摘要')
 def test_offline_never_opens_publisher_browser(self):
  set_setting('offline_mode','1')
  with patch.object(pub,'render_public_page',side_effect=AssertionError('offline')):self.assertIsNone(pub.lookup(m.paper(self.pid),{}))
 def test_crossref_pii_maps_to_publisher_without_guessing(self):
  raw={'message':{'DOI':'10.1016/fixture','title':['Paper'],'link':[{'URL':'https://api.elsevier.com/content/article/PII:S123456789?httpAccept=text/xml'}]}}
  with patch.object(sources.netclient,'cached_get_json',return_value=raw):d=sources.crossref('10.1016/fixture')
  self.assertEqual(d['meta']['publisher_url'],'https://www.sciencedirect.com/science/article/abs/pii/S123456789')
class QuoteAlignment(unittest.TestCase):
 def test_unicode_spaces_recovered_as_literal_source(self):
  source='The accuracy is 95.8\u202f% to 97.1\u00a0%, above baseline.'
  q=m.exact_source_quote(source,'accuracy is 95.8 % to 97.1 %, above baseline.')
  self.assertIn(q,source);self.assertIn('\u202f',q)
 def test_changed_numbers_or_punctuation_still_rejected(self):
  source='The accuracy is 95.8\u202f%.'
  self.assertIsNone(m.exact_source_quote(source,'The accuracy is 95.9 %.'));self.assertIsNone(m.exact_source_quote(source,'The accuracy is 95.8 %!'))
 def test_non_contiguous_text_cannot_be_joined(self):
  self.assertIsNone(m.exact_source_quote('A useful method. Extra context. Final result.','A useful method. Final result.'))
class Fake:
 api_key='fixture-local'
 def __init__(self):self.rows=[{'key':'TOP','data':{'name':'Top'}}];self.creates=[]
 def get_collections(self):return self.rows
 def create_collection(self,name,parent=None):self.creates.append((name,parent));self.rows.append({'key':'NEW','data':{'name':name,'parentCollection':parent}});return 'NEW'
class Collections(unittest.TestCase):
 def setUp(self):self.cli=Fake()
 def test_new_subcollection_verified_and_duplicate_reused(self):
  with patch.object(z,'client',return_value=self.cli):
   first=z.new_collection(z.CollectionIn(name='Reading',parent_key='TOP'));second=z.new_collection(z.CollectionIn(name='reading',parent_key='TOP'))
  self.assertTrue(first['created']);self.assertFalse(second['created']);self.assertEqual(first['collection']['path'],'Top / Reading');self.assertEqual(len(self.cli.creates),1)
 def test_missing_parent_and_blank_name_cannot_create(self):
  with patch.object(z,'client',return_value=self.cli):
   for b in [z.CollectionIn(name='Name',parent_key='LOST'),z.CollectionIn(name='  ')]:
    with self.assertRaises(HTTPException):z.new_collection(b)
  self.assertFalse(self.cli.creates)
 def test_recent_destinations_unique_and_most_recent_first(self):
  with get_db() as c:
   c.execute('DELETE FROM zotero_destinations')
   c.executemany('INSERT INTO zotero_destinations VALUES(?,?,?,?,?)',[(100,'TOP','Top','ITEM1','2026-10-25T10:00'),(101,'TOP','Top','ITEM2','2026-10-25T11:00'),(102,'NEW','New','ITEM3','2026-10-25T12:00')])
  r=z.recent_destinations()['destinations'];self.assertEqual([x['collection_key'] for x in r],['NEW','TOP'])
class Resolver(unittest.TestCase):
 def setUp(self):set_setting('offline_mode','0')
 def test_official_resolver_request_and_cache(self):
  response=MagicMock(status_code=200);response.json.return_value=[{'url':'https://arxiv.org/pdf/2601.12345'}]
  with patch.object(resolver.requests,'post',return_value=response) as post:
   self.assertEqual(resolver.official_candidates('10.5555/cache'),['https://arxiv.org/pdf/2601.12345']);resolver.official_candidates('10.5555/cache');post.assert_called_once();self.assertEqual(post.call_args.kwargs['json'],{'doi':'10.5555/cache'})
 def test_private_or_unsupported_pdf_url_rejected(self):
  response=MagicMock(status_code=200);response.json.return_value=[{'url':'http://127.0.0.1/private'},{'url':'https://untrusted.invalid/a.pdf'}]
  with patch.object(resolver.requests,'post',return_value=response):self.assertEqual(resolver.official_candidates('10.5555/unsafe'),[])
 def test_automatic_lookup_does_not_require_a_second_click(self):
  with patch.object(pdf_access,'find',return_value={'ready':False,'has_public_link':False}),patch.object(pdf_access,'paper',return_value={'doi':'10.5555/auto'}),patch.object(resolver,'official_candidates',return_value=['https://arxiv.org/pdf/2601.12345']),patch.object(pdf_access,'download_url',return_value={'ready':True}) as download:
   with get_db() as c:pid=upsert_paper(c,{'title':'Automatic PDF resolver fixture'},'fixture','library')[0]
   self.assertTrue(resolver.acquire(pid)['ready']);download.assert_called_once()
if __name__=='__main__':unittest.main()
