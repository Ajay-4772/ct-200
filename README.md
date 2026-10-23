# CT-200 QA Traceability API

A focused FastAPI backend for parsing the supplied CardioTrack CT-200 manuals, preserving document versions, pinning selections to historical text, generating structured QA test ideas, and surfacing stale traceability.

> **Important:** This is an internship assignment prototype, not a validated medical-device QA tool. Automated text similarity cannot determine clinical or safety significance. All generated cases and changed requirements need human review.

## What is implemented

- PDF text/layout extraction using PyMuPDF, with OCR fallback for pages that have no usable text layer (Tesseract must be installed for fallback).
- CT-200-specific section parsing and parent-child hierarchy. Source order is preserved, including the 3.4-before-3.3 ordering.
- Content SHA-256 hashes, immutable version records, and version-specific node IDs.
- Logical-node matching by section number; changed body text keeps the logical identity while receiving a new version-specific node ID.
- Browse, search, compare, version-pinned selections, LLM generation, and retrieval-time stale status.
- JSON-file document store for generated LLM outputs (atomic replace on write), justified as the simplest allowed NoSQL alternative for this single-process assignment prototype.
- Pydantic schema validation, at most two LLM attempts, and no persistence when output remains invalid.
- Idempotent generation: submitting the same selection again returns the previous generation unless `force_new=true` is specified.

## Requirements

- Python 3.11+ (developed/tested with Python 3.13 in this environment)
- Tesseract OCR only if a PDF page has no usable text layer
- Optional Groq API key for live LLM generation

## Setup (Windows PowerShell)

```powershell
cd ct200-qa
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `GROQ_API_KEY` in `.env` to enable generation. The app loads `.env` automatically. For a simple PowerShell session instead:

```powershell
$env:GROQ_API_KEY="your_groq_api_key"
$env:GROQ_MODEL="llama-3.3-70b-versatile"
```

## Run

```powershell
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for Swagger UI. Health check: `GET /health`.

## Ingest v1 then v2

From the repository root, in PowerShell:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/ingest?version=1" -F "file=@data/ct200_manual.pdf"
curl.exe -X POST "http://127.0.0.1:8000/ingest?version=2" -F "file=@data/ct200_manual_v2.pdf"
curl.exe "http://127.0.0.1:8000/versions"
curl.exe "http://127.0.0.1:8000/sections?version=1"
curl.exe "http://127.0.0.1:8000/sections?version=2"
```

Versions are immutable. Re-running the same version number returns HTTP 409. To reset a local demo, stop the server and remove `ct200.db` and `generated.json` (or set `DATABASE_URL` and `GENERATIONS_PATH` to fresh paths before starting).

## End-to-end v1 → v2 traceability demonstration

1. Ingest both versions as above.
2. Search for the battery requirement in v1:

```powershell
curl.exe "http://127.0.0.1:8000/search?q=Battery%20Life%20Under%20Typical%20Use&version=1"
```

3. Copy the returned node `id`, then compare it to the latest version:

```powershell
curl.exe "http://127.0.0.1:8000/nodes/NODE_ID/compare"
```

4. Create a version-pinned selection using that v1 node ID:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/selections" -H "Content-Type: application/json" -d "{\"name\":\"Battery requirement v1\",\"version\":1,\"node_ids\":[\"NODE_ID\"]}"
```

5. Generate QA ideas (requires `GROQ_API_KEY`):

```powershell
curl.exe -X POST "http://127.0.0.1:8000/selections/SELECTION_ID/generate"
```

6. Retrieve generation and current traceability status:

```powershell
curl.exe "http://127.0.0.1:8000/generations?selection_id=SELECTION_ID"
curl.exe "http://127.0.0.1:8000/generations?node_id=NODE_ID"
```

A v1 battery selection should show a changed status against v2 because the cycle estimate and low-battery threshold changed. The exact status is a heuristic and is not a clinical risk determination.

## API overview

| Endpoint | Purpose |
|---|---|
| `POST /ingest?version=N` | Ingest a PDF as a new immutable version |
| `GET /versions` | List available versions and source hashes |
| `GET /sections?version=N` | List top-level sections (defaults to latest) |
| `GET /nodes/{node_id}` | Get a node, text, hash, and recursive children |
| `GET /search?q=...&version=N` | Search headings and body text |
| `GET /nodes/{node_id}/compare` | Compare node with latest corresponding logical node |
| `POST /selections` | Create version-pinned selection |
| `GET /selections/{selection_id}` | Resolve the exact stored selection text |
| `POST /selections/{selection_id}/generate` | Generate and validate 3–5 test ideas |
| `GET /generations?selection_id=...` | Retrieve generations and live staleness status |
| `GET /generations?node_id=...` | Retrieve generations sourced from a node and staleness status |

## Tests

```powershell
python -m pytest -q
```

Parser tests cover the skipped section-number level, out-of-order sections, duplicate heading labels under different parents, changed body text across versions, and new v2 sections.

## Known limitations

- The supplied PDFs have usable text layers, so OCR is a fallback rather than the main extraction route. OCR output needs visual verification.
- The main specifications and error-code tables are extracted into structured row/cell arrays. Borderless tables, merged cells, and other unusual table geometries are not guaranteed to reconstruct correctly.
- Matching by section number fails when sections are renumbered, duplicate section numbers are introduced, or the source omits numbering. Such cases need review.
- The JSON generation store is suitable for a local single-process prototype, not concurrent production workers; concurrent writes can race.
- Staleness labels use text similarity and hashes, not semantic/clinical validation. Even a small wording change may be important.
- LLM calls depend on external availability and credentials. Invalid responses are retried once; then the endpoint returns HTTP 502 and saves nothing.
