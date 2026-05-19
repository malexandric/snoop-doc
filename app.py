"""
Snoop Doc — talk to your financial data.

A Streamlit app that lets users ask questions about company financial data
using Claude. Claude has a Python execution tool wired up so it can compute
real answers and draw charts against the data, not just describe it.
"""

import base64
import html
import json
import os
import re
import time
import uuid
from pathlib import Path

import anthropic
import streamlit as st
import streamlit.components.v1 as components

import chart_demo
import context_browser
import context_pointers
import data_browser
import dialogs
import doc_export
import gsheets
import memory
import saved_items
import saved_questions
import stripe_sync
import theme
import tools
from dialogs import confirm_or_run

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Snoop Doc",
    page_icon=":material/forum:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Visual theme: Plotly template applied app-wide + CSS overrides for the
# Stripe-modern look. Must run before any chart or widget is rendered.
theme.install_plotly_template()


theme.inject_css()
theme.install_logo()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Update these as new models are released. The list controls the sidebar dropdown.
AVAILABLE_MODELS = [
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4",
    "claude-sonnet-4",
]
DEFAULT_MODEL = "claude-sonnet-4-5"

# A second, separate model slot for AI *helpers* (context-doc editor, future
# helper features). Defaults to Haiku — focused, cheap, plenty for the
# narrow tasks helpers do. Cleanly separated from the main chat model so
# users can have Sonnet/Opus for analysis without paying that on every
# context-doc edit.
DEFAULT_UTILITY_MODEL = "claude-haiku-4-5"

# When Quick mode is on, this is the model used regardless of the Chat model
# setting. Defaults to Haiku — fastest + cheapest of the lineup — but
# user-overridable in Settings so e.g. someone running Snoop against a Bedrock
# account with a different default fastest model can adjust.
DEFAULT_QUICK_MODE_MODEL = "claude-haiku-4-5"

# Per-model pricing in USD per million tokens. Cache writes are billed at the
# regular input rate × 1.25; cache reads are ~10% of input. Sources: Anthropic
# pricing page. Update if the model line-up or pricing changes.
PRICING_PER_MTOK = {
    "claude-opus-4-5":   {"input": 15.0,  "cached_read": 1.50, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0,   "cached_read": 0.30, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0,   "cached_read": 0.30, "output": 15.0},
    "claude-haiku-4-5":  {"input": 1.0,   "cached_read": 0.10, "output": 5.0},
}
CACHE_WRITE_MULTIPLIER = 1.25  # cache_creation tokens cost 1.25× input rate

# No iteration cap — the agent loops until the model returns an `end_turn`
# (it's done) or the user clicks Stop. If pathological "error → retry"
# loops become a real cost issue later, reintroduce a generous safety cap.

# Sampling temperature. 0.0 = most deterministic; 1.0 = most varied.
DEFAULT_TEMPERATURE = 1.0
TEMPERATURE_RANGE = (0.0, 1.0)

# Cap on the model's output length per response. Tradeoff: lower = cheaper +
# faster but may truncate long reports; higher allows board-deck output.
DEFAULT_MAX_OUTPUT_TOKENS = 4096
MAX_OUTPUT_TOKENS_RANGE = (256, 16384)

# When True, the system prompt nudges Claude to produce a chart whenever the
# answer involves multiple data points — not just when the user asks for one.
DEFAULT_PREFER_CHARTS = True

# Web search via Anthropic's server-side web_search tool. Off by default
# because it costs extra per request and many finance questions don't
# need it. User toggles per-conversation via the chat header.
DEFAULT_WEB_SEARCH_ENABLED = False

# Anthropic server-side web tool type identifiers. These are versioned by
# date — bump the strings if Anthropic releases newer versions. Web fetch
# is always on; web search is gated by DEFAULT_WEB_SEARCH_ENABLED / the
# chat toggle.
ANTHROPIC_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
ANTHROPIC_WEB_FETCH_TOOL = {"type": "web_fetch_20250910", "name": "web_fetch"}

# When True, destructive actions (Clear chat, Delete file, Forget settings)
# show a confirmation dialog. Off for power users who'd rather not click twice.
DEFAULT_CONFIRM_DESTRUCTIVE = True

# When True, every registered Google Sheets sync is re-pulled when the app
# starts. Off by default so launching the app is fast and predictable; on
# for users who want fresh data without remembering to click sync.
DEFAULT_GOOGLE_SYNC_ON_START = False

# Same toggle, but for Stripe. Off by default. Note: the first-ever Stripe
# sync can take 10-25 min on large accounts (~250k invoices), so leave
# this off until after the initial sync has run manually.
DEFAULT_STRIPE_SYNC_ON_START = False
DEFAULT_STRIPE_API_KEY = ""

# Saved questions: persisted to disk so they survive across sessions. Each
# entry is `{"id": str (uuid), "text": str}`. Lives separate from settings
# so its file can be wiped/edited independently.
SAVED_QUESTIONS_PATH = Path.home() / ".snoop-doc" / "saved_questions.json"

# Navigation labels. Used both as button labels and as session_state values,
# so the constants and the labels must stay in sync.
VIEW_CHAT = "Ask the Snoop"
VIEW_SAVED = "Saved Questions"
VIEW_DATA = "Data"
VIEW_CONTEXT = "Context"
VIEW_ITEMS = "Saved Items"
VIEW_CHARTS = "Charts demo"
VIEW_INTEGRATIONS = "Integrations"
VIEW_SETTINGS = "Settings"

# Two-tier nav: interactive views first, utility views second. The chat view
# leads and gets a divider beneath it in the sidebar so the rest of the
# primary list (the surfaces you configure / browse, not the surface you
# talk to) sits visually grouped. Integrations + Settings each used to be
# modals; they outgrew that. Integrations sits just above Settings in the
# utility group so external-data wiring is one click away.
PRIMARY_VIEWS = [VIEW_CHAT, VIEW_DATA, VIEW_CONTEXT, VIEW_SAVED, VIEW_ITEMS]
UTILITY_VIEWS = [VIEW_CHARTS, VIEW_INTEGRATIONS, VIEW_SETTINGS]
VIEWS = PRIMARY_VIEWS + UTILITY_VIEWS

PROMPTS_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system.md"
CONTEXT_DIR = Path(__file__).parent / "data" / "context"
# Doc-related context lives one level down so we can load it conditionally
# (only when its CSV is in the picker's scope). General context docs at
# the root are always loaded.
CONTEXT_TABLES_DIR = CONTEXT_DIR / "tables"

# Where persistent UI settings live. Outside the project so it never lands in git,
# and per-user so two people sharing a machine wouldn't clobber each other.
SETTINGS_PATH = Path.home() / ".snoop-doc" / "settings.json"


def _load_context_docs(table_scope: list[str] | None) -> str:
    """Build the "Company & data context" section of the system prompt.

    Two categories of context doc:
      - **General** (`data/context/*.md`, README excluded) — always
        loaded, regardless of the table scope. These describe
        company-wide facts the agent needs across every conversation.
      - **Doc-related** (`data/context/tables/<csv_stem>.md`) — loaded
        only for tables currently in scope. `table_scope=None` means
        "all tables" (no picker filter); a list means just those.

    Each doc is wrapped in a heading using its filename so Claude can
    cite the source when explaining where a piece of context came from.
    """
    parts: list[str] = []
    if CONTEXT_DIR.exists():
        general_files = sorted(
            p for p in CONTEXT_DIR.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".md"
            and p.name.lower() != "readme.md"
        )
        for path in general_files:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            # Strip the YAML frontmatter (schema-pointer metadata for the
            # Data view). The body is what the agent reads — exposing raw
            # `covers: [...]` lines would just be noise it has to ignore.
            body = context_pointers.strip_frontmatter(raw)
            parts.append(f"### From `{path.name}`\n\n{body}")

    # Doc-related: filter by the current table scope. The scope uses
    # sanitized variable names (e.g. `pnl_2026`); the doc file's stem
    # matches the CSV's stem (e.g. `pnl_2026.md`), which `tools.sanitize_name`
    # would map to the same identifier. So we walk the discovered CSVs,
    # filter by scope, and look for a matching .md in tables/.
    if CONTEXT_TABLES_DIR.exists():
        discovered = tools.discover_tables()
        for var_name, csv_path in discovered.items():
            if table_scope is not None and var_name not in table_scope:
                continue
            doc_path = CONTEXT_TABLES_DIR / f"{csv_path.stem}.md"
            if not doc_path.exists():
                continue
            try:
                parts.append(
                    f"### From `tables/{doc_path.name}` (for `{csv_path.name}`)\n\n"
                    f"{doc_path.read_text(encoding='utf-8')}"
                )
            except OSError:
                continue

    if not parts:
        return ""
    return "## Company & data context\n\n" + "\n\n".join(parts)


_PREFER_CHARTS_INSTRUCTION = (
    "## Chart preference\n\n"
    "When your answer involves 3+ data points across time periods, "
    "categories, products, branches, or any dimension worth comparing, "
    "**produce a chart alongside the text** — even if the user didn't "
    "explicitly ask for one. Single-number lookups and short factual "
    "answers don't need charts."
)


def load_system_prompt(table_scope: list[str] | None) -> str:
    """Build the full system prompt: static behavior + optional chart-preference
    nudge + context docs from disk. Doc-related context is filtered to the
    given scope; general context is always included."""
    if SYSTEM_PROMPT_PATH.exists():
        base = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    else:
        base = "You are Snoop Doc, a helpful financial analyst assistant."

    parts = [base]

    if st.session_state.get("prefer_charts", DEFAULT_PREFER_CHARTS):
        parts.append(_PREFER_CHARTS_INSTRUCTION)

    context = _load_context_docs(table_scope)
    if context:
        parts.append(f"---\n\n{context}")

    return "\n\n".join(parts)


