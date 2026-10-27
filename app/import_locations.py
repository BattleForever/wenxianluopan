"""Read local library membership for a Zotero preview, without guessing identity."""
import json
from .db import get_db
from .store import norm_doi,find_existing

def locations(items):
    with get_db() as c:
        papers={r['id']:dict(r) for r in c.execute('SELECT id,doi,status,meta_json FROM papers')}
        key_ids={}
        for pid,p in papers.items():
            try:key=json.loads(p.get('meta_json') or '{}').get('zotero_key')
            except (ValueError,TypeError,AttributeError):key=None
            if key:key_ids.setdefault(key,set()).add(pid)
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'zotero_item_map' in tables:
            for r in c.execute("SELECT paper_id,zotero_key FROM zotero_item_map WHERE kind='item'"):
                if r[0] in papers:key_ids.setdefault(r[1],set()).add(r[0])
        if 'zotero_destinations' in tables:
            for r in c.execute('SELECT paper_id,item_key FROM zotero_destinations'):
                if r[0] in papers:key_ids.setdefault(r[1],set()).add(r[0])
        doi_ids={}
        for pid,p in papers.items():
            d=norm_doi(p.get('doi'))
            if d:doi_ids.setdefault(d,set()).add(pid)
        tree={r['id']:dict(r) for r in c.execute("SELECT id,parent_id,name FROM organizer_folders WHERE scope='library'")}
        memberships={}
        for fid,pid in c.execute('SELECT folder_id,paper_id FROM organizer_items'):
            if fid in tree:memberships.setdefault(pid,[]).append(fid)
        trashed={r[0] for r in c.execute("SELECT paper_id FROM paper_trash WHERE scope='library'")} if 'paper_trash' in tables else set()
        result={}
        for item in items:
            key=item.get('key') or '';doi=norm_doi(item.get('doi'));a=key_ids.get(key,set());b=doi_ids.get(doi,set())
            candidates=a|b
            conflict=len(candidates)>1 or any(doi and papers[pid].get('doi') and norm_doi(papers[pid]['doi'])!=doi for pid in a)
            if not candidates and not conflict:
                match=find_existing(c,doi=doi,title=item.get('title'))
                if match:candidates={match['id']}
            pid=next(iter(candidates)) if len(candidates)==1 and not conflict else None
            fids=sorted(memberships.get(pid,[]));paths=[]
            for fid in fids:
                names=[];seen=set();node=fid
                while node in tree and node not in seen:
                    seen.add(node);names.insert(0,tree[node]['name']);node=tree[node]['parent_id']
                paths.append(' / '.join(names))
            result[key]={'paper_id':pid,'collected':bool(pid and papers[pid]['status']=='library' and pid not in trashed),'trashed':pid in trashed,'conflict':conflict,'folder_ids':fids,'paths':paths}
        return result
