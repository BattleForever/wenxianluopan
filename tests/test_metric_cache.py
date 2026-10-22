"""Legacy cache compatibility, sanitised imports and honest metric dates."""
import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import venues,data_sources,metadata_sources

class MetricCache(unittest.TestCase):
 def setUp(self):venues._cache=None;venues.CACHE_PATH.unlink(missing_ok=True)
 def row(self,**kw):return {'name':'Test & Journal','k':'test and journal','rank':{'sciif':'8.8','sci':'Q1','sciUp':'1区'},'at':'2026-09-26 01:58:06',**kw}
 def upload(self,row):return data_sources.import_metrics_cache({'text':json.dumps(row,ensure_ascii=False)})
 def test_legacy_key_uses_journal_name_and_ampersand_alias(self):
  venues.CACHE_PATH.write_text(json.dumps(self.row()),encoding='utf-8')
  self.assertEqual(venues.local_rank('Test and Journal')['rank']['sci'],'Q1')
 def test_import_discards_credentials_and_raw_payload(self):
  self.upload(self.row(api_key='synthetic-never-stored',raw={'secretKey':'synthetic-never-stored'}))
  content=venues.CACHE_PATH.read_text(encoding='utf-8');self.assertNotIn('synthetic-never-stored',content)
  self.assertEqual(venues.local_rank('Test & Journal')['rank']['sciif'],'8.8')
 def test_cache_query_date_does_not_become_metric_year(self):
  self.upload(self.row());result=metadata_sources.authorized_metrics('Test & Journal')
  self.assertIsNone(result['metric_year']);self.assertEqual(result['_source'],'easyScholar 缓存')
  self.assertEqual(result['evidence']['cas_zone']['queried_at'],'2026-09-26 01:58:06')
 def test_explicit_metric_year_is_preserved(self):
  self.upload(self.row(metric_year=2024));self.assertEqual(metadata_sources.authorized_metrics('Test & Journal')['metric_year'],2024)
 def test_invalid_records_and_empty_ranks_are_counted(self):
  text='\n'.join([json.dumps(self.row()),'null','[]','invalid',json.dumps(self.row(name='No ranks',rank={}))])
  result=data_sources.import_metrics_cache({'text':text});self.assertEqual(result['imported'],1);self.assertEqual(result['rejected'],4)
 def test_empty_and_oversize_uploads_rejected(self):
  for text in ('', 'x'*8_000_001):
   with self.assertRaises(data_sources.HTTPException):data_sources.import_metrics_cache({'text':text})
 def test_failed_atomic_import_preserves_previous_cache(self):
  self.upload(self.row());before=venues.CACHE_PATH.read_bytes()
  with patch.object(data_sources.os,'replace',side_effect=OSError('disk')):
   with self.assertRaises(data_sources.HTTPException):self.upload(self.row(name='New Journal'))
  self.assertEqual(venues.CACHE_PATH.read_bytes(),before);self.assertIsNone(venues.local_rank('New Journal'))
 def test_negative_cache_does_not_block_configured_online_lookup(self):
  with patch.object(venues,'local_rank',return_value={'name':'Test & Journal','rank':None}),patch.object(venues,'load_key',return_value='synthetic'),patch.object(venues,'online_rank',return_value=(self.row(),False)) as lookup:
   result=metadata_sources.authorized_metrics('Test & Journal')
  lookup.assert_called_once();self.assertEqual(result['_source'],'easyScholar')

if __name__=='__main__':unittest.main()