def load_saved_settings() -> dict:
    """Read persisted settings from `~/.snoop-doc/settings.json`.

    Returns `{api_key, model}`. Handles both the current single-provider
    format and a leftover multi-provider format from an earlier iteration
    (extracts the Anthropic entries from it).
    """
    if not SETTINGS_PATH.exists():
        return {}
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    # Reverse-migrate the multi-provider shape if we see it on disk.
    if "api_keys" in raw or "models" in raw:
        return {
            "api_key": raw.get("api_keys", {}).get("anthropic", ""),
            "model": raw.get("models", {}).get("anthropic", DEFAULT_MODEL),
        }
    return raw


def save_settings() -> None:
    """Persist current settings to disk."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(
            {
                "api_key": st.session_state.get("api_key", ""),
                "model": st.session_state.get("model", DEFAULT_MODEL),
                "utility_model": st.session_state.get(
                    "utility_model", DEFAULT_UTILITY_MODEL
                ),
                "quick_mode_model": st.session_state.get(
                    "quick_mode_model", DEFAULT_QUICK_MODE_MODEL
                ),
                "temperature": st.session_state.get(
                    "temperature", DEFAULT_TEMPERATURE
                ),
                "max_output_tokens": st.session_state.get(
                    "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS
                ),
                "prefer_charts": st.session_state.get(
                    "prefer_charts", DEFAULT_PREFER_CHARTS
                ),
                "web_search_enabled": st.session_state.get(
                    "web_search_enabled", DEFAULT_WEB_SEARCH_ENABLED
                ),
                "confirm_destructive": st.session_state.get(
                    "confirm_destructive", DEFAULT_CONFIRM_DESTRUCTIVE
                ),
                "google_sync_on_start": st.session_state.get(
                    "google_sync_on_start", DEFAULT_GOOGLE_SYNC_ON_START
                ),
                "stripe_api_key": st.session_state.get(
                    "stripe_api_key", DEFAULT_STRIPE_API_KEY
                ),
                "stripe_sync_on_start": st.session_state.get(
                    "stripe_sync_on_start", DEFAULT_STRIPE_SYNC_ON_START
                ),
                "slack_bot_token": st.session_state.get(
                    "slack_bot_token", DEFAULT_SLACK_BOT_TOKEN
                ),
                "slack_app_token": st.session_state.get(
                    "slack_app_token", DEFAULT_SLACK_APP_TOKEN
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # Lock down file permissions on Unix so other users on the box can't read the key.
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass  # Windows / non-POSIX — nothing to do.


def forget_saved_settings() -> None:
    """Delete the persisted settings file and clear in-session values."""
    if SETTINGS_PATH.exists():
        SETTINGS_PATH.unlink()
    st.session_state.api_key = ""
    st.session_state.model = DEFAULT_MODEL
    st.session_state.utility_model = DEFAULT_UTILITY_MODEL
    st.session_state.quick_mode_model = DEFAULT_QUICK_MODE_MODEL
    st.session_state.temperature = DEFAULT_TEMPERATURE
    st.session_state.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    st.session_state.prefer_charts = DEFAULT_PREFER_CHARTS
    st.session_state.web_search_enabled = DEFAULT_WEB_SEARCH_ENABLED
    st.session_state.confirm_destructive = DEFAULT_CONFIRM_DESTRUCTIVE
    st.session_state.google_sync_on_start = DEFAULT_GOOGLE_SYNC_ON_START
    st.session_state.stripe_api_key = DEFAULT_STRIPE_API_KEY
    st.session_state.stripe_sync_on_start = DEFAULT_STRIPE_SYNC_ON_START
    # Drop dialog widget state so re-opening doesn't show stale values.
    st.session_state.pop("_settings_dialog_api_key", None)
    st.session_state.pop("_settings_dialog_model", None)
    st.session_state.pop("_settings_dialog_utility_model", None)
    st.session_state.pop("_settings_dialog_quick_mode_model", None)
    st.session_state.pop("_settings_dialog_temperature", None)
    st.session_state.pop("_settings_dialog_max_output_tokens", None)
    st.session_state.pop("_settings_dialog_confirm_destructive", None)
    st.session_state.pop("_settings_dialog_google_sync_on_start", None)
    st.session_state.pop("_settings_dialog_stripe_api_key", None)
    st.session_state.pop("_settings_dialog_stripe_sync_on_start", None)


# ---------------------------------------------------------------------------
# Saved questions — pinned reusable prompts, persisted to disk
# ---------------------------------------------------------------------------
def load_saved_questions() -> list[dict]:
    """Read the saved-questions list from disk. Returns empty list on missing/bad file."""
    if not SAVED_QUESTIONS_PATH.exists():
        return []
    try:
        data = json.loads(SAVED_QUESTIONS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [q for q in data if isinstance(q, dict) and "text" in q]
    except (json.JSONDecodeError, OSError):
        pass
    return []


def persist_saved_questions() -> None:
    """Write `st.session_state.saved_questions` to disk."""
    SAVED_QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SAVED_QUESTIONS_PATH.write_text(
        json.dumps(st.session_state.get("saved_questions", []), indent=2),
        encoding="utf-8",
    )
    try:
        os.chmod(SAVED_QUESTIONS_PATH, 0o600)
    except OSError:
        pass


# Confirmation helpers (confirm_or_run) live in `dialogs.py` so views can
# import them without re-importing the main script. They were here originally.


# ---------------------------------------------------------------------------
# Usage tracking — count tokens + estimate cost across a session
# ---------------------------------------------------------------------------
def _empty_usage() -> dict:
    return {
        "input": 0,          # uncached input tokens
        "cache_write": 0,    # tokens written to cache (charged 1.25× input)
        "cache_read": 0,     # tokens read from cache (~10% of input)
        "output": 0,         # tokens generated
        "calls": 0,          # number of API requests
        "cost_usd": 0.0,     # accumulated cost estimate
    }


def add_usage(model: str, usage_obj) -> None:
    """Update session usage counters from one API response.

    `usage_obj` is the `.usage` attribute on an Anthropic response — has
    input_tokens, output_tokens, and the cache counters. Cost is computed
    using the model's prices in PRICING_PER_MTOK; unknown models fall back
    to Sonnet pricing so the meter still ticks.
    """
    if usage_obj is None:
        return
    pricing = PRICING_PER_MTOK.get(model, PRICING_PER_MTOK["claude-sonnet-4-5"])

    in_tok = getattr(usage_obj, "input_tokens", 0) or 0
    out_tok = getattr(usage_obj, "output_tokens", 0) or 0
    cw_tok = getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
    cr_tok = getattr(usage_obj, "cache_read_input_tokens", 0) or 0

    cost = (
        in_tok * pricing["input"]
        + cw_tok * pricing["input"] * CACHE_WRITE_MULTIPLIER
        + cr_tok * pricing["cached_read"]
        + out_tok * pricing["output"]
    ) / 1_000_000

    u = st.session_state.usage
    u["input"] += in_tok
    u["cache_write"] += cw_tok
    u["cache_read"] += cr_tok
    u["output"] += out_tok
    u["calls"] += 1
    u["cost_usd"] += cost


def reset_usage() -> None:
    """Zero out the session counters. Called when chat is cleared."""
    st.session_state.usage = _empty_usage()


# ---------------------------------------------------------------------------
# Google Sheets integration UI (rendered inside the Settings dialog)
# ---------------------------------------------------------------------------
def _render_google_settings_section() -> bool:
    """Render the Google Sheets integration block. Returns the current
    value of the "sync on app start" toggle so the dialog's Save handler
    can persist it.

    Connect / Disconnect / Test buttons act immediately on click (they
    write to disk / fire OAuth synchronously) and don't flow through the
    Save handler — saving is only for the toggle.
    """
    if not gsheets.is_configured():
        st.info(
            "Google Sheets sync isn't set up yet. Add the OAuth client "
            "file at `google-client.json` in the project root. See README "
            "for setup instructions.",
            icon=":material/link_off:",
        )
        # Still let the toggle render so we keep dialog layout stable.
        return bool(st.session_state.get(
            "google_sync_on_start", DEFAULT_GOOGLE_SYNC_ON_START
        ))

    email = gsheets.get_connected_email()
    is_connected = email is not None

    if is_connected:
        st.success(
            f"Connected as **{email}**",
            icon=":material/link:",
        )
    else:
        if gsheets.is_connected():
            # Token file exists but couldn't be refreshed — surface that
            # specifically so the user knows it's a revoke / expiry, not
            # a fresh state.
            st.warning(
                "Google connection expired or was revoked. Reconnect "
                "to keep syncing.",
                icon=":material/link_off:",
            )
        else:
            st.caption(
                "Not connected. Sign in with your Workspace account to "
                "sync Google Sheets into `data/tables/`."
            )

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if not is_connected:
            if st.button(
                "Connect Google",
                icon=":material/login:",
                key="_settings_google_connect",
                use_container_width=True,
            ):
                _do_google_connect()
        else:
            if st.button(
                "Disconnect",
                icon=":material/logout:",
                key="_settings_google_disconnect",
                use_container_width=True,
            ):
                gsheets.disconnect()
                st.rerun()
    with btn_col2:
        if is_connected:
            if st.button(
                "Test connection",
                icon=":material/health_and_safety:",
                key="_settings_google_test",
                use_container_width=True,
            ):
                with st.spinner("Pinging Google…"):
                    fresh_email = gsheets.get_connected_email()
                if fresh_email:
                    st.success(f"Connection OK ({fresh_email}).")
                else:
                    st.error("Connection failed — try reconnecting.")

    return st.toggle(
        "Sync all registered sheets on app start",
        value=bool(st.session_state.get(
            "google_sync_on_start", DEFAULT_GOOGLE_SYNC_ON_START
        )),
        help=(
            "When on, every registered Google Sheets sync is re-pulled "
            "the moment the app starts. Off by default so launching is "
            "fast and predictable. You can always click Sync per file "
            "in the Data view."
        ),
        key="_settings_dialog_google_sync_on_start",
        disabled=not is_connected,
    )


def _render_stripe_settings_section() -> tuple[str, bool]:
    """Render the Stripe integration block. Returns
    `(api_key_input, sync_on_start_toggle)` so the Save handler can persist
    both. The Test button acts immediately on click.
    """
    api_key_input = st.text_input(
        "Stripe restricted API key",
        value=st.session_state.get("stripe_api_key", DEFAULT_STRIPE_API_KEY),
        type="password",
        placeholder="rk_live_… or rk_test_…",
        help=(
            "A **restricted** key with read-only access to Customers, "
            "Subscriptions, Invoices, Charges, Products, and Prices. "
            "Create one at dashboard.stripe.com/apikeys → 'Create "
            "restricted key'. Stored only on this machine; never committed."
        ),
        key="_settings_dialog_stripe_api_key",
    )

    test_col, _spacer = st.columns([1, 1])
    with test_col:
        if st.button(
            "Test connection",
            icon=":material/health_and_safety:",
            key="_settings_stripe_test",
            use_container_width=True,
            disabled=not api_key_input,
        ):
            with st.spinner("Pinging Stripe…"):
                ok, msg = stripe_sync.validate_key(api_key_input)
            (st.success if ok else st.error)(msg)

    sync_on_start = st.toggle(
        "Sync Stripe on app start",
        value=bool(st.session_state.get(
            "stripe_sync_on_start", DEFAULT_STRIPE_SYNC_ON_START
        )),
        help=(
            "When on, every registered Stripe object type is incrementally "
            "re-pulled on app start. **Off by default** — the FIRST sync "
            "for a large Stripe account can take 10–25 minutes; run it "
            "manually from the Data view before enabling this toggle. "
            "After the initial pull, incremental syncs take ~30 seconds."
        ),
        key="_settings_dialog_stripe_sync_on_start",
        disabled=not api_key_input,
    )

    return api_key_input, sync_on_start


def _do_google_connect() -> None:
    """Run the OAuth flow synchronously. Streamlit blocks during this
    (which is fine — a spinner shows). When it returns, we rerun so the
    dialog redraws in the connected state.
    """
    try:
        with st.spinner("Waiting for browser…"):
            email = gsheets.connect()
    except gsheets.NotConfiguredError as e:
        st.error(str(e))
        return
    except gsheets.DepsMissingError as e:
        st.error(str(e))
        return
    except Exception as e:  # noqa: BLE001 — OAuth flow can fail many ways
        st.error(f"Google connect failed: {e}")
        return
    st.success(f"Connected as {email or 'Google'}.")
    st.rerun()


# ---------------------------------------------------------------------------
# Slack integration settings
# ---------------------------------------------------------------------------
DEFAULT_SLACK_BOT_TOKEN = ""
DEFAULT_SLACK_APP_TOKEN = ""


def _render_slack_settings_section() -> tuple[str, str]:
    """Render the Slack Bot block. Returns (bot_token, app_token)."""
    st.caption(
        "Connect a Slack app so you can ask `@snoop` questions directly "
        "from any Slack channel. The bot runs as a separate process "
        "(`python slack_bot.py`) alongside the web app — see **SETUP_SLACK.md** "
        "for the full setup guide."
    )

    bot_token_input = st.text_input(
        "Bot Token (`xoxb-…`)",
        value=st.session_state.get("slack_bot_token", DEFAULT_SLACK_BOT_TOKEN),
        type="password",
        key="_slack_bot_token_input",
        help="OAuth Bot Token from your Slack app's OAuth & Permissions page.",
    )
    app_token_input = st.text_input(
        "App-Level Token (`xapp-…`)",
        value=st.session_state.get("slack_app_token", DEFAULT_SLACK_APP_TOKEN),
        type="password",
        key="_slack_app_token_input",
        help="Generated under your Slack app's Basic Information → App-Level Tokens. Needs the `connections:write` scope.",
    )

    if bot_token_input and app_token_input:
        st.success(
            "Tokens configured. Click **Save integrations**, then run "
            "`python slack_bot.py` to start the bot.",
            icon=":material/check_circle:",
        )
    elif bot_token_input or app_token_input:
        st.info(
            "Both tokens are needed — Bot Token and App-Level Token.",
            icon=":material/info:",
        )
    else:
        st.info(
            "No tokens set. Follow **SETUP_SLACK.md** to create a Slack app, "
            "then paste the tokens here.",
            icon=":material/slack:",
        )

    return bot_token_input, app_token_input


# ---------------------------------------------------------------------------
# Integrations view — full page for wiring external data sources
# ---------------------------------------------------------------------------
def render_integrations_view() -> None:
    """Standalone page for external-data integrations (Google Sheets,
    Stripe, future: FastSpring / Freemius / Lemon Squeezy). Used to live
    inside the Settings page; broke out because (a) it has its own
    distinct mental model — wiring up data sources, not configuring the
    app's behaviour — and (b) the section was already pushing the
    Settings page long.

    Per-source Connect / Disconnect / Test buttons fire immediately on
    click. The Save button at the bottom persists the toggle states +
    the Stripe API key (which is a text_input, so doesn't commit until
    Save).
    """
    st.subheader("Integrations")
    st.caption(
        "Wire up external data sources. Each integration pulls data into "
        "`data/tables/` so Snoop can answer questions against it like "
        "any other CSV."
    )

    st.markdown("### Google Sheets")
    google_sync_on_start_input = _render_google_settings_section()

    st.divider()
    st.markdown("### Stripe")
    stripe_api_key_input, stripe_sync_on_start_input = _render_stripe_settings_section()

    st.divider()
    st.markdown("### Slack Bot")
    slack_bot_token_input, slack_app_token_input = _render_slack_settings_section()

    st.divider()
    save_col, _spacer = st.columns([1, 3])
    with save_col:
        if st.button(
            "Save integrations",
            icon=":material/save:",
            type="primary",
            key="_integrations_save",
            use_container_width=True,
        ):
            st.session_state.google_sync_on_start = bool(google_sync_on_start_input)
            st.session_state.stripe_api_key = stripe_api_key_input
            st.session_state.stripe_sync_on_start = bool(stripe_sync_on_start_input)
            st.session_state.slack_bot_token = slack_bot_token_input
            st.session_state.slack_app_token = slack_app_token_input
            save_settings()
            st.toast("Integrations saved.", icon=":material/check_circle:")
            st.rerun()


# ---------------------------------------------------------------------------
# Settings view — full page, was a modal until the control count outgrew it
# ---------------------------------------------------------------------------
def render_settings_view() -> None:
    """Full-page settings view. Used to live in `@st.dialog` but the
    control count outgrew the modal — too much scrolling, too many
    things to look at in a narrow popup.

    Uses explicit "Save settings" rather than auto-save-on-change because
    Streamlit text_input doesn't always commit its value to session_state
    on a partial interaction — saving was unreliable. The page form gets
    the same explicit-Save discipline.
    """
    st.subheader("Settings")
    st.markdown("**AI**")
    api_key_input = st.text_input(
        "Anthropic API Key",
        value=st.session_state.get("api_key", ""),
        type="password",
        help=(
            f"Saved to `{SETTINGS_PATH}` on this machine so you don't have to "
            "re-enter it. File is gitignored and chmod 600."
        ),
        placeholder="sk-ant-...",
        key="_settings_dialog_api_key",
    )

    current_model = st.session_state.get("model", DEFAULT_MODEL)
    if current_model not in AVAILABLE_MODELS:
        current_model = DEFAULT_MODEL
    model_input = st.selectbox(
        "Chat model",
        options=AVAILABLE_MODELS,
        index=AVAILABLE_MODELS.index(current_model),
        help=(
            "Sonnet is the balanced default. Opus for deeper analysis. "
            "Haiku for speed."
        ),
        key="_settings_dialog_model",
    )
    st.caption(
        "Drives your chat conversations in **Ask the Snoop** — the main "
        "agent loop, every question and follow-up."
    )

    current_util = st.session_state.get("utility_model", DEFAULT_UTILITY_MODEL)
    if current_util not in AVAILABLE_MODELS:
        current_util = DEFAULT_UTILITY_MODEL
    utility_model_input = st.selectbox(
        "Utility model",
        options=AVAILABLE_MODELS,
        index=AVAILABLE_MODELS.index(current_util),
        help=(
            "Doesn't need to be as strong as the chat model — Haiku is "
            "usually fine and much cheaper."
        ),
        key="_settings_dialog_utility_model",
    )
    st.caption(
        "Drives AI helpers in the background — the **AI context-doc "
        "editor** (in Context / Data → Context button). Temperature + "
        "max-output settings below do not apply here."
    )

    current_qm = st.session_state.get("quick_mode_model", DEFAULT_QUICK_MODE_MODEL)
    if current_qm not in AVAILABLE_MODELS:
        current_qm = DEFAULT_QUICK_MODE_MODEL
    quick_mode_model_input = st.selectbox(
        "Quick mode model",
        options=AVAILABLE_MODELS,
        index=AVAILABLE_MODELS.index(current_qm),
        help=(
            "Defaults to Haiku — fastest + cheapest, ideal for one-shot "
            "lookups."
        ),
        key="_settings_dialog_quick_mode_model",
    )
    st.caption(
        "Replaces the chat model whenever the **Quick mode** toggle (above "
        "the chat) is on. Same agent loop, just a different model — so "
        "temperature + max-output settings below still apply."
    )

    temp_lo, temp_hi = TEMPERATURE_RANGE
    temperature_input = st.slider(
        "Temperature",
        min_value=temp_lo,
        max_value=temp_hi,
        value=float(st.session_state.get("temperature", DEFAULT_TEMPERATURE)),
        step=0.1,
        help=(
            "How varied vs deterministic the model is. 0.0 = same question "
            "→ same answer every time (best for reproducible numbers). "
            "1.0 = more creative variation. For finance, 0.0–0.3 is usually "
            "right; default is 1.0."
        ),
        key="_settings_dialog_temperature",
    )
    st.caption(
        "Applies to chat (and Quick mode). Does **not** affect the utility "
        "model — the AI context editor uses Anthropic's default temperature."
    )

    mt_lo, mt_hi = MAX_OUTPUT_TOKENS_RANGE
    max_output_tokens_input = st.number_input(
        "Max output tokens per response",
        min_value=mt_lo,
        max_value=mt_hi,
        value=int(st.session_state.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)),
        step=512,
        help=(
            "Caps how long each response can be. Lower → faster, cheaper, "
            "but may truncate long reports. Higher → allows full board-deck "
            "style outputs. Default 4096; a long-form report might need 8192+."
        ),
        key="_settings_dialog_max_output_tokens",
    )
    st.caption(
        "Applies to chat (and Quick mode). The utility model is pinned at "
        "4096 internally and doesn't read this."
    )

    st.divider()
    st.markdown("**Appearance & behavior**")

    confirm_input = st.toggle(
        "Confirm before destructive actions",
        value=bool(st.session_state.get(
            "confirm_destructive", DEFAULT_CONFIRM_DESTRUCTIVE
        )),
        help=(
            "Show a 'Are you sure?' dialog before clearing chat, deleting "
            "files, or forgetting settings. Turn off if you'd rather have "
            "no second-guessing."
        ),
        key="_settings_dialog_confirm_destructive",
    )

    st.caption(
        f"Last saved: `~/.snoop-doc/settings.json`"
        if SETTINGS_PATH.exists()
        else "Click Save settings to store on this machine."
    )

    st.divider()

    col_save, col_forget = st.columns(2)
    with col_save:
        if st.button(
            "Save settings",
            icon=":material/save:",
            type="primary",
            use_container_width=True,
        ):
            st.session_state.api_key = api_key_input
            st.session_state.model = model_input
            st.session_state.utility_model = utility_model_input
            st.session_state.quick_mode_model = quick_mode_model_input
            st.session_state.temperature = float(temperature_input)
            st.session_state.max_output_tokens = int(max_output_tokens_input)
            st.session_state.confirm_destructive = bool(confirm_input)
            save_settings()
            st.toast("Settings saved.", icon=":material/check_circle:")
            st.rerun()
    with col_forget:
        if st.button(
            "Forget saved",
            icon=theme.ICON["forget"],
            help="Delete the saved settings file and clear the form.",
            use_container_width=True,
        ):
            forget_saved_settings()
            st.rerun()


# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------
# `messages` holds the conversation in the Anthropic API format. Each entry is
# {role, content}; content is either a plain string (real user input) or a list
# of content blocks (assistant turns, and user turns carrying tool_results).
if "messages" not in st.session_state:
    st.session_state.messages = []

# Plotly figures aren't JSON-serializable, so we keep them out of the API
# message history and stash them here keyed by tool_use_id. The chat renderer
# pulls them back in when re-drawing history.
if "figures" not in st.session_state:
    st.session_state.figures = {}

# Same pattern for DataFrames the model produced via `result_table`. Lives
# parallel to figures, both populated from each tool call.
if "tables" not in st.session_state:
    st.session_state.tables = {}

# Session-wide usage counters. Updated after every API call in run_agent_turn.
if "usage" not in st.session_state:
    st.session_state.usage = _empty_usage()

# Quick mode: when on, every request goes through Haiku regardless of the
# model picked in Settings. Useful for lookups where Sonnet/Opus is overkill.
if "quick_mode" not in st.session_state:
    st.session_state.quick_mode = False

# First-run precedence for settings:
#   1. Persisted settings file (what the user last set in the UI).
#   2. ANTHROPIC_API_KEY environment variable (handy for CLI users).
#   3. Empty / default.
_saved = load_saved_settings()
if "api_key" not in st.session_state:
    st.session_state.api_key = (
        _saved.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    )
if "model" not in st.session_state:
    st.session_state.model = _saved.get("model", DEFAULT_MODEL)
if "utility_model" not in st.session_state:
    saved_util = _saved.get("utility_model", DEFAULT_UTILITY_MODEL)
    st.session_state.utility_model = (
        saved_util if saved_util in AVAILABLE_MODELS else DEFAULT_UTILITY_MODEL
    )
if "quick_mode_model" not in st.session_state:
    saved_qm = _saved.get("quick_mode_model", DEFAULT_QUICK_MODE_MODEL)
    st.session_state.quick_mode_model = (
        saved_qm if saved_qm in AVAILABLE_MODELS else DEFAULT_QUICK_MODE_MODEL
    )
if "temperature" not in st.session_state:
    saved_temp = float(_saved.get("temperature", DEFAULT_TEMPERATURE))
    lo, hi = TEMPERATURE_RANGE
    st.session_state.temperature = max(lo, min(hi, saved_temp))
if "max_output_tokens" not in st.session_state:
    saved_mt = _saved.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    lo, hi = MAX_OUTPUT_TOKENS_RANGE
    st.session_state.max_output_tokens = max(lo, min(hi, int(saved_mt)))
if "prefer_charts" not in st.session_state:
    st.session_state.prefer_charts = bool(
        _saved.get("prefer_charts", DEFAULT_PREFER_CHARTS)
    )
if "web_search_enabled" not in st.session_state:
    st.session_state.web_search_enabled = bool(
        _saved.get("web_search_enabled", DEFAULT_WEB_SEARCH_ENABLED)
    )
if "confirm_destructive" not in st.session_state:
    st.session_state.confirm_destructive = bool(
        _saved.get("confirm_destructive", DEFAULT_CONFIRM_DESTRUCTIVE)
    )
if "google_sync_on_start" not in st.session_state:
    st.session_state.google_sync_on_start = bool(
        _saved.get("google_sync_on_start", DEFAULT_GOOGLE_SYNC_ON_START)
    )
if "stripe_api_key" not in st.session_state:
    st.session_state.stripe_api_key = str(
        _saved.get("stripe_api_key", DEFAULT_STRIPE_API_KEY)
    )
if "stripe_sync_on_start" not in st.session_state:
    st.session_state.stripe_sync_on_start = bool(
        _saved.get("stripe_sync_on_start", DEFAULT_STRIPE_SYNC_ON_START)
    )
if "slack_bot_token" not in st.session_state:
    st.session_state.slack_bot_token = str(
        _saved.get("slack_bot_token", DEFAULT_SLACK_BOT_TOKEN)
    )
if "slack_app_token" not in st.session_state:
    st.session_state.slack_app_token = str(
        _saved.get("slack_app_token", DEFAULT_SLACK_APP_TOKEN)
    )
if "saved_questions" not in st.session_state:
    st.session_state.saved_questions = load_saved_questions()
# Used by Stop button to interrupt the agent loop between iterations.
if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False

# Current view defaults to Chat on first run; navigation buttons update it.
if "view" not in st.session_state:
    st.session_state.view = VIEW_CHAT

# Sync-on-start (Google): when the user has enabled the toggle AND a
# Google connection exists, re-pull every registered sheet ONCE per
# Streamlit session. Gated by the `_google_sync_on_start_done` flag so
# reruns (button clicks, view switches) don't repeat the sync.
if (
    "_google_sync_on_start_done" not in st.session_state
    and st.session_state.get("google_sync_on_start", DEFAULT_GOOGLE_SYNC_ON_START)
    and gsheets.is_configured()
    and gsheets.is_connected()
    and gsheets.load_registry()
):
    with st.spinner("Syncing Google Sheets…"):
        try:
            updated = gsheets.sync_all()
        except Exception as e:  # noqa: BLE001 — never let a sync error kill the app
            st.toast(f"Google sync failed: {e}", icon=":material/sync_problem:")
            updated = []
    failed = [e for e in updated if e.last_sync_error]
    if failed:
        st.toast(
            f"Synced {len(updated)} sheet(s); {len(failed)} had errors. "
            "See the Data view for details.",
            icon=":material/sync_problem:",
        )
    elif updated:
        st.toast(
            f"Synced {len(updated)} Google Sheet(s).",
            icon=":material/cloud_done:",
        )
st.session_state._google_sync_on_start_done = True

# Sync-on-start (Stripe): same pattern. Requires API key + toggle + at
# least one previously-synced object (so the user can't accidentally
# trigger a 25-min initial sync at app start — toggle is a no-op until
# they've done the first manual sync from the Data view).
if (
    "_stripe_sync_on_start_done" not in st.session_state
    and st.session_state.get("stripe_sync_on_start", DEFAULT_STRIPE_SYNC_ON_START)
    and st.session_state.get("stripe_api_key", "")
    and stripe_sync.load_registry().get("objects")
):
    with st.spinner("Syncing Stripe…"):
        try:
            results = stripe_sync.sync_all(st.session_state.stripe_api_key)
        except Exception as e:  # noqa: BLE001
            st.toast(f"Stripe sync failed: {e}", icon=":material/sync_problem:")
            results = []
    failed = [(k, r) for k, r in results if not r.get("ok")]
    total_rows = sum(r["rows"] for _, r in results if r.get("ok"))
    if failed:
        st.toast(
            f"Stripe sync: {len(results) - len(failed)} of {len(results)} "
            f"object types succeeded. {len(failed)} failed — see the Data view.",
            icon=":material/sync_problem:",
        )
    elif results:
        st.toast(
            f"Stripe sync complete · {total_rows:,} rows refreshed.",
            icon=":material/cloud_done:",
        )
st.session_state._stripe_sync_on_start_done = True


def _switch_view(target: str) -> None:
    """Callback for navigation buttons: change view and rerun cleanly."""
    st.session_state.view = target


# ---------------------------------------------------------------------------
# Sidebar — title, navigation, settings
# ---------------------------------------------------------------------------
NAV_ICONS = {
    VIEW_CHAT: theme.ICON["chat"],
    VIEW_SAVED: theme.ICON["saved"],
    VIEW_ITEMS: theme.ICON["items"],
    VIEW_DATA: theme.ICON["data"],
    VIEW_CONTEXT: theme.ICON["context_doc"],
    VIEW_CHARTS: theme.ICON["charts"],
    VIEW_INTEGRATIONS: theme.ICON["integrations"],
    VIEW_SETTINGS: theme.ICON["settings"],
}


def _render_nav_button(view_name: str) -> None:
    """One nav entry. Ask the Snoop renders as a real Streamlit button
    (primary or secondary) — it's the primary CTA. Every other nav
    entry uses `type="tertiary"` when inactive (Streamlit natively
    renders tertiary buttons as link-style), and `type="primary"` when
    active (filled indigo row). Non-chat items also get wrapped in
    `st.container(key="snoop_navlink_<view>")` as a redundant CSS hook,
    so styling has two ways to attach even on older Streamlit versions
    where tertiary isn't recognised or the container-key class isn't
    rendered."""
    is_active = st.session_state.view == view_name
    is_chat = view_name == VIEW_CHAT

    if is_chat:
        button_type = "primary" if is_active else "secondary"
    else:
        button_type = "primary" if is_active else "tertiary"

    def _render() -> None:
        st.button(
            view_name,
            icon=NAV_ICONS.get(view_name),
            key=f"nav_{view_name}",
            use_container_width=True,
            type=button_type,
            on_click=_switch_view,
            args=(view_name,),
        )

    if is_chat:
        _render()
    else:
        with st.container(key=f"snoop_navlink_{view_name.replace(' ', '_')}"):
            _render()


with st.sidebar:
    # The logo lives in Streamlit's dedicated logo slot above this content
    # area (set up via `theme.install_logo()` at startup). Sidebar content
    # below starts straight into the navigation.

    # Primary nav — the interactive surfaces of the app. Ask the Snoop
    # leads, and its companion actions (Clear chat always; Stop while a
    # conversation is in flight) sit immediately beneath it so the chat's
    # destructive controls live with its nav entry, regardless of which
    # view you're currently looking at. A divider closes the chat group
    # off from the rest of the primary nav.
    for view_name in PRIMARY_VIEWS:
        _render_nav_button(view_name)
        if view_name == VIEW_CHAT:
            def _do_clear_chat() -> None:
                st.session_state.messages = []
                st.session_state.figures = {}
                st.session_state.tables = {}
                reset_usage()

            # Clear chat + Save chat + Stop pick up the same link-style
            # treatment as the other nav items (Ask the Snoop is the only
            # real button — these are companion actions, not the primary
            # CTA). Tertiary type gives Streamlit's native link button
            # look; the container-key wrap is a redundant CSS hook.
            with st.container(key="snoop_navlink_clear_chat"):
                if st.button(
                    "Clear chat",
                    icon=theme.ICON["clear"],
                    key="clear_chat",
                    use_container_width=True,
                    type="tertiary",
                ):
                    confirm_or_run(
                        "Clear all messages in this conversation? This can't be undone.",
                        "Yes, clear",
                        _do_clear_chat,
                    )

            # Save chat — only meaningful when there's a chat to save AND
            # we're on the chat view (otherwise the user isn't looking at
            # what they're about to save). Opens the save dialog in
            # saved_items, which reads session_state directly.
            if (
                st.session_state.view == VIEW_CHAT
                and st.session_state.messages
            ):
                with st.container(key="snoop_navlink_save_chat"):
                    if st.button(
                        "Save chat",
                        icon=":material/bookmark_add:",
                        key="save_chat",
                        use_container_width=True,
                        type="tertiary",
                        help=(
                            "Save the whole conversation — every question, "
                            "response, chart, and table — so you can come "
                            "back to it later from Saved Items › Conversations."
                        ),
                    ):
                        saved_items.show_save_conversation_dialog()

            # Stop button — sets a flag the agent loop checks between
            # iterations. Clicking also triggers a Streamlit rerun,
            # which interrupts in-flight Python (though not an in-flight
            # API call). Visible only when a conversation is in flight
            # AND we're on the chat view — outside the chat view, the
            # agent isn't running so there's nothing to stop.
            if (
                st.session_state.view == VIEW_CHAT
                and st.session_state.messages
            ):
                with st.container(key="snoop_navlink_stop"):
                    if st.button(
                        "Stop",
                        icon=":material/stop_circle:",
                        key="stop_agent",
                        use_container_width=True,
                        type="tertiary",
                        help=(
                            "Stop the agent after its current step finishes. "
                            "Doesn't interrupt an in-flight API call, but "
                            "prevents further iterations."
                        ),
                    ):
                        st.session_state.stop_requested = True
                        st.rerun()

            st.divider()

    st.divider()

    # Utility section — non-conversational surfaces (Charts demo, Settings).
    for view_name in UTILITY_VIEWS:
        _render_nav_button(view_name)

    # Quick mode + Prefer charts toggles live at the top of the chat view
    # itself now (under the data-files picker), not in the sidebar.

    # Visual separator between the nav (Settings is the last entry) and
    # the session usage strip below.
    st.divider()

    # Session usage meter — totals reset when Clear chat is clicked.
    _u = st.session_state.usage
    if _u["calls"] > 0:
        total_in = _u["input"] + _u["cache_write"] + _u["cache_read"]
        st.markdown(
            f"""
            <div style="font-size: 0.8rem; color: #697386; line-height: 1.5;">
              <strong style="color: #1A1F36;">Session usage</strong><br/>
              {_u['calls']} call{'s' if _u['calls'] != 1 else ''}
              · {total_in:,} in · {_u['output']:,} out<br/>
              <span style="color: #1A1F36;">≈ ${_u['cost_usd']:.4f}</span>
              <span title="Cached read tokens cost ~10% of regular input.&#10;Cache writes cost 1.25× input.&#10;Estimate based on model list pricing.">ℹ</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.caption("Session usage will appear after your first question.")

