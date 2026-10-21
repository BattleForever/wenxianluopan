"""期刊/会议等级数据源：easyScholar。
本地优先：先查用户管道的 easyscholar.jsonl 磁盘缓存（含 3,467 条历史查询），
查不到再联网（www.easyscholar.cc，限速 ~1.6 req/s），联网结果同时写回该缓存
（只追加，不删除——与文献整理会话的管道共享同一份缓存）。
"""
import json
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
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (name or "").lower())


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
                    _cache[rec["k"]] = rec
                except (ValueError, KeyError):
                    continue
    return _cache


def local_rank(name: str):
    """本地缓存查询。返回 {rank:{...}, at} 或 None。"""
    cache = _load_cache()
    return cache.get(norm_name(name))


def load_key():
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
    _load_cache()
    _cache[rec["k"]] = rec
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 共享缓存写失败不影响本次结果
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
