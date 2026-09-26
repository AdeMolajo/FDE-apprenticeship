# DD extraction tool

The due-diligence extraction tool for Calder Bennett Partners, designed in the Gate 3 worksheet.

**Built so far: step 1 (financial statement identification) and step 2 (figure extraction).** The tool walks a data room, makes one LLM call per PDF page to decide whether the page is a financial statement, then makes one Ollama call per identified page to pull a fixed set of target figures off it. The later steps in the spec (anomaly checks against a rubric, and the human risk-weighting gate) are separate steps and are not built yet.

## How step 1 follows the spec

| Gate 3 | Implementation |
| --- | --- |
| Topics 1–2: one bounded LLM call per page, structured yes/no output | `identify.make_claude_classifier`: one `messages.parse` call per page, returning a `PageClassification` |
| Topic 3: fixed criteria in the system prompt, page content retrieved per request | `identify.SYSTEM_PROMPT` and `identify.build_user_content` |
| Topic 3: schema validation is code's job | The response is constrained to `PageClassification` and re-validated in code |
| Topic 4: `confidence`, `source_document`, `source_page` | `schema.IdentifiedPage`. The source fields are set by code from the file walk, never by the model |
| Topic 5: content is untrusted data; read-only data room; scoped output | Page text is wrapped in `<page_content>` and the prompt forbids following instructions in it. The model has no tools. The data room is only read, and the CLI refuses to write inside it |
| Topic 6: small, fast model | `claude-haiku-4-5` by default |

## How step 2 (figure extraction) follows the spec

Step 2 only looks at the pages step 1 already flagged — it does not re-scan the whole data room.

**Revision history:** v1 used one Claude call per page. v2 switched to Ollama and batched every identified page of a document into one call, to cut the number of model calls — but that meant the model had to self-report which page each figure came from, since code alone couldn't tell pages apart once several were in the same prompt. v3 (current) reverted the batching back to one call per page, specifically to restore page-level traceability being entirely code-derived, while keeping Ollama as the provider from v2.

| Gate 3 | Implementation |
| --- | --- |
| Topic 1 table, "Extract specific figures ... Single LLM call, one prompt, one answer" | `extract.make_ollama_extractor`: one Ollama chat call per page, returning a `PageExtraction` |
| Topic 3: fixed target metrics in the system prompt | `extract.DEFAULT_TARGET_METRICS` and `extract.build_system_prompt`; override with `--metrics` |
| Topic 3: "which instance of a figure to extract ... left to model judgment" | The prompt tells the model to pick the current-period figure over a comparative or a component, when a metric appears more than once on a page |
| Topic 4 schema: `metric`, `value`, `currency`, `confidence`, `source_document`, `source_page` | `schema.ExtractedFigure`. As with step 1, `source_document`/`source_page` are set by code from the identification report, never by the model. `currency` must be on the accepted list (`--currencies`), checked in code |
| Topic 5: untrusted content, read-only data room, scoped output | Same `<page_content>` framing and the same read/write separation check in the CLI as step 1 |
| Topic 6: small, fast model | `gpt-oss:20b` via Ollama, the same default as step 1's `--provider ollama` option |

**Scope note:** only the extraction call itself was built here, deliberately kept to the one row of the Topic 1 table it corresponds to. Anomaly flagging (deterministic code, per Topic 1) and the human risk-weighting gate are separate rows and separate build steps, not touched in this change.

## Setup

Needs Python 3.9+ and an Anthropic API key.

```bash
cd dd-extraction-tool
python3 -m venv .venv
.venv/bin/pip install --upgrade pip   # Python 3.9's bundled pip is too old for editable installs
.venv/bin/pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...
```

## Run

```bash
.venv/bin/dd-identify "/path/to/dataroom" --out reports/identification.json
```

Example report:

```json
{
  "dataroom": "/path/to/dataroom",
  "model": "claude-haiku-4-5",
  "files_scanned": 3,
  "pages_classified": 4,
  "financial_statement_files": [
    {
      "source_document": "01 Financials/FY25 accounts.pdf",
      "pages": [
        { "source_document": "01 Financials/FY25 accounts.pdf", "source_page": 2, "confidence": "high" }
      ]
    }
  ],
  "skipped": [
    { "source_document": "02 Commercial/notes.txt", "source_page": null, "reason": "not a PDF" }
  ]
}
```

A file is listed if at least one of its pages is a financial statement. Anything that could not be classified goes in `skipped` with a reason: non-PDF files, unreadable PDFs, and pages where the call failed.

Pages with almost no extractable text (scans) are sent to the model as a single-page PDF instead of as text.

## Run step 2: extract figures

Step 2 uses Ollama (Ollama Cloud by default, needs `OLLAMA_API_KEY`; or a local Ollama server, no key needed):

```bash
read -rs "OLLAMA_API_KEY?Paste your Ollama API key, then press Enter: " && export OLLAMA_API_KEY
.venv/bin/dd-extract "/path/to/dataroom" --identification reports/identification.json --out reports/extraction.json
```

Example report:

