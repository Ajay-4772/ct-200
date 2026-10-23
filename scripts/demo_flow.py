"""Live HTTP walkthrough. Start uvicorn first, then run: python scripts/demo_flow.py"""
from pathlib import Path
import os, sys, httpx
BASE=os.getenv('CT200_API_URL','http://127.0.0.1:8000')
ROOT=Path(__file__).parents[1]

def require(ok, message):
    if not ok: raise SystemExit(message)

with httpx.Client(base_url=BASE,timeout=60) as c:
    require(c.get('/health').status_code==200,'API is not reachable. Start uvicorn app.main:app --reload.')
    existing=c.get('/versions').json().get('versions',[])
    have={v['version'] for v in existing}
    for version,filename in [(1,'ct200_manual.pdf'),(2,'ct200_manual_v2.pdf')]:
        if version in have:
            print(f'v{version} already ingested; preserving existing immutable version.')
            continue
        with (ROOT/'data'/filename).open('rb') as f:
            r=c.post('/ingest',params={'version':version},files={'file':(filename,f,'application/pdf')})
        require(r.status_code==200,f'Ingestion v{version} failed: {r.status_code} {r.text}')
        print(f'Ingested v{version}: {r.json()["node_count"]} nodes; warnings={len(r.json()["node_warnings"])}')
    r=c.get('/search',params={'q':'Battery Life Under Typical Use','version':1})
    require(r.status_code==200 and r.json()['count']>0,'Could not find battery section in v1.')
    node=r.json()['results'][0]
    print('\nCompare v1 battery section with latest:')
    print(c.get(f"/nodes/{node['id']}/compare").json())
    r=c.post('/selections',json={'name':'Battery requirement from v1','version':1,'node_ids':[node['id']]})
    require(r.status_code==201,f'Selection creation failed: {r.text}')
    selection_id=r.json()['selection_id']
    print('\nPinned selection:',c.get(f'/selections/{selection_id}').json())
    if os.getenv('GROQ_API_KEY'):
        g=c.post(f'/selections/{selection_id}/generate')
        require(g.status_code in (200,201),f'Generation failed: {g.status_code} {g.text}')
        result=c.get('/generations',params={'selection_id':selection_id})
        print('\nGeneration and live staleness status:')
        print(result.text)
    else:
        print('\nGROQ_API_KEY is not configured; live LLM generation skipped. Set it in .env and rerun with a new selection to exercise generation.')
