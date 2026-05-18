"""
Visual theme: Plotly template + CSS overrides + brand assets.

Centralises the look-and-feel so we can tweak in one place. The Streamlit
side of the theme (colors, base mode) lives in `.streamlit/config.toml`.
"""

from pathlib import Path

import plotly.io as pio
import streamlit as st


# ---------------------------------------------------------------------------
# Brand assets
# ---------------------------------------------------------------------------
# Prefer the PNG wordmark (logo + "SnoopDoc" text together). Fall back to
# the older symbol-only SVG if the PNG isn't present.
LOGO_PATH_PNG = Path(__file__).parent / "assets" / "logo.png"
LOGO_PATH_SVG = Path(__file__).parent / "assets" / "logo.svg"
APP_NAME = "Snoop Doc"
APP_TAGLINE = "Ask questions about your company's data."


# ---------------------------------------------------------------------------
# Palette — matches .streamlit/config.toml
# ---------------------------------------------------------------------------
PRIMARY = "#635BFF"          # Stripe-style indigo (placeholder until logo)
TEXT = "#1A1F36"             # Deep charcoal-navy
TEXT_MUTED = "#697386"       # Captions, hints
SURFACE = "#FFFFFF"          # Main background
SURFACE_ALT = "#F6F9FC"      # Sidebar / cards
BORDER = "#E3E8EE"           # Subtle separators, gridlines

# Categorical palette for charts — distinct enough to compare series,
# muted enough not to feel like a default Plotly chart.
CHART_COLORWAY = [
    PRIMARY,       # indigo
    "#00A5A0",     # teal
    "#FF8F0E",     # amber
    "#0073E6",     # blue
    "#F75757",     # coral
    TEXT_MUTED,    # gray
    "#A463F2",     # violet
    "#34D399",     # mint
]


# ---------------------------------------------------------------------------
# Plotly template — applied globally so AI-generated charts match the demo
# ---------------------------------------------------------------------------
def install_plotly_template() -> None:
    """Register and set the default Plotly template. Call once at startup."""
    base = pio.templates["plotly_white"].to_plotly_json()
    layout = base.setdefault("layout", {})
    layout["font"] = {
        "family": "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        "color": TEXT,
        "size": 13,
    }
    layout["colorway"] = CHART_COLORWAY
    layout["paper_bgcolor"] = SURFACE
    layout["plot_bgcolor"] = SURFACE
    layout["xaxis"] = {**layout.get("xaxis", {}), "gridcolor": BORDER, "zerolinecolor": BORDER}
    layout["yaxis"] = {**layout.get("yaxis", {}), "gridcolor": BORDER, "zerolinecolor": BORDER}
    layout["title"] = {"font": {"color": TEXT, "size": 16}, "x": 0.0, "xanchor": "left"}
    layout["legend"] = {"font": {"color": TEXT}, "bgcolor": "rgba(0,0,0,0)"}

    pio.templates["snoop_doc"] = base
    pio.templates.default = "snoop_doc"


