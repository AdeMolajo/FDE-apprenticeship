"""Command line entry point: dd-identify <dataroom> --out <report.json>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

import anthropic

from .runlog import DEFAULT_LOG_NAME, RunLogger, RunStats
from .identify import (
    DEFAULT_MODEL,
    ProviderAuthError,
    identify_financial_statements,
    make_claude_classifier,
)
from .ollama import DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL, make_ollama_classifier


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="List the files in a data room that contain financial statements."
    )
    parser.add_argument("dataroom", type=Path, help="data-room folder (read only)")
    parser.add_argument("--out", type=Path, required=True, help="JSON report to write")
    parser.add_argument(
        "--provider",
        choices=["anthropic", "ollama"],
        default="anthropic",
        help="model provider (default: anthropic). ollama uses OLLAMA_HOST "
        f"(default {DEFAULT_OLLAMA_HOST}) and OLLAMA_API_KEY",
    )
    parser.add_argument(
        "--model",
        help=f"model name (default: {DEFAULT_MODEL} for anthropic, {DEFAULT_OLLAMA_MODEL} for ollama)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help=f"run log to append to (default: {DEFAULT_LOG_NAME} next to --out)",
    )
    args = parser.parse_args(argv)

    dataroom = args.dataroom.resolve()
    out = args.out.resolve()
    if not dataroom.is_dir():
        parser.error(f"data room folder not found: {dataroom}")
    # Read and write access stay separate (Gate 3, Topic 5).
    if dataroom in out.parents:
        parser.error("--out must be outside the data room; the data room is read only")
    # Check the report can be written before paying for any model calls.
    if out.is_dir():
        parser.error(f"--out is a folder; give a file path such as {out / 'identification.json'}")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error(f"cannot create the folder for --out: {exc}")
    if not os.access(out.parent, os.W_OK) or (out.exists() and not os.access(out, os.W_OK)):
        parser.error(f"--out is not writable: {out}")

    stats = RunStats()
    log_path = (args.log or out.parent / DEFAULT_LOG_NAME).resolve()
    if args.provider == "ollama":
        model = args.model or DEFAULT_OLLAMA_MODEL
        host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
        api_key = os.environ.get("OLLAMA_API_KEY")
        if not api_key and "ollama.com" in host:
            parser.error("OLLAMA_API_KEY is not set (needed for Ollama Cloud)")
        classify = make_ollama_classifier(model=model, host=host, api_key=api_key, stats=stats)
    else:
        model = args.model or DEFAULT_MODEL
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            parser.error("ANTHROPIC_API_KEY is not set")
        classify = make_claude_classifier(model=model, stats=stats)

    inputs = {"dataroom": str(dataroom), "provider": args.provider}
    with RunLogger("dd-identify", "identify", log_path, inputs, f"{args.provider}/{model}",
                   args.provider, stats) as logger:
        try:
            report = identify_financial_statements(dataroom, classify, model=f"{args.provider}/{model}")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, ProviderAuthError) as exc:
            key_var = "OLLAMA_API_KEY" if args.provider == "ollama" else "ANTHROPIC_API_KEY"
            logger.finish(None, None, "error", f"{type(exc).__name__}: {exc}")
            print(f"error: the {args.provider} API rejected the key ({exc}). Check {key_var}.", file=sys.stderr)
            return 1
        data = report.model_dump(mode="json")
        out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.finish(out, data, "ok")

    print(
        f"Scanned {report.files_scanned} PDFs, {report.pages_classified} pages. "
        f"{len(report.financial_statement_files)} files contain financial statements; "
        f"{len(report.skipped)} items skipped; {len(report.review)} flagged for review."
    )
    for flag in report.review:
        print(f"  REVIEW {flag.source_document} p{flag.source_page}: {flag.reason}")
    for f in report.financial_statement_files:
        print(f"  {f.source_document} (pages {', '.join(str(p.source_page) for p in f.pages)})")
    print(f"Report written to {out}")
    record = logger.record
    print(f"Run logged to {log_path}: shape {record['shape']['status']}, "
          f"{record['usage']['model_calls']} model calls, "
          f"{record['usage']['prompt_tokens'] + record['usage']['completion_tokens']:,} tokens"
          + (f", cost {record['cost']['amount']:.4f} {record['cost']['currency']}"
             if record["cost"]["amount"] is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
