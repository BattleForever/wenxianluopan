"""Startup contract: readiness, duplicate launches and recoverable installation."""
import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
T=tempfile.TemporaryDirectory();os.environ['PAPERNET_DATA_DIR']=T.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import launcher
from app import __version__
from app.config import DATA_DIR
from app import startup as s

class Startup(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.data=Path(self.tmp.name)
  self.health={'ok':True,'app_id':s.APP_ID,'data_id':s.data_identity(self.data),'version':__version__}
 def test_identity_does_not_expose_the_directory(self):
  value=s.data_identity(self.data);self.assertEqual(len(value),24);self.assertNotIn(str(self.data),value)
 def test_only_our_version_and_library_are_reused(self):
  self.assertTrue(s.matches(self.health,self.data,__version__))
  for field in ('app_id','data_id','version'):
   self.assertFalse(s.matches({**self.health,field:'other'},self.data,__version__))
 def test_browser_waits_for_actual_readiness(self):
  with patch.object(s,'read_health',side_effect=[None,self.health]),patch.object(s.time,'sleep'),patch.object(s.webbrowser,'open',return_value=True) as browser:
   self.assertTrue(s.open_ready('http://127.0.0.1:8765',self.data,__version__,1))
  browser.assert_called_once_with('http://127.0.0.1:8765')
 def test_service_exit_does_not_open_a_dead_page(self):
  process=MagicMock();process.poll.return_value=1
  with patch.object(s.webbrowser,'open') as browser:
   with self.assertRaises(RuntimeError):s.wait_ready('http://127.0.0.1:8765',self.data,__version__,1,process)
  browser.assert_not_called()
 def test_readiness_timeout_is_reported(self):
  with self.assertRaises(RuntimeError):s.wait_ready('http://127.0.0.1:8765',self.data,__version__,0)
 def test_second_start_reopens_existing_instance(self):
  private=self.data/'private';private.mkdir();(private/'server.local.json').write_text(json.dumps({'port':8767}))
  with patch.dict(os.environ,{'PAPERNET_DATA_DIR':str(self.data)}),patch.object(launcher,'read_health',return_value=self.health),patch.object(launcher,'ensure_runtime') as install,patch.object(launcher.subprocess,'Popen') as spawn:
   self.assertEqual(launcher.launch(True),'http://127.0.0.1:8767')
  install.assert_not_called();spawn.assert_not_called()
 def test_foreign_occupied_port_is_skipped(self):
  process=MagicMock();process.pid=12345
  with patch.dict(os.environ,{'PAPERNET_DATA_DIR':str(self.data),'PAPERNET_PORT':'8765'}),patch.object(launcher,'available',side_effect=[False,True]),patch.object(launcher,'read_health',return_value={'ok':True,'app_id':'another'}),patch.object(launcher,'ensure_runtime',return_value=Path(sys.executable)),patch.object(launcher.subprocess,'Popen',return_value=process) as spawn,patch.object(launcher,'wait_ready'):
   self.assertEqual(launcher.launch(True),'http://127.0.0.1:8766')
  self.assertEqual(spawn.call_args.kwargs['env']['PAPERNET_PORT'],'8766');self.assertEqual(spawn.call_args.kwargs['env']['PAPERNET_NO_BROWSER'],'1')
 def test_old_version_on_same_library_requires_explicit_close(self):
  with patch.dict(os.environ,{'PAPERNET_DATA_DIR':str(self.data),'PAPERNET_PORT':'8765'}),patch.object(launcher,'available',return_value=False),patch.object(launcher,'read_health',return_value={**self.health,'version':'older'}),patch.object(launcher.subprocess,'Popen') as spawn:
   with self.assertRaises(RuntimeError):launcher.launch(True)
  spawn.assert_not_called()
 def test_failed_service_is_cleaned_up(self):
  process=MagicMock();process.poll.return_value=None
  with patch.dict(os.environ,{'PAPERNET_DATA_DIR':str(self.data),'PAPERNET_PORT':'8765'}),patch.object(launcher,'available',return_value=True),patch.object(launcher,'ensure_runtime',return_value=Path(sys.executable)),patch.object(launcher.subprocess,'Popen',return_value=process),patch.object(launcher,'wait_ready',side_effect=RuntimeError('failed')):
   with self.assertRaises(RuntimeError):launcher.launch(True)
  process.terminate.assert_called_once()
 def runtime_path(self):
  f=self.data/'source/.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python');f.parent.mkdir(parents=True);f.touch();return f
 def test_incomplete_environment_is_repaired_on_next_start(self):
  runtime=self.runtime_path()
  with patch.dict(os.environ,{'PAPERNET_PYTHON':''}),patch.object(launcher,'ROOT',self.data/'source'),patch.object(launcher.subprocess,'run',side_effect=[MagicMock(returncode=1),MagicMock(returncode=0),MagicMock(returncode=0)]) as run:
   self.assertEqual(launcher.ensure_runtime(self.data),runtime)
  self.assertEqual(run.call_count,3);self.assertIn('pip',run.call_args_list[1].args[0])
 def test_failed_installation_does_not_claim_success(self):
  self.runtime_path()
  with patch.dict(os.environ,{'PAPERNET_PYTHON':''}),patch.object(launcher,'ROOT',self.data/'source'),patch.object(launcher.subprocess,'run',side_effect=[MagicMock(returncode=1),MagicMock(returncode=1)]):
   with self.assertRaises(RuntimeError):launcher.ensure_runtime(self.data)
 def test_missing_explicit_runtime_is_not_silently_replaced(self):
  with patch.dict(os.environ,{'PAPERNET_PYTHON':str(self.data/'missing.exe')}),patch.object(launcher.subprocess,'run') as run:
   with self.assertRaises(RuntimeError):launcher.ensure_runtime(self.data)
  run.assert_not_called()
 def test_concurrent_starts_are_serialized(self):
  with launcher.startup_lock(self.data):
   with self.assertRaises(RuntimeError):
    with launcher.startup_lock(self.data):pass
 def test_health_identifies_the_actual_data_directory(self):
  from app.main import app
  from fastapi.testclient import TestClient
  with TestClient(app) as client:r=client.get('/api/health').json()
  self.assertEqual(r['app_id'],s.APP_ID);self.assertEqual(r['data_id'],s.data_identity(T.name))

if __name__=='__main__':unittest.main()
