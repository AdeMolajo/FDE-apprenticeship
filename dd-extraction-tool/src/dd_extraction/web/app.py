"""A local web application over the pipeline.

Runs dd-identify and dd-extract on a data room, shows the results with every
figure traceable to its page, and records the human verification step: who
confirmed a figure, when, and how long the page took. That last part is new
work, not a view of something the CLI already did: review time was the one
number nothing measured.

Local only by design: it binds to 127.0.0.1, has no authentication, and runs
as whoever started it. See SECURITY.md.

    .venv/bin/dd-web                # http://127.0.0.1:5000
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from ..dataroom import load_pages, walk_dataroom
from ..extract import (
    DEFAULT_CURRENCIES,
    DEFAULT_TARGET_METRICS,
    extract_figures,
    make_ollama_extractor,
)
from ..identify import ProviderAuthError, identify_financial_statements
from ..ollama import DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, make_ollama_classifier
from ..runlog import DEFAULT_LOG_NAME, RunLogger, RunStats, summarise
from ..schema import ExtractionReport, IdentificationReport


@dataclass
class Job:
    """One pipeline run, in progress or finished."""

    id: str
    dataroom: Path
    out_dir: Path
    metrics: List[str]
    currencies: List[str]
    started_at: str
    state: str = "finding statements"  # -> extracting figures -> done | failed
    pages_total: int = 0
    pages_done: int = 0
    error: Optional[str] = None
    identification: Optional[IdentificationReport] = None
    extraction: Optional[ExtractionReport] = None
    finished_at: Optional[str] = None

    @property
    def progress(self) -> int:
        return round(100 * self.pages_done / self.pages_total) if self.pages_total else 0

    @property
    def done(self) -> bool:
        return self.state in ("done", "failed")


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def count_pages(dataroom: Path) -> int:
    pdfs, _ = walk_dataroom(dataroom)
    total = 0
    for path in pdfs:
        try:
            total += sum(1 for _ in load_pages(dataroom, path))
        except Exception:  # an unreadable PDF is counted in the report, not here
            continue
    return total


def run_job(job: Job, api_key: Optional[str], host: str, model: str) -> None:
    """The pipeline, in a background thread. Every failure lands on the job."""
    log_path = job.out_dir / DEFAULT_LOG_NAME
    try:
        job.pages_total = count_pages(job.dataroom)

        stats = RunStats()
        classify = make_ollama_classifier(model=model, host=host, api_key=api_key, stats=stats)

        def counted_classify(page):
            try:
                return classify(page)
            finally:
                job.pages_done += 1

        with RunLogger("dd-web (identify)", "identify", log_path,
                       {"dataroom": str(job.dataroom), "provider": "ollama"},
                       f"ollama/{model}", "ollama", stats) as logger:
            report = identify_financial_statements(job.dataroom, counted_classify,
                                                   model=f"ollama/{model}")
            data = report.model_dump(mode="json")
            (job.out_dir / "identification.json").write_text(json.dumps(data, indent=2))
            logger.finish(job.out_dir / "identification.json", data, "ok")
        job.identification = report

        job.state = "extracting figures"
        job.pages_total = sum(len(f.pages) for f in report.financial_statement_files) or 1
        job.pages_done = 0

        stats = RunStats()
        extract = make_ollama_extractor(model=model, host=host, api_key=api_key,
                                        target_metrics=job.metrics, currencies=job.currencies,
                                        stats=stats)

        def counted_extract(page):
            try:
                return extract(page)
            finally:
                job.pages_done += 1

        with RunLogger("dd-web (extract)", "extract", log_path,
                       {"dataroom": str(job.dataroom), "metrics": job.metrics,
                        "currencies": job.currencies}, f"ollama/{model}", "ollama", stats) as logger:
            figures = extract_figures(job.dataroom, report, counted_extract,
                                      model=f"ollama/{model}", target_metrics=job.metrics,
                                      currencies=job.currencies)
            data = figures.model_dump(mode="json")
            (job.out_dir / "extraction.json").write_text(json.dumps(data, indent=2))
            logger.finish(job.out_dir / "extraction.json", data, "ok")
        job.extraction = figures
        job.state = "done"
    except ProviderAuthError as exc:
        job.state, job.error = "failed", f"The model provider rejected the key: {exc}"
    except Exception as exc:  # the run is over; the person needs to see why
        job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        job.finished_at = now()


# ----------------------------------------------------------------- verifications
def verification_path(out_dir: Path) -> Path:
    return out_dir / "verifications.jsonl"


def load_verifications(out_dir: Path) -> Dict[str, dict]:
    """Latest decision per figure, keyed by document|page|metric."""
    path = verification_path(out_dir)
    if not path.exists():
        return {}
    out: Dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            out[record["key"]] = record
    return out


def record_verification(out_dir: Path, record: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with verification_path(out_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def figure_key(figure) -> str:
    doc = figure["source_document"] if isinstance(figure, dict) else figure.source_document
    page = figure["source_page"] if isinstance(figure, dict) else figure.source_page
    metric = figure["metric"] if isinstance(figure, dict) else figure.metric
    return f"{doc}|{page}|{metric}"


# ----------------------------------------------------------------- the app
def create_app(reports_root: Optional[Path] = None) -> Flask:
    app = Flask(__name__)
    app.config["REPORTS_ROOT"] = Path(reports_root or Path.cwd() / "reports").resolve()

    def root() -> Path:
        return Path(app.config["REPORTS_ROOT"])

    def job_or_404(job_id: str) -> Job:
        job = JOBS.get(job_id)
        if job is None:
            abort(404)
        return job

    @app.route("/")
    def home():
        log = root() / DEFAULT_LOG_NAME
        with JOBS_LOCK:
            jobs = sorted(JOBS.values(), key=lambda j: j.started_at, reverse=True)
        return render_template("home.html", jobs=jobs,
                               log=summarise(log) if log.exists() else None)

    @app.route("/new", methods=["GET", "POST"])
    def new_run():
        defaults = {"dataroom": "", "metrics": DEFAULT_TARGET_METRICS,
                    "currencies": DEFAULT_CURRENCIES}
        if request.method == "GET":
            return render_template("new_run.html", defaults=defaults, error=None, preview=None)

        dataroom = Path(request.form.get("dataroom", "").strip()).expanduser()
        metrics = [m.strip() for m in request.form.getlist("metrics") if m.strip()]
        currencies = [c.strip().upper() for c in
                      request.form.get("currencies", "").split(",") if c.strip()]
        defaults = {"dataroom": str(dataroom), "metrics": metrics, "currencies": currencies}

        error = None
        if not dataroom.is_dir():
            error = f"No folder at {dataroom}"
        elif not metrics:
            error = "Choose at least one figure to extract."
        elif not currencies:
            error = "Give at least one currency, for example GBP."
        api_key = os.environ.get("OLLAMA_API_KEY")
        host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
        if error is None and not api_key and "ollama.com" in host:
            error = "OLLAMA_API_KEY is not set, so the model cannot be reached."
        if error:
            return render_template("new_run.html", defaults=defaults, error=error,
                                   preview=None), 400

        job = Job(id=uuid.uuid4().hex[:8], dataroom=dataroom.resolve(),
                  out_dir=root() / datetime.now().strftime("%Y%m%d-%H%M%S"),
                  metrics=metrics, currencies=currencies, started_at=now())
        job.out_dir.mkdir(parents=True, exist_ok=True)
        with JOBS_LOCK:
            JOBS[job.id] = job
        threading.Thread(target=run_job, args=(job, api_key, host,
                                               os.environ.get("DD_MODEL", DEFAULT_OLLAMA_MODEL)),
                         daemon=True).start()
        return redirect(url_for("results", job_id=job.id))

    @app.route("/run/<job_id>")
    def results(job_id: str):
        job = job_or_404(job_id)
        if not job.done:
            return render_template("running.html", job=job)
        if job.state == "failed":
            return render_template("failed.html", job=job), 200

        identification, extraction = job.identification, job.extraction
        flagged = {f.source_document: [p.source_page for p in f.pages]
                   for f in identification.financial_statement_files}
        pdfs, others = walk_dataroom(job.dataroom)
        # The report lists only flagged documents; show the rest too, so a document
        # that was read and not flagged is a stated fact rather than an absence.
        read_not_flagged = [p.relative_to(job.dataroom).as_posix() for p in pdfs
                            if p.relative_to(job.dataroom).as_posix() not in flagged]
        verified = load_verifications(job.out_dir)
        figures = []
        for figure in extraction.figures:
            record = verified.get(figure_key(figure))
            figures.append({"figure": figure, "verification": record})
        return render_template("results.html", job=job, identification=identification,
                               extraction=extraction, figures=figures,
                               read_not_flagged=read_not_flagged,
                               not_pdfs=[p.relative_to(job.dataroom).as_posix() for p, _ in others],
                               checked=sum(1 for f in figures if f["verification"]))

    @app.route("/run/<job_id>/page")
    def verify(job_id: str):
        job = job_or_404(job_id)
        if job.extraction is None:
            abort(404)
        document = request.args.get("document", "")
        try:
            page_number = int(request.args.get("page", "0"))
        except ValueError:
            abort(400)
        path = (job.dataroom / document).resolve()
        if job.dataroom not in path.parents or not path.exists():
            abort(404)  # never read outside the data room

        page = next((p for p in load_pages(job.dataroom, path)
                     if p.source_page == page_number), None)
        if page is None:
            abort(404)
        verified = load_verifications(job.out_dir)
        on_page = [{"figure": f, "verification": verified.get(figure_key(f))}
                   for f in job.extraction.figures
                   if f.source_document == document and f.source_page == page_number]
        pages = [(f.source_document, p.source_page)
                 for f in job.identification.financial_statement_files for p in f.pages]
        index = pages.index((document, page_number)) if (document, page_number) in pages else 0
        return render_template("verify.html", job=job, page=page, figures=on_page,
                               lines=(page.text or "").splitlines(),
                               position=index + 1, total=len(pages),
                               next_page=pages[index + 1] if index + 1 < len(pages) else None,
                               opened_at=now(),
                               reviewer=request.args.get("by") or os.environ.get("USER", ""))

    @app.route("/run/<job_id>/verify", methods=["POST"])
    def save_verification(job_id: str):
        job = job_or_404(job_id)
        document = request.form["document"]
        page_number = int(request.form["page"])
        opened_at = request.form.get("opened_at")
        seconds = None
        if opened_at:
            try:
                seconds = round((datetime.now(timezone.utc)
                                 - datetime.fromisoformat(opened_at)).total_seconds(), 1)
            except ValueError:
                seconds = None
        record_verification(job.out_dir, {
            "key": f"{document}|{page_number}|{request.form['metric']}",
            "source_document": document, "source_page": page_number,
            "metric": request.form["metric"], "decision": request.form["decision"],
            "note": request.form.get("note", "").strip(),
            "by": request.form.get("by", "").strip() or "unnamed",
            "at": now(), "seconds_on_page": seconds,
        })
        return redirect(request.form.get("next") or
                        url_for("verify", job_id=job.id, document=document, page=page_number))

    @app.route("/run/<job_id>/export.csv")
    def export_csv(job_id: str):
        job = job_or_404(job_id)
        if job.extraction is None:
            abort(404)
        verified = load_verifications(job.out_dir)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["metric", "value", "currency", "confidence", "source_document",
                         "source_page", "checked_by", "checked_at", "decision"])
        for figure in job.extraction.figures:
            record = verified.get(figure_key(figure), {})
            writer.writerow([figure.metric, figure.value, figure.currency, figure.confidence,
                             figure.source_document, figure.source_page,
                             record.get("by", ""), record.get("at", ""),
                             record.get("decision", "unchecked")])
        return Response(buffer.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": f'attachment; filename="figures-{job.id}.csv"'})

    return app


def main() -> int:
    app = create_app()
    port = int(os.environ.get("DD_WEB_PORT", "5000"))
    print(f"DD extraction tool: http://127.0.0.1:{port}  (local only, no sign-in)")
    app.run(host="127.0.0.1", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
