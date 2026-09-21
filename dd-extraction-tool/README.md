# DD extraction tool

The due-diligence extraction tool for Calder Bennett Partners, designed in the Gate 3 worksheet.

**Built so far: step 1, financial statement identification.** The tool walks a data room, makes one LLM call per PDF page to decide whether the page is a financial statement, and writes a JSON report of the files that contain one. The later steps in the spec (figure extraction, anomaly checks, human review) are not built yet.

## How step 1 follows the spec

| Gate 3 | Implementation |
| --- | --- |
| Topics 1–2: one bounded LLM call per page, structured yes/no output | `identify.make_claude_classifier`: one `messages.parse` call per page, returning a `PageClassification` |
| Topic 3: fixed criteria in the system prompt, page content retrieved per request | `identify.SYSTEM_PROMPT` and `identify.build_user_content` |
| Topic 3: schema validation is code's job | The response is constrained to `PageClassification` and re-validated in code |
| Topic 4: `confidence`, `source_document`, `source_page` | `schema.IdentifiedPage`. The source fields are set by code from the file walk, never by the model |
| Topic 5: content is untrusted data; read-only data room; scoped output | Page text is wrapped in `<page_content>` and the prompt forbids following instructions in it. The model has no tools. The data room is only read, and the CLI refuses to write inside it |
| Topic 6: small, fast model | `claude-haiku-4-5` by default |

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
- **Different trust boundary.** Pages are sent to Ollama's servers. That's fine for test data, but real deal documents should only go to a provider the client has approved (Gate 3, Topic 5).

## Tests

```bash
.venv/bin/pytest
```

The tests build small PDFs and swap in a fake API client, so they need no key and make no network calls.