# ---------------------------------------------------------------------------
# CSS — tightens Streamlit defaults toward the Stripe-modern look
# ---------------------------------------------------------------------------
_CSS = """
<style>
/* Pull the sidebar content closer to the top — Streamlit's default leaves
   ~3-4rem of empty space above the first widget, which makes the logo
   feel like it's floating. */
[data-testid="stSidebarUserContent"] {
    padding-top: 1rem;
}

/* Wordmark in the sidebar — feel less like a default h1, more like a logo. */
[data-testid="stSidebar"] h1:first-of-type {
    font-size: 1.4rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    margin-bottom: 0.25rem;
}

/* Sidebar caption right under the wordmark. */
[data-testid="stSidebar"] [data-testid="stCaptionContainer"]:first-of-type,
[data-testid="stSidebar"] .stCaption:first-of-type {
    color: #697386;
}

/* Less rounded buttons — Stripe's corners are ~6px, not Streamlit's ~10px. */
.stButton button {
    border-radius: 6px;
}

/* Tighten the sidebar dividers — but give the section *below* the hr
   extra breathing room so it doesn't sit right on the line. */
[data-testid="stSidebar"] hr {
    margin: 0.85rem 0 1.4rem 0;
}

/* ---------------------------------------------------------------------
   Sidebar nav fixes — pinned to Streamlit's current emotion-cache
   classes (identified by inspecting the rendered DOM). These hashes
   can shift when Streamlit updates, but for the current install
   they're the only selectors that reliably win against Streamlit's
   own button styles. The structural selectors below are kept as a
   future-proof fallback.
   --------------------------------------------------------------------- */
.stSidebar .st-emotion-cache-tn0cau {
    gap: 0.2rem !important;
}
.stSidebar .st-emotion-cache-1lads1q {
    justify-content: left !important;
}

/* ---------------------------------------------------------------------
   Sidebar nav — compact left-aligned link-with-icon style.

   Two targeting strategies, listed together so EITHER catches the same
   set of buttons:

     (1) `[class*="st-key-snoop_navlink"]` — non-chat items are each
         wrapped in `st.container(key="snoop_navlink_<view>")`, which
         adds a `st-key-snoop_navlink_<view>` class to the container's
         outer element on Streamlit 1.36+.
     (2) `button[kind="tertiary"]` — non-chat items also use
         `type="tertiary"`, which Streamlit 1.42+ renders as a link
         button with `kind="tertiary"` on the button DOM node.

   Either selector matching is enough to style the item. Ask the Snoop
   uses `primary` / `secondary` type and is not wrapped, so neither
   selector catches it — Streamlit's default button chrome applies.

   ACTIVE non-chat items use `type="primary"` so they read as a filled
   indigo row (caught by the navlink-class selector below, since
   primary doesn't have `kind="tertiary"`).
   --------------------------------------------------------------------- */
[data-testid="stSidebar"] [class*="st-key-snoop_navlink"] .stButton,
[data-testid="stSidebar"] .stButton:has(button[kind="tertiary"]) {
    margin-bottom: 2px !important;
}
[data-testid="stSidebar"] [class*="st-key-snoop_navlink"] .stButton button,
[data-testid="stSidebar"] .stButton button[kind="tertiary"] {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 6px 10px !important;
    min-height: auto !important;
    color: #424770 !important;
    font-size: 0.85rem !important;
    font-weight: 500 !important;
    line-height: 1.4 !important;
    text-align: left !important;
    justify-content: flex-start !important;
    border-radius: 5px !important;
    gap: 8px !important;
    transition: opacity 120ms ease !important;
}
/* Material icons in nav links — slightly bigger than text to anchor. */
[data-testid="stSidebar"] [class*="st-key-snoop_navlink"] [data-testid="stIconMaterial"],
[data-testid="stSidebar"] .stButton button[kind="tertiary"] [data-testid="stIconMaterial"] {
    font-size: 1.05rem !important;
}
/* Active link — filled indigo row. Only matches the navlink-class
   selector (active uses kind="primary", not tertiary). */
[data-testid="stSidebar"] [class*="st-key-snoop_navlink"] .stButton button[kind="primary"] {
    background-color: #635BFF !important;
    color: white !important;
    font-weight: 500 !important;
    padding: 6px 10px !important;
    min-height: auto !important;
    text-align: left !important;
    justify-content: flex-start !important;
    border-radius: 5px !important;
    gap: 8px !important;
    font-size: 0.85rem !important;
}
/* Hover on link items: explicit colours + gentle opacity fade. */
[data-testid="stSidebar"] [class*="st-key-snoop_navlink"] .stButton button:hover,
[data-testid="stSidebar"] .stButton button[kind="tertiary"]:hover {
    background-color: transparent !important;
    color: #424770 !important;
    opacity: 0.9 !important;
}
[data-testid="stSidebar"] [class*="st-key-snoop_navlink"] .stButton button[kind="primary"]:hover {
    background-color: #635BFF !important;
    color: white !important;
    opacity: 0.9 !important;
}

/* Chat message bubbles: subtle border, less round than default. */
[data-testid="stChatMessage"] {
    border-radius: 8px;
}

/* Expander headers: subtler colour for a cleaner read. */
[data-testid="stExpander"] summary {
    font-size: 0.9rem;
    color: #424770;
}

/* ---------------------------------------------------------------------
   Hierarchy fix: differentiate list-row containers from action buttons
   --------------------------------------------------------------------- */
/* Bordered containers (used for file lists / saved items / sync rows /
   per-object lists) get a much lighter chrome than the default. They
   read as list rows, not glossy cards — leaving room for the BUTTONS
   inside to be the dominant element. */
[data-testid="stVerticalBlockBorderWrapper"] {
    background-color: #FFFFFF;
    border: 1px solid #EEF1F4;
    border-radius: 6px;
    box-shadow: none;
    transition: border-color 120ms ease, background-color 120ms ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
    border-color: #DCE2EA;
    background-color: #FCFDFE;
}

/* Action buttons inside list rows / cards — strip all chrome (no border,
   no background) and shrink padding so they read as bare icon controls.
   Used on Data, Saved Items, Saved Questions, Context, and the Stripe
   sync expander rows. */
[data-testid="stVerticalBlockBorderWrapper"] .stButton button {
    background-color: transparent !important;
    border: none !important;
    color: #697386 !important;
    padding: 4px 6px !important;
    min-height: auto !important;
    box-shadow: none !important;
}
/* Primary-typed action buttons inside rows (like the View toggle when
   active) get the indigo treatment but stay tight — no border, no
   extra padding. */
[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="primary"] {
    background-color: #635BFF !important;
    color: white !important;
    border: none !important;
    padding: 4px 6px !important;
    min-height: auto !important;
}
/* Hover on row buttons — opacity only, no color shift. */
[data-testid="stVerticalBlockBorderWrapper"] .stButton button:hover {
    background-color: transparent !important;
    color: #697386 !important;
    opacity: 0.9 !important;
}
[data-testid="stVerticalBlockBorderWrapper"] .stButton button[kind="primary"]:hover {
    background-color: #635BFF !important;
    color: white !important;
    opacity: 0.9 !important;
}
/* Download button inside a row (Saved Items → Reports) follows the same
   bare-icon treatment. */
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stDownloadButton"] button {
    background-color: transparent !important;
    border: none !important;
    color: #697386 !important;
    padding: 4px 6px !important;
    min-height: auto !important;
    box-shadow: none !important;
}
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stDownloadButton"] button:hover {
    background-color: transparent !important;
    color: #697386 !important;
    opacity: 0.9 !important;
}

/* ---------------------------------------------------------------------
   Universal button hover (non-sidebar): gentle opacity fade only. Colors
   stay exactly as the non-hover state. We set the hover background /
   border / color explicitly to match the primary / secondary defaults
   so Streamlit's own hover style can't bleed through.
   --------------------------------------------------------------------- */
.stButton button {
    transition: opacity 120ms ease;
}
.stButton button:hover {
    opacity: 0.9 !important;
}
.stButton button[kind="primary"]:hover {
    background-color: #635BFF !important;
    border-color: #635BFF !important;
    color: white !important;
}
.stButton button[kind="secondary"]:hover {
    background-color: white !important;
    border-color: rgba(49, 51, 63, 0.2) !important;
    color: rgb(49, 51, 63) !important;
}
[data-testid="stDownloadButton"] button {
    transition: opacity 120ms ease;
}
[data-testid="stDownloadButton"] button:hover {
    opacity: 0.9 !important;
}

/* Selectboxes / number inputs / sliders inside list rows shouldn't
   inherit the ghost look — keep their normal form-control chrome. */
[data-testid="stVerticalBlockBorderWrapper"] [data-baseweb="select"],
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stNumberInput"] input {
    background: white;
}

/* Saved Items grid cards — taller than a single row, content stacks
   vertically. Slightly different treatment from list rows so the user
   reads them as "objects to interact with" rather than "items in a
   list to scan". */
.snoop-card [data-testid="stVerticalBlockBorderWrapper"] {
    padding: 4px 4px 12px;
    height: 100%;
}

/* ---------------------------------------------------------------------
   Copy-response button — small, unobtrusive, sits at the bottom of an
   assistant message. Lives inside an st.html iframe-less container. */
.snoop-copy-btn {
    background: transparent;
    border: 1px solid #E3E8EE;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 0.75rem;
    color: #697386;
    cursor: pointer;
    font-family: inherit;
    transition: background 80ms ease;
}
.snoop-copy-btn:hover {
    background: #F6F9FC;
    color: #1A1F36;
}

</style>
"""

