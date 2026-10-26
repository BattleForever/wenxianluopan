"""Local diagnosis metadata; no exception messages, credentials or request bodies."""
import json,logging,threading,traceback,uuid
from datetime import datetime,timezone
from pathlib import Path
from .config import DATA_DIR

_lock=threading.Lock()

def record(request,error):
    identifier=uuid.uuid4().hex[:12]
    route=request.scope.get('route');template=getattr(route,'path',None)
    data={'id':identifier,'at':datetime.now(timezone.utc).isoformat(),'type':type(error).__name__,
          'route':template or 'unresolved_route','method':request.method,
          'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)[-12:]]}
    try:
        file=DATA_DIR/'private'/'errors.jsonl';file.parent.mkdir(parents=True,exist_ok=True)
        with _lock,file.open('a',encoding='utf-8') as stream:stream.write(json.dumps(data,ensure_ascii=False)+'\n')
    except OSError:logging.getLogger('papernet').error('local_error_record_unavailable reference=%s',identifier)
    return identifier
