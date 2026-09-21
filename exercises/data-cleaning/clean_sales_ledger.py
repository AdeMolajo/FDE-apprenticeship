"""Clean and reshape the messy Sales Ledger workbook, logging every transformation.

Usage: python clean_sales_ledger.py <ledger.xlsx> <export.csv> <output dir>

Outputs:
  sales_ledger_clean.csv    one tidy table, UTF-8, ISO dates, numeric amounts
  sales_ledger_clean.xlsx   sheets: Clean, Issues explained, Review flags, Removed rows,
                            Transformation log
  CLEANING_LOG.md           the same log, readable

Principles:
  * Every output row keeps its source reference (sheet + row) so it can be traced back.
  * Nothing is guessed. Values that cannot be recovered deterministically become
    blank and are listed under "Review flags" for a person to resolve.
  * Rows are only removed when they are provably not data (headers, blanks, totals)
    or exact duplicates. Possible duplicates are flagged, not removed.
"""

from __future__ import annotations

import csv
import io
import re
import sys
import unicodedata
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

FIELDS = ["invoice_no", "customer", "invoice_date", "amount_gbp", "region", "status"]


# --------------------------------------------------------------------------- log
class Log:
    def __init__(self) -> None:
        self.steps: "OrderedDict[str, dict]" = OrderedDict()
        self.flags: List[dict] = []
        self.removed: List[dict] = []

    def step(self, key: str, title: str, rule: str, why: str) -> None:
        self.steps[key] = {"title": title, "rule": rule, "why": why, "changes": [], "notes": []}

    def change(self, key: str, ref: str, field: str, before, after) -> None:
        self.steps[key]["changes"].append((ref, field, before, after))

    def note(self, key: str, text: str) -> None:
        self.steps[key]["notes"].append(text)

    def flag(self, ref: str, invoice: str, field: str, value, issue: str) -> None:
        self.flags.append({"ref": ref, "invoice_no": invoice, "field": field, "value": value, "issue": issue})

    def remove(self, key: str, ref: str, reason: str, values) -> None:
        self.removed.append({"step": key, "ref": ref, "reason": reason, "values": values})
        self.change(key, ref, "(row)", values, "removed")


def show(v) -> str:
    """repr that makes invisible problems (NBSP, trailing spaces, types) visible."""
    if isinstance(v, str):
        return repr(v)
    if v is None:
        return "blank"
    return f"{v!r} ({type(v).__name__})"


# ----------------------------------------------------------------- 1. read cells
def read_workbook(path: Path, log: Log) -> List[Tuple[str, int, list]]:
    log.step("S01", "Read raw cells with source references",
             "Load every sheet with openpyxl, values only, no type coercion. Tag each row with "
             "'<sheet>!R<row>' and the fiscal year implied by the sheet name.",
             "Keeps the original values for audit and gives every output row a way back to its "
             "source (the same traceability principle as Gate 3, Topic 4).")
    wb = load_workbook(path, data_only=False)
    rows = []
    for ws in wb.worksheets:
        clean_name = ws.title.strip()
        if clean_name != ws.title:
            log.change("S01", ws.title, "(sheet name)", ws.title, clean_name)
        for i, values in enumerate(ws.iter_rows(values_only=True), start=1):
            rows.append((f"{clean_name}!R{i}", i, list(values)))
        log.note("S01", f"Sheet {ws.title!r}: {ws.max_row} rows x {ws.max_column} columns.")
    return rows


# ------------------------------------------------- 2-3. headers and header rows
HEADER_SYNONYMS = {
    "invoice_no": {"invoiceno", "inv", "invoicenumber"},
    "customer": {"customername", "client", "customer"},
    "invoice_date": {"invoicedate", "dateofinvoice", "date"},
    "amount_gbp": {"amountgbp", "amount", "value"},
    "region": {"region", "territory"},
    "status": {"status", "paid"},
}


def header_key(cell) -> Optional[str]:
    if not isinstance(cell, str):
        return None
    k = re.sub(r"[^a-z]", "", cell.lower().replace("£", ""))
    for field, names in HEADER_SYNONYMS.items():
        if k in names:
            return field
    return None


def is_header_row(values: list) -> bool:
    return sum(header_key(v) is not None for v in values) >= 4


