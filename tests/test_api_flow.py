import os
from pathlib import Path
os.environ['DATABASE_URL']='sqlite:////tmp/ct200_api_test.sqlite'
os.environ['GENERATIONS_PATH']='/tmp/ct200_api_test_generations.json'
os.environ.pop('GROQ_API_KEY',None)
from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine

ROOT=Path(__file__).parents[1]
client=TestClient(app)

def reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    Path('/tmp/ct200_api_test_generations.json').unlink(missing_ok=True)

def ingest(path,version):
    with open(path,'rb') as f:
        return client.post('/ingest',params={'version':version},files={'file':(Path(path).name,f,'application/pdf')})

def test_v1_v2_versioning_compare_and_pinned_selection():
    reset()
    a=ingest(ROOT/'data'/'ct200_manual.pdf',1)
    b=ingest(ROOT/'data'/'ct200_manual_v2.pdf',2)
    assert a.status_code==200 and a.json()['node_count'] >= 20
    assert b.status_code==200 and b.json()['node_count'] > a.json()['node_count']
    assert client.get('/sections',params={'version':1}).status_code==200
    found=client.get('/search',params={'q':'Battery Life Under Typical Use','version':1}).json()['results'][0]
    compare=client.get(f"/nodes/{found['id']}/compare").json()
    assert compare['status']=='changed'
    created=client.post('/selections',json={'name':'v1 battery','version':1,'node_ids':[found['id']]})
    assert created.status_code==201
    sel=client.get('/selections/'+created.json()['selection_id']).json()
    assert sel['version']==1 and '300 measurement cycles' in sel['items'][0]['source_text']
    assert client.post('/selections/'+created.json()['selection_id']+'/generate').status_code==503

def test_duplicate_version_is_rejected():
    reset()
    assert ingest(ROOT/'data'/'ct200_manual.pdf',1).status_code==200
    assert ingest(ROOT/'data'/'ct200_manual_v2.pdf',1).status_code==409

def test_generation_persists_snapshot_and_reports_staleness(monkeypatch):
    reset()
    assert ingest(ROOT/'data'/'ct200_manual.pdf',1).status_code==200
    assert ingest(ROOT/'data'/'ct200_manual_v2.pdf',2).status_code==200
    found=client.get('/search',params={'q':'Battery Life Under Typical Use','version':1}).json()['results'][0]
    selection=client.post('/selections',json={'name':'battery test source','version':1,'node_ids':[found['id']]}).json()
    async def fake_llm(source_text):
        return ({'test_cases':[{'title':f'Battery cycle check {i}','requirement_reference':'Section 2.1.1.1','preconditions':['Four fresh AA alkaline batteries are installed'],'steps':['Perform measurements under typical use conditions'],'expected_result':'The displayed low-battery icon follows the specified threshold.','risk':'high'} for i in range(1,4)]},1)
    monkeypatch.setattr('app.main.call_llm',fake_llm)
    first=client.post(f"/selections/{selection['selection_id']}/generate")
    assert first.status_code==201
    generation_id=first.json()['generation']['generation_id']
    second=client.post(f"/selections/{selection['selection_id']}/generate")
    assert second.status_code==200 and second.json()['policy']=='idempotent'
    assert second.json()['generation']['generation_id']==generation_id
    retrieved=client.get('/generations',params={'selection_id':selection['selection_id']}).json()
    assert retrieved['count']==1
    trace=retrieved['generations'][0]['traceability']
    assert trace['status'] in {'wording_changed','substantive_change_suspected'}
    assert trace['sources'][0]['status'] != 'current'
