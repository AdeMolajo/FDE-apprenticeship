"""A structured log of every run of dd-identify and dd-extract.

One JSON object per run, appended to a JSON Lines file (default: runs.jsonl next to
the report). Each record says what went in, what came out, whether the output matched
the expected shape, when it ran, how long it took and what it cost in model calls and
tokens.

Money: token counts are always recorded because the providers report them. A cost in
currency is only recorded when prices are configured, via a JSON file named by the
DD_PRICING environment variable:

    {"ollama/gpt-oss:20b": {"input_per_mtok": 0.10, "output_per_mtok": 0.40}}

Without it the run still logs calls and tokens, and cost.amount is null with a reason,
rather than a made-up number.

Read the log back with:  python -m dd_extraction.runlog runs.jsonl
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .schema import ExtractionReport, IdentificationReport

LOG_SCHEMA = "dd-run-log/1"
DEFAULT_LOG_NAME = "runs.jsonl"


@dataclass
class RunStats:
    """Model usage for one run. Providers report token counts; we only add them up."""

    model_calls: int = 0
    retried_calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record_ollama(self, body: Dict[str, Any]) -> None:
        self.model_calls += 1
        self.prompt_tokens += int(body.get("prompt_eval_count") or 0)
        self.completion_tokens += int(body.get("eval_count") or 0)

    def record_anthropic(self, response: Any) -> None:
        self.model_calls += 1
        usage = getattr(response, "usage", None)
        self.prompt_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.completion_tokens += int(getattr(usage, "output_tokens", 0) or 0)

    def record_retry(self) -> None:
        self.retried_calls += 1

    def record_failure(self) -> None:
        self.failed_calls += 1


def load_pricing() -> Optional[Dict[str, Dict[str, float]]]:
    path = os.environ.get("DD_PRICING")
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def cost_for(model: str, stats: RunStats) -> Dict[str, Any]:
    prices = load_pricing()
    if prices is None:
        return {"amount": None, "currency": None,
                "basis": "no pricing configured (set DD_PRICING to a JSON price file)"}
    rate = prices.get(model) or prices.get(model.split("/")[-1])
    if rate is None:
        return {"amount": None, "currency": None, "basis": f"no price listed for {model}"}
    amount = (stats.prompt_tokens * float(rate.get("input_per_mtok", 0.0))
              + stats.completion_tokens * float(rate.get("output_per_mtok", 0.0))) / 1_000_000
    return {"amount": round(amount, 6), "currency": rate.get("currency", "USD"),
            "basis": "tokens reported by the provider x configured price"}


# --------------------------------------------------------------- shape checks
def check_shape(kind: str, data: Dict[str, Any]) -> List[str]:
    """Does the written report match the expected shape? Returns problems, empty if fine.

    Validates against the report model, then checks the invariants the schema alone
    cannot: required traceability fields present and sane, counts consistent, every
    skipped item carrying a reason.
    """
    model = IdentificationReport if kind == "identify" else ExtractionReport
    try:
        report = model.model_validate(data)
    except ValidationError as exc:
        return [f"{e['loc']}: {e['msg']}" for e in exc.errors()[:10]]

    problems: List[str] = []
    if kind == "identify":
        pages = [(f.source_document, p) for f in report.financial_statement_files for p in f.pages]
        for doc, page in pages:
            if page.source_document != doc:
                problems.append(f"{doc} p{page.source_page}: page cites a different document")
            if page.source_page < 1:
                problems.append(f"{doc}: source_page {page.source_page} is not a page number")
        if report.pages_classified < len(pages):
            problems.append(f"pages_classified ({report.pages_classified}) is fewer than the "
                            f"{len(pages)} pages reported as financial statements")
        if report.files_scanned < len(report.financial_statement_files):
            problems.append("files_scanned is fewer than the files reported")
    else:
        for figure in report.figures:
            if not figure.source_document or figure.source_page < 1:
                problems.append(f"{figure.metric}: missing or invalid source reference")
            if figure.metric not in report.target_metrics:
                problems.append(f"{figure.metric}: not one of the requested metrics")
            if figure.currency not in report.currencies and figure.currency != "NOT_STATED":
                problems.append(f"{figure.metric}: currency {figure.currency} is not accepted")
        if report.figures and report.pages_processed < 1:
            problems.append("figures reported but pages_processed is 0")
    for item in report.skipped:
        if not item.reason:
            problems.append(f"{item.source_document}: skipped with no reason")
    return problems


# --------------------------------------------------------------- writing
def write_run(log_path: Path, record: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=False) + "\n")


class RunLogger:
    """Times a run and writes one record when it ends, however it ends."""

    def __init__(self, command: str, kind: str, log_path: Path, inputs: Dict[str, Any],
                 model: str, provider: str, stats: RunStats) -> None:
        self.command, self.kind, self.log_path, self.inputs = command, kind, log_path, inputs
        self.model, self.provider, self.stats = model, provider, stats
        self.record: Dict[str, Any] = {}

    def __enter__(self) -> "RunLogger":
        self.started = datetime.now(timezone.utc)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # A run that crashed is still a run: log it rather than lose it.
        if not self.record:
            error = f"{exc_type.__name__}: {exc}" if exc_type else "ended without writing a report"
            self.finish(None, None, "error", error)
        return False

    def finish(self, output_path: Optional[Path], data: Optional[Dict[str, Any]],
               status: str, error: Optional[str] = None) -> Dict[str, Any]:
        finished = datetime.now(timezone.utc)
        problems = check_shape(self.kind, data) if data is not None else ["no output written"]
        counts: Dict[str, Any] = {}
        if data is not None:
            counts = {k: (len(v) if isinstance(v, list) else v) for k, v in data.items()
                      if k not in ("dataroom", "model", "target_metrics", "currencies")}
        self.record = {
            "schema": LOG_SCHEMA,
            "run_id": uuid.uuid4().hex[:12],
            "command": self.command,
            "started_at": self.started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": round((finished - self.started).total_seconds(), 1),
            "host": platform.node(),
            "provider": self.provider,
            "model": self.model,
            "input": self.inputs,
            "output": {"path": str(output_path) if output_path else None, "counts": counts},
            "shape": {"status": "pass" if not problems and status == "ok" else "fail",
                      "problems": problems},
            "usage": asdict(self.stats),
            "cost": cost_for(self.model, self.stats),
            "status": status,
            "error": error,
        }
        write_run(self.log_path, self.record)
        return self.record


# --------------------------------------------------------------- reading back
def summarise(log_path: Path) -> str:
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    if not rows:
        return "no runs logged"
    head = f"{'started (UTC)':<20}{'command':<12}{'status':<8}{'shape':<7}{'secs':>6}" \
           f"{'calls':>7}{'tokens':>9}{'cost':>10}  output"
    lines = [head, "-" * len(head)]
    for r in rows:
        usage = r["usage"]
        tokens = usage["prompt_tokens"] + usage["completion_tokens"]
        cost = r["cost"]["amount"]
        lines.append(
            f"{r['started_at'][:19].replace('T', ' '):<20}{r['command']:<12}{r['status']:<8}"
            f"{r['shape']['status']:<7}{r['duration_seconds']:>6}{usage['model_calls']:>7}"
            f"{tokens:>9}{('—' if cost is None else f'{cost:.4f}'):>10}  "
            f"{Path(r['output']['path']).name if r['output']['path'] else '—'}")
        for problem in r["shape"]["problems"]:
            lines.append(f"{'':<20}  problem: {problem}")
    totals = {"calls": sum(r["usage"]["model_calls"] for r in rows),
              "tokens": sum(r["usage"]["prompt_tokens"] + r["usage"]["completion_tokens"] for r in rows),
              "retries": sum(r["usage"]["retried_calls"] for r in rows),
              "failed": sum(r["usage"]["failed_calls"] for r in rows)}
    costs = [r["cost"]["amount"] for r in rows if r["cost"]["amount"] is not None]
    lines += ["-" * len(head),
              f"{len(rows)} runs · {totals['calls']} model calls · {totals['tokens']:,} tokens · "
              f"{totals['retries']} retries · {totals['failed']} failed calls · "
              f"cost {('not priced' if not costs else f'{sum(costs):.4f}')}"]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    import sys
    print(summarise(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG_NAME)))