def to_records(rows, log: Log) -> List[dict]:
    log.step("S02", "Harmonise column headers",
             "Normalise each header (lower-case, drop non-letters and '£') and map known "
             "synonyms to one canonical schema: " + ", ".join(FIELDS) + ".",
             "The workbook uses three different header sets for the same six columns.")
    log.step("S03", "Remove embedded header rows",
             "A row where at least 4 of 6 cells are recognised header names is a header, "
             "not data. Drop it and use it to (re)map the columns that follow.",
             "Two exports were pasted into one sheet, leaving a second header row mid-table.")
    records, mapping, sheet, seen_variants = [], None, None, set()
    for ref, rownum, values in rows:
        this_sheet = ref.split("!")[0]
        if this_sheet != sheet:
            sheet, mapping = this_sheet, None
        if is_header_row(values):
            mapping = [header_key(v) for v in values]
            variant = tuple(values)
            if variant not in seen_variants:
                seen_variants.add(variant)
                for raw, key in zip(values, mapping):
                    log.change("S02", ref, "(header)", raw, key)
            if rownum != 1:
                log.remove("S03", ref, "embedded header row", values)
            continue
        rec = {"ref": ref, "sheet": sheet}
        for key, v in zip(mapping, values):
            if key:
                rec[key] = v
        rec["amount_raw"] = rec.get("amount_gbp")  # untouched original, for audit
        records.append(rec)
    return records


