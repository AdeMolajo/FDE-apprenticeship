"""Output schemas for the financial statement identification step (Gate 3, pipeline step 1).

Two layers, deliberately separated:

* ``PageClassification`` is the only thing the model is allowed to produce. It is
  passed to the API as the structured-output format, so the response is
  constrained to it and then validated again in code (Gate 3, Topic 3).
* The traceability fields (``source_document``, ``source_page``) come from Gate 3,
  Topic 4. They are attached by code from the file walk, never taken from the
  model, so a model error or an injected instruction cannot cite the wrong page.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]

StatementType = Literal[
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
    "statement_of_changes_in_equity",
]


class PageClassification(BaseModel):
    """What the model returns for one page. Nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")

    is_financial_statement: bool = Field(
        description="True only if the page contains a primary financial statement."
    )
    statement_types: List[StatementType] = Field(
        description="Which primary statements appear on the page. Empty when "
        "is_financial_statement is false."
    )
    confidence: Confidence = Field(description="How sure the classification is.")


class IdentifiedPage(BaseModel):
    """One page classified as a financial statement, with its source reference."""

    source_document: str = Field(
        description="Path of the file relative to the data-room root."
    )
    source_page: int = Field(ge=1, description="1-indexed page number in source_document.")
    statement_types: List[StatementType]
    confidence: Confidence


class FinancialStatementFile(BaseModel):
    """A data-room file containing at least one financial statement page."""

    source_document: str
    page_count: int = Field(ge=1)
    pages: List[IdentifiedPage]


class SkippedItem(BaseModel):
    """A file or page that was not classified, and why. Nothing is dropped silently."""

    source_document: str
    source_page: Optional[int] = None
    reason: str


class IdentificationReport(BaseModel):
    """Full output of the identification step, written to the output location."""

    dataroom: str
    model: str
    generated_at: str
    files_scanned: int
    pages_classified: int
    financial_statement_files: List[FinancialStatementFile]
    skipped: List[SkippedItem]
