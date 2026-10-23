"""Small local-only lifecycle journal, without request bodies or credentials."""
from datetime import datetime, timezone
import os
import threading
from . import __version__
from .config import DATA_DIR
from .private_settings import atomic_json, read_json
LOCK=threading.Lock()

def record(event, reason=''):
    with LOCK:
        path=DATA_DIR/'private'/'lifecycle.local.json'
        events=read_json(path).get('events',[])[-39:]
        events.append({'time':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),'version':__version__,
                       'event':event,'reason':reason})
        atomic_json(path,{'events':events})
