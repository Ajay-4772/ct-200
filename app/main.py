from __future__ import annotations
import os, json, uuid, hashlib, difflib, re, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import Base, engine, SessionLocal
from .models import Document, DocumentVersion, Node, Selection, SelectionItem
from .parser import parse_pdf, sha, normalize

Base.metadata.create_all(bind=engine)
app = FastAPI(
    title='CT-200 QA Traceability API',
    description='FastAPI service for parsing CT-200 manuals, version tracking, pinned selection QA test generation, and retrieval-time staleness monitoring.',
    version='0.2.0'
)
GENERATIONS_PATH = Path(os.getenv('GENERATIONS_PATH', './generated.json'))

SAFETY_KEYWORDS = {
    'alarm', 'alarms', 'emergency', 'overpressure', 'deflation', 'venting',
    'contraindication', 'contraindications', 'hazard', 'safety', 'fault',
    'malfunction', 'accuracy', 'tolerance', 'risk', 'abort', 'aborts', 'discontinue'
}
TIMING_AND_UNITS = {
    'second', 'seconds', 'sec', 'minute', 'minutes', 'min', 'hour', 'hours',
    'mmhg', 'bpm', 'cycle', 'cycles', '%', 'percent', 'cm', 'year', 'years',
    'v', 'dc', 'aa', 'volt', 'volts', 'reading', 'readings'
}
NEGATION_WORDS = {
    'not', 'no', 'never', 'without', 'except', 'discontinue', 'halt', 'cannot', 'prohibited'
}

def extract_quantities(text: str) -> set[str]:
    return set(re.findall(r'\b\d+(?:\.\d+)?%?\b', text.lower()))

def detect_substantive_changes(old_text: str, new_text: str) -> tuple[str, list[str], str]:
    """Analyzes differences between two versions of text.
    Returns (status, triggers, diff_summary) where status is:
    'current' | 'wording_changed' | 'substantive_change_suspected'.
    """
    norm_old = normalize(old_text)
    norm_new = normalize(new_text)
    if sha(norm_old) == sha(norm_new):
        return 'current', [], 'Content hash is unchanged.'

    triggers = []
    # 1. Number and threshold modifications
    old_nums = extract_quantities(norm_old)
    new_nums = extract_quantities(norm_new)
    removed_nums = old_nums - new_nums
    added_nums = new_nums - old_nums
    if removed_nums or added_nums:
        triggers.append(f'Numeric values/thresholds modified: removed {sorted(removed_nums)}, added {sorted(added_nums)}')

    # 2. Timing and unit terminology differences
    old_words = set(re.findall(r'\b[a-z%]+\b', norm_old.lower()))
    new_words = set(re.findall(r'\b[a-z%]+\b', norm_new.lower()))
    unit_diff = (old_words ^ new_words) & TIMING_AND_UNITS
    if unit_diff:
        triggers.append(f'Timing or unit terminology changed: {sorted(unit_diff)}')

    # 3. Negation differences
    negation_diff = (old_words ^ new_words) & NEGATION_WORDS
    if negation_diff:
        triggers.append(f'Negation/restriction wording changed: {sorted(negation_diff)}')

    # 4. Safety terminology differences
    safety_diff = (old_words ^ new_words) & SAFETY_KEYWORDS
    if safety_diff:
        triggers.append(f'Safety or risk terminology changed: {sorted(safety_diff)}')

    diff = list(difflib.unified_diff(
        norm_old.splitlines(),
        norm_new.splitlines(),
        fromfile='source_version',
        tofile='target_version',
        lineterm=''
    ))
    diff_summary = '\n'.join(diff[:24]) if diff else 'Heading/content changed.'

    ratio = difflib.SequenceMatcher(None, norm_old, norm_new).ratio()
    if triggers or ratio < 0.90:
        status = 'substantive_change_suspected'
    else:
        status = 'wording_changed'

    return status, triggers, diff_summary

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def read_generations():
    try: return json.loads(GENERATIONS_PATH.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): return []

def write_generations(rows):
    GENERATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GENERATIONS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(GENERATIONS_PATH)

class SelectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    node_ids: list[str] = Field(min_length=1)
    version: Optional[int] = None