# ---------------------------------------------------------------------------
# Chat rendering helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Confidence + source-reference badge
# ---------------------------------------------------------------------------
_CONFIDENCE_RE = re.compile(
    r"<snoop-confidence>(.*?)</snoop-confidence>", re.DOTALL
)

_CONFIDENCE_STYLE = {
    "high":   ("#065F46", "#D1FAE5", "#6EE7B7", "High"),
    "medium": ("#92400E", "#FEF3C7", "#FCD34D", "Medium"),
    "low":    ("#991B1B", "#FEE2E2", "#FCA5A5", "Low"),
}


def _parse_confidence(text: str) -> tuple[str, dict | None]:
    """Strip the <snoop-confidence> block from *text* and return
    (clean_text, parsed_dict).  Returns (text, None) when no block is found."""
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return text, None
    try:
        data = json.loads(m.group(1).strip())
    except json.JSONDecodeError:
        data = None
    clean = _CONFIDENCE_RE.sub("", text).rstrip()
    return clean, data


def _render_confidence_badge(conf: dict) -> None:
    """Render a colour-coded confidence + source-references card."""
    level = (conf.get("level") or "").lower()
    reason = conf.get("reason", "")
    sources: list[str] = conf.get("sources") or []

    text_color, bg_color, border_color, label = _CONFIDENCE_STYLE.get(
        level, ("#1A1F36", "#F6F9FC", "#E3E8EE", level.capitalize() or "Unknown")
    )

    sources_html = ""
    if sources:
        tags = "".join(
            f'<span style="background:#EEF2FF;color:#3730A3;border-radius:3px;'
            f'padding:1px 7px;font-size:0.75rem;margin-right:4px;">{s}</span>'
            for s in sources
        )
        sources_html = (
            f'<div style="margin-top:5px;font-size:0.75rem;color:#697386;">'
            f'Sources:&nbsp;{tags}</div>'
        )

    st.markdown(
        f'<div style="border-left:3px solid {border_color};background:{bg_color};'
        f'border-radius:0 6px 6px 0;padding:7px 12px;margin-top:10px;">'
        f'<span style="font-weight:600;font-size:0.8rem;color:{text_color};">&#9679; {label} confidence</span>'
        f'<span style="font-size:0.8rem;color:#697386;margin-left:8px;">{html.escape(reason)}</span>'
        f'{sources_html}</div>',
        unsafe_allow_html=True,
    )


