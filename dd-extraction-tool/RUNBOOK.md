# Operations runbook

What to check when something goes wrong. Current as of 23 September 2026.

**Exit codes:** `0` success, `1` the provider rejected the key, `2` a usage or validation error (nothing was spent).

**First stop, always:** the run log. Every run appends one record, by default `reports/runs.jsonl`.

```bash
.venv/bin/python -m dd_extraction.runlog reports/runs.jsonl
```

It gives each run's status, shape pass/fail, duration, model calls, retries, failed calls, tokens and cost, and lists shape problems under the run that had them.

## Symptoms

**`OLLAMA_API_KEY is not set (needed for Ollama Cloud)`** — load the key from the Keychain into the session. Nothing was spent. (`dd-identify --provider anthropic` says `ANTHROPIC_API_KEY is not set` instead.)

**`error: Ollama rejected the key (...). Check OLLAMA_API_KEY.`**, or from `dd-identify`, `error: the ollama API rejected the key (...)` — the key is revoked, rotated or mistyped. The run stops at once by design, rather than skipping every page. Store a fresh key in the Keychain and rerun.

**`--out is a folder; give a file path such as ...`**, `--out is not writable`, `--out must be outside the data room; the data room is read only`, `cannot create the folder for --out`, or `data room folder not found` — all checked before any model call, so nothing was spent. Fix the path and rerun.

**`identification report not found`** or **`identification report is not valid`** — `dd-extract` needs the JSON that `dd-identify` wrote. Check the path, and that the earlier run actually completed.

**`--currencies needs three-letter ISO codes such as GBP,USD`** — one of the codes given is malformed; codes are upper-cased for you, so this means a non-three-letter value.

**The run finished but figures are missing.** Read `skipped` and `review` in the report.
- "unreadable PDF" — corrupt or password-protected file; get a clean copy.
- "scanned page with no text layer" — no text to read; OCR it first.
- "extraction failed: no JSON in response" — a transient model failure. **Rerun that run.** This is a known weakness; a page that fails once usually succeeds next time.
- "currency X is not accepted" — rerun with `--currencies` including X.
- "no target metrics extracted from a page identified as a financial statement" — either the page genuinely lacks those metrics, or something suppressed it. Open the page and check.
- "page text contains a lookalike of the pipeline's `<page_content>` tag" — treat as a possible injection attempt and verify that page's figures by hand.

**A whole page or document is absent from the report.** `dd-identify` lists only pages it flagged, so a document that was read and rejected leaves no per-document record. Compare `files_scanned` and `pages_classified` against the data room to confirm it was read at all.

**The run is slow.** Expect roughly 2s per page for identify and 4s for extract; provider latency has varied between 2 and 6 seconds per page. Compare `duration_seconds` and `model_calls` across runs in the log. Runs are sequential, with no resume: a rerun starts from the beginning.

**Rate limits (429).** Retried three times with backoff, with a line on stderr each time. If pages are still skipped, the provider is throttling harder than that: rerun later. Check `retried_calls` in the log.

**A run crashed mid-way.** The log still records it with `status: "error"` and the message. A non-JSON reply from the provider is a known crash path.

**You suspect a quality regression.** Run `pytest` (expect 103 passing), then the golden suite, and compare against the last baseline of 13/16 cases on every field, with identification at 13 of 15 pages and no false positives.

**You need to undo a change.** `git revert <sha>`, push, pull in the installation, then confirm with the tests and a run. Reports written while the change was live are not rolled back; rerun anything from that window.