class TestCase(BaseModel):
    title: str = Field(min_length=4, max_length=180)
    rationale: str = Field(default='', description="Rationale explaining test intent, clinical or device risk addressed")
    requirement_reference: str = Field(default='', min_length=1, max_length=300)
    source_reference: Optional[str] = Field(default=None)
    preconditions: list[str] = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    expected_result: str = Field(default='', min_length=5)
    expected_results: Optional[list[str]] = Field(default=None)
    severity: str = Field(default='medium', pattern='^(low|medium|high|critical)$')
    risk: Optional[str] = Field(default=None, pattern='^(low|medium|high|critical)$')

    def model_post_init(self, __context):
        # Synchronize risk <-> severity
        if self.risk and not self.severity:
            self.severity = self.risk
        elif self.severity and not self.risk:
            self.risk = self.severity
        # Synchronize source_reference <-> requirement_reference
        if self.source_reference and not self.requirement_reference:
            self.requirement_reference = self.source_reference
        elif self.requirement_reference and not self.source_reference:
            self.source_reference = self.requirement_reference
        # If expected_result is empty but expected_results array is supplied
        if not self.expected_result and self.expected_results:
            self.expected_result = '\n'.join(self.expected_results)
        # Default rationale if omitted
        if not self.rationale:
            self.rationale = f"Verifies device requirement under {self.requirement_reference or self.title}"

class LLMOutput(BaseModel):
    test_cases: list[TestCase] = Field(min_length=3, max_length=5)

@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'ct200-qa-traceability'}

@app.post('/ingest')
async def ingest(file: UploadFile = File(...), version: Optional[int] = None, db: Session = Depends(get_db)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, 'Upload a PDF file.')
    raw = await file.read()
    if not raw.startswith(b'%PDF'):
        raise HTTPException(400, 'Uploaded file does not look like a PDF.')

    tmp = Path(tempfile.gettempdir()) / f'{uuid.uuid4().hex}.pdf'
    tmp.write_bytes(raw)
    try:
        parsed = parse_pdf(tmp)
    except Exception as e:
        raise HTTPException(422, f'PDF parsing failed: {type(e).__name__}: {e}')
    finally:
        tmp.unlink(missing_ok=True)

    doc = db.scalar(select(Document).where(Document.name == 'CardioTrack CT-200'))
    if not doc:
        doc = Document(name='CardioTrack CT-200')
        db.add(doc)
        db.flush()

    current = max([v.version_number for v in doc.versions], default=0)
    vernum = version or current + 1
    if db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.version_number == vernum)):
        raise HTTPException(409, f'Version {vernum} already exists; versions are immutable.')

    ver = DocumentVersion(
        document_id=doc.id,
        version_number=vernum,
        source_filename=file.filename,
        source_sha256=hashlib.sha256(raw).hexdigest()
    )
    db.add(ver)
    db.flush()

    # Match by logical key, which excludes body text. Duplicate occurrences carry occurrence suffixes.
    previous = db.scalars(select(Node).join(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.version_number == current)).all() if current else []
    old_by_key = {n.logical_key: n for n in previous}
    parsed_nodes = parsed['nodes']
    id_by_key = {n['logical_key']: f'node_{uuid.uuid4().hex[:16]}' for n in parsed_nodes}

    for item in parsed_nodes:
        node_id = id_by_key[item['logical_key']]
        parent_id = id_by_key.get(item['parent_logical_key']) if item.get('parent_logical_key') else None
        body = item.get('body_text', '').strip()
        db.add(Node(
            id=node_id,
            logical_key=item['logical_key'],
            version_id=ver.id,
            parent_id=parent_id,
            heading=item['heading'],
            level=item['level'],
            section_number=item['section_number'],
            body_text=body,
            tables_json=json.dumps(item.get('tables', []), ensure_ascii=False),
            content_hash=sha(item['heading'] + '\n' + body),
            node_type=item['node_type'],
            page_start=item.get('page_start', 1),
            page_end=item.get('page_end', item.get('page_start', 1)),
            warnings_json=json.dumps(item.get('warnings', []), ensure_ascii=False)
        ))
    db.commit()

    return {
        'document': 'CardioTrack CT-200',
        'version': vernum,
        'version_id': ver.id,
        'source_sha256': ver.source_sha256,
        'node_count': len(parsed_nodes),
        'warnings': parsed['warnings'],
        'node_warnings': [{'heading': n['heading'], 'warnings': n['warnings']} for n in parsed_nodes if n['warnings']],
        'matched_logical_nodes': sum(1 for n in parsed_nodes if n['logical_key'] in old_by_key),
        'changed_existing_nodes': sum(1 for n in parsed_nodes if n['logical_key'] in old_by_key and sha(n['heading'] + '\n' + n.get('body_text', '')) != old_by_key[n['logical_key']].content_hash),
        'parser_pages': parsed['pages']
    }

