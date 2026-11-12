# Pipeline correctness validation, 2026-11-01

Base: dev@18488f4. The audit's original 14 defect checks all failed before
implementation. They pass after fixes, alongside additional pair, completion,
legacy registry, migration and OCR-backend checks. Independent review led to
additional fixes for partial manual approval, cross-pair dedup evidence,
serialized pair identity, legacy registry lookup and bounded Paddle startup.

## Real inputs

Both source DOCX files were parsed again. TU: 610 fragments, 85 chunks,
176 clauses. ZnP: 1329 fragments, 206 chunks, 336 clauses. These are observed
counts, not engineer-annotated ground truth.

The local API flow uploaded both DOCX files, analyzed them twice, loaded the
report and attempted premature approval. It returned 176 checks (0 fulfilled,
9 missing, 167 needs_review). Both runs preserved verification IDs and premature
approval was blocked. Live DeepSeek was disabled. This validates storage and
failure behavior, not semantic accuracy.

For OCR comparison, source DOCX text was independently reflowed with ReportLab
(TU 21 pages, ZnP 41), and six selected pages per document were rasterized at
200 dpi. This is a controlled raster test; original Word pagination/table layout
is not preserved. It is not a repeat of the audit's earlier 119-page LibreOffice
run. TU pages: 1,2,3,10,20,21. ZnP pages: 1,2,3,20,40,41.

Real engines: Tesseract with Russian tessdata_best and English distro data;
PaddleOCR 3.3.2 / PaddlePaddle 3.2.2 with official PP-OCRv5 mobile detection and
East Slavic recognition weights. Recognizers were not mocked in this trial.

| Document | Tesseract word multiset coverage | Paddle word multiset coverage |
|---|---:|---:|
| TU | 98.34% | 98.38% |
| ZnP | 96.82% | 97.71% |

Initial time including model startup: TU 19.33s / 52.20s, ZnP 23.09s / 29.76s
(Tesseract / Paddle). A two-thread Paddle TU run took 33.57s with unchanged
coverage. CPU load and startup affect measurements; this is not a production
throughput benchmark. Both engines produced nonempty chunks and clauses.
Distinct clause counts do not establish which engine extracts more requirements.

Word multiset coverage ignores order and is not WER, requirement precision/recall,
numeric-association accuracy or a semantic score. Results do not justify an
unconditional engine switch. Tesseract stays default; Paddle is an explicit option.

## Limits and upgrade

Scanned tables still lack complete cell relationships. A labeled engineering
benchmark, including degraded paper scans, is needed for accuracy claims. Live
DeepSeek was not exercised. PostgreSQL migration/integration is checked separately
by CI; local regressions use SQLite. Large comparisons remain synchronous: a
job queue and total analysis budget remain follow-up work.

Migration 0008 preserves human decisions and scopes legacy rows only where a
source fragment identifies the ZnP. Unscoped history is retained. Re-analysis is
required before approving a draft project. Re-upload old scans if OCR text or
fragment boundaries change. Reindexing refuses to break existing citation IDs.
