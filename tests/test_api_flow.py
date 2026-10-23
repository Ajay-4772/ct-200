import os, tempfile, json
from pathlib import Path
from fastapi import HTTPException
import httpx
import pytest

# Cross-platform safe temp directory for tests
TEST_DB = Path(tempfile.gettempdir()) / 'ct200_api_test.sqlite'
TEST_GEN = Path(tempfile.gettempdir()) / 'ct200_api_test_generations.json'
os.environ['DATABASE_URL'] = f'sqlite:///{TEST_DB.as_posix()}'
os.environ['GENERATIONS_PATH'] = str(TEST_GEN)
os.environ.pop('GROQ_API_KEY', None)

from fastapi.testclient import TestClient
from app.main import app, call_llm
from app.db import Base, engine

ROOT = Path(__file__).parents[1]
client = TestClient(app)

def reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    TEST_GEN.unlink(missing_ok=True)

def ingest(path, version):
    with open(path, 'rb') as f:
        return client.post('/ingest', params={'version': version}, files={'file': (Path(path).name, f, 'application/pdf')})

def test_v1_v2_versioning_compare_and_pinned_selection(monkeypatch):
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    reset()
    a = ingest(ROOT / 'data' / 'ct200_manual.pdf', 1)
    b = ingest(ROOT / 'data' / 'ct200_manual_v2.pdf', 2)
    assert a.status_code == 200 and a.json()['node_count'] >= 20
    assert b.status_code == 200 and b.json()['node_count'] > a.json()['node_count']
    assert client.get('/sections', params={'version': 1}).status_code == 200
    found = client.get('/search', params={'q': 'Battery Life Under Typical Use', 'version': 1}).json()['results'][0]
    compare = client.get(f"/nodes/{found['id']}/compare").json()
    assert compare['status'] == 'changed'
    assert compare['substantive_change_suspected'] is True
    assert any('300' in t for t in compare['substantive_triggers'])
    created = client.post('/selections', json={'name': 'v1 battery', 'version': 1, 'node_ids': [found['id']]})
    assert created.status_code == 201
    sel = client.get('/selections/' + created.json()['selection_id']).json()
    assert sel['version'] == 1 and '300 measurement cycles' in sel['items'][0]['source_text']
    assert client.post('/selections/' + created.json()['selection_id'] + '/generate').status_code == 503

def test_duplicate_version_is_rejected():
    reset()
    assert ingest(ROOT / 'data' / 'ct200_manual.pdf', 1).status_code == 200
    assert ingest(ROOT / 'data' / 'ct200_manual_v2.pdf', 1).status_code == 409

def test_versions_compare_endpoint_flags_added_removed_changed_unchanged():
    reset()
    assert ingest(ROOT / 'data' / 'ct200_manual.pdf', 1).status_code == 200
    assert ingest(ROOT / 'data' / 'ct200_manual_v2.pdf', 2).status_code == 200
    diff_res = client.get('/versions/compare', params={'from_version': 1, 'to_version': 2})
    assert diff_res.status_code == 200
    diff = diff_res.json()
    # Section 5.3 Data Export is newly added in v2
    added_keys = [n['logical_key'] for n in diff['added_nodes']]
    assert 'SECTION:5.3' in added_keys
    # No sections were deleted between v1 and v2
    assert len(diff['removed_nodes']) == 0
    # Changed sections include battery (2.1.1.1), cuff inflation (3.2), error codes (4.2)
    changed_keys = [n['logical_key'] for n in diff['changed_nodes']]
    assert 'SECTION:2.1.1.1' in changed_keys
    assert 'SECTION:3.2' in changed_keys
    assert 'SECTION:4.2' in changed_keys
    # Unchanged sections include 1, 1.1, 1.2, 2.2, etc.
    unchanged_keys = [n['logical_key'] for n in diff['unchanged_nodes']]
    assert 'SECTION:1' in unchanged_keys
    assert 'SECTION:1.1' in unchanged_keys
    assert diff['summary']['added_count'] >= 1
    assert diff['summary']['changed_count'] >= 3
    assert diff['summary']['unchanged_count'] >= 15

def test_missing_api_key_returns_503(monkeypatch):
    reset()
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    assert ingest(ROOT / 'data' / 'ct200_manual.pdf', 1).status_code == 200
    found = client.get('/search', params={'q': 'Battery Life Under Typical Use', 'version': 1}).json()['results'][0]
    selection = client.post('/selections', json={'name': 'battery', 'version': 1, 'node_ids': [found['id']]}).json()
    res = client.post(f"/selections/{selection['selection_id']}/generate")
    assert res.status_code == 503
    assert 'GROQ_API_KEY' in res.json()['detail']

