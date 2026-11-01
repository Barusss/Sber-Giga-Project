# Pipeline audit fixes implementation plan

Goal: repair all reproduced audit defects and provide a selectable modern Russian OCR backend.
Architecture: preserve original fragments and evidence; scope comparison records to immutable uploaded document pairs. Offline retrieval produces review candidates, never semantic approval.
Tech stack: FastAPI, SQLAlchemy/Alembic, PyMuPDF, React; optional PaddleOCR PP-OCRv5.
Spec: confirmed defects in the 2026-11-01 pipeline audit and the user's request to fix them.

## Constraints and review focus
- Preserve engineer decisions within their original document pair, including missing matches without a ZnP fragment.
- Do not delete results from other TU documents or other ZnP pairs.
- Reject empty/unindexed extraction and block approval without completed checks.
- Preserve all source text, real PDF coordinates and distinct numbered requirements.
- OCR backend failure must be explicit, with bounded resource use and no silent model substitution.

## Tasks
- [x] Reproduce audit's 14 failures, then fix parser TOC boundaries, extraction deduplication, partial-layer OCR, and empty uploads. Run parser/audit tests.
- [x] Restrict offline verification to needs_review; validate meaningful empty analysis failure. Run comparison tests and update obsolete offline-approval expectations.
- [x] Add pair-scoped verification migration, confirmation identifiers and frontend selection. Test A/B/A pairs, no-match human verdicts and multiple TUs.
- [x] Fix normative roles, normalized registry uniqueness and pending finding reconciliation. Test rechecks and preserved human review.
- [x] Guard project approval with completed analysis/verification and useful document content. Test incomplete and successful workflows.
- [x] Add selectable PaddleOCR PP-OCRv5 adapter, optional Docker dependencies and bounded OCR settings. Test adapter and real scans where model downloads are available.
- [x] Run full backend suite, frontend build, migrations and review diff; publish fixes into dev only after checks pass.