def _render_copy_button(text: str) -> None:
    """A small clipboard button rendered inside an iframe so its inline JS
    runs without being blocked by Streamlit's page-level CSP.

    Uses the older `document.execCommand('copy')` rather than the modern
    `navigator.clipboard.writeText()` — execCommand works in iframes without
    requiring explicit clipboard permissions, which `components.html` doesn't
    expose. The visible behaviour is identical from the user's perspective.

    The text is passed as a JS string literal via `json.dumps`, which safely
    handles quotes, newlines, and unicode. Since it lives inside `<script>`
    tag content (not an HTML attribute), no HTML escaping is needed.
    """
    if not text:
        return
    text_js = json.dumps(text)
    components.html(
        f"""
        <html><head><style>
          body {{ margin: 0; padding: 0; font-family: system-ui, sans-serif; }}
          .row {{ display: flex; justify-content: flex-end; padding: 4px 0; }}
          button {{
            background: transparent;
            border: 1px solid #E3E8EE;
            border-radius: 4px;
            padding: 3px 12px;
            font-size: 0.75rem;
            color: #697386;
            cursor: pointer;
            font-family: inherit;
            transition: background 80ms ease;
          }}
          button:hover {{ background: #F6F9FC; color: #1A1F36; }}
        </style></head><body>
          <div class="row">
            <button id="copy-btn">Copy</button>
          </div>
          <script>
            (function() {{
              const btn = document.getElementById("copy-btn");
              const text = {text_js};
              btn.addEventListener("click", function() {{
                // execCommand is more reliable than navigator.clipboard in
                // a sandboxed iframe without explicit permissions.
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                try {{ document.execCommand("copy"); }}
                catch (e) {{ /* swallow — button label will not change */ }}
                document.body.removeChild(ta);
                btn.innerText = "✓ Copied";
                setTimeout(function() {{ btn.innerText = "Copy"; }}, 1500);
              }});
            }})();
          </script>
        </body></html>
        """,
        height=36,
    )


