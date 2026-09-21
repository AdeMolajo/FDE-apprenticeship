"""Read-only access to a data room: find the PDFs and load their pages.

This module is the pipeline's read boundary (Gate 3, Topic 5). It only opens files
for reading, never follows symlinks, and never touches anything outside the
data-room root.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from pypdf import PdfReader, PdfWriter

# Pages with less extracted text than this are treated as scanned images, and the
# page itself is sent to the model instead of its (missing) text layer.
MIN_TEXT_CHARS = 50

SUPPORTED_SUFFIXES = {".pdf"}


@dataclass(frozen=True)
class Page:
    source_document: str  # path relative to the data-room root
    source_page: int  # 1-indexed
    text: str
    pdf_bytes: Optional[bytes]  # single-page PDF, set only when text is too thin

    @property
    def is_scanned(self) -> bool:
        return self.pdf_bytes is not None


def walk_dataroom(root: Path) -> Tuple[List[Path], List[Tuple[Path, str]]]:
    """Return (supported files, [(unsupported file, reason)]), in a stable order."""
    root = root.resolve()
    supported: List[Path] = []
    unsupported: List[Tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                unsupported.append((path, "symlink not followed"))
            elif path.suffix.lower() in SUPPORTED_SUFFIXES:
                supported.append(path)
            else:
                unsupported.append((path, f"unsupported file type '{path.suffix or 'none'}'"))
    return supported, unsupported


def load_pages(root: Path, path: Path) -> Iterator[Page]:
    """Yield every page of a PDF with its text, or the page itself if scanned."""
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    reader = PdfReader(str(path))
    for index, pdf_page in enumerate(reader.pages):
        text = (pdf_page.extract_text() or "").strip()
        pdf_bytes = None
        if len(text) < MIN_TEXT_CHARS:
            writer = PdfWriter()
            writer.add_page(pdf_page)
            buffer = io.BytesIO()
            writer.write(buffer)
            pdf_bytes = buffer.getvalue()
        yield Page(relative, index + 1, text, pdf_bytes)


def page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)
