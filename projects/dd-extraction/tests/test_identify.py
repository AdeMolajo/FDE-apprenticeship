from __future__ import annotations

import json
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
    "Creditors due within one year (310,000)",
    "Net assets 1,530,000",
]
NARRATIVE = [
    "Management commentary",
    "Trading in the year was strong and the board is pleased with progress.",
    "We expect continued growth across all regions next year.",
]


def make_pdf(path: Path, pages: List[Optional[List[str]]]) -> None:
    """Write a PDF; a None page is left blank, standing in for a scanned image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines or []:
            pdf.drawString(72, y, line)
            y -= 18
        pdf.showPage()
    pdf.save()


def keyword_classifier(page) -> PageClassification:
    """Stand-in for the model: 'Balance sheet' in the text means a statement."""
    if "Balance sheet" in page.text:
        return PageClassification(
            is_financial_statement=True, statement_types=["balance_sheet"], confidence="high"
        )
    return PageClassification(is_financial_statement=False, statement_types=[], confidence="high")


@pytest.fixture
def dataroom(tmp_path: Path) -> Path:
    root = tmp_path / "dataroom"
    make_pdf(root / "01 Financials" / "FY25 accounts.pdf", [NARRATIVE, BALANCE_SHEET])
    make_pdf(root / "02 Commercial" / "overview.pdf", [NARRATIVE])
    make_pdf(root / "03 Scans" / "scan.pdf", [None])
    (root / "02 Commercial" / "notes.txt").write_text("not a pdf")
    (root / ".DS_Store").write_text("")
    return root


def test_walk_finds_pdfs_and_reports_everything_else(dataroom: Path):
    pdfs, unsupported = walk_dataroom(dataroom)
    assert [p.relative_to(dataroom).as_posix() for p in pdfs] == [
        "01 Financials/FY25 accounts.pdf",
        "02 Commercial/overview.pdf",
        "03 Scans/scan.pdf",
    ]
    assert [(p.name, reason) for p, reason in unsupported] == [
        ("notes.txt", "unsupported file type '.txt'")
    ]


def test_blank_page_is_sent_as_pdf_not_text(dataroom: Path):
    [page] = list(load_pages(dataroom, dataroom / "03 Scans" / "scan.pdf"))
    assert page.is_scanned and page.pdf_bytes.startswith(b"%PDF")
    [page] = list(load_pages(dataroom, dataroom / "02 Commercial" / "overview.pdf"))
    assert not page.is_scanned


def test_report_lists_only_statement_files_with_source_pages(dataroom: Path):
    report = identify_financial_statements(dataroom, keyword_classifier)

    assert report.files_scanned == 3
    assert report.pages_classified == 4
    [found] = report.financial_statement_files
    assert found.source_document == "01 Financials/FY25 accounts.pdf"
    assert found.page_count == 2
    [page] = found.pages
    assert (page.source_document, page.source_page) == ("01 Financials/FY25 accounts.pdf", 2)
    assert page.statement_types == ["balance_sheet"]
    assert [s.source_document for s in report.skipped] == ["02 Commercial/notes.txt"]


def test_failed_page_is_recorded_not_dropped(dataroom: Path):
    def flaky(page):
        if page.is_scanned:
            raise ClassificationError("no classification (stop_reason=max_tokens)")
        return keyword_classifier(page)

    report = identify_financial_statements(dataroom, flaky)
    failed = [s for s in report.skipped if s.source_page is not None]
    assert [(s.source_document, s.source_page) for s in failed] == [("03 Scans/scan.pdf", 1)]
    assert report.pages_classified == 3


def test_unreadable_pdf_is_skipped(dataroom: Path):
    (dataroom / "broken.pdf").write_bytes(b"not really a pdf")
    report = identify_financial_statements(dataroom, keyword_classifier)
    assert any(
        s.source_document == "broken.pdf" and s.reason.startswith("unreadable PDF")
        for s in report.skipped
    )


class FakeMessages:
    def __init__(self, parsed: Optional[PageClassification], stop_reason: str = "end_turn"):
        self.parsed, self.stop_reason, self.calls = parsed, stop_reason, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.parsed)


def test_claude_classifier_makes_one_constrained_call_per_page(dataroom: Path):
    messages = FakeMessages(
        PageClassification(
            is_financial_statement=False, statement_types=["balance_sheet"], confidence="low"
        )
    )
    classify = make_claude_classifier(SimpleNamespace(messages=messages))
    [page] = list(load_pages(dataroom, dataroom / "02 Commercial" / "overview.pdf"))

    result = classify(page)

    [call] = messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_format"] is PageClassification
    assert "tools" not in call
    text = call["messages"][0]["content"][0]["text"]
    assert "<page_content>" in text and "Trading in the year" in text
    # A 'no' answer never carries statement types, whatever the model said.
    assert result.statement_types == []


def test_claude_classifier_sends_scanned_page_as_document(dataroom: Path):
    messages = FakeMessages(
        PageClassification(is_financial_statement=False, statement_types=[], confidence="low")
    )
    classify = make_claude_classifier(SimpleNamespace(messages=messages))
    [page] = list(load_pages(dataroom, dataroom / "03 Scans" / "scan.pdf"))
    classify(page)
    content = messages.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"


def test_claude_classifier_rejects_truncated_or_refused_output(dataroom: Path):
    classify = make_claude_classifier(
        SimpleNamespace(messages=FakeMessages(None, stop_reason="refusal"))
    )
    [page] = list(load_pages(dataroom, dataroom / "02 Commercial" / "overview.pdf"))
    with pytest.raises(ClassificationError):
        classify(page)


def test_cli_refuses_to_write_inside_the_dataroom(dataroom: Path):
    with pytest.raises(SystemExit):
        main([str(dataroom), "--out", str(dataroom / "report.json")])


def test_report_json_round_trips(dataroom: Path, tmp_path: Path):
    report = identify_financial_statements(dataroom, keyword_classifier)
    data = json.loads(report.model_dump_json())
    assert data["financial_statement_files"][0]["pages"][0]["source_page"] == 2