def _assemble_agent_tools(scope: list[str] | None) -> list[dict]:
    """Build the full tools list for one API call.

    Always includes: run_python (table scope baked into the description),
    save_memory (so the agent can persist durable facts), and web_fetch
    (so the agent can pull a specific URL when the user mentions one).
    Conditionally includes web_search when the user has toggled it on
    — it costs extra per request, so we don't enable it for everyone
    on every turn.
    """
    tools_list = list(tools.build_tools(selected_tables=scope))  # run_python
    tools_list.append(memory.MEMORY_TOOL)
    tools_list.append(ANTHROPIC_WEB_FETCH_TOOL)
    if st.session_state.get("web_search_enabled", DEFAULT_WEB_SEARCH_ENABLED):
        tools_list.append(ANTHROPIC_WEB_SEARCH_TOOL)
    return tools_list


def _md_chat(text: str) -> str:
    """Pre-process text before rendering with `st.markdown` in chat
    bubbles. Streamlit's markdown component interprets `$...$` as LaTeX
    math mode, which mangles every finance message (`$538.00 / a / f /
    t / e / r / $458.00`). Escape bare dollar signs to `\\$` so they
    render as literal `$`. Saved-as-report rendering uses the standalone
    `markdown` package which doesn't have this problem, so no escape
    is needed there.
    """
    return text.replace("$", r"\$")


