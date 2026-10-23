# CT-200 QA Traceability API

A production-grade FastAPI backend for parsing CardioTrack CT-200 Technical and User Manuals, maintaining immutable version trees, pinning QA selections to historical source text snapshots, generating structured QA test cases via LLM, and continuously tracking traceability and substantive staleness across versions.

> [!IMPORTANT]
> **Clinical & Regulatory Disclaimer:** This system provides automated engineering and QA traceability heuristics. Automated text comparison and heuristic triggers (for thresholds, numbers, units, or timing) cannot determine clinical or safety significance. All generated test cases and requirement modifications require qualified human engineering and clinical review.

---

## Architecture & Features

- **PDF Parsing & Layout Extraction:** Uses PyMuPDF for layout and text layer analysis, with OCR fallback for image-only pages. Preserves multi-line document titles, sections, subsections, bullet/numbered lists, and tables.
- **Structural Irregularity Detection:** Identifies skipped numbering levels (e.g. `2.1.1.1` skipping `2.1.1`), out-of-order sibling sections (e.g. `3.4 Auto Shutoff` preceding `3.3 Result Display`), duplicate heading labels (`4.2 Error Codes` vs `7.1 Error Codes`), and page-break continuations (e.g. `3.1` spanning pages 2–3, `4.3` spanning pages 4–5).
- **Immutable Document Versions:** Ingests v1 and v2 as separate, immutable versions with source file SHA-256 validation. Duplicate version ingestion is rejected (HTTP 409).
- **Logical Node Matching:** Matches nodes across versions using stable logical paths (`SECTION:<num>`), allowing body text to evolve while maintaining historical identity.
- **Version Comparison & Diffing:** Categorizes nodes between versions into `added`, `removed`, `changed`, and `unchanged` (`GET /versions/compare`), and generates unified diffs with substantive change triggers (`GET /nodes/{node_id}/compare`).
- **Version-Pinned Selections:** Pin named test suites to exact historical node/version pairs, capturing frozen text snapshots and content hashes.
- **Structured QA Test Generation:** Generates 3–5 structured test cases containing `title`, `rationale`, `source_reference`, `preconditions`, `steps`, `expected_result`, and `severity`. Validated via Pydantic with bounded retries and fail-closed error handling (HTTP 502 with nothing persisted on invalid output).
- **Substantive Staleness Analysis:** Evaluates exact source text snapshots against the latest corresponding nodes. Detects and surfaces modifications to numerical thresholds (e.g. `300 -> 250` cycles, `15% -> 10%`), units, timing (e.g. `2s -> 1.5s`), negation words, and safety terminology.
- **Idempotency:** Re-submitting an existing selection returns the prior generation by default, while supporting forced re-generation via `force_new=true`.
- **JSON Document Store:** Persists generation records with full provenance (model, provider, selection ID, version ID, source snapshots, timestamps, validation status).

---

## Prerequisites

- **Python:** Python 3.11+ (verified on Python 3.13 on Windows)
- **OCR Engine (Optional):** Tesseract OCR (only needed if PDF pages lack a digital text layer)
- **LLM Provider (Optional for live generation):** Groq API key (`GROQ_API_KEY`)

---

## Setup (Windows PowerShell)

Open PowerShell in the project directory:

```powershell
cd ct200-qa
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

To configure live LLM test generation with Groq, edit `.env` or set environment variables:

```powershell
$env:GROQ_API_KEY="your_groq_api_key_here"
$env:GROQ_MODEL="llama-3.3-70b-versatile"
```

*(If `GROQ_API_KEY` is not set, all parsing, search, compare, versioning, selection, and staleness endpoints work completely; the generation endpoint returns HTTP 503 explaining that the key is unconfigured).*

---

## Running the API Server

Start the local server using Uvicorn:

```powershell
uvicorn app.main:app --reload
```

- **Interactive API Documentation (Swagger UI):** `http://127.0.0.1:8000/docs`
- **Alternative ReDoc UI:** `http://127.0.0.1:8000/redoc`
- **Health Check:** `http://127.0.0.1:8000/health`

---

## End-to-End Workflow Demonstration

You can run the fully automated demonstration script at any time:

```powershell
python scripts/demo_flow.py
```

The script automatically detects whether the live Uvicorn server is running or runs in-process via FastAPI's TestClient, providing a complete walkthrough of health check, v1/v2 ingestion, version diffing, search, compare, selection creation, generation, and retrieval staleness.

### Manual Ingestion & Verification (PowerShell / curl)

#### 1. Ingest v1 and v2 Manuals
```powershell
curl.exe -X POST "http://127.0.0.1:8000/ingest?version=1" -F "file=@data/ct200_manual.pdf"
curl.exe -X POST "http://127.0.0.1:8000/ingest?version=2" -F "file=@data/ct200_manual_v2.pdf"
```

