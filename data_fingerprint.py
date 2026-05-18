"""
DataFrame profiler — produces a comprehensive but token-bounded summary
of a CSV for AI consumption.

Used by the context-chat assistant to understand the data well enough to
ask focused questions and verify claims before adding them to the doc.

Goes deeper than `df.head(10)`: per-column stats over the *whole* file,
plus head + tail + random middle samples, plus detection of common
anomalies (mixed currency symbols, sentinel values, parenthesized
negatives, etc.). All driven by pandas methods that are efficient even
on multi-million-row files.
"""

from __future__ import annotations

import pandas as pd


MAX_COLUMNS_DETAIL = 40   # cap per-column detail to stay within token budget
MAX_UNIQUE_SAMPLES = 12   # how many distinct values to list per string column
ANOMALY_SENTINELS = (
    "N/A", "NA", "n/a", "na",
    "TBD", "tbd",
    "-", "—", "?",
    "null", "Null", "NULL",
    "None", "none",
    "unknown", "Unknown",
)


def fingerprint(df: pd.DataFrame) -> str:
    """Build a markdown summary of `df` suitable for sending to an LLM.

    Includes shape, per-column profile (with anomaly detection), and head
    + tail + random-middle row samples. Bounded in size: about 3-8 KB of
    tokens for a typical 20-column file; longer for wider tables but the
    column-detail cap keeps growth linear in MAX_COLUMNS_DETAIL.
    """
    parts: list[str] = [
        f"**Shape:** {len(df):,} rows × {len(df.columns)} columns",
        "",
        "## Per-column profile",
    ]

    cols = list(df.columns)
    for col in cols[:MAX_COLUMNS_DETAIL]:
        parts.append(_describe_column(df[col]))

    if len(cols) > MAX_COLUMNS_DETAIL:
        rest = cols[MAX_COLUMNS_DETAIL:]
        rest_str = ", ".join(f"`{c}`" for c in rest[:20])
        if len(rest) > 20:
            rest_str += f", … ({len(rest) - 20} more)"
        parts.append(
            f"\n_({len(rest)} more columns not detailed: {rest_str})_"
        )

    parts.append("")
    parts.append("## Row samples")
    parts.append("### First 15 rows")
    parts.append("```")
    parts.append(df.head(15).to_string(max_cols=12, max_colwidth=30))
    parts.append("```")
    parts.append("### Last 15 rows")
    parts.append("```")
    parts.append(df.tail(15).to_string(max_cols=12, max_colwidth=30))
    parts.append("```")
    if len(df) > 40:
        n = min(15, len(df) - 30)
        sample = df.sample(n=n, random_state=42).sort_index()
        parts.append("### Random middle sample")
        parts.append("```")
        parts.append(sample.to_string(max_cols=12, max_colwidth=30))
        parts.append("```")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-column descriptions
# ---------------------------------------------------------------------------
def _describe_column(s: pd.Series) -> str:
    lines = [f"\n### `{s.name}`"]
    lines.append(f"- dtype: `{s.dtype}`")
    lines.append(f"- non-null: {int(s.notna().sum()):,} / {len(s):,}")
    try:
        lines.append(f"- unique: {int(s.nunique(dropna=True)):,}")
    except TypeError:
        pass

    if pd.api.types.is_numeric_dtype(s):
        nn = s.dropna()
        if len(nn) > 0:
            lines.append(f"- range: {_fmt(nn.min())} to {_fmt(nn.max())}")
            lines.append(f"- mean: {_fmt(nn.mean())}")
    else:
        nn = s.dropna().astype(str)
        if len(nn) > 0:
            uniques = list(nn.unique()[:MAX_UNIQUE_SAMPLES])
            sample_str = ", ".join(f"`{_truncate(v)}`" for v in uniques)
            total_unique = nn.nunique()
            if total_unique > MAX_UNIQUE_SAMPLES:
                sample_str += f", … ({total_unique - MAX_UNIQUE_SAMPLES} more)"
            lines.append(f"- sample values: {sample_str}")

            for note in _detect_anomalies(nn):
                lines.append(f"- ⚠ {note}")

    return "\n".join(lines)


def _fmt(x) -> str:
    """Format a numeric value for the profile: thousands separators,
    a few sig-figs, no scientific notation for normal ranges."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    if abs(f) >= 1000 or f == int(f):
        return f"{f:,.2f}"
    return f"{f:,.4g}"


def _truncate(value: str, n: int = 40) -> str:
    return value if len(value) <= n else value[:n] + "…"


# ---------------------------------------------------------------------------
# Anomaly detection — patterns that often hint at "watch out for this"
# ---------------------------------------------------------------------------
def _detect_anomalies(s: pd.Series) -> list[str]:
    notes: list[str] = []
    n = len(s)

    # Currency-style $ usage that isn't universal
    try:
        has_dollar = int(s.str.contains(r"\$", regex=True, na=False).sum())
        if 0 < has_dollar < n:
            notes.append(
                f"mixed $ usage: {has_dollar:,}/{n:,} rows include a $ sign"
            )
    except Exception:  # noqa: BLE001 — defensive against weird dtypes
        pass

    # Accounting-style negatives — values wrapped in parentheses
    try:
        has_paren = int(s.str.contains(r"^\(.+\)$", regex=True, na=False).sum())
        if 0 < has_paren < n:
            notes.append(
                f"some values wrapped in parens ({has_paren:,}/{n:,}) — "
                "possibly accounting-style negatives"
            )
    except Exception:  # noqa: BLE001
        pass

    # Common sentinel values that masquerade as data
    for sentinel in ANOMALY_SENTINELS:
        try:
            count = int((s == sentinel).sum())
        except Exception:  # noqa: BLE001
            continue
        if count > 0:
            notes.append(f"contains sentinel `'{sentinel}'` ({count:,}x)")

    return notes
