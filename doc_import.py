"""
Document import utilities — extract text or data from non-CSV files
so they can enter the app either as AI context docs or as data tables.

Supported import paths:
  PDF  → extract text → save as .md context doc
  DOCX → extract text → save as .md context doc
  TXT  → read as-is  → save as .md context doc
  XLSX / XLS → read sheets → convert each to a pandas DataFrame
               (caller saves each DataFrame as a CSV in data/tables/)

Each format is guarded by an availability check so the app starts
even if a dependency isn't installed — the UI surfaces the error only
when the user tries to use that specific format.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Availability flags — checked at call time, not import time
# ---------------------------------------------------------------------------
try:
    import pypdf as _pypdf  # type: ignore
    _PYPDF_OK = True
except ImportError:
    _PYPDF_OK = False

try:
    import docx as _docx  # type: ignore
    _DOCX_OK = True
except ImportError:
    _DOCX_OK = False

try:
    import openpyxl  # type: ignore  # noqa: F401
    _OPENPYXL_OK = True
except ImportError:
    _OPENPYXL_OK = False


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def extract_pdf_text(file_bytes: bytes) -> str:
    """Extract plain text from a PDF. Returns the concatenated page text.

    Raises RuntimeError if pypdf is not installed.
    Raises ValueError if no text could be extracted (e.g. scanned-image PDF).
    """
    if not _PYPDF_OK:
        raise RuntimeError(
            "pypdf is not installed. Run: pip install pypdf"
        )
    reader = _pypdf.PdfReader(io.BytesIO(file_bytes))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        stripped = text.strip()
        if stripped:
            pages.append(stripped)
    if not pages:
        raise ValueError(
            "No text could be extracted. This PDF may contain only images "
            "(scanned pages). Try a PDF with selectable text."
        )
    return "\n\n".join(pages)


def extract_docx_text(file_bytes: bytes) -> str:
    """Extract paragraph text from a .docx file.

    Raises RuntimeError if python-docx is not installed.
    """
    if not _DOCX_OK:
        raise RuntimeError(
            "python-docx is not installed. Run: pip install python-docx"
        )
    doc = _docx.Document(io.BytesIO(file_bytes))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


def extract_txt_text(file_bytes: bytes) -> str:
    """Decode plain text with an encoding fallback chain."""
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Excel → DataFrames
# ---------------------------------------------------------------------------
def read_excel_sheets(file_bytes: bytes) -> dict[str, pd.DataFrame]:
    """Read every sheet from an Excel workbook.

    Returns a dict of {sheet_name: DataFrame}. Empty sheets are included
    (with an empty DataFrame) so the user can see they exist.

    Raises RuntimeError if openpyxl is not installed.
    Raises ValueError on unreadable files.
    """
    if not _OPENPYXL_OK:
        raise RuntimeError(
            "openpyxl is not installed. Run: pip install openpyxl"
        )
    try:
        sheets: dict[str, pd.DataFrame] = pd.read_excel(
            io.BytesIO(file_bytes),
            sheet_name=None,   # read all sheets
            engine="openpyxl",
        )
    except Exception as exc:
        raise ValueError(f"Could not read Excel file: {exc}") from exc
    return sheets
