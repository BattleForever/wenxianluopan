"""期刊/会议等级数据源：easyScholar。
本地优先：先查用户管道的 easyscholar.jsonl 磁盘缓存（含 3,467 条历史查询），
查不到再联网（www.easyscholar.cc，限速 ~1.6 req/s），联网结果同时写回该缓存
（只追加，不删除——与文献整理会话的管道共享同一份缓存）。
"""
import json
import hashlib
import os
import re
import threading
import time
from pathlib import Path

import requests

from .config import DATA_DIR
KEY_PATH = Path(os.environ.get('PAPERNET_METRIC_KEY_FILE', str(DATA_DIR / 'private' / 'easyscholar_key.txt')))
CACHE_PATH = DATA_DIR / 'metrics_cache.jsonl'
BASE = "https://www.easyscholar.cc/open/getPublicationRank"

_min_interval = 0.6
_lock = threading.Lock()
_last_ts = [0.0]
_cache = None


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (name or "").lower().replace('&',' and '))


def _load_cache():
    global _cache
    if _cache is None:
        _cache = {}
        if CACHE_PATH.exists():
            for line in CACHE_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    # Older workflows used spaces and "and" in the cache key;
                    # the journal's actual name is the stable matching input.
                    key=norm_name(rec.get('name') or rec.get('k'))
                    if key:_cache[key] = rec
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    return _cache


def local_rank(name: str):
    """本地缓存查询。返回 {rank:{...}, at} 或 None。"""
    from .private_settings import LOCK
    with LOCK:return _load_cache().get(norm_name(name))


def remember_records(records):
    """Keep immutable metric versions in the local catalog, without raw payloads."""
    from .db import get_db
    with get_db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS journal_metric_history (
            id TEXT PRIMARY KEY,name_norm TEXT NOT NULL,name TEXT NOT NULL,
            metric_year INTEGER,checked_at TEXT,fields_json TEXT NOT NULL,source TEXT NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_metric_history_name_year ON journal_metric_history(name_norm,metric_year)')
        for rec in records:
            name=rec.get('name');rank=rec.get('rank')
            if not isinstance(name,str) or not isinstance(rank,dict):continue
            fields=rank_to_fields(rank)
            if not fields:continue
            year=rec.get('metric_year')
            if not isinstance(year,int) or isinstance(year,bool) or not 1900<=year<=2099:year=None
            value={'name':name[:300],'year':year,'at':str(rec.get('at') or '')[:50],
                   'fields':{k:str(v)[:100] for k,v in fields.items()},'source':'easyScholar 导入缓存' if rec.get('imported') else 'easyScholar'}
            digest=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            c.execute('INSERT OR IGNORE INTO journal_metric_history VALUES(?,?,?,?,?,?,?)',
                      (digest,norm_name(name),value['name'],year,value['at'],json.dumps(value['fields'],ensure_ascii=False),value['source']))


def catalog_stats():
    values=list(_load_cache().values())
    valid=[r for r in values if isinstance(r.get('rank'),dict) and rank_to_fields(r['rank'])]
    return {'journals':len(valid),'if_known':sum(bool(rank_to_fields(r['rank']).get('impact_factor')) for r in valid),
            'cas_known':sum(bool(rank_to_fields(r['rank']).get('cas_zone')) for r in valid),
            'jcr_known':sum(bool(rank_to_fields(r['rank']).get('jcr_zone')) for r in valid)}


def load_key():
    from .private_settings import service_key_exists,service_key
    if service_key_exists('easyscholar'):return service_key('easyscholar')
    if KEY_PATH.exists():
        k = KEY_PATH.read_text(encoding="utf-8").strip()
        return k or None
    return None


def online_rank(name: str, key: str):
    """联网查询 easyScholar。返回 (rec|None, not_found_bool)。"""
    with _lock:
        wait = _min_interval - (time.time() - _last_ts[0])
        if wait > 0:
            time.sleep(wait)
        _last_ts[0] = time.time()
    resp = requests.get(
        BASE,
        params={"secretKey": key, "publicationName": name},
        timeout=30,
        headers={"User-Agent": "PaperNet/0.1"},
        allow_redirects=False,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"easyscholar HTTP {resp.status_code}")
    body = resp.json()
    if body.get("errCode") not in (0, None, "0"):
        # 未找到刊物等情况
        return None, True
    data = body.get("data") or {}
    allrank = data.get("officialRank", {}).get("all") or {}
    if not allrank:
        return None, True
    rec = {
        "k": norm_name(name),
        "name": data.get("name") or name,
        "rank": allrank,
        "at": time.strftime("%Y-%m-%d", time.localtime()),
    }
    from .private_settings import LOCK
    with LOCK:
        _load_cache()
        _cache[rec["k"]] = rec
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with CACHE_PATH.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 缓存写失败不影响本次结果
    remember_records([rec])
    return rec, False


FIELD_MAP = [
    ("sciif", "impact_factor"),
    ("sci", "jcr_zone"),
    ("sciUp", "cas_zone"),
    ("sciUpTop", "cas_top"),
    ("sciUpSmall", "cas_sub"),
]


def rank_to_fields(rank: dict) -> dict:
    """easyScholar officialRank.all -> PaperNet venues 字段。"""
    out = {}
    for src, dst in FIELD_MAP:
        v = rank.get(src)
        if v not in (None, "", "null"):
            out[dst] = str(v)
    return out
