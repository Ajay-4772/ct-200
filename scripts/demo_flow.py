"""CT-200 QA Traceability — End-to-End Demonstration Script.

Usage:
    python scripts/demo_flow.py

Can run against a live running server (http://127.0.0.1:8000) or directly in-process
via FastAPI TestClient if the server is not started yet.
"""
from __future__ import annotations
import os, sys, json
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import httpx

BASE_URL = os.getenv('CT200_API_URL', 'http://127.0.0.1:8000')

def get_client():
    # Test if live server is reachable
    try:
        r = httpx.get(f'{BASE_URL}/health', timeout=1.5)
        if r.status_code == 200:
            print(f"[Mode] Connected to live FastAPI server at {BASE_URL}")
            return httpx.Client(base_url=BASE_URL, timeout=60), False
    except Exception:
        pass

    # Fallback to in-process client
    print("[Mode] Live server not detected on localhost:8000; running in-process via TestClient...")
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app), True

def main():
    client, in_process = get_client()

    print("\n=======================================================")
    print(" 1. HEALTH CHECK")
    print("=======================================================")
    r = client.get('/health')
    print("GET /health ->", r.json())
    assert r.status_code == 200

    print("\n=======================================================")
    print(" 2. INGESTING V1 AND V2 AS IMMUTABLE VERSIONS")
    print("=======================================================")
    existing_versions = client.get('/versions').json().get('versions', [])
    have = {v['version'] for v in existing_versions}

    for ver_num, filename in [(1, 'ct200_manual.pdf'), (2, 'ct200_manual_v2.pdf')]:
        if ver_num in have:
            print(f"Version {ver_num} already exists in DB; preserving immutable version record.")
            continue
        pdf_path = ROOT / 'data' / filename
        with open(pdf_path, 'rb') as f:
            res = client.post('/ingest', params={'version': ver_num}, files={'file': (filename, f, 'application/pdf')})
        if res.status_code != 200:
            print(f"Ingestion error for v{ver_num}:", res.text)
            sys.exit(1)
        data = res.json()
        print(f"Ingested v{ver_num} ({filename}):")
        print(f"  - Node count: {data['node_count']}")
        print(f"  - Source SHA256: {data['source_sha256']}")
        print(f"  - Document warnings: {len(data['warnings'])}")
        print(f"  - Structural node warnings detected: {len(data['node_warnings'])}")

    print("\n=======================================================")
    print(" 3. DOCUMENT-LEVEL VERSION COMPARISON (v1 -> v2)")
    print("=======================================================")
    v_diff = client.get('/versions/compare', params={'from_version': 1, 'to_version': 2}).json()
    print("Summary:")
    for k, v in v_diff['summary'].items():
        print(f"  {k}: {v}")

    print("\nNewly Added Nodes in v2:")
    for n in v_diff['added_nodes']:
        print(f"  + [{n['logical_key']}] {n['heading']}")

    print("\nChanged Nodes (v1 -> v2):")
    for n in v_diff['changed_nodes']:
        print(f"  * [{n['logical_key']}] {n['heading']}")
        print(f"    Triggers: {n['substantive_triggers']}")

    print("\n=======================================================")
    print(" 4. SEARCHING HEADINGS AND BODY TEXT IN V1")
    print("=======================================================")
    search_res = client.get('/search', params={'q': 'Battery Life Under Typical Use', 'version': 1}).json()
    print(f"Query: 'Battery Life Under Typical Use' (v1) -> Found {search_res['count']} match(es):")
    battery_node = search_res['results'][0]
    print(f"  Node ID: {battery_node['id']}")
    print(f"  Heading: {battery_node['heading']}")
    print(f"  Content Hash: {battery_node['content_hash']}")
    print(f"  Body Preview:\n    {battery_node['body_text']}")

    print("\n=======================================================")
    print(" 5. COMPARING V1 BATTERY NODE WITH LATEST (v2)")
    print("=======================================================")
    node_comp = client.get(f"/nodes/{battery_node['id']}/compare").json()
    print("Comparison Result:")
    print(f"  Status: {node_comp['status']} (detailed: {node_comp.get('detailed_status')})")
    print(f"  Substantive change suspected: {node_comp.get('substantive_change_suspected')}")
    print(f"  Detected Triggers: {node_comp.get('substantive_triggers')}")
    print("  Diff summary:")
    for line in node_comp.get('diff_summary', '').splitlines()[:10]:
        print(f"    {line}")

    print("\n=======================================================")
    print(" 6. CREATING VERSION-PINNED SELECTION (v1)")
    print("=======================================================")
    sel_res = client.post('/selections', json={
        'name': 'v1 Battery Requirement Selection',
        'version': 1,
        'node_ids': [battery_node['id']]
    })
    sel_data = sel_res.json()
    selection_id = sel_data['selection_id']
    print(f"Selection Created: {selection_id}")
    print(f"Pinned to Version: {sel_data['version']}, Node Count: {sel_data['node_count']}")

    print("\n=======================================================")
    print(" 7. GENERATING STRUCTURED QA TEST CASES")
    print("=======================================================")
    groq_key = os.getenv('GROQ_API_KEY')
    if groq_key:
        print("[Live LLM] Using configured GROQ_API_KEY...")
        gen_res = client.post(f"/selections/{selection_id}/generate")
    else:
        print("[Demo Mode] GROQ_API_KEY not set in environment.")
        print("Executing generation endpoint with simulated validated QA generator to illustrate full output structure...")
        # Patch call_llm temporarily for the demo run
        from app import main as app_main
        async def demo_llm(source_text):
            return ({
                'test_cases': [
                    {
                        'title': 'Verify Battery Cycle Life Under Typical Measurement Conditions',
                        'rationale': 'Ensure device reaches rated 300 cycles before requiring replacement (Section 2.1.1.1 v1).',
                        'source_reference': 'Section 2.1.1.1',
                        'preconditions': ['Four fresh AA alkaline batteries installed', 'Room temperature 20-25 deg C'],
                        'steps': [
                            'Configure device for User 1 profile',
                            'Execute 3 measurement cycles per day simulating oscillometric load',
                            'Log battery indicator status on display after each cycle'
                        ],
                        'expected_result': 'Device completes approximately 300 cycles without low-battery warning.',
                        'severity': 'high'
                    },
                    {
                        'title': 'Verify Low Battery Warning Icon at Threshold Capacity',
                        'rationale': 'Prevent unexpected measurement abort during critical use when battery drops below 15%.',
                        'source_reference': 'Section 2.1.1.1',
                        'preconditions': ['Variable DC power supply connected simulating battery voltage depletion'],
                        'steps': [
                            'Reduce supply voltage until calculated capacity reaches 15%',
                            'Observe LCD screen icons during standby and inflation sequence'
                        ],
                        'expected_result': 'Low-battery icon illuminates promptly when capacity reaches <15%.',
                        'severity': 'critical'
                    },
                    {
                        'title': 'Verify Graceful Measurement Abort on Critical Battery Depletion',
                        'rationale': 'Ensure overpressure relief valve and deflation trigger safely if power fails mid-inflation.',
                        'source_reference': 'Section 2.1.1.1 & Section 4.2 (E4)',
                        'preconditions': ['Cuff attached to artificial arm fixture', 'Battery capacity set near cutoff'],
                        'steps': [
                            'Initiate measurement inflation',
                            'Drop supply voltage below operating threshold mid-measurement'
                        ],
                        'expected_result': 'Device aborts measurement, displays E4 error code, and deflates cuff within 2 seconds.',
                        'severity': 'critical'
                    }
                ]
            }, 1)
        app_main.call_llm = demo_llm
        gen_res = client.post(f"/selections/{selection_id}/generate")

    print(f"Generation HTTP Status: {gen_res.status_code}")
    gen_data = gen_res.json()
    print("Policy:", gen_data.get('policy'))
    generation = gen_data.get('generation', {})
    print(f"Generation ID: {generation.get('generation_id')}")
    print(f"Provider: {generation.get('provider')}, Model: {generation.get('model')}")
    print(f"Validation: {generation.get('validation')}, Attempts: {generation.get('llm_attempts')}")
    print(f"Generated Test Cases ({len(generation.get('test_cases', []))} cases):")
    for tc in generation.get('test_cases', []):
        print(f"\n  - Title: {tc['title']}")
        print(f"    Severity: {tc['severity']}")
        print(f"    Source Reference: {tc['source_reference']}")
        print(f"    Rationale: {tc.get('rationale')}")
        print(f"    Preconditions: {tc['preconditions']}")
        print(f"    Steps: {tc['steps']}")
        print(f"    Expected Result: {tc['expected_result']}")

    print("\n=======================================================")
    print(" 8. RETRIEVAL & LIVE STALENESS MONITORING")
    print("=======================================================")
    ret_res = client.get('/generations', params={'selection_id': selection_id}).json()
    gen_record = ret_res['generations'][0]
    trace = gen_record['traceability']
    print(f"Traceability Overall Status: {trace['status']}")
    print(f"Is Stale: {trace['is_stale']}")
    print(f"Compared against latest version: v{trace['latest_version']}")
    print(f"Clinical Disclaimer: {trace.get('clinical_disclaimer')}")
    print("Per-source traceability breakdown:")
    for src in trace['sources']:
        print(f"  Node {src['node_id']} -> Status: {src['status']}")
        print(f"    Summary: {src['summary']}")
        print(f"    Substantive Triggers: {src['substantive_triggers']}")

    print("\n=======================================================")
    print(" 9. IDEMPOTENCY VERIFICATION")
    print("=======================================================")
    idempotent_res = client.post(f"/selections/{selection_id}/generate")
    print(f"Submitting same selection again -> Status: {idempotent_res.status_code}")
    print(f"Policy: {idempotent_res.json().get('policy')}")
    print(f"Returned same generation_id: {idempotent_res.json()['generation']['generation_id'] == generation['generation_id']}")

    print("\nSUCCESS: End-to-end demonstration completed successfully!")

if __name__ == '__main__':
    main()