# ------------------------------------------------------- 4. blank / total rows
def drop_non_data(records: List[dict], log: Log) -> List[dict]:
    log.step("S04", "Remove blank and total rows",
             "Drop rows with no values, and rows whose invoice_no is 'TOTAL'. Totals are "
             "recomputed from the cleaned data instead.",
             "A total row is derived data; keeping it would double count. Its formula is "
             "also unreliable (see notes).")
    kept = []
    for r in records:
        vals = [r.get(f) for f in FIELDS]
        if all(v in (None, "") for v in vals):
            log.remove("S04", r["ref"], "blank row", vals)
        elif isinstance(r.get("invoice_no"), str) and r["invoice_no"].strip().upper() == "TOTAL":
            formula = r.get("amount_gbp")
            log.remove("S04", r["ref"], f"total row ({formula})", vals)
            m = re.search(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", str(formula))
            own_row = int(r["ref"].split("R")[-1])
            if m and int(m.group(2)) <= own_row <= int(m.group(4)):
                log.note("S04", f"The total at {r['ref']} ({formula}) includes its own cell, a "
                                "circular reference, so Excel cannot calculate it. SUM would also "
                                "skip every amount stored as text.")
        else:
            kept.append(r)
    return kept


# --------------------------------------------------------------- 5. mojibake
CP1252_SPECIAL = set("€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ")


def _suspect(ch: str) -> bool:
    return 0x80 <= ord(ch) <= 0xFF or ch in CP1252_SPECIAL


def _to_byte(ch: str) -> Optional[int]:
    try:
        return ch.encode("cp1252")[0]
    except UnicodeEncodeError:
        return ord(ch) if ord(ch) < 256 else None  # C1 controls, e.g. U+0098


def fix_mojibake(text: str) -> str:
    """Re-decode runs of Latin-1/CP1252 characters that are really UTF-8 bytes.

    Works run by run, so a genuine accented letter next to a garbled one is left alone.
    """
    out, i = [], 0
    while i < len(text):
        if not _suspect(text[i]):
            out.append(text[i])
            i += 1
            continue
        j = i
        while j < len(text) and _suspect(text[j]):
            j += 1
        run = text[i:j]
        raw = [_to_byte(c) for c in run]
        fixed = run
        if None not in raw:
            try:
                fixed = bytes(raw).decode("utf-8")
            except UnicodeDecodeError:
                # Try the longest decodable prefix, keep the rest as-is.
                for cut in range(len(raw) - 1, 1, -1):
                    try:
                        fixed = bytes(raw[:cut]).decode("utf-8") + run[cut:]
                        break
                    except UnicodeDecodeError:
                        continue
        out.append(fixed)
        i = j
    return "".join(out)


def repair_encoding(records: List[dict], log: Log) -> None:
    log.step("S05", "Repair mis-decoded text (mojibake)",
             "For each run of Latin-1/CP1252 characters, re-encode to bytes and decode as UTF-8; "
             "keep the result only if it decodes cleanly. Characters already lost (U+FFFD '�') "
             "cannot be recovered and are flagged, not guessed.",
             "Text saved as UTF-8 was read back as Windows-1252, turning 'é' into 'Ã©', '£' into "
             "'Â£' and '–' into 'â€“'.")
    for r in records:
        for f in ("customer", "amount_gbp", "status", "region"):
            v = r.get(f)
            if not isinstance(v, str):
                continue
            fixed = fix_mojibake(v)
            if fixed != v:
                log.change("S05", r["ref"], f, v, fixed)
                r[f] = fixed
            if "�" in r[f]:
                log.flag(r["ref"], r.get("invoice_no"), f, r[f],
                         "character lost in an earlier encoding step (�); needs the original source")


# --------------------------------------------------------- 6. whitespace etc.
def normalise_text(records: List[dict], log: Log) -> None:
    log.step("S06", "Normalise whitespace and punctuation",
             "Unicode NFC; non-breaking spaces to spaces; trim and collapse spaces; curly "
             "apostrophes to straight ones.",
             "Invisible differences ('Greenfield Farms' vs 'Greenfield Farms ') stop matching "
             "and de-duplication from working.")
    for r in records:
        for f in ("invoice_no", "customer", "region", "status"):
            v = r.get(f)
            if not isinstance(v, str):
                continue
            new = unicodedata.normalize("NFC", v).replace(" ", " ").replace("’", "'")
            new = re.sub(r"\s+", " ", new).strip()
            if new != v:
                log.change("S06", r["ref"], f, v, new)
                r[f] = new


# ------------------------------------------------- 7. categorical vocabularies
REGION_MAP = {"north": "North", "south": "South", "london": "London", "eu": "EU",
              "scotland": "Scotland", "wales": "Wales", "ni": "Northern Ireland"}
STATUS_MAP = {"paid": "Paid", "y": "Paid", "yes": "Paid", "1": "Paid", "true": "Paid",
              "outstanding": "Outstanding", "n": "Outstanding", "no": "Outstanding",
              "0": "Outstanding", "false": "Outstanding",
              "paid – partial": "Partially paid", "paid - partial": "Partially paid",
              "credit note": "Credit note", "disputed": "Disputed"}


def standardise_categories(records: List[dict], log: Log) -> None:
    log.step("S07", "Standardise region and status values",
             "Map case/spelling variants to a controlled list. Regions: " +
             ", ".join(sorted(set(REGION_MAP.values()))) + ". Status: " +
             ", ".join(sorted(set(STATUS_MAP.values()))) + ". FY24's 'Paid?' yes/no column "
             "maps to Paid / Outstanding. 'NI' is expanded to Northern Ireland.",
             "The same category appears as 'scotland'/'Scotland', 'paid'/'Paid'/'Y'/1/TRUE.")
    for r in records:
        for f, table in (("region", REGION_MAP), ("status", STATUS_MAP)):
            v = r.get(f)
            if v in (None, ""):
                r[f] = None
                log.flag(r["ref"], r.get("invoice_no"), f, v, f"missing {f}")
                continue
            key = str(v).strip().lower()
            new = table.get(key)
            if new is None:
                log.flag(r["ref"], r.get("invoice_no"), f, v, f"unrecognised {f} value")
                r[f] = None
            elif new != v:
                log.change("S07", r["ref"], f, v, new)
                r[f] = new


# ------------------------------------------------------------- 8. amounts
PLACEHOLDERS = {"n/a", "na", "tbc", "-", "", "none", "null"}


def parse_amount(v) -> Tuple[Optional[float], str]:
    if isinstance(v, bool):
        return None, "boolean in amount column"
    if isinstance(v, (int, float)):
        return float(v), "number"
    if v is None:
        return None, "missing"
    s = str(v).replace(" ", " ").strip()
    if s.lower() in PLACEHOLDERS:
        return None, f"placeholder '{s}'"
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("£", "").replace("GBP", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{2}", s):  # European 2.950,00
        s, kind = s.replace(".", "").replace(",", "."), "European decimal format"
    else:
        s, kind = s.replace(",", ""), "text number"
    try:
        value = float(s)
    except ValueError:
        return None, f"unparseable '{v}'"
    return (-value if negative else value), kind + (" in parentheses (negative)" if negative else "")


def parse_amounts(records: List[dict], log: Log) -> None:
    log.step("S08", "Convert amounts to numbers",
             "Numbers pass through. Text: strip '£'/'GBP' and spaces (including non-breaking), "
             "treat '(x)' as negative, read '1.234,56' as European, drop thousands commas. "
             "Placeholders (N/A, TBC, -), booleans and blanks become empty and are flagged. "
             "The original value is kept in amount_raw.",
             "One column held numbers, text numbers, currency strings, accounting negatives, a "
             "European decimal, placeholders and a TRUE/FALSE value.")
    for r in records:
        raw = r.get("amount_gbp")  # after S05/S06 text repair; the original is in amount_raw
        value, kind = parse_amount(raw)
        r["amount_gbp"] = value
        if value is None:
            log.flag(r["ref"], r.get("invoice_no"), "amount_gbp", raw, f"no usable amount: {kind}")
            log.change("S08", r["ref"], "amount_gbp", raw, "blank (flagged)")
        elif kind != "number" or not isinstance(raw, float):
            log.change("S08", r["ref"], "amount_gbp", raw, f"{value:.2f}  [{kind}]")


# --------------------------------------------------------------- 9. dates
def parse_date(v) -> Tuple[Optional[date], str, Optional[date]]:
    """Return (date, format, alternative reading if ambiguous)."""
    if isinstance(v, datetime):
        return v.date(), "Excel date cell", None
    s = str(v).strip() if v is not None else ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.strptime(s, "%Y-%m-%d").date(), "ISO yyyy-mm-dd text", None
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        uk = date(y, b, a)
        alt = date(y, a, b) if a <= 12 and b <= 12 and a != b else None
        return uk, "dd/mm/yyyy text (UK)", alt
    if re.fullmatch(r"\d{1,2}-[A-Za-z]{3}-\d{2}", s):
        return datetime.strptime(s, "%d-%b-%y").date(), "d-Mon-yy text", None
    return None, f"unparseable '{s}'", None


def parse_dates(records: List[dict], log: Log) -> None:
    log.step("S09", "Convert dates to ISO dates",
             "Excel date cells are used as-is. Text is parsed by exact pattern: yyyy-mm-dd; "
             "dd/mm/yyyy (read as UK, since the ledger is a UK GBP ledger); d-Mon-yy. When a "
             "dd/mm date is also a valid mm/dd date, check both readings against invoice-number "
             "order: if only the UK reading keeps dates in sequence it is confirmed, otherwise "
             "it is flagged.",
             "Three date formats in one column, and dd/mm vs mm/dd cannot be told apart by "
             "pattern alone.")
    formats: Dict[str, int] = {}
    for r in records:
        raw = r.get("invoice_date")
        d, fmt, alt = parse_date(raw)
        formats[fmt] = formats.get(fmt, 0) + 1
        r["invoice_date"], r["_date_alt"], r["_date_raw"] = d, alt, raw
        if d is None:
            log.flag(r["ref"], r.get("invoice_no"), "invoice_date", raw, f"date {fmt}")
        log.change("S09", r["ref"], "invoice_date", raw, f"{d.isoformat() if d else 'blank'}  [{fmt}]")
    log.note("S09", "Formats found: " + "; ".join(f"{k}: {n}" for k, n in formats.items()) + ".")

    for sheet in sorted({r["sheet"] for r in records}):
        seq = sorted((r for r in records if r["sheet"] == sheet and r["invoice_date"]),
                     key=lambda r: r["invoice_no"])
        for i, r in enumerate(seq):
            if not r["_date_alt"]:
                continue
            prev = seq[i - 1]["invoice_date"] if i else date.min
            nxt = seq[i + 1]["invoice_date"] if i + 1 < len(seq) else date.max
            uk_ok = prev <= r["invoice_date"] <= nxt
            us_ok = prev <= r["_date_alt"] <= nxt
            verdict = (f"UK reading {r['invoice_date']} confirmed; US reading {r['_date_alt']} "
                       "would break invoice order") if uk_ok and not us_ok else None
            if verdict:
                log.note("S09", f"{r['ref']} {r['invoice_no']} '{r['_date_raw']}': {verdict}.")
            else:
                log.flag(r["ref"], r["invoice_no"], "invoice_date", r["_date_raw"],
                         f"ambiguous: UK {r['invoice_date']} and US {r['_date_alt']} both fit "
                         "invoice order; UK used")


# ---------------------------------------------------------- 10. duplicates
def deduplicate(records: List[dict], log: Log) -> List[dict]:
    log.step("S10", "Remove exact duplicates; flag possible duplicates",
             "After cleaning, rows identical on all six fields are exact duplicates: keep the "
             "first, remove the rest. Rows with different invoice numbers but the same customer, "
             "date and amount are flagged, not removed.",
             "Two invoices can legitimately share a customer, date and amount. Removing one "
             "is a judgement call for a person, not a rule.")
    seen: Dict[tuple, str] = {}
    kept = []
    for r in records:
        key = tuple(r.get(f) for f in FIELDS)
        if key in seen:
            log.remove("S10", r["ref"], f"exact duplicate of {seen[key]}", list(key))
            continue
        seen[key] = r["ref"]
        kept.append(r)
    by_content: Dict[tuple, List[dict]] = {}
    for r in kept:
        by_content.setdefault((r["customer"], r["invoice_date"], r["amount_gbp"]), []).append(r)
    for group in by_content.values():
        if len(group) > 1:
            names = ", ".join(f"{g['invoice_no']} ({g['ref']})" for g in group)
            for g in group:
                log.flag(g["ref"], g["invoice_no"], "(row)", names,
                         "possible duplicate: same customer, date and amount, different invoice no")
    return kept


# ---------------------------------------------------- 11. reshape and output
def reshape(records: List[dict], log: Log) -> List[dict]:
    log.step("S11", "Reshape into one tidy table",
             "Stack FY24 and FY25 into a single table with a fiscal_year column, one row per "
             "invoice, columns: fiscal_year, " + ", ".join(FIELDS) + ", amount_raw, source_ref. "
             "Sort by invoice number.",
             "Two sheets with the same data model belong in one table for analysis; the "
             "fiscal year moves from the sheet name into the data.")
    out = []
    for r in sorted(records, key=lambda r: r["invoice_no"]):
        fy = re.search(r"fy\s*(\d{2})", r["sheet"], re.I)
        out.append({"fiscal_year": f"FY{fy.group(1)}" if fy else None,
                    **{f: r.get(f) for f in FIELDS},
                    "amount_raw": r["amount_raw"], "source_ref": r["ref"]})
    log.note("S11", f"{len(out)} rows in the clean table.")
    return out


def clean(records_in, log: Log) -> List[dict]:
    records = to_records(records_in, log)
    records = drop_non_data(records, log)
    repair_encoding(records, log)
    normalise_text(records, log)
    standardise_categories(records, log)
    parse_amounts(records, log)
    parse_dates(records, log)
    records = deduplicate(records, log)
    return reshape(records, log)


# ---------------------------------------------------- 12. CSV reconciliation
def read_csv_export(path: Path, log: Log):
    log.step("S12", "Decode the CSV export line by line and reconcile it with the workbook",
             "Strip the byte-order mark, split on newlines, drop '\\r', decode each line as "
             "UTF-8 and fall back to Windows-1252 for lines that are not valid UTF-8. Clean it "
             "with the same steps as the workbook, then compare invoice by invoice.",
             "The file mixes encodings, so no single decoding reads every line. Reconciling "
             "two versions of the same data is a check on both.")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        log.note("S12", "Removed UTF-8 byte-order mark.")
    crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
    log.note("S12", f"Line endings: {crlf} CRLF, {lf - crlf} LF. Normalised to LF.")
    lines = []
    for n, line in enumerate(raw.split(b"\n"), start=1):
        line = line.rstrip(b"\r")
        if not line:
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            text = line.decode("cp1252")
            log.change("S12", f"CSV line {n}", "(encoding)", "invalid UTF-8", "decoded as Windows-1252")
        lines.append(text)
    rows = [(f"Sales FY25 (CSV)!R{i}", i, row) for i, row in enumerate(csv.reader(lines), start=1)]
    return rows


def reconcile(xlsx_rows: List[dict], csv_rows: List[dict], log: Log) -> None:
    a = {r["invoice_no"]: r for r in xlsx_rows if r["fiscal_year"] == "FY25"}
    b = {r["invoice_no"]: r for r in csv_rows}
    agree = 0
    for inv in sorted(set(a) | set(b)):
        if inv not in a or inv not in b:
            log.note("S12", f"{inv} only in {'CSV' if inv in b else 'workbook'}.")
            continue
        diffs = [f for f in FIELDS if a[inv][f] != b[inv][f]]
        if not diffs:
            agree += 1
        for f in diffs:
            log.note("S12", f"{inv} {f}: workbook {a[inv][f]!r} vs CSV {b[inv][f]!r}.")
            log.flag(a[inv]["source_ref"], inv, f, a[inv][f],
                     f"workbook and CSV export disagree (CSV has {b[inv][f]!r})")
    log.note("S12", f"{agree} of {len(a)} FY25 invoices match the CSV on all six fields.")


# ------------------------------------------------ issues explained (per issue)
# (#, issue found, transformation applied, step, why it was necessary)
ISSUES = [
    (1, "Three different header sets for the same six columns: 'Invoice No' / 'Inv #' / 'InvoiceNumber', 'Amount (GBP)' / 'Amount' / 'Value £', 'Region' / 'Territory', 'Status' / 'Paid?'.",
     "Mapped every variant to one set of column names: invoice_no, customer, invoice_date, amount_gbp, region, status.", "S02",
     "Code looking for 'Amount (GBP)' would not find FY24's 'Value £', so a whole year's amounts would be missing without any error. The two years could not be combined at all."),
    (2, "A second header row in the middle of the data (Sales FY25!R13), where two exports were pasted together.",
     "Removed it and used it to re-map the columns below it.", "S03",
     "Otherwise it is read as an invoice numbered 'Inv #' with amount 'Amount'. If the second export's columns had been in a different order, every row below it would have gone into the wrong columns."),
    (3, "Trailing spaces in names: header 'Customer Name ', sheet 'sales fy24 '.",
     "Trimmed.", "S01, S02",
     "The spaces are invisible, but a search for 'Customer Name' does not match 'Customer Name ', so the column or sheet 'doesn't exist'."),
    (4, "Blank row (Sales FY25!R21).",
     "Removed.", "S04",
     "It would count as an invoice with no data, add one to the invoice count, and raise false 'missing value' alerts."),
    (5, "A TOTAL row inside the data, with =SUM(D2:D24) (Sales FY25!R22).",
     "Removed; totals recalculated from the cleaned rows.", "S04",
     "Summing the column would count every invoice twice. The formula was wrong anyway: its range includes its own cell, so Excel cannot calculate it, and SUM skips amounts stored as text."),
    (6, "Garbled customer names: 'SociÃ©tÃ© GÃ©nÃ©rale', 'MÃ¼ller', 'Ã˜rsted', 'ZÃ¼rich'. UTF-8 text was misread as Windows-1252.",
     "Re-read the garbled characters with the correct encoding.", "S05",
     "The same customer appears under two spellings across sources, so customer totals split in two, and matching against any other list (a customer master, a sanctions screen) fails."),
    (7, "A garbled pound sign in amounts: 'Â£3,400', 'Â£ 950.00'.",
     "Same encoding repair, before the amounts were converted.", "S05",
     "The stray 'Â' stops the text being read as a number, so the amount would be lost."),
    (8, "A garbled dash in a status: 'Paid â€“ partial'.",
     "Same encoding repair.", "S05",
     "It would not match any known status, so a partial payment would go unrecognised."),
    (9, "A character destroyed at source: 'Caf� Nero Holdings'.",
     "Flagged, not fixed.", "S05",
     "The original letter is gone, and the '�' symbol only says something was there. 'Café' is a likely guess, but writing a guess into financial records would undermine the audit trail. It needs the original document."),
    (10, "A non-breaking space inside a number: '2 100.50' (INV-2507).",
     "Converted to an ordinary space, then removed.", "S06, S08",
     "It looks exactly like a normal space, so nobody would spot it, but number parsing fails or reads only '2'."),
    (11, "A trailing space and a case difference hiding a match: 'Greenfield Farms ' vs 'Greenfield Farms', 'paid' vs 'Paid'.",
     "Trimmed spaces, standardised case.", "S06, S07",
     "Otherwise the software sees two different customers and two statuses, so grouping and duplicate detection both fail."),
    (12, "A curly apostrophe: 'O’Connor & Sons'.",
     "Replaced with a straight apostrophe.", "S06",
     "A search typed on a keyboard (O'Connor) would not find it."),
    (13, "Inconsistent region values: 'scotland' vs 'Scotland', the abbreviation 'NI'.",
     "Mapped to a fixed list, with NI -> Northern Ireland.", "S07",
     "A revenue-by-region report would show Scotland as two separate lines. The NI expansion is an assumption and is recorded as one."),
    (14, "Status recorded two different ways: FY25 uses words ('Paid', 'Outstanding'); FY24 uses a yes/no column 'Paid?' with Y, yes, 1, TRUE, N, No.",
     "Mapped both to one list: Paid, Partially paid, Outstanding, Credit note, Disputed.", "S07",
     "Without this you cannot count paid invoices across both years. 'No' is taken to mean Outstanding, an assumption that is written down."),
    (15, "Numbers stored as text: '1,250.00', '6,780.00', '15000', mixed in with real numbers.",
     "Converted to numbers, keeping the original in amount_raw.", "S08",
     "Excel's SUM and most tools skip text, so the totals would be too low without any warning. Text numbers also sort alphabetically, putting '15000' before '870'."),
    (16, "Currency symbols in amounts: '£4,050', '£ 950.00'.",
     "Removed '£' / 'GBP' before converting.", "S08",
     "A value with a symbol cannot be read as a number."),
    (17, "Negative amounts in accounting brackets: '(500.00)' on credit note INV-2505.",
     "Read (x) as -x.", "S08",
     "A naive read gives +500, so a credit note would increase revenue instead of reducing it, a swing of £1,000."),
    (18, "A European number format: '2.950,00' (INV-2402), where the dot separates thousands and the comma is the decimal point.",
     "Recognised the pattern and converted it to 2950.00.", "S08",
     "UK-style parsing reads it as 2.95, understating the invoice by £2,947.05."),
    (19, "Placeholder text in place of amounts: 'N/A', 'TBC', '-', 'n/a'.",
     "Left blank and flagged.", "S08",
     "These are not zero. 'TBC' means unknown. Treating it as £0 would understate revenue and hide the fact that the numbers are incomplete."),
    (20, "TRUE in the amount column (INV-2516).",
     "Left blank and flagged.", "S08",
     "A true/false value has no meaning as money, and Excel treats TRUE as 1 in some calculations."),
    (21, "Dates in three formats in one column: ISO text '2025-01-14', UK text '14/02/2025', and real Excel date cells.",
     "Read each one by its exact format and converted all to ISO dates.", "S09",
     "Text dates do not sort or filter by time ('14/02/2025' sorts before '2025-01-14'), and you cannot do date arithmetic such as days outstanding or filtering by quarter."),
    (22, "Dates that can be read two ways: '03/04/2025' is 3 April in the UK but 4 March in the US.",
     "Read as UK, then checked against invoice-number order: 5 confirmed, 1 (INV-2405 '07/08/2024') flagged.", "S09",
     "The wrong reading moves an invoice into a different month or quarter. That misstates revenue for the period, which is exactly what due diligence checks (cut-off testing). The sequence check turns an assumption into evidence."),
    (23, "Exact duplicate rows: INV-2507, INV-2514 and INV-2404 each appear twice.",
     "Kept the first copy, removed the second.", "S10",
     "Revenue was overstated by £21,150.50 (2,100.50 + 15,000 + 4,050)."),
    (24, "A possible duplicate: INV-2509 and INV-2510 have the same customer, date and amount (£4,300) but different invoice numbers.",
     "Flagged, not removed.", "S10",
     "It could be a data-entry duplicate, or two genuine orders. Deleting it wrongly understates revenue by £4,300; keeping it wrongly overstates it. That is a judgement call for a person, not a rule."),
    (25, "Each year in a separate sheet with its own layout, and the year only in the sheet name.",
     "Stacked into one table with a fiscal_year column.", "S11",
     "Comparing years needs one consistent table. Once the sheets are combined, a year that exists only in a sheet name is lost."),
    (26, "The CSV mixes encodings and line endings: a byte-order mark at the start, 3 lines in Windows-1252, the rest UTF-8, both Windows and Unix line endings.",
     "Decoded line by line, falling back to Windows-1252 only for the lines that need it.", "S12",
     "A strict UTF-8 read crashes at the first 'é'. A lenient read silently turns it into '�', the same damage as issue 9, but done by us."),
    (27, "The workbook and the CSV disagree: 'Smërgåsbord' vs 'Smörgåsbord' (INV-2516).",
     "Flagged.", "S12",
     "One of the two sources is wrong. The CSV spelling is probably right, but that has to be confirmed; you cannot simply trust whichever file you happened to read."),
]


def write_issues_sheet(wb) -> None:
    from openpyxl.styles import Alignment

    ws = wb.create_sheet("Issues explained", 1)
    ws.append(["#", "Issue found in the raw data", "Transformation applied", "Step", "Why it was necessary"])
    for n, issue, fix, step, why in ISSUES:
        ws.append([n, issue, fix, step, why])
    ws.append([])
    ws.append(["", "Across all steps: every output row keeps source_ref (its original sheet and row) and "
                   "amount_raw (the untouched original amount), so each cleaned value can be checked "
                   "against the raw data."])
    for col, width in zip("ABCDE", (5, 55, 45, 10, 65)):
        ws.column_dimensions[col].width = width
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = wrap
    ws.freeze_panes = "B2"


# ---------------------------------------------------------------- writing
def write_outputs(rows: List[dict], log: Log, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cols = ["fiscal_year", *FIELDS, "amount_raw", "source_ref"]

    with open(out_dir / "sales_ledger_clean.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "invoice_date": r["invoice_date"].isoformat() if r["invoice_date"] else ""})

    wb = Workbook()
    ws = wb.active
    ws.title = "Clean"
    ws.append(cols)
    for r in rows:
        ws.append([r[c] if c != "amount_raw" else (None if r[c] is None else str(r[c])) for c in cols])
    for cell in ws["D"][1:]:
        cell.number_format = "yyyy-mm-dd"
    for cell in ws["E"][1:]:
        cell.number_format = "#,##0.00"

    write_issues_sheet(wb)

    ws = wb.create_sheet("Review flags")
    ws.append(["source_ref", "invoice_no", "field", "value", "issue"])
    for f in log.flags:
        ws.append([f["ref"], f["invoice_no"], f["field"], str(f["value"]), f["issue"]])

    ws = wb.create_sheet("Removed rows")
    ws.append(["step", "source_ref", "reason", "values"])
    for rm in log.removed:
        ws.append([rm["step"], rm["ref"], rm["reason"], str(rm["values"])])

    ws = wb.create_sheet("Transformation log")
    ws.append(["step", "source_ref", "field", "before", "after"])
    for key, s in log.steps.items():
        ws.append([f"{key} {s['title']}"])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        for ref, field, before, after in s["changes"]:
            ws.append([key, ref, field, show(before), str(after)])
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True)
    wb.save(out_dir / "sales_ledger_clean.xlsx")

    md = ["# Sales ledger cleaning log", "",
          "Generated by `clean_sales_ledger.py`. Every count and example below comes from the run.", ""]
    for key, s in log.steps.items():
        md += [f"## {key}. {s['title']}", "", f"**Rule:** {s['rule']}", "", f"**Why:** {s['why']}", ""]
        for n in s["notes"]:
            md.append(f"- {n}")
        if s["notes"]:
            md.append("")
        if s["changes"]:
            md += [f"{len(s['changes'])} change(s):", "", "| Source | Field | Before | After |",
                   "| --- | --- | --- | --- |"]
            for ref, field, before, after in s["changes"]:
                cell = lambda x: str(x).replace("|", "\\|")
                md.append(f"| {ref} | {field} | `{cell(show(before))}` | {cell(after)} |")
            md.append("")
    md += ["## Open items for review", "",
           f"{len(log.flags)} flag(s). These were not guessed; each needs a person or the original source.", "",
           "| Source | Invoice | Field | Value | Issue |", "| --- | --- | --- | --- | --- |"]
    for f in log.flags:
        md.append(f"| {f['ref']} | {f['invoice_no']} | {f['field']} | `{f['value']}` | {f['issue']} |")
    (out_dir / "CLEANING_LOG.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main(xlsx: str, csv_path: str, out: str) -> None:
    log = Log()
    rows = clean(read_workbook(Path(xlsx), log), log)

    csv_log = Log()  # the CSV is cleaned with the same steps, but only S12 is reported
    csv_rows_raw = read_csv_export(Path(csv_path), log)
    csv_rows = clean(csv_rows_raw, csv_log)
    reconcile(rows, csv_rows, log)

    write_outputs(rows, log, Path(out))
    total = sum(r["amount_gbp"] for r in rows if r["amount_gbp"] is not None)
    print(f"{len(rows)} clean rows, {len(log.removed)} removed, {len(log.flags)} flagged. "
          f"Total of known amounts: {total:,.2f}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