def get_version(db: Session, version: Optional[int]):
    doc = db.scalar(select(Document).where(Document.name == 'CardioTrack CT-200'))
    if not doc:
        raise HTTPException(404, 'No document ingested yet.')
    versions = sorted(doc.versions, key=lambda v: v.version_number)
    if not versions:
        raise HTTPException(404, 'No document versions.')
    if version is None:
        return versions[-1]
    for v in versions:
        if v.version_number == version:
            return v
    raise HTTPException(404, f'Document version {version} not found.')

def node_payload(db: Session, n: Node, with_children: bool = True):
    children = db.scalars(select(Node).where(Node.parent_id == n.id)).all() if with_children else []
    return {
        'id': n.id,
        'logical_key': n.logical_key,
        'version_id': n.version_id,
        'heading': n.heading,
        'section_number': n.section_number,
        'level': n.level,
        'page_start': n.page_start,
        'page_end': n.page_end,
        'body_text': n.body_text,
        'full_text': (n.heading + '\n' + n.body_text).strip(),
        'content_hash': n.content_hash,
        'tables': json.loads(n.tables_json or '[]'),
        'warnings': json.loads(n.warnings_json or '[]'),
        'children': [node_payload(db, c, True) for c in children]
    }

@app.get('/sections')
def sections(version: Optional[int] = None, db: Session = Depends(get_db)):
    v = get_version(db, version)
    nodes = db.scalars(select(Node).where(Node.version_id == v.id).order_by(Node.section_number, Node.id)).all()
    return {
        'version': v.version_number,
        'sections': [node_payload(db, n, False) for n in nodes if n.level == 1]
    }

@app.get('/nodes/{node_id}')
def get_node(node_id: str, db: Session = Depends(get_db)):
    n = db.get(Node, node_id)
    if not n:
        raise HTTPException(404, 'Node ID not found.')
    return node_payload(db, n)

@app.get('/search')
def search(q: str = Query(min_length=1), version: Optional[int] = None, limit: int = 50, db: Session = Depends(get_db)):
    v = get_version(db, version)
    needle = q.casefold()
    nodes = db.scalars(select(Node).where(Node.version_id == v.id)).all()
    hits = [n for n in nodes if needle in n.heading.casefold() or needle in n.body_text.casefold()]
    return {
        'version': v.version_number,
        'query': q,
        'count': len(hits[:max(1, min(limit, 200))]),
        'results': [node_payload(db, n, False) for n in hits[:max(1, min(limit, 200))]]
    }

