"""Run suites in separate processes because each uses its own data directory."""
import subprocess
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
(root/'evidence').mkdir(exist_ok=True)
failed=[]
for suite in sorted((root/'tests').glob('test_*.py')):
    print('\n'+suite.name,flush=True)
    if subprocess.run([sys.executable,str(suite)],cwd=root).returncode:
        failed.append(suite.name)
print('\nFAILED: '+(', '.join(failed) if failed else 'none'))
raise SystemExit(bool(failed))
