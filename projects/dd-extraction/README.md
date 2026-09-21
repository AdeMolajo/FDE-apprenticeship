# DD extraction: financial statement identification

Step 1 of the Calder Bennett Partners DD extraction pipeline designed in the Gate 3 worksheet. It walks a data room, classifies every PDF page with a single LLM call, and writes a JSON report of the files (and pages) that contain financial statements.

## How it maps to the Gate 3 spec

| Spec | Where it lives |
| --- | --- |
| Topic 1: single LLM call per page, no multi-step autonomy | `identify.make_claude_classifier`: one `messages.parse` call per page, no tools |
| Topic 3: fixed criteria in the system prompt, page content retrieved per request | `identify.SYSTEM_PROMPT` and `identify.build_user_content` |
| Topic 3: schema validation is code's job | `PageClassification` is the structured-output format and is re-validated in code |
| Topic 4: traceability (`source_document`, `source_page`, `confidence`) | `schema.IdentifiedPage`. Source fields are set by code from the file walk, never by the model |
| Topic 5: extracted content is untrusted data | Page text is wrapped in `<page_content>`, and the system prompt says never to follow instructions in it. With no tools, the worst an injection can do is flip one page's answer |
| Topic 5: read-only data room, scoped output location | `dataroom.py` only reads and never follows symlinks. The CLI refuses an `--out` path inside the data room |
| Topic 6: small, fast model | `claude-haiku-4-5` by default, override with `--model` |

## What counts as a financial statement

The fixed criteria are in `SYSTEM_PROMPT`. A page qualifies if it presents tabulated figures for an income statement, balance sheet, cash flow statement or statement of changes in equity. That covers audited, management and interim accounts. Notes to the accounts, auditor's reports, budgets, forecasts, KPI dashboards and narrative commentary do not count.

## Setup

Needs Python 3.9 or later and an Anthropic API key.

```bash
cd projects/dd-extraction
python3 -m venv .venv
.venv/bin/pip install --upgrade pip   # the pip bundled with Python 3.9 is too old for editable installs
.venv/bin/pip install -e ".[dev]"
export ANTHROPIC_API_KEY=...
```

## Run

```bash
.venv/bin/dd-identify /path/to/dataroom --out reports/identification.json
```

It prints a summary and writes the full report:

```json
{
  "dataroom": "/path/to/dataroom",
  "model": "claude-haiku-4-5",
  "generated_at": "2026-09-21T11:00:00+00:00",
  "files_scanned": 3,
  "pages_classified": 4,
  "financial_statement_files": [
    {
      "source_document": "01 Financials/FY25 accounts.pdf",
      "page_count": 2,
      "pages": [
        {
          "source_document": "01 Financials/FY25 accounts.pdf",
          "source_page": 2,
          "statement_types": ["balance_sheet"],
          "confidence": "high"
        }
      ]
    }
  ],
  "skipped": [
    { "source_document": "02 Commercial/notes.txt", "source_page": null, "reason": "unsupported file type '.txt'" }
  ]
}
```

Anything not classified goes in `skipped` with a reason: non-PDF files, unreadable PDFs, and pages where the API call failed or the model gave no answer. Nothing is dropped silently.

## Behaviour worth knowing

- **PDFs only.** The spec describes a data room of PDFs. Other file types are listed in `skipped`.
- **Scanned pages.** Pages with under 50 characters of extractable text are sent to the model as a single-page PDF, so image-only scans are still classified.
- **Sequential.** Pages are classified one at a time. The SDK retries rate limits and server errors. A large data room makes one call per page, so expect cost and runtime to scale with page count.

## Tests

```bash
.venv/bin/pytest
```

The tests build real PDFs, including a blank "scanned" page, and replace the API with a fake client. They need no key and make no network calls.
