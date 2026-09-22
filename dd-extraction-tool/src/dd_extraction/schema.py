"""Schemas for the financial statement identification and extraction steps (Gate 3,
pipeline steps 1 and 2).

* ``PageClassification`` and ``PageExtraction`` are the only things the model
  produces for each step: the structured yes/no answer from Topic 2, step 1, and
  the list of figures for step 2, both using the ``confidence`` enum from Topic 4.
  They are sent to the API as the output format (step 1) or the requested JSON
  shape (step 2, Ollama) and re-validated in code (Topic 3).
* ``source_document`` and ``source_page`` are the required traceability fields
  from Topic 4, for both steps. Code fills them in from the file walk (step 1) or
  the identification report (step 2); the model never supplies them, so it cannot
  cite the wrong file or page. (An intermediate version of step 2 batched every
  identified page of a document into one call to save on model calls, which meant
  the model had to self-report ``source_page`` -- that was reverted back to one
  call per page specifically to restore this guarantee.)
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]

# ISO currency codes the extraction step will accept (Topic 4: "chosen from a
# defined list of ISO currency codes"). Extend this list if a new currency is needed.
Currency = Literal["USD", "GBP", "EUR"]


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


class ExtractedMetric(BaseModel):
    """The model's answer for one figure found on one page. Nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")

    metric: str = Field(description="One of the target metrics named in the system prompt.")
    value: float = Field(description="The figure as a plain number, no currency symbols or commas.")
    currency: Currency
    confidence: Confidence


class PageExtraction(BaseModel):
    """The model's answer for one page: every requested metric it actually found there."""

    model_config = ConfigDict(extra="forbid")

    figures: List[ExtractedMetric]


class ExtractedFigure(BaseModel):
    """One figure extracted from a financial statement page, traceable to its source."""

    metric: str
    value: float
    currency: Currency
    confidence: Confidence
    source_document: str = Field(description="File path relative to the data-room root.")
    source_page: int = Field(ge=1, description="1-indexed page number within source_document.")


class SkippedExtraction(BaseModel):
    """A page that could not be processed for extraction, and why."""

    source_document: str
    source_page: Optional[int] = None
    reason: str


class ExtractionReport(BaseModel):
    dataroom: str
    model: str
    target_metrics: List[str]
    pages_processed: int
    figures: List[ExtractedFigure]
    skipped: List[SkippedExtraction]