@app.get('/nodes/{node_id}/compare')
def compare(node_id: str, target_version: Optional[int] = None, db: Session = Depends(get_db)):
    n = db.get(Node, node_id)
    if not n:
        raise HTTPException(404, 'Node ID not found.')
    v = db.get(DocumentVersion, n.version_id)
    doc = db.get(Document, v.document_id)
    target_v = get_version(db, target_version) if target_version is not None else max(doc.versions, key=lambda x: x.version_number)

    # If comparing node against its own version, check if it was newly added relative to the prior version
    if target_v.id == v.id:
        earlier = [ver for ver in doc.versions if ver.version_number < v.version_number]
        if earlier:
            prev_v = max(earlier, key=lambda x: x.version_number)
            prev_node = db.scalar(select(Node).where(Node.version_id == prev_v.id, Node.logical_key == n.logical_key))
            if not prev_node:
                return {
                    'node_id': node_id,
                    'logical_key': n.logical_key,
                    'source_version': v.version_number,
                    'target_version': prev_v.version_number,
                    'status': 'added',
                    'diff_summary': f'Node was newly added in version {v.version_number}.',
                    'substantive_triggers': ['new_section_added'],
                    'clinical_disclaimer': 'Automated text comparison does not determine clinical or regulatory significance. Human review required.'
                }

    target_node = db.scalar(select(Node).where(Node.version_id == target_v.id, Node.logical_key == n.logical_key))
    if not target_node:
        return {
            'node_id': node_id,
            'logical_key': n.logical_key,
            'source_version': v.version_number,
            'target_version': target_v.version_number,
            'status': 'removed',
            'diff_summary': f'No corresponding logical node found in version {target_v.version_number}.',
            'substantive_triggers': ['section_removed'],
            'clinical_disclaimer': 'Automated text comparison does not determine clinical or regulatory significance. Human review required.'
        }

    status, triggers, diff_summary = detect_substantive_changes(
        n.heading + '\n' + n.body_text,
        target_node.heading + '\n' + target_node.body_text
    )
    effective_status = 'unchanged' if status == 'current' else status

    return {
        'node_id': node_id,
        'logical_key': n.logical_key,
        'source_version': v.version_number,
        'target_version': target_v.version_number,
        'current_node_id': target_node.id,
        'target_node_id': target_node.id,
        'status': 'changed' if status in ('changed', 'wording_changed', 'substantive_change_suspected') else effective_status,
        'detailed_status': status,
        'substantive_change_suspected': (status == 'substantive_change_suspected'),
        'substantive_triggers': triggers,
        'diff_summary': diff_summary,
        'clinical_disclaimer': 'Automated text comparison does not determine clinical or regulatory significance. Human clinical review required.'
    }

@app.get('/versions/compare')
def compare_versions(from_version: int = 1, to_version: Optional[int] = None, db: Session = Depends(get_db)):
    v_from = get_version(db, from_version)
    v_to = get_version(db, to_version)

    nodes_from = {n.logical_key: n for n in v_from.nodes}
    nodes_to = {n.logical_key: n for n in v_to.nodes}

    keys_from = set(nodes_from.keys())
    keys_to = set(nodes_to.keys())

    added_keys = keys_to - keys_from
    removed_keys = keys_from - keys_to
    common_keys = keys_from & keys_to

    added = [{'node_id': nodes_to[k].id, 'logical_key': k, 'section_number': nodes_to[k].section_number, 'heading': nodes_to[k].heading} for k in sorted(added_keys)]
    removed = [{'node_id': nodes_from[k].id, 'logical_key': k, 'section_number': nodes_from[k].section_number, 'heading': nodes_from[k].heading} for k in sorted(removed_keys)]
    changed = []
    unchanged = []

    for k in sorted(common_keys):
        nf = nodes_from[k]
        nt = nodes_to[k]
        if nf.content_hash == nt.content_hash:
            unchanged.append({
                'from_node_id': nf.id,
                'to_node_id': nt.id,
                'logical_key': k,
                'section_number': nt.section_number,
                'heading': nt.heading
            })
        else:
            status, triggers, diff_summary = detect_substantive_changes(
                nf.heading + '\n' + nf.body_text,
                nt.heading + '\n' + nt.body_text
            )
            changed.append({
                'from_node_id': nf.id,
                'to_node_id': nt.id,
                'logical_key': k,
                'section_number': nt.section_number,
                'heading': nt.heading,
                'change_classification': status,
                'substantive_triggers': triggers,
                'diff_summary': diff_summary
            })

    return {
        'from_version': v_from.version_number,
        'to_version': v_to.version_number,
        'summary': {
            'added_count': len(added),
            'removed_count': len(removed),
            'changed_count': len(changed),
            'unchanged_count': len(unchanged),
            'total_from': len(nodes_from),
            'total_to': len(nodes_to)
        },
        'added_nodes': added,
        'removed_nodes': removed,
        'changed_nodes': changed,
        'unchanged_nodes': unchanged,
        'clinical_disclaimer': 'Automated text comparison does not prove clinical or safety significance. Human clinical review required.'
    }