#### 2. Verify Ingested Versions
```powershell
curl.exe "http://127.0.0.1:8000/versions"
```

#### 3. Compare Version 1 and Version 2
```powershell
curl.exe "http://127.0.0.1:8000/versions/compare?from_version=1&to_version=2"
```
*Categorizes nodes into `added_nodes` (e.g. 5.3 Data Export), `changed_nodes` (2.1.1.1 battery, 3.2 inflation sequence, 4.2 error codes), `removed_nodes`, and `unchanged_nodes`.*

#### 4. Search for Requirements in v1
```powershell
curl.exe "http://127.0.0.1:8000/search?q=Battery%20Life%20Under%20Typical%20Use&version=1"
```
Copy the returned `id` (e.g., `node_8bdcea724033445c`).

#### 5. Compare Node with Target/Latest Version
```powershell
curl.exe "http://127.0.0.1:8000/nodes/NODE_ID/compare"
```
*Returns `status: changed`, `substantive_change_suspected: true`, substantive triggers (e.g. `300 -> 250`, `15% -> 10%`), and unified line diff.*

#### 6. Create Version-Pinned Selection
```powershell
curl.exe -X POST "http://127.0.0.1:8000/selections" `
  -H "Content-Type: application/json" `
  -d '{"name": "v1 Battery Requirements", "version": 1, "node_ids": ["NODE_ID"]}'
```
Copy the returned `selection_id` (e.g. `sel_bdd2f2af0fd24516`).

#### 7. Generate QA Test Cases (Requires `GROQ_API_KEY`)
```powershell
curl.exe -X POST "http://127.0.0.1:8000/selections/SELECTION_ID/generate"
```

#### 8. Retrieve Generated Test Cases & Staleness
```powershell
curl.exe "http://127.0.0.1:8000/generations?selection_id=SELECTION_ID"
```
*Shows `traceability.is_stale: true`, `status: substantive_change_suspected`, and exact triggers highlighting the modified numbers/units.*

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check endpoint |
| `POST` | `/ingest?version=N` | Ingests a PDF file as an immutable document version |
| `GET` | `/versions` | Lists all ingested document versions and source SHA-256 hashes |
| `GET` | `/versions/compare` | Compares two versions; lists added, removed, changed, unchanged nodes |
| `GET` | `/sections?version=N` | Lists top-level sections (defaults to latest version) |
| `GET` | `/nodes/{node_id}` | Retrieves node, complete text, content hash, and recursive children |
| `GET` | `/search?q=...&version=N` | Searches headings and body text within a given version |
| `GET` | `/nodes/{node_id}/compare` | Compares node with corresponding node in target/latest version |
| `POST` | `/selections` | Creates a version-pinned selection with frozen source snapshots |
| `GET` | `/selections/{selection_id}`| Retrieves selection and exact pinned source text |
| `POST` | `/selections/{id}/generate` | Generates 3–5 structured QA test cases (idempotent unless `force_new=true`) |
| `GET` | `/generations` | Retrieves generated test suites by selection or node ID with staleness |

---

## Running the Automated Test Suite

Run the full suite of unit and integration tests using pytest:

```powershell
python -m pytest -v
```

The test suite covers:
- **Skipped Numbering Levels:** Section `2.1.1.1` attaching to `2.1` with explicit warning.
- **Out-of-Order Sibling Sections:** Section `3.4` appearing before `3.3` preserved in source order with warning.
- **Duplicate Heading Text:** Section `4.2 Error Codes` and `7.1 Error Codes` retained as distinct logical nodes under parents `4` and `7`.
- **Page-Break Continuations:** Section `3.1` spanning pages 2–3 in v1; section `4.3` spanning pages 4–5 in v2.
- **Multi-line Wrapped Title:** Merging wrapped lines on page 1 into a single document title node.
- **List Preservation:** Preserving clinical classification list items under Section 3.3 in body text without misparsing them as section nodes.
- **Tables:** Reconstructing specifications (8×2) and error code tables (6×3 in v1, 7×3 in v2) with row/cell data.
- **Version Diffing (`/versions/compare`):** Accurate flagging of added (5.3), changed (2.1.1.1, 3.2, 4.2), and unchanged sections.
- **Provider & Auth Failures:** Missing `GROQ_API_KEY` returning HTTP 503.
- **Fail-Closed Retries:** Malformed LLM outputs and schema failures bounded to 2 attempts, returning HTTP 502 with nothing persisted.
- **Lifecycle Staleness:** Test cases generated from v1 battery requirements transitioning from `current` (`is_stale: false`) to `substantive_change_suspected` (`is_stale: true`) upon v2 ingestion.
- **Idempotency:** Verification of duplicate submission protection and explicit regeneration via `force_new=true`.