def inject_css() -> None:
    """Inject the project's CSS overrides. Call once near the top of app.py."""
    st.markdown(_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sidebar logo + tagline
# ---------------------------------------------------------------------------
def install_logo() -> None:
    """Wire our PNG into Streamlit's dedicated sidebar logo slot.

    `st.logo()` renders into the area above the sidebar's user content,
    which is the "right" home for a brand logo. Falls back to the older
    symbol-only SVG if no PNG is present; silently skips if neither exists.
    """
    if LOGO_PATH_PNG.exists():
        st.logo(str(LOGO_PATH_PNG), size="large")
    elif LOGO_PATH_SVG.exists():
        st.logo(str(LOGO_PATH_SVG), size="large")


# ---------------------------------------------------------------------------
# Icon map — central source of truth for Material Icon names used in the UI
# ---------------------------------------------------------------------------
ICON = {
    # Navigation
    "chat": ":material/forum:",
    "saved": ":material/bookmark:",
    "items": ":material/inventory_2:",
    "data": ":material/table_view:",
    "charts": ":material/insights:",
    "settings": ":material/settings:",
    # Actions
    "clear": ":material/delete_sweep:",
    "forget": ":material/lock_reset:",
    "save": ":material/save:",
    # Inline (chat, expanders)
    "code": ":material/code:",
    "output": ":material/terminal:",
    "readme": ":material/menu_book:",
    "tables": ":material/table_view:",
    "context_doc": ":material/description:",
    "integrations": ":material/hub:",
    # Chart category section headers
    "trends": ":material/trending_up:",
    "compare": ":material/equalizer:",
    "composition": ":material/donut_large:",
    "relationships": ":material/scatter_plot:",
    "patterns": ":material/grid_view:",
    "finance": ":material/account_balance:",
}