@app.post('/selections', status_code=201)
def create_selection(data: SelectionIn, db: Session = Depends(get_db)):
    versions = {db.get(Node, nid).version_id for nid in data.node_ids if db.get(Node, nid)}
    if len(versions) == 0 or len(versions) != 1:
        raise HTTPException(400, 'All node IDs must exist and belong to the same document version.')
    ver_id = next(iter(versions))
    v = db.get(DocumentVersion, ver_id)
    if data.version is not None and data.version != v.version_number:
        raise HTTPException(400, 'Requested version does not match node version IDs.')

    selection_id = 'sel_' + uuid.uuid4().hex[:16]
    sel = Selection(
        id=selection_id,
        name=data.name,
        document_version_id=ver_id,
        created_at=datetime.now(timezone.utc).isoformat()
    )
    db.add(sel)
    for nid in dict.fromkeys(data.node_ids):
        n = db.get(Node, nid)
        db.add(SelectionItem(
            selection_id=selection_id,
            node_id=n.id,
            version_id=n.version_id,
            source_text=(n.heading + '\n' + n.body_text).strip(),
            source_hash=n.content_hash
        ))
    db.commit()
    return {
        'selection_id': selection_id,
        'name': data.name,
        'version': v.version_number,
        'node_count': len(set(data.node_ids)),
        'pinned': True
    }

@app.get('/selections/{selection_id}')
def get_selection(selection_id: str, db: Session = Depends(get_db)):
    sel = db.get(Selection, selection_id)
    if not sel:
        raise HTTPException(404, 'Selection not found.')
    v = db.get(DocumentVersion, sel.document_version_id)
    items = db.scalars(select(SelectionItem).where(SelectionItem.selection_id == sel.id)).all()
    return {
        'selection_id': sel.id,
        'name': sel.name,
        'version': v.version_number,
        'items': [{'node_id': i.node_id, 'version_id': i.version_id, 'source_text': i.source_text, 'source_hash': i.source_hash} for i in items]
    }

PROMPT = '''You are a medical-device QA test design assistant. Use ONLY the supplied manual text.
Do not invent specifications or claim regulatory compliance.
Return JSON exactly matching the following schema:
{
  "test_cases": [
    {
      "title": "Descriptive test case title",
      "rationale": "Clear rationale explaining what device/clinical risk is verified",
      "source_reference": "Section number and requirement verified",
      "preconditions": ["Initial precondition state"],
      "steps": ["Step 1", "Step 2"],
      "expected_result": "Observable expected outcome",
      "severity": "low|medium|high|critical"
    }
  ]
}
Provide 3 to 5 concrete, repeatable test case ideas. Each case must have observable expected results.
If source ambiguity exists, make it a test/clarification rather than inventing an answer.
Manual text follows:
'''

async def call_llm(source_text: str) -> tuple[dict, int]:
    key = os.getenv('GROQ_API_KEY')
    if not key:
        raise HTTPException(503, 'LLM is not configured. Set GROQ_API_KEY in .env to enable generation.')
    model = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
    last_error = 'unknown error'

    for attempt in range(2):
        prompt = PROMPT + source_text
        if attempt:
            prompt += '\nPrevious response failed schema validation. Return corrected JSON only, with 3-5 complete test_cases.'
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.post(
                    'https://api.groq.com/openai/v1/chat/completions',
                    headers={'Authorization': f'Bearer {key}'},
                    json={
                        'model': model,
                        'temperature': 0.1,
                        'response_format': {'type': 'json_object'},
                        'messages': [{'role': 'user', 'content': prompt}]
                    }
                )
            r.raise_for_status()
            raw = r.json()['choices'][0]['message']['content']
            parsed = json.loads(raw)
            validated = LLMOutput.model_validate(parsed)
            return validated.model_dump(), attempt + 1
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as e:
            last_error = f'{type(e).__name__}: {e}'

    raise HTTPException(502, f'LLM output failed validation after 2 attempts; nothing was saved. Details: {last_error[:300]}')

