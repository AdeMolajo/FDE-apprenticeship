"""The structured run log: one record per run, with input, output, shape check,
timestamps and cost."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dd_extraction.runlog import (
    DEFAULT_LOG_NAME,
    LOG_SCHEMA,
    RunLogger,
    RunStats,
    check_shape,
    cost_for,
    summarise,
)

from test_identify import make_pdf

IDENTIFY_OK = {
    "dataroom": "/dr", "model": "ollama/gpt-oss:20b", "files_scanned": 2, "pages_classified": 5,
    "financial_statement_files": [{"source_document": "a.pdf", "pages": [
        {"source_document": "a.pdf", "source_page": 2, "confidence": "high"}]}],
    "skipped": [{"source_document": "b.csv", "source_page": None, "reason": "not a PDF"}],
    "review": [],
}
EXTRACT_OK = {
    "dataroom": "/dr", "model": "ollama/gpt-oss:20b", "target_metrics": ["revenue"],
    "currencies": ["GBP"], "pages_processed": 1,
    "figures": [{"metric": "revenue", "value": 100.0, "currency": "GBP", "confidence": "high",
                 "source_document": "a.pdf", "source_page": 2}],
    "skipped": [], "review": [],
}


def logger_for(tmp_path: Path, kind: str = "identify", stats: RunStats = None) -> RunLogger:
    return RunLogger("dd-" + kind, kind, tmp_path / DEFAULT_LOG_NAME, {"dataroom": "/dr"},
                     "ollama/gpt-oss:20b", "ollama", stats or RunStats())


# ------------------------------------------------------------------ shape
def test_a_good_report_has_no_shape_problems():
    assert check_shape("identify", IDENTIFY_OK) == []
    assert check_shape("extract", EXTRACT_OK) == []


def test_shape_catches_a_report_that_is_not_the_right_type():
    problems = check_shape("identify", {"dataroom": "/dr"})
    assert problems and any("Field required" in p for p in problems)


@pytest.mark.parametrize("change, expected", [
    ({"pages_classified": 0}, "fewer than"),
    ({"skipped": [{"source_document": "b.csv", "source_page": None, "reason": ""}]}, "no reason"),
])
def test_shape_catches_inconsistent_identification(change, expected):
    problems = check_shape("identify", {**IDENTIFY_OK, **change})
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize("figure_change, expected", [
    ({"metric": "ebitda"}, "not one of the requested metrics"),
    ({"currency": "SEK"}, "not accepted"),
    ({"source_document": ""}, "missing or invalid source reference"),
])
def test_shape_catches_a_figure_that_breaks_the_contract(figure_change, expected):
    data = {**EXTRACT_OK, "figures": [{**EXTRACT_OK["figures"][0], **figure_change}]}
    assert any(expected in p for p in check_shape("extract", data)), check_shape("extract", data)


# ------------------------------------------------------------------ record
def test_a_run_writes_one_record_with_everything_needed(tmp_path: Path):
    stats = RunStats(model_calls=5, retried_calls=1, prompt_tokens=900, completion_tokens=100)
    with logger_for(tmp_path, stats=stats) as log:
        record = log.finish(tmp_path / "report.json", IDENTIFY_OK, "ok")

    [line] = (tmp_path / DEFAULT_LOG_NAME).read_text().splitlines()
    written = json.loads(line)
    assert written == record
    assert written["schema"] == LOG_SCHEMA and written["command"] == "dd-identify"
    assert written["status"] == "ok" and written["shape"] == {"status": "pass", "problems": []}
    assert written["input"]["dataroom"] == "/dr"
    assert written["output"]["counts"]["pages_classified"] == 5
    assert written["usage"] == {"model_calls": 5, "retried_calls": 1, "failed_calls": 0,
                                "prompt_tokens": 900, "completion_tokens": 100}
    assert written["started_at"] <= written["finished_at"] and written["duration_seconds"] >= 0


def test_runs_append_rather_than_overwrite(tmp_path: Path):
    for _ in range(3):
        with logger_for(tmp_path) as log:
            log.finish(tmp_path / "report.json", IDENTIFY_OK, "ok")
    assert len((tmp_path / DEFAULT_LOG_NAME).read_text().splitlines()) == 3


def test_a_bad_shape_is_recorded_as_a_failure(tmp_path: Path):
    with logger_for(tmp_path, "extract") as log:
        record = log.finish(tmp_path / "r.json", {**EXTRACT_OK, "currencies": ["USD"]}, "ok")
    assert record["shape"]["status"] == "fail"
    assert any("not accepted" in p for p in record["shape"]["problems"])


def test_a_failed_run_is_still_logged(tmp_path: Path):
    with logger_for(tmp_path) as log:
        log.finish(None, None, "error", "ProviderAuthError: rejected the API key (401)")
    written = json.loads((tmp_path / DEFAULT_LOG_NAME).read_text())
    assert written["status"] == "error" and "401" in written["error"]
    assert written["shape"] == {"status": "fail", "problems": ["no output written"]}


def test_a_crash_is_logged_too(tmp_path: Path):
    with pytest.raises(ValueError):
        with logger_for(tmp_path):
            raise ValueError("boom")
    written = json.loads((tmp_path / DEFAULT_LOG_NAME).read_text())
    assert written["status"] == "error" and written["error"] == "ValueError: boom"


# ------------------------------------------------------------------ cost
def test_cost_is_null_without_pricing(monkeypatch):
    monkeypatch.delenv("DD_PRICING", raising=False)
    cost = cost_for("ollama/gpt-oss:20b", RunStats(prompt_tokens=1_000_000))
    assert cost["amount"] is None and "no pricing configured" in cost["basis"]


def test_cost_uses_provider_token_counts_when_priced(monkeypatch, tmp_path: Path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"ollama/gpt-oss:20b": {"input_per_mtok": 0.10,
                                                         "output_per_mtok": 0.40}}))
    monkeypatch.setenv("DD_PRICING", str(prices))
    cost = cost_for("ollama/gpt-oss:20b", RunStats(prompt_tokens=2_000_000, completion_tokens=500_000))
    assert cost["amount"] == pytest.approx(0.4) and cost["currency"] == "USD"


def test_unknown_model_is_not_priced(monkeypatch, tmp_path: Path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"other-model": {"input_per_mtok": 1.0}}))
    monkeypatch.setenv("DD_PRICING", str(prices))
    assert cost_for("ollama/gpt-oss:20b", RunStats(prompt_tokens=10))["amount"] is None


# ------------------------------------------------------------------ usage capture
def test_token_counts_come_from_the_provider_response():
    stats = RunStats()
    stats.record_ollama({"prompt_eval_count": 640, "eval_count": 48})
    stats.record_ollama({"prompt_eval_count": 500, "eval_count": 30})
    assert (stats.model_calls, stats.prompt_tokens, stats.completion_tokens) == (2, 1140, 78)


def test_a_real_classifier_call_records_usage_and_retries():
    from dd_extraction.dataroom import Page
    from dd_extraction.ollama import make_ollama_classifier

    replies = [httpx.Response(429, json={}),
               httpx.Response(200, json={"message": {"content": '{"is_financial_statement": true, '
                                                                '"confidence": "high"}'},
                                         "prompt_eval_count": 700, "eval_count": 20})]

    def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0)

    stats = RunStats()
    classify = make_ollama_classifier(
        client=httpx.Client(base_url="https://ollama.com", transport=httpx.MockTransport(handler)),
        stats=stats)
    classify(Page("a.pdf", 2, "Balance sheet as at 31 December 2025", None))
    # A 429 the retry recovered from counts as a retry, not a failure: failed_calls
    # means calls that ultimately failed.
    assert (stats.model_calls, stats.retried_calls, stats.failed_calls) == (1, 1, 0)
    assert (stats.prompt_tokens, stats.completion_tokens) == (700, 20)


# ------------------------------------------------------------------ end to end
def test_the_cli_logs_its_run(tmp_path: Path, monkeypatch):
    import dd_extraction.cli as cli
    from dd_extraction.schema import PageClassification

    root = tmp_path / "dataroom"
    make_pdf(root / "accounts.pdf", [["Acme Ltd", "Balance sheet as at 31 December 2025",
                                      "Net assets 1,530,000"]])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(cli, "make_claude_classifier", lambda model, stats=None: (
        lambda page: PageClassification(is_financial_statement=True, confidence="high")))

    out = tmp_path / "out" / "identification.json"
    assert cli.main([str(root), "--out", str(out)]) == 0

    written = json.loads((out.parent / DEFAULT_LOG_NAME).read_text())
    assert written["command"] == "dd-identify" and written["shape"]["status"] == "pass"
    assert written["output"]["path"] == str(out) and written["input"]["dataroom"] == str(root.resolve())
    assert "runs" in summarise(out.parent / DEFAULT_LOG_NAME)


def test_summary_table_shows_each_run_and_totals(tmp_path: Path):
    stats = RunStats(model_calls=3, prompt_tokens=1000, completion_tokens=200)
    with logger_for(tmp_path, stats=stats) as log:
        log.finish(tmp_path / "report.json", IDENTIFY_OK, "ok")
    with logger_for(tmp_path, "extract") as log:
        log.finish(tmp_path / "r.json", {**EXTRACT_OK, "currencies": ["USD"]}, "ok")

    table = summarise(tmp_path / DEFAULT_LOG_NAME)
    assert "dd-identify" in table and "dd-extract" in table
    assert "pass" in table and "fail" in table
    assert "2 runs" in table and "3 model calls" in table and "1,200 tokens" in table
    assert "not priced" in table
