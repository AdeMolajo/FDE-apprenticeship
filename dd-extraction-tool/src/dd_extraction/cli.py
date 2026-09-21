"""Command line entry point: dd-identify <dataroom> --out <report.json>"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

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
    if dataroom in out.parents:
        parser.error("--out must be outside the data room; the data room is read only")

    report = identify_financial_statements(
        dataroom, make_claude_classifier(model=args.model), model=args.model
    )
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
