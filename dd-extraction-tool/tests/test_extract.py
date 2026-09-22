from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import httpx
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from dd_extraction.cli_extract import main as extract_main
from dd_extraction.dataroom import load_pages
from dd_extraction.extract import (
    DEFAULT_TARGET_METRICS,
    ExtractionError,
    build_system_prompt,
    build_user_text,
    extract_figures,
    make_ollama_extractor,
    parse_answer,
)
from dd_extraction.identify import ProviderAuthError
from dd_extraction.schema import (
    ExtractedMetric,
    FinancialStatementFile,
    IdentificationReport,
    IdentifiedPage,
    PageExtraction,
)

BALANCE_SHEET = [
    "Acme Holdings Ltd",
    "Balance sheet as at 31 December 2025",
    "Revenue 1,250,000",
    "Net profit 340,000",
    "Total assets 2,100,000",
]


def make_pdf(path: Path, pages: List[Optional[List[str]]]) -> None:
    """Write a PDF. A None page is left blank, standing in for a scan."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for lines in pages:
        for i, line in enumerate(lines or []):
            pdf.drawString(72, 800 - 18 * i, line)
        pdf.showPage()
    pdf.save()


@pytest.fixture
def dataroom(tmp_path: Path) -> Path:
    root = tmp_path / "dataroom"
    make_pdf(root / "01 Financials" / "FY25 accounts.pdf", [["Cover page"], BALANCE_SHEET, ["Scan page"]])
    return root


@pytest.fixture
def identification(dataroom: Path) -> IdentificationReport:
    """A step-1 report that flagged page 2 of the FY25 accounts as a financial statement."""
    return IdentificationReport(
        dataroom=str(dataroom),
        model="claude-haiku-4-5",
        files_scanned=1,
        pages_classified=3,
        financial_statement_files=[
            FinancialStatementFile(
                source_document="01 Financials/FY25 accounts.pdf",
                pages=[
                    IdentifiedPage(
                        source_document="01 Financials/FY25 accounts.pdf",
                        source_page=2,
                        confidence="high",
                    )
                ],
            )
        ],
        skipped=[],
    )


def keyword_extractor(page) -> PageExtraction:
    """Stand-in for the model."""
    if "Revenue" not in page.text:
        return PageExtraction(figures=[])
    return PageExtraction(
        figures=[
            ExtractedMetric(metric="revenue", value=1250000, currency="GBP", confidence="high"),
            ExtractedMetric(metric="net_profit", value=340000, currency="GBP", confidence="high"),
        ]
    )


def test_only_pages_identification_flagged_are_processed(dataroom: Path, identification):
    calls = []

    def counting(page):
        calls.append(page.source_page)
        return keyword_extractor(page)

    report = extract_figures(dataroom, identification, counting)

    assert calls == [2]  # one call for the one identified page, not all three pages of the file
    assert report.pages_processed == 1
    assert len(report.figures) == 2
    assert report.skipped == []


def test_traceability_fields_come_from_the_identification_report_not_the_model(
    dataroom: Path, identification
):
    """The model never states source_document/source_page (Topic 4) -- code fills them in."""
    report = extract_figures(dataroom, identification, keyword_extractor)

    for figure in report.figures:
        assert figure.source_document == "01 Financials/FY25 accounts.pdf"
        assert figure.source_page == 2


def test_metric_not_present_on_the_page_is_not_reported(dataroom: Path, identification):
    def blank_extractor(page):
        return PageExtraction(figures=[])

    report = extract_figures(dataroom, identification, blank_extractor)
    assert report.pages_processed == 1
    assert report.figures == []


def test_extraction_failure_is_skipped_not_fatal(dataroom: Path, identification):
    def flaky(page):
        raise ExtractionError("no JSON in response")

    report = extract_figures(dataroom, identification, flaky)
    assert report.figures == []
    [skip] = report.skipped
    assert skip.source_document == "01 Financials/FY25 accounts.pdf"
    assert skip.source_page == 2
    assert skip.reason.startswith("extraction failed")


def test_auth_error_stops_the_run_instead_of_skipping_every_page(dataroom: Path, identification):
    def rejected(page):
        raise ProviderAuthError("Ollama rejected the API key (401)")

    with pytest.raises(ProviderAuthError):
        extract_figures(dataroom, identification, rejected)


def test_scanned_page_is_rejected_by_the_ollama_extractor():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("scanned pages must not be sent to Ollama")

    client = httpx.Client(base_url="https://ollama.com", transport=httpx.MockTransport(handler))
    extract = make_ollama_extractor(client=client)
    from dd_extraction.dataroom import Page

    scanned = Page("scan.pdf", 1, "", b"%PDF-1.4")
    with pytest.raises(ExtractionError, match="scanned") as exc:
        extract(scanned)
    # dd-extract has no Claude provider, so the message must not point to one.
    assert "Claude" not in str(exc.value) and "OCR" in str(exc.value)


def test_system_prompt_lists_target_metrics_and_schema():
    prompt = build_system_prompt(["revenue", "net_profit"], ["GBP", "SEK"])
    assert "revenue" in prompt and "net_profit" in prompt
    assert "GBP, SEK" in prompt and "NOT_STATED" in prompt
    assert "untrusted data" in prompt
    assert '"figures"' in prompt  # the JSON schema is embedded, Ollama Cloud doesn't enforce it


def test_parses_plain_and_fenced_json():
    reply = '{"figures": [{"metric": "revenue", "value": 100.0, "currency": "GBP", "confidence": "high"}]}'
    assert parse_answer(reply).figures[0].metric == "revenue"
    fenced = f"```json\n{reply}\n```"
    assert parse_answer(fenced).figures[0].metric == "revenue"


def test_unusable_answer_is_an_error():
    with pytest.raises(ExtractionError):
        parse_answer("Sure, here are the figures you asked for.")


def test_one_ollama_call_per_page_with_schema_and_untrusted_framing(dataroom: Path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"figures": []}'}})

    client = httpx.Client(base_url="https://ollama.com", transport=httpx.MockTransport(handler))
    extract = make_ollama_extractor(client=client)
    pages = list(load_pages(dataroom, dataroom / "01 Financials" / "FY25 accounts.pdf"))
    page = pages[1]  # the balance-sheet page

    extract(page)
    [body] = calls
    assert body["model"] == "gpt-oss:20b"
    assert body["stream"] is False
    assert "figures" in body["format"]["properties"]
    assert "tools" not in body
    assert "<page_content>" in body["messages"][1]["content"] and "Revenue" in body["messages"][1]["content"]


def test_rejected_key_raises_provider_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.Client(base_url="https://ollama.com", transport=httpx.MockTransport(handler))
    extract = make_ollama_extractor(client=client)
    from dd_extraction.dataroom import Page

    with pytest.raises(ProviderAuthError):
        extract(Page("f.pdf", 2, "Revenue 100", None))


def test_server_error_is_an_extraction_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    client = httpx.Client(base_url="https://ollama.com", transport=httpx.MockTransport(handler))
    extract = make_ollama_extractor(client=client)
    from dd_extraction.dataroom import Page

    with pytest.raises(ExtractionError):
        extract(Page("f.pdf", 2, "Revenue 100", None))


def test_cli_requires_an_identification_report(dataroom: Path, tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    with pytest.raises(SystemExit):
        extract_main(
            [
                str(dataroom),
                "--identification",
                str(tmp_path / "missing.json"),
                "--out",
                str(tmp_path / "figures.json"),
            ]
        )


def test_cli_will_not_write_inside_the_dataroom(dataroom: Path, tmp_path, identification, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    report_path = tmp_path / "identification.json"
    report_path.write_text(identification.model_dump_json())
    with pytest.raises(SystemExit):
        extract_main(
            [str(dataroom), "--identification", str(report_path), "--out", str(dataroom / "figures.json")]
        )


def test_cli_rejects_missing_ollama_key_for_cloud_host(dataroom, tmp_path, identification, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    report_path = tmp_path / "identification.json"
    report_path.write_text(identification.model_dump_json())
    with pytest.raises(SystemExit):
        extract_main(
            [str(dataroom), "--identification", str(report_path), "--out", str(tmp_path / "figures.json")]
        )


def test_cli_valid_run_writes_the_report(dataroom: Path, tmp_path, identification, monkeypatch):
    import dd_extraction.cli_extract as cli_extract

    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    monkeypatch.setattr(
        cli_extract,
        "make_ollama_extractor",
        lambda model, host, api_key, target_metrics=None, currencies=None: keyword_extractor,
    )

    report_path = tmp_path / "identification.json"
    report_path.write_text(identification.model_dump_json())
    out = tmp_path / "figures.json"

    assert extract_main([str(dataroom), "--identification", str(report_path), "--out", str(out)]) == 0
    assert out.exists()
    assert "revenue" in out.read_text()


def figure_extractor(currency: str):
    """Stand-in that reports revenue in the given currency."""

    def extract(page) -> PageExtraction:
        return PageExtraction(
            figures=[ExtractedMetric(metric="revenue", value=1250000, currency=currency, confidence="high")]
        )

    return extract


def test_unaccepted_currency_is_skipped_not_relabelled(dataroom: Path, identification):
    report = extract_figures(dataroom, identification, figure_extractor("SEK"))

    assert report.figures == []
    [skip] = report.skipped
    assert (skip.source_document, skip.source_page) == ("01 Financials/FY25 accounts.pdf", 2)
    assert "SEK" in skip.reason and "--currencies" in skip.reason
    assert report.currencies == ["GBP", "USD", "EUR"]


def test_currencies_option_accepts_an_extra_currency(dataroom: Path, identification):
    report = extract_figures(dataroom, identification, figure_extractor("SEK"), currencies=["GBP", "SEK"])

    [figure] = report.figures
    assert figure.currency == "SEK"
    assert report.currencies == ["GBP", "SEK"]


def test_not_stated_currency_is_kept(dataroom: Path, identification):
    report = extract_figures(dataroom, identification, figure_extractor("NOT_STATED"))

    [figure] = report.figures
    assert figure.currency == "NOT_STATED"


def test_output_schema_does_not_force_a_currency_from_the_list():
    from dd_extraction.extract import SCHEMA

    currency = SCHEMA["$defs"]["ExtractedMetric"]["properties"]["currency"]
    assert "enum" not in currency and currency["pattern"] == "^([A-Z]{3}|NOT_STATED)$"


def test_invalid_currency_code_in_answer_is_an_error():
    with pytest.raises(ExtractionError):
        parse_answer('{"figures": [{"metric": "revenue", "value": 1.0, "currency": "pounds", "confidence": "high"}]}')


def test_cli_rejects_a_malformed_currencies_option(dataroom: Path, tmp_path, identification, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    report_path = tmp_path / "identification.json"
    report_path.write_text(identification.model_dump_json())
    with pytest.raises(SystemExit):
        extract_main(
            [str(dataroom), "--identification", str(report_path), "--out", str(tmp_path / "f.json"),
             "--currencies", "GBP,pounds"]
        )


def test_cli_passes_currencies_through(dataroom: Path, tmp_path, identification, monkeypatch):
    import dd_extraction.cli_extract as cli_extract

    seen = {}

    def fake_factory(model, host, api_key, target_metrics=None, currencies=None):
        seen["currencies"] = currencies
        return figure_extractor("SEK")

    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    monkeypatch.setattr(cli_extract, "make_ollama_extractor", fake_factory)
    report_path = tmp_path / "identification.json"
    report_path.write_text(identification.model_dump_json())
    out = tmp_path / "f.json"

    assert extract_main(
        [str(dataroom), "--identification", str(report_path), "--out", str(out), "--currencies", "gbp, sek"]
    ) == 0
    assert seen["currencies"] == ["GBP", "SEK"]
    assert '"currency": "SEK"' in out.read_text()
