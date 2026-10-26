"""Local keyword lookup, separate from recommendation/ranking.

Small explicit terminology aliases help bilingual metadata lookup. They are
query expansion rules, not AI translation or a relevance/quality classifier.
Quoted phrases remain literal. All unquoted terms must match.
"""
import re,unicodedata

CONCEPTS=(
 ('气体/气流',('气体','气流','gas','gases','air','airflow','air flow')),
 ('流量',('流量计','流量','流速','flow','flows','flowmeter','flowmeters','flow meter','flow meters','flowrate','flow rate','airflow','air flow')),
 ('超声',('超声波','超声','ultrasonic','ultrasonics','ultrasound')),
 ('PMUT',('压电微机械超声换能器','压电微机械超声换能','pmut','pmuts')),
 ('CMUT',('电容微机械超声换能器','cmut','cmuts')),
)

def normalize(text):return unicodedata.normalize('NFKC',str(text or '')).casefold().strip()

def prepare(query):
    query=normalize(query);query=re.sub(r'https?://(?:dx\.)?doi\.org/','',query)
    aliases={}
    for _,values in CONCEPTS:
        for alias in values:aliases.setdefault(normalize(alias),[]).append(values)
    chinese=sorted((a for a in aliases if re.search(r'[\u4e00-\u9fff]',a)),key=len,reverse=True)
    phrases=sorted((a for a in aliases if ' ' in a),key=len,reverse=True)
    pattern='|'.join(re.escape(a) for a in phrases)+r'|[a-z0-9][a-z0-9./:_-]*|[\u4e00-\u9fff]+'
    clauses=[];expansions=[]
    def add(term,literal=False):
        if not term:return
        for group in ([(term,)] if literal else aliases.get(term,[(term,)])):
            values=tuple(dict.fromkeys(normalize(v) for v in group))
            key=(term,values,literal)
            if any(key==c[0] for c in clauses):continue
            expression='|'.join(re.escape(v) if literal or re.search(r'[\u4e00-\u9fff]',v) else r'(?<![a-z0-9])'+re.escape(v)+r'(?![a-z0-9])' for v in values)
            clauses.append((key,re.compile(expression)))
            if len(values)>1:expansions.append({'term':term,'alternatives':list(values)})
    for match in re.finditer(r'"([^"\n]+)"|“([^”\n]+)”|([^"“”]+)',query):
        if match.group(1) or match.group(2):add((match.group(1) or match.group(2)).strip(),True);continue
        for token in re.findall(pattern,match.group(3) or ''):
            if not re.search(r'[\u4e00-\u9fff]',token):add(token);continue
            at=0;unknown=''
            while at<len(token):
                alias=next((a for a in chinese if token.startswith(a,at)),None)
                if alias:
                    if unknown:add(unknown);unknown=''
                    add(alias);at+=len(alias)
                else:unknown+=token[at];at+=1
            if unknown:add(unknown)
    # Punctuation-only input still performs a literal lookup, not "match all".
    if query and not clauses:add(query,True)
    def matches(paper):
        fields=[paper.get(k) for k in ('title','title_zh','doi','venue','abstract','abstract_zh')]
        fields.append(' '.join(paper.get('authors') or []))
        text=normalize(' '.join(str(v or '') for v in fields))
        return all(expression.search(text) for _,expression in clauses)
    return matches,expansions
