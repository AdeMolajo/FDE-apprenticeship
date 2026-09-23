from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from dd_extraction.cli import main
from dd_extraction.dataroom import load_pages, walk_dataroom
from dd_extraction.identify import (
    SYSTEM_PROMPT,
    ClassificationError,
    identify_financial_statements,
    make_claude_classifier,
)
from dd_extraction.schema import PageClassification

BALANCE_SHEET = [
    "Acme Holdings Ltd",
    "Balance sheet as at 31 December 2025",
    "Fixed assets 1,200,000",
    "Current assets 640,000",
    "Net assets 1,530,000",
]
NARRATIVE = [
    "Management commentary",
    "Trading in the year was strong and the board is pleased with progress.",
]

YES = PageClassification(is_financial_statement=True, confidence="high")
NO = PageClassification(is_financial_statement=False, confidence="high")


def make_pdf(path: Path, pages: List[Optional[List[str]]]) -> None:
    """Write a PDF. A None page is left blank, standing in for a scan."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for lines in pages:
        for i, line in enumerate(lines or []):
            pdf.drawString(72, 800 - 18 * i, line)
        pdf.showPage()
    pdf.save()


def keyword_classifier(page) -> PageClassification:
    """Stand-in for the model."""
    return YES if "Balance sheet" in page.text else NO


@pytest.fixture
def dataroom(tmp_path: Path) -> Path:
    root = tmp_path / "dataroom"
    make_pdf(root / "01 Financials" / "FY25 accounts.pdf", [NARRATIVE, BALANCE_SHEET])
    make_pdf(root / "02 Commercial" / "overview.pdf", [NARRATIVE])
    make_pdf(root / "03 Scans" / "scan.pdf", [None])
    (root / "02 Commercial" / "notes.txt").write_text("not a pdf")
    (root / ".DS_Store").write_text("")
    return root


def test_walk_finds_pdfs_in_subfolders(dataroom: Path):
    pdfs, others = walk_dataroom(dataroom)
    assert [p.relative_to(dataroom).as_posix() for p in pdfs] == [
        "01 Financials/FY25 accounts.pdf",
        "02 Commercial/overview.pdf",
        "03 Scans/scan.pdf",
    ]
    assert [(p.name, reason) for p, reason in others] == [("notes.txt", "not a PDF")]


def test_report_lists_only_financial_statement_files(dataroom: Path):
    report = identify_financial_statements(dataroom, keyword_classifier)

    assert report.files_scanned == 3
    assert report.pages_classified == 4
    [found] = report.financial_statement_files
    assert found.source_document == "01 Financials/FY25 accounts.pdf"
    assert [(p.source_document, p.source_page, p.confidence) for p in found.pages] == [
        ("01 Financials/FY25 accounts.pdf", 2, "high")
    ]
    assert [s.source_document for s in report.skipped] == ["02 Commercial/notes.txt"]


def test_failed_and_unreadable_items_are_reported(dataroom: Path):
    (dataroom / "broken.pdf").write_bytes(b"not really a pdf")

    def flaky(page):
        if page.pdf_bytes is not None:
            raise ClassificationError("no classification (stop_reason=max_tokens)")
        return keyword_classifier(page)

    report = identify_financial_statements(dataroom, flaky)
    reasons = {(s.source_document, s.source_page): s.reason for s in report.skipped}
    assert reasons[("broken.pdf", None)].startswith("unreadable PDF")
    assert reasons[("03 Scans/scan.pdf", 1)].startswith("classification failed")


class FakeMessages:
    def __init__(self, parsed: Optional[PageClassification], stop_reason: str = "end_turn"):
        self.parsed, self.stop_reason, self.calls = parsed, stop_reason, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.parsed)


def test_one_constrained_call_per_text_page(dataroom: Path):
    messages = FakeMessages(NO)
    classify = make_claude_classifier(SimpleNamespace(messages=messages))
    [page] = load_pages(dataroom, dataroom / "02 Commercial" / "overview.pdf")

    assert classify(page) == NO
    [call] = messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_format"] is PageClassification
    assert "tools" not in call
    text = call["messages"][0]["content"][0]["text"]
    assert "<page_content>" in text and "Trading in the year" in text


def test_scanned_page_is_sent_as_a_pdf(dataroom: Path):
    messages = FakeMessages(NO)
    classify = make_claude_classifier(SimpleNamespace(messages=messages))
    [page] = load_pages(dataroom, dataroom / "03 Scans" / "scan.pdf")

    classify(page)
    block = messages.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"


def test_refused_or_truncated_response_is_an_error(dataroom: Path):
    classify = make_claude_classifier(
        SimpleNamespace(messages=FakeMessages(None, stop_reason="refusal"))
    )
    [page] = load_pages(dataroom, dataroom / "02 Commercial" / "overview.pdf")
    with pytest.raises(ClassificationError):
        classify(page)


def test_cli_will_not_write_inside_the_dataroom(dataroom: Path):
    with pytest.raises(SystemExit):
        main([str(dataroom), "--out", str(dataroom / "report.json")])


def test_cli_rejects_a_missing_dataroom(tmp_path: Path):
    with pytest.raises(SystemExit):
        main([str(tmp_path / "missing"), "--out", str(tmp_path / "report.json")])


def test_bad_api_key_stops_the_run(dataroom: Path, tmp_path: Path, monkeypatch, capsys):
    import anthropic
    import httpx

    import dd_extraction.cli as cli

    def rejected(page):
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com"))
        raise anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invalid")
    monkeypatch.setattr(cli, "make_claude_classifier", lambda model, stats=None: rejected)
    out = tmp_path / "report.json"

    assert main([str(dataroom), "--out", str(out)]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not out.exists()


def test_cli_rejects_a_missing_api_key(dataroom: Path, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main([str(dataroom), "--out", str(tmp_path / "report.json")])


@pytest.fixture
def counting_classifier(monkeypatch):
    """Patch the CLI's Claude classifier with one that records every page it sees."""
    import dd_extraction.cli as cli

    calls = []

    def classify(page):
        calls.append(page)
        return NO

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(cli, "make_claude_classifier", lambda model, stats=None: classify)
    return calls


def test_out_as_a_folder_fails_before_any_model_call(dataroom, tmp_path, counting_classifier):
    folder = tmp_path / "reports"
    folder.mkdir()
    with pytest.raises(SystemExit):
        main([str(dataroom), "--out", str(folder)])
    assert counting_classifier == []


def test_out_under_a_file_fails_before_any_model_call(dataroom, tmp_path, counting_classifier):
    blocker = tmp_path / "notes.txt"
    blocker.write_text("")
    with pytest.raises(SystemExit):
        main([str(dataroom), "--out", str(blocker / "report.json")])
    assert counting_classifier == []


def test_unwritable_out_fails_before_any_model_call(dataroom, tmp_path, counting_classifier):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(SystemExit):
            main([str(dataroom), "--out", str(locked / "report.json")])
    finally:
        locked.chmod(0o700)
    assert counting_classifier == []


def test_valid_out_writes_the_report(dataroom, tmp_path, counting_classifier):
    out = tmp_path / "new" / "report.json"
    assert main([str(dataroom), "--out", str(out)]) == 0
    assert out.exists() and len(counting_classifier) == 4
