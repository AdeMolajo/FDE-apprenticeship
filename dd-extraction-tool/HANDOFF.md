# dd-extraction-tool handoff

State as of 23 September 2026, branch `dev` (four commits ahead of `main`, which has none of the below).

## Architecture as it actually is

Two CLIs over a shared package. `dd-identify` walks a data room read-only (`dataroom.py`), and makes **one LLM call per PDF page** asking a yes/no question. `dd-extract` takes that report and makes **one call per identified page** for a fixed metric list. Ollama (`gpt-oss:20b`) is the provider for both; step 1 also has a Claude path, untested live.

`schema.py` holds the Gate 3 Topic 4 contract as pydantic models. **`source_document` and `source_page` are set by code**, from the file walk and the identification report: no model output can change them, proven by seven scripted attacks. Currencies are checked against a per-run accepted list (`--currencies`); an unaccepted currency is skipped with a reason, never relabelled.

`framing.py` is the Gate 5 fix. Page text that contained a literal `</page_content>` used to close the untrusted-data frame, so an injected "SYSTEM:" instruction read as a real one; extraction obeyed it 5/5 and lost a real figure silently. The module escapes any frame-tag lookalike and restates the real instruction after the page content. A3 went 0/5 → 5/5. Both reports also carry a `review` list: tag lookalikes and zero-figure statement pages are flagged, so suppression can't be silent.

`retrying.py` retries 429/5xx/network errors three times with backoff, honouring `Retry-After`; before it, a rate limit became a skipped page. `runlog.py` appends one JSON record per run: input, output counts, a pass/fail shape check, timestamps, duration, calls, retries, tokens and cost (tokens measured from the provider; money only when `DD_PRICING` is set).

`eval/` holds the 16-case golden dataset and a harness running the real commands: last run **13/16 on every field, 15/16 ignoring confidence**, identification 13/15 pages with zero false positives. A three-agent pilot (analyst, two partners, four sessions) found **15/15 figures correct, three injections resisted, zero wrong figures**. Rollback is `git revert`, verified end to end on an MXN change: behaviour, help text, tests and file bytes all returned to the prior state.

## Known limitations

Confidence is always "high", so it's decoration. `metric` accepts any text, so an injection can smuggle a false source into the label. A 200 reply that isn't JSON still crashes a run. Nothing is cached or deduplicated: the same page under two filenames is processed twice and reported twice, which a naive roll-up would double-count. Documents considered but not flagged leave no record. Runs are sequential (~2s/page identify, ~4s/page extract) and a rerun overwrites the previous report. Measurement is thin: one model, synthetic fixtures, mostly single runs.

## Deliberately out of scope

Anomaly checks and the human risk gate (Topic 1 assigns them to later steps), OCR, non-PDF files, and any agentic loop or tool use (Topic 2 argues against it). No service, scheduler or dashboard exists by design.

## What good looks like next

Bounded concurrency with rate-limit awareness; content-hash deduplication; a complete "considered, not flagged" inventory; timestamped immutable outputs keyed by `run_id`; push alerting on review and skipped counts rather than pull-only logs; metric-name validation; hardened JSON parsing; a call ceiling; and CI running the tests and golden suite on every change.
