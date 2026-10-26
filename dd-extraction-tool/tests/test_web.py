"""The local web application: running the pipeline, showing results, and recording
the human verification step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dd_extraction.schema import (
    ExtractedFigure,
    ExtractionReport,
    FinancialStatementFile,
    IdentificationReport,
    IdentifiedPage,
    ReviewFlag,
    SkippedItem,
)
from dd_extraction.web.app import Job, create_app, load_verifications

from test_identify import make_pdf

DOC = "01 Financial/FY25 accounts.pdf"


@pytest.fixture
def dataroom(tmp_path: Path) -> Path:
    root = tmp_path / "dataroom"
    make_pdf(root / DOC, [["Acme Ltd", "Cover page"],
                          ["Acme Ltd", "Income statement", "Revenue 1,250,000",
                           "Profit for the year 340,000"]])
    make_pdf(root / "02 Legal" / "Agreement.pdf", [["Services agreement", "Clause 1"]])
    (root / "02 Legal" / "notes.txt").write_text("not a pdf")
    return root


@pytest.fixture
def finished_job(dataroom: Path, tmp_path: Path) -> Job:
    """A completed run, as the pipeline would leave it."""
    out_dir = tmp_path / "reports" / "run"
    out_dir.mkdir(parents=True)
    job = Job(id="abc123", dataroom=dataroom, out_dir=out_dir,
              metrics=["revenue", "net_profit"], currencies=["GBP"],
              started_at="2026-10-26T10:00:00+00:00", state="done",
              finished_at="2026-10-26T10:01:00+00:00")
    job.identification = IdentificationReport(
        dataroom=str(dataroom), model="ollama/gpt-oss:20b", files_scanned=2, pages_classified=3,
        financial_statement_files=[FinancialStatementFile(
            source_document=DOC,
            pages=[IdentifiedPage(source_document=DOC, source_page=2, confidence="high")])],
        skipped=[SkippedItem(source_document="02 Legal/notes.txt", reason="not a PDF")],
        review=[ReviewFlag(source_document=DOC, source_page=2,
                           reason="page text contains a lookalike of the pipeline's tag")],
    )
    job.extraction = ExtractionReport(
        dataroom=str(dataroom), model="ollama/gpt-oss:20b",
        target_metrics=["revenue", "net_profit"], currencies=["GBP"], pages_processed=1,
        figures=[
            ExtractedFigure(metric="revenue", value=1250000, currency="GBP", confidence="high",
                            source_document=DOC, source_page=2),
            ExtractedFigure(metric="net_profit", value=340000, currency="NOT_STATED",
                            confidence="high", source_document=DOC, source_page=2),
        ],
        skipped=[], review=[],
    )
    return job


@pytest.fixture
def client(tmp_path: Path, finished_job: Job, monkeypatch):
    import dd_extraction.web.app as web

    monkeypatch.setattr(web, "JOBS", {finished_job.id: finished_job})
    app = create_app(reports_root=tmp_path / "reports")
    app.config.update(TESTING=True)
    return app.test_client()


def test_home_lists_the_run(client, finished_job):
    page = client.get("/").get_data(as_text=True)
    assert "Complete" in page and str(finished_job.dataroom) in page


def test_results_show_every_figure_with_its_source(client):
    page = client.get("/run/abc123").get_data(as_text=True)
    assert "1,250,000" in page and "340,000" in page
    assert DOC in page and "p2" in page
    assert "not stated" in page  # the NOT_STATED currency is labelled, not blank


def test_results_list_documents_read_but_not_flagged(client):
    """The report itself omits them; the app states them, which the pilot asked for."""
    page = client.get("/run/abc123").get_data(as_text=True)
    assert "02 Legal/Agreement.pdf" in page and "Read, no statements found" in page
    assert "notes.txt" in page and "Not a PDF" in page


def test_results_surface_the_review_flag(client):
    page = client.get("/run/abc123").get_data(as_text=True)
    assert "Needs your eye" in page and "lookalike" in page


def test_verify_page_shows_the_real_page_text(client):
    page = client.get(f"/run/abc123/page?document={DOC}&page=2").get_data(as_text=True)
    assert "Income statement" in page and "Revenue 1,250,000" in page
    assert "Matches the page" in page and "Wrong figure" in page


def test_verify_refuses_a_document_outside_the_dataroom(client):
    assert client.get("/run/abc123/page?document=../../etc/hosts&page=1").status_code == 404


def test_confirming_a_figure_records_who_when_and_how_long(client, finished_job):
    response = client.post("/run/abc123/verify", data={
        "document": DOC, "page": "2", "metric": "revenue", "decision": "matches",
        "by": "A. Molajo", "note": "agrees with the audited accounts",
        "opened_at": "2026-10-26T10:05:00+00:00",
    })
    assert response.status_code == 302

    [record] = load_verifications(finished_job.out_dir).values()
    assert record["decision"] == "matches" and record["by"] == "A. Molajo"
    assert record["metric"] == "revenue" and record["source_page"] == 2
    assert record["note"] == "agrees with the audited accounts"
    assert record["seconds_on_page"] is not None  # review time, which nothing measured before
    assert "T" in record["at"]


def test_a_confirmed_figure_shows_as_checked(client):
    client.post("/run/abc123/verify", data={"document": DOC, "page": "2", "metric": "revenue",
                                            "decision": "matches", "by": "A. Molajo"})
    page = client.get("/run/abc123").get_data(as_text=True)
    assert "Confirmed" in page and "1 of 2 figures checked" in page


def test_marking_a_figure_wrong_is_recorded_as_wrong(client, finished_job):
    client.post("/run/abc123/verify", data={"document": DOC, "page": "2", "metric": "net_profit",
                                            "decision": "wrong", "by": "A. Molajo"})
    [record] = load_verifications(finished_job.out_dir).values()
    assert record["decision"] == "wrong"
    assert "Wrong" in client.get("/run/abc123").get_data(as_text=True)


def test_export_csv_includes_the_check_columns(client):
    client.post("/run/abc123/verify", data={"document": DOC, "page": "2", "metric": "revenue",
                                            "decision": "matches", "by": "A. Molajo"})
    response = client.get("/run/abc123/export.csv")
    body = response.get_data(as_text=True)
    assert response.headers["Content-Type"].startswith("text/csv")
    assert "metric,value,currency,confidence,source_document,source_page,checked_by" in body
    assert "revenue,1250000.0,GBP,high" in body and "A. Molajo" in body
    assert "unchecked" in body  # the figure nobody has checked


def test_a_run_in_progress_shows_progress_not_results(client, finished_job):
    finished_job.state = "finding statements"
    finished_job.pages_total, finished_job.pages_done = 10, 4
    page = client.get("/run/abc123").get_data(as_text=True)
    assert "finding statements" in page and "4 of 10 pages" in page
    assert "Extracted figures" not in page


def test_a_failed_run_says_what_went_wrong(client, finished_job):
    finished_job.state, finished_job.error = "failed", "The model provider rejected the key: 401"
    page = client.get("/run/abc123").get_data(as_text=True)
    assert "The run stopped" in page and "rejected the key" in page


def test_new_run_rejects_a_folder_that_is_not_there(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    response = client.post("/new", data={"dataroom": "/nowhere/at/all", "metrics": ["revenue"],
                                         "currencies": "GBP"})
    assert response.status_code == 400
    assert "No folder at" in response.get_data(as_text=True)


def test_new_run_needs_a_key_before_it_starts(client, dataroom, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    response = client.post("/new", data={"dataroom": str(dataroom), "metrics": ["revenue"],
                                         "currencies": "GBP"})
    assert response.status_code == 400
    assert "OLLAMA_API_KEY is not set" in response.get_data(as_text=True)


def test_unknown_run_is_a_404(client):
    assert client.get("/run/nope").status_code == 404
