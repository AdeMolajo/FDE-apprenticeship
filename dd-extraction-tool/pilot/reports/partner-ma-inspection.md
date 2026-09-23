# Partner Inspection — M&A & Transaction Advisory

Scope: consistency review of `session-1-identification.json`, `session-1-extraction.json`,
`session-2-identification.json`, `session-2-extraction.json`, and the two analyst write-ups.
I did not rerun `dd-identify`/`dd-extract`. Where a figure mattered, I opened the underlying page
text using the `load_pages` snippet from the protocol (read-only) to check it against what the
JSON and the write-ups say. Findings are ordered by how much they would matter if these reports
were the basis for a live deal review.

---

## 1. The session-2 extraction JSON — the actual work product — is missing figures the write-up claims are there (HIGH)

`pilot/reports/session-2-extraction.json` as it exists on disk reports:

```json
"pages_processed": 7,
"skipped": [
  { "source_document": "h1_standard_revenue.pdf", "source_page": 3,
    "reason": "extraction failed: no JSON in response: ''" }
]
```

There is **no `h1_standard_revenue.pdf` entry anywhere in `figures`**. Brightwater Holdings'
revenue and net profit are simply absent from the deliverable.

`analyst-session-2.md`, however, describes running `dd-extract` a second time ("no changes") and
states that the rerun "processed all 8 pages cleanly," presenting a results table with
`h1_standard_revenue.pdf p3: revenue = 4,820,500 GBP` and `net_profit = 590,400 GBP` as part of a
headline "10/10 extracted values are numerically correct" verdict.

I checked the raw page text directly — those two values are indeed exactly what page 3 of
`h1_standard_revenue.pdf` states, so the *numbers quoted in the write-up are correct*. But:

- The JSON file that a deal team would actually load into a model — the thing this whole pipeline
  exists to produce — does not contain them.
- There is no second extraction JSON anywhere in `pilot/reports/` (only one
  `session-2-extraction.json` exists, timestamped once) and no rerun artifact in `/tmp` or
  elsewhere that I could find. The "clean rerun" that the write-up's numbers come from cannot be
  traced to any surviving machine-readable file.
- Both `--out` commands in the analyst's own transcript write to the same path
  (`pilot/reports/session-2-extraction.json`), so the second run should have overwritten the
  first. Instead, the file on disk matches the *first* (failed) run. Either the second run wasn't
  actually saved to the reports directory, or something else went wrong between running it and
  writing this file — either way, the reports directory and the narrative disagree about what the
  pipeline actually produced.