def _render_exchange_action_row(
    question: str,
    assistant_blocks: list[dict],
    copy_text: str,
    key_suffix: str,
) -> None:
    """Render the Save-as-report + Copy buttons at the bottom of one
    assistant exchange. Both align right via narrow columns so the row
    sits unobtrusively below the response. The Save button opens the
    save dialog in `saved_items`; the Copy button is the existing
    iframe-based clipboard helper."""
    if not assistant_blocks and not copy_text:
        return
    _spacer, save_col, copy_col = st.columns([5, 1.2, 1])
    with save_col:
        if assistant_blocks:
            if st.button(
                "Save as report",
                icon=":material/article:",
                key=f"save_report_btn_{key_suffix}",
                use_container_width=True,
                help="Save this exchange as a self-contained HTML report.",
            ):
                saved_items.show_save_report_dialog(question, assistant_blocks)
    with copy_col:
        if copy_text:
            _render_copy_button(copy_text)


def _assistant_text_for_copy(content) -> str:
    """Extract a plain-markdown version of an assistant message for clipboard.
    Strips <snoop-confidence> blocks so raw tags never land in the clipboard."""
    if isinstance(content, str):
        return _CONFIDENCE_RE.sub("", content).rstrip()
    if isinstance(content, list):
        parts = [
            _CONFIDENCE_RE.sub("", b.get("text", "")).rstrip()
            for b in content if b.get("type") == "text"
        ]
        return "\n\n".join(p for p in parts if p)
    return ""



def _render_block_in_assistant_bubble(block: dict, key_suffix: str) -> None:
    """Render a single content block inside an already-open assistant bubble.

    Handles every block type we expect to see in the assistant's response:
      - text: assistant prose
      - tool_use: a client-side tool call. Branches by name —
          `run_python` shows a collapsed code expander, `save_memory`
          shows a small "saved to memory" caption.
      - tool_result: result we returned to Claude (figure + stdout/error
          expander for run_python; small caption for save_memory).
      - server_tool_use: Anthropic-side tool the model invoked
          (web_search / web_fetch). We don't run it — Anthropic did —
          but we render a caption so the user sees it happened.
      - web_search_tool_result / web_fetch_tool_result: results from
          those server tools. Rendered as a sources list in an expander.
    """
    btype = block.get("type")

    if btype == "text":
        raw = block.get("text", "")
        clean, conf = _parse_confidence(raw)
        st.markdown(_md_chat(clean))
        if conf:
            _render_confidence_badge(conf)

    elif btype == "tool_use":
        name = block.get("name", "")
        if name == "save_memory":
            content = block.get("input", {}).get("content", "")
            category = block.get("input", {}).get("category", "fact")
            st.caption(f"✎ Saved to memory ({category}) — {content}")
        else:
            # Default: run_python (or any other client tool). Show the
            # code in a collapsed expander.
            code = block.get("input", {}).get("code", "")
            with st.expander("Ran code", expanded=False, icon=theme.ICON["code"]):
                st.code(code, language="python")

    elif btype == "server_tool_use":
        name = block.get("name", "")
        inp = block.get("input", {}) or {}
        if name == "web_search":
            query = inp.get("query", "")
            st.caption(f":material/search: Searched the web for: **{query}**")
        elif name == "web_fetch":
            url = inp.get("url", "")
            st.caption(f":material/language: Fetched: {url}")
        else:
            st.caption(f":material/cloud_sync: Used server tool: `{name}`")

    elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
        results = block.get("content", [])
        # web_search returns a list of {url, title, page_age, encrypted_content};
        # web_fetch returns a single page document. Render compactly in
        # an expander so the chat stays readable.
        with st.expander(
            "Web search results" if btype == "web_search_tool_result" else "Fetched page",
            expanded=False,
            icon=":material/link:",
        ):
            if isinstance(results, list):
                for item in results[:10]:  # cap the display
                    if isinstance(item, dict):
                        title = item.get("title") or item.get("url") or "(untitled)"
                        url = item.get("url", "")
                        if url:
                            st.markdown(f"- [{title}]({url})")
                        else:
                            st.markdown(f"- {title}")
                    else:
                        st.text(str(item))
            else:
                st.text(str(results))

    elif btype == "tool_result":
        tool_use_id = block.get("tool_use_id", "")
        fig = st.session_state.figures.get(tool_use_id)
        if fig is not None:
            st.plotly_chart(
                fig, use_container_width=True, key=f"fig_{tool_use_id}_{key_suffix}"
            )
            # Save-chart button below each rendered chart.
            _spacer, save_col = st.columns([5, 1])
            with save_col:
                if st.button(
                    "Save",
                    icon=":material/bookmark_add:",
                    key=f"save_fig_{tool_use_id}_{key_suffix}",
                    use_container_width=True,
                    help="Save this chart to Saved Items.",
                ):
                    saved_items.show_save_chart_dialog(tool_use_id)

        df = st.session_state.tables.get(tool_use_id)
        if df is not None:
            st.dataframe(df, use_container_width=True)
            _spacer, dl_col, save_col = st.columns([5, 1, 1])
            with dl_col:
                try:
                    excel_bytes = doc_export.dataframe_to_excel_bytes(df)
                    st.download_button(
                        "",
                        data=excel_bytes,
                        file_name="table.xlsx",
                        mime=(
                            "application/vnd.openxmlformats-officedocument"
                            ".spreadsheetml.sheet"
                        ),
                        icon=":material/table_view:",
                        key=f"dl_tbl_{tool_use_id}_{key_suffix}",
                        use_container_width=True,
                        help="Download as Excel (.xlsx)",
                    )
                except RuntimeError:
                    pass  # openpyxl not installed — skip silently
            with save_col:
                if st.button(
                    "Save",
                    icon=":material/bookmark_add:",
                    key=f"save_tbl_{tool_use_id}_{key_suffix}",
                    use_container_width=True,
                    help="Save this table to Saved Items.",
                ):
                    saved_items.show_save_table_dialog(tool_use_id)

        result_text = block.get("content", "")
        is_error = isinstance(result_text, str) and result_text.startswith("Error:")
        if isinstance(result_text, str) and result_text.strip():
            with st.expander("Output", expanded=is_error, icon=theme.ICON["output"]):
                if is_error:
                    st.error(result_text)
                else:
                    st.text(result_text)


