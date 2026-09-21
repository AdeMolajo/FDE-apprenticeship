"""Read-only traversal of a data room (Gate 3, Topic 5: read access only).

Finds the PDFs under the data-room root and loads each page's content. Files are
only opened for reading, symlinks are not followed, and nothing outside the root
is touched.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from pypdf import PdfReader, PdfWriter

# Below this much extractable text a page is treated as a scan, and the page
# itself is sent to the model instead of its (empty) text layer.
MIN_TEXT_CHARS = 50


@dataclass(frozen=True)
class Page:
    source_document: str  # relative to the data-room root
    source_page: int  # 1-indexed
    text: str
    pdf_bytes: Optional[bytes]  # single-page PDF, only for scanned pages


def walk_dataroom(root: Path) -> Tuple[List[Path], List[Tuple[Path, str]]]:
    """Return (PDF files, [(other file, reason)]) under root, in a stable order."""
    pdfs: List[Path] = []
    others: List[Tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                others.append((path, "symlink not followed"))
            elif path.suffix.lower() == ".pdf":
                pdfs.append(path)
            else:
                others.append((path, "not a PDF"))
    return pdfs, others


def load_pages(root: Path, path: Path) -> Iterator[Page]:
    """Yield each page of a PDF with its text, or the page itself if it is a scan."""
    relative = path.relative_to(root).as_posix()
    for index, pdf_page in enumerate(PdfReader(str(path)).pages):
        text = (pdf_page.extract_text() or "").strip()
        pdf_bytes = None
        if len(text) < MIN_TEXT_CHARS:
            writer = PdfWriter()
            writer.add_page(pdf_page)
            buffer = io.BytesIO()
            writer.write(buffer)
            pdf_bytes = buffer.getvalue()
        yield Page(relative, index + 1, text, pdf_bytes)
