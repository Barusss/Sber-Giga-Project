# P0 #56: two-call probe

Use backend/scripts/p0_56_deepseek_probe.py with the saved private response and original Koktal PDFs. Default: dry-run via MockTransport. No verify_all or database access.

Selected candidates: TU 3 / ZnP 8 (possible partial substation relationship), TU 3 / ZnP 9 (negative control: transformer apparent power versus station active power). Neither is a proven positive match. TU warnings remain active.

Offline verification: 6 probe tests and 9 auditor tests. The hard transport cap is two HTTP attempts; any network failure, HTTP 402, or invalid JSON stops the run. Do not rerun a paid invocation automatically. Max completion tokens: 1024 per request.

The dry-run estimate uses UTF-8 prompt bytes plus 200 per request as a deliberately conservative token proxy, not the DeepSeek tokenizer or billed usage. Two inputs: 9906 estimated upper tokens; completions: at most 2048; combined approximate upper budget: 11954 tokens. No dollar estimate is asserted.

After separate explicit authorization, from backend/ in an environment with backend dependencies and DEEPSEEK_API_KEY configured:

```powershell
$env:PYTHONPATH = '.'
python scripts/p0_56_deepseek_probe.py --saved-result ../diagnostics/p0-56/original-pair-stub.json --tu-pdf D:/Sber/koktal_tu.pdf --znp-pdf D:/Sber/koktal_znp.pdf --output ../diagnostics/p0-56/deepseek-probe-live.json --private-output ../diagnostics/p0-56/deepseek-probe-live.private.json --live --confirm-paid-two-calls
```

Omit both live flags for dry-run. Private requests and responses stay under ignored diagnostics/. Source ownership is checked against the saved extracted atoms; PDFs are hash-checked, not extracted again. The snapshot does not contain the full original Fragment.text, so this probe does not independently re-prove text offsets.

source_coverage.complete=false (11 unclassified fragments). The synthetic guard rehearsal must remain needs_review. This probe only evaluates links; it does not publish a comparison verdict. A real first run still requires API credentials, provider balance, and explicit user approval. Truncation at 1024 output tokens stops the experiment without retry.
