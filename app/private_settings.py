"""Atomic, local-only configuration storage; never returned through APIs."""
import json
import os
import tempfile
import threading
from pathlib import Path
from .config import DATA_DIR

LOCK=threading.RLock()

def read_json(path,default=None):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else (default or {})
    except (OSError,ValueError):return default or {}

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:json.dump(value,out,ensure_ascii=False,indent=2)
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)

def service_key(name):
    values=read_json(DATA_DIR/'private'/'data_sources.local.json')
    if name in values:return values[name] or None
    if name=='openalex':return os.environ.get('OPENALEX_API_KEY') or None
    return None

def service_key_exists(name):
    return name in read_json(DATA_DIR/'private'/'data_sources.local.json')
