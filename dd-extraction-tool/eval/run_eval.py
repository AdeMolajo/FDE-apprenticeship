"""Evaluation harness: run the golden dataset through the real pipeline and compare
actual output to expected output field by field (Gate 3, Topic 4 schema).

    export OLLAMA_API_KEY=...          # both commands use Ollama here
    .venv/bin/python eval/make_fixtures.py
    .venv/bin/python eval/run_eval.py

Two modes, both using the real CLI commands:
  end_to_end                    dd-identify on the data room, its report fed to dd-extract
  extract_given_expected_pages  dd-extract fed the dataset's expected pages, so an
                                identification miss does not hide extraction quality

Writes eval/reports/<timestamp>/ (results.json, report.md and every raw command output).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EVAL = Path(__file__).resolve().parent
BIN = Path(sys.executable).parent
FIELDS = ["metric", "value", "currency", "confidence", "source_page", "source_document"]
VALUE_TOLERANCE = 0.5
MODES = ["end_to_end", "extract_given_expected_pages"]


# ------------------------------------------------------------------ dataset
def load_cases() -> Tuple[List[dict], dict]:
    golden = json.loads((EVAL / "golden_dataset.json").read_text())
    res = json.loads((EVAL / "golden_resolution.json").read_text())
    aliases, values = res["metric_aliases"], res["values"]
    cases = []
    for c in golden["cases"]:
        ref = res["input_ref_overrides"].get(c["id"], c["input_ref"])
        docs = [d.strip() for d in ref.split(",")]
        exp = c["expected"]
        figures = []
        for f in exp["extract"]:
            doc = f["source_document"]
            if c["id"] in res["input_ref_overrides"]:
                doc = res["input_ref_overrides"][c["id"]]
            figures.append({
                "golden_metric": f["metric"],
                "metric": aliases.get(f["metric"], f["metric"]),
                "value": values.get(c["id"], {}).get(f["metric"], f["value"]),
                "currency": f["currency"] if f["currency"] is not None else res["null_currency"],
                "confidence": f["confidence"],
                "source_page": f["source_page"],
                "source_document": doc,
            })
        metrics = sorted({f["metric"] for f in figures}) or res["default_metrics"]
        cases.append({
            "id": c["id"], "category": c["category"], "description": c["description"],
            "docs": docs,
            "flagged_pages": exp["identify"].get("flagged_pages"),
            "error_prefix": res["error_types"].get(exp["identify"].get("error_type", "")),
            "figures": figures, "metrics": metrics,
            "pass_condition": exp.get("pass_condition"),
        })
    return cases, res


# ------------------------------------------------------------------ pipeline
def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())


def identification_for(docs: Dict[str, List[int]], dataroom: Path) -> dict:
    """A dd-identify-shaped report listing the given pages."""
    return {
        "dataroom": str(dataroom), "model": "eval", "files_scanned": len(docs), "pages_classified": 0,
        "financial_statement_files": [
            {"source_document": d, "pages": [{"source_document": d, "source_page": p, "confidence": "high"}
                                             for p in pages]}
            for d, pages in docs.items() if pages
        ],
        "skipped": [],
    }


def run_extract(case: dict, pages: Dict[str, List[int]], dataroom: Path, out_dir: Path,
                res: dict) -> Tuple[List[dict], List[dict], List[dict], Optional[str]]:
    """Run dd-extract for one case. Returns (figures, skipped, review, error)."""
    if not any(pages.values()):
        return [], [], [], None
    ident = out_dir / f"{case['id']}-identification.json"
    ident.write_text(json.dumps(identification_for(pages, dataroom), indent=2))
    out = out_dir / f"{case['id']}-extraction.json"
    proc = run([str(BIN / "dd-extract"), str(dataroom), "--identification", str(ident), "--out", str(out),
                "--metrics", ",".join(case["metrics"]), "--currencies", ",".join(res["currencies"])])
    if proc.returncode != 0:
        return [], [], [], (proc.stderr.strip().splitlines() or ["dd-extract failed"])[-1]
    report = json.loads(out.read_text())
    return report["figures"], report["skipped"], report.get("review", []), None


# ------------------------------------------------------------------ scoring
def score_identify(case: dict, ident: dict) -> dict:
    flagged = {f["source_document"]: [p["source_page"] for p in f["pages"]]
               for f in ident["financial_statement_files"]}
    skipped = {}
    for s in ident["skipped"]:
        skipped.setdefault(s["source_document"], []).append(s)
    docs = []
    for d in case["docs"]:
        actual = sorted(flagged.get(d, []))
        reasons = [f"p{s['source_page']}: {s['reason']}" if s.get("source_page") else s["reason"]
                   for s in skipped.get(d, [])]
        if case["error_prefix"]:
            ok = not actual and any(s["reason"].startswith(case["error_prefix"]) for s in skipped.get(d, []))
            expected = f"error ({case['error_prefix']}...)"
        else:
            expected = sorted(case["flagged_pages"])
            ok = actual == expected
        review = [f"p{r['source_page']}: {r['reason']}" for r in ident.get("review", [])
                  if r["source_document"] == d]
        docs.append({"doc": d, "expected": expected, "actual": actual, "ok": ok, "skipped": reasons,
                     "review": review})
    return {"ok": all(x["ok"] for x in docs), "docs": docs}


def score_extract(case: dict, actual: List[dict]) -> dict:
    pool = list(actual)
    rows = []
    for exp in case["figures"]:
        cands = [a for a in pool if a["metric"] == exp["metric"]]
        cands.sort(key=lambda a: (a["source_document"] != exp["source_document"],
                                  a["source_page"] != exp["source_page"]))
        got = cands[0] if cands else None
        if got:
            pool.remove(got)
        fields = {}
        for f in FIELDS:
            if got is None:
                fields[f] = False
            elif f == "value":
                fields[f] = exp[f] is not None and abs(float(got[f]) - float(exp[f])) <= VALUE_TOLERANCE
            else:
                fields[f] = got[f] == exp[f]
        rows.append({"expected": exp, "actual": got, "fields": fields})
    return {"figures": rows, "extras": pool}


def verdict(identify: dict, extract: dict, ignore: Tuple[str, ...] = ()) -> bool:
    return (identify["ok"] and not extract["extras"] and
            all(ok for r in extract["figures"] for f, ok in r["fields"].items() if f not in ignore))


# ------------------------------------------------------------------ report
def fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, int) else str(v)


def write_report(results: dict, out_dir: Path) -> str:
    lines = [f"# Golden dataset evaluation", "",
             f"Run: {results['run_at']} · model `{results['model']}` · {len(results['cases'])} cases", ""]
    lines += ["## Summary", "", "| Mode | Cases passed (all fields) | Cases passed (excluding confidence) |",
              "| --- | --- | --- |"]
    for m in MODES:
        s = results["summary"][m]
        lines.append(f"| {m} | {s['pass']}/{s['cases']} | {s['pass_ex_conf']}/{s['cases']} |")
    lines += ["", "## Field accuracy (expected figures matched field by field)", "",
              "| Field | " + " | ".join(MODES) + " |", "| --- | " + " | ".join("---" for _ in MODES) + " |"]
    for f in FIELDS:
        cells = []
        for m in MODES:
            ok, n = results["summary"][m]["fields"][f]
            cells.append(f"{ok}/{n} ({100 * ok / n:.0f}%)" if n else "—")
        lines.append(f"| {f} | " + " | ".join(cells) + " |")
    ex = [str(results["summary"][m]["extras"]) for m in MODES]
    lines.append("| unexpected extra figures | " + " | ".join(ex) + " |")
    rv = [str(results["summary"][m]["review"]) for m in MODES]
    lines.append("| pages flagged for review (dd-extract) | " + " | ".join(rv) + " |")
    ident = results["summary"]["identify"]
    lines += ["", f"**Identification (page level, end_to_end):** {ident['tp']} correct flags, "
              f"{ident['fn']} missed, {ident['fp']} false positives across {ident['docs']} documents.", ""]

    lines += ["## Per case", "", "| Case | Category | Identify | Figures | end_to_end | given expected pages |",
              "| --- | --- | --- | --- | --- | --- |"]
    for c in results["cases"]:
        e2e, ora = c["modes"]["end_to_end"], c["modes"]["extract_given_expected_pages"]
        figs = f"{sum(all(r['fields'].values()) for r in e2e['extract']['figures'])}/{len(e2e['extract']['figures'])}"

        def v(m):
            return "PASS" if m["pass"] else ("pass excl. confidence" if m["pass_ex_conf"] else "FAIL")
        lines.append(f"| {c['id']} | {c['category']} | {'ok' if e2e['identify']['ok'] else 'MISMATCH'} | "
                     f"{figs} exact | {v(e2e)} | {v(ora)} |")

    lines += ["", "## Case detail", ""]
    for c in results["cases"]:
        lines += [f"### {c['id']} ({c['category']})", "", c["description"], ""]
        if c["pass_condition"]:
            lines += [f"*Pass condition:* {c['pass_condition']}", ""]
        for d in c["modes"]["end_to_end"]["identify"]["docs"]:
            mark = "✓" if d["ok"] else "✗"
            lines.append(f"- Identify `{d['doc']}`: expected {d['expected']}, actual {d['actual']} {mark}")
            for s in d["skipped"]:
                lines.append(f"  - skipped: {s}")
            for r in d.get("review", []):
                lines.append(f"  - review (dd-identify): {r}")
        for m in MODES:
            mode = c["modes"][m]
            if mode.get("error"):
                lines.append(f"- **{m}: dd-extract error:** {mode['error']}")
            for r in mode["extract"]["figures"]:
                bad = [f for f, ok in r["fields"].items() if not ok]
                a = r["actual"]
                if not bad:
                    lines.append(f"- {m}: `{r['expected']['golden_metric']}` all fields match "
                                 f"({fmt(a['value'])} {a['currency']}, p{a['source_page']}, {a['confidence']})")
                elif a is None:
                    lines.append(f"- {m}: `{r['expected']['golden_metric']}` **not extracted** "
                                 f"(expected {fmt(r['expected']['value'])} {r['expected']['currency']} "
                                 f"p{r['expected']['source_page']})")
                else:
                    diffs = ", ".join(f"{f}: expected {fmt(r['expected'][f])}, got {fmt(a[f])}" for f in bad)
                    lines.append(f"- {m}: `{r['expected']['golden_metric']}` mismatch: {diffs}")
            for x in mode["extract"]["extras"]:
                lines.append(f"- {m}: unexpected extra figure `{x['metric']}` = {fmt(x['value'])} "
                             f"{x['currency']} p{x['source_page']} ({x['confidence']})")
            for s in mode["skipped"]:
                lines.append(f"- {m}: extraction skipped p{s.get('source_page')}: {s['reason']}")
            for r in mode.get("review", []):
                lines.append(f"- {m}: review p{r['source_page']}: {r['reason']}")
        lines.append("")
    text = "\n".join(lines) + "\n"
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    return text


# ------------------------------------------------------------------ main
def main() -> int:
    if not os.environ.get("OLLAMA_API_KEY"):
        print("error: OLLAMA_API_KEY is not set", file=sys.stderr)
        return 2
    cases, res = load_cases()
    dataroom = (EVAL / res["dataroom"]).resolve()
    missing = [d for c in cases for d in c["docs"] if not (dataroom / d).exists()]
    if missing:
        print(f"error: fixtures missing (run eval/make_fixtures.py): {missing}", file=sys.stderr)
        return 2
    out_dir = EVAL / "reports" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)

    print(f"Step 1: dd-identify on {dataroom} ...", flush=True)
    ident_path = out_dir / "identification.json"
    proc = run([str(BIN / "dd-identify"), str(dataroom), "--provider", res["identify_provider"],
                "--out", str(ident_path)])
    print(proc.stdout.strip() or proc.stderr.strip(), flush=True)
    if proc.returncode != 0:
        return 1
    ident = json.loads(ident_path.read_text())
    flagged = {f["source_document"]: [p["source_page"] for p in f["pages"]]
               for f in ident["financial_statement_files"]}

    results = {"run_at": datetime.now().isoformat(timespec="seconds"), "model": ident["model"], "cases": []}
    for case in cases:
        print(f"Step 2: {case['id']} ...", flush=True)
        entry = {k: case[k] for k in ("id", "category", "description", "pass_condition", "metrics")}
        identify = score_identify(case, ident)
        entry["modes"] = {}
        for mode in MODES:
            if mode == "end_to_end":
                pages = {d: flagged.get(d, []) for d in case["docs"]}
            else:
                pages = {d: sorted({f["source_page"] for f in case["figures"] if f["source_document"] == d})
                         for d in case["docs"]}
            mdir = out_dir / mode
            mdir.mkdir(exist_ok=True)
            figures, skipped, review, error = run_extract(case, pages, dataroom, mdir, res)
            extract = score_extract(case, figures)
            ident_for_verdict = identify if mode == "end_to_end" else {"ok": True}
            entry["modes"][mode] = {
                "identify": identify, "extract": extract, "skipped": skipped, "review": review, "error": error,
                "pass": verdict(ident_for_verdict, extract) and not error,
                "pass_ex_conf": verdict(ident_for_verdict, extract, ("confidence",)) and not error,
            }
        results["cases"].append(entry)

    summary = {}
    for mode in MODES:
        ms = [c["modes"][mode] for c in results["cases"]]
        summary[mode] = {
            "cases": len(ms), "pass": sum(m["pass"] for m in ms), "pass_ex_conf": sum(m["pass_ex_conf"] for m in ms),
            "fields": {f: (sum(r["fields"][f] for m in ms for r in m["extract"]["figures"]),
                           sum(len(m["extract"]["figures"]) for m in ms)) for f in FIELDS},
            "extras": sum(len(m["extract"]["extras"]) for m in ms),
            "review": sum(len(m["review"]) for m in ms),
        }
    tp = fn = fp = docs = 0
    for c in results["cases"]:
        for d in c["modes"]["end_to_end"]["identify"]["docs"]:
            docs += 1
            exp = set(d["expected"]) if isinstance(d["expected"], list) else set()
            tp += len(exp & set(d["actual"]))
            fn += len(exp - set(d["actual"]))
            fp += len(set(d["actual"]) - exp)
    summary["identify"] = {"tp": tp, "fn": fn, "fp": fp, "docs": docs}
    results["summary"] = summary

    (out_dir / "results.json").write_text(json.dumps(results, indent=2, default=str))
    report = write_report(results, out_dir)
    print("\n" + report.split("## Case detail")[0])
    print(f"Full report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
