"""Command line entry point: dd-identify <dataroom> --out <report.json>"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

import anthropic

from .identify import DEFAULT_MODEL, identify_financial_statements, make_claude_classifier


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="List the files in a data room that contain financial statements."
    )
    parser.add_argument("dataroom", type=Path, help="data-room folder (read only)")
    parser.add_argument("--out", type=Path, required=True, help="JSON report to write")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    args = parser.parse_args(argv)

    dataroom = args.dataroom.resolve()
    out = args.out.resolve()
    # Read and write access stay separate (Gate 3, Topic 5).
    if not dataroom.is_dir():
        parser.error(f"data room folder not found: {dataroom}")
    if dataroom in out.parents:
        parser.error("--out must be outside the data room; the data room is read only")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        parser.error("ANTHROPIC_API_KEY is not set")

    try:
        report = identify_financial_statements(
            dataroom, make_claude_classifier(model=args.model), model=args.model
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        print(f"error: the Anthropic API rejected the key ({exc.status_code}). "
              "Check that ANTHROPIC_API_KEY is set to a valid key.", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")

    print(
        f"Scanned {report.files_scanned} PDFs, {report.pages_classified} pages. "
        f"{len(report.financial_statement_files)} files contain financial statements; "
        f"{len(report.skipped)} items skipped."
    )
    for f in report.financial_statement_files:
        print(f"  {f.source_document} (pages {', '.join(str(p.source_page) for p in f.pages)})")
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
