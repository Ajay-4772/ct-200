"""CT-200-specific PDF parser. Uses PDF text/layout first, OCR fallback for image-only pages.
Conservative by design: uncertain heading patterns remain visible as warnings rather than guessed away.
"""
from __future__ import annotations
import hashlib, re, unicodedata
from pathlib import Path
try:
    import pymupdf as fitz
except ImportError:
    import fitz

SECTION_RE = re.compile(r'^\s*(\d+(?:\.\d+)*\.?)\s+(.+?)\s*$')

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
                # Large horizontal gaps between spans usually indicate separate table columns.
                # Preserve those boundaries as pipes; ordinary word spacing remains untouched.
                pieces=[]; previous_x1=None
                for span in spans:
                    sx0=(span.get('bbox') or [0,0,0,0])[0]; sx1=(span.get('bbox') or [0,0,0,0])[2]
                    if pieces and previous_x1 is not None and sx0-previous_x1 > 35:
                        pieces.append(' | ')
                    pieces.append(span.get('text','')); previous_x1=sx1
                text = normalize(''.join(pieces))
                if not text or text.startswith('<IMAGE'): continue
                size = max((s.get('size', 0) for s in spans), default=0)
                bold = any('bold' in s.get('font','').lower() for s in spans)
                page_lines.append({'text':text,'size':size,'bold':bold,'page':pno,'y0':line.get('bbox',[0,0,0,0])[1]})
        # OCR fallback only if page has virtually no usable text.
        if not page_lines:
            try:
                import pytesseract
                from PIL import Image
                import io
                pix = page.get_pixmap(matrix=fitz.Matrix(2,2))
                ocr = pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes('png'))))
                page_lines = [{'text':normalize(x),'size':0,'bold':False,'page':pno,'y0':-1} for x in ocr.splitlines() if normalize(x)]
                warnings.append(f'Page {pno}: OCR fallback used; verify extraction visually.')
            except Exception as e:
                warnings.append(f'Page {pno}: no text layer and OCR unavailable ({type(e).__name__}).')
        lines.extend(page_lines)
    return lines, warnings