def render_history() -> None:
    """Re-render the full chat from session_state.messages on each script run.

    User text inputs get a user bubble; every assistant message (and the
    tool_result message that follows a tool_use) gets folded into a single
    assistant bubble per exchange so the UI stays readable. Each assistant
    exchange gets a Copy button at the bottom.
    """
    msgs = st.session_state.messages
    i = 0
    while i < len(msgs):
        msg = msgs[i]

        # Real user input — show in its own bubble.
        if msg["role"] == "user" and isinstance(msg["content"], str):
            user_question = msg["content"]
            with st.chat_message("user"):
                st.markdown(_md_chat(user_question))
            exchange_start_idx = i  # for unique button key
            i += 1

            # Everything until the next plain-text user message belongs to
            # this exchange and goes into a single assistant bubble.
            with st.chat_message("assistant"):
                copy_payload_parts: list[str] = []
                all_blocks_for_exchange: list[dict] = []
                while i < len(msgs) and not (
                    msgs[i]["role"] == "user" and isinstance(msgs[i]["content"], str)
                ):
                    blocks = msgs[i]["content"]
                    if isinstance(blocks, list):
                        for block in blocks:
                            _render_block_in_assistant_bubble(block, key_suffix=str(i))
                            all_blocks_for_exchange.append(block)
                        if msgs[i]["role"] == "assistant":
                            copy_payload_parts.append(_assistant_text_for_copy(blocks))
                    i += 1
                # Save-as-report + Copy buttons at the bottom of the exchange.
                full_text = "\n\n".join(p for p in copy_payload_parts if p)
                _render_exchange_action_row(
                    question=user_question,
                    assistant_blocks=all_blocks_for_exchange,
                    copy_text=full_text,
                    key_suffix=f"hist_{exchange_start_idx}",
                )
        else:
            # Shouldn't happen if message flow is correct, but skip defensively.
            i += 1


# ---------------------------------------------------------------------------
# Agent loop — one user turn, possibly many tool iterations
# ---------------------------------------------------------------------------
# Quick mode model is configurable via Settings — see DEFAULT_QUICK_MODE_MODEL
# at the top of the file and `_effective_model()` below.


# ---------------------------------------------------------------------------
# Table scope — which CSVs are visible to the agent for this conversation
# ---------------------------------------------------------------------------
def _scope_key(name: str) -> str:
    """Stable session_state key for the per-table scope checkbox."""
    return f"_scope_chk_{name}"


def _current_table_scope() -> list[str] | None:
    """Return the list of table names currently selected, or None for "all".

    The source of truth is each table's checkbox state, stored under
    `_scope_chk_<name>`. A missing key means "checked" (we default to all
    tables included). Returning None lets `tools` skip the filter entirely
    in the common case (everything selected).

    Empty selection (every box unchecked) is also treated as "all" — there
    isn't a useful interpretation of "exclude everything" in a chat that's
    about answering questions, and the picker label already reads "Working
    with all N data files" in that state. A user who's unchecked everything
    most likely meant to reset and select a fresh subset.
    """
    available = list(tools.discover_tables().keys())
    if not available:
        return None
    selected = [n for n in available if st.session_state.get(_scope_key(n), True)]
    if not selected or len(selected) == len(available):
        return None  # nothing or everything = no filter
    return selected


def _render_scope_picker() -> None:
    """Compact 'Working with N of M files' popover above the chat history."""
    available = list(tools.discover_tables().keys())
    if not available:
        return  # nothing to scope

    scope = _current_table_scope()
    total = len(available)
    active = total if scope is None else len(scope)

    label = (
        f"Working with all {total} data file{'s' if total != 1 else ''}"
        if scope is None
        else f"Working with {active} of {total} data files"
    )

    with st.popover(label, icon=":material/folder_open:"):
        st.caption("Choose which data files the agent can see in this conversation.")
        col_all, col_none = st.columns(2)
        with col_all:
            if st.button("Select all", key="_scope_select_all", use_container_width=True):
                for n in available:
                    st.session_state[_scope_key(n)] = True
                st.rerun()
        with col_none:
            if st.button("Clear all", key="_scope_clear_all", use_container_width=True):
                for n in available:
                    st.session_state[_scope_key(n)] = False
                st.rerun()
        st.markdown("---")
        for n in available:
            key = _scope_key(n)
            if key not in st.session_state:
                st.session_state[key] = True
            st.checkbox(n, key=key)


def _messages_for_api(messages: list[dict]) -> list[dict]:
    """Strip session-only fields (like `timestamp`) from messages before
    sending to Anthropic. The API rejects any unknown keys with a 400; we
    keep extras in session_state only and strip them on the way out.
    """
    return [
        {k: v for k, v in m.items() if k in ("role", "content")}
        for m in messages
    ]


def _safe_chart_info(fig) -> str:
    """One-line description of a Plotly figure for the status log.

    Defensively coded — must never raise. Plotly trace data lives in different
    attributes depending on chart type (`y` for line/bar/scatter, `values`
    for pie/treemap, `z` for heatmap) and may be numpy arrays whose truthiness
    can't be evaluated. We try each candidate attribute behind try/except.
    """
    if fig is None:
        return "no chart"
    try:
        traces = list(fig.data)
    except Exception:  # noqa: BLE001
        return "chart (could not inspect)"

    n_traces = len(traces)
    n_points = 0
    for tr in traces:
        for attr in ("y", "values", "z"):
            try:
                val = getattr(tr, attr, None)
                if val is None:
                    continue
                size = int(val.size) if hasattr(val, "size") else len(val)
                n_points += size
                break
            except Exception:  # noqa: BLE001
                continue
    return f"chart with {n_traces} traces / ~{n_points} data points"


def _effective_model() -> str:
    """Which model to actually call: Settings choice for the chat, or the
    Quick-mode model when that toggle is on."""
    if st.session_state.get("quick_mode"):
        return st.session_state.get("quick_mode_model", DEFAULT_QUICK_MODE_MODEL)
    return st.session_state.model