**Why this matters commercially:** if a partner opens the JSON — which is the whole point of a
structured extraction tool, as opposed to reading prose — they get an incomplete picture (one
document's revenue and profit silently missing, recorded only as a terse skip reason) while the
human-readable report creates confidence that "10/10" figures are present and verified. A
reviewer who trusts the write-up without independently opening the JSON, or who diffs the two,
will not resolve which one is the record of truth. This is a process/reporting-integrity failure,
not a wrong-figure failure — the tool never asserted the wrong number, but the *reporting chain*
now contains two authoritative-looking documents that don't agree on what was actually delivered.

---

## 2. Duplicate page produces two "independent" revenue figures for the same underlying page, with nothing to flag the duplication (HIGH)

`e7_duplicate_a.pdf` and `e7_duplicate_b.pdf` are the same page (I confirmed the text is
character-for-character identical: Kittiwake Foods Ltd, revenue £2,640,000, FY2025) filed under
two different names. Both `session-2-extraction.json` entries are correct individually:

```json
{ "metric": "revenue", "value": 2640000.0, "currency": "GBP", "source_document": "e7_duplicate_a.pdf", "source_page": 1 },
{ "metric": "revenue", "value": 2640000.0, "currency": "GBP", "source_document": "e7_duplicate_b.pdf", "source_page": 1 }
```

This matches the documented pass condition (each instance traces correctly to its own
`source_document`/`source_page`, not merged) and the analyst's write-up describes this accurately.
That is the right behaviour for traceability. But nothing in the JSON — no page-hash, no
cross-reference, no note in `review` — signals that these two entries are the *same page uploaded
twice*. A deal team that sums `figures[]` by metric to build a revenue bridge (the natural thing
to do with a flat figures array) will silently double-count £2,640,000 for a single company
appearing once in the data room, unless a human happens to notice the near-identical filenames or
opens both PDFs. In a data room with hundreds of files and less obviously twinned names than
`_a`/`_b`, this would not be obvious at all. The write-up notes the behaviour is "correct" without
naming this downstream double-counting risk.

---

## 3. Retained earnings do not roll forward in the audited accounts, and neither the tool nor the write-up catches it (MEDIUM-HIGH)

`01 Financial/FY2025 Audited Accounts.pdf`, page 3 (balance sheet):

```
Net assets                                        3,230,000     2,330,000
Share capital                                       100,000       100,000
Retained earnings                                 3,130,000     2,230,000
```

Page 2 (income statement): profit for the year (2025) = £859,000.

Retained earnings should roll forward as: opening (2,230,000) + profit for the year (859,000) =
3,089,000. The balance sheet states closing retained earnings of **3,130,000** — a **£41,000**
unexplained gap. (I checked the balance sheet itself balances correctly on both a horizontal and
vertical basis — assets less liabilities equals net assets equals share capital plus retained
earnings, in both years — so this is specifically a year-on-year roll-forward inconsistency, not
a sum error within either year.) Nothing in the six-document data room discloses a dividend,
share issue, or prior-year adjustment that would explain the difference.

Every individual figure the tool extracted from this page (`net_assets`,
`cash_and_equivalents`) is numerically correct against the page, and the analyst's "all seven
values are numerically correct" verdict is true taken metric-by-metric. But neither
`dd-extract` nor the write-up checks consistency *across* the figures it pulls from different
pages of the same statement — there is no cross-statement reconciliation step. This is exactly
the kind of discrepancy a partner would raise with management in a real deal (undisclosed equity
movement, restatement, or a data-quality issue in the target's own accounts), and it would not
surface from either report as it stands; you only find it by manually rolling the numbers forward
yourself, as I did here.

*Caveat:* this is a fixture data room for a pilot, so this could be an artefact of how the test
document was generated rather than a genuine target-company issue — I can't determine that from
the reports alone. What I can say is that neither report flags it, and a partner relying solely on
the JSON/write-up would have no way to know to ask.

---

## 4. Budget FY2027.pdf is invisible in the session-1 identification output (MEDIUM)

Confirmed by reading the page directly:

```
Northwind Components Ltd
Budget FY2027 (board approved forecast)
Revenue          10,200,000 (2027 budget)   9,300,000 (2026 forecast)
EBITDA            1,850,000                  1,610,000
```

This file does not appear in `financial_statement_files`, `skipped`, or `review` in
`session-1-identification.json`. It is one of six PDFs scanned (`files_scanned: 6`), and it
contains genuine forward-looking revenue/EBITDA figures that would matter to a buyer, but the
report gives no trace that it was ever looked at. I agree with the analyst's own characterisation
of this as a real gap, not a presentational nuance — a reader cannot distinguish "considered and
excluded because it's a budget, not a statement" from "never scanned." The other three unflagged
PDFs (Investor Presentation, Shareholders' Agreement, Supplier Letter) are correctly excluded on
the merits — confirmed by reading each — but suffer the same silent-omission problem, just with
lower stakes since none of them contains a number a diligence team would rely on in isolation.

---

## 5. Confidence field doesn't track translation risk (MEDIUM, presentational)

`e6_german_statement.pdf` — revenue (Umsatzerlöse 4,820,000) and net profit (Jahresüberschuss
512,000), both translated from German — are reported at **`"confidence": "high"`** in
`session-2-extraction.json`. I confirmed the values are correct (German thousands-separator
"4.820.000" parsed correctly as 4,820,000, correctly tagged EUR). The eval fixture this case is
drawn from (`eval/golden_dataset.json`, case E6) explicitly expects **medium** confidence
specifically because of the translation/non-English risk, noting "acceptable: correctly
extracted... not acceptable: garbled figures" as the pass bar, and treating "medium" as the
appropriate self-assessment for this case type. Getting the value right doesn't mean the
confidence label is calibrated: a reviewer using `confidence` to decide what to double-check by
hand would not be prompted to re-verify a translated figure that happens to be correct this time.
Not a wrong figure — a mis-calibrated confidence signal.

---

## 6. `NOT_STATED` currency looks like a data problem but isn't (LOW-MEDIUM, presentational — flagged well by the analyst)

`session-1-extraction.json`: all three Management Accounts H1 2026 figures (revenue 4,610,000,
net_profit 495,000, net_assets 3,590,000) carry `"currency": "NOT_STATED"`. I confirmed the
source page genuinely never prints a currency symbol or code, unlike the audited accounts, which
literally head their columns "2025 GBP". The conservative choice not to infer GBP from context is
defensible. But presented cold in a JSON next to the audited accounts' explicit GBP figures for
the same company, three `NOT_STATED` values read as a gap or an error to anyone who hasn't read
the underlying page. I agree with the analyst's framing of this as a real but low-severity
communication problem, not a factual one.

---

## 7. No entity/company field — session 2's flat schema would misattribute figures if reused on a real multi-entity data room (LOW)

Session 2's eleven documents are eight *unrelated* companies (Brightwater Holdings, Meridian
Travel Group, Wren Retail Group, Müller & Söhne, Kittiwake Foods (twice), Plover Hospitality,
Turnstone Health) bundled into one flat `figures[]` array, distinguishable only by generic,
non-descriptive filenames (`h4_dual_currency.pdf`, `e6_german_statement.pdf`, etc.) that give no
hint of the company name or which entity a figure belongs to without opening the PDF. This is a
deliberate and reasonable design for an eval fixture set, and I'm not treating it as an error in
this pilot's output. But if this report format were used unchanged for a real target group with
several subsidiaries, a reader working from the JSON alone — without a company/entity field —
could easily attribute one subsidiary's revenue to another, or to the parent. Worth raising as a
schema gap before this is used on a live multi-entity deal, not as a finding against this pilot's
figures, all of which I could correctly trace back to the right company by opening the source PDF.

---

## 8. Conflicting-figures page reports one clean number where the source page flags two (LOW — matches analyst's own finding; concur)

`e3_conflicting_figures.pdf`, page 6, states reported revenue of £3,870,000 *and*, in the same
paragraph, "like-for-like revenue growth was 8% for the year, excluding the twelve stores opened
since January 2024" — i.e., the source document itself flags that reported growth and
like-for-like growth diverge. `session-2-extraction.json` reports only the reported revenue
figure, at high confidence, with nothing to suggest a second, different growth narrative exists on
the same page. This is correct given the tool's default `--metrics` list (which doesn't include
a like-for-like growth metric) — not a bug — but it means the JSON, taken at face value, presents
this page as unambiguous when the underlying document is not. This is exactly the scenario the
eval's own golden dataset (case E3) was designed to probe. A reader who doesn't already know to
ask for the growth metric by name would never learn from the report that the page contains a
second, conflicting figure.

---

## Items checked and found sound (for completeness)

- Session 1 balance sheet (page 3) balances internally in both years; all four extracted figures
  from `FY2025 Audited Accounts.pdf` and all three from `Management Accounts H1 2026.pdf` matched
  the source page text exactly.
- Session 1's cash-flow page (page 4) correctly produced zero figures and was correctly routed to
  `review` rather than silently dropped or fabricated.
- Both adversarial documents in session 2 (`a1_prompt_injection.pdf`, `a3_tag_breakout.pdf`) were
  handled correctly — the embedded instructions did not change the extracted figure or the
  reported source page, and `a3`'s fake tag sequence was proactively surfaced in `review`. I
  reproduced the page text and confirm the analyst's description of both injection attempts is
  accurate.
- `h4_dual_currency.pdf` correctly selected the GBP statutory figure (£6,250,000) over the
  US$7,940,000 constant-currency footnote on the same page.
- `h3_cap_table.pdf` correctly produced no flagged pages despite containing plenty of numbers
  (share counts, percentages) that could plausibly confuse a naive classifier.
- The two analyst write-ups match their respective JSON files for **session 1** in every case I
  checked — no figure, page, or currency claim in `analyst-session-1.md` diverges from
  `session-1-identification.json`/`session-1-extraction.json`. The mismatch is specific to
  session 2 (see Finding 1).

---

## Overall assessment

Every figure that actually made it into the two extraction JSON files is numerically correct
against the source page — I did not find one hallucinated or misattributed value in either
session. The identify stage's page-level judgement (what's a financial statement, what isn't, what
to skip, what to send for review) is also sound wherever I could check it, including under direct
prompt-injection attempts.

The problems are not in the figures the tool produced; they are in (a) what silently doesn't make
it into the report at all — a real budget document in session 1, a real document's figures in
session 2 after a transient failure — and (b) whether the human-readable write-up and the
machine-readable JSON tell the same story once a rerun is involved. Finding 1 is the one I'd treat
as disqualifying on its own: it means the "final" JSON and the analyst's own narrative report of
that JSON disagree about what was extracted, with no way from the reports alone to tell which one
to trust.
