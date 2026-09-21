"""Schemas for the financial statement identification step (Gate 3, pipeline step 1).

* ``PageClassification`` is the only thing the model produces: the structured
  yes/no answer from Topic 2, step 1, plus the ``confidence`` enum from Topic 4.
  It is sent to the API as the output format and re-validated in code (Topic 3).
* ``source_document`` and ``source_page`` are the required traceability fields
  from Topic 4. Code fills them in from the file walk; the model never supplies
  them, so it cannot cite the wrong file or page.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]


class PageClassification(BaseModel):
    """The model's answer for one page. Nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")

    is_financial_statement: bool
    confidence: Confidence


class IdentifiedPage(BaseModel):
    """A page classified as a financial statement, traceable to its source."""

    source_document: str = Field(description="File path relative to the data-room root.")
    source_page: int = Field(ge=1, description="1-indexed page number within source_document.")
    confidence: Confidence


class FinancialStatementFile(BaseModel):
    """A data-room file with at least one financial statement page."""

    source_document: str
    pages: List[IdentifiedPage]


class SkippedItem(BaseModel):
    """A file or page that could not be classified, and why."""

    source_document: str
    source_page: Optional[int] = None
    reason: str


class IdentificationReport(BaseModel):
    dataroom: str
    model: str
    files_scanned: int
    pages_classified: int
    financial_statement_files: List[FinancialStatementFile]
    skipped: List[SkippedItem]
