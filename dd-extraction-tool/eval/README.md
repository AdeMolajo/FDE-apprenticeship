# Golden dataset evaluation

Runs the Gate 5 golden dataset through the real pipeline (`dd-identify`, then `dd-extract`) and compares the actual output with the expected output field by field, using the Gate 3 Topic 4 schema: `metric`, `value`, `currency`, `confidence`, `source_page`, `source_document`.

## Files

| File | Purpose |
| --- | --- |
| `golden_dataset.json` | The golden dataset, exactly as supplied: 16 cases (6 happy path, 7 edge, 3 adversarial) |
| `golden_resolution.json` | Fills the dataset's gaps against the real code (fixture paths, expected values, metric aliases, error mapping). Every deviation is listed under `decisions` |
| `make_fixtures.py` | Generates the synthetic fixture PDFs into `dataroom/fixtures/`, printing exactly the values in `golden_resolution.json` |
| `run_eval.py` | The harness |

## Run

From `dd-extraction-tool/`, with the Ollama key in the Keychain:

```bash
.venv/bin/python eval/make_fixtures.py
```

```bash
export OLLAMA_API_KEY="$(security find-generic-password -a "$USER" -s ollama-api-key -w)" && .venv/bin/python eval/run_eval.py
```

Each run writes `eval/reports/<timestamp>/` (ignored by git): `report.md`, `results.json`, and every raw command output, so any result can be traced to what the pipeline actually produced.

## How it scores

Two modes, both using the real commands:

- **end_to_end**: `dd-identify` runs on the whole data room, and its report is fed to `dd-extract`. This is the pipeline as a user runs it.
- **extract_given_expected_pages**: `dd-extract` is fed the dataset's expected pages, so an identification miss doesn't hide extraction quality.

Per case:

- **Identify**: the flagged pages for each document must equal the expected pages exactly. For E5 (`error_type: unreadable_document`), the document must appear in `skipped` with an "unreadable PDF" reason.
- **Extract**: each expected figure is matched to the actual figure with the same metric (preferring the same document and page), then every field is compared. `value` must be within 0.5. A missing figure fails every field. Actual figures with no expected match are counted as unexpected extras.
- **Verdict**: PASS needs identification correct, every field of every figure matching, and no extras. A second verdict ignores `confidence`, since that's the model's self-assessment rather than a fact on the page.

Model output varies from run to run, so treat one run as a sample. Rerun to see which results are stable.