def run_agent_turn(client: anthropic.Anthropic, user_text: str) -> None:
    """Send the user's message, then keep calling tools until Claude is done.

    Renders the full response after it arrives (non-streaming). Tracks token
    usage + cost. Checks `stop_requested` between iterations so the user can
    halt a runaway loop without refreshing the browser.
    """
    # Reset the stop flag at the start of each turn.
    st.session_state.stop_requested = False

    # Record + show the user's message.
    st.session_state.messages.append({"role": "user", "content": user_text})
    with st.chat_message("user"):
        st.markdown(_md_chat(user_text))

    model = _effective_model()

    # `selected_tables=None` means "all"; otherwise only the user-picked
    # subset is loaded into the sandbox and described to the model. The
    # scope is also what decides which doc-related context docs make it
    # into the system prompt.
    scope = _current_table_scope()

    # System prompt + tool description are stable across the conversation, so
    # cache them with Anthropic's prompt cache. Saves tokens on every turn
    # after the first within a ~5-minute window.
    system_blocks = [
        {
            "type": "text",
            "text": load_system_prompt(scope),
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # One execution namespace for the whole question. Each tool call reuses
    # it, so variables/helpers/DataFrame mutations persist across iterations
    # within the question. Next question starts fresh.
    sandbox = tools.build_initial_namespace(selected_tables=scope)

    # Single assistant bubble that grows as Claude does more work.
    with st.chat_message("assistant"):
        # Status panel — shows real-time progress so a hung run is debuggable.
        # Stays expanded while running; collapses to a summary on success;
        # stays open in error state.
        status = st.status(f"Working with `{model}`…", expanded=True)
        turn_start = time.time()

        # Unbounded loop: agent runs until the model returns `end_turn` or
        # the user clicks Stop. Iteration is 1-indexed for log readability.
        iteration = 0
        while True:
            iteration += 1
            # Honour a Stop request between iterations.
            if st.session_state.get("stop_requested"):
                total_dur = time.time() - turn_start
                status.update(
                    label=f"Stopped by user after {iteration - 1} iteration(s) in {total_dur:.1f}s",
                    state="error",
                    expanded=False,
                )
                st.session_state.stop_requested = False
                return

            status.write(f"**Iteration {iteration}** · calling Claude…")

            t0 = time.time()
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=st.session_state.max_output_tokens,
                    temperature=st.session_state.temperature,
                    system=system_blocks,
                    tools=_assemble_agent_tools(scope),
                    messages=_messages_for_api(st.session_state.messages),
                )
            except anthropic.AuthenticationError:
                status.update(label="Authentication failed", state="error")
                st.error("Authentication failed. Check your API key in Settings.")
                st.session_state.messages.pop()
                return
            except anthropic.APIError as e:
                status.update(label="API error", state="error")
                st.error(f"API error: {e}")
                st.session_state.messages.pop()
                return

            api_dur = time.time() - t0

            # Per-call token breakdown — exposes whether caching is hitting.
            u = getattr(response, "usage", None)
            if u is not None:
                in_tok = getattr(u, "input_tokens", 0) or 0
                out_tok = getattr(u, "output_tokens", 0) or 0
                cr = getattr(u, "cache_read_input_tokens", 0) or 0
                cw = getattr(u, "cache_creation_input_tokens", 0) or 0
                status.write(
                    f"  ↳ API: **{api_dur:.1f}s** · "
                    f"in {in_tok} fresh / {cr} cached / {cw} written · "
                    f"out {out_tok}"
                )
            else:
                status.write(f"  ↳ API: **{api_dur:.1f}s** (no usage info)")

            add_usage(model, u)

            # Convert content blocks into plain dicts for history + rendering.
            # `text` and `tool_use` are hand-mapped (the existing pattern).
            # Server-side tool blocks (web_search / web_fetch invocations
            # and results) come back with shapes we don't want to hardcode
            # — use `model_dump()` to preserve them faithfully so they
            # round-trip back to the API on the next turn.
            assistant_content: list[dict] = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                else:
                    # server_tool_use, web_search_tool_result,
                    # web_fetch_tool_result, or any future block type.
                    # model_dump returns a plain dict that the API
                    # accepts back as-is when we replay history.
                    try:
                        assistant_content.append(block.model_dump())
                    except AttributeError:
                        # Pre-Pydantic SDK shape — fall back to dict-of-attrs.
                        assistant_content.append(
                            {k: getattr(block, k) for k in dir(block)
                             if not k.startswith("_") and not callable(getattr(block, k))}
                        )
            st.session_state.messages.append(
                {"role": "assistant", "content": assistant_content}
            )

            n_text = sum(1 for b in assistant_content if b["type"] == "text")
            n_tools = sum(1 for b in assistant_content if b["type"] == "tool_use")
            status.write(
                f"  ↳ Response: {n_text} text block(s), {n_tools} tool call(s), "
                f"stop_reason = `{response.stop_reason}`"
            )

            # Render text + code expanders for this iteration's blocks.
            for block_dict in assistant_content:
                _render_block_in_assistant_bubble(
                    block_dict, key_suffix=f"live_{iteration}"
                )

            # Execute tool calls and feed results back. We only handle
            # client-side `tool_use` blocks here — `server_tool_use`
            # blocks (web search / fetch) are auto-executed by Anthropic
            # and their results already arrived in this same response.
            tool_results: list[dict] = []
            for block_dict in assistant_content:
                if block_dict.get("type") != "tool_use":
                    continue

                tool_name = block_dict.get("name", "")

                # save_memory has a completely different shape from
                # run_python — handle it as an early-return branch so the
                # default path below stays clean.
                if tool_name == "save_memory":
                    inp = block_dict.get("input", {}) or {}
                    try:
                        memory.save_memory(
                            content=inp.get("content", ""),
                            category=inp.get("category", "fact"),
                            rationale=inp.get("rationale", ""),
                        )
                        status.write(
                            f"  ↳ Saved to memory: '{inp.get('content', '')[:80]}'"
                        )
                        result_text = (
                            f"Memory entry saved. Next conversation will see it as "
                            f"part of the loaded context."
                        )
                    except Exception as e:  # noqa: BLE001
                        status.write(f"  ↳ Memory save failed: {e}")
                        result_text = f"Error saving memory: {e}"
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block_dict["id"],
                            "content": result_text,
                        }
                    )
                    continue

                # Default path: run_python
                code = block_dict["input"].get("code", "")
                code_lines = code.count("\n") + 1
                status.write(f"  ↳ Running tool ({code_lines} lines of code)…")

                t0 = time.time()
                result = tools.execute(code, sandbox)
                exec_dur = time.time() - t0

                # Save the figure / table and build the tool result FIRST.
                # The message-history bookkeeping has to happen no matter
                # what, so we don't let logging come between execution and
                # storage.
                if result.fig is not None:
                    st.session_state.figures[block_dict["id"]] = result.fig
                if result.table is not None:
                    st.session_state.tables[block_dict["id"]] = result.table

                result_text = result.to_tool_result_text()
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": block_dict["id"],
                    "content": result_text,
                }
                tool_results.append(tool_result_block)

                # Now log to the status panel. Wrapped in try/except so a
                # logging bug can never break the conversation turn — we'd
                # rather have a quiet log than a crashed app.
                try:
                    if result.error:
                        last_err = (
                            result.error.strip().splitlines()[-1]
                            if result.error.strip()
                            else "unknown"
                        )
                        status.write(
                            f"  ↳ Tool **errored** in {exec_dur:.1f}s: `{last_err}`"
                        )
                    else:
                        info = (
                            _safe_chart_info(result.fig)
                            if result.fig is not None
                            else "no chart"
                        )
                        status.write(
                            f"  ↳ Tool OK in **{exec_dur:.1f}s** · "
                            f"stdout {len(result.stdout)} chars · {info}"
                        )
                except Exception:  # noqa: BLE001
                    pass  # logging failures must never break the turn

                _render_block_in_assistant_bubble(
                    tool_result_block, key_suffix=f"live_{iteration}"
                )

            if tool_results:
                st.session_state.messages.append(
                    {"role": "user", "content": tool_results}
                )

            # Done when Claude stops calling tools.
            if response.stop_reason == "end_turn":
                total_dur = time.time() - turn_start
                status.update(
                    label=(
                        f"Done in {total_dur:.1f}s · "
                        f"{iteration} iteration{'' if iteration == 1 else 's'}"
                    ),
                    state="complete",
                    expanded=False,
                )
                # Save-as-report + Copy buttons at the bottom of the turn.
                # Scoped to THIS turn only — find the last plain-text user
                # message and treat everything after it as this exchange's
                # assistant content.
                last_user_idx = max(
                    (
                        idx for idx, m in enumerate(st.session_state.messages)
                        if m["role"] == "user" and isinstance(m.get("content"), str)
                    ),
                    default=-1,
                )
                if last_user_idx >= 0:
                    this_question = st.session_state.messages[last_user_idx]["content"]
                    this_turn_blocks: list[dict] = []
                    this_turn_text_parts: list[str] = []
                    for m in st.session_state.messages[last_user_idx + 1:]:
                        if isinstance(m.get("content"), list):
                            this_turn_blocks.extend(m["content"])
                            if m["role"] == "assistant":
                                this_turn_text_parts.append(_assistant_text_for_copy(m["content"]))
                    _render_exchange_action_row(
                        question=this_question,
                        assistant_blocks=this_turn_blocks,
                        copy_text="\n\n".join(p for p in this_turn_text_parts if p),
                        key_suffix=f"live_{last_user_idx}",
                    )
                return

        # The loop only exits via `return` (end_turn or stop_requested).
        # No iteration cap — anything else would be unreachable code.


# ---------------------------------------------------------------------------
# Main content — switched by the sidebar navigation
# ---------------------------------------------------------------------------
current_view = st.session_state.view

if current_view == VIEW_CHAT:
    # Compact scope picker at the top — "Working with N of M data files".
    _render_scope_picker()

    # Chat-request knobs that used to live in the sidebar — moved here
    # because they're only relevant when you're about to ask something,
    # and the sidebar already carries a lot. Use narrow columns so the
    # toggles sit next to each other on the left, rather than stretched
    # across half the page each.
    _qm_col, _pc_col, _ws_col, _spacer = st.columns([1, 1, 1, 5])
    with _qm_col:
        st.toggle(
            "Quick mode",
            key="quick_mode",
            help=(
                "When on, the next request uses Claude Haiku regardless of the "
                "model in Settings. Best for simple lookups; switch off for "
                "deeper analysis."
            ),
        )
    with _pc_col:
        st.toggle(
            "Prefer charts",
            key="prefer_charts",
            help=(
                "When on, Snoop produces a chart whenever your answer has 3+ "
                "values worth comparing — even if you didn't explicitly ask."
            ),
            on_change=save_settings,
        )
    with _ws_col:
        st.toggle(
            "Web search",
            key="web_search_enabled",
            help=(
                "When on, Snoop can search the web for external info "
                "(exchange rates, industry benchmarks, news, regulations). "
                "Off by default — costs extra per request and most finance "
                "questions are answered from your own data. Web fetch (for "
                "a specific URL) is always available."
            ),
            on_change=save_settings,
        )

    render_history()

    # If chat_input fired on the previous run, the prompt was stashed in
    # session state. Process it now so the response renders below history.
    pending = st.session_state.pop("pending_prompt", None)
    if pending:
        client = anthropic.Anthropic(api_key=st.session_state.api_key)
        run_agent_turn(client, pending)
        # Force a rerun once the turn finishes so the sidebar's session
        # usage meter picks up the freshly-updated counters. The sidebar
        # rendered at the top of THIS run with the pre-turn state — without
        # a rerun, the meter stays one turn behind. The chat itself
        # re-renders identically via render_history on the next pass.
        st.rerun()

    # Saved questions dropdown — sits at the bottom of the chat view's
    # content, which Streamlit puts visually just above the pinned chat
    # input.
    if st.session_state.get("saved_questions"):
        def _queue_saved(q: str) -> None:
            st.session_state.pending_prompt = q
            st.session_state.view = VIEW_CHAT

        saved_questions.render_dropdown(_queue_saved)

elif current_view == VIEW_SAVED:
    saved_questions.render(persist_saved_questions)

elif current_view == VIEW_ITEMS:
    saved_items.render()

elif current_view == VIEW_CHARTS:
    chart_demo.render()

elif current_view == VIEW_DATA:
    data_browser.render()

elif current_view == VIEW_CONTEXT:
    context_browser.render()

elif current_view == VIEW_INTEGRATIONS:
    render_integrations_view()

elif current_view == VIEW_SETTINGS:
    render_settings_view()

# ---------------------------------------------------------------------------
# Chat input — at top level so Streamlit pins it to the viewport bottom.
# Only rendered on the Ask the Snoop view; other views (Data, Saved Items,
# etc.) get a clean page without a chat box at the bottom.
# ---------------------------------------------------------------------------
if current_view == VIEW_CHAT:
    if prompt := st.chat_input("Ask the Snoop about your data..."):
        if not st.session_state.api_key:
            st.error("Please add your Anthropic API key in Settings to start.")
        else:
            st.session_state.pending_prompt = prompt
            st.rerun()