def test_malformed_llm_json_and_schema_failure_handling(monkeypatch):
    reset()
    assert ingest(ROOT / 'data' / 'ct200_manual.pdf', 1).status_code == 200
    found = client.get('/search', params={'q': 'Battery Life Under Typical Use', 'version': 1}).json()['results'][0]
    selection = client.post('/selections', json={'name': 'battery test', 'version': 1, 'node_ids': [found['id']]}).json()

    # Case 1: Provider error or malformed non-JSON response causes 502 with fail-closed behavior
    attempts_seen = []
    async def mock_failing_llm(source_text):
        attempts_seen.append(1)
        raise HTTPException(502, "LLM output failed validation after 2 attempts; nothing was saved. Details: JSONDecodeError: Expecting value: line 1 column 1")
    monkeypatch.setattr('app.main.call_llm', mock_failing_llm)

    res = client.post(f"/selections/{selection['selection_id']}/generate")
    assert res.status_code == 502
    assert 'nothing was saved' in res.json()['detail']

    # Confirm nothing was saved in the document store
    generations_res = client.get('/generations', params={'selection_id': selection['selection_id']})
    assert generations_res.json()['count'] == 0

def test_generation_persists_snapshot_and_reports_staleness(monkeypatch):
    reset()
    assert ingest(ROOT / 'data' / 'ct200_manual.pdf', 1).status_code == 200
    assert ingest(ROOT / 'data' / 'ct200_manual_v2.pdf', 2).status_code == 200
    found = client.get('/search', params={'q': 'Battery Life Under Typical Use', 'version': 1}).json()['results'][0]
    selection = client.post('/selections', json={'name': 'battery test source', 'version': 1, 'node_ids': [found['id']]}).json()

    async def fake_llm(source_text):
        return ({
            'test_cases': [
                {
                    'title': f'Battery cycle check {i}',
                    'rationale': 'Verify battery cycle life complies with specification before cutoff',
                    'requirement_reference': 'Section 2.1.1.1',
                    'source_reference': 'Section 2.1.1.1',
                    'preconditions': ['Four fresh AA alkaline batteries are installed'],
                    'steps': ['Perform measurements under typical use conditions'],
                    'expected_result': 'The displayed low-battery icon follows the specified threshold.',
                    'severity': 'high',
                    'risk': 'high'
                }
                for i in range(1, 4)
            ]
        }, 1)
    monkeypatch.setattr('app.main.call_llm', fake_llm)

    first = client.post(f"/selections/{selection['selection_id']}/generate")
    assert first.status_code == 201
    generation = first.json()['generation']
    generation_id = generation['generation_id']
    # Check that model and provider provenance are persisted
    assert 'model' in generation and 'provider' in generation
    assert generation['validation'] == 'passed'

    second = client.post(f"/selections/{selection['selection_id']}/generate")
    assert second.status_code == 200 and second.json()['policy'] == 'idempotent'
    assert second.json()['generation']['generation_id'] == generation_id

    # Test force_new=True forces regeneration
    third = client.post(f"/selections/{selection['selection_id']}/generate", params={'force_new': True})
    assert third.status_code == 201 and third.json()['policy'] == 'new_generation'
    assert third.json()['generation']['generation_id'] != generation_id

    retrieved = client.get('/generations', params={'selection_id': selection['selection_id']}).json()
    assert retrieved['count'] == 2
    trace = retrieved['generations'][0]['traceability']
    assert trace['status'] == 'substantive_change_suspected'
    assert trace['is_stale'] is True
    assert any('300' in t for t in trace['sources'][0]['substantive_triggers'])

def test_lifecycle_staleness_reporting_after_v2_ingestion(monkeypatch):
    """Explicitly tests that a test case generated from v1 battery requirements
    is reported 'current' initially, and is reported 'stale' after ingesting v2."""
    reset()
    # 1. Ingest v1 ONLY
    assert ingest(ROOT / 'data' / 'ct200_manual.pdf', 1).status_code == 200

    # 2. Find v1 battery requirement and create pinned selection
    found = client.get('/search', params={'q': 'Battery Life Under Typical Use', 'version': 1}).json()['results'][0]
    selection = client.post('/selections', json={'name': 'v1 battery selection', 'version': 1, 'node_ids': [found['id']]}).json()

    async def fake_llm(source_text):
        return ({
            'test_cases': [
                {
                    'title': f'Battery depletion test {i}',
                    'rationale': 'Ensure device alerts at proper battery threshold',
                    'requirement_reference': 'Section 2.1.1.1',
                    'preconditions': ['New AA batteries installed'],
                    'steps': ['Run test cycles'],
                    'expected_result': 'Low battery icon displays at threshold',
                    'severity': 'medium'
                }
                for i in range(1, 4)
            ]
        }, 1)
    monkeypatch.setattr('app.main.call_llm', fake_llm)

    # 3. Generate test cases under v1
    gen_res = client.post(f"/selections/{selection['selection_id']}/generate")
    assert gen_res.status_code == 201

    # 4. Under v1 alone, traceability should be 'current'
    check1 = client.get('/generations', params={'selection_id': selection['selection_id']}).json()
    trace1 = check1['generations'][0]['traceability']
    assert trace1['status'] == 'current'
    assert trace1['is_stale'] is False

    # 5. Now ingest v2 (battery cycles change 300->250, threshold 15%->10%)
    assert ingest(ROOT / 'data' / 'ct200_manual_v2.pdf', 2).status_code == 200

    # 6. Retrieve generations again: should NOW be reported stale!
    check2 = client.get('/generations', params={'selection_id': selection['selection_id']}).json()
    trace2 = check2['generations'][0]['traceability']
    assert trace2['status'] == 'substantive_change_suspected'
    assert trace2['is_stale'] is True
    assert trace2['latest_version'] == 2
    assert any('300' in t for t in trace2['sources'][0]['substantive_triggers'])

    # 7. Also verify retrieval by node_id returns staleness
    node_retrieval = client.get('/generations', params={'node_id': found['id']}).json()
    assert node_retrieval['count'] >= 1
    assert node_retrieval['generations'][0]['traceability']['is_stale'] is True

