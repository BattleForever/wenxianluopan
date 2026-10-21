"""Local, deterministic text relevance. No external model or document upload."""
import hashlib
import re
import threading

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer, ENGLISH_STOP_WORDS
from sklearn.preprocessing import normalize

_lock = threading.Lock()
_cache = {}


def tokens(text):
    words = re.findall(r'[a-z][a-z0-9-]{1,}|[\u4e00-\u9fff]+', text.lower())
    out = []
    for word in words:
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            out.extend(word[i:i+2] for i in range(len(word)-1))
        elif word not in ENGLISH_STOP_WORDS:
            out.append(word)
    return out


def document(p):
    # Repeating the title gives it more weight than a single abstract sentence.
    title = ' '.join(str(p.get(k) or '') for k in ('title', 'title_zh'))
    return title + ' ' + title + ' ' + ' '.join(str(p.get(k) or '') for k in ('abstract', 'abstract_zh'))


def rank(papers, query='', seed_ids=None):
    seed_ids = set(seed_ids or [])
    papers = sorted(papers, key=lambda p: p['id'])
    docs = [document(p)[:40000] for p in papers]
    query_text = query.strip()[:4000] or ' '.join(docs[i] for i,p in enumerate(papers) if p['id'] in seed_ids)[:16000]
    base = {'method':'TF-IDF', 'corpus_size':len(papers), 'components':0,
            'note':'本地统计学习模型；文本相似度不代表论文质量；不自动翻译或验证结论。'}
    if not papers or not query_text:
        return {}, {**base, 'status':'no_query', 'hint':'输入研究问题，或选择种子文献后排序。'}
    key = hashlib.sha256('\0'.join(str(p['id'])+'\0'+d for p,d in zip(papers,docs)).encode()).hexdigest()
    with _lock:
        model = _cache.get('model') if _cache.get('key') == key else None
        if model is None:
            vectorizer = TfidfVectorizer(analyzer=tokens, max_features=30000, sublinear_tf=True)
            try:
                matrix = vectorizer.fit_transform(docs)
            except ValueError:
                return {}, {**base, 'status':'empty_corpus', 'hint':'本地文献缺少可建模的标题或摘要。'}
            svd = None
            latent = None
            components = min(64, max(1, len(papers)//2), matrix.shape[1]-1)
            if len(papers) >= 6 and matrix.shape[1] >= 10:
                svd = TruncatedSVD(n_components=components, random_state=42)
                latent = normalize(svd.fit_transform(matrix))
            model = (vectorizer, matrix, svd, latent)
            _cache.update(key=key, model=model)
        vectorizer, matrix, svd, latent = model
        q = vectorizer.transform([query_text])
        if not q.nnz:
            return {}, {**base, 'status':'out_of_vocabulary', 'hint':'研究问题与本地语料没有词项交集，请调整表述或扩充文献。'}
        lexical = (matrix @ q.T).toarray().ravel()
        scores = lexical.copy()
        if svd is not None:
            semantic = (latent @ normalize(svd.transform(q)).T).ravel()
            scores = .7 * lexical + .3 * np.maximum(0, semantic)
            base.update(method='TF-IDF + LSA', components=int(svd.n_components))
        terms = vectorizer.get_feature_names_out()
        q_terms = set(q.indices)
        results = {}
        for i,p in enumerate(papers):
            common = sorted(q_terms.intersection(matrix[i].indices), key=lambda j:-matrix[i,j])[:6]
            results[p['id']] = {'relevance':round(float(np.clip(scores[i],0,1)),6),
                                 'matched_terms':[str(terms[j]) for j in common]}
    return results, {**base, 'status':'ok', 'hint':'相关度用于安排阅读顺序，请结合摘要及原文判断。'}
