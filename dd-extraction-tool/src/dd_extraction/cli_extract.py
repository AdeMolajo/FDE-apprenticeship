"""Command line entry point: dd-extract <dataroom> --identification <report.json> --out <figures.json>

Takes the identification report ``dd-identify`` already produced and extracts the
target figures from the pages it flagged, one Ollama call per page. Kept as its
own script, mirroring ``dd-identify``, rather than folded into one multi-purpose
command.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from .extract import (
    DEFAULT_HOST,
    DEFAULT_MODEL,
    DEFAULT_TARGET_METRICS,
    extract_figures,
    make_ollama_extractor,
)
from .identify import ProviderAuthError
from .schema import IdentificationReport


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract specific figures from data-room pages already identified as "
            "financial statements (one Ollama call per page)."
        )
    )
    parser.add_argument("dataroom", type=Path, help="data-room folder (read only)")
    parser.add_argument(
        "--identification",
        type=Path,
        required=True,
        help="identification.json produced by dd-identify",
    )
    parser.add_argument("--out", type=Path, required=True, help="JSON report to write")
    parser.add_argument(
        "--host",
        help=f"Ollama host (default: {DEFAULT_HOST}, or $OLLAMA_HOST)",
    )
    parser.add_argument("--model", help=f"model name (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--metrics",
        help="comma-separated metrics to extract (default: " + ", ".join(DEFAULT_TARGET_METRICS) + ")",
    )
    args = parser.parse_args(argv)

    dataroom = args.dataroom.resolve()
    out = args.out.resolve()
    if not dataroom.is_dir():
        parser.error(f"data room folder not found: {dataroom}")
    # Read and write access stay separate (Gate 3, Topic 5), same rule as dd-identify.
    if dataroom in out.parents:
        parser.error("--out must be outside the data room; the data room is read only")
    if out.is_dir():
        parser.error(f"--out is a folder; give a file path such as {out / 'extraction.json'}")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error(f"cannot create the folder for --out: {exc}")
    if not os.access(out.parent, os.W_OK) or (out.exists() and not os.access(out, os.W_OK)):
        parser.error(f"--out is not writable: {out}")

    if not args.identification.is_file():
        parser.error(f"identification report not found: {args.identification}")
    try:
        identification = IdentificationReport.model_validate_json(args.identification.read_text())
    except ValueError as exc:
        parser.error(f"identification report is not valid: {exc}")

    host = args.host or os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
    api_key = os.environ.get("OLLAMA_API_KEY")
    if not api_key and "ollama.com" in host:
        parser.error("OLLAMA_API_KEY is not set (needed for Ollama Cloud)")

    model = args.model or DEFAULT_MODEL
    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()] if args.metrics else None
    extract = make_ollama_extractor(model=model, host=host, api_key=api_key, target_metrics=metrics)

    try:
        report = extract_figures(dataroom, identification, extract, model=f"ollama/{model}", target_metrics=metrics)
    except ProviderAuthError as exc:
        print(f"error: Ollama rejected the key ({exc}). Check OLLAMA_API_KEY.", file=sys.stderr)
        return 1

    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")

    print(
        f"Processed {report.pages_processed} pages, extracted {len(report.figures)} figures; "
        f"{len(report.skipped)} pages skipped."
    )
    for figure in report.figures:
        print(
            f"  {figure.source_document} p{figure.source_page}: {figure.metric} = "
            f"{figure.value} {figure.currency} ({figure.confidence})"
        )
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