def parse_pdf(path: str | Path) -> dict:
    lines, warnings = extract_pdf(path)
    table_regions=[]
    source_doc=fitz.open(path)
    for pno,page in enumerate(source_doc, start=1):
        try:
            candidates=[]
            for table in page.find_tables(strategy='lines').tables:
                if table.row_count >= 3 and table.col_count >= 2:
                    rows=table.extract()
                    if rows and any(any(cell and str(cell).strip() for cell in row) for row in rows):
                        candidates.append({'page':pno,'bbox':table.bbox,'rows':rows})
            # Some PDF table detectors return fragments contained inside a correctly detected table.
            for candidate in candidates:
                x0,y0,x1,y1=candidate['bbox']
                contained=any(other is not candidate and (other['bbox'][0] <= x0+1 and other['bbox'][1] <= y0+1 and other['bbox'][2] >= x1-1 and other['bbox'][3] >= y1-1) and (other['bbox'][2]-other['bbox'][0])*(other['bbox'][3]-other['bbox'][1]) > (x1-x0)*(y1-y0)*1.05 for other in candidates)
                if not contained: table_regions.append(candidate)
        except Exception as e:
            warnings.append(f'Page {pno}: table detection unavailable ({type(e).__name__}); table content retained as text.')
    nodes=[]
    current=None
    last_page=1
    for item in lines:
        text=item['text']
        if item.get('y0',-1) >= 0 and any(t['page']==item['page'] and t['bbox'][1]-1 <= item['y0'] <= t['bbox'][3]+1 for t in table_regions):
            continue
        # Filter PDF page furniture, but do not silently drop content-like lines.
        if text.startswith('<IMAGE FOR PAGE:') or text.startswith('<PARSED TEXT FOR PAGE:'): continue
        m=SECTION_RE.match(text)
        # A heading is numbered and short; font/bold is a secondary signal, not sole criterion.
        is_heading=bool(m and len(text) < 110 and (item['bold'] or item['size'] >= 12))
        if is_heading:
            num, title=m.group(1).rstrip('.'), normalize(m.group(2))
            display_heading=f'{num}. {title}' if '.' not in num else f'{num} {title}'
            node={'section_number':num,'heading':display_heading,'title':title,'level':num.count('.')+1,
                  'body_text':'','page_start':item['page'],'page_end':item['page'],'y_start':item.get('y0',0),'children':[],'tables':[],'node_type':'section','warnings':[]}
            nodes.append(node); current=node
            # Preserve duplicate headings as distinct nodes and surface them.
            if sum(1 for n in nodes if n['heading'].casefold()==node['heading'].casefold()) > 1:
                node['warnings'].append('Duplicate heading text; retained as a distinct occurrence.')
            last_page=item['page']; continue
        if current is None:
            if text.lower().startswith('cardiotrack ct-200'):
                nodes.insert(0, {'section_number':'','heading':text,'title':text,'level':0,'body_text':'','page_start':item['page'],'page_end':item['page'],'y_start':item.get('y0',0),'children':[],'tables':[],'node_type':'document_title','warnings':[]})
                current=nodes[0]
            else:
                warnings.append(f'Page {item["page"]}: pre-heading text retained as document preamble: {text[:80]}')
                nodes.insert(0, {'section_number':'','heading':'Document preamble','title':'Document preamble','level':0,'body_text':text,'page_start':item['page'],'page_end':item['page'],'y_start':item.get('y0',0),'children':[],'tables':[],'node_type':'preamble','warnings':[]})
                current=nodes[0]
        else:
            # The PDF title wraps across lines; merge title lines instead of treating them as body.
            if current.get('node_type') == 'document_title' and not current.get('section_number') and len(current.get('heading','')) < 140:
                current['heading'] = normalize(current['heading'] + ' ' + text)
                current['title'] = current['heading']
            else:
                # Preserve lines and table rows. Do not invent cell boundaries from text extraction.
                current['body_text']=(current['body_text']+'\n'+text).strip()
            # Track page continuations across page breaks
            if item['page'] > current.get('page_start', 1):
                current['page_end'] = item['page']
                continuation_warning = f'Page-break continuation: section spans across pages {current["page_start"]}-{item["page"]}.'
                if continuation_warning not in current['warnings']:
                    current['warnings'].append(continuation_warning)
        if item['page'] != last_page and current:
            last_page=item['page']
    # Attach detected tables to the closest preceding section heading on the same page.
    for table in table_regions:
        page_no=table['page']; table_y=table['bbox'][1]
        candidates=[n for n in nodes if n.get('section_number') and n['page_start']==page_no and n.get('y_start',0) <= table_y]
        if not candidates:
            continue
        owner=max(candidates,key=lambda n:n.get('y_start',0))
        rows=[[normalize(str(cell or '')) for cell in row] for row in table['rows']]
        # Normalize row widths and preserve cells as a structured table.
        width=max((len(r) for r in rows),default=0)
        rows=[r+['']*(width-len(r)) for r in rows]
        owner['tables'].append({'page':page_no,'rows':rows})
        if rows:
            owner['body_text']=(owner['body_text']+'\n\n'+'\n'.join('| '+' | '.join(r)+' |' for r in rows)).strip()

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
    order=[n['section_number'] for n in all_nodes]
    if '3.4' in order and '3.3' in order and order.index('3.4') < order.index('3.3'):
        s34 = next((n for n in all_nodes if n['section_number']=='3.4'), None)
        if s34 and 'Section 3.4 appears before section 3.3 in source order.' not in s34['warnings']:
            s34['warnings'].append('Section 3.4 appears before section 3.3 in source order.')
    for n in all_nodes:
        if n['section_number']=='2.1.1.1':
            w = 'Numbering skips 2.1.1; attached to nearest existing ancestor 2.1.'
            if w not in n['warnings']:
                n['warnings'].append(w)
        # Deduplicate warnings list preserving order
        seen = set()
        deduped = []
        for w in n['warnings']:
            if w not in seen:
                seen.add(w)
                deduped.append(w)
        n['warnings'] = deduped
    return {'title':'CardioTrack CT-200 Home Blood Pressure Monitor — Technical & User Manual',
            'nodes':all_nodes,'warnings':warnings,'pages':len(fitz.open(path))}

def make_logical_key(n: dict) -> str:
    # Section number is stable across body/heading edits; unnumbered title uses normalized title.
    # This intentionally does not solve renumbering or duplicate section numbers.
    if n.get('section_number'):
        return 'SECTION:' + n['section_number']
    return 'ROOT::' + normalize(n['heading']).casefold()
