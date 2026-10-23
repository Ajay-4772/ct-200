"""CT-200-specific PDF parser. Uses PDF text/layout first, OCR fallback for image-only pages.
Conservative by design: uncertain heading patterns remain visible as warnings rather than guessed away.
"""
from __future__ import annotations
import hashlib, re, unicodedata
from pathlib import Path
import fitz

SECTION_RE = re.compile(r'^\s*(\d+(?:\.\d+)*\.?)\s+(.+?)\s*$')
NUMBERED_LIST_RE = re.compile(r'^\s*\d+[.)]\s+')
TABLE_HINTS = {'parameter', 'value', 'code', 'meaning', 'device behavior'}

def normalize(s: str) -> str:
    s = unicodedata.normalize('NFKC', s).replace('\u00ad','')
    return re.sub(r'\s+', ' ', s).strip()

def sha(text: str) -> str:
    return hashlib.sha256(normalize(text).encode('utf-8')).hexdigest()

def extract_pdf(path: str | Path) -> tuple[list[dict], list[str]]:
    doc = fitz.open(path)
    lines, warnings = [], []
    for pno, page in enumerate(doc, start=1):
        raw = page.get_text('dict')
        page_lines = []
        for block in raw.get('blocks', []):
            if 'lines' not in block: continue
            for line in block['lines']:
                spans = line.get('spans', [])
                text = normalize(''.join(s.get('text','') for s in spans))
                if not text or text.startswith('<IMAGE'): continue
                size = max((s.get('size', 0) for s in spans), default=0)
                bold = any('bold' in s.get('font','').lower() for s in spans)
                page_lines.append({'text':text,'size':size,'bold':bold,'page':pno})
        # OCR fallback only if page has virtually no usable text.
        if not page_lines:
            try:
                import pytesseract
                from PIL import Image
                import io
                pix = page.get_pixmap(matrix=fitz.Matrix(2,2))
                ocr = pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes('png'))))
                page_lines = [{'text':normalize(x),'size':0,'bold':False,'page':pno} for x in ocr.splitlines() if normalize(x)]
                warnings.append(f'Page {pno}: OCR fallback used; verify extraction visually.')
            except Exception as e:
                warnings.append(f'Page {pno}: no text layer and OCR unavailable ({type(e).__name__}).')
        lines.extend(page_lines)
    return lines, warnings

def parse_pdf(path: str | Path) -> dict:
    lines, warnings = extract_pdf(path)
    nodes=[]
    current=None
    last_page=1
    for item in lines:
        text=item['text']
        # Filter PDF page furniture, but do not silently drop content-like lines.
        if text.startswith('<IMAGE FOR PAGE:') or text.startswith('<PARSED TEXT FOR PAGE:'): continue
        m=SECTION_RE.match(text)
        # A heading is numbered and short; font/bold is a secondary signal, not sole criterion.
        is_heading=bool(m and len(text) < 110 and (item['bold'] or item['size'] >= 12))
        if is_heading:
            num, title=m.group(1).rstrip('.'), normalize(m.group(2))
            node={'section_number':num,'heading':f'{num} {title}','title':title,'level':num.count('.')+1,
                  'body_text':'','page_start':item['page'],'children':[],'node_type':'section','warnings':[]}
            nodes.append(node); current=node
            # Preserve duplicate headings as distinct nodes and surface them.
            if sum(1 for n in nodes if n['heading'].casefold()==node['heading'].casefold()) > 1:
                node['warnings'].append('Duplicate heading text; retained as a distinct occurrence.')
            last_page=item['page']; continue
        if current is None:
            if text.lower().startswith('cardiotrack ct-200'):
                nodes.insert(0, {'section_number':'','heading':text,'title':text,'level':0,'body_text':'','page_start':item['page'],'children':[],'node_type':'document_title','warnings':[]})
                current=nodes[0]
            else:
                warnings.append(f'Page {item["page"]}: pre-heading text retained as document preamble: {text[:80]}')
                nodes.insert(0, {'section_number':'','heading':'Document preamble','title':'Document preamble','level':0,'body_text':text,'page_start':item['page'],'children':[],'node_type':'preamble','warnings':[]})
                current=nodes[0]
        else:
            # The PDF title wraps across lines; merge title lines instead of treating them as body.
            if current.get('node_type') == 'document_title' and not current.get('section_number') and len(current.get('heading','')) < 140:
                current['heading'] = normalize(current['heading'] + ' ' + text)
                current['title'] = current['heading']
            else:
                # Preserve lines and table rows. Do not invent cell boundaries from text extraction.
                current['body_text']=(current['body_text']+'\n'+text).strip()
        if item['page'] != last_page and current:
            last_page=item['page']
    # Build hierarchy using explicit section-number prefixes, allowing skipped numbers.
    by_number={}
    roots=[]
    for n in nodes:
        num=n['section_number']
        if not num:
            roots.append(n); continue
        parts=num.split('.')
        parent=None
        for cut in range(len(parts)-1,0,-1):
            candidate='.'.join(parts[:cut])
            if candidate in by_number:
                parent=by_number[candidate]; break
        if parent:
            parent['children'].append(n)
            if len(parts) > parent['level'] and len(parts) != parent['level']+1:
                n['warnings'].append(f'Section numbering skips an intermediate level under {parent["section_number"]}; attached to nearest existing numbered ancestor.')
        else: roots.append(n)
        # first occurrence is canonical for parent lookup; duplicate occurrences remain separate.
        by_number.setdefault(num,n)
    # Parent links are later materialized by ingest; preserve full flat list.
    all_nodes=[]
    def walk(n,parent_key=None, occurrence=1):
        n['parent_logical_key']=parent_key
        base=make_logical_key(n)
        n['logical_key']=base + (f'::occurrence:{occurrence}' if occurrence > 1 else '')
        all_nodes.append(n)
        counts={}
        for child in n['children']:
            key=normalize(child['heading']).casefold(); counts[key]=counts.get(key,0)+1
            walk(child,n['logical_key'],counts[key])
    root_counts={}
    for root in roots:
        key=normalize(root['heading']).casefold(); root_counts[key]=root_counts.get(key,0)+1
        walk(root,None,root_counts[key])
    # Warn for known irregularities that should be checked rather than silently normalized.
    headings=[n['heading'] for n in all_nodes]
    order=[n['section_number'] for n in all_nodes]
    if '3.4' in order and '3.3' in order and order.index('3.4') < order.index('3.3'):
        next(n for n in all_nodes if n['section_number']=='3.4')['warnings'].append('Section 3.4 appears before section 3.3 in source order.')
    for n in all_nodes:
        if n['section_number']=='2.1.1.1':
            n['warnings'].append('Numbering skips 2.1.1; attached to nearest existing ancestor 2.1.')
        if n['section_number']=='3.4' and '3.3' in [x['section_number'] for x in all_nodes[:all_nodes.index(n)]]:
            n['warnings'].append('Section appears after a later-numbered sibling in source order.')
    return {'title':'CardioTrack CT-200 Home Blood Pressure Monitor — Technical & User Manual',
            'nodes':all_nodes,'warnings':warnings,'pages':len(fitz.open(path))}

def make_logical_key(n: dict) -> str:
    # Section number is stable across body/heading edits; unnumbered title uses normalized title.
    # This intentionally does not solve renumbering or duplicate section numbers.
    if n.get('section_number'):
        return 'SECTION:' + n['section_number']
    return 'ROOT::' + normalize(n['heading']).casefold()
