# Data-cleaning exercise: messy sales ledger

A deliberately messy data-room spreadsheet, and a script that cleans and reshapes it while documenting every transformation.

## Files

| Path | What it is |
| --- | --- |
| `raw/Sales Ledger FY24-FY25 (FINAL v3).xlsx` | The messy workbook: inconsistent headers, mixed types in one column, duplicate rows, encoding damage, and dates in three formats |
| `raw/Sales Ledger FY25 export.csv` | A CSV export of the FY25 sheet with genuine byte-level encoding faults (mixed UTF-8 and Windows-1252, a byte-order mark, mixed line endings) |
| `make_messy_spreadsheet.py` | Generates both raw files |
| `clean_sales_ledger.py` | Cleans the workbook, reconciles it against the CSV, and writes the outputs |
| `output/sales_ledger_clean.xlsx` | Sheets: **Clean**, **Issues explained** (issue, transformation, why), **Review flags**, **Removed rows**, **Transformation log** |
| `output/sales_ledger_clean.csv` | The clean table: UTF-8, ISO dates, numeric amounts, `source_ref` back to the original cell |
| `output/CLEANING_LOG.md` | Every step's rule and reason, with each change listed |

## Result

22 clean rows, 6 removed (an embedded header, a blank row, a total row, 3 exact duplicates), and 11 items flagged for review rather than guessed: 5 missing amounts, a missing status, a character lost at source, an ambiguous date, a possible duplicate pair, and a spelling conflict between the workbook and the CSV.

## Rerun

From this folder:

```bash
python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python clean_sales_ledger.py "raw/Sales Ledger FY24-FY25 (FINAL v3).xlsx" "raw/Sales Ledger FY25 export.csv" output
```
