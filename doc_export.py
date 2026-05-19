"""
Document export utilities — turn in-app analysis artefacts into files
the user can share outside the app.

Export paths:
  DataFrame(s)  → .xlsx  (one or many sheets, column widths auto-fitted)
  Report blocks → .pdf   (text + tables; charts noted but not embedded)

Both functions return raw bytes so callers can feed them straight into
st.download_button(data=...) without touching the filesystem.

PDF notes
---------
Generated with fpdf2 (pure-Python, no system dependencies). Charts are
not embedded — the PDF includes a placeholder note for each one. Use the
HTML report (Save as Report → Download) for a fully interactive version
that includes Plotly charts.

Characters outside the Windows-1252 range (extended Latin) are replaced
with '?' in the PDF. For full Unicode coverage, use the HTML version.
"""

from __future__ import annotations

import html as _html_mod
import io
from datetime import datetime
from typing import Any

import pandas as pd

try:
    import markdown as _md_lib  # type: ignore
    _MD_OK = True
except ImportError:
    _MD_OK = False

try:
    from fpdf import FPDF  # type: ignore
    from fpdf.enums import XPos, YPos  # type: ignore
    _FPDF_OK = True
except ImportError:
    _FPDF_OK = False

try:
    import openpyxl  # type: ignore  # noqa: F401
    _OPENPYXL_OK = True
except ImportError:
    _OPENPYXL_OK = False


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------
def dataframe_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    """Serialise one DataFrame to an .xlsx file in memory.

    Column widths are auto-fitted to content (capped at 60 chars) and the
    header row is bold. Returns raw bytes suitable for st.download_button.

    Raises RuntimeError if openpyxl is not installed.
    """
    if not _OPENPYXL_OK:
        raise RuntimeError("openpyxl is not installed. Run: pip install openpyxl")
    return dataframes_to_excel_bytes({sheet_name: df})


def dataframes_to_excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    """Serialise multiple DataFrames to a single .xlsx workbook.

    Each key in `sheets` becomes a sheet tab (truncated to 31 chars — the
    Excel limit). Column widths are auto-fitted; header rows are bold.

    Raises RuntimeError if openpyxl is not installed.
    """
    if not _OPENPYXL_OK:
        raise RuntimeError("openpyxl is not installed. Run: pip install openpyxl")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for raw_name, df in sheets.items():
            safe_name = str(raw_name)[:31]
            df.to_excel(writer, sheet_name=safe_name, index=False)
            ws = writer.sheets[safe_name]
            _autofit_columns(ws, df)
    return buf.getvalue()


def _autofit_columns(ws: Any, df: pd.DataFrame) -> None:
    """Bold the header row and set each column width to fit its content."""
    from openpyxl.styles import Font  # type: ignore

    for col_idx, col_name in enumerate(df.columns, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = Font(bold=True)
        # Width = max of header length and the longest value, capped at 60.
        max_len = max(
            len(str(col_name)),
            df[col_name].astype(str).str.len().max() if len(df) > 0 else 0,
        )
        ws.column_dimensions[cell.column_letter].width = min(max_len + 2, 62)


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------
def report_to_pdf_bytes(
    title: str,
    assistant_blocks: list[dict],
    figures: dict,
    tables: dict[str, pd.DataFrame],
) -> bytes:
    """Render a chat exchange as a downloadable PDF.

    Parameters match those passed to reports.render_exchange_to_html:
      title           – displayed in the PDF header.
      assistant_blocks – flat list of Anthropic content blocks.
      figures         – tool_use_id → Plotly Figure (charts noted, not embedded).
      tables          – tool_use_id → DataFrame.

    Raises RuntimeError if fpdf2 is not installed.
    """
    if not _FPDF_OK:
        raise RuntimeError("fpdf2 is not installed. Run: pip install fpdf2")

    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M")

    # Build an HTML string fpdf2 can render.  We walk the same block list
    # that reports.py uses so the content order matches the HTML report.
    html_parts: list[str] = []
    chart_count = 0

    for block in assistant_blocks:
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "").strip()
            if not text:
                continue
            if _MD_OK:
                html_parts.append(
                    _md_lib.markdown(
                        text,
                        extensions=["fenced_code", "tables", "sane_lists"],
                        output_format="html5",
                    )
                )
            else:
                escaped = _html_mod.escape(text).replace("\n", "<br>")
                html_parts.append(f"<p>{escaped}</p>")

        elif btype == "tool_result":
            tuid = block.get("tool_use_id", "")

            if tuid in figures:
                chart_count += 1
                html_parts.append(
                    f"<p><em>[Chart {chart_count}: open the HTML version "
                    f"for interactive charts]</em></p>"
                )

            if tuid in tables:
                df = tables[tuid]
                display_df = df.head(200)
                html_parts.append(
                    display_df.to_html(index=False, border=1)
                )
                if len(df) > 200:
                    html_parts.append(
                        f"<p><em>(Showing first 200 of {len(df):,} rows.)</em></p>"
                    )

    body_html = "\n".join(html_parts) if html_parts else "<p><em>(No content)</em></p>"

    # Sanitise for the Windows-1252 font set fpdf2 uses by default.
    body_html = _cp1252_safe(body_html)
    safe_title = _cp1252_safe(title)
    safe_meta = _cp1252_safe(f"Generated by Snoop Doc · {generated_at}")

    pdf = FPDF()
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # Header
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 9, safe_title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, safe_meta, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)

    # Body
    pdf.set_font("Helvetica", "", 10)
    pdf.write_html(body_html)

    # Render to bytes
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


def _cp1252_safe(text: str) -> str:
    """Replace characters outside Windows-1252 with '?' so fpdf2 doesn't crash."""
    return text.encode("cp1252", errors="replace").decode("cp1252")
