"""Connection-scoped model request slots, shared by briefs, guides and completion."""
import hashlib,threading
from contextlib import contextmanager
class Cancelled(Exception):pass
_guard=threading.Condition();_counts={}
@contextmanager
def slot(cfg):
    raw=cfg['chat_url']+'\0'+str(cfg.get('api_key',''))
    key=hashlib.sha256(raw.encode()).hexdigest();limit=max(1,min(8,int(cfg.get('max_concurrency',1))))
    with _guard:
        while _counts.get(key,0)>=limit:
            if cfg.get('_cancel_event') and cfg['_cancel_event'].is_set():raise Cancelled()
            _guard.wait(.25)
        if cfg.get('_cancel_event') and cfg['_cancel_event'].is_set():raise Cancelled()
        _counts[key]=_counts.get(key,0)+1
    try:yield
    finally:
        with _guard:
            _counts[key]-=1
            if not _counts[key]:_counts.pop(key)
            _guard.notify_all()
