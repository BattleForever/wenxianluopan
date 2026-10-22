"""Conservative layout inventory, independent of model-generated explanations.

Detected headings/captions are a coverage aid, not a claim that arbitrary PDF
layouts can be parsed completely. No figure pixels are interpreted here.
"""
import re
import unicodedata
from collections import Counter


def normalize(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip()


def inventory(doc):
    blocks = []
    sizes = Counter()
    fonts = Counter()
    for page_no, page in enumerate(doc, 1):
        for block in page.get_text('dict', sort=False)['blocks']:
            lines = block.get('lines', [])
            records = []
            for line in lines:
                spans = [s for s in line['spans'] if s['text'].strip()]
                if not spans:
                    continue
                text = normalize(''.join(s['text'] for s in line['spans']))
                for s in spans:
                    sizes[round(s['size'], 1)] += len(s['text'])
                    fonts[s['font'].split('+')[0]] += len(s['text'])
                records.append((text, spans, line['bbox']))
            blocks.append((page_no, records, page.rect.height))
    body_size = sizes.most_common(1)[0][0] if sizes else 10
    body_font = fonts.most_common(1)[0][0] if fonts else ''
    anchors = re.compile(r'^(?:\d+[.\s]+)?(?:abstract|introduction|background|materials and methods|methods|results(?: and discussion)?|discussion|conclusions?)$', re.I)
    caption_re = re.compile(r'^(?:Fig\.?|Figure)\s*(\d+)\b[.:]?\s+', re.I)
    styles = set()
    figures = {}
    for page_no, lines, height in blocks:
        for text, spans, bbox in lines:
            if anchors.fullmatch(text) or (caption_re.match(text) and spans[0]['size'] < 0.95 * body_size):
                styles.add(spans[0]['font'].split('+')[0])
        text = normalize(' '.join(line[0] for line in lines))
        match = caption_re.match(text)
        if match and int(match[1]) <= 60:
            # Only a sequence starting a,b is accepted; an isolated article 'a'
            # must never become an invented panel label.
            labels = list(dict.fromkeys(re.findall(r'\b([a-z])\s+(?=[A-Z])', text)))
            labels = labels if len(labels) >= 2 and labels[:2] == ['a', 'b'] and labels == sorted(labels) else []
            figures.setdefault(int(match[1]), {'number': int(match[1]), 'page': page_no,
                                              'caption': text, 'panels': labels})
    sections = []
    styles.discard(body_font)
    seen = set()
    stopped = False
    for page_no, lines, height in blocks:
        if lines and re.match(r'^Table\s+\d+',lines[0][0],re.I):
            continue
        for text, spans, bbox in lines:
            if re.fullmatch(r'References|Acknowledg[e]?ments|Author contributions|Competing interests', text, re.I):
                stopped = True
            if stopped:
                continue
            style = spans[0]['font'].split('+')[0]
            if (style not in styles or not 0.9 * body_size <= spans[0]['size'] <= 1.5 * body_size
                    or not 4 <= len(text) <= 150 or bbox[1] < 45 or bbox[3] > height - 30
                    or caption_re.match(text) or re.search(r'[.!?;:=¼]|\bdoi\b', text, re.I)
                    or sum(len(w)==1 for w in text.split()) > len(text.split()) / 2
                    or not re.match(r'(?:\d+[.\s]+)?[A-Z]', text)
                    or len(text.split()) < 2 and not anchors.fullmatch(text)):
                continue
            key = text.casefold()
            if key not in seen:
                sections.append({'heading': text, 'page': page_no})
                seen.add(key)
    return {'basis': 'layout_detected', 'sections': sections,
            'figures': list(sorted(figures.values(), key=lambda f: f['number'])),
            'note': '覆盖检查以版面识别的标题和图注为准，特殊排版可能需要人工补核。'}


def check_coverage(data, expected):
    headings = [normalize(s['heading']).casefold() for s in data['sections']]
    required = [normalize(s['heading']).casefold() for s in expected['sections']]
    if len(headings) != len(set(headings)) or any(h not in headings for h in required):
        raise ValueError('section coverage')
    if [headings.index(h) for h in required] != sorted(headings.index(h) for h in required):
        raise ValueError('section order')
    figures = {f['number']: f for f in data['figures']}
    if set(figures) != {f['number'] for f in expected['figures']}:
        raise ValueError('figure coverage')
    for item in expected['figures']:
        actual = figures[item['number']]
        if actual['evidence']['page'] != item['page']:
            raise ValueError('figure evidence page')
        labels = [p['label'].strip().lower() for p in actual['panels']]
        if item['panels']:
            if labels != item['panels']:
                raise ValueError('figure panel coverage')
        elif labels != ['整体']:
            raise ValueError('unlabelled figure panels')
    return {'basis': expected['basis'], 'sections_detected': len(required),
            'sections_covered': len(required), 'figures_detected': len(figures),
            'figures_covered': len(figures), 'note': expected['note']}
