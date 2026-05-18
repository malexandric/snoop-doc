"""
Charts demo view — a gallery of every chart type Snoop can produce, against
fake P&L-shaped data. Doubles as:

  1. The "Charts demo" sidebar view (a showcase users can scroll through to
     see what's possible — each chart has a "When to use" + "Finance
     example" caption so they know what to ask Snoop for).
  2. The fallback data source for the run_python sandbox when no real
     CSVs are present in `data/tables/`. `tools.py` imports the two
     DataFrame generators below.

The data is intentionally fake but shaped like real finance data
(monthly periods, multiple branches / products, plausible mix of
revenue + costs) so the charts read as realistic at a glance.

NOTE: this module was rebuilt from scratch on 2026-05-18 after a
deletion mishap during the GitHub-push prep. Functionally equivalent to
the original spec (14 chart types in 6 sections) but specific labels /
example prose may differ slightly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import theme


# ---------------------------------------------------------------------------
# Fake data — also re-exported via `tools.build_initial_namespace` when
# `data/tables/` is empty, so the chat sandbox has something to work with
# out of the box.
# ---------------------------------------------------------------------------
_BRANCHES = ["Products", "Services", "Marketplace"]
_PRODUCTS = ["Pro Plan", "Enterprise", "Starter", "Add-ons", "Legacy"]


def _months(start: str, periods: int) -> pd.DatetimeIndex:
    """Month-start timestamps starting at `start`, e.g. '2024-01-01'."""
    return pd.date_range(start=start, periods=periods, freq="MS")


def _pnl_by_branch() -> pd.DataFrame:
    """Monthly P&L by branch, Jan 2024 – Dec 2025 (24 months × 3 branches).

    Columns: Month, Branch, Revenue, COGS, OpEx, Gross_Profit, Operating_Income.
    Numbers chosen to be plausible: Products ~ $400k–$800k MRR with
    growth; Services ~ $200k–$400k; Marketplace launched mid-2024, smaller.
    """
    rng = np.random.default_rng(seed=42)
    months = _months("2024-01-01", 24)

    rows: list[dict] = []
    base_rev = {"Products": 420_000, "Services": 210_000, "Marketplace": 0}
    growth = {"Products": 0.025, "Services": 0.012, "Marketplace": 0.08}
    cogs_rate = {"Products": 0.18, "Services": 0.42, "Marketplace": 0.10}
    opex_rate = {"Products": 0.45, "Services": 0.38, "Marketplace": 0.62}

    for i, m in enumerate(months):
        for b in _BRANCHES:
            if b == "Marketplace" and i < 6:
                continue  # Marketplace launches in July 2024
            seasonality = 1.0 + 0.08 * np.sin((i + 3) / 12 * 2 * np.pi)
            noise = 1.0 + rng.normal(0, 0.04)
            rev = base_rev[b] * (1 + growth[b]) ** i * seasonality * noise
            if b == "Marketplace":
                rev = 35_000 * (1 + growth[b]) ** (i - 6) * seasonality * noise
            cogs = rev * cogs_rate[b] * (1 + rng.normal(0, 0.05))
            opex = rev * opex_rate[b] * (1 + rng.normal(0, 0.03))
            gp = rev - cogs
            oi = gp - opex
            rows.append({
                "Month": m,
                "Branch": b,
                "Revenue": round(rev, 2),
                "COGS": round(cogs, 2),
                "OpEx": round(opex, 2),
                "Gross_Profit": round(gp, 2),
                "Operating_Income": round(oi, 2),
            })
    return pd.DataFrame(rows)


def _product_margins() -> pd.DataFrame:
    """Monthly gross-margin % by product, 2025 (12 months × 5 products).

    Columns: Month, Product, Margin_Pct. Pro Plan is the healthy default
    (~78–82%); Enterprise slightly higher (~85%); Starter lower (~65–70%);
    Add-ons very high (~90%); Legacy declining."""
    rng = np.random.default_rng(seed=7)
    months = _months("2025-01-01", 12)

    base = {"Pro Plan": 0.80, "Enterprise": 0.85, "Starter": 0.67,
            "Add-ons": 0.90, "Legacy": 0.55}
    drift = {"Pro Plan": 0.001, "Enterprise": 0.0005, "Starter": 0.002,
             "Add-ons": -0.001, "Legacy": -0.008}

    rows: list[dict] = []
    for i, m in enumerate(months):
        for p in _PRODUCTS:
            value = base[p] + drift[p] * i + rng.normal(0, 0.012)
            rows.append({
                "Month": m,
                "Product": p,
                "Margin_Pct": round(max(0.0, min(1.0, value)) * 100, 2),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# View entry point — Charts demo sidebar nav route
# ---------------------------------------------------------------------------
def render() -> None:
    st.subheader("Charts demo")
    st.caption(
        "Every chart type Snoop can produce, against fake P&L data. "
        "Use this as a vocabulary lift: when you see a chart shape that "
        "fits your question, ask Snoop to produce it against your real data."
    )

    pnl = _pnl_by_branch()
    margins = _product_margins()

    # ---------------- Trends over time ----------------
    st.markdown(f"## {theme.ICON['trends']} Trends over time")

    _chart_block(
        name="Line chart",
        when="Show how a metric evolves over time. The default for any "
             "month-over-month or quarter-over-quarter view.",
        finance="Monthly revenue per branch. Slope tells you growth rate.",
        fig=_line_revenue_by_branch(pnl),
    )
    _chart_block(
        name="Stacked area chart",
        when="Show total over time AND its composition. Each layer = a "
             "category contributing to the running total.",
        finance="Revenue stack: Products + Services + Marketplace summing "
                "to total company revenue each month.",
        fig=_area_revenue_stack(pnl),
    )

    # ---------------- Comparing categories ----------------
    st.markdown(f"## {theme.ICON['compare']} Comparing categories")

    _chart_block(
        name="Bar chart (vertical)",
        when="Compare a single metric across a handful of named categories.",
        finance="Total 2025 revenue per branch — who's the biggest?",
        fig=_bar_revenue_per_branch(pnl),
    )
    _chart_block(
        name="Horizontal bar chart",
        when="Same as a vertical bar, but easier to read with long category "
             "labels or when you have 10+ categories.",
        finance="Average gross margin % per product, ranked highest to lowest.",
        fig=_hbar_margin_by_product(margins),
    )
    _chart_block(
        name="Grouped bar chart",
        when="Compare 2-3 metrics side by side across categories.",
        finance="Revenue / COGS / OpEx per branch in the most recent month.",
        fig=_grouped_bar_pnl_breakdown(pnl),
    )
    _chart_block(
        name="100% stacked bar",
        when="Show share-of-total instead of absolute amounts. Good for "
             "mix-shift questions.",
        finance="Revenue mix by branch over each quarter of 2025 (each bar "
                "sums to 100%).",
        fig=_pct_stack_revenue_mix(pnl),
    )

    # ---------------- Composition ----------------
    st.markdown(f"## {theme.ICON['composition']} Composition")

    _chart_block(
        name="Treemap",
        when="Hierarchical part-to-whole. Each box is sized by its "
             "contribution. Better than pie when you have many slices.",
        finance="2025 revenue by branch, then by month within each branch.",
        fig=_treemap_revenue(pnl),
    )
    _chart_block(
        name="Donut chart",
        when="Single-level part-to-whole with 3-5 slices. Avoid more than "
             "that — humans can't compare slice angles past 5.",
        finance="2025 full-year revenue mix across the three branches.",
        fig=_donut_revenue_mix(pnl),
    )

    # ---------------- Relationships ----------------
    st.markdown(f"## {theme.ICON['relationships']} Relationships")

    _chart_block(
        name="Dual-axis combo chart",
        when="Show a dollar amount and a ratio on the same chart. One Y axis "
             "for each — they share the X axis (typically time).",
        finance="Revenue ($) bars + Gross margin (%) line, for the Products "
                "branch over 24 months.",
        fig=_dual_axis_revenue_margin(pnl),
    )
    _chart_block(
        name="Scatter plot",
        when="Look at the relationship between two metrics. Each dot is one "
             "observation (e.g. one month, one customer, one product).",
        finance="OpEx vs Revenue across all (branch, month) pairs — does "
                "OpEx scale with revenue?",
        fig=_scatter_opex_vs_revenue(pnl),
    )

    # ---------------- Patterns ----------------
    st.markdown(f"## {theme.ICON['patterns']} Patterns")

    _chart_block(
        name="Heatmap",
        when="See patterns across two dimensions at once. Color = value, "
             "rows / cols = the two dimensions.",
        finance="Gross margin % heatmap — products on Y, months on X. "
                "Spot which product is improving or declining.",
        fig=_heatmap_margin(margins),
    )

    # ---------------- Finance-specific ----------------
    st.markdown(f"## {theme.ICON['finance']} Finance-specific")

    _chart_block(
        name="Waterfall chart",
        when="Show how a starting number becomes an ending number through "
             "a sequence of additions and subtractions. Made for P&L bridges.",
        finance="Revenue → COGS → Gross Profit → OpEx → Operating Income, "
                "for the Products branch in the most recent month.",
        fig=_waterfall_pnl_bridge(pnl),
    )
    _chart_block(
        name="Bullet chart (actuals vs target)",
        when="Compare actual performance to a target with one glance. The "
             "bar is the actual; the line is the target.",
        finance="Each branch's YTD 2025 revenue vs target.",
        fig=_bullet_actuals_vs_target(pnl),
    )
    _chart_block(
        name="Sankey diagram",
        when="Show flows between categories. Width of each ribbon = "
             "magnitude. Good for showing where money goes.",
        finance="Revenue (by branch) → split into COGS, OpEx, Operating "
                "Income for the most recent quarter.",
        fig=_sankey_revenue_flow(pnl),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _chart_block(name: str, when: str, finance: str, fig: go.Figure) -> None:
    """Render one demo entry: name + when-to-use + finance example + chart."""
    with st.container(border=True):
        st.markdown(f"### {name}")
        st.caption(f"**When to use:** {when}")
        st.caption(f"**Finance example:** {finance}")
        st.plotly_chart(fig, use_container_width=True)


def _fmt_currency_axis(fig: go.Figure, axis: str = "y") -> go.Figure:
    """Format an axis as `$1.2M` / `$450k` for readability."""
    layout_key = f"{axis}axis"
    fig.update_layout(**{layout_key: dict(tickprefix="$", tickformat=".2s")})
    return fig


# ---------------------------------------------------------------------------
# Chart factories — each returns a Figure ready to render
# ---------------------------------------------------------------------------
def _line_revenue_by_branch(pnl: pd.DataFrame) -> go.Figure:
    fig = px.line(
        pnl, x="Month", y="Revenue", color="Branch",
        title="Monthly revenue by branch",
        labels={"Revenue": "Revenue ($)"},
    )
    return _fmt_currency_axis(fig)


def _area_revenue_stack(pnl: pd.DataFrame) -> go.Figure:
    fig = px.area(
        pnl, x="Month", y="Revenue", color="Branch",
        title="Revenue stack by branch",
        labels={"Revenue": "Revenue ($)"},
    )
    return _fmt_currency_axis(fig)


def _bar_revenue_per_branch(pnl: pd.DataFrame) -> go.Figure:
    year_2025 = pnl[pnl["Month"].dt.year == 2025]
    totals = year_2025.groupby("Branch", as_index=False)["Revenue"].sum()
    fig = px.bar(
        totals.sort_values("Revenue", ascending=False),
        x="Branch", y="Revenue",
        title="2025 total revenue per branch",
        labels={"Revenue": "Revenue ($)"},
    )
    return _fmt_currency_axis(fig)


def _hbar_margin_by_product(margins: pd.DataFrame) -> go.Figure:
    avg = margins.groupby("Product", as_index=False)["Margin_Pct"].mean()
    avg = avg.sort_values("Margin_Pct")
    fig = px.bar(
        avg, x="Margin_Pct", y="Product", orientation="h",
        title="Average gross margin % by product (2025 avg)",
        labels={"Margin_Pct": "Gross margin (%)"},
    )
    fig.update_layout(xaxis=dict(ticksuffix="%"))
    return fig


def _grouped_bar_pnl_breakdown(pnl: pd.DataFrame) -> go.Figure:
    latest = pnl["Month"].max()
    snap = pnl[pnl["Month"] == latest]
    long = snap.melt(
        id_vars="Branch", value_vars=["Revenue", "COGS", "OpEx"],
        var_name="Metric", value_name="Amount",
    )
    fig = px.bar(
        long, x="Branch", y="Amount", color="Metric", barmode="group",
        title=f"Revenue / COGS / OpEx per branch ({latest.strftime('%b %Y')})",
        labels={"Amount": "Amount ($)"},
    )
    return _fmt_currency_axis(fig)


def _pct_stack_revenue_mix(pnl: pd.DataFrame) -> go.Figure:
    year_2025 = pnl[pnl["Month"].dt.year == 2025].copy()
    year_2025["Quarter"] = year_2025["Month"].dt.to_period("Q").astype(str)
    grouped = year_2025.groupby(["Quarter", "Branch"], as_index=False)["Revenue"].sum()
    totals = grouped.groupby("Quarter")["Revenue"].transform("sum")
    grouped["Share"] = grouped["Revenue"] / totals * 100
    fig = px.bar(
        grouped, x="Quarter", y="Share", color="Branch",
        title="Revenue mix by branch — 2025 quarterly (100% stacked)",
        labels={"Share": "Share of revenue (%)"},
    )
    fig.update_layout(yaxis=dict(ticksuffix="%", range=[0, 100]))
    return fig


def _treemap_revenue(pnl: pd.DataFrame) -> go.Figure:
    year_2025 = pnl[pnl["Month"].dt.year == 2025].copy()
    year_2025["MonthLabel"] = year_2025["Month"].dt.strftime("%b")
    fig = px.treemap(
        year_2025, path=["Branch", "MonthLabel"], values="Revenue",
        title="2025 revenue — branch › month",
    )
    return fig


def _donut_revenue_mix(pnl: pd.DataFrame) -> go.Figure:
    year_2025 = pnl[pnl["Month"].dt.year == 2025]
    totals = year_2025.groupby("Branch", as_index=False)["Revenue"].sum()
    fig = go.Figure(go.Pie(
        labels=totals["Branch"], values=totals["Revenue"],
        hole=0.55,
    ))
    fig.update_layout(title="2025 revenue mix")
    return fig


def _dual_axis_revenue_margin(pnl: pd.DataFrame) -> go.Figure:
    products = pnl[pnl["Branch"] == "Products"].copy().sort_values("Month")
    products["Margin_Pct"] = (products["Gross_Profit"] / products["Revenue"]) * 100
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=products["Month"], y=products["Revenue"], name="Revenue ($)",
        marker_color=theme.PRIMARY,
    ))
    fig.add_trace(go.Scatter(
        x=products["Month"], y=products["Margin_Pct"],
        name="Gross margin (%)", yaxis="y2", mode="lines+markers",
        line=dict(color="#FF8F0E", width=3),
    ))
    fig.update_layout(
        title="Products branch — revenue ($) + gross margin (%)",
        yaxis=dict(title="Revenue", tickprefix="$", tickformat=".2s"),
        yaxis2=dict(title="Margin %", overlaying="y", side="right",
                    ticksuffix="%"),
        legend=dict(orientation="h", yanchor="bottom", y=-0.25),
    )
    return fig


def _scatter_opex_vs_revenue(pnl: pd.DataFrame) -> go.Figure:
    fig = px.scatter(
        pnl, x="Revenue", y="OpEx", color="Branch",
        title="OpEx vs Revenue — does OpEx scale with revenue?",
        labels={"Revenue": "Revenue ($)", "OpEx": "OpEx ($)"},
        trendline="ols",
    )
    fig.update_layout(
        xaxis=dict(tickprefix="$", tickformat=".2s"),
        yaxis=dict(tickprefix="$", tickformat=".2s"),
    )
    return fig


def _heatmap_margin(margins: pd.DataFrame) -> go.Figure:
    wide = margins.copy()
    wide["MonthLabel"] = wide["Month"].dt.strftime("%b")
    pivot = wide.pivot(index="Product", columns="MonthLabel", values="Margin_Pct")
    # Reorder columns by month order
    month_order = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    pivot = pivot[[m for m in month_order if m in pivot.columns]]
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorscale="RdYlGn",
        text=[[f"{v:.1f}%" for v in row] for row in pivot.values],
        texttemplate="%{text}",
        colorbar=dict(ticksuffix="%"),
    ))
    fig.update_layout(title="Gross margin % by product × month (2025)")
    return fig


def _waterfall_pnl_bridge(pnl: pd.DataFrame) -> go.Figure:
    latest = pnl["Month"].max()
    snap = pnl[(pnl["Month"] == latest) & (pnl["Branch"] == "Products")].iloc[0]
    fig = go.Figure(go.Waterfall(
        name="P&L",
        orientation="v",
        measure=["absolute", "relative", "total", "relative", "total"],
        x=["Revenue", "COGS", "Gross Profit", "OpEx", "Operating Income"],
        y=[snap["Revenue"], -snap["COGS"], None, -snap["OpEx"], None],
        textposition="outside",
        text=[f"${v/1000:.0f}k" for v in
              [snap["Revenue"], -snap["COGS"],
               snap["Gross_Profit"], -snap["OpEx"], snap["Operating_Income"]]],
        connector=dict(line=dict(color=theme.TEXT_MUTED)),
    ))
    fig.update_layout(
        title=f"P&L bridge — Products, {latest.strftime('%b %Y')}",
        yaxis=dict(tickprefix="$", tickformat=".2s"),
    )
    return fig


def _bullet_actuals_vs_target(pnl: pd.DataFrame) -> go.Figure:
    """One bullet per branch — actual YTD vs target."""
    year_2025 = pnl[pnl["Month"].dt.year == 2025]
    actual = year_2025.groupby("Branch")["Revenue"].sum()
    # Synthetic targets ~10% above actual for Products+Services, below for Marketplace
    targets = {"Products": actual.get("Products", 0) * 1.08,
               "Services": actual.get("Services", 0) * 1.05,
               "Marketplace": actual.get("Marketplace", 0) * 0.85}
    fig = go.Figure()
    for i, branch in enumerate(["Products", "Services", "Marketplace"]):
        if branch not in actual.index:
            continue
        fig.add_trace(go.Indicator(
            mode="number+gauge",
            value=actual[branch],
            number=dict(prefix="$", valueformat=".2s"),
            domain=dict(x=[0.25, 1], y=[i / 3 + 0.02, (i + 1) / 3 - 0.02]),
            title=dict(text=branch),
            gauge=dict(
                shape="bullet",
                axis=dict(range=[0, max(actual.max(), max(targets.values())) * 1.1],
                          tickprefix="$", tickformat=".2s"),
                threshold=dict(line=dict(color="red", width=3),
                               thickness=0.85, value=targets[branch]),
                bar=dict(color=theme.PRIMARY),
            ),
        ))
    fig.update_layout(
        title="YTD 2025 revenue vs target (bullet)",
        height=300,
    )
    return fig


def _sankey_revenue_flow(pnl: pd.DataFrame) -> go.Figure:
    latest_quarter = pnl["Month"].max().to_period("Q")
    q_data = pnl[pnl["Month"].dt.to_period("Q") == latest_quarter]
    snap = q_data.groupby("Branch", as_index=False)[
        ["Revenue", "COGS", "OpEx", "Operating_Income"]
    ].sum()

    # Build Sankey nodes: branches -> P&L lines
    branches = snap["Branch"].tolist()
    lines = ["COGS", "OpEx", "Operating Income"]
    nodes = branches + lines
    node_idx = {n: i for i, n in enumerate(nodes)}

    source: list[int] = []
    target: list[int] = []
    value: list[float] = []
    for _, r in snap.iterrows():
        # Branch -> COGS / OpEx / Operating Income, sized by amount
        for line, val in [("COGS", r["COGS"]), ("OpEx", r["OpEx"]),
                          ("Operating Income", max(r["Operating_Income"], 0))]:
            source.append(node_idx[r["Branch"]])
            target.append(node_idx[line])
            value.append(val)

    fig = go.Figure(go.Sankey(
        node=dict(label=nodes, pad=15, thickness=18,
                  color=[theme.PRIMARY] * len(branches) +
                        ["#FF8F0E", "#F75757", "#34D399"]),
        link=dict(source=source, target=target, value=value),
    ))
    fig.update_layout(
        title=f"Revenue flow — {latest_quarter} (by branch → P&L line)",
    )
    return fig
