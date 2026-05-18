"""
Code execution tool for Snoop Doc.

Loads CSVs from `data/tables/` at runtime and exposes them as DataFrames in
the execution sandbox. If no real CSVs are present, falls back to the fake
demo data in `chart_demo.py` so the app still works out of the box.

The tool description is built dynamically so Claude always sees the current
list of tables, their shapes, and a preview of the first few rows.
"""

import contextlib
import io
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

import chart_demo


TABLES_DIR = Path(__file__).parent / "data" / "tables"


# ---------------------------------------------------------------------------
# Discovery + loading
# ---------------------------------------------------------------------------
def sanitize_name(filename: str) -> str:
    """Convert a filename into a valid Python identifier.

    Examples:
        'P&L 2026 - P&L 2026.csv' -> 'p_l_2026_p_l_2026'
        'salaries.csv'            -> 'salaries'
        '2025 revenue.csv'        -> 'df_2025_revenue'
    """
    name = Path(filename).stem
    name = re.sub(r"\W+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.lower().strip("_")
    if not name:
        return "df"
    if name[0].isdigit():
        name = f"df_{name}"
    return name


# Backwards-compat alias for code that still imports the underscore version.
_sanitize_name = sanitize_name


def _list_csv_files() -> list[Path]:
    """List `.csv` files in `data/tables/`, ignoring Windows ADS markers."""
    if not TABLES_DIR.exists():
        return []
    return sorted(
        p for p in TABLES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() == ".csv"
    )


def discover_tables() -> dict[str, Path]:
    """Return `{sanitized_name: file_path}` for every CSV in `data/tables/`.

    If two files sanitize to the same identifier, suffix the later ones with
    `_2`, `_3`, etc., so each DataFrame still has a unique name.
    """
    result: dict[str, Path] = {}
    for path in _list_csv_files():
        base = _sanitize_name(path.name)
        candidate = base
        i = 1
        while candidate in result:
            i += 1
            candidate = f"{base}_{i}"
        result[candidate] = path
    return result


def _read_csv_robust(path: Path) -> pd.DataFrame | None:
    """Try to read a CSV with the most common encodings before giving up.

    Real-world finance CSVs are often exported from Excel / Windows with
    encodings like cp1252 or latin-1 (especially when they contain € or
    accented characters). Without a fallback chain we'd silently drop
    those files. `latin-1` always succeeds at the byte level (no character
    is invalid), so it's the safety-net last attempt — at worst, the AI
    sees some mojibake in non-ASCII cells and can ask about it.
    """
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
        except Exception:
            # Non-encoding error (malformed CSV, parser failure, etc.).
            # Retrying with a different encoding won't help.
            return None
    return None


def _load_tables(selected: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Read every discoverable CSV into a DataFrame, skipping any that error.

    If `selected` is given (a list of sanitized table names), only those
    tables are loaded — used by the chat's data-scope picker. `None` means
    load everything (the default).
    """
    tables: dict[str, pd.DataFrame] = {}
    for name, path in discover_tables().items():
        if selected is not None and name not in selected:
            continue
        df = _read_csv_robust(path)
        if df is not None:
            tables[name] = df
        # else: silently skip; the format_table_summary or the context-chat
        # error-handling path will surface this to the user.
    return tables


# ---------------------------------------------------------------------------
# Tool definition (dynamic, so Claude sees the current state on each call)
# ---------------------------------------------------------------------------
def _format_table_summary(selected: list[str] | None = None) -> str:
    """Produce a markdown description of available tables for Claude."""
    tables = _load_tables(selected=selected)

    if not tables:
        return (
            "No CSVs found in `data/tables/`. Using DEMO data:\n"
            "  - `pnl_by_branch` (monthly P&L by branch, Jan 2024 – Dec 2025)\n"
            "  - `product_margins` (monthly gross margin % by product, 2025)\n\n"
            "Flag clearly in your answer that this is sample data."
        )

    discovered = discover_tables()
    lines = ["Available DataFrames loaded from `data/tables/`:", ""]
    for name, df in tables.items():
        source = discovered.get(name)
        source_str = f" (from `{source.name}`)" if source else ""
        lines.append(f"**`{name}`**{source_str} — {len(df):,} rows × {len(df.columns)} columns")
        # First 5 rows as a preview so Claude can spot multi-row headers, currency
        # strings, etc. Truncate wide tables so we don't blow the token budget.
        try:
            preview = df.head(5).to_string(max_cols=8, max_colwidth=24)
        except Exception:
            preview = "(could not generate preview)"
        lines.append(f"```\n{preview}\n```")
        lines.append("")

    if len(tables) == 1:
        only_name = next(iter(tables.keys()))
        lines.append(
            f"**Convenience alias:** since there's only one table loaded, you "
            f"can also refer to it as `df` (same object as `{only_name}`)."
        )
        lines.append("")

    lines.append(
        "Note: data is loaded raw from CSV. Real spreadsheets often have "
        "multi-row headers, currency strings like '$1,234.56', section-header "
        "rows with empty values, and trailing whitespace in column names. "
        "Inspect the DataFrame first and clean as needed before computing — "
        "strip whitespace with `df.columns = df.columns.str.strip()`, parse "
        "currency with `.str.replace('$', '').str.replace(',', '').astype(float)`."
    )
    return "\n".join(lines)


def build_tools(selected_tables: list[str] | None = None) -> list[dict]:
    """Construct the tools list (Anthropic-native format).

    Call this fresh on every API request so the table summary reflects
    the current filesystem state. `selected_tables` restricts which tables
    are described to the model and loaded into its sandbox.
    """
    return [
        {
            "name": "run_python",
            "description": (
                "Execute Python code against the company's financial data. "
                "Returns stdout, any error, and a Plotly figure if one was "
                "assigned to the variable `fig`.\n\n"
                f"{_format_table_summary(selected=selected_tables)}\n\n"
                "Available libraries in the namespace:\n"
                "  - `pd`, `np` — pandas, numpy\n"
                "  - `px`, `go` — plotly.express, plotly.graph_objects\n\n"
                "To return a chart: assign a Plotly figure to a variable named "
                "`fig`. The app renders it inline for the user.\n"
                "To return a table the user might want to keep / inspect "
                "interactively: assign a pandas DataFrame to a variable "
                "named `result_table`. The app renders it as a sortable "
                "table with a Save button (vs. a `print(df)` which is just "
                "text in the output expander).\n"
                "To return numbers / text: use `print()`. Captured stdout is "
                "returned to you.\n"
                "All three in one call is fine.\n\n"
                "**State persists across tool calls within the same user "
                "question.** Variables you assign, helper functions you "
                "define, and modifications you make to DataFrames are all "
                "available on the next call. Build incrementally — define a "
                "helper once, reuse it. State resets when the user asks a "
                "new question."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute.",
                    }
                },
                "required": ["code"],
            },
        }
    ]


# Static `TOOLS` kept for backwards compatibility with anything that
# imports it directly. New callers should use `build_tools()` so the table
# previews stay fresh.
TOOLS = build_tools()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
@dataclass
class ExecResult:
    """Result of running a code snippet in the sandbox."""

    stdout: str = ""
    fig: Any | None = None
    table: Any | None = None  # pandas DataFrame, when the code assigns one to `result_table`
    error: str | None = None

    def to_tool_result_text(self) -> str:
        """String form sent back to Claude as the `tool_result.content`."""
        parts: list[str] = []
        if self.error:
            parts.append(f"Error:\n{self.error}")
        if self.stdout:
            parts.append(f"stdout:\n{self.stdout}")
        if self.fig is not None:
            parts.append("(A Plotly figure was created and rendered to the user.)")
        if self.table is not None:
            rows = len(self.table)
            cols = len(self.table.columns)
            parts.append(
                f"(A table with {rows:,} rows × {cols} columns was rendered to "
                "the user as `result_table`. They can save it directly.)"
            )
        if not parts:
            return "Code executed with no output."
        return "\n\n".join(parts)


def build_initial_namespace(
    selected_tables: list[str] | None = None,
    *,
    extra_globals: dict | None = None,
    skip_demo_fallback: bool = False,
) -> dict:
    """Build a fresh execution namespace with libraries + DataFrames.

    Called once per user question; the agent loop reuses this namespace
    across every tool iteration for that question. `selected_tables` (if
    set) restricts which CSVs from `data/tables/` are loaded into the
    sandbox — used by the chat's data-scope picker.

    Convenience: when there's exactly one real table loaded, we also expose
    it as `df`. Models reach for `df` reflexively when writing pandas code,
    and a sanitized filename like `p_l_2026_p_l_2026` is easy to forget
    mid-script. The alias prevents the common NameError → retry loop.

    `extra_globals` (e.g. `{"csv_path": "..."}`): merged into the namespace
    after tables are loaded. Used by the context-chat to expose the path
    of the CSV being documented so the AI can recover from auto-load
    failures.

    `skip_demo_fallback`: when True and no real tables load, don't seed
    the demo DataFrames. Set this in the context-chat path so the AI
    doesn't see demo data and confuse it with the file under discussion.
    """
    namespace: dict[str, Any] = {
        "pd": pd,
        "np": np,
        "px": px,
        "go": go,
    }
    real_tables = _load_tables(selected=selected_tables)
    if real_tables:
        namespace.update(real_tables)
        if len(real_tables) == 1:
            namespace["df"] = next(iter(real_tables.values()))
    elif not skip_demo_fallback:
        namespace["pnl_by_branch"] = chart_demo._pnl_by_branch().copy()
        namespace["product_margins"] = chart_demo._product_margins().copy()
    if extra_globals:
        namespace.update(extra_globals)
    return namespace


def execute(code: str, namespace: dict | None = None) -> ExecResult:
    """Run a snippet of Python code and capture stdout + any plotly figure.

    If `namespace` is passed in, the code runs inside it and any state the
    model creates (vars, functions, mutated DataFrames) persists for the
    next call sharing the same namespace. If `None`, a fresh per-call
    namespace is built — present as a fallback but not the path the app
    uses.
    """
    if namespace is None:
        namespace = build_initial_namespace()

    # Reset `fig` and `result_table` between calls so we only surface
    # things the model creates *in this call* — not leftovers from a
    # previous one.
    namespace.pop("fig", None)
    namespace.pop("result_table", None)

    stdout_buf = io.StringIO()
    result = ExecResult()

    try:
        with contextlib.redirect_stdout(stdout_buf):
            exec(code, namespace)
    except Exception:
        # `limit=3` keeps the traceback short enough for Claude to read,
        # but long enough that it can usually self-correct on retry.
        result.error = traceback.format_exc(limit=3)

    result.stdout = stdout_buf.getvalue()

    fig_candidate = namespace.get("fig")
    if isinstance(fig_candidate, go.Figure):
        result.fig = fig_candidate

    table_candidate = namespace.get("result_table")
    if isinstance(table_candidate, pd.DataFrame):
        result.table = table_candidate

    return result
