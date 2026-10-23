# CT-200 Parsing, Versioning & QA Traceability — Approach

## 1. Scope and source inspection

The parser targets the two supplied CT-200 PDFs rather than arbitrary PDFs. The accompanying assignment update explicitly says to work directly from the PDF. The v1 manual has 6 pages and v2 has 7 pages. Both contain a usable text layer plus visual tables. PyMuPDF is used for text extraction and layout spans; OCR is attempted only when a page yields no usable text, because OCR can introduce character and threshold errors unnecessarily when digital text is already available.

Before finalizing the parser, the extracted structure was compared with the PDF's visible headings and page content. The parser retains body lines instead of silently dropping unfamiliar content. Table text remains in the owning section, but reliable cell reconstruction is not claimed where the PDF text layer exposes cells as separate positioned fragments.

## 2. Data model

- `Document`: stable identity for CardioTrack CT-200.
- `DocumentVersion`: immutable version number, source filename and SHA-256 of the original PDF.
- `Node`: version-specific ID, logical key, section number, heading, level, parent ID, body text, node type and content hash.
- `Selection`: named selection pinned to one document version.
- `SelectionItem`: exact node/version pair, source text snapshot and source hash.
- `generated.json`: generation ID, selection ID, version, source node IDs/hashes/text snapshots, structured test cases, validation status and attempt count.

The SQL database is the source of truth for the tree and versioned selection records. A JSON document store is used for generated outputs because the assignment explicitly permits a justified JSON store. Writes use a temporary file followed by atomic replacement. This is not a multi-process transactional store and is a known prototype limitation.

## 3. Parsing and hierarchy reconstruction

The parser extracts line text and font information from each PDF page. A short numbered line with a heading-like font/size is treated as a section heading. Other lines are appended to the active section body, preserving their order and line breaks. The title wraps across several lines and is merged into one document-title node.

Section numbering drives parent reconstruction. For a section like `2.1.1.1`, the parser looks for the nearest existing numbered ancestor. In these files, `2.1.1` is absent, so `2.1.1.1 Battery Life Under Typical Use` is attached to `2.1 General Specifications` and receives a warning. Source order is not sorted into numeric order: `3.4 Auto Shutoff` appears before `3.3 Result Display and Classification`, and this order is preserved and flagged.

The manual contains repeated heading text `Error Codes` in sections 4.2 and 7.1. These are separate logical nodes because their section numbers differ and their parents are different. The tree keeps node IDs distinct rather than merging based on title text.

Tables appear under `2.1 General Specifications` and `4.2 Error Codes`. PyMuPDF's line-based table detector reconstructs these as 8×2 and 6×3 tables in v1 (8×2 and 7×3 in v2). The parser filters smaller detector fragments contained within a larger table, stores the rows as structured JSON, and also appends a Markdown-like representation to the node text so selected source text includes table content. This works for the main tables in these supplied PDFs, but may fail on borderless tables or unusual merged cells; a future improvement would add more layout strategies and validate every cell against page images.

## 4. Structural cases found in the actual PDFs

1. **Skipped numbering level:** `2.1.1.1 Battery Life Under Typical Use` has no visible `2.1.1` parent. It is attached to the nearest existing ancestor and a warning is emitted.
2. **Out-of-order siblings:** `3.4 Auto Shutoff` occurs before `3.3 Result Display and Classification`. The parser preserves source order and emits a warning.
3. **Repeated heading label:** `4.2 Error Codes` and `7.1 Error Codes` share a title but represent different sections and parents.
4. **Tables:** the specifications and error-code tables are reconstructed into row/cell arrays; a filter removes false table fragments contained within the real table.
5. **Title wraps across lines:** the document title spans multiple lines and must not be treated as body text.
6. **v2 additions:** section `5.3 Data Export` and error code E6 are new in v2.

## 5. Version matching strategy

Logical matching uses the explicit section number (`SECTION:2.1.1.1`) rather than body hash or heading text. Therefore, changes to body wording do not create a new logical identity. Each ingestion still creates new version-specific node IDs and keeps the previous version intact. Content hashes are SHA-256 over normalized heading + body text and detect changes. The original PDF also receives a source SHA-256.

