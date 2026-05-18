"""
HTML report rendering for the Save-as-report feature.

Takes one chat exchange — a user question plus the assistant's response,
including any inline Plotly figures and pandas DataFrames — and produces
a self-contained HTML string suitable for saving, forwarding, or opening
offline.

Self-contained means: Plotly.js is inlined ONCE alongside the first
figure (~3 MB), subsequent figures are lightweight `<div>`s that
reference that single JS bundle, tables are converted to styled HTML,
and the assistant's markdown is rendered with the `markdown` package.
The result opens in any browser without internet access and looks the
same in every email client / preview pane. We piggyback on Plotly's
own `include_plotlyjs=True` on the first figure rather than calling
`pio.get_plotlyjs()` directly — the latter isn't part of Plotly's
stable public API across versions.

This module is intentionally a pure function — no Streamlit imports. The
caller (saved_items.show_save_report_dialog) passes in the data; this
module produces the HTML.
"""

from __future__ import annotations

import html as html_module
from datetime import datetime
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

try:
    import markdown as md_lib  # type: ignore
except ImportError:  # pragma: no cover — flagged at first use, not import
    md_lib = None  # type: ignore


# ---------------------------------------------------------------------------
# Styling — matches Snoop's Stripe-modern aesthetic so saved reports feel
# like artefacts from the same product
# ---------------------------------------------------------------------------
_CSS = """
* { box-sizing: border-box; }
body {
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  color: #1A1F36;
  background: #FAFBFC;
  margin: 0;
  padding: 32px 16px;
}
.report {
  max-width: 880px;
  margin: 0 auto;
  background: white;
  border-radius: 12px;
  box-shadow: 0 4px 20px rgba(26, 31, 54, 0.06);
  padding: 48px 56px;
}
.report header { margin-bottom: 32px; }
.report header h1 {
  margin: 0;
  font-size: 1.75rem;
  font-weight: 600;
  color: #1A1F36;
  letter-spacing: -0.01em;
}
.report header .meta {
  color: #697386;
  font-size: 0.825rem;
  margin: 6px 0 0;
}
.report section { margin-top: 36px; }
.report h2 {
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #697386;
  font-weight: 600;
  margin: 0 0 12px;
}
.report .question p {
  background: #F6F9FC;
  border-left: 3px solid #635BFF;
  padding: 14px 18px;
  border-radius: 4px;
  margin: 0;
  color: #424770;
}
.report .response > h1, .report .response > h2, .report .response > h3,
.report .response > h4, .report .response > h5, .report .response > h6 {
  color: #1A1F36;
  letter-spacing: normal;
  text-transform: none;
  font-weight: 600;
  margin-top: 28px;
  margin-bottom: 12px;
}
.report .response > h1 { font-size: 1.4rem; }
.report .response > h2 { font-size: 1.2rem; }
.report .response > h3 { font-size: 1.05rem; }
.report .response p { margin: 12px 0; }
.report .response ul, .report .response ol { padding-left: 24px; margin: 12px 0; }
.report .response li { margin: 4px 0; }
.report .response a { color: #635BFF; text-decoration: none; }
.report .response a:hover { text-decoration: underline; }
.report .response strong { color: #1A1F36; }
.report .response code {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.9em;
  background: #F6F9FC;
  padding: 2px 6px;
  border-radius: 3px;
}
.report .response pre {
  background: #F6F9FC;
  padding: 14px 18px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 0.875rem;
  border: 1px solid #E3E8EE;
}
.report .response pre code { background: none; padding: 0; }
.report .response blockquote {
  border-left: 3px solid #E3E8EE;
  margin: 16px 0;
  padding: 4px 16px;
  color: #697386;
}
.report-table {
  border-collapse: collapse;
  width: 100%;
  font-size: 0.9rem;
  margin: 16px 0;
  border: 1px solid #E3E8EE;
  border-radius: 6px;
  overflow: hidden;
}
.report-table th, .report-table td {
  text-align: left;
  padding: 10px 14px;
  border-bottom: 1px solid #E3E8EE;
}
.report-table tr:last-child td { border-bottom: none; }
.report-table th {
  background: #F6F9FC;
  font-weight: 600;
  color: #424770;
  font-size: 0.825rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.report-table tbody tr:hover { background: #FAFBFC; }
.plotly-chart { margin: 24px 0; }
.snoop-footer {
  margin-top: 48px;
  padding-top: 20px;
  border-top: 1px solid #E3E8EE;
  color: #697386;
  font-size: 0.825rem;
  text-align: center;
}
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def render_exchange_to_html(
    question: str,
    assistant_blocks: list[dict],
    figures: dict[str, go.Figure],
    tables: dict[str, pd.DataFrame],
    report_name: str | None = None,
) -> str:
    """Render one chat exchange to a self-contained HTML document.

    Parameters
    ----------
    question : the user's prompt text for this exchange.
    assistant_blocks : flat list of Anthropic-style content blocks across
        the assistant's response — `{"type": "text", "text": ...}`,
        `{"type": "tool_use", ...}` (skipped — internal plumbing), or
        `{"type": "tool_result", "tool_use_id": ..., "content": ...}`
        (we look up the matching figure or table by `tool_use_id`).
    figures : mirror of `st.session_state.figures` — `tool_use_id → Figure`.
    tables : mirror of `st.session_state.tables` — `tool_use_id → DataFrame`.
    report_name : optional title shown in the header. Falls back to the
        first 80 chars of the question.

    Returns the full HTML document as a string. Plotly.js is inlined
    once at the top; subsequent figures reference it.
    """
    title = report_name or (question[:80] + ("…" if len(question) > 80 else ""))
    title_html = html_module.escape(title)
    question_html = html_module.escape(question).replace("\n", "<br>")
    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M")

    body_html = _render_body(assistant_blocks, figures, tables)

    # `question_html` is computed but no longer rendered — kept around in
    # case we want a hover tooltip or metadata-only inclusion later. The
    # title at the top is what reflects "what this report is about" since
    # the user can name it during save.
    _ = question_html

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <style>{_CSS}</style>
</head>
<body>
  <article class="report">
    <header>
      <h1>{title_html}</h1>
      <p class="meta">Generated by Snoop Doc · {generated_at}</p>
    </header>
    <section class="response">
      {body_html}
    </section>
    <footer class="snoop-footer">
      Snoop Doc - talk to your data.
    </footer>
  </article>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Body rendering — walk the assistant blocks, emit one HTML fragment per
# ---------------------------------------------------------------------------
def _render_body(
    assistant_blocks: list[dict],
    figures: dict[str, go.Figure],
    tables: dict[str, pd.DataFrame],
) -> str:
    """Walk the assistant content blocks in order, producing an HTML
    fragment per block. Text blocks are markdown-converted; tool_result
    blocks contribute a figure / table if one was registered for their
    `tool_use_id`. tool_use blocks (the model's internal `run_python`
    invocations) are skipped — they're plumbing, not content.

    The *first* figure rendered bundles Plotly.js inline; subsequent
    figures get a bare div. This way the document is self-contained
    without needing to call any private/unstable Plotly API to extract
    the JS separately.
    """
    fragments: list[str] = []
    seen_figure_ids: set[str] = set()
    seen_table_ids: set[str] = set()
    plotlyjs_included = False
    seen_any_content = False  # tracked so the preamble heuristic only
                              # applies to the FIRST text block

    for block in assistant_blocks:
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "")
            if text.strip():
                # Defensive: if this is the leading block and looks like
                # a "I'll create a report..." preamble, skip it. The
                # system prompt also tells the model not to write these,
                # but model compliance is imperfect.
                if not seen_any_content and _looks_like_preamble(text):
                    continue
                fragments.append(_render_markdown(text))
                seen_any_content = True

        elif btype == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            fig = figures.get(tool_use_id)
            if fig is not None and tool_use_id not in seen_figure_ids:
                fragments.append(_render_figure(
                    fig, tool_use_id, include_plotlyjs=not plotlyjs_included
                ))
                plotlyjs_included = True
                seen_figure_ids.add(tool_use_id)
                seen_any_content = True
            df = tables.get(tool_use_id)
            if df is not None and tool_use_id not in seen_table_ids:
                fragments.append(_render_table(df))
                seen_table_ids.add(tool_use_id)
                seen_any_content = True
            # tool_result.content (stdout / error text) is intentionally
            # not embedded — it's debug output, not narrative. The model
            # already incorporated relevant numbers into its text blocks.

        # tool_use blocks skipped.

    if not fragments:
        return "<p><em>(No response content.)</em></p>"
    return "\n".join(fragments)


_PREAMBLE_OPENERS = (
    "i'll ",
    "i will ",
    "i'm going to",
    "i am going to",
    "let me ",
    "i can ",
    "here's a ",
    "here's the ",
    "here is a ",
    "here is the ",
    "i'll create",
    "i'll build",
    "i'll analyse",
    "i'll analyze",
    "i'll put together",
)


def _looks_like_preamble(text: str) -> bool:
    """True if a leading text block looks like throat-clearing rather
    than report content. The pattern: short (< ~300 chars), no markdown
    headers, and starts with a first-person "I'll do X" type opener.

    Substantive opening paragraphs — proper orientation paragraphs in
    the narrate-as-you-go pattern — don't match because they're either
    longer, contain a heading, or don't start with "I'll" / "Let me"
    style language."""
    stripped = text.strip()
    if not stripped or len(stripped) > 300:
        return False
    # If it has any markdown heading marker, it's structured content.
    if any(line.lstrip().startswith("#") for line in stripped.splitlines()):
        return False
    lowered = stripped.lower()
    return lowered.startswith(_PREAMBLE_OPENERS)


def _render_markdown(text: str) -> str:
    """Convert assistant markdown text to HTML. Falls back to a plain
    paragraph wrap with newlines preserved if the markdown library
    isn't available."""
    if md_lib is None:
        escaped = html_module.escape(text).replace("\n", "<br>")
        return f"<p>{escaped}</p>"
    return md_lib.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )


def _render_figure(
    fig: go.Figure, tool_use_id: str, include_plotlyjs: bool = False
) -> str:
    """One Plotly figure as a `<div>`. Pass `include_plotlyjs=True` for
    the FIRST figure in the document — that figure's HTML will inline
    the full Plotly.js library, and subsequent figures can reference it
    by leaving `include_plotlyjs=False`."""
    div = pio.to_html(
        fig,
        include_plotlyjs=include_plotlyjs,
        full_html=False,
        div_id=f"fig-{tool_use_id}",
        config={"displaylogo": False, "responsive": True},
    )
    return f'<div class="plotly-chart">{div}</div>'


def _render_table(df: pd.DataFrame) -> str:
    """One pandas DataFrame as a styled HTML table. Capped at 500 rows
    to keep file size reasonable — anything bigger probably belongs in
    a saved table CSV, not a forwarded report."""
    truncated = False
    if len(df) > 500:
        df = df.head(500)
        truncated = True
    html = df.to_html(
        classes="report-table",
        index=False,
        border=0,
        escape=True,
    )
    if truncated:
        html += '<p><em>(Showing first 500 rows.)</em></p>'
    return html