```json
{
  "dataroom": "/path/to/dataroom",
  "model": "ollama/gpt-oss:20b",
  "target_metrics": ["revenue", "net_profit", "total_assets", "total_liabilities", "net_assets", "cash_and_equivalents"],
  "currencies": ["GBP", "USD", "EUR"],
  "pages_processed": 2,
  "figures": [
    { "metric": "revenue", "value": 4820500.0, "currency": "GBP", "confidence": "high", "source_document": "01 Financial/FY25 Audited Accounts.pdf", "source_page": 2 },
    { "metric": "total_assets", "value": 6340000.0, "currency": "GBP", "confidence": "high", "source_document": "01 Financial/FY25 Audited Accounts.pdf", "source_page": 3 }
  ],
  "skipped": []
}
```

Only metrics actually stated on a page are reported; the model is told not to guess a figure that isn't there. Use `--metrics` to extract a different set, e.g. `--metrics revenue,net_profit`. Use `--host http://localhost:11434` for a local Ollama server instead of Ollama Cloud (no key needed). A scanned page with no text layer is skipped with a reason, because Ollama reads text only; run OCR on the page first.

**Currencies.** `--currencies` sets the ISO 4217 codes to accept (default `GBP,USD,EUR`), for example `--currencies GBP,EUR,SEK`. The model reports the currency the page actually states, or `NOT_STATED` if the page shows none; it is never forced to pick from the list. A figure in a currency that isn't accepted goes in `skipped` with a reason instead of being relabelled, so add the currency and rerun to include it. The accepted list is recorded in the report as `currencies`.

## Using Ollama instead of Claude

`--provider ollama` sends the same one-call-per-page classification to an Ollama model. By default it uses Ollama Cloud (`https://ollama.com`) with `gpt-oss:20b`.

```bash
read -rs "OLLAMA_API_KEY?Paste your Ollama API key, then press Enter: " && export OLLAMA_API_KEY
.venv/bin/dd-identify sample-dataroom --provider ollama --out reports/identification.json
```

For a local Ollama server, set `OLLAMA_HOST=http://localhost:11434`; no key is needed. Pick another model with `--model`.

Differences from the Claude provider:

- **Output is validated, not constrained.** Ollama Cloud ignores the JSON schema in `format`, so the schema is also stated in the prompt and each answer is parsed and validated in code. Pages whose answer does not parse go in `skipped`.
- **Text only.** Scanned pages with no text layer are not sent; they go in `skipped` with a reason.
- **Rate limits and transient errors are retried.** A 429, a 5xx or a dropped connection is retried up to 3 times with exponential backoff (about 2s, 4s then 8s, or the provider's `Retry-After` if it sends one), and each retry is announced on stderr. A page is only skipped once the retries are exhausted. A rejected key still stops the run immediately, and a 400 is never retried.
- **Different trust boundary.** Pages are sent to Ollama's servers. That's fine for test data, but real deal documents should only go to a provider the client has approved (Gate 3, Topic 5).

## Run log

Every run of either command appends one JSON object to a run log, by default `runs.jsonl` next to the report (`--log` sets another path). Each record holds the input, the output and its counts, a pass/fail check of the output's shape, start and finish timestamps, duration, model calls, retries, failed calls, token counts and cost.

Read it back as a table:

```bash
.venv/bin/python -m dd_extraction.runlog reports/runs.jsonl
```

```
started (UTC)       command     status  shape    secs  calls   tokens      cost  output
---------------------------------------------------------------------------------------
2026-09-23 11:21:36 dd-identify ok      pass     24.3     11     7992    0.0012  log-demo-identification.json
2026-09-23 11:22:02 dd-extract  ok      pass     21.9      5     6767    0.0012  log-demo-extraction.json
---------------------------------------------------------------------------------------
2 runs · 16 model calls · 14,759 tokens · 0 retries · 0 failed calls · cost 0.0024
```

**Shape check.** `pass` means the report validated against its schema and the invariants the schema alone can't express: every figure traceable to a document and a page, every metric one that was requested, every currency one that was accepted, counts consistent, and every skipped item carrying a reason. Any failure is listed in the record under `shape.problems`.

**Cost.** Token counts always come from the provider's own response, so they are measured, not estimated. A money figure appears only when prices are configured, via a JSON file named by `DD_PRICING`:

```bash
export DD_PRICING="$(git rev-parse --show-toplevel)/dd-extraction-tool/pricing.json"
```

`pricing.json` in this folder holds published rates as of 26 September 2026 (`gpt-oss:20b`: $0.07 per million input tokens, $0.30 output). Rates change, so check them before quoting a cost, and note that Ollama's cheaper cached-input rate is not applied because its API does not report cached tokens: logged costs are an upper bound. Without the file, `cost.amount` is `null` with a reason, rather than a made-up number.

**Failures are logged too.** A rejected key, an unwritten report or an unexpected crash still writes a record, with `status: "error"` and the error message, so the log is a complete history of attempts rather than only of successes.

## Tests

```bash
.venv/bin/pytest
```

The tests build small PDFs and swap in a fake API client, so they need no key and make no network calls.