This is a pragmatic path-based strategy. It breaks if section numbering changes, if two nodes use the same section number, or if a section is moved and renumbered. Unmatched nodes are reported as unresolved/removed rather than force-matched based on a fuzzy title. A future production matcher could combine normalized section path, heading similarity, parent identity, and manual review for ambiguous matches.

## 6. LLM prompt and structured-output strategy

The prompt instructs the model to use only the selected manual text, return 3–5 concrete test ideas, include a source-section reference, and avoid inventing specifications. Each test case requires title, requirement reference, preconditions, steps, expected result, and risk label. Pydantic validates the response shape and fields. The call uses JSON response mode when supported by Groq. On provider failure, malformed JSON, or schema validation failure, the system retries once with a correction instruction. If the second attempt fails, it returns HTTP 502 and does not persist a successful generation.

The same selection is idempotent by default: if a generation already exists, the API returns it. `force_new=true` explicitly requests a new generation. This avoids accidental duplicate costs and makes retries predictable, while still allowing deliberate regeneration.

## 7. Traceability and staleness

Selections store exact node/version pairs, source text and source hashes. Generation records repeat those snapshots so later ingestion cannot rewrite the provenance of a previous test case. Retrieval compares each saved source against the latest node with the same logical key. Hash equality is `current`; differences are classified as `wording_changed` or `substantive_change_suspected` using a simple similarity threshold; absent matches are `unresolved`.

These labels are triage hints only. The system cannot know whether a changed word is clinically important. In v1→v2, changes such as 300→250 battery cycles, 15%→10% low-battery threshold, 40→30 mmHg inflation increments, E3 2→1.5 seconds, and the addition of E6 are important to review. The exact content diff is exposed, but no automated claim is made that a generated test remains safe or valid. Human review is required.

## 8. What the initial implementation failed to handle and how it improved

The first parser pass used a section-number expression that missed top-level headings written as `1. Device Overview` because the delimiter dot was not accounted for. Inspection of the extracted node list showed the first numbered section being absorbed into title text. The regex was updated to accept the optional delimiter and normalize the section number. A second inspection found the title split across three visual lines, so title continuation lines are now merged into one title node.

The parser initially also had no explicit warning for the 3.4/3.3 ordering anomaly. A source-order check and test were added. Tests are run with `python -m pytest -q` and are backed by both actual PDFs, not only synthetic text.

## 9. Decision log

**1. Most likely silent wrong result:** A parser can attach a paragraph or table row to the wrong section while still producing valid JSON and a clean-looking tree. I catch this with explicit hierarchy assertions, warnings for skipped numbering, a check for source-order anomalies, and manual comparison against the rendered PDF pages. Table reconstruction can still fail for borderless or merged-cell layouts; I added explicit assertions for the two main tables and documented the unsupported layouts.

**2. Simplicity chosen over correctness:** The JSON generation store and section-number identity matcher are simpler than a transactional document database and semantic entity resolution. Production would first break under concurrent generation writes or source renumbering; the JSON store could lose concurrent updates, while renumbered sections could become unresolved or incorrectly appear new.

**3. Unhandled input:** A scanned PDF page whose text layer is empty and whose Tesseract installation is missing or broken cannot be extracted reliably. The parser emits a warning when OCR is unavailable; for a real regulatory workflow, ingestion should fail closed or require manual verification instead of treating the result as complete. Another known boundary is duplicate section numbers, which are not safely disambiguated by the current matcher.

## 10. What I would do with more time

- Add page-level OCR confidence and a visual review report for low-confidence pages.
- Extend table extraction to borderless tables and merged cells, then validate table cells against rendered page images.
- Add migration and rollback tests, API integration tests for all endpoints, and concurrency tests for generation persistence.
- Replace JSON file storage with MongoDB or another transactional document store if multiple workers are used.
- Add reviewable semantic change classification for numerical thresholds and safety-critical modal language, without replacing human approval.
- Add a richer node lifecycle (`added`, `changed`, `unchanged`, `removed`, `unresolved`) to version comparison output.
