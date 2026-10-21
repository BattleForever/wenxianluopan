"""Explicit missing-last sorting and metric formatting of reference metadata."""
import json
import re
from .db import get_db
from .store import norm_venue, paper_dict

SORTS = {'cited', 'year_desc', 'year_asc', 'impact_factor', 'cas_zone'}

YEAR_RE = re.compile(r'^(19|20)\d{2}$')
METRIC_YEAR_KEYS = ('metric_year', 'if_year', 'impact_factor_year', 'year')


def metric_year_from_extra(extra_json):
    """Read a verifiable metric year from existing venues.extra_json. No schema change.

    Only a plain four-digit year 1900-2099 counts; anything else stays unknown
    so the cache update time is never presented as the metric's own year.
    """
    try:
        extra = json.loads(extra_json or '{}')
    except (ValueError, TypeError):
        return None
    sources = []
    if isinstance(extra, dict):
        sources.append(extra)
        rank = extra.get('rank')
        if isinstance(rank, dict):
            sources.append(rank)
    for src in sources:
        for key in METRIC_YEAR_KEYS:
            value = src.get(key)
            text = str(value).strip() if value is not None else ''
            if YEAR_RE.match(text):
                return int(text)
    return None


def venue_public(v):
    """Venue fields exposed to clients, including metric year when determinable."""
    out = {k: v.get(k) for k in ('name', 'impact_factor', 'cas_zone', 'jcr_zone', 'ccf_tier', 'source', 'updated_at')}
    out['metric_year'] = metric_year_from_extra(v.get('extra_json'))
    return out


def number(value):
    match = re.search(r'\d+(?:\.\d+)?', str(value).replace(',', '')) if value is not None else None
    return float(match.group()) if match else None

def zone_number(value):
    text = str(value or '').strip()
    match = re.search(r'([1-4])\s*区', text) or re.fullmatch(r'([1-4])', text)
    return int(match.group(1)) if match else None

def linked_papers(paper_id, direction='references', sort='cited', limit=300):
    if direction not in {'references', 'citing'} or sort not in SORTS:
        raise ValueError('Unsupported direction or sort')
    limit = max(1, min(int(limit), 1000))
    with get_db() as conn:
        if direction == 'references':
            rows = conn.execute('SELECT p.* FROM citations c JOIN papers p ON p.id=c.to_id WHERE c.from_id=?', (int(paper_id),)).fetchall()
        else:
            rows = conn.execute('SELECT p.* FROM citations c JOIN papers p ON p.id=c.from_id WHERE c.to_id=?', (int(paper_id),)).fetchall()
        venues = {}; output = []
        for row in rows:
            p = paper_dict(row); vk = norm_venue(p.get('venue') or '')
            if vk and vk not in venues:
                v = conn.execute('SELECT name,impact_factor,jcr_zone,cas_zone,cas_top,cas_sub,ccf_tier,source,extra_json,updated_at FROM venues WHERE name_norm=?', (vk,)).fetchone()
                venues[vk] = venue_public(dict(v)) if v else None
            p['venue_info'] = venues.get(vk); output.append(p)
    def key(p):
        vi = p.get('venue_info') or {}
        value = number(vi.get('impact_factor')) if sort == 'impact_factor' else zone_number(vi.get('cas_zone')) if sort == 'cas_zone' else number(p.get('year')) if sort.startswith('year') else number(p.get('cited_by_count'))
        primary = (value if sort in {'cas_zone','year_asc'} else -value) if value is not None else 0
        return (value is None, primary, -(p.get('year') or 0), p['id'])
    output.sort(key=key)
    return {'paper_id':paper_id,'direction':direction,'sort':sort,'total':len(output),'count':min(len(output),limit),'papers':output[:limit],
            'metric_note':'期刊指标仅作筛选参考；未知值显示未知并排在最后；缓存更新时间不等于指标所属年份。'}
