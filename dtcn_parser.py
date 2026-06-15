"""Parse DTCN PDF documents into full text + page count."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pdfplumber


def parse_dtcn(local_path: str | Path) -> dict[str, Any]:
    """Extract full text and page count from a DTCN PDF.

    Returns {"raw_text": str, "page_count": int}.
    """
    path = Path(local_path)
    chunks: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text:
                chunks.append(text)

    return {"raw_text": "\n".join(chunks), "page_count": page_count}