@app.post('/selections/{selection_id}/generate')
async def generate(selection_id: str, force_new: bool = False, db: Session = Depends(get_db)):
    sel = db.get(Selection, selection_id)
    if not sel:
        raise HTTPException(404, 'Selection not found.')
    stored = read_generations()
    # Idempotent policy: return existing generation for same pinned selection unless force_new=True.
    existing = [g for g in stored if g['selection_id'] == selection_id]
    if existing and not force_new:
        return JSONResponse(status_code=200, content={'policy': 'idempotent', 'generation': existing[-1]})

    items = db.scalars(select(SelectionItem).where(SelectionItem.selection_id == selection_id)).all()
    source = '\n\n--- SELECTED NODE ---\n\n'.join(i.source_text for i in items)
    output, attempts = await call_llm(source)

    model = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')
    gen = {
        'generation_id': 'gen_' + uuid.uuid4().hex[:16],
        'selection_id': selection_id,
        'document_version_id': sel.document_version_id,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'model': model,
        'provider': 'groq',
        'source_nodes': [
            {
                'node_id': i.node_id,
                'version_id': i.version_id,
                'source_hash': i.source_hash,
                'source_text': i.source_text
            }
            for i in items
        ],
        'test_cases': output['test_cases'],
        'validation': 'passed',
        'llm_attempts': attempts
    }
    stored.append(gen)
    write_generations(stored)
    return JSONResponse(status_code=201, content={'policy': 'new_generation', 'generation': gen})

def generation_status(gen: dict, db: Session) -> dict:
    latest = get_version(db, None)
    statuses = []
    for src in gen['source_nodes']:
        old = db.get(Node, src['node_id'])
        logical = old.logical_key if old else None
        current = db.scalar(select(Node).where(Node.version_id == latest.id, Node.logical_key == logical)) if logical else None
        if not current:
            status = 'unresolved'
            triggers = ['node_missing_in_latest_version']
            summary = f'No corresponding node matched in the latest version (v{latest.version_number}).'
        elif current.content_hash == src['source_hash']:
            status = 'current'
            triggers = []
            summary = 'Source content hash unchanged in latest version.'
        else:
            status, triggers, diff_summary = detect_substantive_changes(
                src['source_text'],
                (current.heading + '\n' + current.body_text).strip()
            )
            summary = f'Source text modified in v{latest.version_number}. ' + ('; '.join(triggers) if triggers else 'Wording modified.')
        statuses.append({
            'node_id': src['node_id'],
            'latest_node_id': current.id if current else None,
            'status': status,
            'substantive_triggers': triggers,
            'summary': summary
        })

    labels = {x['status'] for x in statuses}
    overall = (
        'current' if labels == {'current'}
        else ('unresolved' if 'unresolved' in labels
        else ('substantive_change_suspected' if 'substantive_change_suspected' in labels
        else 'wording_changed'))
    )
    return {
        'status': overall,
        'is_stale': overall != 'current',
        'sources': statuses,
        'latest_version': latest.version_number,
        'clinical_disclaimer': 'Automated staleness detection is heuristic and does not determine clinical or regulatory safety significance. Human clinical review required.'
    }

@app.get('/generations')
def retrieve_generations(selection_id: Optional[str] = None, node_id: Optional[str] = None, db: Session = Depends(get_db)):
    if not selection_id and not node_id:
        raise HTTPException(400, 'Provide selection_id or node_id.')
    rows = read_generations()
    out = []
    for g in rows:
        if selection_id and g['selection_id'] != selection_id:
            continue
        if node_id and not any(x['node_id'] == node_id for x in g['source_nodes']):
            continue
        out.append({**g, 'traceability': generation_status(g, db)})
    return {'count': len(out), 'generations': out}

@app.get('/versions')
def versions(db: Session = Depends(get_db)):
    doc = db.scalar(select(Document).where(Document.name == 'CardioTrack CT-200'))
    if not doc:
        return {'versions': []}
    return {
        'versions': [
            {
                'version': v.version_number,
                'version_id': v.id,
                'source_filename': v.source_filename,
                'source_sha256': v.source_sha256,
                'node_count': len(v.nodes)
            }
            for v in sorted(doc.versions, key=lambda x: x.version_number)
        ]
    }