@pytest.mark.anyio
async def test_call_llm_retries_and_fails_on_malformed_json(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'test_key_placeholder')
    attempts = []

    class MockResponse:
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'message': {'content': 'invalid json {'}}]}

    async def mock_post(*args, **kwargs):
        attempts.append(1)
        return MockResponse()

    monkeypatch.setattr('httpx.AsyncClient.post', mock_post)
    with pytest.raises(HTTPException) as exc:
        await call_llm("sample manual text")
    assert exc.value.status_code == 502
    assert len(attempts) == 2

@pytest.mark.anyio
async def test_call_llm_retries_and_fails_on_invalid_count(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'test_key_placeholder')
    attempts = []

    class MockResponse:
        def raise_for_status(self): pass
        def json(self):
            # Only 1 test case (< 3 minimum required)
            return {'choices': [{'message': {'content': json.dumps({
                'test_cases': [{
                    'title': 'Test 1',
                    'rationale': 'R1',
                    'source_reference': 'S1',
                    'preconditions': ['P1'],
                    'steps': ['Step 1'],
                    'expected_result': 'Result 1',
                    'severity': 'low'
                }]
            })}}]}

    async def mock_post(*args, **kwargs):
        attempts.append(1)
        return MockResponse()

    monkeypatch.setattr('httpx.AsyncClient.post', mock_post)
    with pytest.raises(HTTPException) as exc:
        await call_llm("sample manual text")
    assert exc.value.status_code == 502
    assert len(attempts) == 2

@pytest.mark.anyio
async def test_call_llm_recovers_on_second_attempt(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'test_key_placeholder')
    attempts = []

    class MockFailResponse:
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'message': {'content': 'malformed'}}]}

    class MockSuccessResponse:
        def raise_for_status(self): pass
        def json(self):
            return {'choices': [{'message': {'content': json.dumps({
                'test_cases': [
                    {
                        'title': f'TestCase {i}',
                        'rationale': f'Rationale {i}',
                        'source_reference': f'Section {i}',
                        'preconditions': ['Precond'],
                        'steps': ['Step 1', 'Step 2'],
                        'expected_result': 'Expected outcome verified',
                        'severity': 'medium'
                    }
                    for i in range(1, 4)
                ]
            })}}]}

    async def mock_post(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return MockFailResponse()
        return MockSuccessResponse()

    monkeypatch.setattr('httpx.AsyncClient.post', mock_post)
    output, attempt_num = await call_llm("sample manual text")
    assert attempt_num == 2
    assert len(output['test_cases']) == 3

@pytest.mark.anyio
async def test_call_llm_handles_provider_http_error(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'test_key_placeholder')
    attempts = []

    async def mock_post(*args, **kwargs):
        attempts.append(1)
        req = httpx.Request('POST', 'https://api.groq.com')
        resp = httpx.Response(500, request=req)
        raise httpx.HTTPStatusError("Internal Server Error", request=req, response=resp)

    monkeypatch.setattr('httpx.AsyncClient.post', mock_post)
    with pytest.raises(HTTPException) as exc:
        await call_llm("sample manual text")
    assert exc.value.status_code == 502
    assert len(attempts) == 2

@pytest.mark.skipif(not os.getenv('GROQ_API_KEY'), reason="GROQ_API_KEY not configured in environment")
def test_live_groq_llm_smoke_test():
    reset()
    assert ingest(ROOT / 'data' / 'ct200_manual.pdf', 1).status_code == 200
    found = client.get('/search', params={'q': 'Battery Life Under Typical Use', 'version': 1}).json()['results'][0]
    selection = client.post('/selections', json={'name': 'Live Groq Test', 'version': 1, 'node_ids': [found['id']]}).json()
    res = client.post(f"/selections/{selection['selection_id']}/generate")
    assert res.status_code == 201
    gen = res.json()['generation']
    assert 3 <= len(gen['test_cases']) <= 5
    for tc in gen['test_cases']:
        assert tc['title'] and tc['steps'] and tc['expected_result'] and tc['severity']
    assert gen['model'] and gen['provider'] == 'groq'

